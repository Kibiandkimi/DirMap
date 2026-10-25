#!/usr/bin/env python3
"""Browser-test server with disposable copies of the actual project data."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    import uvicorn
    from dirmap import store
    with tempfile.TemporaryDirectory(prefix='dirmap-browser-data-') as directory:
        data = Path(directory)/'data'
        source = store.DATA
        def ignore(path, names):
            return [name for name in names if name == '.write.lock'
                    or name.startswith(('.processing-', '.terrain-', '.anchors-'))
                    or (Path(path) == source and name == 'jobs')]
        # Freeze the manifest and derived files together. A live teacher can
        # keep working; never replay their queued production jobs in tests.
        with store.LOCK:
            shutil.copytree(source, data, ignore=ignore)
        assert not (data/'jobs').exists()
        os.environ['DIRMAP_DATA_DIR'] = str(data)
        store.DATA = data
        os.environ.pop('DIRMAP_ADMIN_TOKEN', None)
        uvicorn.run('dirmap.app:app', host='127.0.0.1', port=8768)
