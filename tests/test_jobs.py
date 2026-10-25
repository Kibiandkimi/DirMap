"""Real worker ownership and crash recovery, including the commit/ack gap."""
import copy
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import pytest

from dirmap import service, store
from dirmap.geometry import Frame, Registration, affine_points
from dirmap.jobs import JobQueue
from dirmap.protocol import TERRAIN_SCHEMA


def wait_job(queue, job_id, timeout=15):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        job = queue.get(job_id)
        if job['state'] in ('succeeded', 'failed'):
            return job
        time.sleep(.01)
    pytest.fail(f'Worker did not finish {queue.get(job_id)}')


def test_persistent_inputs_results_and_retries_survive_new_queue_instances(tmp_path):
    calls = []
    def action(job, progress):
        calls.append(job['payload'])
        progress(70, 'working')
        return {'answer': job['payload']['people']}
    first = JobQueue(tmp_path, action)
    job = first.submit('plan', 'map', {'people': 4}, {'version': 1}, 'client-req-1')
    # The first process never even started a worker.
    second = JobQueue(tmp_path, action, .01)
    second.start()
    try:
        result = wait_job(second, job['id'])
        assert result['state'] == 'succeeded' and result['result'] == {'answer': 4}
        # A lost HTTP response can be retried after completion. The changed
        # current map version must not cause a duplicate execution.
        assert second.submit('plan', 'map', {'people': 4}, {'version': 2}, 'client-req-1') == result
        with pytest.raises(ValueError, match='同一提交'):
            second.submit('plan', 'map', {'people': 5}, {'version': 2}, 'client-req-1')
        assert calls == [{'people': 4}]
        assert first.get(job['id']) == result
        assert first.recent('map')[0]['state'] == 'succeeded'
    finally:
        second.close()


def test_second_worker_cannot_steal_a_live_slow_job_or_duplicate_a_submission(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    def action(job, progress):
        calls.append(job['id']); started.set()
        assert release.wait(10)
        return {'done': True}
    one, two = (JobQueue(tmp_path, action, .01) for _ in range(2))
    job = one.submit('plan', 'map', {}, {'version': 1}, 'request-one')
    one.start()
    try:
        assert started.wait(5)
        with one.database() as db:
            db.execute('UPDATE jobs SET started_at=0 WHERE id=?', (job['id'],))
        two.start()
        same = two.submit('plan', 'map', {}, {'version': 1}, 'request-two')
        assert same['id'] == job['id']
        # Observe many recovery polls with a deliberately ancient timestamp.
        time.sleep(.15)
        assert len(calls) == 1
        assert two.get(job['id'])['attempts'] == 1
        assert not two.get(job['id'])['recovered']
        release.set()
        result = wait_job(two, job['id'])
        assert result['state'] == 'succeeded'
        assert two.by_request('request-two')['id'] == job['id']
        assert two.submit('plan', 'map', {}, {'version': 99}, 'request-two')['id'] == job['id']
    finally:
        release.set(); one.close(); two.close()


def terrain_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path/'data')
    image = np.full((41, 41, 3), 245, np.uint8)
    image[20:30, 20:30] = 180
    source = tmp_path/'source.png'; cv2.imwrite(str(source), image)
    asset = {'id': 'crash-map', 'path': str(source), 'sha256': store.file_hash(source)}
    monkeypatch.setattr(store, 'catalog', lambda: [asset])
    reg = Registration([[1, 0, 0], [0, -1, 40]], [[0, 0], [40, 0], [40, 40], [0, 40]],
                       affine_points([[1, 0, 0], [0, -1, 40]], [[0, 0], [40, 0], [40, 40], [0, 40]]))
    state = {'assetId': asset['id'], 'sourceHash': asset['sha256'], 'version': 1, 'width': 41, 'height': 41,
             'controls': [{'id': 'c1', 'label': '教师标记', 'pixel': [10, 10], 'state': 'accepted'}],
             'edits': [], 'registration': reg.as_dict(), 'frame': Frame().as_dict(), 'quality': {},
             'terrainSchema': TERRAIN_SCHEMA, 'statistics': {}, 'reviewed': True, 'graphHash': None}
    service.project_controls(state)
    directory = store.asset_dir(asset['id'])
    np.savez_compressed(directory/'terrain.npz', costs=np.ones((41, 41), np.float32))
    cv2.imwrite(str(directory/'overlay.png'), np.zeros((10, 10, 4), np.uint8))
    store.save_state(asset['id'], state)
    store.atomic_json(store.DATA/'test-asset.json', asset)
    return asset, state


CRASH_WORKER = r'''
import json, sys, threading
from dirmap import store, service
from dirmap.jobs import JobQueue
asset = store.read_json(store.DATA/'test-asset.json')
store.catalog = lambda: [asset]
save = store.save_state
def checkpoint():
    print('checkpoint', flush=True)
    threading.Event().wait()
def interrupted(asset_id, value, operation_id=None, result=None):
    if operation_id and sys.argv[1] != 'after': checkpoint()
    save(asset_id, value, operation_id, result)
    if operation_id: checkpoint()
store.save_state = interrupted
queue = JobQueue(store.DATA, service.execute_job, .01)
job = queue.submit('terrain', asset['id'], {'version': 1}, service.job_inputs('terrain', asset['id']))
print(job['id'], flush=True)
queue.start()
threading.Event().wait()
'''


