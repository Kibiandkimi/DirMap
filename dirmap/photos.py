"""A group's temporary checkpoint album and watermarked, portable exports."""
from contextlib import ExitStack
from functools import lru_cache
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
import warnings
import zipfile
import zlib

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from . import groups, store

MAX_UPLOAD_BYTES = 20 * 1024**2
MAX_PHOTO_BYTES = 2 * 1024**2
MAX_GROUP_BYTES = 120 * 1024**2
MIN_FREE_BYTES = 128 * 1024**2
MAX_SIDE = 1600
LONG_MAX_PIXELS = 24_000_000
LONG_MAX_HEIGHT = 30_000
# Decode/compose one image job at a time on the small classroom server.
RENDER = threading.Lock()


class PhotoConflict(ValueError):
    pass


class PhotoStorageFull(ValueError):
    pass


def context(db, group_id, token, now):
    me = groups.member(db, group_id, token)
    group = db.execute('SELECT * FROM groups WHERE id=?', (group_id,)).fetchone()
    if group['active_at'] + groups.IDLE_SEC < now or group['created_at'] + groups.MAX_AGE_SEC < now:
        raise groups.Gone('小组已过期')
    return group, me


def controls_for(group):
    if not group['release_id']:
        return [], None
    release = store.read_json(store.DATA/'releases'/group['release_id']/'release.json')
    if not release:
        raise ValueError('课程资料暂不可用，请稍后再试')
    selected = {identifier for route in release['plan']['routes'] for identifier in route['controlIds']}
    controls = [{'id': c['id'], 'label': str(c['label'])} for c in release['controls'] if c['id'] in selected]
    def order(control):
        return [(0, int(p)) if p.isdigit() else (1, p.casefold()) for p in re.split(r'(\d+)', control['label'])]
    return sorted(controls, key=order), release.get('title', '定向越野课')


def _rows(db, group_id):
    return db.execute('''SELECT p.*, m.name AS author FROM group_photos p
                         LEFT JOIN members m ON m.group_id=p.group_id AND m.id=p.member_id
                         WHERE p.group_id=? ORDER BY p.control_id''', (group_id,)).fetchall()


def album_view(db, group):
    controls, title = controls_for(group)
    rows = _rows(db, group['id'])
    pictures = {r['control_id']: {'revision': r['revision'], 'width': r['width'], 'height': r['height'],
                                 'bytes': r['bytes'], 'uploadedAt': r['uploaded_at'], 'author': r['author'] or '已离开组员'} for r in rows}
    return {'groupId': group['id'], 'releaseId': group['release_id'], 'title': title, 'groupTitle': group['title'],
            'expiresAt': min(group['active_at'] + groups.IDLE_SEC, group['created_at'] + groups.MAX_AGE_SEC),
            'revision': hashlib.sha256('|'.join(r['revision'] for r in rows).encode()).hexdigest()[:16],
            'count': len(rows), 'bytes': sum(r['bytes'] for r in rows),
            'controls': [{**c, 'photo': pictures.get(c['id'])} for c in controls]}


def album(group_id, token):
    with groups.database() as db:
        group, _ = context(db, group_id, token, time.time())
        return album_view(db, group)


def _existing(db, group_id, control_id, expected):
    row = db.execute('SELECT * FROM group_photos WHERE group_id=? AND control_id=?', (group_id, control_id)).fetchone()
    if (row['revision'] if row else '') != expected:
        raise PhotoConflict('这张照片已被组员更新或删除。请查看最新照片后再操作。')
    return row


def _jpeg(image, quality=86):
    output = io.BytesIO()
    image.info.clear()  # EXIF/GPS, thumbnails, comments and profiles stay private.
    image.save(output, 'JPEG', quality=quality)
    return output.getvalue()


