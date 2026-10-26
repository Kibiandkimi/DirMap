"""Long-running image/graph work, versioned results, and course planning."""

import copy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
import tempfile

import cv2
import numpy as np

from .geometry import Frame, Registration
from .access import AccessResolver
from .graph import build_graph, expand_route, plan_matrix
from .optimizer import solve, evaluate_routes
from .route_search import validate_sequences
from .route_scores import improves
from .schedule import apply_schedule
from .team_planning import recommend_teams
from .ocr import label_controls
from .reference import load_reference
from .registration import auto_register, extend_transfer_boundary, manual_register, recover_boundary_buildings, register_to_map, warp_overlay
from . import store, graph_cache, comparisons, layers
from .protocol import ROUTING_SCHEMA
from .images import read_map_image
from .recognition import reconcile_controls
from .vision import TERRAIN_SCHEMA, analyze, colour_masks, read_image, save_analysis

GRAPH_CACHE = {}
GRAPH_LOCK = threading.RLock()
NAVIGATION_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def implementation_signature():
    paths = sorted(Path(__file__).parent.glob('*.py'))+sorted(Path(__file__).parent.glob('*.cpp'))
    paths.append(Path(__file__).with_name('schemas.json'))
    return hashlib.sha256(b''.join(path.read_bytes() for path in paths)).hexdigest()


def job_inputs(kind, asset_id, payload=None):
    """Freeze the inputs which must not change while a job waits/restarts."""
    with store.LOCK:
        asset = store.get_asset(asset_id)
        state = store.get_state(asset_id)
        snapshot = {'sourceHash': asset['sha256'], 'version': (state or {}).get('version', 0),
                    'implementation': implementation_signature()}
        if state:
            snapshot['stateHash'] = hashlib.sha256(json.dumps({key: state.get(key) for key in
                ('registration', 'frame', 'controls', 'edits', 'surfaces', 'portals', 'terrainSchema', 'reviewed')}, sort_keys=True).encode()).hexdigest()
            terrain = store.artifact_path(state, 'terrain.npz')
            if terrain.exists():
                snapshot['terrainHash'] = store.file_hash(terrain)
        if kind == 'process':
            bases = {}
            for candidate in store.catalog():
                base = store.get_state(candidate['id'])
                if candidate['id'] != asset_id and base and base.get('registration'):
                    bases[candidate['id']] = [base['sourceHash'], registration_stamp(base)]
            snapshot['bases'] = bases
            path = reference_path()
            snapshot['referenceHash'] = store.file_hash(path) if path.exists() else None
        elif state and (state.get('quality') or {}).get('baseAssetId'):
            base = store.get_state(state['quality']['baseAssetId'])
            snapshot['baseRegistration'] = registration_stamp(base) if base else None
        if kind == 'choose_teams':
            if not payload:
                raise ValueError('缺少分队比较记录')
            comparison = comparisons.load(asset_id, payload['comparisonId'], payload['comparisonToken'])
            snapshot['comparisonToken'] = comparison['token']
            if payload.get('confirmPlanId'):
                path = comparisons.plan_path(asset_id, payload['confirmPlanId'])
                if not path.exists():
                    raise ValueError('待核验的路线不存在，请重新选择方案')
                snapshot['selectedPlanHash'] = store.file_hash(path)
        if kind in ('refine_plan', 'confirm_plan'):
            if not payload:
                raise ValueError('缺少原路线标识')
            source = saved_plan(asset_id, payload['planId'])
            snapshot.update(sourcePlanHash=source['hash'], continuationToken=source['saved']['token'])
        return snapshot


def execute_job(job, progress):
    asset_id, operation_id = job['assetId'], job['id']
    committed = store.operation_result(asset_id, operation_id)
    if committed is not None:
        progress(100, '已恢复原任务提交的结果')
        return committed
    if job['inputs'] != job_inputs(job['kind'], asset_id, job['payload']):
        raise ValueError('等待或恢复期间地图、参照或程序已更新；已保留最新修改，请重新提交任务')
    if job['attempts'] > 3:
        raise ValueError('任务已多次随服务中断，已保留已有结果；请检查服务后重新提交')
    try:
        result = _execute_operation(job, progress)
    except Exception:
        # Optional graph warming can fail after the manifest has committed.
        # Its receipt is authoritative; never misreport a saved map as lost.
        result = store.operation_result(asset_id, operation_id)
        if result is None:
            raise
    try:
        store.prune_artifacts(asset_id)
    except OSError:
        pass  # cleanup cannot undo an otherwise valid committed result
    return result


def _execute_operation(job, progress):
    asset_id, operation_id = job['assetId'], job['id']
    payload = job['payload']
    if job['kind'] == 'process':
        result = process_asset(asset_id, progress, expected_version=job['inputs']['version'], operation_id=operation_id,
                               preserve_registration=payload.get('preserveRegistration', False))
    elif job['kind'] == 'terrain':
        result = refresh_terrain(asset_id, payload['version'], progress, operation_id=operation_id)
    elif job['kind'] == 'anchors':
        progress(10, '校准同名点与生成地图叠层')
        result = apply_anchors(asset_id, payload['anchors'], payload['version'], operation_id=operation_id)
    elif job['kind'] == 'plan':
        result = plan_course(payload, progress, operation_id=operation_id)
    elif job['kind'] == 'auto_teams':
        result = plan_course(payload, progress, operation_id=operation_id, auto_teams=True)
    elif job['kind'] == 'choose_teams':
        result = choose_team_count(payload, progress, operation_id=operation_id)
    elif job['kind'] in ('refine_plan', 'confirm_plan'):
        result = continue_plan(payload, progress, operation_id=operation_id, confirm=job['kind'] == 'confirm_plan')
    else:
        raise ValueError('不支持的任务类型')
    return result