def read_process_line(process, timeout=20):
    with selectors.DefaultSelector() as ready:
        ready.register(process.stdout, selectors.EVENT_READ)
        assert ready.select(timeout), 'Test worker did not reach its checkpoint'
        line = process.stdout.readline()
        assert line, process.stderr.read().decode()
        return line.decode().strip()


@pytest.mark.parametrize('phase', ['before', 'after', 'edited'])
def test_killed_terrain_worker_recovers_same_handle_without_partial_publish_or_lost_edits(tmp_path, monkeypatch, phase):
    asset, original = terrain_fixture(tmp_path, monkeypatch)
    original_path = store.artifact_path(original, 'terrain.npz')
    original_bytes = original_path.read_bytes()
    env = dict(os.environ, DIRMAP_DATA_DIR=str(store.DATA))
    process = subprocess.Popen([sys.executable, '-B', '-c', CRASH_WORKER, phase], env=env, cwd=store.ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        job_id = read_process_line(process)
        assert read_process_line(process) == 'checkpoint'
        state = store.get_state(asset['id'])
        if phase == 'after':
            assert state['version'] == 2
            assert store.read_json(store.DATA/'jobs'/'results'/f'{job_id}.json')['version'] == 2
        else:
            # All new files have been written, but none is visible through
            # the authoritative state until its single manifest is switched.
            assert state['version'] == 1 and 'artifacts' not in state
            assert store.artifact_path(state, 'terrain.npz').read_bytes() == original_bytes
        process.kill(); process.communicate(timeout=10)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill(); process.communicate(timeout=10)
    if phase == 'edited':
        changed = copy.deepcopy(state)
        changed['version'] = 2
        changed['controls'][0]['label'] = '重启前的教师修正'
        store.save_state(asset['id'], changed)
    if phase == 'after':
        def must_not_repeat(*args, **kwargs):
            pytest.fail('Committed image work was repeated instead of recovered')
        monkeypatch.setattr(service, 'refresh_terrain', must_not_repeat)
    queue = JobQueue(store.DATA, service.execute_job, .01)
    queue.start()
    try:
        result = wait_job(queue, job_id)
        assert result['id'] == job_id and result['recovered'] and result['attempts'] == 2
        current = store.get_state(asset['id'])
        if phase == 'edited':
            assert result['state'] == 'failed' and '更新' in result['message']
            assert current == changed
        else:
            assert result['state'] == 'succeeded', result
            assert current['version'] == result['result']['version'] == 2
            assert current['controls'] == original['controls']
            assert current['registration'] == original['registration']
            with np.load(store.artifact_path(current, 'terrain.npz')) as terrain:
                assert terrain['costs'][25, 25] == 0
            for name in ('terrain.npz', 'terrain.png', 'terrain-warp.png', 'blocked.png', 'overlay.png'):
                assert store.artifact_path(current, name).exists()
        assert original_path.read_bytes() == original_bytes
        assert store.file_hash(asset['path']) == asset['sha256']
    finally:
        queue.close(); service.GRAPH_CACHE.clear()


EDIT_WORKER = r'''
import json, sys, time
from fastapi.testclient import TestClient
from dirmap import store
from dirmap.app import app
read = store.get_state
def slow_read(asset_id):
    result = read(asset_id)
    time.sleep(.15)  # force a read/modify overlap unless the OS lock holds
    return result
store.get_state = slow_read
with TestClient(app) as client:
    print('ready', flush=True)
    sys.stdin.readline()
    response = client.patch('/api/maps/crash-map', json={'version': 1, 'edits': [
        {'kind': 'close', 'pixels': [[int(sys.argv[1]), 12]], 'radiusPx': 1}]})
    print(response.status_code, flush=True)
'''


def test_independent_api_processes_do_not_overwrite_each_others_edits(tmp_path, monkeypatch):
    asset, original = terrain_fixture(tmp_path, monkeypatch)
    env = dict(os.environ, DIRMAP_DATA_DIR=str(store.DATA))
    workers = [subprocess.Popen([sys.executable, '-B', '-c', EDIT_WORKER, str(x)], env=env, cwd=store.ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0) for x in (8, 30)]
    try:
        for process in workers:
            assert read_process_line(process) == 'ready'
        for process in workers:
            process.stdin.write(b'go\n'); process.stdin.flush()
        statuses = [int(read_process_line(process)) for process in workers]
        for process in workers:
            process.communicate(timeout=10)
            assert process.returncode == 0
        assert sorted(statuses) == [200, 409]
        state = store.get_state(asset['id'])
        assert state['version'] == 2 and state['controls'] == original['controls']
        assert state['edits'][0]['pixels'] == [[(8, 30)[statuses.index(200)], 12]]
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill(); process.communicate(timeout=10)
