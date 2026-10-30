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


def fixture_download(query, progress=lambda percent, message: None, rounds=2):
    """Stands in for public Overpass: one building and a path at the range centre."""
    import re
    south, west, north, east = map(float, re.search(r'\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\)', query).groups())
    lat, lng = (south+north)/2, (west+east)/2
    square = [{'lat': lat+dy, 'lon': lng+dx} for dx, dy in ((0, 0), (.0004, 0), (.0004, .0003), (0, .0003), (0, 0))]
    path = [{'lat': south+(north-south)*.2, 'lon': lng}, {'lat': north-(north-south)*.2, 'lon': lng}]
    progress(20, '下载 OpenStreetMap 参照（测试数据）')
    return {'elements': [{'type': 'way', 'id': 1, 'tags': {'building': 'yes', 'name': '测试楼'}, 'geometry': square},
                         {'type': 'way', 'id': 2, 'tags': {'highway': 'footway'}, 'geometry': path}]}, 'fixture://overpass'


if __name__ == '__main__':
    import uvicorn
    from dirmap import store
    remove_stale_copies()
    with tempfile.TemporaryDirectory(prefix=PREFIX) as directory:
        (Path(directory)/'owner.pid').write_text(str(os.getpid()))
        data = Path(directory)/'data'
        source = store.DATA
        def ignore(path, names):
            # Accounts, their workbenches and live groups stay on this machine;
            # every browser run starts with the original workbench unclaimed.
            return [name for name in names if name == '.write.lock'
                    or name.startswith(('.processing-', '.terrain-', '.anchors-', 'accounts.sqlite3', 'groups.sqlite3'))
                    or (Path(path) == source and name in ('jobs', 'workspaces', 'group-photos', 'elevation'))]
        # Freeze the manifest and derived files together. A live teacher can
        # keep working; never replay their queued production jobs in tests.
        with store.LOCK:
            shutil.copytree(source, data, ignore=ignore)
        assert not (data/'jobs').exists()
        os.environ['DIRMAP_DATA_DIR'] = str(data)
        store.DATA = data
        os.environ.pop('DIRMAP_ADMIN_TOKEN', None)
        from dirmap import sites
        sites.download = fixture_download
        uvicorn.run('dirmap.app:app', host='127.0.0.1', port=8768)