def normalize(stream):
    stream.seek(0, os.SEEK_END)
    if stream.tell() > MAX_UPLOAD_BYTES:
        raise ValueError('每张照片最大 20 MiB，请选择较小的照片')
    stream.seek(0)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(stream) as original:
                if original.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise ValueError('照片支持 JPEG、PNG 或 WebP；请将 HEIC 转为 JPEG 后上传')
                width, height = original.size
                max_pixels = 100_000_000 if original.format == 'JPEG' else 30_000_000
                if min(width, height) < 32 or width * height > max_pixels:
                    raise ValueError('照片尺寸不合适，请选择普通拍照尺寸的图片')
                # JPEG's reduced decoder avoids allocating a full phone image.
                original.draft('RGB', (MAX_SIDE, MAX_SIDE))
                oriented = ImageOps.exif_transpose(original)
                oriented.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                if oriented.mode in ('RGBA', 'LA', 'P'):
                    rgba = oriented.convert('RGBA')
                    image = Image.new('RGB', rgba.size, 'white')
                    image.paste(rgba, mask=rgba.getchannel('A'))
                else:
                    image = oriented.convert('RGB')
                jpeg = _jpeg(image)
                if len(jpeg) > MAX_PHOTO_BYTES:
                    jpeg = _jpeg(image, 75)
                thumb = image.copy()
                thumb.thumbnail((280, 280), Image.Resampling.LANCZOS)
                return jpeg, _jpeg(thumb, 75), image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('无法读取照片，请检查文件是否完整并使用 JPEG、PNG 或 WebP') from exc


def _path(group_id, revision, thumbnail=False):
    return store.DATA/'group-photos'/group_id/(revision + ('-thumb.jpg' if thumbnail else '.jpg'))


def _replayed_upload(db, group_id, member_id, control_id, upload_id):
    if not upload_id:
        return False
    row = db.execute('SELECT control_id FROM photo_uploads WHERE group_id=? AND member_id=? AND upload_id=?',
                     (group_id, member_id, upload_id)).fetchone()
    if row and row['control_id'] != control_id:
        raise PhotoConflict('上传记录与检查点不一致，请重新选择照片')
    return row is not None


def put(group_id, token, control_id, expected, stream, upload_id=''):
    if upload_id and not re.fullmatch(r'[0-9a-f]{32}', upload_id):
        raise ValueError('上传标识无效，请重新选择照片')
    # Refuse an unauthorized upload before decoding any pixels.
    with groups.database() as db:
        group, me = context(db, group_id, token, time.time())
        if control_id not in {c['id'] for c in controls_for(group)[0]}:
            raise ValueError('请从课程的小组相册选择有效的检查点')
        # A lost success reply must not turn a retry into a replacement, even
        # if another member has since changed or cleared the shared photo.
        if _replayed_upload(db, group_id, me['id'], control_id, upload_id):
            return album_view(db, group)
        _existing(db, group_id, control_id, expected)
    with RENDER:
        jpeg, thumbnail, (width, height) = normalize(stream)
        revision = secrets.token_hex(16)
        paths = [_path(group_id, revision), _path(group_id, revision, True)]
        published = False
        try:
            with groups.transaction() as db:
                now = time.time()
                group, me = context(db, group_id, token, now)
                if _replayed_upload(db, group_id, me['id'], control_id, upload_id):
                    return album_view(db, group)
                previous = _existing(db, group_id, control_id, expected)
                size = len(jpeg) + len(thumbnail)
                used = db.execute('SELECT COALESCE(SUM(bytes),0) FROM group_photos WHERE group_id=?', (group_id,)).fetchone()[0]
                if used - (previous['bytes'] if previous else 0) + size > MAX_GROUP_BYTES:
                    raise PhotoStorageFull('小组照片已达到存储上限，请先导出并清理已保存的照片')
                if shutil.disk_usage(store.DATA).free < MIN_FREE_BYTES + size:
                    raise PhotoStorageFull('服务器空间不足，请稍后重试；已上传的照片仍可导出')
                paths[0].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                for path, content in zip(paths, (jpeg, thumbnail)):
                    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as target:
                        target.write(content)
                        target.flush()
                        os.fsync(target.fileno())
                db.execute('''INSERT INTO group_photos VALUES(?,?,?,?,?,?,?,?)
                              ON CONFLICT(group_id,control_id) DO UPDATE SET revision=excluded.revision,
                              member_id=excluded.member_id,width=excluded.width,height=excluded.height,
                              bytes=excluded.bytes,uploaded_at=excluded.uploaded_at''',
                           (group_id, control_id, revision, me['id'], width, height, size, now))
                if upload_id:
                    db.execute('INSERT INTO photo_uploads VALUES(?,?,?,?)', (group_id, me['id'], upload_id, control_id))
                db.execute('UPDATE groups SET active_at=? WHERE id=?', (now, group_id))
                result = album_view(db, db.execute('SELECT * FROM groups WHERE id=?', (group_id,)).fetchone())
            published = True
            return result
        finally:
            if not published:
                for path in paths:
                    path.unlink(missing_ok=True)


