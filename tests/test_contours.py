import cv2
import numpy as np
import pytest

from dirmap.contours import extract, contribute
from dirmap.elevation_model import reconstruct, HeightModel
from dirmap.geometry import Frame, Registration, affine_points
from dirmap import elevation, store


def contour_observations():
    xy = np.array([[x, y] for y in [0, 40, 80, 120] for x in np.arange(0, 161, 5.)])
    return {'id': 'contours', 'kind': 'contours', 'points': np.column_stack((Frame().to_lnglat(xy), 20+xy[:, 1]*.1))}


def test_brown_curves_are_candidates_without_invented_heights_or_court_rectangles():
    image = np.full((640, 720, 3), 255, np.uint8)
    cv2.rectangle(image, (30, 30), (690, 610), (150, 225, 170), -1)
    for radius in [60, 95, 130]:
        cv2.ellipse(image, (250, 320), (radius, round(radius*.8)), 17, 0, 360, (65, 125, 188), 3, cv2.LINE_8)
    cv2.rectangle(image, (470, 240), (620, 380), (65, 125, 188), 3)
    curves = extract(image, [{'radiusPx': 25}])
    assert len(curves) == 3 and all(c['closed'] and c['heightM'] is None for c in curves)
    assert all(np.asarray(c['pixels'])[:, 0].max() < 430 for c in curves)


def test_confirmed_contours_constrain_the_surface_and_anchor_phone_drift():
    contour = contour_observations()
    xy = np.array([[x, y] for x in [10, 70, 140] for y in np.arange(0, 121, 5.)])
    walk = {'id': 'phone', 'points': np.column_stack((Frame().to_lnglat(xy), 26+xy[:, 1]*.1))}
    queries = np.array([[30, 30], [70, 50], [110, 100]])
    for tracks in ([contour], [contour, walk]):
        metadata, arrays = reconstruct(tracks)
        heights = HeightModel(metadata, arrays).sample(Frame().to_lnglat(queries))
        np.testing.assert_allclose(heights, 20+queries[:, 1]*.1, atol=.15)
        assert metadata['contourSources'] == 1
        fixed = next(t for t in metadata['alignment']['tracks'] if t['id'] == 'contours')
        assert fixed['driftRangeM'] == 0 and fixed['correctionM'] == 0
        if len(tracks) > 1:
            phone = next(t for t in metadata['alignment']['tracks'] if t['id'] == 'phone')
            assert phone['correctionM'] == pytest.approx(-6., abs=.15)


@pytest.mark.parametrize('zero', [0., 1000.])
def test_relative_contour_zero_is_aligned_to_tracks_without_biasing_their_height(zero):
    contour = contour_observations()
    contour.update(heightReference='relative')
    contour['points'][:, 2] += zero
    xy = np.array([[x, y] for x in [10, 70, 140] for y in np.arange(0, 121, 5.)])
    walk = {'id': 'phone', 'points': np.column_stack((Frame().to_lnglat(xy), 50+xy[:, 1]*.1))}
    metadata, arrays = reconstruct([contour, walk])
    query = np.array([[30, 30], [70, 50], [110, 100]])
    heights = HeightModel(metadata, arrays).sample(Frame().to_lnglat(query))
    np.testing.assert_allclose(heights, 50+query[:, 1]*.1, atol=.2)
    assert metadata['datum'] == 'relative-recorded-height'
    assert metadata['relativeContourSources'] == 1
    aligned = {t['id']: t for t in metadata['alignment']['tracks']}
    assert aligned['contours']['driftRangeM'] == 0
    assert aligned['contours']['correctionM'] == pytest.approx(30-zero, abs=.2)
    assert aligned['phone']['correctionM'] == pytest.approx(0., abs=.2)


def test_relative_contours_wait_for_a_height_reference_instead_of_creating_zero_altitude():
    contour = contour_observations(); contour['heightReference'] = 'relative'
    metadata, arrays = reconstruct([contour])
    assert metadata['supportedCells'] == 0 and np.isnan(arrays['height']).all()
    assert metadata['alignment']['unanchoredComponents'] == 1
    assert metadata['alignment']['tracks'][0]['anchored'] is False
    assert arrays['observations'].shape == (0, 3)


def test_curved_relative_levels_constrain_a_hill_away_from_the_recorded_walk():
    angles = np.linspace(0, 2*np.pi, 32, endpoint=False)
    points = np.concatenate([np.column_stack((radius*np.cos(angles), radius*np.sin(angles), np.full(len(angles), level)))
                             for radius, level in [(40., 0.), (np.sqrt(1100.), 2.), (np.sqrt(600.), 4.)]])
    contour = {'id': 'hill', 'kind': 'contours', 'heightReference': 'relative',
               'points': np.column_stack((Frame().to_lnglat(points[:, :2]), points[:, 2]))}
    x = np.linspace(-65, 65, 120); xy = np.column_stack((x, np.full(len(x), 2.)))
    walk = {'id': 'walk', 'points': np.column_stack((Frame().to_lnglat(xy), 60-.004*np.sum(xy*xy, axis=1)+.6*np.sin(x/6)))}
    metadata, arrays = reconstruct([walk, contour])
    queries = np.array([[radius*np.cos(angle), radius*np.sin(angle)] for radius in [40., np.sqrt(1100.), np.sqrt(600.)]
                        for angle in np.radians([52.5, 127.5, 232.5, 307.5])])
    heights = HeightModel(metadata, arrays).sample(Frame().to_lnglat(queries))
    error = heights-(60-.004*np.sum(queries*queries, axis=1))
    # The noisy walk supplies the absolute datum; the held-out angular
    # locations separately check the contour shape, beyond that noise.
    datum_error = np.median(error)
    assert abs(datum_error) <= .6
    np.testing.assert_allclose(error-datum_error, 0, atol=.35)


