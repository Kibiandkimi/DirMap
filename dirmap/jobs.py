"""Small durable queue with a single OS-locked worker per data directory.

SQLite stores serializable inputs and results. Recovery requires obtaining
the exclusive worker lock: a slow job is never stolen because of a timeout.
The handler reconciles its atomic commit receipt before replaying any work.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
import uuid


log = logging.getLogger(__name__)


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class JobQueue:
    def __init__(self, data, handler, poll_sec=.25):
        self.root = Path(data)/'jobs'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root/'queue.sqlite3'
        self.handler = handler
        self.poll_sec = poll_sec
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        with self.database() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, asset_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    payload TEXT NOT NULL, inputs TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed')),
                    progress REAL NOT NULL DEFAULT 0, message TEXT, error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, recovered INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
                    result TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_input ON jobs(input_hash)
                    WHERE state IN ('queued','running');
                CREATE INDEX IF NOT EXISTS job_order ON jobs(state, created_at);
                CREATE TABLE IF NOT EXISTS submissions (
                    request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    kind TEXT NOT NULL, asset_id TEXT NOT NULL, payload TEXT NOT NULL
                );
            ''')

    @contextmanager
    def database(self):
        # Rollback journaling keeps this tiny local database self-contained.
        # No connection is shared across threads or inherited across forks.
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def public(row, with_result=True):
        if row is None:
            return None
        result = {'id': row['id'], 'kind': row['kind'], 'assetId': row['asset_id'],
                  'state': row['state'], 'progress': row['progress'], 'message': row['message'],
                  'createdAt': row['created_at'], 'startedAt': row['started_at'],
                  'finishedAt': row['finished_at'], 'attempts': row['attempts'],
                  'recovered': bool(row['recovered'])}
        if row['error']:
            result['error'] = row['error']
        if with_result and row['result'] is not None:
            result['result'] = json.loads(row['result'])
        return result

    def get(self, job_id):
        with self.database() as db:
            return self.public(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def by_request(self, request_id, kind=None, asset_id=None, payload=None):
        if not request_id:
            return None
        with self.database() as db:
            entry = db.execute('SELECT * FROM submissions WHERE request_id=?', (request_id,)).fetchone()
            if not entry:
                return None
            if kind is not None and (entry['kind'], entry['asset_id'], entry['payload']) != (kind, asset_id, encode(payload)):
                raise ValueError('同一提交标识不能用于不同的任务')
            return self.public(db.execute('SELECT * FROM jobs WHERE id=?', (entry['job_id'],)).fetchone())

    def recent(self, asset_id=None, limit=20):
        with self.database() as db:
            clause, args = (' WHERE asset_id=?', [asset_id]) if asset_id else ('', [])
            rows = db.execute('SELECT * FROM jobs'+clause+
                              " ORDER BY state IN ('queued','running') DESC, created_at DESC LIMIT ?",
                              [*args, min(200, max(1, limit))]).fetchall()
            return [self.public(row, with_result=False) for row in rows]

    def submit(self, kind, asset_id, payload, inputs, request_id=None):
        previous = self.by_request(request_id, kind, asset_id, payload)
        if previous:
            return previous
        fingerprint = hashlib.sha256(encode([kind, asset_id, payload, inputs]).encode()).hexdigest()
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            # Recheck inside the transaction: two server processes can accept
            # the same network retry at precisely the same time.
            submission = db.execute('SELECT * FROM submissions WHERE request_id=?', (request_id,)).fetchone()
            if submission:
                if (submission['kind'], submission['asset_id'], submission['payload']) != (kind, asset_id, encode(payload)):
                    raise ValueError('同一提交标识不能用于不同的任务')
                row = db.execute('SELECT * FROM jobs WHERE id=?', (submission['job_id'],)).fetchone()
            else:
                row = db.execute("SELECT * FROM jobs WHERE input_hash=? AND state IN ('queued','running')", (fingerprint,)).fetchone()
                if row is None:
                    job_id = uuid.uuid4().hex[:16]
                    db.execute('''INSERT INTO jobs(id,kind,asset_id,input_hash,payload,inputs,state,created_at)
                                  VALUES(?,?,?,?,?,?,'queued',?)''',
                               (job_id, kind, asset_id, fingerprint, encode(payload), encode(inputs), time.time()))
                    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if request_id:
                    db.execute('INSERT INTO submissions VALUES(?,?,?,?,?)',
                               (request_id, row['id'], kind, asset_id, encode(payload)))
            db.execute('COMMIT')
        self.wake.set()
        return self.public(row)

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.serve, name='dirmap-durable-worker', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join()

    def recover(self):
        # Called only while holding the exclusive live worker descriptor.
        with self.database() as db:
            db.execute("""UPDATE jobs SET state='queued', recovered=1, progress=0,
                          message='服务恢复，正在核对原任务的提交结果'
                          WHERE state='running'""")

    def claim(self):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row:
                db.execute("""UPDATE jobs SET state='running', attempts=attempts+1,
                              started_at=?, progress=0 WHERE id=?""", (time.time(), row['id']))
                row = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            db.execute('COMMIT')
            return row

    def execute(self, row):
        def progress(percent, message):
            with self.database() as db:
                db.execute("UPDATE jobs SET progress=?,message=? WHERE id=? AND state='running'",
                           (max(0, min(100, float(percent))), str(message), row['id']))
        envelope = {'id': row['id'], 'kind': row['kind'], 'assetId': row['asset_id'],
                    'payload': json.loads(row['payload']), 'inputs': json.loads(row['inputs']),
                    'attempts': row['attempts'], 'recovered': bool(row['recovered'])}
        try:
            result = encode(self.handler(envelope, progress))
        except Exception as exc:
            with self.database() as db:
                db.execute("""UPDATE jobs SET state='failed',message=?,error=?,finished_at=? WHERE id=?""",
                           (str(exc), type(exc).__name__, time.time(), row['id']))
        else:
            with self.database() as db:
                db.execute("""UPDATE jobs SET state='succeeded',progress=100,result=?,
                              message='处理完成',finished_at=? WHERE id=?""", (result, time.time(), row['id']))

    def serve(self):
        while not self.stop.is_set():
            with (self.root/'worker.lock').open('a+b') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.stop.wait(self.poll_sec)
                    continue
                try:
                    self.recover()
                    while not self.stop.is_set():
                        row = self.claim()
                        if row:
                            self.execute(row)
                        else:
                            self.wake.wait(self.poll_sec)
                            self.wake.clear()
                except Exception:
                    # A database/filesystem failure does not erase a running
                    # job. Reacquire the lock and reconcile it on recovery.
                    log.exception('Task queue interrupted; retained durable job state')
            self.stop.wait(self.poll_sec)
