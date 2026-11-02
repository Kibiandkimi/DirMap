#!/usr/bin/env python3
"""Check the real Dashahe sheet and both campus collage encodings from pixels.

Photo positions below were read from the supplied sheets, independently of
the detector. This verifies crops, printed numbers and control links, not
geographic accuracy or arbitrary photographic page layouts.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def campus_center(label):
    number = int(label)
    if number <= 37:
        return [83+121*(number-31), 72]
    if number <= 42:
        return [83, 172+95*(number-38)]
    if number <= 45:
        return [808, 173+100*(number-43)]
    return [666 if number == 46 else 792, 650]


def preview(image, photos, path):
    from dirmap.photos import _fonts
    font = _fonts(22)[0]
    width, height, columns = 260, 220, 4
    canvas = Image.new('RGB', (width*columns, height*((len(photos)+columns-1)//columns)), '#fafaf5')
    draw = ImageDraw.Draw(canvas)
    source = Image.fromarray(image[:, :, ::-1])
    for index, photo in enumerate(sorted(photos, key=lambda p: p['label'])):
        crop = source.crop(photo['pixelBox']); crop.thumbnail((width-16, height-42))
        x, y = index % columns*width, index//columns*height
        canvas.paste(crop, (x+(width-crop.width)//2, y+8))
        draw.text((x+10, y+height-31), 'CP '+photo['label'], fill='#244735', font=font)
    canvas.save(path)


def main():
    from dirmap.images import read_map_image
    from dirmap.vision import analyze
    from dirmap.ocr import label_controls
    from dirmap.service import implementation_signature
    from verify_dashahe_real import photo_centre
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dashahe', type=Path, default=ROOT/'data/projects/upload-55496a067ba7/source.img')
    parser.add_argument('--campus', type=Path, default=ROOT/'data/projects/upload-fbdc0f2cf044/source.img')
    parser.add_argument('--compressed', type=Path, default=ROOT/'data/projects/upload-f4fd82f6605e/source.img')
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/sheet-photos.json')
    parser.add_argument('--workdir', type=Path)
    args = parser.parse_args()
    folder = args.workdir or Path(tempfile.mkdtemp(prefix='dirmap-sheet-photos-'))
    folder.mkdir(parents=True, exist_ok=True)
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'scope': __doc__, 'implementationSignature': implementation_signature(), 'cases': []}
    cases = [
        ('dashahe', args.dashahe, '21800411acc7cc83220de66d1c31b7b33b9ac20d1bcff0da699ea9c45dc8d4b8', 62),
        ('campus', args.campus, '67db55e8b7249ae947a8e253aded172e343207b931432f622d407ceb74402bd8', 47),
        ('campus-compressed', args.compressed, '068f178d909ed25fe54e051b107dd785a58642053ebcdda5ebbc6c509b07673e', 47),
    ]
    for name, path, digest, last in cases:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, path
        image, image_info = read_map_image(path)
        started = time.perf_counter()
        result = analyze(image, labeler=label_controls)
        elapsed = time.perf_counter()-started
        photos, controls = result['sheetPhotos'], result['controls']
        expected = [str(i) for i in range(31, last+1)]
        assert sorted(p['label'] or '' for p in photos) == expected, (name, photos)
        assert sorted(c['label'] for c in controls if c.get('role') == 'control') == expected, (name, controls)
        assert all(p.get('controlId') for p in photos), (name, photos)
        scale = 1400/result['width'] if name == 'dashahe' else 1000/result['height']
        truth = photo_centre if name == 'dashahe' else campus_center
        errors = [float(np.linalg.norm((np.array(p['pixelBox'][:2])+p['pixelBox'][2:])/2*scale-truth(p['label']))) for p in photos]
        assert max(errors) < (16 if name == 'dashahe' else 25), (name, errors)
        target = folder/(name+'.jpg'); preview(image, photos, target)
        report['cases'].append({'sheet': name, 'sourceSha256': digest, 'photoCount': len(photos),
            'expectedPhotoCount': len(expected), 'labels': expected, 'allLinked': True,
            'maxPhotoCentreErrorAtPreviewPx': round(max(errors), 2), 'previewScale': scale,
            'controlCount': len(controls), 'photoPixelBoxes': photos, 'image': image_info,
            'analysisElapsedSec': round(elapsed, 3), 'preview': str(target)})
        print(name, len(photos), 'labelled and linked photos;', round(elapsed, 2), 's image analysis', flush=True)
    report['allChecksPassed'] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(args.output, flush=True)


if __name__ == '__main__':
    main()