def picture(group_id, token, control_id, revision, thumbnail=False):
    with groups.transaction() as db:
        context(db, group_id, token, time.time())
        row = _existing(db, group_id, control_id, revision)
        if not row:
            raise ValueError('这个检查点还没有照片')
        return _path(group_id, row['revision'], thumbnail).read_bytes()


def delete(group_id, token, control_id, expected):
    with groups.transaction() as db:
        group, _ = context(db, group_id, token, time.time())
        _existing(db, group_id, control_id, expected)
        db.execute('DELETE FROM group_photos WHERE group_id=? AND control_id=?', (group_id, control_id))
        return album_view(db, group)


@lru_cache(maxsize=1)
def _cjk_font():
    # A compressed, licensed resource fits the existing release protocol's
    # data-file allowlist and does not need a system font installation.
    asset = json.loads(Path(__file__).with_name('photo-font.json').read_text())
    return zlib.decompress(base64.b64decode(asset['data']))


@lru_cache(maxsize=4)
def _fonts(size):
    # Droid supplies CJK, while Pillow's scalable built-in font supplies ASCII.
    return ImageFont.load_default(size=size), ImageFont.truetype(io.BytesIO(_cjk_font()), size)


def watermark(image, label):
    """Bake the full checkpoint label into the pixels, including Chinese."""
    image = image.convert('RGB')
    width, height = image.size
    margin = max(3, round(min(width, height) * .025))
    text = 'CP ' + str(label)
    size = max(12, min(56, round(min(width, height) * .065)))
    while True:
        latin, cjk = _fonts(size)
        glyphs = [(ch, latin if ord(ch) < 256 else cjk) for ch in text]
        length = math.ceil(sum(font.getlength(ch) for ch, font in glyphs))
        boxes = [font.getbbox(ch, anchor='ls') for ch, font in glyphs]
        top, bottom = min(b[1] for b in boxes), max(b[3] for b in boxes)
        if (length <= width - margin * 4 and bottom - top <= height - margin * 4) or size <= 4:
            break
        size -= 1
    draw = ImageDraw.Draw(image, 'RGBA')
    x, y = margin * 2, height - margin * 2 - bottom
    draw.rounded_rectangle((margin, y + top - margin, x + length + margin, height - margin), radius=margin, fill=(15, 27, 24, 200))
    for ch, font in glyphs:
        draw.text((x, y), ch, font=font, anchor='ls', fill=(255, 255, 255, 255))
        x += font.getlength(ch)
    return image


def long_layout(sizes):
    ratios = [height / width for width, height in sizes]
    ratio = sum(ratios) + .015 * (len(sizes) - 1)
    width = max(1, min(1200, max(w for w, _ in sizes), int(math.sqrt(LONG_MAX_PIXELS / ratio)), int(LONG_MAX_HEIGHT / ratio)))
    while True:
        gap = max(2, round(width * .015))
        heights = [max(1, round(width * r)) for r in ratios]
        height = sum(heights) + gap * (len(heights) - 1)
        if (height <= LONG_MAX_HEIGHT and width * height <= LONG_MAX_PIXELS) or width == 1:
            return width, heights, gap
        width -= 1


