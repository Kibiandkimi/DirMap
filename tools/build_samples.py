#!/usr/bin/env python3
"""Build public starter maps from original images in an empty temporary workbench.

No account's states, landmarks, controls, edits, jobs, or uploads are copied.
The only inputs are program code, original sample images and public references.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'dirmap/public-samples.json')
    args = parser.parse_args()
    from dirmap import store
    from dirmap.samples import write_bundle
    from dirmap.service import process_asset
    from dirmap.memory import configure, release as release_memory
    configure()
    with tempfile.TemporaryDirectory(prefix='dirmap-public-samples-') as temporary:
        store.DATA = Path(temporary)/'data'
        with store.using(store.LEGACY):
            for asset in store.builtin_maps():
                assert store.get_state(asset['id']) is None
                state = process_asset(asset['id'], lambda percent, message: print(f'{asset["id"]}: {percent}% {message}', flush=True),
                                      expected_version=0, preserve_edits=False)
                assert state.get('registration'), f'{asset["id"]}: automatic registration did not complete'
                del state
                release_memory()
            report = write_bundle(store.DATA, args.output)
    print(json.dumps({'output': str(args.output), **report}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
