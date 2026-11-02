"""GPS check points measure the registration without changing the map."""
import numpy as np
import pytest

from dirmap import field_checks, service, store
from dirmap.geometry import Frame, Registration, affine_points
from dirmap.protocol import TERRAIN_SCHEMA


@pytest.fixture
def mapped(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    pixels = [[0, 0], [200, 0], [200, 200], [0, 200]]
    affine = [[1, 0, 0], [0, -1, 200]]  # one pixel is one metre, north up
    reg = Registration(affine, pixels, affine_points(affine, pixels))
    store.save_state('map', {'assetId': 'map', 'version': 4, 'width': 201, 'height': 201, 'registration': reg.as_dict(),
                             'controls': [], 'edits': [], 'terrainSchema': TERRAIN_SCHEMA})
    return reg


def gps(local):
    return Frame().to_lnglat(np.asarray(local, float)).tolist()


def test_error_is_the_distance_between_registered_and_measured_positions(mapped):
    result = field_checks.add('map', {'pixel': [50, 150], 'lnglat': gps([53, 54]), 'accuracyM': 4, 'measuredAt': 1})
    check = result['checks'][0]
    assert check['usable'] and check['errorM'] == pytest.approx(5, abs=1e-6)
    assert np.allclose(Frame().to_local(check['predictedLnglat']), [50, 50])
    assert result['summary'] == {'count': 1, 'usable': 1, 'medianM': 5., 'p95M': 5., 'maxM': 5., 'accuracyMedianM': 4.,
                                 'thresholds': field_checks.THRESHOLDS, 'maxAccuracyM': 10., 'minimumForVerdict': 5}
    assert store.get_state('map')['version'] == 4


def test_imprecise_or_outside_points_are_kept_but_not_counted_and_verdict_needs_five(mapped):
    for offset in (1, 2, 1, 2, 1):
        field_checks.add('map', {'pixel': [100, 100], 'lnglat': gps([100+offset, 100]), 'accuracyM': 5, 'measuredAt': 1})
    field_checks.add('map', {'pixel': [100, 100], 'lnglat': gps([130, 100]), 'accuracyM': 25, 'measuredAt': 1})
    result = field_checks.add('map', {'pixel': [200.5, 100], 'lnglat': gps([0, 0]), 'accuracyM': 3, 'measuredAt': 1})
    summary = result['summary']
    assert (summary['count'], summary['usable'], summary['medianM'], summary['maxM']) == (7, 5, 1., 2.)
    assert summary['meetsThresholds'] is True
    assert result['checks'][5]['errorM'] == pytest.approx(30) and not result['checks'][5]['usable']
    assert result['checks'][6]['errorM'] is None and not result['checks'][6]['insideRegistration']
    after = field_checks.remove('map', result['checks'][0]['id'])
    assert after['summary']['usable'] == 4 and 'meetsThresholds' not in after['summary']
    with pytest.raises(ValueError, match='不存在'):
        field_checks.remove('map', 'missing')
    with pytest.raises(ValueError, match='超出原图'):
        field_checks.add('map', {'pixel': [500, 1], 'lnglat': gps([0, 0]), 'accuracyM': 3, 'measuredAt': 1})


def test_errors_follow_a_later_recalibration(mapped):
    field_checks.add('map', {'pixel': [50, 150], 'lnglat': gps([50, 50]), 'accuracyM': 3, 'measuredAt': 1})
    state = store.get_state('map')
    pixels = [[0, 0], [200, 0], [200, 200], [0, 200]]
    shifted = [[1, 0, 10], [0, -1, 200]]
    state['registration'] = Registration(shifted, pixels, affine_points(shifted, pixels)).as_dict()
    store.save_state('map', state)
    assert field_checks.report('map')['checks'][0]['errorM'] == pytest.approx(10)


def test_capture_time_and_exported_model_are_frozen_independently_of_later_calibration(mapped):
    state = store.get_state('map'); state['sourceHash'] = 'sheet-A'; store.save_state('map', state)
    instant = 1792803928000
    result = field_checks.add('map', {'pixel': [50, 150], 'lnglat': gps([53, 54]), 'accuracyM': 4,
        'measuredAt': instant, 'receivedAt': instant, 'rawTimestamp': 12345, 'positionClock': 'received',
        'sourceHash': 'sheet-A', 'imageSize': [201, 201]}, now=instant/1000+120)
    row = result['checks'][0]
    assert row['measuredAt'] == instant and row['recordedAt'] == instant/1000+120
    assert row['rawTimestamp'] == 12345 and row['positionClock'] == 'received'
    exported = field_checks.export_record('map')
    assert exported['schema'] == 'dirmap-field-checks-v1'
    assert exported['map']['sourceHash'] == 'sheet-A' and exported['map']['imageSize'] == [201, 201]
    predicted, valid = Registration.from_dict(exported['map']['registration']).forward([row['pixel']])
    assert valid.all()
    np.testing.assert_allclose(np.linalg.norm(predicted-Frame.of(exported['map']).to_local([row['lnglat']]), axis=1), [row['errorM']])
    state['registration']['affine'][0][2] += 10
    state['registration']['target'] = (np.array(state['registration']['target'])+[10, 0]).tolist()
    store.save_state('map', state)
    assert exported['map']['registration']['affine'][0][2] == 0
    assert exported['checks'][0]['errorM'] == pytest.approx(5)
    assert field_checks.export_record('map')['checks'][0]['errorM'] != pytest.approx(5)
    assert store.get_state('map')['version'] == 4


def test_changed_image_identity_is_rejected_or_excluded_from_the_comparison(mapped):
    state = store.get_state('map'); state['sourceHash'] = 'sheet-A'; store.save_state('map', state)
    check = {'pixel': [50, 150], 'lnglat': gps([50, 50]), 'accuracyM': 4, 'measuredAt': 1,
             'sourceHash': 'sheet-A', 'imageSize': [201, 201]}
    with pytest.raises(ValueError, match='原图或像素尺寸'):
        field_checks.add('map', {**check, 'sourceHash': 'sheet-B'})
    with pytest.raises(ValueError, match='原图或像素尺寸'):
        field_checks.add('map', {**check, 'imageSize': [200, 201]})
    assert field_checks.report('map')['checks'] == []
    field_checks.add('map', check)
    state['sourceHash'] = 'sheet-B'; store.save_state('map', state)
    report = field_checks.report('map')
    assert report['summary']['usable'] == 0
    assert report['checks'][0]['pixelFrameMatches'] is False
    assert report['checks'][0]['errorM'] is None and report['checks'][0]['predictedLnglat'] is None


def test_legacy_observations_are_exported_without_fabricating_capture_metadata(mapped):
    store.atomic_json(field_checks.path('map'), [{'id': 'old', 'pixel': [50, 150], 'lnglat': gps([50, 50]),
        'accuracyM': 4, 'measuredAt': 1000, 'recordedAt': 2}])
    report = field_checks.export_record('map')
    assert report['checks'][0]['measuredAt'] == 1000
    assert 'receivedAt' not in report['checks'][0] and 'positionClock' not in report['checks'][0]
    assert report['checks'][0]['usable']
