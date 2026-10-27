"""Groups joined with a short temporary code: shared positions and messages.

Phones in a group poll one small sync request: it carries the member's own
position while sharing is on and returns everyone's latest state. Positions
are kept only while their owner shares them, are cleared on leaving, and the
whole group disappears after it has been idle or open too long. A group
opened from a course is also visible to that course's teacher.
"""
from contextlib import contextmanager
import hashlib
import secrets
import sqlite3
import threading
import time

from . import store

CODE_DIGITS = 6
IDLE_SEC = 6*3600
MAX_AGE_SEC = 24*3600
MAX_MEMBERS = 60
ONLINE_SEC = 45
POSITION_TTL_SEC = 15*60
MESSAGE_KEEP = 200
MESSAGE_LIMIT = (20, 300)    # per member: messages per window seconds
# A whole class often shares one school address: limit guessing, not use.
JOIN_LIMIT = (60, 600)       # per address: failed joins per window seconds
CREATE_LIMIT = (60, 3600)    # per address: new groups per window seconds
KINDS = ('text', 'pin', 'help', 'resolved')
_schema = set()
_schema_lock = threading.Lock()


class GroupError(ValueError):
    pass


class Gone(GroupError):
    """The group was closed or expired, or this member was removed."""


@contextmanager
def database():
    path = store.DATA/'groups.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with _schema_lock:
            if str(path) not in _schema:
                # Many tiny position writes: losing the last second in a
                # power cut is acceptable, an fsync per heartbeat is not.
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS groups (
                        id TEXT PRIMARY KEY, code TEXT NOT NULL, title TEXT NOT NULL,
                        release_id TEXT, workspace TEXT, center_lng REAL, center_lat REAL,
                        created_at REAL NOT NULL, active_at REAL NOT NULL,
                        locked INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0);
                    CREATE UNIQUE INDEX IF NOT EXISTS open_code ON groups(code) WHERE closed=0;
                    CREATE INDEX IF NOT EXISTS group_release ON groups(release_id);
                    CREATE TABLE IF NOT EXISTS members (
                        group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                        id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                        team_id TEXT, color INTEGER NOT NULL, host INTEGER NOT NULL DEFAULT 0,
                        joined_at REAL NOT NULL, seen_at REAL NOT NULL, removed INTEGER NOT NULL DEFAULT 0,
                        sharing INTEGER NOT NULL DEFAULT 0, lng REAL, lat REAL, accuracy REAL,
                        heading REAL, fix_at REAL, help_at REAL,
                        PRIMARY KEY(group_id, id));
                    CREATE TABLE IF NOT EXISTS messages (
                        group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                        seq INTEGER NOT NULL, member_id TEXT NOT NULL, kind TEXT NOT NULL,
                        text TEXT, lng REAL, lat REAL, created_at REAL NOT NULL,
                        PRIMARY KEY(group_id, seq));
                    CREATE TABLE IF NOT EXISTS attempts (key TEXT NOT NULL, at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS group_attempt_key ON attempts(key, at);
                ''')
                _schema.add(str(path))
        db.execute('PRAGMA synchronous=NORMAL')
        yield db
    finally:
        db.close()


@contextmanager
def transaction():
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise


def token_hash(token):
    return hashlib.sha256((token or '').encode()).hexdigest()


def expire(db, now):
    db.execute('DELETE FROM groups WHERE active_at<? OR created_at<?', (now-IDLE_SEC, now-MAX_AGE_SEC))
    db.execute('DELETE FROM attempts WHERE at<?', (now-24*3600,))


def limited(db, key, limit, now):
    count, window = limit
    return db.execute('SELECT COUNT(*) FROM attempts WHERE key=? AND at>?', (key, now-window)).fetchone()[0] >= count


def clean_name(name):
    name = ' '.join(str(name).split())
    if not 1 <= len(name) <= 20:
        raise GroupError('请填写 1–20 字的显示名称')
    return name


def add_member(db, group_id, name, team_id, host, now):
    used = {row['color'] for row in db.execute('SELECT color FROM members WHERE group_id=? AND removed=0', (group_id,))}
    color = next((index for index in range(MAX_MEMBERS) if index not in used), 0)
    member_id, token = secrets.token_hex(4), secrets.token_urlsafe(24)
    db.execute('''INSERT INTO members(group_id,id,token_hash,name,team_id,color,host,joined_at,seen_at)
                  VALUES(?,?,?,?,?,?,?,?,?)''', (group_id, member_id, token_hash(token), name, team_id, color, int(host), now, now))
    return member_id, token


def create(name, address, title=None, release=None, team_id=None, center=None, now=None):
    now = time.time() if now is None else now
    name = clean_name(name)
    title = ' '.join(str(title or '').split())[:40] or f'{name}的小组'
    with transaction() as db:
        expire(db, now)
        if limited(db, 'create:'+address, CREATE_LIMIT, now):
            raise GroupError('创建小组过于频繁，请稍后再试')
        for _ in range(50):
            code = f'{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}'
            if not db.execute('SELECT 1 FROM groups WHERE code=? AND closed=0', (code,)).fetchone():
                break
        else:
            raise GroupError('暂时无法分配组队码，请稍后再试')
        group_id = secrets.token_hex(8)
        db.execute('''INSERT INTO groups(id,code,title,release_id,workspace,center_lng,center_lat,created_at,active_at)
                      VALUES(?,?,?,?,?,?,?,?,?)''',
                   (group_id, code, title, release and release['id'], release and release['workspace'],
                    *(center or (None, None)), now, now))
        member_id, token = add_member(db, group_id, name, team_id, True, now)
        db.execute('INSERT INTO attempts VALUES(?,?)', ('create:'+address, now))
        return {'groupId': group_id, 'memberId': member_id, 'token': token, **view(db, group_id, member_id, 0, now)}


def join(code, name, address, team_id=None, now=None):
    now = time.time() if now is None else now
    name = clean_name(name)
    code = ''.join(ch for ch in str(code) if ch.isdigit())
    with transaction() as db:
        expire(db, now)
        if limited(db, 'join:'+address, JOIN_LIMIT, now):
            raise GroupError('尝试次数过多，请 10 分钟后再试')
        group = db.execute('SELECT * FROM groups WHERE code=? AND closed=0', (code,)).fetchone() if len(code) == CODE_DIGITS else None
        if group is None:
            # Committed below, so failed guesses count toward the limit.
            db.execute('INSERT INTO attempts VALUES(?,?)', ('join:'+address, now))
        elif group['locked']:
            raise GroupError('小组已锁定，不再接受新成员')
        elif db.execute('SELECT COUNT(*) FROM members WHERE group_id=? AND removed=0', (group['id'],)).fetchone()[0] >= MAX_MEMBERS:
            raise GroupError('小组人数已满')
        else:
            member_id, token = add_member(db, group['id'], name, team_id, False, now)
            db.execute('UPDATE groups SET active_at=? WHERE id=?', (now, group['id']))
            return {'groupId': group['id'], 'memberId': member_id, 'token': token, **view(db, group['id'], member_id, 0, now)}
    raise GroupError('组队码不存在或已过期')


def member(db, group_id, token):
    row = db.execute('''SELECT members.*, groups.closed AS closed FROM members JOIN groups ON groups.id=members.group_id
                        WHERE members.group_id=? AND token_hash=?''', (group_id, token_hash(token))).fetchone()
    if row is None or row['closed']:
        raise Gone('小组已解散或已过期')
    if row['removed']:
        raise Gone('你已离开或被移出这个小组')
    return row


def sync(group_id, token, sharing, position, after, now=None):
    """Record this member's heartbeat and position; return the group."""
    now = time.time() if now is None else now
    with transaction() as db:
        expire(db, now)
        me = member(db, group_id, token)
        if sharing and position is not None:
            lng, lat = position['lnglat']
            age = now-position['timestampMs']/1000
            if not (-180 <= lng <= 180 and -90 <= lat <= 90):
                raise GroupError('位置无效')
            # A phone with a wrong clock still shares; the server notes when
            # it received the fix instead of trusting that clock.
            fix_at = position['timestampMs']/1000 if -60 <= age <= POSITION_TTL_SEC else now
            db.execute('''UPDATE members SET sharing=1, lng=?, lat=?, accuracy=?, heading=?, fix_at=?, seen_at=?
                          WHERE group_id=? AND id=?''', (lng, lat, position['accuracyM'], position.get('headingDeg'),
                                                        fix_at, now, group_id, me['id']))
        elif sharing:
            db.execute('UPDATE members SET sharing=1, seen_at=? WHERE group_id=? AND id=?', (now, group_id, me['id']))
        else:
            db.execute('''UPDATE members SET sharing=0, lng=NULL, lat=NULL, accuracy=NULL, heading=NULL, fix_at=NULL, seen_at=?
                          WHERE group_id=? AND id=?''', (now, group_id, me['id']))
        db.execute('UPDATE groups SET active_at=? WHERE id=?', (now, group_id))
        return view(db, group_id, me['id'], after, now)


def post(group_id, token, kind, text=None, lnglat=None, after=0, now=None):
    now = time.time() if now is None else now
    if kind not in KINDS:
        raise GroupError('消息类型无效')
    text = ' '.join(str(text or '').split())[:120] or None
    if kind == 'text' and not text:
        raise GroupError('请输入消息')
    if kind == 'pin' and lnglat is None:
        raise GroupError('请在地图上选择标记位置')
    with transaction() as db:
        expire(db, now)
        me = member(db, group_id, token)
        count, window = MESSAGE_LIMIT
        if db.execute('SELECT COUNT(*) FROM messages WHERE group_id=? AND member_id=? AND created_at>?',
                      (group_id, me['id'], now-window)).fetchone()[0] >= count:
            raise GroupError('发送过于频繁，请稍后再试')
        seq = db.execute('SELECT COALESCE(MAX(seq), 0)+1 FROM messages WHERE group_id=?', (group_id,)).fetchone()[0]
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)', (group_id, seq, me['id'], kind, text,
                   *(lnglat if lnglat is not None else (None, None)), now))
        db.execute('DELETE FROM messages WHERE group_id=? AND seq<=?', (group_id, seq-MESSAGE_KEEP))
        if kind in ('help', 'resolved'):
            db.execute('UPDATE members SET help_at=? WHERE group_id=? AND id=?', (now if kind == 'help' else None, group_id, me['id']))
        db.execute('UPDATE groups SET active_at=? WHERE id=?', (now, group_id))
        return view(db, group_id, me['id'], after, now)


def leave(group_id, token, now=None):
    now = time.time() if now is None else now
    with transaction() as db:
        me = member(db, group_id, token)
        db.execute('''UPDATE members SET removed=1, sharing=0, lng=NULL, lat=NULL, accuracy=NULL, heading=NULL,
                      fix_at=NULL, help_at=NULL WHERE group_id=? AND id=?''', (group_id, me['id']))
        if me['host']:
            # Hosting passes to the longest-standing remaining member.
            successor = db.execute('SELECT id FROM members WHERE group_id=? AND removed=0 ORDER BY joined_at, rowid LIMIT 1',
                                   (group_id,)).fetchone()
            if successor is None:
                db.execute('UPDATE groups SET closed=1 WHERE id=?', (group_id,))
            else:
                db.execute('UPDATE members SET host=1 WHERE group_id=? AND id=?', (group_id, successor['id']))


def manage(group_id, token, action, target=None, now=None):
    """Host actions: lock/unlock joining, remove a member, or disband."""
    now = time.time() if now is None else now
    with transaction() as db:
        me = member(db, group_id, token)
        if not me['host']:
            raise GroupError('只有组长可以管理小组')
        if action in ('lock', 'unlock'):
            db.execute('UPDATE groups SET locked=? WHERE id=?', (int(action == 'lock'), group_id))
        elif action == 'remove':
            if target == me['id'] or not db.execute('SELECT 1 FROM members WHERE group_id=? AND id=? AND removed=0',
                                                   (group_id, target)).fetchone():
                raise GroupError('成员不存在')
            db.execute('''UPDATE members SET removed=1, sharing=0, lng=NULL, lat=NULL, accuracy=NULL, heading=NULL,
                          fix_at=NULL, help_at=NULL WHERE group_id=? AND id=?''', (group_id, target))
        elif action == 'close':
            db.execute('UPDATE groups SET closed=1 WHERE id=?', (group_id,))
            return {'closed': True}
        else:
            raise GroupError('操作无效')
        return view(db, group_id, me['id'], 0, now)


def members_view(db, group_id, now):
    rows = db.execute('SELECT * FROM members WHERE group_id=? AND removed=0 ORDER BY joined_at, rowid', (group_id,)).fetchall()
    result = []
    for row in rows:
        fresh = row['sharing'] and row['fix_at'] is not None and now-row['fix_at'] <= POSITION_TTL_SEC
        result.append({'id': row['id'], 'name': row['name'], 'teamId': row['team_id'], 'color': row['color'],
                       'host': bool(row['host']), 'online': now-row['seen_at'] <= ONLINE_SEC, 'seenAt': row['seen_at'],
                       'sharing': bool(row['sharing']), 'helpAt': row['help_at'],
                       'position': {'lnglat': [row['lng'], row['lat']], 'accuracyM': row['accuracy'],
                                    'headingDeg': row['heading'], 'at': row['fix_at']} if fresh else None})
    return result


def group_view(row):
    return {'id': row['id'], 'code': row['code'], 'title': row['title'], 'releaseId': row['release_id'],
            'center': [row['center_lng'], row['center_lat']] if row['center_lng'] is not None else None,
            'createdAt': row['created_at'], 'expiresAt': min(row['active_at']+IDLE_SEC, row['created_at']+MAX_AGE_SEC),
            'locked': bool(row['locked'])}


def view(db, group_id, member_id, after, now):
    group = db.execute('SELECT * FROM groups WHERE id=?', (group_id,)).fetchone()
    names = {row['id']: row['name'] for row in db.execute('SELECT id, name FROM members WHERE group_id=?', (group_id,))}
    messages = [{'seq': row['seq'], 'memberId': row['member_id'], 'name': names.get(row['member_id'], '已离开'),
                 'kind': row['kind'], 'text': row['text'], 'at': row['created_at'],
                 'lnglat': [row['lng'], row['lat']] if row['lng'] is not None else None}
                for row in db.execute('SELECT * FROM messages WHERE group_id=? AND seq>? ORDER BY seq', (group_id, after))]
    return {'group': group_view(group), 'me': member_id, 'members': members_view(db, group_id, now),
            'messages': messages, 'serverTime': now}


def for_release(release_id, now=None):
    """Every open group of a course with its shared positions, for its teacher."""
    now = time.time() if now is None else now
    with database() as db:
        rows = db.execute('''SELECT * FROM groups WHERE release_id=? AND closed=0 AND active_at>=? AND created_at>=?
                             ORDER BY created_at''', (release_id, now-IDLE_SEC, now-MAX_AGE_SEC)).fetchall()
        result = []
        for row in rows:
            recent = [{'seq': m['seq'], 'memberId': m['member_id'], 'kind': m['kind'], 'text': m['text'], 'at': m['created_at'],
                       'lnglat': [m['lng'], m['lat']] if m['lng'] is not None else None}
                      for m in db.execute('''SELECT * FROM messages WHERE group_id=? AND kind IN ('pin','help')
                                             ORDER BY seq DESC LIMIT 10''', (row['id'],))]
            result.append({**group_view(row), 'members': members_view(db, row['id'], now), 'recent': recent})
    return {'groups': result, 'serverTime': now}
