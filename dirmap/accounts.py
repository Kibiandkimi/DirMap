"""Accounts, sessions and each account's own workbench.

An account owns one workbench directory. Inside it every teacher action —
processing a picture, planning, publishing — needs no further teacher token:
the session already says whose workbench it is. The original single-teacher
workbench at the data root is claimed once by an account that presents the
old teacher token (or, on a machine without one, by explicit choice); from
then on visitors without a session no longer reach it.
"""
from contextlib import contextmanager
import hashlib
import hmac
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
import zipfile

from . import samples, store

COOKIE = 'dirmap_session'
SESSION_SEC = 30*86400
TOUCH_SEC = 3600
USERNAME = re.compile(r'[\w.-]{2,32}')
MIN_PASSWORD, MAX_PASSWORD = 8, 128
SCRYPT = {'n': 2**14, 'r': 8, 'p': 1}
# (limit, window seconds) of failed logins per address and per name, and of
# new accounts per address. A class often signs up from one school address.
LOGIN_LIMIT = (100, 900)
NAME_LIMIT = (10, 900)
SIGNUP_LIMIT = (60, 3600)
_schema = set()
_schema_lock = threading.Lock()


class AuthError(ValueError):
    """A credential or session problem; reported without further detail."""


@contextmanager
def database():
    # Password and session digests: readable by the server's user only.
    path = store.private_database(store.DATA/'accounts.sqlite3')
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with _schema_lock:
            if str(path) not in _schema:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
                        password TEXT NOT NULL, workspace TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL, last_login_at REAL);
                    CREATE TABLE IF NOT EXISTS sessions (
                        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        created_at REAL NOT NULL, expires_at REAL NOT NULL, seen_at REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS attempts (key TEXT NOT NULL, at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS attempt_key ON attempts(key, at);
                ''')
                _schema.add(str(path))
        yield db
    finally:
        db.close()


def username_key(name):
    return unicodedata.normalize('NFKC', name).casefold()


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=64*1024*1024, **SCRYPT)
    return f'scrypt${SCRYPT["n"]}${SCRYPT["r"]}${SCRYPT["p"]}${salt.hex()}${digest.hex()}'


def check_password(password, stored):
    try:
        _, n, r, p, salt, expected = stored.split('$')
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                dklen=32, maxmem=64*1024*1024)
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected)


def public(row):
    return None if row is None else {'id': row['id'], 'username': row['username'], 'createdAt': row['created_at'],
                                     'ownsOriginalWorkbench': row['workspace'] == store.LEGACY}


def throttled(db, key, limit, now):
    count, window = limit
    db.execute('DELETE FROM attempts WHERE at<?', (now-24*3600,))
    return db.execute('SELECT COUNT(*) FROM attempts WHERE key=? AND at>?', (key, now-window)).fetchone()[0] >= count


def validate(username, password):
    username = unicodedata.normalize('NFKC', username.strip())
    if not USERNAME.fullmatch(username):
        raise AuthError('用户名为 2–32 个字，可用中文、字母、数字、点、下划线和连字符')
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AuthError(f'密码长度应为 {MIN_PASSWORD}–{MAX_PASSWORD} 个字符')
    return username


def legacy_owner():
    with database() as db:
        return public(db.execute('SELECT * FROM users WHERE workspace=?', (store.LEGACY,)).fetchone())


def workbench_scope(user):
    return (user['id'] + (':original' if user['workspace'] == store.LEGACY else ':personal')) if user else 'original-anonymous'


def workspace_matches(user_id, workspace):
    with database() as db:
        return db.execute('SELECT 1 FROM users WHERE id=? AND workspace=?', (user_id, workspace)).fetchone() is not None


def _workspace_has_work(workspace):
    root = store.workspace_root(workspace)
    if store.read_json(root/'uploads.json', []) or store.read_json(root/'sites.json', []) or owned_releases(workspace):
        return True
    allowed = {root/'uploads.json', root/'sites.json'}
    for asset in store.builtin_maps():
        path = root/'projects'/asset['id']/'state.json'
        if path.exists():
            try:
                state = store.read_json(path)
                if not samples.untouched(state):
                    return True
                allowed.add(path)
                allowed.update(path.parent/name for name in state['artifacts'].values())
            except (OSError, ValueError, KeyError, TypeError):
                return True
    if any(path.is_file() and path not in allowed for path in root.rglob('*')):
        return True
    history = store.DATA/'jobs'/'queue.sqlite3'
    if history.is_file():
        with sqlite3.connect(history.resolve().as_uri()+'?mode=ro', uri=True) as db:
            if 'workspace' in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                if db.execute('SELECT 1 FROM jobs WHERE workspace=? LIMIT 1', (workspace,)).fetchone():
                    return True
    return False


def legacy_has_data():
    return bool(store.read_json(store.DATA/'uploads.json', [])) or any(
        (store.DATA/'projects'/asset['id']/'state.json').exists() for asset in store.builtin_maps())


def check_legacy_token(token):
    expected = os.environ.get('DIRMAP_ADMIN_TOKEN')
    if expected and not secrets.compare_digest(token or '', expected):
        raise AuthError('原教师口令不正确')


def register(username, password, address, claim_legacy=False, legacy_token=None, now=None):
    now = time.time() if now is None else now
    username = validate(username, password)
    if claim_legacy:
        check_legacy_token(legacy_token)
    stored = hash_password(password)
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if throttled(db, 'signup:'+address, SIGNUP_LIMIT, now):
                raise AuthError('注册过于频繁，请稍后再试')
            if db.execute('SELECT 1 FROM users WHERE username_key=?', (username_key(username),)).fetchone():
                raise AuthError('用户名已被使用')
            if claim_legacy and db.execute('SELECT 1 FROM users WHERE workspace=?', (store.LEGACY,)).fetchone():
                raise AuthError('原工作台已归属其他账户')
            workspace = store.LEGACY if claim_legacy else secrets.token_hex(8)
            user_id = secrets.token_hex(12)
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,NULL)',
                       (user_id, username, username_key(username), stored, workspace, now))
            db.execute('INSERT INTO attempts VALUES(?,?)', ('signup:'+address, now))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    if workspace != store.LEGACY:
        seed_workspace(workspace)
    return start_session(user_id, now)


def login(username, password, address, now=None):
    now = time.time() if now is None else now
    key = username_key(unicodedata.normalize('NFKC', username.strip()))
    with database() as db:
        if throttled(db, 'login:'+address, LOGIN_LIMIT, now) or throttled(db, 'name:'+key, NAME_LIMIT, now):
            raise AuthError('登录尝试过多，请 15 分钟后再试')
        row = db.execute('SELECT * FROM users WHERE username_key=?', (key,)).fetchone()
    # Hash even for an unknown name so the response time reveals nothing.
    valid = check_password(password, row['password'] if row else hash_password('x'*MIN_PASSWORD))
    if not row or not valid:
        with database() as db:
            db.executemany('INSERT INTO attempts VALUES(?,?)', [('login:'+address, now), ('name:'+key, now)])
        raise AuthError('用户名或密码不正确')
    with database() as db:
        db.execute('UPDATE users SET last_login_at=? WHERE id=?', (now, row['id']))
    return start_session(row['id'], now)


def start_session(user_id, now):
    token = secrets.token_urlsafe(32)
    with database() as db:
        db.execute('DELETE FROM sessions WHERE expires_at<?', (now,))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, now, now+SESSION_SEC, now))
        user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    return token, user_record(user)


def user_record(row):
    return {**public(row), 'workspace': row['workspace']}


def session_user(token, now=None):
    """The signed-in account for a session cookie, sliding its expiry."""
    if not token or len(token) > 100:
        return None
    now = time.time() if now is None else now
    digest = hashlib.sha256(token.encode()).hexdigest()
    with database() as db:
        row = db.execute('''SELECT users.*, sessions.seen_at AS seen_at FROM sessions JOIN users ON users.id=sessions.user_id
                            WHERE token_hash=? AND expires_at>?''', (digest, now)).fetchone()
        if row is None:
            return None
        if now-row['seen_at'] > TOUCH_SEC:
            db.execute('UPDATE sessions SET seen_at=?, expires_at=? WHERE token_hash=?', (now, now+SESSION_SEC, digest))
    return user_record(row)


def logout(token):
    if token:
        with database() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))


def change_password(user, old, new, keep_token, now=None):
    now = time.time() if now is None else now
    validate(user['username'], new)
    with database() as db:
        row = db.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
    if not row or not check_password(old, row['password']):
        raise AuthError('原密码不正确')
    stored = hash_password(new)
    keep = hashlib.sha256(keep_token.encode()).hexdigest()
    with database() as db:
        db.execute('UPDATE users SET password=? WHERE id=?', (stored, user['id']))
        # Signing in elsewhere with the old password must stop working.
        db.execute('DELETE FROM sessions WHERE user_id=? AND token_hash<>?', (user['id'], keep))


def claim_legacy(user, legacy_token):
    """Move an account onto the original workbench, before it holds its own work."""
    check_legacy_token(legacy_token)
    previous = user['workspace']
    if previous == store.LEGACY:
        return user
    root = store.workspace_root(previous)
    with store.LOCK:
        if not workspace_matches(user['id'], previous):
            raise AuthError('工作台已变化，请刷新后再操作')
        if _workspace_has_work(previous):
            raise AuthError('这个账户已有自己的修改或处理记录，不能替换工作台；请用空白账户认领')
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('SELECT 1 FROM users WHERE workspace=?', (store.LEGACY,)).fetchone():
                    raise AuthError('原工作台已归属其他账户')
                db.execute('UPDATE users SET workspace=? WHERE id=?', (store.LEGACY, user['id']))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        shutil.rmtree(root, ignore_errors=True)
    return {**user, 'workspace': store.LEGACY, 'ownsOriginalWorkbench': True}


def owned_releases(workspace):
    return [path.parent.name for path in (store.DATA/'releases').glob('*/meta.json')
            if (store.read_json(path) or {}).get('workspace', store.LEGACY) == workspace]


def seed_workspace(workspace):
    """Clone only public starter maps. An owner's workbench is never a seed."""
    root = store.workspace_root(workspace)
    with store.LOCK:
        try:
            public = samples.source_root()
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            logging.getLogger(__name__).exception('Public starter maps are unavailable; the workbench can process the original images')
            return
        if public is None:
            return
        for asset in store.builtin_maps():
            source = public/'projects'/asset['id']
            state = store.read_json(source/'state.json')
            if not state or state.get('sourceHash') != asset['sha256']:
                continue
            target = root/'projects'/asset['id']
            # This also lets setup initialize an empty original workbench
            # without replacing any existing edits or processing output.
            if target.exists() and any(target.iterdir()):
                continue
            created = []
            try:
                for relative in state['artifacts'].values():
                    destination = target/relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(source/relative, destination)
                        created.append(destination)
                    except OSError:
                        with (source/relative).open('rb') as incoming, destination.open('xb') as outgoing:
                            created.append(destination)
                            shutil.copyfileobj(incoming, outgoing)
                state.update(reviewed=False, seededFrom='public-samples', seededBundle=public.name, updatedAt=time.time())
                state['seededStateHash'] = samples.state_hash(state)
                store.atomic_json(target/'state.json', state)
            except OSError:
                if (target/'state.json').is_file():
                    continue  # The atomic publication completed before a directory-sync error.
                for path in created:
                    path.unlink(missing_ok=True)
                try:
                    target.rmdir()
                except OSError:
                    pass
                continue
