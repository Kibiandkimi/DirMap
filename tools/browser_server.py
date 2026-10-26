#!/usr/bin/env python3
"""Browser-test server with disposable copies of the actual project data."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PREFIX = 'dirmap-browser-data-'


def remove_stale_copies(now=None):
    """A killed run cannot clean up; its copy would stay in a RAM-backed /tmp."""
    now = time.time() if now is None else now
    for directory in Path(tempfile.gettempdir()).glob(PREFIX+'*'):
        try:
            os.kill(int((directory/'owner.pid').read_text()), 0)
            continue
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        except (OSError, ValueError):
            # No owner yet: another run may still be creating it.
            if now-directory.stat().st_mtime < 3600:
                continue
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == '__main__':
    import uvicorn
    from dirmap import store
    remove_stale_copies()
    with tempfile.TemporaryDirectory(prefix=PREFIX) as directory:
        (Path(directory)/'owner.pid').write_text(str(os.getpid()))
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
