import cv2
import numpy as np
from fastapi.testclient import TestClient

from test_api import client, request as plan_request, wait_job
from dirmap import sheet, store
from dirmap.app import app


def test_reference_photos_are_private_then_frozen_with_the_course(client):
    state = store.get_state('szu-1')
    state['controls'][0]['state'] = 'accepted'
    state['sheetPhotos'] = [{'id': 'photo-1', 'label': '31', 'labelSource': 'tesseract',
                             'pixelBox': [2, 3, 28, 30], 'controlId': 'c1'}]
    image = np.full((41, 41, 3), (20, 95, 175), np.uint8)
    sheet.save_photos(image, state['sheetPhotos'], store.asset_dir('szu-1'))
    store.save_state('szu-1', state)
    original = client.get('/assets/maps/szu-1/sheet-photos/photo-1.jpg?v=1')
    assert original.status_code == 200
    assert cv2.imdecode(np.frombuffer(original.content, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (27, 26)
    assert client.get('/assets/maps/szu-1/sheet-photos/missing.jpg?v=1').status_code == 404
    assert client.get('/assets/maps/szu-1/sheet-photos/photo-1.jpg?v=2').status_code == 409
    owner = client.post('/api/auth/register', json={'username': 'photo-teacher', 'password': 'photo-password-123', 'claimOriginal': True})
    assert owner.status_code == 200, owner.text
    outsider = TestClient(app)
    assert outsider.get('/assets/maps/szu-1/sheet-photos/photo-1.jpg?v=1').status_code == 401
    assert client.post('/api/maps/szu-1/review', json={'version': 1, 'registrationChecked': True,
        'controlsChecked': True, 'passagesChecked': True}).status_code == 200
    plan = wait_job(client, client.post('/api/plans', json=plan_request()).json()['id'])['result']
    published = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']})
    assert published.status_code == 200, published.text
    identifier = published.json()['id']
    assert outsider.get(f'/api/releases/{identifier}/sheet-photos/photo-1.jpg').content == original.content
    changed = client.patch('/api/maps/szu-1/sheet-photos/photo-1', json={'version': 1, 'label': '42'})
    assert changed.status_code == 200 and changed.json()['sheetPhotos'][0]['controlId'] is None
    assert client.patch('/api/maps/szu-1/sheet-photos/photo-1', json={'version': 1, 'label': '31'}).status_code == 409
    sheet.save_photos(np.zeros_like(image), state['sheetPhotos'], store.asset_dir('szu-1'))
    assert outsider.get(f'/api/releases/{identifier}/sheet-photos/photo-1.jpg').content == original.content
    assert outsider.get(f'/api/releases/{identifier}').json()['sheetPhotos'][0]['label'] == '31'
