#!/usr/bin/env python3
"""Extract public Debian/Ubuntu Tesseract packages into /tmp; no apt install."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def public_version(package):
    result = subprocess.run(['apt-cache', 'madison', package], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        parts = line.split('|')
        if len(parts) >= 3 and 'esm.ubuntu.com' not in parts[2]:
            return parts[1].strip()
    return None


def main():
    root = Path(os.environ.get('DIRMAP_OCR_DIR', '/tmp/dirmap-ocr'))
    if shutil.which('tesseract') or (root/'usr/bin/tesseract').exists():
        print('Tesseract is already available')
        return
    if not all(shutil.which(program) for program in ('apt-get', 'apt-cache', 'dpkg-deb')):
        raise RuntimeError('OCR setup needs apt/dpkg; install Tesseract separately on this platform')
    leptonica = next((name for name in ('libleptonica6', 'liblept5') if public_version(name)), None)
    if not leptonica:
        raise RuntimeError('No public Leptonica package is available')
    packages = ['tesseract-ocr', 'tesseract-ocr-eng', 'libtesseract5', leptonica]
    chosen = {package: public_version(package) for package in packages}
    if not all(chosen.values()):
        raise RuntimeError('Public OCR packages are missing; no system packages were changed')
    download = root/'packages'; download.mkdir(parents=True, exist_ok=True)
    subprocess.run(['apt-get', 'download', *(f'{p}={v}' for p, v in chosen.items())], cwd=download, check=True)
    for package in download.glob('*.deb'):
        subprocess.run(['dpkg-deb', '--extract', str(package), str(root)], check=True)
    env = {**os.environ, 'LD_LIBRARY_PATH': str(root/'usr/lib/x86_64-linux-gnu')+':'+os.environ.get('LD_LIBRARY_PATH', ''),
           'TESSDATA_PREFIX': str(root/'usr/share/tesseract-ocr/5/tessdata')}
    subprocess.run([str(root/'usr/bin/tesseract'), '--version'], env=env, check=True)
    print('OCR packages extracted; system installation unchanged')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
