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
CANCELLED = 'JobCancelled'


class JobCancelled(Exception):
    """Raised at the next stage boundary after a teacher cancels a job."""


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class JobQueue:
    def __init__(self, data, handler, poll_sec=.25, maintenance=None, maintenance_interval_sec=6*3600):
        self.root = Path(data)/'jobs'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root/'queue.sqlite3'
        self.handler = handler
        self.poll_sec = poll_sec
        self.maintenance = maintenance
        self.maintenance_interval_sec = maintenance_interval_sec
        self.last_maintenance = None
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
            # Older databases gain the cancellation and retention markers.
            # Another server process may be upgrading the same file.
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            for column in ('cancel_requested', 'result_pruned'):
                if column not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
            db.execute('COMMIT')

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
        if row['error'] == CANCELLED:
            result['cancelled'] = True
        elif row['cancel_requested'] and row['state'] in ('queued', 'running'):
            result['cancelRequested'] = True
        if row['result_pruned']:
            result['resultPruned'] = True
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
                if row is not None and row['cancel_requested']:
                    # Submitting identical work again means its result is
                    # still wanted; joining it must not inherit the cancel.
                    db.execute("UPDATE jobs SET cancel_requested=0,message='继续处理中' WHERE id=?", (row['id'],))
                    row = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
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

    def cancel(self, job_id):
        """Stop queued work now and running work at its next stage boundary.

        A result committed before that boundary still succeeds: the map has
        already changed, so reporting the job as cancelled would be false.
        A recovered job may have committed before the restart; its handler
        reconciles the receipt first, then observes the request.
        """
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is not None and row['state'] == 'queued' and not row['recovered']:
                db.execute("""UPDATE jobs SET state='failed',message='已取消',error=?,finished_at=? WHERE id=?""",
                           (CANCELLED, time.time(), job_id))
            elif row is not None and row['state'] in ('queued', 'running'):
                db.execute("""UPDATE jobs SET cancel_requested=1,message='正在取消，当前步骤结束后停止'
                              WHERE id=?""", (job_id,))
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            db.execute('COMMIT')
        return self.public(row, with_result=False)

    def compact(self, keep_per_asset=20, keep_sec=30*86400, now=None):
        """Drop old result bodies; metadata stays visible in the history.

        The newest finished results of each map are kept regardless of age.
        Published courses copy their plan, so they never depend on these.
        """
        now = time.time() if now is None else now
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("""SELECT id FROM (SELECT id, finished_at, ROW_NUMBER() OVER (
                                     PARTITION BY asset_id ORDER BY finished_at DESC, id) AS position
                                 FROM jobs WHERE state IN ('succeeded','failed') AND result IS NOT NULL)
                                 WHERE position > ? AND finished_at < ?""", (keep_per_asset, now-keep_sec)).fetchall()
            db.executemany('UPDATE jobs SET result=NULL, result_pruned=1 WHERE id=?', [(row['id'],) for row in rows])
            db.execute('COMMIT')
            free = db.execute('PRAGMA freelist_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
            if free > 8*1024*1024:
                try:
                    db.execute('VACUUM')
                except sqlite3.OperationalError:
                    pass  # a reader holds the file; the space is reused or freed next time
        return len(rows)

    def maintain(self):
        # Called only by the live worker between jobs.
        self.last_maintenance = time.monotonic()
        try:
            self.compact()
            if self.maintenance is not None:
                self.maintenance()
        except Exception:
            log.exception('Data retention pass failed; retrying at the next interval')

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
                db.execute("UPDATE jobs SET progress=?,message=? WHERE id=? AND state='running' AND cancel_requested=0",
                           (max(0, min(100, float(percent))), str(message), row['id']))
                cancelled = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (row['id'],)).fetchone()[0]
            # A completion report is never interrupted; cancelling applies to
            # the work that remains.
            if cancelled and percent < 100:
                raise JobCancelled('任务已取消')
        envelope = {'id': row['id'], 'kind': row['kind'], 'assetId': row['asset_id'],
                    'payload': json.loads(row['payload']), 'inputs': json.loads(row['inputs']),
                    'attempts': row['attempts'], 'recovered': bool(row['recovered'])}
        try:
            result = encode(self.handler(envelope, progress))
        except JobCancelled:
            with self.database() as db:
                db.execute("""UPDATE jobs SET state='failed',message='已取消',error=?,finished_at=? WHERE id=?""",
                           (CANCELLED, time.time(), row['id']))
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
                        elif (self.last_maintenance is None
                              or time.monotonic()-self.last_maintenance >= self.maintenance_interval_sec):
                            self.maintain()
                        else:
                            self.wake.wait(self.poll_sec)
                            self.wake.clear()
                except Exception:
                    # A database/filesystem failure does not erase a running
                    # job. Reacquire the lock and reconcile it on recovery.
                    log.exception('Task queue interrupted; retained durable job state')
            self.stop.wait(self.poll_sec)
