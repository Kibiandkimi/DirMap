from datetime import datetime, timezone

import numpy as np
import pytest

from dirmap import elevation, store
from dirmap.elevation_model import HeightModel, reconstruct, slope_factors
from dirmap.geometry import Frame


def kml(points, name='Recorded walk', gx=False):
    if gx:
        coordinates = ''.join(f'<gx:coord>{x} {y} {z}</gx:coord>' for x, y, z in points)
        geometry = f'<gx:Track>{coordinates}</gx:Track>'
    else:
        coordinates = ' '.join(f'{x},{y},{z}' for x, y, z in points)
        geometry = f'<LineString><coordinates>{coordinates}</coordinates></LineString>'
    return (f'<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">'
            f'<Document><name>{name}</name><Placemark>{geometry}</Placemark></Document></kml>').encode()


def plane(offset=0, shift=0):
    x, y = np.meshgrid(np.arange(0, 121, 8), np.arange(0, 121, 8))
    xy = np.column_stack((x.ravel()+shift, y.ravel()))
    return np.column_stack((Frame().to_lnglat(xy), 20+xy[:, 0]*.1+xy[:, 1]*.05+offset))


def test_track_formats_dedupe_by_observations_and_do_not_read_markup():
    points = plane()
    first, arrays = elevation.parse_kml(kml(points, gx=True), 'first.kml')
    second, _ = elevation.parse_kml(kml(points, 'Changed export name'), 'second.KML')
    assert first['id'] == second['id'] and len(arrays['points']) == len(points)
    malicious = b'<!DOCTYPE kml [<!ENTITY x SYSTEM "file:///etc/passwd">]><kml>&x;</kml>'
    with pytest.raises(ValueError, match='DTD'):
        elevation.parse_kml(malicious, 'bad.kml')
    with pytest.raises(ValueError, match='全部为 0'):
        elevation.parse_kml(kml([(113, 22, 0), (113.01, 22, 0), (113, 22.01, 0)]), 'zero.kml')
    with pytest.raises(ValueError, match='相对地面'):
        elevation.parse_kml(kml(points).replace(b'<LineString>', b'<LineString><altitudeMode>relativeToGround</altitudeMode>'), 'relative.kml')


