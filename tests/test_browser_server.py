"""Disposable browser-test data must not accumulate after a killed run."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

spec = importlib.util.spec_from_file_location('browser_server', Path(__file__).resolve().parents[1]/'tools/browser_server.py')
browser_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser_server)


def test_only_copies_of_exited_or_abandoned_runs_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path))
    exited = subprocess.Popen([sys.executable, '-c', 'pass'])
    exited.wait()
    copies = {name: tmp_path/(browser_server.PREFIX+name) for name in ('live', 'exited', 'creating', 'abandoned')}
    for directory in copies.values():
        (directory/'data').mkdir(parents=True)
    (copies['live']/'owner.pid').write_text(str(os.getpid()))
    (copies['exited']/'owner.pid').write_text(str(exited.pid))
    old = time.time()-7200
    os.utime(copies['abandoned'], (old, old))
    other = tmp_path/'unrelated'
    other.mkdir()
    browser_server.remove_stale_copies()
    assert {name for name, directory in copies.items() if directory.exists()} == {'live', 'creating'}
    assert other.exists()
