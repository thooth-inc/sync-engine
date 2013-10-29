import logging as log

import json
import postel
from bson import json_util
from models import db_session, MessageMeta, FolderMeta, SharedFolderNSMeta
from models import Namespace, User, IMAPAccount, TodoNSMeta, TodoItem

from ..util.html import plaintext2html

from sqlalchemy.orm import joinedload

import zerorpc
import os

class NSAuthError(Exception):
    pass

def namespace_auth(fn):
    """
    decorator that checks whether user has permissions to access namespace
    """
    from functools import wraps
    @wraps(fn)
    def namespace_auth_fn(self, user_id, namespace_id, *args, **kwargs):
        self.user_id = user_id
        self.namespace_id = namespace_id
        user = db_session.query(User).filter_by(id=user_id).join(IMAPAccount).one()
        for account in user.accounts:
            if account.namespace.id == namespace_id:
                self.namespace = account.namespace
                return fn(self, *args, **kwargs)

        shared_nses = db_session.query(SharedFolderNSMeta)\
                .filter(SharedFolderNSMeta.user_id == user_id)
        for shared_ns in shared_nses:
            if shared_ns.id == namespace_id:
                return fn(self, *args, **kwargs)

        raise NSAuthError("User '{0}' does not have access to namespace '{1}'".format(user_id, namespace_id))

    return namespace_auth_fn

def jsonify(fn):
    """ decorator that JSONifies a function's return value """
    def wrapper(*args, **kwargs):
        ret = fn(*args, **kwargs)
        return json.dumps(ret, default=json_util.default) # fixes serializing date.datetime
    return wrapper

# should this be moved to model.py or similar?
def get_or_create_todo_namespace(user_id):
    user = db_session.query(User).join(TodoNSMeta, Namespace, TodoItem) \
            .get(user_id)
    if user.todo_ns_meta is not None:
        return user.todo_ns_meta.namespace

    # create a todo namespace
    todo_ns = Namespace(imapaccount_id=None, namespace_type='todo')
    db_session.add(todo_ns)
    db_session.commit()

    todo_ns_meta = TodoNSMeta(namespace_id=todo_ns.id, user_id=user_id)
    db_session.add(todo_ns_meta)
    db_session.commit()

    log.info('todo namespace id {0}'.format(todo_ns.id))
    return todo_ns