def reference_path():
    path = store.DATA/'reference'/'yuehai-osm.json'
    if not path.exists():
        path = store.ROOT/'data'/'reference'/'yuehai-osm.json'
    return path


def reference():
    path = reference_path()
    if not path.exists():
        raise ValueError('缺少校园地理参照，请先运行参考下载命令')
    return load_reference(path)


def registration_stamp(state):
    return hashlib.sha256(json.dumps({'registration': state.get('registration'), 'frame': state.get('frame')},
                                     sort_keys=True).encode()).hexdigest()


def registration_stale(state):
    quality = state.get('quality') or {}
    base_id = quality.get('baseAssetId')
    if not base_id:
        return False
    base = store.get_state(base_id)
    return (not base or not base.get('registration') or base['sourceHash'] != quality.get('baseSourceHash')
            or registration_stamp(base) != quality.get('baseRegistrationHash'))


def process_asset(asset_id, progress=lambda p, m: None, expected_version=None, operation_id=None, preserve_edits=True,
                  preserve_registration=False):
    with store.LOCK:
        previous = store.get_state(asset_id)
        if expected_version is not None and (previous or {}).get('version', 0) != expected_version:
            raise ValueError('地图已更新，请刷新后重试')
    with tempfile.TemporaryDirectory(prefix='.processing-', dir=store.asset_dir(asset_id)) as directory:
        return _process_asset(asset_id, progress, Path(directory), previous, operation_id, preserve_edits, preserve_registration)


def refresh_ocr(asset_id):
    state = store.get_state(asset_id)
    if not state:
        raise ValueError('请先识别地图')
    asset = store.get_asset(asset_id)
    if state['sourceHash'] != asset['sha256']:
        raise ValueError('原图已改变，请重新识别整图')
    before = copy.deepcopy(state)
    masks = colour_masks(read_image(asset['path'], (state['width'], state['height'])))
    state['statistics']['ocr'] = label_controls(masks['purple'], state['controls'], masks['course_text'])
    if state == before:
        return state
    with store.LOCK:
        if store.get_state(asset_id)['version'] != before['version']:
            raise ValueError('识别编号期间地图被修改，已保留最新修改')
        state.update(version=before['version']+1, reviewed=False, graphHash=None, updatedAt=time.time())
        store.save_state(asset_id, state)
    GRAPH_CACHE.pop(asset_id, None)
    return state


def refresh_terrain(asset_id, expected_version=None, progress=lambda p, m: None, operation_id=None):
    """Upgrade source interpretation without replacing teacher edits or fits."""
    state = store.get_state(asset_id)
    if not state or not state.get('registration'):
        raise ValueError('请先完成地图配准')
    if expected_version is not None and state['version'] != expected_version:
        raise ValueError('地图已更新，请刷新后重试')
    asset = store.get_asset(asset_id)
    if state['sourceHash'] != asset['sha256']:
        raise ValueError('原图已改变，请重新识别整图')
    before = state['version']; started = time.perf_counter()
    progress(10, '分离紫色课程标记，恢复被遮挡的水体、建筑与围墙')
    result = analyze(read_image(asset['path'], (state['width'], state['height'])), labeler=label_controls)
    with tempfile.TemporaryDirectory(prefix='.terrain-', dir=store.asset_dir(asset_id)) as temporary:
        directory = Path(temporary)
        save_analysis(result, directory)
        progress(65, '生成更新后的障碍图')
        terrain = cv2.imread(str(directory/'terrain.png'), cv2.IMREAD_UNCHANGED)
        warp_overlay(terrain, Registration.from_dict(state['registration']), directory/'terrain-warp.png', max_side=1200, nearest=True)
        for key in ('forbiddenPixelFraction', 'restoredCourseInkPixels', 'continuedBarrierPixels',
                    'continuedRoofPixels', 'staircaseCandidates', 'stairTreadPixels', 'pavedBoundaryPixels', 'lowerLevelHatchingPixels'):
            state['statistics'][key] = result['statistics'][key]
        state['terrainFeatures'] = result['terrainFeatures']
        state.update(terrainSchema=TERRAIN_SCHEMA, version=before+1, reviewed=False, graphHash=None,
                     updatedAt=time.time(), terrainProcessingElapsedSec=round(time.perf_counter()-started, 3))
        with store.LOCK:
            if store.get_state(asset_id)['version'] != before:
                raise ValueError('识别期间地图被修改，已保留最新修改，请重试')
            store.stage_artifacts(state, directory)
            store.save_state(asset_id, state, operation_id)
    GRAPH_CACHE.pop(asset_id, None)
    progress(90, '已保留点位及教师修正，旧路线已失效')
    get_graph(state)
    return state


