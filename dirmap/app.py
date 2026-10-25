"""Run with: python -m uvicorn dirmap.app:app --port 8765."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import time
from typing import Annotated, Literal
import uuid

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from . import service, store
from .geometry import Frame
from .graph import ROUTING_SCHEMA
from .jobs import JobQueue
from .reference import geojson
from .schedule import custom_sequences
from .team_planning import team_counts
from .vision import read_image

@asynccontextmanager
async def lifespan(app):
    queue = JobQueue(store.DATA, service.execute_job)
    app.state.jobs = queue
    queue.start()
    try:
        yield
    finally:
        await asyncio.to_thread(queue.close)


app = FastAPI(title='DirMap', version='0.1.0', lifespan=lifespan)
Point = tuple[FiniteFloat, FiniteFloat]


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Team(Model):
    id: str = Field(min_length=1, max_length=50)
    people: int = Field(ge=1, le=500, strict=True)
    speedMps: FiniteFloat = Field(ge=.2, le=10)
    departureSec: FiniteFloat = Field(default=0, ge=0, le=86400)


class CustomRoute(Model):
    teamId: str = Field(min_length=1, max_length=50)
    controlIds: list[str] = Field(min_length=1, max_length=60)


class PlanRequest(Model):
    assetId: str
    version: int = Field(ge=1)
    controlIds: list[str] = Field(min_length=1, max_length=60)
    startPixel: Point
    finishPixel: Point
    mode: Literal['all_visit', 'split_coverage', 'fixed_order', 'mixed_coverage', 'custom'] = 'all_visit'
    mandatoryControlIds: list[str] = Field(default_factory=list, max_length=60)
    mandatoryOrder: bool = False
    customRoutes: list[CustomRoute] = Field(default_factory=list, max_length=30)
    objective: Literal['min_max_finish_then_sum', 'min_total_time', 'min_distance'] = 'min_max_finish_then_sum'
    teams: list[Team] = Field(min_length=1, max_length=30)
    timeLimitSec: FiniteFloat = Field(default=5, ge=.1, le=30)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.controlIds)) != len(self.controlIds):
            raise ValueError('目标点不能重复')
        if len({t.id for t in self.teams}) != len(self.teams):
            raise ValueError('队伍编号不能重复')
        if len(set(self.mandatoryControlIds)) != len(self.mandatoryControlIds) or not set(self.mandatoryControlIds).issubset(self.controlIds):
            raise ValueError('每队必到点必须是已经选择的唯一目标')
        if self.mode == 'mixed_coverage' and not self.mandatoryControlIds:
            raise ValueError('混合模式至少需要一个每队必到点')
        if self.mode not in ('mixed_coverage', 'custom') and (self.mandatoryControlIds or self.mandatoryOrder):
            raise ValueError('集合点设置仅用于混合或自定义模式')
        if self.mode == 'custom':
            custom_sequences(self.controlIds, [t.id for t in self.teams],
                             [r.model_dump() for r in self.customRoutes], self.mandatoryControlIds, self.mandatoryOrder)
        elif self.customRoutes:
            raise ValueError('自定义路线仅用于自定义模式')
        return self


class AutoTeamRequest(Model):
    assetId: str
    version: int = Field(ge=1)
    controlIds: list[str] = Field(min_length=1, max_length=60)
    startPixel: Point
    finishPixel: Point
    mode: Literal['all_visit', 'split_coverage', 'fixed_order', 'mixed_coverage'] = 'all_visit'
    mandatoryControlIds: list[str] = Field(default_factory=list, max_length=60)
    mandatoryOrder: bool = False
    objective: Literal['min_max_finish_then_sum', 'min_total_time', 'min_distance'] = 'min_max_finish_then_sum'
    totalPeople: int = Field(ge=1, le=500, strict=True)
    minPeoplePerTeam: int = Field(default=2, ge=1, le=500, strict=True)
    maxTeams: int = Field(default=30, ge=1, le=30, strict=True)
    speedMps: FiniteFloat = Field(ge=.2, le=10)
    staggerSec: FiniteFloat = Field(default=0, ge=0, le=86400)
    targetGainFraction: FiniteFloat = Field(default=.9, ge=.5, le=1)
    timeLimitSec: FiniteFloat = Field(default=5, ge=.1, le=30)

    @model_validator(mode='after')
    def valid_course(self):
        data = self.model_dump()
        fields = {key: data[key] for key in ('assetId', 'version', 'controlIds', 'startPixel', 'finishPixel',
                  'mode', 'mandatoryControlIds', 'mandatoryOrder', 'objective', 'timeLimitSec')}
        PlanRequest(**fields, teams=[{'id': 'comparison', 'people': self.totalPeople, 'speedMps': self.speedMps}])
        team_counts(self.totalPeople, self.minPeoplePerTeam, self.maxTeams, len(self.controlIds), self.mode, self.staggerSec)
        return self


class Control(Model):
    id: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=40)
    pixel: Point
    approachPixel: Point | None = None
    radiusPx: FiniteFloat = Field(default=15, gt=0, le=200)
    required: bool = True
    role: Literal['control', 'start', 'finish', 'start_finish'] = 'control'
    serviceBaseSec: FiniteFloat = Field(default=5, ge=0, le=3600)
    servicePerPersonSec: FiniteFloat = Field(default=1, ge=0, le=300)
    state: Literal['candidate', 'accepted', 'rejected'] = 'candidate'

    @model_validator(mode='after')
    def reserved_id(self):
        if self.id in ('start', 'finish'):
            raise ValueError('start 和 finish 为路线端点保留标识，请使用其他点位标识')
        return self


class Edit(Model):
    kind: Literal['open', 'close']
    pixels: list[Point] = Field(min_length=1, max_length=2000)
    radiusPx: int = Field(default=6, ge=1, le=100)
    cost: FiniteFloat = Field(default=1, ge=1, le=10)


class StatePatch(Model):
    version: int
    controls: list[Control] | None = Field(default=None, max_length=120)
    edits: list[Edit] | None = Field(default=None, max_length=500)


class Anchor(Model):
    pixel: Point
    lnglat: Point
    purpose: Literal['fit', 'validation'] = 'fit'


class AnchorRequest(Model):
    version: int
    anchors: list[Anchor] = Field(min_length=3, max_length=200)


class TerrainRequest(Model):
    version: int = Field(ge=1)


class ReviewRequest(Model):
    version: int
    registrationChecked: bool
    controlsChecked: bool
    passagesChecked: bool
    notes: str = Field(default='', max_length=2000)


class ReleaseRequest(Model):
    assetId: str
    planId: str
    title: str = Field(default='深大定向越野课', min_length=1, max_length=100)


class NavigationRequest(Model):
    teamId: str = Field(min_length=1, max_length=50)
    lnglat: Point
    accuracyM: FiniteFloat = Field(ge=0, le=10000)
    timestampMs: FiniteFloat
    completedControlIds: list[str] = Field(default_factory=list, max_length=60)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse(status_code=422, content={'detail': str(exc)})


@app.middleware('http')
async def origin_guard(request: Request, call_next):
    # Guard cross-origin writes to a local teaching server. A deployment can
    # additionally require an operator token; release reads remain public.
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            from urllib.parse import urlparse
            if urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse(status_code=403, content={'detail': '不允许跨站修改课程'})
        token = os.environ.get('DIRMAP_ADMIN_TOKEN')
        student_navigation = request.method == 'POST' and re.fullmatch(r'/api/releases/[0-9a-f]{32}/navigate', request.url.path)
        if token and not student_navigation:
            import secrets
            supplied = request.headers.get('x-dirmap-token', '')
            if not secrets.compare_digest(supplied, token):
                return JSONResponse(status_code=401, content={'detail': '请输入教师管理口令'})
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if request.url.path.startswith('/api/jobs'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/api/health')
def health():
    from datetime import date
    from pygeomag import GeoMag
    today = date.today()
    decimal_year = today.year + (today-date(today.year, 1, 1)).days / ((date(today.year+1, 1, 1)-date(today.year, 1, 1)).days)
    declination = None
    if 2025 <= decimal_year <= 2030:
        declination = GeoMag().calculate(glat=Frame().lat, glon=Frame().lng, alt=0, time=decimal_year).d
    deployment = store.read_json(store.ROOT/'deployment.json', {})
    return {'status': 'ok', 'version': '0.1.0', 'frame': Frame().as_dict(),
            'revision': deployment.get('revision'),
            'adminTokenRequired': bool(os.environ.get('DIRMAP_ADMIN_TOKEN')),
            'magneticDeclinationDeg': declination, 'magneticModel': 'WMM-2025', 'modelDate': today.isoformat()}


@app.get('/api/catalog')
def catalog():
    maps = []
    for asset in store.catalog():
        state = store.get_state(asset['id'])
        maps.append({k: v for k, v in asset.items() if k != 'path'} | {'processed': bool(state),
                     'version': state['version'] if state else None,
                     'controls': len(state['controls']) if state else None,
                     'courseDefaults': store.course_defaults(asset['sha256'])})
    legends = [{'name': p.name, 'url': f'/assets/legend/{index}'} for index, p in enumerate(legend_paths())]
    return {'maps': maps, 'legends': legends, 'reference': {'crs': 'WGS84', 'name': '深圳大学粤海校区',
            'attribution': '© OpenStreetMap contributors', 'licenseUrl': 'https://www.openstreetmap.org/copyright'}}


def legend_paths():
    return sorted([*store.ROOT.glob('*.pdf'), *store.ROOT.glob('*.png')])


@app.get('/assets/legend/{index}')
def legend(index: int):
    paths = legend_paths()
    if not 0 <= index < len(paths):
        raise HTTPException(404)
    return FileResponse(paths[index])


@app.get('/assets/maps/{asset_id}/original')
def original(asset_id: str):
    return FileResponse(store.get_asset(asset_id)['path'])


@app.get('/assets/maps/{asset_id}/{artifact}')
def artifact(asset_id: str, artifact: str, v: int | None = None):
    if artifact not in ('overlay.png', 'terrain.png', 'terrain-warp.png', 'blocked.png', 'working.jpg'):
        raise HTTPException(404)
    state = store.get_state(asset_id)
    if v is not None and (not state or state['version'] != v):
        raise HTTPException(409, '地图版本已更新，请刷新后查看叠层')
    if artifact in ('overlay.png', 'terrain-warp.png') and state and service.registration_stale(state):
        raise HTTPException(409, '基准图已更新，请重新配准')
    try:
        path = store.artifact_path(state, artifact) if state else store.asset_dir(asset_id)/artifact
    except FileNotFoundError:
        path = None
    if artifact == 'working.jpg' and (path is None or not path.exists()):
        path = Path(store.get_asset(asset_id)['path'])
    if path is None or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, headers={'Cache-Control': 'no-cache'})


@app.get('/api/reference')
def reference():
    return geojson(service.reference())


@app.get('/api/maps/{asset_id}')
def map_state(asset_id: str):
    store.get_asset(asset_id)
    state = store.get_state(asset_id)
    if state is None:
        return {'assetId': asset_id, 'processed': False}
    if service.registration_stale(state):
        state.update(registrationStale=True, reviewed=False)
        for control in state['controls']:
            control.update(calibrated=False, lnglat=None)
        state.setdefault('warnings', []).append('基准图配准已变化，请重新配准当前图幅')
    terrain_stale = state.get('terrainSchema') != service.TERRAIN_SCHEMA
    if terrain_stale:
        state['reviewed'] = False
    return state | {'processed': True, 'terrainStale': terrain_stale}


def submit_job(request: Request, kind, asset_id, payload, expected_version=None):
    request_id = request.headers.get('x-dirmap-request-id')
    if request_id and not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id):
        raise ValueError('提交标识格式不正确')
    queue = request.app.state.jobs
    prior = queue.by_request(request_id, kind, asset_id, payload)
    if prior:
        return prior
    with store.LOCK:
        # A matching request may have committed while this server process
        # waited for the map lock; replay it before checking its old version.
        prior = queue.by_request(request_id, kind, asset_id, payload)
        if prior:
            return prior
        if expected_version is not None:
            state = store.get_state(asset_id)
            if not state or state['version'] != expected_version:
                raise HTTPException(409, '地图已更新，请刷新后重试')
        inputs = service.job_inputs(kind, asset_id)
        return queue.submit(kind, asset_id, payload, inputs, request_id)


@app.post('/api/maps/{asset_id}/process')
def process_map(asset_id: str, request: Request, preserveRegistration: bool = False):
    payload = {'preserveRegistration': True} if preserveRegistration else {}
    return submit_job(request, 'process', asset_id, payload)


@app.post('/api/maps/{asset_id}/terrain')
def refresh_terrain(asset_id: str, request: TerrainRequest, http_request: Request):
    return submit_job(http_request, 'terrain', asset_id, request.model_dump(), request.version)


@app.get('/api/jobs')
def recent_jobs(request: Request, assetId: str | None = None):
    return request.app.state.jobs.recent(assetId)


@app.get('/api/jobs/request/{request_id}')
def job_by_request(request_id: str, request: Request):
    job = request.app.state.jobs.by_request(request_id)
    if job is None:
        raise HTTPException(404, '未找到这次提交，请检查任务记录后重新操作')
    return job


@app.get('/api/jobs/{job_id}')
def job(job_id: str, request: Request):
    result = request.app.state.jobs.get(job_id)
    if result is None:
        raise HTTPException(404, '任务不存在，请检查任务记录和当前地图')
    return result


@app.post('/api/maps/upload')
async def upload(file: UploadFile = File(...)):
    if Path(file.filename or '').suffix.lower() not in ('.jpg', '.jpeg', '.png'):
        raise ValueError('请上传 JPEG 或 PNG 地图')
    payload = await file.read(20*1024*1024+1)
    if len(payload) > 20*1024*1024:
        raise ValueError('地图大小不能超过 20 MiB')
    asset_id = 'upload-'+uuid.uuid4().hex[:12]
    path = store.asset_dir(asset_id)/'source.img'
    path.write_bytes(payload)
    try:
        read_image(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    asset = {'id': asset_id, 'path': path.relative_to(store.DATA).as_posix(), 'name': Path(file.filename).stem[:80],
             'filename': Path(file.filename).name, 'role': 'map', 'sha256': hashlib.sha256(payload).hexdigest()}
    with store.LOCK:
        uploads = store.read_json(store.DATA/'uploads.json', [])
        uploads.append(asset)
        store.atomic_json(store.DATA/'uploads.json', uploads)
    return {k: v for k, v in asset.items() if k != 'path'}


@app.patch('/api/maps/{asset_id}')
def patch_map(asset_id: str, patch: StatePatch):
    with store.LOCK:
        state = store.get_state(asset_id)
        if not state or state['version'] != patch.version:
            raise HTTPException(409, '地图已更新，请刷新后重试')
        data = patch.model_dump(exclude_none=True)
        for control in data.get('controls', []):
            for p in (control['pixel'], control.get('approachPixel')):
                if p is not None and not (0 <= p[0] < state['width'] and 0 <= p[1] < state['height']):
                    raise ValueError('点位超出地图范围')
        if patch.controls is not None:
            if len({c.id for c in patch.controls}) != len(patch.controls):
                raise ValueError('目标点标识重复')
            previous = {c['id']: c for c in state['controls']}
            for control in data['controls']:
                old = previous.get(control['id'], {})
                fields = set(old.get('manualFields', []))
                fields.update(key for key in ('label', 'pixel', 'approachPixel', 'radiusPx', 'role', 'serviceBaseSec', 'servicePerPersonSec')
                              if key in old and control.get(key) != old.get(key))
                if fields:
                    control['manualFields'] = sorted(fields)
                if control['label'] != old.get('label'):
                    control['labelSource'] = 'manual'
                else:
                    for key in ('labelSource', 'labelConfidence', 'labelPixelBox', 'labelAmbiguous', 'confidence'):
                        if key in old:
                            control[key] = old[key]
            present = {c['id'] for c in data['controls']}
            state.setdefault('recognitionExclusions', []).extend(
                {k: old[k] for k in ('id', 'pixel', 'radiusPx') if k in old}
                for identifier, old in previous.items() if identifier not in present)
            state['controls'] = data['controls']
        if patch.edits is not None:
            for edit in data['edits']:
                if any(not (0 <= x < state['width'] and 0 <= y < state['height']) for x, y in edit['pixels']):
                    raise ValueError('通行修正超出地图')
            state['edits'] = data['edits']
        state.update(version=state['version']+1, reviewed=False, graphHash=None, updatedAt=time.time())
        service.project_controls(state)
        store.save_state(asset_id, state)
        service.GRAPH_CACHE.pop(asset_id, None)
        return state


@app.post('/api/maps/{asset_id}/anchors')
def anchors(asset_id: str, request: AnchorRequest, http_request: Request):
    return submit_job(http_request, 'anchors', asset_id, request.model_dump(), request.version)


@app.post('/api/maps/{asset_id}/review')
def review(asset_id: str, request: ReviewRequest):
    with store.LOCK:
        state = store.get_state(asset_id)
        if not state or request.version != state['version']:
            raise HTTPException(409, '地图已更新，请刷新后重试')
        if not state.get('registration') or not all((request.registrationChecked, request.controlsChecked, request.passagesChecked)):
            raise ValueError('请先完成配准、点位和可通行区域核验')
        if any(c['required'] and (not c.get('calibrated') or c['state'] != 'accepted') for c in state['controls']):
            raise ValueError('仍有必到点未确认或超出配准范围')
        state.update(reviewed=True, review={'at': time.time(), **request.model_dump()})
        store.save_state(asset_id, state)
        return state


@app.post('/api/plans')
def plan(request: PlanRequest, http_request: Request):
    # Requests with different constraints are different jobs. Do not dedupe
    # solely by map ID and accidentally return somebody else's route.
    return submit_job(http_request, 'plan', request.assetId, request.model_dump())


@app.post('/api/plans/auto-teams')
def auto_teams(request: AutoTeamRequest, http_request: Request):
    return submit_job(http_request, 'auto_teams', request.assetId, request.model_dump())


@app.post('/api/releases')
def release(request: ReleaseRequest):
    if any(c not in '0123456789abcdef' for c in request.planId):
        raise ValueError('路线标识无效')
    with store.LOCK:
        state = store.get_state(request.assetId)
        plan = store.read_json(store.asset_dir(request.assetId)/f'plan-{request.planId}.json')
        if not state or not plan or not state['reviewed']:
            raise ValueError('请先核验地图并生成路线')
        if any(a.get('automatic') for a in plan.get('approaches', [])):
            raise ValueError('请先核对并保存建议的到访位置，再生成发布路线')
        if state['version'] != plan['version'] or state['graphHash'] != plan['graphHash']:
            raise ValueError('地图或通行状态已变化，请重新生成路线')
        if service.get_graph(state).content_hash != plan['graphHash']:
            raise ValueError('通行算法或数据已更新，请重新生成并核验路线')
        release_id = uuid.uuid4().hex
        result = {'id': release_id, 'title': request.title, 'assetId': request.assetId, 'createdAt': time.time(),
                  'plan': plan, 'controls': state['controls'], 'frame': state['frame'], 'extent': state['extent'],
                  'quality': state['quality'], 'review': state['review'], 'version': state['version'],
                  'routingSchema': ROUTING_SCHEMA}
        # Freeze derived raster together with the course. A later source edit
        # never mutates a previously issued student link.
        import shutil
        path = store.DATA/'releases'/release_id
        path.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store.artifact_path(state, 'overlay.png'), path/'overlay.png')
        store.atomic_json(path/'release.json', result)
        return {'id': release_id, 'url': '/?course='+release_id}


@app.get('/api/releases/{release_id}')
def get_release(release_id: str):
    if len(release_id) != 32 or any(c not in '0123456789abcdef' for c in release_id):
        raise HTTPException(404)
    value = store.read_json(store.DATA/'releases'/release_id/'release.json')
    if not value:
        raise HTTPException(404, '课程不存在')
    state = store.get_state(value['assetId'])
    value['superseded'] = (not state or state['version'] != value['version']
                           or state['graphHash'] != value['plan']['graphHash']
                           or value.get('routingSchema') != ROUTING_SCHEMA
                           or service.registration_stale(state))
    return value


@app.post('/api/releases/{release_id}/navigate')
def navigate(release_id: str, request: NavigationRequest):
    release = get_release(release_id)
    if release['superseded']:
        raise HTTPException(409, '课程已更新，请使用教师提供的新课程链接')
    return service.navigate_course(release, request.model_dump())


@app.get('/api/releases/{release_id}/overlay.png')
def release_overlay(release_id: str):
    get_release(release_id)
    return FileResponse(store.DATA/'releases'/release_id/'overlay.png', headers={'Cache-Control': 'public, max-age=31536000, immutable'})


@app.get('/api/maps/{asset_id}/export')
def export(asset_id: str):
    state = store.get_state(asset_id)
    if not state:
        raise HTTPException(404)
    return JSONResponse(state, headers={'Content-Disposition': f'attachment; filename="{asset_id}.json"'})


DIST = store.ROOT/'web'/'dist'
if DIST.exists():
    app.mount('/', StaticFiles(directory=DIST, html=True), name='web')
else:
    @app.get('/')
    def not_built():
        return JSONResponse({'message': '运行 npm run build 后重启服务，或通过 npm run dev 打开开发界面。'})
