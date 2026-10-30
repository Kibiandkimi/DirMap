#!/usr/bin/env python3
"""Append KML recordings to the shared terrain, owned by an existing account.

Run on the application server (or against a local data directory). Sources
stay unchanged; only normalized coordinate/height observations are stored.
Repeated exports are deduplicated, including different names and timestamps.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path, help='KML files or directories containing KML files')
    parser.add_argument('--owner', required=True, help='existing account username; never transfers another contributor\'s track')
    parser.add_argument('--data-dir', type=Path, help='application data directory; defaults to DIRMAP_DATA_DIR or ./data')
    parser.add_argument('--output', type=Path, help='optional JSON report with model counts and import receipts')
    args = parser.parse_args()
    from dirmap import accounts, elevation, store
    from dirmap.memory import configure, release
    configure()
    if args.data_dir:
        store.DATA = args.data_dir.resolve()
    database = store.DATA/'accounts.sqlite3'
    if not database.is_file():
        raise SystemExit('The target data directory has no accounts. Register the owning account first.')
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute('SELECT id, username FROM users WHERE username_key=?', (accounts.username_key(args.owner),)).fetchone()
    if row is None:
        raise SystemExit(f'No existing account named {args.owner!r}; no data was imported.')
    paths = []
    for path in args.inputs:
        paths.extend(sorted(p for p in path.iterdir() if p.suffix.lower() == '.kml')) if path.is_dir() else paths.append(path)
    paths = list(dict.fromkeys(p.resolve() for p in paths))
    if not paths:
        raise SystemExit('No KML files found.')
    receipts = []
    batch, size = [], 0
    def save():
        if batch:
            receipt = elevation.add_tracks(batch, dict(row))
            receipts.append(receipt)
            print(json.dumps({'added': len(receipt['added']), 'duplicates': len(receipt['duplicates']), 'errors': receipt['errors']}, ensure_ascii=False), flush=True)
            batch.clear()
    for path in paths:
        if path.stat().st_size > elevation.MAX_FILE_BYTES:
            raise SystemExit(f'{path.name}: exceeds the per-file size limit')
        data = path.read_bytes()
        if len(batch) >= 32 or size+len(data) > elevation.MAX_BATCH_BYTES:
            save(); size = 0
        batch.append((path.name, data)); size += len(data)
    save()
    result = elevation.rebuild(elevation.catalogue()['version'], lambda p, message: print(f'{p}% {message}', flush=True))
    report = {'owner': dict(row), 'receipts': receipts, 'trackCount': len(result['tracks']), 'pointCount': result['pointCount'],
              'modelVersion': result['modelVersion'], 'regions': [{key: region[key] for key in
                ('id', 'name', 'pointCount', 'observationCells', 'supportedCells', 'coverageFraction', 'spacingM',
                 'minHeightM', 'maxHeightM', 'fingerprint', 'alignment')} for region in result['regions']]}
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    release()


if __name__ == '__main__':
    main()