def test_unanchored_relative_lines_do_not_add_a_height_step_beside_a_distant_track():
    contour = contour_observations(); contour['heightReference'] = 'relative'
    xy = np.array([[1000+x, y] for x in [0, 40, 80] for y in np.arange(0, 121, 5.)])
    walk = {'id': 'far-walk', 'points': np.column_stack((Frame().to_lnglat(xy), 50+xy[:, 1]*.1))}
    metadata, arrays = reconstruct([contour, walk])
    values = HeightModel(metadata, arrays).sample(Frame().to_lnglat([[70, 50], [1040, 50]]))
    assert np.isnan(values[0]) and 54 < values[1] < 56
    assert metadata['minHeightM'] > 40 and metadata['alignment']['unanchoredComponents'] == 1


def test_revised_contours_replace_only_the_contributors_previous_set(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    matrix = [[1, 0, 0], [0, -1, 150]]
    points = [[0, 0], [160, 0], [160, 150], [0, 150]]
    state = {'name': 'Park', 'sourceHash': 'map-image', 'registration': Registration(matrix, points, affine_points(matrix, points)).as_dict(),
             'frame': Frame().as_dict(), 'contours': [{'id': 'contour-aaaaaaaaaaaa', 'pixels': [[10, 40], [80, 40], [150, 40]]}]}
    alice, bob = {'id': 'alice', 'username': 'Alice'}, {'id': 'bob', 'username': 'Bob'}
    selected = lambda height: [{'id': state['contours'][0]['id'], 'heightM': height}]
    first = contribute(state, selected(30), alice)['added'][0]
    other = contribute(state, selected(31), bob)['added'][0]
    latest = contribute(state, selected(32), alice)['added'][0]
    records = {t['id']: t for t in elevation.catalogue()['tracks']}
    assert not records[first]['enabled'] and records[other]['enabled'] and records[latest]['enabled']
    elevation.change_track(latest, alice, elevation.catalogue()['version'], enabled=False)
    contribute(state, selected(32), alice)
    assert next(t for t in elevation.catalogue()['tracks'] if t['id'] == latest)['enabled']


def test_relative_levels_deduplicate_independent_of_the_chosen_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    matrix = [[1, 0, 0], [0, -1, 150]]
    corners = [[0, 0], [160, 0], [160, 150], [0, 150]]
    state = {'name': 'Park', 'sourceHash': 'same-map', 'frame': Frame().as_dict(),
        'registration': Registration(matrix, corners, affine_points(matrix, corners)).as_dict(),
        'contours': [{'id': 'contour-'+c*12, 'pixels': [[10, y], [80, y], [150, y]]} for c, y in [('a', 40), ('b', 80)]]}
    owner = {'id': 'alice', 'username': 'Alice'}
    chosen = lambda zero: [{'id': c['id'], 'heightM': zero+i*2} for i, c in enumerate(state['contours'])]
    first = contribute(state, chosen(20), owner, 'relative')
    again = contribute(state, chosen(1020), owner, 'relative')
    assert again['added'] == [] and again['duplicates'][0]['id'] == first['added'][0]
    record = elevation.catalogue()['tracks'][0]
    assert record['heightReference'] == 'relative' and record['minHeightM'] == 0 and record['maxHeightM'] == 2
    absolute = contribute(state, chosen(20), owner, 'reference')
    assert absolute['added'][0] != first['added'][0]


def test_unlabelled_contours_learn_independent_levels_from_crossing_tracks():
    contour = contour_observations()
    contour.update(heightReference='shape', breaks=np.arange(0, len(contour['points']), 33))
    contour['points'][:, 2] = 0
    xy = np.array([[x, y] for x in [10, 70, 140] for y in np.arange(0, 121, 5.)])
    walk = {'id': 'phone', 'points': np.column_stack((Frame().to_lnglat(xy), 50+xy[:, 1]*.1))}
    alone, raw = reconstruct([contour])
    assert alone['supportedCells'] == 0 and np.isnan(raw['height']).all()
    metadata, arrays = reconstruct([contour, walk])
    query = np.array([[30, 30], [70, 50], [110, 100]])
    heights = HeightModel(metadata, arrays).sample(Frame().to_lnglat(query))
    np.testing.assert_allclose(heights, 50+query[:, 1]*.1, atol=.2)
    assert metadata['alignment']['tracks'][0]['anchoredContours'] == 4
    assert set(arrays['observation_track']) == {0, 1}
    assert len(arrays['observation_track']) == len(arrays['observations'])


def test_an_unrelated_unlabelled_line_on_the_same_sheet_keeps_waiting():
    contour = contour_observations()
    count = len(contour['points'])
    contour.update(heightReference='shape', breaks=np.r_[np.arange(0, count, 33), count])
    far = np.column_stack((Frame().to_lnglat([[1000+x, 40] for x in range(0, 121, 5)]), np.zeros(25)))
    contour['points'] = np.vstack((contour['points'], far)); contour['points'][:, 2] = 0
    xy = np.array([[10, y] for y in np.arange(0, 121, 5.)])
    walk = {'id': 'phone', 'points': np.column_stack((Frame().to_lnglat(xy), 50+xy[:, 1]*.1))}
    metadata, arrays = reconstruct([contour, walk])
    assert metadata['alignment']['tracks'][0]['anchoredContours'] == 4
    assert metadata['alignment']['tracks'][0]['pendingContours'] == 1
    assert np.isnan(HeightModel(metadata, arrays).sample(Frame().to_lnglat([[1060, 40]]))).all()
