import cv2
import numpy as np
import pytest

from test_api import client, wait_job, request as plan_request
from test_elevation_api import register
from dirmap import elevation, store, service
from dirmap.geometry import Frame


def test_map_contours_require_ownership_and_current_registration_then_rebuild_shared_height(client):
    state = store.get_state('szu-1')
    state['contours'] = [{'id': 'contour-'+letter*12, 'pixels': [[3, y], [20, y], [37, y]],
                           'heightM': None, 'closed': False, 'lengthPx': 34}
                          for letter, y in [('a', 8), ('b', 18), ('c', 30)]]
    store.save_state('szu-1', state)
    report = client.get('/api/maps/szu-1/contours').json()
    assert len(report['contours']) == 3 and all(c['calibrated'] and c['suggestedHeightM'] is None for c in report['contours'])
    payload = {'version': 1, 'intervalM': 2, 'contours': [{'id': c['id'], 'heightM': z} for c, z in zip(state['contours'], [30, 25, 20])]}
    assert client.post('/api/maps/szu-1/contours', json=payload).status_code in (401, 403)
    assert elevation.catalogue()['tracks'] == []
    owner = register(client, 'contour-teacher', True)
    state['controls'][0]['state'] = 'accepted'
    store.save_state('szu-1', state)
    assert client.post('/api/maps/szu-1/review', json={'version': 1, 'registrationChecked': True,
        'controlsChecked': True, 'passagesChecked': True}).status_code == 200
    plan = wait_job(client, client.post('/api/plans', json=plan_request()).json()['id'])['result']
    published = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']})
    assert published.status_code == 200, published.text
    course_id = published.json()['id']
    response = client.post('/api/maps/szu-1/contours', json=payload)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['state']['version'] == 1 and receipt['state']['contourRevision'] == 1
    assert receipt['state']['reviewed']
    job = wait_job(client, receipt['job']['id'])
    assert job['state'] == 'succeeded', job
    track = elevation.catalogue()['tracks'][0]
    assert track['kind'] == 'contours' and track['contourCount'] == 3 and track['ownerId'] == owner['id']
    heights = elevation.Sampler(elevation.models()['regions']).sample(Frame().to_lnglat([[20, 20]]))
    assert np.isfinite(heights).all() and 23 < heights[0] < 26
    assert client.post('/api/maps/szu-1/contours', json=payload).status_code == 409
    assert len(elevation.catalogue()['tracks']) == 1
    course = client.get('/api/releases/'+course_id).json()
    assert not course['superseded']
    assert course['plan']['elevationSnapshot'] == plan['elevationSnapshot']
    assert service.get_graph(store.get_state('szu-1'), elevation_snapshot=plan['elevationSnapshot']).content_hash == plan['graphHash']


def test_calibration_cannot_overwrite_contour_annotations_saved_during_warp(client, monkeypatch):
    initial = store.get_state('szu-1')
    fired = False
    def warp(_image, _reg, output, **_options):
        nonlocal fired
        if not fired:
            latest = store.get_state('szu-1')
            latest.update(contourRevision=1, contourHeights={'contour-aaaaaaaaaaaa': 30})
            store.save_state('szu-1', latest); fired = True
        cv2.imwrite(str(output), np.zeros((4, 4, 4), np.uint8))
        return [0, 0, 40, 40]
    monkeypatch.setattr(service, 'warp_overlay', warp)
    frame = Frame.of(initial)
    anchors = [{'pixel': p, 'lnglat': frame.to_lnglat([p])[0].tolist(), 'purpose': 'fit'}
               for p in [[0, 0], [40, 0], [40, 40], [0, 40]]]
    with pytest.raises(ValueError, match='校准期间地图被修改'):
        service.apply_anchors('szu-1', anchors, 1)
    current = store.get_state('szu-1')
    assert current['version'] == 1 and current['contourHeights'] == {'contour-aaaaaaaaaaaa': 30}
    assert current['registration'] == initial['registration']
