"""Host-local single-writer versioned state. No external adapters."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


class StateError(ValueError):
    pass


class WriterBusy(StateError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def local_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise StateError('path must be absolute and host-local')
    resolved = path.resolve()
    for shared in (Path.home() / 'workbench', Path.home() / 'Documents/workbench'):
        if resolved.is_relative_to(shared.resolve()):
            raise StateError('runtime state/build/backup may not enter shared workbench')
    return path


def atomic_file(path, data):
    temporary = path.with_name(path.name + '.next')
    with temporary.open('wb') as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


SCHEMA = """
CREATE TABLE work (
 id TEXT PRIMARY KEY, task TEXT NOT NULL, next_action TEXT NOT NULL,
 resume_at TEXT, blocker TEXT, note TEXT NOT NULL,
 completed INTEGER NOT NULL CHECK(completed IN (0,1)));
CREATE TABLE history (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
 work_id TEXT, event TEXT NOT NULL);
CREATE TABLE wakes (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, status TEXT NOT NULL,
 checkpoint TEXT NOT NULL);
CREATE TABLE memory_versions (
 version INTEGER PRIMARY KEY, lo INTEGER NOT NULL, hi INTEGER NOT NULL,
 content TEXT NOT NULL, sha256 TEXT NOT NULL);
PRAGMA user_version=1;
"""


SCHEMA_V2 = """
CREATE TABLE changes (id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
 verb TEXT NOT NULL, summary TEXT NOT NULL, undo_of TEXT);
CREATE TABLE jobs (name TEXT NOT NULL, version INTEGER NOT NULL, change_id TEXT NOT NULL,
 spec_json TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(name,version));
CREATE TABLE runs (run_id TEXT NOT NULL, version INTEGER NOT NULL, change_id TEXT NOT NULL,
 job TEXT NOT NULL, state TEXT NOT NULL, planned_for TEXT NOT NULL, reason TEXT NOT NULL,
 started_at TEXT, ended_at TEXT, unit TEXT, exit_code INTEGER, manifest TEXT,
 spec_json TEXT NOT NULL, PRIMARY KEY(run_id,version));
CREATE TABLE told (key TEXT PRIMARY KEY, at TEXT NOT NULL, job TEXT, run_id TEXT,
 topic TEXT NOT NULL, message_id INTEGER NOT NULL);
CREATE TABLE outbox (id TEXT PRIMARY KEY, change_id TEXT NOT NULL, topic TEXT NOT NULL,
 kind TEXT NOT NULL, text TEXT NOT NULL, told_key TEXT, state TEXT NOT NULL,
 message_id INTEGER, sent_at TEXT);
ALTER TABLE wakes ADD COLUMN gate_reason TEXT;
ALTER TABLE wakes ADD COLUMN harness_run_id TEXT;
ALTER TABLE wakes ADD COLUMN change_cursor INTEGER NOT NULL DEFAULT 0;
PRAGMA user_version=2;
"""


SCHEMA_V3 = """
CREATE TABLE senses (id INTEGER PRIMARY KEY, name TEXT NOT NULL, at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ok','unknown')), payload TEXT NOT NULL,
 change_id TEXT NOT NULL, event INTEGER NOT NULL);