class API(object):

    _zmq_search = None
    @property
    def z_search(self):
        """ Proxy function for the ZeroMQ search service. """
        if not self._zmq_search:
            self._zmq_search = zerorpc.Client(
                    os.environ.get('SEARCH_SERVER_LOC', None))
        return self._zmq_search.search

    @jsonify
    def sync_status(self):
        """ Returns data representing the status of all syncing users, like:

            user_id: {
                state: 'initial sync',
                stored_data: '12127227',
                stored_messages: '50000',
                status: '56%',
            }
            user_id: {
                state: 'poll',
                stored_data: '1000000000',
                stored_messages: '200000',
                status: '2013-06-08 14:00',
            }
        """
        if not self._sync:
            self._sync = zerorpc.Client(os.environ.get('CRISPIN_SERVER_LOC', None))
        status = self._sync.status()
        user_ids = status.keys()
        users = db_session.query(User).filter(User.id.in_(user_ids))
        for user in users:
            status[user.id]['stored_data'] = user.total_stored_data()
            status[user.id]['stored_messages'] = user.total_stored_messages()
        return status

    @jsonify
    def search_folder(self, namespace_id, search_query):
        results = self.z_search(namespace_id, search_query)
        meta_ids = [r[0] for r in results]
        return meta_ids

    @namespace_auth
    @jsonify
    def messages_for_folder(self, folder_name):
        """ Returns all messages in a given folder.

            Note that this may be more messages than included in the IMAP
            folder, since we fetch the full thread if one of the messages is in
            the requested folder.
        """
        assert self.namespace.is_root, "get_messages is only defined on root namespaces"

        # Get all thread IDs for all messages in this folder.
        imapaccount_id = self.namespace.imapaccount_id
        all_msgids = db_session.query(FolderMeta.messagemeta_id)\
              .filter(FolderMeta.folder_name == folder_name,
                      FolderMeta.imapaccount_id == imapaccount_id)
        all_thrids = set()
        for thrid, in db_session.query(MessageMeta.g_thrid).filter(
                MessageMeta.namespace_id == self.namespace_id,
                MessageMeta.id.in_(all_msgids)):
            all_thrids.add(thrid)

        # Get all messages for those thread IDs
        messages = []
        from .util.itert import chunk
        DB_CHUNK_SIZE = 100
        for g_thrids in chunk(list(all_thrids), DB_CHUNK_SIZE):
            all_msgs_query = db_session.query(MessageMeta).filter(
                    MessageMeta.namespace_id == self.namespace_id,
                    MessageMeta.g_thrid.in_(g_thrids))
            messages += all_msgs_query.all()


        log.info('found {0} message IDs'.format(len(messages)))
        return [m.cereal() for m in messages]

    @jsonify
    def messages_with_ids(self, namespace_id, msg_ids):
        """ Returns MessageMeta objects for the given msg_ids """
        all_msgs_query = db_session.query(MessageMeta)\
            .filter(MessageMeta.id.in_(msg_ids), 
                    MessageMeta.namespace_id == namespace_id)
        all_msgs = all_msgs_query.all()

        log.info('found %i messages IDs' % len(all_msgs))
        return [m.cereal() for m in all_msgs]

    def send_mail(self, namespace_id, recipients, subject, body):
        """ Sends a message with the given objects """

        account = db_session.query(IMAPAccount).join(Namespace).filter(
                Namespace.id==namespace_id).one()
        with postel.SMTP(account) as smtp:
            smtp.send_mail(recipients, subject, body)
        return "OK"

    @namespace_auth
    @jsonify
    def meta_with_id(self, data_id):
        existing_msgs_query = db_session.query(MessageMeta).join(Namespace)\
                .filter(MessageMeta.g_msgid == data_id, Namespace.id == self.namespace_id)\
                .options(joinedload("parts"))
        meta = existing_msgs_query.all()
        if not len(meta) == 1:
            log.error("messagemeta query returned %i results" % len(meta))
        if len(meta) == 0: return []
        m = meta[0]

        parts = m.parts

        return [p.cereal() for p in parts]

    @namespace_auth
    @jsonify
    def body_for_messagemeta(self, meta_id):
        message_meta = db_session.query(MessageMeta).filter_by(id=meta_id).one()
        parts = message_meta.parts
        plain_data = None
        html_data = None

        for part in parts:
            if part.content_type == 'text/html':
                html_data = part.get_data()
                break
        for part in parts:
            if part.content_type == 'text/plain':
                plain_data = part.get_data()
                break

        prettified = None
        if html_data:
            if 'font:' in html_data or 'font-face:' in html_data or 'font-family:' in html_data:
                prettified = html_data
            else:
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "message_template.html")
                with open(path, 'r') as f:
                    # template has %s in it. can't do format because python misinterprets css
                    prettified = f.read() % html_data
        else:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "message_template.html")
            with open(path, 'r') as f:
                prettified = f.read() % plaintext2html(plain_data)

        return {'data': prettified}

    @jsonify
    def top_level_namespaces(self, user_id):
        """for the user, get the namespaces for all the accounts associated as well as all the shared folder metas
        returns a list of tuples of display name, type, and id"""
        nses = {'private': [], 'shared': []}

        user = db_session.query(User).join(IMAPAccount).get(user_id)
        for account in user.accounts:
            account_ns = account.namespace
            nses['private'].append(account_ns.cereal())

        shared_nses = db_session.query(SharedFolderNSMeta)\
                .filter(SharedFolderNSMeta.user_id == user_id)
        for shared_ns in shared_nses:
            nses['shared'].append(shared_ns.cereal())

        return nses

    @jsonify
    def todo_items(self, user_id):
        todo_ns = get_or_create_todo_namespace(user_id)
        todo_items = todo_ns.todo_items
        return [i.cereal() for i in todo_items]

    @namespace_auth
    def create_todo(self, g_thrid):
        log.info('creating todo from namespace {0} g_thrid {1}'.format(self.namespace_id, g_thrid))

        # TODO abstract this logic out
        messages_in_thread = db_session.query(MessageMeta).filter(
                MessageMeta.namespace_id == self.namespace_id,
                MessageMeta.g_thrid == g_thrid).all()

        todo_ns = get_or_create_todo_namespace(self.user_id)
        for message in messages_in_thread:
            message.namespace_id = todo_ns.id

        todo_item = TodoItem(
                g_thrid = g_thrid,
                imapaccount_id = self.namespace.imapaccount_id,
                namespace_id = todo_ns.id,
                display_name = messages_in_thread[0].subject,
                due_date = 'Soon',
                date_completed = None,
                sort_index = 0,
            )
        db_session.add(todo_item)
        db_session.commit()
        return "OK"

    def start_sync(self, namespace_id):
        """ Talk to the Sync service and have it launch a sync. """
        pass

