import cv2
import numpy as np

from test_api import client, wait_job
from dirmap import store, service
from dirmap.geometry import Frame, Registration, affine_points


def test_a_refused_registration_offers_its_places_and_the_teacher_adopts_one(client, monkeypatch):
    state = store.get_state('szu-1')
    state['quality']['method'] = 'automatic_reference_fit'
    store.save_state('szu-1', state)
    source = store.DATA/'source.png'; cv2.imwrite(str(source), np.full((41, 41, 3), 245, np.uint8))
    monkeypatch.setattr(store, 'get_asset', lambda asset_id: {'id': asset_id, 'path': str(source),
        'sha256': 'fixture', 'name': 'test', 'filename': 'source.png'})
    pixels = [[0, 0], [40, 0], [40, 40], [0, 40]]
    near, far = [[1, 0, 2], [0, -1, 42]], [[1, 0, 302], [0, -1, 42]]
    def ambiguous(image, result, reference, places=None):
        for value, affine in ((.33, near), (.31, far)):
            places.append((value, Registration(affine, pixels, affine_points(affine, pixels)),
                           {'method': 'global_search_layer_correlation', 'p95ErrorM': 4.2, 'landmarks': [],
                            'validationScope': '图块留一核查。'}))
        return None, None, ['存在吻合度相近的不同位置（0.33 与 0.31），请补充同名点确定位置']
    monkeypatch.setattr(service, 'geographic_registration', ambiguous)
    done = wait_job(client, client.post('/api/maps/szu-1/process').json()['id'])
    assert done['state'] == 'succeeded', done
    refused = client.get('/api/maps/szu-1').json()
    assert refused['registration'] is None and 'extent' not in refused
    offered = refused['registrationCandidates']
    assert [p['id'] for p in offered] == ['place-1', 'place-2'] and offered[0]['support'] == .33
    assert client.get(f'/assets/maps/szu-1/{offered[1]["preview"]}?v={refused["version"]}').status_code == 200
    # The choice is the teacher's: a stale version or an unknown place is rejected.
    assert client.post('/api/maps/szu-1/places/place-2', json={'version': refused['version']-1}).status_code == 409
    assert client.post('/api/maps/szu-1/places/overlay', json={'version': refused['version']}).status_code == 422
    job = client.post('/api/maps/szu-1/places/place-2', json={'version': refused['version']}).json()
    done = wait_job(client, job['id'])
    assert done['state'] == 'succeeded', done
    adopted = client.get('/api/maps/szu-1').json()
    assert adopted['version'] == refused['version']+1 and not adopted['reviewed']
    assert np.allclose(adopted['registration']['affine'], far)
    assert adopted['quality']['chosenPlace'] == {'id': 'place-2', 'support': .31, 'offered': 2}
    assert adopted['quality']['state'] == 'needs_review' and '教师' in adopted['quality']['validationScope']
    assert adopted['extent'] and client.get(f'/assets/maps/szu-1/overlay.png?v={adopted["version"]}').status_code == 200
    # The other place stays available in case the first choice was wrong.
    assert [p['id'] for p in adopted['registrationCandidates']] == ['place-1', 'place-2']
    # Re-recognition of this source must keep the teacher's place decision.
    def must_keep_choice(*args, **kwargs):
        raise AssertionError('A teacher-selected place was sent back to automatic matching')
    monkeypatch.setattr(service, 'geographic_registration', must_keep_choice)
    done = wait_job(client, client.post('/api/maps/szu-1/process').json()['id'])
    assert done['state'] == 'succeeded', done
    refreshed = client.get('/api/maps/szu-1').json()
    assert refreshed['registration'] == adopted['registration']
    assert refreshed['quality']['chosenPlace'] == adopted['quality']['chosenPlace']
    assert refreshed['registrationCandidates'] == adopted['registrationCandidates']
    assert client.get(f'/assets/maps/szu-1/{offered[0]["preview"]}?v={refreshed["version"]}').status_code == 200
    # Explicit new landmarks supersede the old offers, including their API.
    frame = Frame.of(refreshed)
    anchors = [{'pixel': p, 'lnglat': frame.to_lnglat(t).tolist()}
               for p, t in zip(pixels, affine_points(near, pixels))]
    done = wait_job(client, client.post('/api/maps/szu-1/anchors',
                    json={'version': refreshed['version'], 'anchors': anchors}).json()['id'])
    assert done['state'] == 'succeeded', done
    anchored = client.get('/api/maps/szu-1').json()
    assert not anchored.get('registrationCandidates') and not anchored['quality'].get('chosenPlace')
    assert all(p['preview'] not in anchored['artifacts'] for p in offered)
    done = wait_job(client, client.post('/api/maps/szu-1/places/place-1',
                    json={'version': anchored['version']}).json()['id'])
    assert done['state'] == 'failed' and '已失效' in done['message']
