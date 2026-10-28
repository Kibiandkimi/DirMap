"""Shared photos survive failed exports and are deleted only after receipt."""
import io
import json
from pathlib import Path
import time
import zipfile

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from dirmap import groups, photos, store
from dirmap.app import app


def jpeg(color='skyblue', size=(640, 480), orientation=None):
    image = Image.new('RGB', size, color)
    exif = Image.Exif()
    exif[270] = 'private camera metadata'
    if orientation:
        exif[274] = orientation
    output = io.BytesIO()
    image.save(output, 'JPEG', exif=exif)
    return output.getvalue()


@pytest.fixture
def classroom(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.delenv('DIRMAP_ADMIN_TOKEN', raising=False)
    release = {'id': 'a' * 32, 'workspace': store.LEGACY, 'title': '大沙河课程',
               'controls': [{'id': 'c1', 'label': '31'}, {'id': 'c2', 'label': '32'}, {'id': 'c3', 'label': '补点33'}, {'id': 'excluded', 'label': '99'}],
               'plan': {'routes': [{'controlIds': ['c3', 'c2', 'c1']}]}}
    directory = tmp_path/'releases'/release['id']
    directory.mkdir(parents=True)
    (directory/'release.json').write_text(json.dumps(release))
    host = groups.create('小王', 'host', release=release)
    colleague = groups.join(host['group']['code'], '小李', 'other')
    stranger = groups.create('其他组', 'stranger', release=release)
    with TestClient(app) as client:
        yield client, host, colleague, stranger


def headers(who, revision=''):
    return {'X-DirMap-Member': who['token'], 'X-DirMap-Photo-Revision': revision}


def url(who, suffix=''):
    return f'/api/groups/{who["groupId"]}/photos' + suffix


def upload(client, who, control='c1', data=None, revision='', upload_id=None):
    request_headers = headers(who, revision)
    if upload_id is not None:
        request_headers['X-DirMap-Photo-Upload'] = upload_id
    return client.put(url(who, '/'+control), headers=request_headers,
                      files={'file': ('camera.jpg', data or jpeg(), 'image/jpeg')})


def photo(album, control='c1'):
    return next(c['photo'] for c in album['controls'] if c['id'] == control)


def test_all_members_see_and_replace_one_photo_per_checkpoint(classroom):
    client, host, colleague, stranger = classroom
    album = client.get(url(host), headers=headers(host)).json()
    assert [c['label'] for c in album['controls']] == ['31', '32', '补点33']
    uploaded = upload(client, host, data=jpeg(size=(2400, 1800), orientation=6))
    assert uploaded.status_code == 200 and uploaded.headers['cache-control'] == 'no-store'
    album = uploaded.json()
    original = photo(album)
    assert album['count'] == 1 and (original['width'], original['height']) == (1200, 1600)
    assert original['author'] == '小王'
    shared = client.get(url(colleague), headers=headers(colleague)).json()
    assert shared == album
    image = client.get(url(host, '/c1/image'), params={'revision': original['revision']}, headers=headers(colleague))
    assert image.status_code == 200 and image.headers['cache-control'] == 'no-store'
    with Image.open(io.BytesIO(image.content)) as decoded:
        assert decoded.size == (1200, 1600) and not decoded.getexif()
    assert b'private camera metadata' not in image.content
    assert client.get(url(stranger), headers=headers(stranger)).json()['count'] == 0
    assert client.get(url(host), headers=headers(stranger)).status_code == 410
    assert client.get(url(host, '/c1/image'), params={'revision': original['revision']}).status_code == 410
    old_path = photos._path(host['groupId'], original['revision'])
    assert old_path.stat().st_mode & 0o777 == 0o600
    assert old_path.parent.stat().st_mode & 0o777 == 0o700
    replaced = upload(client, colleague, data=jpeg('orange'), revision=original['revision']).json()
    assert replaced['count'] == 1 and photo(replaced)['author'] == '小李'
    assert not old_path.exists() and not old_path.with_stem(old_path.stem+'-thumb').exists()
    sync = client.post(f'/api/groups/{host["groupId"]}/sync', headers=headers(host), json={}).json()
    assert sync['photoCount'] == 1 and sync['photoRevision'] == replaced['revision']
    assert sync['photoRevision'] != album['revision']


def test_stale_upload_or_delete_cannot_overwrite_a_teammates_picture(classroom):
    client, host, colleague, _ = classroom
    first = photo(upload(client, host).json())
    second = photo(upload(client, colleague, revision=first['revision']).json())
    assert upload(client, host, revision=first['revision']).status_code == 409
    assert client.delete(url(host, '/c1'), headers=headers(host, first['revision'])).status_code == 409
    assert photo(client.get(url(host), headers=headers(host)).json()) == second
    assert upload(client, host, control='excluded').status_code == 422
    removed = client.delete(url(host, '/c1'), headers=headers(host, second['revision']))
    assert removed.status_code == 200 and removed.json()['count'] == 0
    assert not photos._path(host['groupId'], second['revision']).exists()


def test_lost_upload_reply_can_be_retried_without_overwriting_or_restoring_photos(classroom):
    client, host, colleague, _ = classroom
    upload_id = 'a' * 32
    original = photo(upload(client, host, upload_id=upload_id).json())
    groups._schema.clear()  # Receipts survive a server restart.
    replay = upload(client, host, upload_id=upload_id)
    assert replay.status_code == 200 and photo(replay.json()) == original
    # This member's receipt cannot be reused for a different checkpoint.
    assert upload(client, host, control='c2', upload_id=upload_id).status_code == 409
    # The other member can independently use the same request identifier.
    replacement = photo(upload(client, colleague, revision=original['revision'], upload_id=upload_id).json())
    replay = upload(client, host, upload_id=upload_id)
    assert replay.status_code == 200 and photo(replay.json()) == replacement
    assert client.delete(url(host, '/c1'), headers=headers(host, replacement['revision'])).status_code == 200
    # Retrying a previously accepted upload never restores a cleared album.
    replay = upload(client, host, upload_id=upload_id)
    assert replay.status_code == 200 and replay.json()['count'] == 0
    assert not (store.DATA/'group-photos'/host['groupId']).exists()
    assert upload(client, host, upload_id='invalid').status_code == 422


def test_three_export_formats_include_every_uploaded_checkpoint_and_keep_originals(classroom):
    client, host, colleague, _ = classroom
    for control, color in [('c1', 'skyblue'), ('c2', 'orange'), ('c3', 'green')]:
        assert upload(client, host, control=control, data=jpeg(color)).status_code == 200
    outputs = {}
    for format in ('pdf', 'long-image', 'images'):
        result = client.post(url(colleague, '/export'), headers=headers(colleague), json={'format': format})
        assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
        assert result.headers['x-dirmap-photo-count'] == '3'
        assert len(result.headers['x-dirmap-photo-export']) == 32
        outputs[format] = result.content
    assert outputs['pdf'].startswith(b'%PDF-1.4') and b'/Count 3 ' in outputs['pdf']
    assert outputs['pdf'].count(b'/Subtype /Image') == 3
    with zipfile.ZipFile(io.BytesIO(outputs['images'])) as archive:
        assert archive.namelist() == ['01_CP_31.jpg', '02_CP_32.jpg', '03_CP_补点33.jpg']
        for name in archive.namelist():
            with Image.open(io.BytesIO(archive.read(name))) as image:
                assert image.size == (640, 480)
                assert image.getpixel((25, 450)) != image.getpixel((25, 25))  # Baked watermark badge.
    with Image.open(io.BytesIO(outputs['long-image'])) as image:
        assert image.size == (640, 1460)
        for y in (0, 490, 980):
            assert image.getpixel((25, y+450)) != image.getpixel((25, y+25))
    assert client.get(url(host), headers=headers(host)).json()['count'] == 3
    # Exported labels must differ in the actual pixels, not only file names.
    with Image.open(io.BytesIO(jpeg())) as image:
        assert photos.watermark(image, '31').tobytes() != photos.watermark(image, '32').tobytes()
        assert photos.watermark(image, '补点31').tobytes() != photos.watermark(image, '补点32').tobytes()


def test_finishing_clears_only_the_exported_versions_even_after_restart(classroom):
    client, host, colleague, _ = classroom
    first = photo(upload(client, host).json())
    upload(client, host, control='c2')
    exported = client.post(url(host, '/export'), headers=headers(host), json={'format': 'images'})
    export_id = exported.headers['x-dirmap-photo-export']
    # Another member replaces c1 and adds c3 after the export was generated.
    second = photo(upload(client, colleague, revision=first['revision']).json())
    upload(client, colleague, control='c3')
    # This receipt is private to the member who generated the download.
    assert client.post(url(colleague, '/finish'), headers=headers(colleague), json={'exportId': export_id}).status_code == 409
    groups._schema.clear()  # A new process recreates only missing tables.
    result = client.post(url(host, '/finish'), headers=headers(host), json={'exportId': export_id}).json()
    assert result['deleted'] == 1 and result['album']['count'] == 2
    assert photo(result['album']) == second and photo(result['album'], 'c2') is None
    assert client.post(url(host, '/finish'), headers=headers(host), json={'exportId': export_id}).json()['deleted'] == 0
    for path in (store.DATA/'group-photos'/host['groupId']).glob('*'):
        assert path.stem.removesuffix('-thumb') in {c['photo']['revision'] for c in result['album']['controls'] if c['photo']}


def test_failed_upload_and_export_keep_the_existing_photos(classroom, monkeypatch):
    client, host, _, _ = classroom
    first = photo(upload(client, host).json())
    assert upload(client, host, data=b'not a picture', revision=first['revision']).status_code == 422
    monkeypatch.setattr(photos, 'MIN_FREE_BYTES', 10**18)
    assert upload(client, host, revision=first['revision']).status_code == 507
    monkeypatch.setattr(photos, '_pdf', lambda *args: (_ for _ in ()).throw(ValueError('模拟导出中断')))
    assert client.post(url(host, '/export'), headers=headers(host), json={'format': 'pdf'}).status_code == 422
    assert photo(client.get(url(host), headers=headers(host)).json()) == first
    with groups.database() as db:
        assert db.execute('SELECT COUNT(*) FROM photo_exports').fetchone()[0] == 0
    assert photos._path(host['groupId'], first['revision']).exists()


def test_removed_members_lose_photo_access_and_disbanding_or_expiry_removes_files(classroom):
    client, host, colleague, stranger = classroom
    upload(client, host)
    upload(client, stranger)
    client.post(f'/api/groups/{host["groupId"]}/manage', headers=headers(host), json={'action': 'remove', 'memberId': colleague['memberId']})
    assert client.get(url(colleague), headers=headers(colleague)).status_code == 410
    assert upload(client, colleague, control='c2').status_code == 410
    client.post(f'/api/groups/{host["groupId"]}/manage', headers=headers(host), json={'action': 'close'})
    assert not (store.DATA/'group-photos'/host['groupId']).exists()
    assert (store.DATA/'group-photos'/stranger['groupId']).exists()
    groups.prune(now=time.time()+groups.MAX_AGE_SEC+1)
    assert not (store.DATA/'group-photos'/stranger['groupId']).exists()
    with groups.database() as db:
        assert db.execute('SELECT COUNT(*) FROM group_photos').fetchone()[0] == 0


def test_retention_recovers_a_crash_between_metadata_delete_and_file_removal(classroom, monkeypatch):
    client, host, _, _ = classroom
    original = photo(upload(client, host).json())
    with monkeypatch.context() as patch:
        patch.setattr(groups, 'remove_photo_files', lambda db: None)
        client.delete(url(host, '/c1'), headers=headers(host, original['revision']))
    assert photos._path(host['groupId'], original['revision']).exists()
    groups.prune()
    assert not photos._path(host['groupId'], original['revision']).exists()
    # And a file written just before a rolled-back metadata transaction.
    orphan = photos._path(host['groupId'], 'b'*32)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(jpeg())
    groups.prune()
    assert not orphan.exists()


def test_long_image_bounds_with_full_albums_of_portrait_and_landscape_photos():
    for sizes in [[(1200, 1600)]*60, [(1600, 900)]*60, [(1600, 900)]*20+[(600, 1600)]*40, [(700, 900)]*14]:
        width, heights, gap = photos.long_layout(sizes)
        height = sum(heights)+gap*(len(heights)-1)
        assert height <= photos.LONG_MAX_HEIGHT and width*height <= photos.LONG_MAX_PIXELS
        assert len(heights) == len(sizes) and min(heights) > 0