def _process_asset(asset_id, progress, directory, previous, operation_id=None, preserve_edits=True, preserve_registration=False):
    started = time.perf_counter()
    asset = store.get_asset(asset_id)
    keep = bool(preserve_edits and previous and previous.get('sourceHash') == asset['sha256'])
    if preserve_registration:
        if not keep or not previous.get('registration'):
            raise ValueError('没有与当前原图匹配的校准，请先自动识别与配准')
        if registration_stale(previous):
            raise ValueError('基准图配准已变化，请先重新配准当前图幅')
    size = (previous['width'], previous['height']) if keep else None
    image, image_info = read_map_image(asset['path'], size)
    progress(10, '提取控制圈、建筑、水体与通行候选')
    result = analyze(image, labeler=label_controls)
    save_analysis(result, directory)
    if image_info['resized'] or image_info['exifOrientation'] != 1:
        cv2.imwrite(str(directory/'working.jpg'), image, [cv2.IMWRITE_JPEG_QUALITY, 94])
    progress(35, '匹配校园参照与其他原图')
    reg, quality = None, None
    failures = []
    keep_registration = (keep and previous.get('registration') and
        (preserve_registration or (previous.get('quality') or {}).get('method') == 'reviewed_landmarks_mesh'
         or (previous.get('quality') or {}).get('method', '').startswith('manual')))
    if keep_registration:
        reg, quality = Registration.from_dict(previous['registration']), copy.deepcopy(previous['quality'])
    else:
        try:
            geographic_reference = reference()
            reg, quality = auto_register(result['features'], geographic_reference, result['width'], result['height'])
            if result['controls'] and not reg.forward([c['pixel'] for c in result['controls']])[1].all():
                reg, quality = recover_boundary_buildings(image, reg, quality, geographic_reference)
        except ValueError as exc:
            failures.append(str(exc))
    # Prefer direct geographic evidence. A low-resolution copy may inherit
    # more reliable detail from a higher-resolution source of the same map.
    bases = [] if keep_registration else [a for a in store.catalog() if a['id'] != asset_id]
    for base in bases:
        state = store.get_state(base['id'])
        if not state or not state.get('registration') or state['quality'].get('baseAssetId'):
            continue
        if reg is not None and state['width']*state['height'] < image.shape[0]*image.shape[1]*1.5:
            continue
        try:
            candidate, info = register_to_map(image, read_image(base['path'], (state['width'], state['height'])), Registration.from_dict(state['registration']),
                                               base['id'], state['quality'])
            candidate, info = extend_transfer_boundary(candidate, info, quality)
            pixels = [c['pixel'] for c in result['controls']]
            old_coverage = int(reg.forward(pixels)[1].sum()) if reg else 0
            new_coverage = int(candidate.forward(pixels)[1].sum())
            if info['p95ErrorM'] < 6 and new_coverage >= old_coverage:
                reg, quality = candidate, info
                quality.update(baseVersion=state['version'], baseSourceHash=state['sourceHash'],
                               baseRegistrationHash=registration_stamp(state))
                break
        except ValueError as exc:
            failures.append(str(exc))
    state = {'assetId': asset_id, 'name': asset['name'], 'filename': asset['filename'], 'sourceHash': asset['sha256'],
             'width': result['width'], 'height': result['height'], 'controls': result['controls'], 'sourceImage': image_info,
             'statistics': result['statistics'], 'warnings': result['warnings'],
             'terrainFeatures': result['terrainFeatures'], 'edits': [], 'frame': Frame().as_dict(),
             'version': (previous or {}).get('version', 0)+1, 'updatedAt': time.time(),
             'reviewed': False, 'quality': quality, 'registration': reg.as_dict() if reg else None,
             'graphHash': None, 'failures': failures, 'terrainSchema': result['terrainSchema']}
    if keep:
        state['controls'], state['recognitionReview'] = reconcile_controls(result['controls'], previous)
        state['edits'] = copy.deepcopy(previous.get('edits', []))
        state['surfaces'] = copy.deepcopy(previous.get('surfaces', []))
        state['portals'] = copy.deepcopy(previous.get('portals', []))
        state['recognitionExclusions'] = copy.deepcopy(previous.get('recognitionExclusions', []))
    if keep_registration:
        # Keep the serialized registration stamp too, so unchanged transforms
        # do not invalidate dependent sheets through int/float round-tripping.
        state['registration'] = copy.deepcopy(previous['registration'])
        state['frame'] = copy.deepcopy(previous['frame'])
    if reg is not None:
        progress(60, '生成统一坐标叠层与目标点')
        state['extent'] = warp_overlay(image, reg, directory/'overlay.png')
        terrain = cv2.imread(str(directory/'terrain.png'), cv2.IMREAD_UNCHANGED)
        warp_overlay(terrain, reg, directory/'terrain-warp.png', max_side=1200, nearest=True)
        project_controls(state)
    else:
        state['warnings'].append('自动配准证据不足，请录入同名点后继续。')
    progress(90, '保存可复核结果')
    state['processingElapsedSec'] = round(time.perf_counter()-started, 3)
    # Generating a new source interpretation invalidates old route versions.
    GRAPH_CACHE.pop(asset_id, None)
    with store.LOCK:
        current = store.get_state(asset_id)
        if (current or {}).get('version', 0) != (previous or {}).get('version', 0):
            raise ValueError('处理期间地图被修改，已保留最新修改；请重新处理')
        store.stage_artifacts(state, directory, replace_all=True)
        store.save_state(asset_id, state, operation_id)
    if reg is not None:
        progress(95, '准备可复用的通行图')
        try:
            get_graph(state)
        except ValueError as exc:
            state['warnings'].append(f'通行图待核验：{exc}')
    state['processingElapsedSec'] = round(time.perf_counter()-started, 3)
    with store.LOCK:
        if store.get_state(asset_id)['version'] == state['version']:
            store.save_state(asset_id, state, operation_id)
    return state


