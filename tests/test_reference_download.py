import io
import json
import urllib.error
import urllib.request

import pytest

from dirmap import sites, store

BBOX = [113.95, 22.55, 113.955, 22.555]
XML = b'''<osm version="0.6">
  <node id="1" lat="22.551" lon="113.951"/>
  <node id="2" lat="22.551" lon="113.952"/>
  <node id="3" lat="22.552" lon="113.952"/>
  <node id="4" lat="22.552" lon="113.951"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/></way>
  <way id="11"><nd ref="1"/><nd ref="999"/><nd ref="4"/><tag k="highway" v="footway"/></way>
</osm>'''


def test_new_place_uses_a_read_only_map_when_all_overpass_mirrors_fail(monkeypatch):
    calls = []

    def response(request, timeout):
        calls.append((request.full_url, request.get_method()))
        if request.full_url in sites.OVERPASS:
            raise OSError('mirror unavailable')
        assert request.full_url.startswith('https://api.openstreetmap.org/api/0.6/map?bbox=')
        return io.BytesIO(XML)

    monkeypatch.setattr(urllib.request, 'urlopen', response)
    data, source = sites.download_reference(BBOX)
    assert [element['id'] for element in data['elements']] == [10]
    assert len(data['elements'][0]['geometry']) == 5
    assert source == 'https://api.openstreetmap.org/api/0.6/map'
    assert len(calls) == len(sites.OVERPASS)+1 and calls[-1][1] == 'GET'


def test_a_healthy_overpass_response_does_not_use_the_fallback(monkeypatch):
    calls = []

    def response(request, timeout):
        calls.append(request.full_url)
        assert request.full_url == sites.OVERPASS[0]
        return io.BytesIO(json.dumps({'elements': [{'id': 1}]}).encode())

    monkeypatch.setattr(urllib.request, 'urlopen', response)
    assert sites.download_reference(BBOX)[0] == {'elements': [{'id': 1}]}
    assert len(calls) == 1


def test_node_limit_splits_serially_and_joins_only_complete_geometry(monkeypatch):
    from dirmap import osm_reference
    calls = []
    # A way and its relation cross tile boundaries. Nodes arrive later;
    # the missing way in relation 21 must not become a fabricated polygon.
    tiles = [b'''<osm><node id="1" lat="22.551" lon="113.951"/>
      <node id="2" lat="22.551" lon="113.952"/>
      <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
      <relation id="20"><member type="way" ref="10" role="outer"/><tag k="building" v="yes"/></relation>
      <relation id="21"><member type="way" ref="10" role="outer"/>
        <member type="way" ref="99" role="outer"/><tag k="natural" v="water"/></relation></osm>''',
      b'<osm><node id="3" lat="22.552" lon="113.952"/><node id="4" lat="22.552" lon="113.951"/></osm>',
      b'<osm/>', b'<osm/>']

    def response(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 400, 'Bad Request', {},
                                         io.BytesIO(b'You requested too many nodes (limit is 50000)'))
        return io.BytesIO(tiles[len(calls)-2])

    monkeypatch.setattr(urllib.request, 'urlopen', response)
    data, _ = osm_reference.download_map(BBOX)
    assert len(calls) == 5 and len(set(calls)) == 5
    assert [e['id'] for e in data['elements'] if e['type'] == 'relation'] == [20]
    relation = next(e for e in data['elements'] if e['type'] == 'relation')
    points = relation['members'][0]['geometry']
    assert len(points) == 5 and points[0] == points[-1]


def test_rate_limits_do_not_trigger_a_tile_request_storm(monkeypatch):
    from dirmap import osm_reference
    calls = []

    def response(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'retry later'))

    monkeypatch.setattr(urllib.request, 'urlopen', response)
    with pytest.raises(ValueError, match='429'):
        osm_reference.download_map(BBOX)
    assert len(calls) == 1


def test_oversized_fallback_is_rejected_without_partial_results(monkeypatch):
    from dirmap import osm_reference
    monkeypatch.setattr(osm_reference, 'MAX_TOTAL_BYTES', 32)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(XML))
    with pytest.raises(ValueError, match='过多|过大'):
        osm_reference.download_map(BBOX)


@pytest.mark.parametrize('bbox', [[113.95, 22.55, 113.9, 22.56], [113.9, 22.5, 114.5, 23.]])
def test_fallback_is_limited_to_a_valid_teaching_viewport(monkeypatch, bbox):
    def unexpected(*args, **kwargs):
        pytest.fail('An invalid viewport reached the network')
    monkeypatch.setattr(urllib.request, 'urlopen', unexpected)
    with pytest.raises(ValueError, match='范围'):
        sites.download_reference(bbox)


def test_unusable_refresh_preserves_the_previous_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    site = {'id': 'site-test', 'bbox': BBOX, 'origin': [113.9525, 22.5525], 'status': 'ready'}
    monkeypatch.setattr(sites, 'get', lambda _: site)
    monkeypatch.setattr(sites, 'update', lambda _, **changes: site.update(changes) or site)
    monkeypatch.setattr(sites, 'download_reference', lambda *args: ({'elements': []}, 'fixture://empty'))
    path = store.workspace_root()/'sites/site-test/reference.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'previously downloaded reference')
    with pytest.raises(ValueError, match='地物|参照'):
        sites.fetch_reference('site-test')
    assert path.read_bytes() == b'previously downloaded reference'
    assert site['status'] == 'ready' and site['error']
