"""Class progress shared by the phones of one published course.

A phone reports what it has confirmed for its own team: the completed prefix
of the team's published order, when each target was confirmed on that phone,
and which class meeting points the team has left. No location is stored.
Each phone numbers its reports, so a retry or a late delivery never replaces
newer state. A team's progress is its furthest phone, with the earliest
reported time for each target.
"""
import time

from . import store

MAX_DEVICES = 60
CLOCK_TOLERANCE_SEC = 12*3600


def path(release_id):
    return store.DATA/'releases'/release_id/'progress.json'


def started_at(release_id):
    """When the teacher started the class clock, in server seconds."""
    return (store.read_json(store.DATA/'releases'/release_id/'session.json') or {}).get('startedAt')


def set_started(release_id, moment):
    target = store.DATA/'releases'/release_id/'session.json'
    with store.LOCK:
        if moment is None:
            target.unlink(missing_ok=True)
        else:
            store.atomic_json(target, {'startedAt': moment})


def record(release, report, now=None):
    now = time.time() if now is None else now
    route = next((r for r in release['plan']['routes'] if r['teamId'] == report['teamId']), None)
    if route is None:
        raise ValueError('课程中没有这个队伍')
    completed = list(report['completed'])
    if completed != route['controlIds'][:len(completed)]:
        raise ValueError('打点必须是本队路线开头的连续目标')
    meetings = set((release['plan'].get('coverage') or {}).get('mandatoryControlIds', []))
    if not set(report['departed']) <= set(completed) & meetings:
        raise ValueError('只能离开本队已到达的全班集合点')
    if not set(report['checkIns']) <= set(completed):
        raise ValueError('打点时间只能对应已完成的目标')
    arrived = bool(report.get('arrived'))
    if arrived and completed != route['controlIds']:
        raise ValueError('完成本队所有目标后才能到达终点')
    # A phone with a wrong clock still reports completion; only the time
    # it cannot support is left out of the class record.
    plausible = lambda value: value is not None and abs(float(value)/1000-now) <= CLOCK_TOLERANCE_SEC
    times = {key: float(value) for key, value in report['checkIns'].items() if plausible(value)}
    arrived_at = float(report['arrivedAt']) if arrived and plausible(report.get('arrivedAt')) else None
    with store.LOCK:
        saved = store.read_json(path(release['id']), {'teams': {}})
        devices = saved['teams'].setdefault(route['teamId'], {})
        previous = devices.get(report['deviceId'])
        if previous is not None and previous['revision'] >= report['revision']:
            return summary(release, saved, now)
        if previous is None and sum(map(len, saved['teams'].values())) >= MAX_DEVICES:
            raise ValueError('本课上报进度的设备过多')
        devices[report['deviceId']] = {'revision': report['revision'], 'completed': completed, 'checkIns': times,
                                       'departed': sorted(set(report['departed'])), 'arrived': arrived,
                                       'arrivedAt': arrived_at, 'receivedAt': now}
        store.atomic_json(path(release['id']), saved)
    return summary(release, saved, now)


def summary(release, saved=None, now=None):
    saved = store.read_json(path(release['id']), {'teams': {}}) if saved is None else saved
    teams = []
    for route in release['plan']['routes']:
        devices = list(saved['teams'].get(route['teamId'], {}).values())
        completed = route['controlIds'][:max((len(d['completed']) for d in devices), default=0)]
        times = {}
        for device in devices:
            for key, value in device['checkIns'].items():
                times[key] = min(value, times.get(key, value))
        departed = set().union(*(device['departed'] for device in devices)) & set(completed)
        arrivals = [device for device in devices if device.get('arrived')]
        arrival_times = [device['arrivedAt'] for device in arrivals if device.get('arrivedAt') is not None]
        teams.append({'teamId': route['teamId'], 'completed': completed,
                      'checkIns': {key: times[key] for key in completed if key in times},
                      'departed': [key for key in completed if key in departed],
                      'finished': len(completed) == len(route['controlIds']),
                      'arrived': bool(arrivals), 'arrivedAt': min(arrival_times, default=None),
                      'reportedAt': max((device['receivedAt'] for device in devices), default=None)})
    return {'teams': teams, 'serverTime': time.time() if now is None else now}