def project_controls(state):
    if not state.get('registration'):
        return
    reg = Registration.from_dict(state['registration'])
    if not state['controls']:
        return
    world, inside = reg.forward([c['pixel'] for c in state['controls']])
    frame = Frame()
    for control, point, valid in zip(state['controls'], world, inside):
        control['lnglat'] = frame.to_lnglat(point).tolist() if valid else None
        control['calibrated'] = bool(valid)


def get_graph(state):
    # Same lock order as publication; keep terrain and state from the same
    # atomic processing commit while constructing a new cached graph.
    with store.LOCK, GRAPH_LOCK:
        if state.get('terrainSchema') != TERRAIN_SCHEMA:
            raise ValueError('通行识别规则已更新，请先更新通行识别；旧结果不能用于规划')
        if registration_stale(state):
            raise ValueError('基准图配准已变化，请重新配准当前图幅')
        current = store.get_state(state['assetId'])
        if current and current['version'] != state['version']:
            raise ValueError('地图已更新，请刷新后重试')
        return _get_graph(state)


def _get_graph(state):
    asset_id = state['assetId']
    cached = GRAPH_CACHE.get(asset_id)
    if cached and cached[0] == state['version']:
        return cached[1]
    if not state.get('registration'):
        raise ValueError('请先完成地图配准')
    terrain_path = store.artifact_path(state, 'terrain.npz')
    cache_key = graph_cache.key(state, terrain_path)
    with np.load(terrain_path) as artifact:
        costs = artifact['costs']
        semantics = {kind: artifact[kind] for kind in ('building', 'water', 'point_ink', 'vegetation', 'restricted', 'lower_level') if kind in artifact}
    reg = Registration.from_dict(state['registration'])
    graph = graph_cache.read(cache_key)
    if graph is None:
        graph = layers.build(costs, reg, state['sourceHash'], state['edits'], state.get('surfaces', []),
                             state.get('portals', []), semantics.get('restricted'), step=4, lower_level=semantics.get('lower_level'))
        try:
            graph_cache.write(cache_key, graph)
        except OSError:
            pass  # a full/unwritable scratch volume must not lose a valid route
    graph.access_resolver = AccessResolver(graph, reg, semantics) if semantics else None
    # At most one native-pixel graph cached: bounded memory for three maps.
    GRAPH_CACHE.clear()
    GRAPH_CACHE[asset_id] = (state['version'], graph)
    return graph


def navigate_course(release, request):
    """Rejoin the published team's remaining order without storing location.

    A student's visit history must be a prefix of that team's course. Rejoin
    paths use exactly the same checked graph and confirmed approaches as the
    release. No nearest-road move is allowed through a wall/forbidden area.
    """
    if request['accuracyM'] > 20:
        raise ValueError('定位精度不足，请移至开阔处后重试')
    age = time.time()*1000-request['timestampMs']
    if age < -5000 or age > 20000:
        raise ValueError('定位已过期，请等待新的定位样本')
    published = release['plan']
    route = next((r for r in published['routes'] if r['teamId'] == request['teamId']), None)
    if route is None:
        raise ValueError('队伍不属于这份课程')
    completed = request['completedControlIds']
    if completed != route['controlIds'][:len(completed)]:
        raise ValueError('已完成点必须按本队课程顺序记录，不能跳过必到点')
    with NAVIGATION_LOCK:
        if time.time()*1000-request['timestampMs'] > 20000:
            raise ValueError('定位已过期，请等待新的定位样本')
        state = store.get_state(release['assetId'])
        if not state or state['version'] != release['version'] or not state['reviewed']:
            raise ValueError('课程已更新，请使用教师提供的新课程链接')
        graph = get_graph(state)
        if graph.content_hash != published['graphHash']:
            raise ValueError('通行图已更新，请使用教师提供的新课程链接')
        reg = Registration.from_dict(state['registration'])
        world = Frame().to_local([request['lnglat']])
        pixel = reg.inverse(world)[0]
        if not np.isfinite(pixel).all():
            raise ValueError('当前位置超出已校准的课程范围')
        remaining = route['controlIds'][len(completed):]
        lookup = {c['id']: c for c in release['controls']}
        controls = [lookup[c] for c in remaining]
        team = next(t for t in published['request']['teams'] if t['id'] == route['teamId'])
        bundle = plan_matrix(graph, controls, pixel.tolist(), published['request']['finishPixel'], reg,
                             start_surface=request.get('surfaceId'), finish_surface=published['request'].get('finishSurfaceId'))
        if bundle['unresolved']:
            return {'status': 'NEEDS_REVIEW', 'unresolved': bundle['unresolved'],
                    'version': state['version'], 'graphHash': graph.content_hash, 'route': None}
        geometry = expand_route(graph, bundle, list(range(len(controls)+2)))
        travel = geometry['travelWeightM']/team['speedMps']
        service_time = sum(c.get('serviceBaseSec', 5)+team['people']*c.get('servicePerPersonSec', 1) for c in controls)
        # Recheck after computation; a closure saved concurrently must not
        # publish one last navigation result using the previous graph.
        current = store.get_state(release['assetId'])
        if not current or current['version'] != state['version'] or not current['reviewed']:
            raise ValueError('计算期间课程已更新，请使用新的课程链接')
        return {'status': 'READY', 'version': state['version'], 'graphHash': graph.content_hash,
                'completedControlIds': completed, 'generatedAt': time.time(),
                'route': {**geometry, 'teamId': route['teamId'], 'people': team['people'],
                          'controlIds': remaining, 'travelSec': travel, 'serviceSec': service_time,
                          'durationSec': travel+service_time, 'departureSec': 0, 'finishSec': travel+service_time},
                'scope': '从当前位置到本队剩余点及终点的合法最短路；保持发布时的点位分配和访问顺序。'}


