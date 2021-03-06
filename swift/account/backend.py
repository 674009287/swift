# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Pluggable Back-end for Account Server
"""

from __future__ import with_statement
import os
from uuid import uuid4
import time
import cPickle as pickle
import errno

import sqlite3

from swift.common.utils import normalize_timestamp, lock_parent_directory
from swift.common.db import DatabaseBroker, DatabaseConnectionError, \
    PENDING_CAP, PICKLE_PROTOCOL, utf8encode


class AccountBroker(DatabaseBroker):
    """Encapsulates working with an account database."""
    db_type = 'account'
    db_contains_type = 'container'
    db_reclaim_timestamp = 'delete_timestamp'

    def _initialize(self, conn, put_timestamp):
        """
        Create a brand new account database (tables, indices, triggers, etc.)

        :param conn: DB connection object
        :param put_timestamp: put timestamp
        """
        if not self.account:
            raise ValueError(
                'Attempting to create a new database with no account set')
        self.create_container_table(conn)
        self.create_account_stat_table(conn, put_timestamp)

    def create_container_table(self, conn):
        """
        Create container table which is specific to the account DB.

        :param conn: DB connection object
        """
        conn.executescript("""
            CREATE TABLE container (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                put_timestamp TEXT,
                delete_timestamp TEXT,
                object_count INTEGER,
                bytes_used INTEGER,
                deleted INTEGER DEFAULT 0
            );

            CREATE INDEX ix_container_deleted_name ON
                container (deleted, name);

            CREATE TRIGGER container_insert AFTER INSERT ON container
            BEGIN
                UPDATE account_stat
                SET container_count = container_count + (1 - new.deleted),
                    object_count = object_count + new.object_count,
                    bytes_used = bytes_used + new.bytes_used,
                    hash = chexor(hash, new.name,
                                  new.put_timestamp || '-' ||
                                    new.delete_timestamp || '-' ||
                                    new.object_count || '-' || new.bytes_used);
            END;

            CREATE TRIGGER container_update BEFORE UPDATE ON container
            BEGIN
                SELECT RAISE(FAIL, 'UPDATE not allowed; DELETE and INSERT');
            END;


            CREATE TRIGGER container_delete AFTER DELETE ON container
            BEGIN
                UPDATE account_stat
                SET container_count = container_count - (1 - old.deleted),
                    object_count = object_count - old.object_count,
                    bytes_used = bytes_used - old.bytes_used,
                    hash = chexor(hash, old.name,
                                  old.put_timestamp || '-' ||
                                    old.delete_timestamp || '-' ||
                                    old.object_count || '-' || old.bytes_used);
            END;
        """)

    def create_account_stat_table(self, conn, put_timestamp):
        """
        Create account_stat table which is specific to the account DB.
        Not a part of Pluggable Back-ends, internal to the baseline code.

        :param conn: DB connection object
        :param put_timestamp: put timestamp
        """
        conn.executescript("""
            CREATE TABLE account_stat (
                account TEXT,
                created_at TEXT,
                put_timestamp TEXT DEFAULT '0',
                delete_timestamp TEXT DEFAULT '0',
                container_count INTEGER,
                object_count INTEGER DEFAULT 0,
                bytes_used INTEGER DEFAULT 0,
                hash TEXT default '00000000000000000000000000000000',
                id TEXT,
                status TEXT DEFAULT '',
                status_changed_at TEXT DEFAULT '0',
                metadata TEXT DEFAULT ''
            );

            INSERT INTO account_stat (container_count) VALUES (0);
        """)

        conn.execute('''
            UPDATE account_stat SET account = ?, created_at = ?, id = ?,
                   put_timestamp = ?
            ''', (self.account, normalize_timestamp(time.time()), str(uuid4()),
                  put_timestamp))

    def get_db_version(self, conn):
        if self._db_version == -1:
            self._db_version = 0
            for row in conn.execute('''
                    SELECT name FROM sqlite_master
                    WHERE name = 'ix_container_deleted_name' '''):
                self._db_version = 1
        return self._db_version

    def _delete_db(self, conn, timestamp, force=False):
        """
        Mark the DB as deleted.

        :param conn: DB connection object
        :param timestamp: timestamp to mark as deleted
        """
        conn.execute("""
            UPDATE account_stat
            SET delete_timestamp = ?,
                status = 'DELETED',
                status_changed_at = ?
            WHERE delete_timestamp < ? """, (timestamp, timestamp, timestamp))

    def _commit_puts_load(self, item_list, entry):
        """See :func:`swift.common.db.DatabaseBroker._commit_puts_load`"""
        (name, put_timestamp, delete_timestamp,
         object_count, bytes_used, deleted) = \
            pickle.loads(entry.decode('base64'))
        item_list.append(
            {'name': name,
             'put_timestamp': put_timestamp,
             'delete_timestamp': delete_timestamp,
             'object_count': object_count,
             'bytes_used': bytes_used,
             'deleted': deleted})

    def empty(self):
        """
        Check if the account DB is empty.

        :returns: True if the database has no active containers.
        """
        self._commit_puts_stale_ok()
        with self.get() as conn:
            row = conn.execute(
                'SELECT container_count from account_stat').fetchone()
            return (row[0] == 0)

    def put_container(self, name, put_timestamp, delete_timestamp,
                      object_count, bytes_used):
        """
        Create a container with the given attributes.

        :param name: name of the container to create
        :param put_timestamp: put_timestamp of the container to create
        :param delete_timestamp: delete_timestamp of the container to create
        :param object_count: number of objects in the container
        :param bytes_used: number of bytes used by the container
        """
        if delete_timestamp > put_timestamp and \
                object_count in (None, '', 0, '0'):
            deleted = 1
        else:
            deleted = 0
        record = {'name': name, 'put_timestamp': put_timestamp,
                  'delete_timestamp': delete_timestamp,
                  'object_count': object_count,
                  'bytes_used': bytes_used,
                  'deleted': deleted}
        if self.db_file == ':memory:':
            self.merge_items([record])
            return
        if not os.path.exists(self.db_file):
            raise DatabaseConnectionError(self.db_file, "DB doesn't exist")
        pending_size = 0
        try:
            pending_size = os.path.getsize(self.pending_file)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
        if pending_size > PENDING_CAP:
            self._commit_puts([record])
        else:
            with lock_parent_directory(self.pending_file,
                                       self.pending_timeout):
                with open(self.pending_file, 'a+b') as fp:
                    # Colons aren't used in base64 encoding; so they are our
                    # delimiter
                    fp.write(':')
                    fp.write(pickle.dumps(
                        (name, put_timestamp, delete_timestamp, object_count,
                         bytes_used, deleted),
                        protocol=PICKLE_PROTOCOL).encode('base64'))
                    fp.flush()

    def is_deleted(self):
        """
        Check if the account DB is considered to be deleted.

        :returns: True if the account DB is considered to be deleted, False
                  otherwise
        """
        if self.db_file != ':memory:' and not os.path.exists(self.db_file):
            return True
        self._commit_puts_stale_ok()
        with self.get() as conn:
            row = conn.execute('''
                SELECT put_timestamp, delete_timestamp, container_count, status
                FROM account_stat''').fetchone()
            return row['status'] == 'DELETED' or (
                row['container_count'] in (None, '', 0, '0') and
                row['delete_timestamp'] > row['put_timestamp'])

    def is_status_deleted(self):
        """Only returns true if the status field is set to DELETED."""
        with self.get() as conn:
            row = conn.execute('''
                SELECT status
                FROM account_stat''').fetchone()
            return (row['status'] == "DELETED")

    def get_info(self):
        """
        Get global data for the account.

        :returns: dict with keys: account, created_at, put_timestamp,
                  delete_timestamp, container_count, object_count,
                  bytes_used, hash, id
        """
        self._commit_puts_stale_ok()
        with self.get() as conn:
            return dict(conn.execute('''
                SELECT account, created_at,  put_timestamp, delete_timestamp,
                       container_count, object_count, bytes_used, hash, id
                FROM account_stat
            ''').fetchone())

    def list_containers_iter(self, limit, marker, end_marker, prefix,
                             delimiter):
        """
        Get a list of containers sorted by name starting at marker onward, up
        to limit entries. Entries will begin with the prefix and will not have
        the delimiter after the prefix.

        :param limit: maximum number of entries to get
        :param marker: marker query
        :param end_marker: end marker query
        :param prefix: prefix query
        :param delimiter: delimiter for query

        :returns: list of tuples of (name, object_count, bytes_used, 0)
        """
        (marker, end_marker, prefix, delimiter) = utf8encode(
            marker, end_marker, prefix, delimiter)
        self._commit_puts_stale_ok()
        if delimiter and not prefix:
            prefix = ''
        orig_marker = marker
        with self.get() as conn:
            results = []
            while len(results) < limit:
                query = """
                    SELECT name, object_count, bytes_used, 0
                    FROM container
                    WHERE deleted = 0 AND """
                query_args = []
                if end_marker:
                    query += ' name < ? AND'
                    query_args.append(end_marker)
                if marker and marker >= prefix:
                    query += ' name > ? AND'
                    query_args.append(marker)
                elif prefix:
                    query += ' name >= ? AND'
                    query_args.append(prefix)
                if self.get_db_version(conn) < 1:
                    query += ' +deleted = 0'
                else:
                    query += ' deleted = 0'
                query += ' ORDER BY name LIMIT ?'
                query_args.append(limit - len(results))
                curs = conn.execute(query, query_args)
                curs.row_factory = None

                if prefix is None:
                    # A delimiter without a specified prefix is ignored
                    return [r for r in curs]
                if not delimiter:
                    if not prefix:
                        # It is possible to have a delimiter but no prefix
                        # specified. As above, the prefix will be set to the
                        # empty string, so avoid performing the extra work to
                        # check against an empty prefix.
                        return [r for r in curs]
                    else:
                        return [r for r in curs if r[0].startswith(prefix)]

                # We have a delimiter and a prefix (possibly empty string) to
                # handle
                rowcount = 0
                for row in curs:
                    rowcount += 1
                    marker = name = row[0]
                    if len(results) >= limit or not name.startswith(prefix):
                        curs.close()
                        return results
                    end = name.find(delimiter, len(prefix))
                    if end > 0:
                        marker = name[:end] + chr(ord(delimiter) + 1)
                        dir_name = name[:end + 1]
                        if dir_name != orig_marker:
                            results.append([dir_name, 0, 0, 1])
                        curs.close()
                        break
                    results.append(row)
                if not rowcount:
                    break
            return results

    def merge_items(self, item_list, source=None):
        """
        Merge items into the container table.

        :param item_list: list of dictionaries of {'name', 'put_timestamp',
                          'delete_timestamp', 'object_count', 'bytes_used',
                          'deleted'}
        :param source: if defined, update incoming_sync with the source
        """
        with self.get() as conn:
            max_rowid = -1
            for rec in item_list:
                record = [rec['name'], rec['put_timestamp'],
                          rec['delete_timestamp'], rec['object_count'],
                          rec['bytes_used'], rec['deleted']]
                query = '''
                    SELECT name, put_timestamp, delete_timestamp,
                           object_count, bytes_used, deleted
                    FROM container WHERE name = ?
                '''
                if self.get_db_version(conn) >= 1:
                    query += ' AND deleted IN (0, 1)'
                curs = conn.execute(query, (rec['name'],))
                curs.row_factory = None
                row = curs.fetchone()
                if row:
                    row = list(row)
                    for i in xrange(5):
                        if record[i] is None and row[i] is not None:
                            record[i] = row[i]
                    if row[1] > record[1]:  # Keep newest put_timestamp
                        record[1] = row[1]
                    if row[2] > record[2]:  # Keep newest delete_timestamp
                        record[2] = row[2]
                    # If deleted, mark as such
                    if record[2] > record[1] and \
                            record[3] in (None, '', 0, '0'):
                        record[5] = 1
                    else:
                        record[5] = 0
                conn.execute('''
                    DELETE FROM container WHERE name = ? AND
                                                deleted IN (0, 1)
                ''', (record[0],))
                conn.execute('''
                    INSERT INTO container (name, put_timestamp,
                        delete_timestamp, object_count, bytes_used,
                        deleted)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', record)
                if source:
                    max_rowid = max(max_rowid, rec['ROWID'])
            if source:
                try:
                    conn.execute('''
                        INSERT INTO incoming_sync (sync_point, remote_id)
                        VALUES (?, ?)
                    ''', (max_rowid, source))
                except sqlite3.IntegrityError:
                    conn.execute('''
                        UPDATE incoming_sync SET sync_point=max(?, sync_point)
                        WHERE remote_id=?
                    ''', (max_rowid, source))
            conn.commit()
