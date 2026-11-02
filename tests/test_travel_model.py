import copy
import math

import numpy as np
import pytest

from dirmap import travel_model
from dirmap.geometry import Frame


def recording(xy, times, identifier='walk', activity='walk', breaks=(0,)):
    return {'id': identifier, 'points': np.column_stack((Frame().to_lnglat(xy), np.full(len(xy), 20.))),
            'times': np.array(times, float), 'activity': activity, 'breaks': list(breaks)}


def test_missing_clocks_gaps_geometry_breaks_and_jumps_never_invent_speeds():
    xy = np.column_stack((np.arange(400.), np.zeros(400)))
    track = recording(xy, np.full(400, np.nan))
    assert travel_model.observations(track, Frame())[0] == []
    track = recording(xy, np.arange(400.), breaks=(0, 120, 260))
    track['times'][260:] += 600
    track['times'][180] = track['times'][179]  # duplicate clock
    track['points'][320, 0] += .02  # kilometre-scale GNSS spike
    rows, summary = travel_model.observations(track, Frame())
    assert rows and summary['rejectedPairs'] >= 5
    for row in rows:
        start, end = row['start'][0], row['end'][0]
        assert not any(start < boundary < end for boundary in (120, 180, 260, 320))
        assert row['distance']/row['seconds'] == pytest.approx(1, abs=.01)


def test_stops_and_one_second_jitter_are_removed_before_sampling():
    rng = np.random.default_rng(73)
    xy = np.column_stack((np.r_[np.arange(150.), np.full(100, 150), np.arange(150., 350.)], np.zeros(450)))
    xy += rng.normal(0, .5, xy.shape)
    rows, summary = travel_model.observations(recording(xy, np.arange(450.)), Frame())
    assert summary['stoppedSec'] >= 80
    assert .9 < summary['medianSpeedMps'] < 1.1
    assert all(r['distance']/r['seconds'] > .7 for r in rows)
    assert summary['episodes'] <= 2


def test_fast_unlabelled_records_stay_ambiguous_and_explicit_labels_win():
    xy = np.column_stack((np.arange(400.)*3.2, np.zeros(400)))
    track = recording(xy, np.arange(400.), activity='auto')
    rows, summary = travel_model.observations(track, Frame())
    assert summary['inferredActivity'] == 'unknown'
    assert all(r['confidence'] == 0 for r in rows)
    for mode in ('walk', 'run', 'foot', 'bicycle'):
        rows, summary = travel_model.observations({**track, 'activity': mode}, Frame())
        assert summary['inferredActivity'] == travel_model.learning_mode(mode) and summary['confidence'] == 1
        assert all(r['mode'] == travel_model.learning_mode(mode) for r in rows)
    rows, summary = travel_model.observations({**track, 'activity': 'exclude'}, Frame())
    assert summary['inferredActivity'] == 'excluded' and not any(r['confidence'] for r in rows)


def test_mixed_running_and_walking_has_one_learning_class_and_separate_pace_episodes():
    speed = np.r_[np.full(240, 1.2), np.full(240, 2.7), np.full(240, 1.2)]
    xy = np.column_stack((np.cumsum(speed), np.zeros(len(speed))))
    rows, summary = travel_model.observations(recording(xy, np.arange(len(speed)), activity='foot'), Frame())
    assert {r['mode'] for r in rows} == {'foot'}
    assert {r['track'] for r in rows} == {'walk'}
    assert summary['episodes'] >= 3
    assert summary['speedP10Mps'] == pytest.approx(1.2, abs=.02)
    assert summary['speedP90Mps'] == pytest.approx(2.7, abs=.02)


def test_historical_walk_run_and_mixed_labels_pool_their_independent_support():
    rows = fit_rows(paces=(1.2, 2.8, 1.9))
    for row in rows:
        row['mode'] = {'t0': 'walk', 't1': 'run', 't2': 'foot'}[row['track']]
    profile = travel_model.fit_profile(rows, 'foot')
    assert profile['tracks'] == 3 and len(profile['corridors']) == 2
    assert all(set(c['trackIds']) == {'t0', 't1', 't2'} for c in profile['corridors'])
    for mode in ('walk', 'run'):
        compatible = travel_model.fit_profile(rows, mode)
        assert compatible == profile
    changed = copy.deepcopy(rows)
    for row in changed:
        row['seconds'] *= {'t0': 2., 't1': .45, 't2': 1.7}[row['track']]
    normalized = travel_model.fit_profile(changed, 'foot')
    np.testing.assert_allclose([c['logFactor'] for c in profile['corridors']],
                               [c['logFactor'] for c in normalized['corridors']], atol=1e-5)