def _pdf(items, count):
    """A4 pages with JPEG XObjects; no font, PDF or image recompression library.

    Write one image at a time instead of decoding the whole album at once.
    """
    output = io.BytesIO()
    output.write(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    def obj(number, body):
        offsets.append(output.tell())
        output.write(f'{number} 0 obj\n'.encode() + body + b'\nendobj\n')
    def stream(content, fields=b''):
        return b'<< ' + fields + f' /Length {len(content)} >>\nstream\n'.encode() + content + b'\nendstream'
    obj(1, b'<< /Type /Catalog /Pages 2 0 R >>')
    kids = ' '.join(f'{3 + i * 3} 0 R' for i in range(count))
    obj(2, f'<< /Type /Pages /Count {count} /Kids [{kids}] >>'.encode())
    for i, (jpeg, (width, height)) in enumerate(items):
        page, content, image = 3 + i * 3, 4 + i * 3, 5 + i * 3
        pw, ph = (842, 595) if width > height else (595, 842)
        scale = min((pw - 48) / width, (ph - 48) / height)
        w, h = width * scale, height * scale
        obj(page, f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw} {ph}] /Resources << /XObject << /Photo {image} 0 R >> >> /Contents {content} 0 R >>'.encode())
        obj(content, stream(f'q {w:.3f} 0 0 {h:.3f} {(pw-w)/2:.3f} {(ph-h)/2:.3f} cm /Photo Do Q'.encode()))
        obj(image, stream(jpeg, f'/Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode'.encode()))
    xref = output.tell()
    output.write(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        output.write(f'{offset:010d} 00000 n \n'.encode())
    output.write(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return output.getvalue()


def export(group_id, token, format):
    if format not in ('pdf', 'long-image', 'images'):
        raise ValueError('请选择 PDF、长图或多图')
    with RENDER, ExitStack() as stack:
        # Keep descriptors for this snapshot: a teammate may replace or delete
        # a photo while we export, but this export still sees complete files.
        with groups.transaction() as db:
            group, me = context(db, group_id, token, time.time())
            controls, _ = controls_for(group)
            rows = {r['control_id']: r for r in _rows(db, group_id)}
            selected = [(c, rows[c['id']]) for c in controls if c['id'] in rows]
            if not selected:
                raise ValueError('小组还没有上传照片')
            sources = [(c, row, stack.enter_context(_path(group_id, row['revision']).open('rb'))) for c, row in selected]

        def images():
            for control, row, source in sources:
                with Image.open(source) as image:
                    stamped = watermark(image, control['label'])
                    yield _jpeg(stamped), (row['width'], row['height'])

        if format == 'pdf':
            content, media, suffix = _pdf(images(), len(sources)), 'application/pdf', 'pdf'
        elif format == 'images':
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
                for i, ((jpeg, _), (control, _, _)) in enumerate(zip(images(), sources), 1):
                    label = re.sub(r'[^\w.-]+', '_', control['label'])[:60]
                    archive.writestr(f'{i:02d}_CP_{label}.jpg', jpeg)
            content, media, suffix = output.getvalue(), 'application/zip', 'zip'
        else:
            width, heights, gap = long_layout([(r['width'], r['height']) for _, r, _ in sources])
            sheet = Image.new('RGB', (width, sum(heights) + gap * (len(heights) - 1)), 'white')
            y = 0
            for (control, _, source), height in zip(sources, heights):
                with Image.open(source) as image:
                    part = watermark(image.resize((width, height), Image.Resampling.LANCZOS), control['label'])
                    sheet.paste(part, (0, y))
                y += height + gap
            content, media, suffix = _jpeg(sheet, 88), 'image/jpeg', 'jpg'
        export_id = secrets.token_hex(16)
        manifest = {c['id']: r['revision'] for c, r in selected}
        with groups.transaction() as db:
            context(db, group_id, token, time.time())
            db.execute('INSERT INTO photo_exports VALUES(?,?,?,?,?)', (export_id, group_id, me['id'], json.dumps(manifest), time.time()))
            # Keep manifests small even if a group repeatedly exports.
            db.execute('''DELETE FROM photo_exports WHERE group_id=? AND id NOT IN
                          (SELECT id FROM photo_exports WHERE group_id=? ORDER BY created_at DESC LIMIT 30)''', (group_id, group_id))
        return {'content': content, 'mediaType': media, 'filename': f'dirmap-{group["code"]}-photos.{suffix}', 'exportId': export_id, 'count': len(selected)}


def finish(group_id, token, export_id):
    with groups.transaction() as db:
        group, me = context(db, group_id, token, time.time())
        exported = db.execute('SELECT * FROM photo_exports WHERE id=? AND group_id=? AND member_id=?', (export_id, group_id, me['id'])).fetchone()
        if not exported:
            raise PhotoConflict('导出记录已过期，请重新导出后再结束')
        deleted = 0
        for identifier, revision in json.loads(exported['manifest']).items():
            deleted += db.execute('DELETE FROM group_photos WHERE group_id=? AND control_id=? AND revision=?', (group_id, identifier, revision)).rowcount
        # Retain the small receipt so retrying a lost response is harmless.
        return {'deleted': deleted, 'album': album_view(db, group)}