def saved_plan(asset_id, identifier):
    """Bind a stored plan to its immutable calculation and candidate."""
    with store.LOCK:
        path = comparisons.plan_path(asset_id, identifier)
        plan = store.read_json(path)
        if not plan or plan.get('id') != identifier or plan.get('assetId') != asset_id or not plan.get('routes'):
            raise ValueError('原路线不存在或没有可用方案，请重新生成')
        reference = plan.get('continuation')
        if not reference:
            raise ValueError('这份旧路线没有保留继续计算所需的数据，请重新生成')
        saved = comparisons.load(asset_id, reference['id'], reference['token'])
        request, original = plan['request'], comparisons.source_request(saved)
        item = saved['content']['candidates'].get(str(len(request['teams'])))
        if (not item or request['teams'] != item['teams'] or [r['order'] for r in plan['routes']] != item['orders']
                or plan['version'] != saved['content']['version'] or plan['graphHash'] != saved['content']['graphHash']
                or plan.get('routingSchema') != saved['content']['routingSchema']):
            raise ValueError('原路线与保存的计算记录不一致，请重新生成')
        defaults = {'mandatoryControlIds': [], 'mandatoryOrder': False, 'customRoutes': [],
                    'startSurfaceId': None, 'finishSurfaceId': None}
        for key in ('assetId', 'controlIds', 'startPixel', 'finishPixel', 'mode', 'objective', *defaults):
            if request.get(key, defaults.get(key)) != original.get(key, defaults.get(key)):
                raise ValueError('原路线的课程规则已变化，请重新生成')
        return {'plan': plan, 'saved': saved, 'candidate': item, 'hash': store.file_hash(path)}


def continue_plan(payload, progress=lambda p, m: None, operation_id=None, confirm=False):
    source = saved_plan(payload['assetId'], payload['planId'])
    request = copy.deepcopy(source['plan']['request'])
    if not confirm:
        if request['version'] != payload['version']:
            raise ValueError('地图已更新，请重新生成后继续优化')
        if request['mode'] not in ('all_visit', 'split_coverage', 'mixed_coverage'):
            raise ValueError('固定或自定义点序没有可继续优化的访问顺序')
        request['timeLimitSec'] = payload['timeLimitSec']
    request['version'] = payload['version']
    source.update(confirm=confirm, payload=payload)
    return plan_course(request, progress, operation_id, continuing=source)


def choose_team_count(payload, progress=lambda p, m: None, operation_id=None):
    saved = comparisons.load(payload['assetId'], payload['comparisonId'], payload['comparisonToken'])
    content = saved['content']
    if not content.get('recommendation'):
        raise ValueError('这份路线不是队数比较，请先比较队数')
    candidate = content['candidates'].get(str(payload['teamCount']))
    if not candidate or len(candidate['teams']) != payload['teamCount']:
        raise ValueError('该队数不在原比较范围内，请重新比较')
    if not payload.get('confirmPlanId') and payload['version'] != content['version']:
        raise ValueError('这次比较所用地图已更新，请重新比较')
    original = content['recommendation']['sourceRequest']
    request = {key: original[key] for key in ('assetId', 'controlIds', 'startPixel', 'finishPixel', 'startSurfaceId', 'finishSurfaceId',
               'mode', 'objective', 'timeLimitSec', 'mandatoryControlIds', 'mandatoryOrder') if key in original}
    request.update(version=payload['version'], teams=copy.deepcopy(candidate['teams']))
    return plan_course(request, progress, operation_id, selection={'saved': saved, 'payload': payload, 'candidate': candidate})