CREATE INDEX senses_latest ON senses(name,id);
PRAGMA user_version=3;
"""


SCHEMA_V4 = """
ALTER TABLE runs ADD COLUMN cost_minutes REAL;
ALTER TABLE runs ADD COLUMN cost_tokens INTEGER;
PRAGMA user_version=4;
"""


SCHEMA_V5 = """
CREATE TABLE inbox (update_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL,
 thread_id INTEGER, at TEXT NOT NULL, text TEXT NOT NULL, reply_to INTEGER,
 handled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE message_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
ALTER TABLE outbox ADD COLUMN thread_id INTEGER;
ALTER TABLE outbox ADD COLUMN delivery TEXT NOT NULL DEFAULT 'immediate';
CREATE TABLE message_changes (outbox_id TEXT NOT NULL, change_id TEXT NOT NULL,
 PRIMARY KEY(outbox_id,change_id));
PRAGMA user_version=5;
"""


SCHEMA_V6 = """
CREATE TABLE demands (name TEXT NOT NULL, version INTEGER NOT NULL, change_id TEXT NOT NULL,
 spec_json TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(name,version));
CREATE TABLE blocks (event_key TEXT NOT NULL, version INTEGER NOT NULL, change_id TEXT NOT NULL,
 demand TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(event_key,version));
PRAGMA user_version=6;
"""


SCHEMA_V7 = """
CREATE TABLE interests (name TEXT NOT NULL, version INTEGER NOT NULL, change_id TEXT NOT NULL,
 spec_json TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(name,version));
ALTER TABLE outbox ADD COLUMN job TEXT;
ALTER TABLE outbox ADD COLUMN run_id TEXT;
PRAGMA user_version=7;
"""


SCHEMA_V8 = """
DROP TABLE memory_versions;
CREATE TABLE journal_files (day TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE memories (layer TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
 change_id TEXT NOT NULL, content TEXT, PRIMARY KEY(layer,key,version));
CREATE TABLE provocations (week TEXT PRIMARY KEY, number INTEGER NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL);
PRAGMA user_version=8;
"""


SCHEMA_V9 = """
CREATE TABLE tick_skips (skip_id TEXT NOT NULL, version INTEGER NOT NULL,
 change_id TEXT NOT NULL, state TEXT NOT NULL, from_at TEXT NOT NULL,
 to_at TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(skip_id,version));
PRAGMA user_version=9;
"""


SCHEMA_V10 = """
ALTER TABLE inbox ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]';
PRAGMA user_version=10;
"""


SCHEMA_V11 = """
CREATE TABLE pages (slug TEXT PRIMARY KEY, title TEXT NOT NULL, content_type TEXT NOT NULL,
 sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, change_id TEXT NOT NULL, wake_id INTEGER,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, opened_at TEXT,
 opens INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL CHECK(state IN ('live','failed','removed')));
PRAGMA user_version=11;
"""


SCHEMA_V12 = """
CREATE TABLE drafts (change_id TEXT PRIMARY KEY, account TEXT NOT NULL,
 draft_id TEXT, url TEXT, state TEXT NOT NULL);
PRAGMA user_version=12;
"""


SCHEMA_V13 = """
CREATE TABLE outbox_keys (outbox_id TEXT NOT NULL, key TEXT NOT NULL,
 PRIMARY KEY(outbox_id,key));
CREATE INDEX outbox_keys_key ON outbox_keys(key);
INSERT INTO outbox_keys SELECT id, told_key FROM outbox WHERE told_key IS NOT NULL;
UPDATE outbox SET told_key=NULL;
ALTER TABLE pages ADD COLUMN public INTEGER NOT NULL DEFAULT 0 CHECK(public IN (0,1));
PRAGMA user_version=13;
"""


SCHEMA_V14 = """
DROP TABLE IF EXISTS work;
DROP TABLE IF EXISTS history;
PRAGMA user_version=14;
"""


SCHEMA_V15 = """
DROP TABLE IF EXISTS retirements;
PRAGMA user_version=15;
"""


SCHEMA_V16 = """
CREATE TABLE moments (name TEXT NOT NULL, version INTEGER NOT NULL, change_id TEXT NOT NULL,
 start TEXT NOT NULL, deadline TEXT NOT NULL, releases TEXT NOT NULL,
 PRIMARY KEY(name,version));
INSERT INTO changes VALUES ('moments-seed', strftime('%Y-%m-%dT%H:%M:%SZ','now'),
 'migration', 'moment seed', 'Seed the owner moments', NULL);
INSERT INTO moments VALUES ('orientation', 1, 'moments-seed', '07:00', '07:45', '');
INSERT INTO moments VALUES ('day_close', 1, 'moments-seed', '18:30', '19:00', 'digest');
PRAGMA user_version=16;
"""


SCHEMA_V17 = """
ALTER TABLE runs ADD COLUMN retry_of TEXT;
UPDATE runs SET state='refused' WHERE state='skipped';
UPDATE wakes SET status='skipped' WHERE status='idle';
-- Existing message linkage is the reporting boundary, not a second cursor.
-- Suppressed, not sent: no historical edit becomes new owner-facing news.
INSERT INTO changes
 SELECT 'changes-v17-baseline', strftime('%Y-%m-%dT%H:%M:%SZ','now'),
 'migration', 'changes baseline', 'Suppress edits predating release 17', NULL
 WHERE EXISTS (SELECT 1 FROM changes WHERE actor NOT IN ('tick','migration'));
INSERT INTO outbox (id,change_id,topic,kind,text,state,delivery)
 SELECT 'changes-v17-baseline', id, 'Changes', 'change',
 'Edits predating release 17 are not reported.', 'suppressed', 'silent'
 FROM changes WHERE id='changes-v17-baseline';
INSERT INTO message_changes
 SELECT 'changes-v17-baseline', c.id FROM changes c
 WHERE c.actor NOT IN ('tick','migration')
 AND NOT EXISTS (SELECT 1 FROM message_changes m WHERE m.change_id=c.id);
PRAGMA user_version=17;
"""


SCHEMA_V18 = """
-- All non-owner/non-model maintenance uses a neutral actor.
UPDATE changes SET actor='maintenance'
 WHERE actor NOT IN ('owner','model','maintenance','tick','migration');
UPDATE outbox SET state='suppressed' WHERE state IN ('pending','failed')
 AND change_id IN (SELECT id FROM changes WHERE actor='maintenance');
UPDATE message_meta SET key='your_list_slug' WHERE key='your_items_slug';
ALTER TABLE outbox DROP COLUMN kind;
PRAGMA user_version=18;
"""


SCHEMA_V19 = """
CREATE TABLE IF NOT EXISTS subject_showings (
 subject TEXT NOT NULL, outbox_id TEXT NOT NULL, PRIMARY KEY(subject,outbox_id));
CREATE INDEX IF NOT EXISTS message_changes_change ON message_changes(change_id);
CREATE INDEX IF NOT EXISTS blocks_change ON blocks(change_id);
CREATE INDEX IF NOT EXISTS moments_change ON moments(change_id);
PRAGMA user_version=19;
"""


SCHEMA_V20 = """
-- Keep the three v19 indexes: Changes still queries message_changes, blocks
-- and moments by change_id. outbox_reply remains the reply delivery primitive.
DROP TABLE IF EXISTS subject_showings;
DELETE FROM message_meta WHERE key IN ('changes_cursor','changes_waiting')
 OR key GLOB 'change_subjects:*' OR key GLOB 'change_context:*'
 OR key GLOB 'outbox_subjects:*';
PRAGMA user_version=20;
"""


@contextmanager
def writer(root, initialize=False, wait=0):
    """Never delete/replace the lock inode; every local writer uses this lock.

    wait: seconds to wait for the lock. Verbs and jobs wait briefly, because the tick that
    launched them may still hold it; the tick itself never waits (it runs again in 20 min).
    """
    root = local_path(root)
    if initialize:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise StateError('state not initialized')
    with (root / 'writer.lock').open('a+b') as lock:
        os.chmod(root / 'writer.lock', 0o600)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WriterBusy('single writer busy') from None
                time.sleep(0.5)
        dbpath = root / 'state.sqlite'
        exists = dbpath.exists()
        if not exists and not initialize:
            raise StateError('state not initialized')
        db = sqlite3.connect(dbpath, timeout=5)
        os.chmod(dbpath, 0o600)
        db.row_factory = sqlite3.Row
        try:
            if not exists:
                db.executescript(SCHEMA)
            if db.execute('PRAGMA user_version').fetchone()[0] == 1:
                db.executescript('BEGIN;\n' + SCHEMA_V2 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 2:
                db.executescript('BEGIN;\n' + SCHEMA_V3 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 3:
                db.executescript('BEGIN;\n' + SCHEMA_V4 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 4:
                db.executescript('BEGIN;\n' + SCHEMA_V5 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 5:
                db.executescript('BEGIN;\n' + SCHEMA_V6 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 6:
                db.executescript('BEGIN;\n' + SCHEMA_V7 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 7:
                if db.execute('SELECT count(*) FROM memory_versions').fetchone()[0]:
                    raise StateError('legacy memory is not empty; manual migration required')
                db.executescript('BEGIN;\n' + SCHEMA_V8 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 8:
                db.executescript('BEGIN;\n' + SCHEMA_V9 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 9:
                db.executescript('BEGIN;\n' + SCHEMA_V10 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 10:
                db.executescript('BEGIN;\n' + SCHEMA_V11 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 11:
                db.executescript('BEGIN;\n' + SCHEMA_V12 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 12:
                db.executescript('BEGIN;\n' + SCHEMA_V13 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 13:
                db.executescript('BEGIN;\n' + SCHEMA_V14 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 14:
                db.executescript('BEGIN;\n' + SCHEMA_V15 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 15:
                db.executescript('BEGIN;\n' + SCHEMA_V16 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 16:
                db.executescript('BEGIN;\n' + SCHEMA_V17 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 17:
                db.executescript('BEGIN;\n' + SCHEMA_V18 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 18:
                db.executescript('BEGIN;\n' + SCHEMA_V19 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] == 19:
                db.executescript('BEGIN;\n' + SCHEMA_V20 + '\nCOMMIT;')
            if db.execute('PRAGMA user_version').fetchone()[0] != 20:
                raise StateError('unsupported state schema')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            store = Store(root, db)
            from .memory import mirror
            mirror(db)
            yield store
        finally:
            db.close()


class Store:
    def __init__(self, root, db):
        self.root, self.db = root, db

    def backup(self, target):
        target = local_path(target)
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
        destination = sqlite3.connect(target / 'state.sqlite')
        try:
            self.db.backup(destination)
        finally:
            destination.close()
        os.chmod(target / 'state.sqlite', 0o600)
        hashes = {path.name: digest(path.read_bytes()) for path in target.iterdir()}
        atomic_file(target / 'manifest.json', json.dumps({'schema':20,'sha256':hashes}, sort_keys=True).encode())
        return {'status':'backup_complete', 'files':len(hashes)}


def restore(source, target):
    """Isolated fresh destination only; root owns any production restoration."""
    source, target = local_path(source), local_path(target)
    if target.exists():
        raise StateError('restore requires absolute source and fresh isolated target')
    manifest = json.loads((source / 'manifest.json').read_text())
    names = set(manifest.get('sha256', {}))
    if manifest.get('schema') not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20) or names not in ({'state.sqlite'}, {'state.sqlite','memory.md'}):
        raise StateError('invalid backup manifest')
    for name, expected in manifest['sha256'].items():
        if digest((source / name).read_bytes()) != expected:
            raise StateError('backup digest mismatch')
    db = sqlite3.connect(f'file:{source / "state.sqlite"}?mode=ro', uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA user_version').fetchone()[0] not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20):
            raise StateError('backup integrity/schema invalid')
        target.mkdir(mode=0o700, parents=True)
        out = sqlite3.connect(target / 'state.sqlite')
        try:
            db.backup(out)
        finally:
            out.close()
        os.chmod(target / 'state.sqlite', 0o600)
    finally:
        db.close()
    with writer(target) as store:
        from .memory import mirror
        mirror(store.db)
    return {'status':'restored_isolated'}