def test_shared_observations_keep_owner_and_identical_uploads_do_not_claim_them(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    alice = {'id': 'alice', 'username': 'Alice'}
    bob = {'id': 'bob', 'username': 'Bob'}
    with store.using('a'*16):
        first = elevation.add_tracks([('walk.kml', kml(plane()))], alice)
    with store.using('b'*16):
        same = elevation.add_tracks([('another-name.kml', kml(plane()))], bob)
        assert same['added'] == [] and same['version'] == first['version']
        identifier = first['added'][0]
        with pytest.raises(elevation.NotOwner):
            elevation.change_track(identifier, bob, first['version'], delete=True)
        assert elevation.catalogue()['tracks'][0]['ownerId'] == alice['id']
    changed = elevation.change_track(identifier, alice, first['version'], enabled=False)
    assert changed['version'] == first['version']+1
    with pytest.raises(elevation.Conflict):
        elevation.change_track(identifier, alice, first['version'], delete=True)


def test_overlapping_tracks_align_relative_heights_and_interpolate_held_out_positions():
    tracks = [{'id': 'reference', 'points': plane()}, {'id': 'barometer-offset', 'points': plane(15)}]
    metadata, arrays = reconstruct(tracks)
    corrections = {t['id']: t['correctionM'] for t in metadata['alignment']['tracks']}
    # The mean recording sets the datum; only the difference is observable.
    assert corrections['barometer-offset']-corrections['reference'] == pytest.approx(-15, abs=.05)
    model = HeightModel(metadata, arrays)
    xy = np.array([[35, 45], [55, 65], [500, 500]])
    result = model.sample(Frame().to_lnglat(xy))-corrections['reference']
    np.testing.assert_allclose(result[:2], 20+xy[:2, 0]*.1+xy[:2, 1]*.05, atol=.35)
    assert np.isnan(result[2])  # The convex hull and distance bound are real, not decorative.


def crossing_walks():
    """Two phones walk a grid of crossing paths; the second one drifts."""
    def lines(vertical):
        parts = []
        for k in range(7):
            along = np.arange(0, 301, 2.)[::1 if k % 2 == 0 else -1]
            across = np.full_like(along, 20+k*40)
            parts.append(np.column_stack((across, along) if vertical else (along, across)))
        return np.concatenate(parts)
    def ground(xy):
        return 20+8*np.exp(-((xy[:, 0]-150)**2+(xy[:, 1]-150)**2)/(2*80**2))
    walks = []
    for identifier, xy, offset, drift, seed in (('steady', lines(False), 0, 0, 1), ('drifting', lines(True), 5, .004, 2)):
        seconds = np.r_[0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]/1.2
        heights = ground(xy)+offset+drift*seconds+np.random.default_rng(seed).normal(0, .8, len(xy))
        walks.append({'id': identifier, 'points': np.column_stack((Frame().to_lnglat(xy), heights)), 'times': 1.8e9+seconds})
    return walks, ground


def test_crossing_recordings_separate_a_phones_height_drift_from_the_ground():
    walks, ground = crossing_walks()
    metadata, arrays = reconstruct(walks)
    tracks = {t['id']: t for t in metadata['alignment']['tracks']}
    # 0.004 m/s over the drifting walk's 1960 s: about 7.8 m of drift.
    assert tracks['drifting']['driftRangeM'] == pytest.approx(7.8, abs=.6) and tracks['steady']['driftRangeM'] < 1
    assert tracks['drifting']['correctionM']-tracks['steady']['correctionM'] == pytest.approx(-5-3.9, abs=.8)
    xy = np.mgrid[30:280:10, 30:280:10].reshape(2, -1).T.astype(float)
    error = HeightModel(metadata, arrays).sample(Frame().to_lnglat(xy))-ground(xy)
    assert np.isfinite(error).all()
    error -= np.median(error)
    assert np.percentile(np.abs(error), 95) < .4


def test_timed_tracks_keep_their_clock_and_a_duplicate_linestring_is_not_counted_twice():
    points = plane()[:20]
    whens = ''.join(f'<when>2026-10-27T11:00:{i:02d}Z</when>' for i in range(20))
    coordinates = ''.join(f'<gx:coord>{x} {y} {z}</gx:coord>' for x, y, z in points)
    line = ' '.join(f'{x},{y},{z}' for x, y, z in points)
    content = (f'<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document>'
               f'<Placemark><gx:Track>{whens}{coordinates}</gx:Track></Placemark>'
               f'<Placemark><LineString><coordinates>{line}</coordinates></LineString></Placemark></Document></kml>').encode()
    summary, arrays = elevation.parse_kml(content, 'timed.kml')
    assert summary['pointCount'] == 20 and np.all(np.diff(arrays['times']) == 1)
    assert summary['recordedAt'] == datetime(2026, 10, 27, 11, tzinfo=timezone.utc).timestamp()
    untimed, plain = elevation.parse_kml(kml(points), 'line.kml')
    assert untimed['id'] == summary['id'] and untimed['recordedAt'] is None and np.isnan(plain['times']).all()


def test_rebuilding_and_removing_contributions_change_the_model_but_renaming_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    owner = {'id': 'owner', 'username': 'Owner'}
    first = elevation.add_tracks([('one.kml', kml(plane()))], owner)
    built = elevation.rebuild(first['version'], operation_id='first')
    assert len(built['regions']) == 1 and not built['needsRebuild']
    region_id = built['regions'][0]['id']; signature = built['regions'][0]['fingerprint']
    renamed = elevation.change_track(first['added'][0], owner, first['version'], name='New name')
    again = elevation.rebuild(renamed['version'])
    assert again['regions'][0]['fingerprint'] == signature and again['regions'][0]['id'] == region_id
    pinned = elevation.root()/built['regions'][0]['artifact']
    heights = pinned.read_bytes()
    second = elevation.add_tracks([('two.kml', kml(plane(5, 24)))], owner)
    latest = elevation.rebuild(second['version'])
    assert latest['regions'][0]['id'] == region_id and latest['regions'][0]['fingerprint'] != signature
    # A course may still use the earlier file; a rebuild never rewrites it.
    assert latest['regions'][0]['artifact'] != built['regions'][0]['artifact'] and pinned.read_bytes() == heights
    grid = elevation.model_grid(region_id)
    assert len(grid['height']) == grid['rows']*grid['columns'] and None in grid['height']
    disabled = elevation.change_track(first['added'][0], owner, latest['version'], enabled=False)
    assert elevation.public_status()['needsRebuild']
    with pytest.raises(elevation.Conflict):
        elevation.rebuild(latest['version'])
    elevation.rebuild(disabled['version'])


def test_grade_cost_is_directional_and_missing_height_is_not_invented():
    uphill, downhill = slope_factors([0, 10], [10, 0], [100, 100])
    assert uphill > 1 and downhill < uphill
    assert slope_factors(0, -5, 100) < 1
    assert slope_factors(0, -40, 100) > 1  # steep downhill is not arbitrarily faster
    assert slope_factors(np.nan, 20, 100) == 1
    assert slope_factors(0, 0, 100) == 1