def plan_course(request, progress=lambda p, m: None, operation_id=None, auto_teams=False, selection=None, continuing=None):
    started = time.perf_counter()
    state = store.get_state(request['assetId'])
    if not state:
        raise ValueError('请先识别地图')
    if request.get('version') != state['version']:
        raise ValueError('地图已更新，请刷新后重新规划')
    lookup = {c['id']: c for c in state['controls']}
    ids = request['controlIds']
    if len(set(ids)) != len(ids) or any(i not in lookup for i in ids):
        raise ValueError('目标点重复或不存在')
    controls = [lookup[i] for i in ids]
    if not controls:
        raise ValueError('请至少选择一个目标')
    if any(c.get('role', 'control') != 'control' or c['state'] == 'rejected' for c in controls):
        raise ValueError('路线目标不能包含起终点符号或已排除的候选')
    confirm_choice = bool(selection and selection['payload'].get('confirmPlanId'))
    confirm_saved = bool(continuing and continuing['confirm'])
    if confirm_choice:
        previous = store.read_json(comparisons.plan_path(state['assetId'], selection['payload']['confirmPlanId']))
        chosen = (previous or {}).get('teamRecommendation') or {}
        if (chosen.get('selectionMode') != 'teacher' or chosen.get('comparisonId') != selection['payload']['comparisonId']
                or chosen.get('comparisonToken') != selection['payload']['comparisonToken']
                or chosen.get('selectedCount') != selection['payload']['teamCount']):
            raise ValueError('待发布路线与所选比较方案不一致')
    if confirm_choice or confirm_saved:
        required = {c['id'] for c in state['controls'] if c.get('required') and c.get('role', 'control') == 'control'}
        if not state['reviewed'] or required != set(ids) or any(c['state'] != 'accepted' or not c.get('calibrated') for c in controls):
            raise ValueError('请先核验所选课程目标，再恢复要发布的方案')
    progress(10, '建立保留细墙的通行图')
    graph = get_graph(state)
    reg = Registration.from_dict(state['registration'])
    progress(40, '计算必到点之间的合法最短路')
    bundle = plan_matrix(graph, controls, request['startPixel'], request['finishPixel'], reg,
                         metric='distance' if request['objective'] == 'min_distance' else 'time',
                         start_surface=request.get('startSurfaceId'), finish_surface=request.get('finishSurfaceId'))
    plan_id = operation_id or uuid.uuid4().hex[:16]
    if bundle['unresolved']:
        if selection or continuing:
            raise ValueError('原方案的点位已不可达，请重新核对并比较')
        return {'id': plan_id, 'assetId': state['assetId'], 'request': request,
                'routingSchema': ROUTING_SCHEMA, 'status': 'NEEDS_REVIEW', 'unresolved': bundle['unresolved'],
                'routes': [], 'graphHash': graph.content_hash, 'version': state['version'],
                'approaches': bundle.get('approaches', [])}
    progress(65, '复核原方案并保留当前队数' if continuing else '复核已比较的点序与路线' if selection else '优化队伍分配与访问顺序')
    preparation_elapsed = time.perf_counter()-started
    # Graphs are prepared with the map. Include reloading and the point matrix
    # in the class's calculation budget; keep room to expand and save paths.
    solver_budget = max(.01, request.get('timeLimitSec', 5)-preparation_elapsed-.65)
    comparison_to_save = None
    geometries = {}
    def expand_result(value):
        for route, team in zip(value['routes'], request['teams']):
            order = tuple(route['order'])
            if order not in geometries:
                geometries[order] = expand_route(graph, bundle, route['order'])
            route.update(geometries[order])
            route['travelSec'] = route['travelWeightM']/team['speedMps']
        value['rendezvous'] = apply_schedule(value['routes'], controls, request['teams'], request.get('mandatoryControlIds', []))
        return comparisons.route_metrics(value)

    def restore_candidate(item):
        mandatory_ids = request.get('mandatoryControlIds', [])
        node_ids = {c['id']: i+1 for i, c in enumerate(controls)}
        mandatory = tuple(node_ids[c] for c in mandatory_ids)
        validate_sequences(item['orders'], len(controls), len(request['teams']), request['mode'], mandatory,
                           mandatory if request.get('mandatoryOrder') else ())
        proof = copy.deepcopy(item['proof'])
        return evaluate_routes(bundle['matrix'], controls, request['teams'], copy.deepcopy(item['orders']),
            request['mode'], request['objective'], mandatory_ids, request.get('mandatoryOrder', False),
            proven=proof['globalOptimalityProven'], algorithm=proof['algorithm'], proof_details=proof)
    if auto_teams:
        metrics = {}
        def measure(candidate, teams):
            for route, team in zip(candidate['routes'], teams):
                legs = []
                for a, b in zip(route['order'], route['order'][1:]):
                    if (a, b) not in metrics:
                        path = graph.expand(bundle['parents'][a], bundle['nodes'][a], bundle['nodes'][b])
                        lengths = weights = 0.
                        if len(path) > 1:
                            lengths = float(graph.lengths[path[:-1], path[1:]].sum())
                            weights = float(graph.weights[path[:-1], path[1:]].sum())
                        metrics[a, b] = {'distanceM': lengths+float(bundle['connectors'][a]+bundle['connectors'][b]),
                                         'travelWeightM': weights+float(bundle['connectorWeights'][a]+bundle['connectorWeights'][b]),
                                         'pathHash': comparisons.path_fingerprint(path)}
                    legs.append(metrics[a, b])
                route['legs'] = legs
                route['distanceM'] = sum(leg['distanceM'] for leg in legs)
                route['travelSec'] = sum(leg['travelWeightM'] for leg in legs)/team['speedMps']
            candidate['rendezvous'] = apply_schedule(candidate['routes'], controls, teams, request.get('mandatoryControlIds', []))
            return {'latestFinishSec': max(r['finishSec'] for r in candidate['routes']),
                    'totalFinishSec': sum(r['finishSec'] for r in candidate['routes']),
                    'totalDistanceM': sum(r['distanceM'] for r in candidate['routes']),
                    'totalWaitSec': sum(r['waitSec'] for r in candidate['routes'])}
        result, chosen_teams = recommend_teams(bundle['matrix'], controls, request, solver_budget, measure, progress)
        comparison_to_save = comparisons.create(state, request, result, bundle, metrics, plan_id,
                                                 graph.content_hash, implementation_signature())
        result['teamRecommendation'] = comparisons.public(comparison_to_save, len(chosen_teams), 'automatic')
        request = {key: request[key] for key in ('assetId', 'version', 'controlIds', 'startPixel', 'finishPixel', 'startSurfaceId', 'finishSurfaceId',
                   'mode', 'objective', 'timeLimitSec', 'mandatoryControlIds', 'mandatoryOrder') if key in request}
        request['teams'] = chosen_teams
    elif selection:
        saved, candidate = selection['saved'], selection['candidate']
        comparisons.verify_and_restore(saved, state, graph, bundle, implementation_signature())
        mandatory_ids = request.get('mandatoryControlIds', [])
        node_ids = {c['id']: i+1 for i, c in enumerate(controls)}
        mandatory = tuple(node_ids[c] for c in mandatory_ids)
        validate_sequences(candidate['orders'], len(controls), len(request['teams']), request['mode'], mandatory,
                           mandatory if request.get('mandatoryOrder') else ())
        proof = copy.deepcopy(candidate['proof'])
        proof['scope'] += ' 沿用同次比较的分队、点序与证明；本次只复核并恢复路线，未重新优化。'
        proof['reusedComparisonId'] = saved['content']['id']
        result = evaluate_routes(bundle['matrix'], controls, request['teams'], candidate['orders'],
            request['mode'], request['objective'], mandatory_ids, request.get('mandatoryOrder', False),
            proven=proof['globalOptimalityProven'], algorithm=proof['algorithm'], proof_details=proof)
        if confirm_choice:
            comparison_to_save = comparisons.revalidated_copy(saved, state, plan_id)
            saved = comparison_to_save
        result['teamRecommendation'] = comparisons.public(saved, len(request['teams']), 'teacher')
        if candidate.get('refinement'):
            result['refinement'] = copy.deepcopy(candidate['refinement'])
    elif continuing:
        saved, item = continuing['saved'], continuing['candidate']
        comparisons.verify_and_restore(saved, state, graph, bundle, implementation_signature())
        baseline = restore_candidate(item)
        before = expand_result(baseline)
        stored = comparisons.route_metrics(continuing['plan'])
        if any(abs(value-stored[key]) > max(1e-5, abs(value)*1e-10) for key, value in before.items()) \
                or any(a['geometry'] != b['geometry'] for a, b in zip(baseline['routes'], continuing['plan']['routes'])):
            raise ValueError('原路线与当前复核结果不一致，请重新生成')
        result = baseline
        if confirm_saved:
            solver_budget = 0
            if item.get('refinement'):
                result['refinement'] = copy.deepcopy(item['refinement'])
            comparison_to_save = comparisons.revalidated_copy(saved, state, plan_id)
        else:
            attempted = not baseline['proof']['globalOptimalityProven']
            if attempted:
                preparation_elapsed = time.perf_counter()-started
                solver_budget = max(.01, request['timeLimitSec']-preparation_elapsed-.65)
                progress(70, f'保持 {len(request["teams"])} 队，继续优化当前路线')
                candidate = solve(bundle['matrix'], controls, request['teams'], request['mode'], request['objective'], solver_budget,
                    mandatory_ids=request.get('mandatoryControlIds'), mandatory_order=request.get('mandatoryOrder', False),
                    initial_sequences=item['orders'])
                after = expand_result(candidate)
                old_value, new_value = comparisons.objective_value(before, request['objective']), comparisons.objective_value(after, request['objective'])
                same_orders = [r['order'] for r in candidate['routes']] == item['orders']
                if improves(new_value, old_value) or (same_orders and not improves(old_value, new_value)):
                    result = candidate
            else:
                solver_budget = 0
            after = comparisons.route_metrics(result)
            result['refinement'] = {'sourcePlanId': continuing['plan']['id'], 'budgetSec': request['timeLimitSec'],
                'before': before, 'after': after, 'attempted': attempted,
                'improved': improves(comparisons.objective_value(after, request['objective']), comparisons.objective_value(before, request['objective'])),
                'round': (item.get('refinement') or {}).get('round', 0)+1}
            comparison_to_save = comparisons.refined_copy(saved, state, plan_id, result, request, graph, bundle)
        if saved['content'].get('recommendation'):
            mode = (continuing['plan'].get('teamRecommendation') or {}).get('selectionMode', 'automatic') if confirm_saved else 'teacher'
            result['teamRecommendation'] = comparisons.public(comparison_to_save, len(request['teams']), mode)
    else:
        result = solve(bundle['matrix'], controls, request['teams'], request['mode'], request['objective'], solver_budget,
                       mandatory_ids=request.get('mandatoryControlIds'), mandatory_order=request.get('mandatoryOrder', False),
                       custom_routes=request.get('customRoutes'))
    expand_result(result)
    if selection:
        row = next(r for r in selection['saved']['content']['recommendation']['candidates'] if r['teamCount'] == len(request['teams']))
        observed = {'latestFinishSec': max(r['finishSec'] for r in result['routes']),
                    'totalFinishSec': sum(r['finishSec'] for r in result['routes']),
                    'totalDistanceM': sum(r['distanceM'] for r in result['routes']),
                    'totalWaitSec': sum(r['waitSec'] for r in result['routes'])}
        if any(abs(value-row[key]) > max(1e-5, abs(value)*1e-10) for key, value in observed.items()):
            raise ValueError('复核后的路线指标与原比较不一致，请重新比较')
    if not (auto_teams or selection or continuing):
        paths = comparisons.used_paths(graph, bundle, [r['order'] for r in result['routes']])
        metrics = {tuple(map(int, key.split(':'))): {'pathHash': value} for key, value in paths.items()}
        comparison_to_save = comparisons.create(state, request, result, bundle, metrics, plan_id,
                                                 graph.content_hash, implementation_signature())
    context = comparison_to_save or (selection['saved'] if selection else None)
    result['continuation'] = comparisons.reference(context)
    result.update(id=plan_id, assetId=state['assetId'], version=state['version'], graphHash=graph.content_hash,
                  routingSchema=ROUTING_SCHEMA,
                  request=request, draft=not state['reviewed'] or any(c['state'] != 'accepted' or not c.get('calibrated') for c in controls), createdAt=time.time(),
                  approaches=bundle.get('approaches', []),
                  graphStatistics={'nodes': len(graph.pixels), 'edges': graph.weights.nnz,
                                   'components': int(graph.component_count), 'gridStepPx': graph.step,
                                   'fineGridStepPx': 1 if graph.adaptive else graph.step})
    result['proof']['graphHash'] = graph.content_hash
    result['targetSurfaces'] = {endpoint['id']: endpoint.get('surfaceId', 'ground') for endpoint in bundle['endpoints']}
    result['surfaces'] = [{key: graph.surface_info(identifier)[key] for key in ('id', 'label', 'level', 'accepted')}
                          for identifier in getattr(graph, 'surface_order', ['ground'])
                          if graph.surface_info(identifier).get('accepted', True)]
    for route in result['routes']:
        for stop in route.get('stops', []):
            stop['surfaceId'] = result['targetSurfaces'][stop['controlId']]
    result['preparationSec'] = round(preparation_elapsed, 3)
    result['proof']['solverBudgetSec'] = 0 if selection or confirm_saved else round(solver_budget, 3)
    result['planningElapsedSec'] = round(time.perf_counter()-started, 3)
    if any(a['automatic'] for a in result['approaches']):
        result['draft'] = True
        result['proof']['scope'] += ' 自动建议的到访位置尚待核验；最优性针对当前选定位置。'
    with store.LOCK:
        current = store.get_state(state['assetId'])
        if current['version'] != state['version']:
            raise ValueError('计算期间地图已更新，请重新规划')
        if selection:
            comparisons.load(state['assetId'], selection['payload']['comparisonId'], selection['payload']['comparisonToken'])
        if continuing:
            latest = saved_plan(state['assetId'], continuing['plan']['id'])
            if latest['hash'] != continuing['hash'] or latest['saved']['token'] != continuing['saved']['token']:
                raise ValueError('继续计算期间原路线记录已变化，请重新选择')
            if comparisons.model_inputs(current, ids) != comparisons.model_inputs(state, ids):
                raise ValueError('继续计算期间地图输入已变化，请重新生成')
        if (confirm_choice or confirm_saved) and not current['reviewed']:
            raise ValueError('课程核验状态已改变，请重新核验')
        current['graphHash'] = graph.content_hash
        if comparison_to_save:
            store.atomic_json(comparisons.path(state['assetId'], comparison_to_save['content']['id']), comparison_to_save)
        store.atomic_json(store.asset_dir(state['assetId'])/f'plan-{plan_id}.json', result)
        store.save_state(state['assetId'], current, operation_id, result)
    return result