def fit_rows(paces=(.75, 1.1, 1.6, 1.9, .9, 1.4)):
    rows = []
    for track, pace in enumerate(paces):
        for repeat in range(8):
            for road, factor in enumerate((1., 1.6)):
                rows.append({'start': [100*road+2, 4], 'end': [100*road+42, 4], 'distance': 40.,
                    'seconds': 40*factor/pace, 'grade': 0., 'mode': 'walk', 'confidence': 1.,
                    'track': f't{track}', 'episode': f't{track}:1'})
    return rows


def predictor(profile):
    return travel_model.Predictor({'frame': Frame().as_dict(), 'profiles': {'walk': profile}})


def test_personal_pace_and_cycling_do_not_become_road_penalties():
    rows = fit_rows()
    normal = travel_model.fit_profile(rows, 'walk')
    changed = copy.deepcopy(rows)
    for row in changed:
        # Entire recordings become dramatically slower or faster; path effects
        # must remain the same under their unpenalized activity intercepts.
        row['seconds'] *= .4 if row['track'] in ('t1', 't2') else 3.
    for row in rows:
        changed.append({**row, 'mode': 'bicycle', 'track': 'bike-'+row['track'], 'seconds': row['seconds']/6})
    again = travel_model.fit_profile(changed, 'walk')
    np.testing.assert_allclose([c['logFactor'] for c in normal['corridors']],
                               [c['logFactor'] for c in again['corridors']], atol=1e-5)
    model = predictor(normal)
    cost, supported, _ = model.predict([[2, 4], [102, 4]], [[42, 4], [142, 4]], [0, 0])
    assert supported.all() and cost[1]/cost[0] > 1.3
    assert len(normal['paceMps']) == 6


def test_repeated_laps_cannot_substitute_for_independent_recordings():
    rows = fit_rows((1.1,))
    profile = travel_model.fit_profile(rows*100, 'walk')
    assert profile['status'] == 'insufficient_data' and not profile['corridors']
    profile = travel_model.fit_profile(fit_rows(), 'walk')
    many_laps = travel_model.fit_profile(fit_rows()+[r for r in fit_rows() if r['track'] == 't0']*50, 'walk')
    np.testing.assert_allclose([c['logFactor'] for c in profile['corridors']],
                               [c['logFactor'] for c in many_laps['corridors']], atol=1e-5)
    # Repeating noisy observations must not change the robust scale either.
    rows = fit_rows()
    rng = np.random.default_rng(21)
    for row in rows:
        row['seconds'] *= float(np.exp(rng.normal(0, .3)))
    profile = travel_model.fit_profile(rows, 'walk')
    repeated = travel_model.fit_profile(rows+[r for r in rows if r['track'] == 't0']*30, 'walk')
    np.testing.assert_allclose([c['logFactor'] for c in profile['corridors']],
                               [c['logFactor'] for c in repeated['corridors']], atol=1e-5)


def test_between_person_grade_and_pace_differences_do_not_identify_slope():
    rows = fit_rows()
    for row in rows:
        row['grade'] = (int(row['track'][1:])-2)*.04
    profile = travel_model.fit_profile(rows, 'walk')
    assert not profile['gradeLearned']
    assert profile['coefficients'] == travel_model.PRIOR.tolist()


def test_corridors_do_not_cover_crossing_or_parallel_roads_and_zero_length():
    model = predictor(travel_model.fit_profile(fit_rows(), 'walk'))
    _, supported, _ = model.predict([[110, 0], [110, 22], [112, 4], [200, 4]],
                                     [[110, 12], [135, 22], [112, 4], [220, 4]], [0]*4)
    assert not supported.any()


def test_grade_learning_is_directional_pace_normalized_and_bounded_by_observations():
    rows = []
    rng = np.random.default_rng(19)
    true = np.array([6.5, -1.2, 7.])
    for t, pace in enumerate((.8, 1.1, 1.8, 1.5, .9, 1.3, 1.7, 1.2)):
        for i in range(80):
            grade = float(rng.choice([-.12, -.08, -.04, 0, .04, .08, .12]))
            cost = math.exp(travel_model.grade_log_cost([grade], true)[0])
            rows.append({'start': [5, 5], 'end': [35, 5], 'distance': 30., 'seconds': 30*cost/pace,
                         'track': f't{t}', 'episode': f't{t}:1', 'grade': grade, 'mode': 'walk', 'confidence': 1.})
    profile = travel_model.fit_profile(rows, 'walk')
    model = predictor(profile)
    assert profile['gradeLearned']
    assert profile['coefficients'][0] > 4 and profile['coefficients'][1] > -3
    cost = model.grade_cost([-.08, .08, 0, np.nan])
    assert cost[1] > cost[0] and cost[2] == 0 and cost[3] == 0
    # The learnt adjustment saturates outside observed grades; the reference
    # formula supplies the additional slope response instead of extrapolation.
    extra = model.grade_cost([.4])[0]-model.grade_cost([.3])[0]
    assert extra == pytest.approx(.35)
