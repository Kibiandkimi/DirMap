"""Shared class progress: furthest phone per team, no stale overwrite."""
import pytest

from dirmap import progress, store


def release():
    return {'id': 'a'*32, 'plan': {'coverage': {'mandatoryControlIds': ['m']},
            'routes': [{'teamId': 'A', 'controlIds': ['c1', 'm', 'c2']}, {'teamId': 'B', 'controlIds': ['m', 'c3']}]}}


def report(team='A', device='phone-one', revision=1, completed=(), times=None, departed=()):
    return {'teamId': team, 'deviceId': device, 'revision': revision, 'completed': list(completed),
            'checkIns': times or {}, 'departed': list(departed)}


@pytest.fixture
def course(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    return release()


def test_team_progress_is_its_furthest_phone_with_earliest_times(course):
    now = 1_800_000_000.
    progress.record(course, report(completed=['c1'], times={'c1': now*1000-60_000}), now)
    progress.record(course, report(device='phone-two', completed=['c1', 'm'], times={'c1': now*1000-30_000, 'm': now*1000}), now+1)
    summary = progress.record(course, report(team='B', device='phone-three', completed=['m'], departed=['m'], times={'m': now*1000}), now+2)
    a, b = summary['teams']
    assert a == {'teamId': 'A', 'completed': ['c1', 'm'], 'checkIns': {'c1': now*1000-60_000, 'm': now*1000},
                 'departed': [], 'finished': False, 'reportedAt': now+1}
    assert b['completed'] == ['m'] and b['departed'] == ['m'] and not b['finished']
    # Persisted for every reader, including a later server process.
    assert progress.summary(course, now=now+3)['teams'] == summary['teams']


def test_retries_and_late_reports_never_replace_newer_phone_state(course):
    now = 1_800_000_000.
    progress.record(course, report(revision=5, completed=['c1', 'm', 'c2']), now)
    late = progress.record(course, report(revision=4, completed=['c1']), now+5)
    assert late['teams'][0]['completed'] == ['c1', 'm', 'c2'] and late['teams'][0]['finished']
    undone = progress.record(course, report(revision=6, completed=['c1', 'm']), now+6)
    assert undone['teams'][0]['completed'] == ['c1', 'm'] and undone['teams'][0]['reportedAt'] == now+6


def test_invalid_order_targets_and_meetings_are_rejected(course):
    for bad, message in ((report(team='C'), '没有这个队伍'), (report(completed=['m']), '连续目标'),
                         (report(completed=['c1'], departed=['c1']), '集合点'),
                         (report(completed=['c1'], times={'m': 1.}), '已完成')):
        with pytest.raises(ValueError, match=message):
            progress.record(course, bad)
    assert not progress.path(course['id']).exists()


def test_a_wrong_phone_clock_keeps_completion_but_not_its_time(course):
    now = 1_800_000_000.
    summary = progress.record(course, report(completed=['c1'], times={'c1': (now-86400)*1000}), now)
    assert summary['teams'][0]['completed'] == ['c1'] and summary['teams'][0]['checkIns'] == {}


def test_number_of_reporting_phones_is_bounded(course, monkeypatch):
    monkeypatch.setattr(progress, 'MAX_DEVICES', 2)
    progress.record(course, report(device='phone-one'))
    progress.record(course, report(team='B', device='phone-two'))
    with pytest.raises(ValueError, match='设备过多'):
        progress.record(course, report(device='phone-three'))
    assert progress.record(course, report(device='phone-one', revision=2, completed=['c1']))['teams'][0]['completed'] == ['c1']