def apply_anchors(asset_id, anchors, expected_version=None, operation_id=None):
    state = store.get_state(asset_id)
    if not state:
        raise ValueError('请先分析原图')
    if expected_version is not None and state['version'] != expected_version:
        raise ValueError('地图已更新，请刷新后重新校准')
    for anchor in anchors:
        x, y = anchor['pixel']
        if not 0 <= x < state['width'] or not 0 <= y < state['height']:
            raise ValueError('同名点超出原图范围')
    reg, quality = manual_register(anchors)
    image = read_image(store.get_asset(asset_id)['path'], (state['width'], state['height']))
    state['registration'] = reg.as_dict()
    state['quality'] = quality
    initial_version = state['version']
    with tempfile.TemporaryDirectory(prefix='.anchors-', dir=store.asset_dir(asset_id)) as temporary:
        directory = Path(temporary)
        state['extent'] = warp_overlay(image, reg, directory/'overlay.png')
        terrain = cv2.imread(str(store.artifact_path(state, 'terrain.png')), cv2.IMREAD_UNCHANGED)
        warp_overlay(terrain, reg, directory/'terrain-warp.png', max_side=1200, nearest=True)
        state['version'] += 1
        state['reviewed'] = False
        state['graphHash'] = None
        project_controls(state)
        with store.LOCK:
            if store.get_state(asset_id)['version'] != initial_version:
                raise ValueError('校准期间地图被修改，已保留最新修改')
            store.stage_artifacts(state, directory)
            store.save_state(asset_id, state, operation_id)
    GRAPH_CACHE.pop(asset_id, None)
    return state
