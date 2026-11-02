from test_api import client
from test_elevation_api import register


def test_field_export_keeps_workbench_access_and_checks_capture_metadata(client):
    base = '/api/maps/szu-1/field-checks'
    body = {'pixel': [10, 10], 'lnglat': [113.9304, 22.535], 'accuracyM': 4,
            'measuredAt': 1792803928000, 'receivedAt': 1792803928100,
            'rawTimestamp': 123, 'positionClock': 'received', 'sourceHash': 'fixture', 'imageSize': [41, 41]}
    assert client.post(base, json={**body, 'receivedAt': body['measuredAt']-1000}).status_code == 422
    assert client.post(base, json={**body, 'imageSize': None}).status_code == 422
    assert client.post(base, json=body).status_code == 200
    register(client, 'field-exporter', True)
    response = client.get(base+'/export')
    assert response.status_code == 200
    assert response.headers['content-disposition'].endswith('szu-1-gps-checks.json"')
    assert response.headers['cache-control'] == 'no-store'
    data = response.json()
    assert len(data['checks']) == 1 and data['map']['sourceHash'] == 'fixture'
    assert data['checks'][0]['measuredAt'] == body['measuredAt']
    assert data['checks'][0]['receivedAt'] == body['receivedAt']
    assert client.get('/api/maps/szu-1').json()['version'] == 1
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get(base+'/export').status_code == 401
