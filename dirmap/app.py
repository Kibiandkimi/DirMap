"""Run with: python -m uvicorn dirmap.app:app --port 8765."""

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import math
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from typing import Annotated, Literal
import uuid

import anyio
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from . import accounts, groups, photos, service, store, layers, memory, printing, progress, field_checks, sites
from .geometry import Frame
from .graph import ROUTING_SCHEMA
from .jobs import JobQueue
from .reference import geojson
from .schedule import custom_sequences
from .team_planning import team_counts
from .vision import read_image

@asynccontextmanager
async def lifespan(app):
    memory.configure()
    queue = JobQueue(store.DATA, service.execute_job, maintenance=store.prune_history)
    app.state.jobs = queue
    queue.start()
    async def retain_groups():
        while True:
            try:
                await anyio.to_thread.run_sync(groups.prune)
            except Exception:
                logging.getLogger(__name__).exception('Group retention will retry in one minute')
            await asyncio.sleep(60)
    retention = asyncio.create_task(retain_groups())
    try:
        yield
    finally:
        retention.cancel()
        with suppress(asyncio.CancelledError):
            await retention
        await asyncio.to_thread(queue.close)


app = FastAPI(title='DirMap', version='0.1.0', lifespan=lifespan)
# Course packages are large JSON and the server's link to the class is slow;
# images are already compressed and stay as they are.
app.add_middleware(GZipMiddleware, minimum_size=1024)
Point = tuple[FiniteFloat, FiniteFloat]
MAX_UPLOADS = 30
# Anyone may register, so uploads stop well before the data disk fills.
MIN_FREE_BYTES = 2*1024**3


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
    startSurfaceId: str | None = Field(default=None, min_length=1, max_length=50)
    finishSurfaceId: str | None = Field(default=None, min_length=1, max_length=50)
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
    startSurfaceId: str | None = Field(default=None, min_length=1, max_length=50)
    finishSurfaceId: str | None = Field(default=None, min_length=1, max_length=50)
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
        fields = {key: data[key] for key in ('assetId', 'version', 'controlIds', 'startPixel', 'finishPixel', 'startSurfaceId', 'finishSurfaceId',
                  'mode', 'mandatoryControlIds', 'mandatoryOrder', 'objective', 'timeLimitSec')}
        PlanRequest(**fields, teams=[{'id': 'comparison', 'people': self.totalPeople, 'speedMps': self.speedMps}])
        team_counts(self.totalPeople, self.minPeoplePerTeam, self.maxTeams, len(self.controlIds), self.mode, self.staggerSec)
        return self


class TeamCountChoiceRequest(Model):
    assetId: str
    version: int = Field(ge=1)
    comparisonId: str = Field(pattern=r'^(?:[0-9a-f]{16}|[0-9a-f]{32})$')
    comparisonToken: str = Field(pattern=r'^[0-9a-f]{64}$')
    teamCount: int = Field(ge=1, le=30, strict=True)
    confirmPlanId: str | None = Field(default=None, pattern=r'^(?:[0-9a-f]{16}|[0-9a-f]{32})$')


class SavedPlanRequest(Model):
    assetId: str
    version: int = Field(ge=1)
    planId: str = Field(pattern=r'^(?:[0-9a-f]{16}|[0-9a-f]{32})$')


class RefinePlanRequest(SavedPlanRequest):
    timeLimitSec: FiniteFloat = Field(default=30, ge=.1, le=30)


class Control(Model):
    id: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=40)
    pixel: Point
    approachPixel: Point | None = None
    surfaceId: str | None = Field(default=None, min_length=1, max_length=50)
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
    surfaceId: str = Field(default='ground', min_length=1, max_length=50)
    separateGround: bool = False


class Surface(Model):
    id: str = Field(min_length=1, max_length=50, pattern=r'^[A-Za-z0-9_-]+$')
    label: str = Field(min_length=1, max_length=50)
    level: int = Field(ge=-20, le=20, strict=True)
    accepted: bool = False


class Portal(Model):
    id: str = Field(min_length=1, max_length=50, pattern=r'^[A-Za-z0-9_-]+$')
    label: str = Field(default='换层入口', min_length=1, max_length=50)
    fromSurface: str = Field(min_length=1, max_length=50)
    toSurface: str = Field(min_length=1, max_length=50)
    pixel: Point
    radiusPx: int = Field(default=8, ge=1, le=100)
    lengthM: FiniteFloat = Field(default=0, ge=0, le=1000)
    costFactor: FiniteFloat = Field(default=1, ge=1, le=10)
    accepted: bool = False


class StatePatch(Model):
    version: int
    controls: list[Control] | None = Field(default=None, max_length=120)
    edits: list[Edit] | None = Field(default=None, max_length=500)
    surfaces: list[Surface] | None = Field(default=None, max_length=16)
    portals: list[Portal] | None = Field(default=None, max_length=64)


class Anchor(Model):
    pixel: Point
    lnglat: Point
    purpose: Literal['fit', 'validation'] = 'fit'


class FieldCheck(Model):
    pixel: Point
    lnglat: Point
    accuracyM: FiniteFloat = Field(gt=0, le=1000)
    measuredAt: FiniteFloat

    @model_validator(mode='after')
    def plausible_position(self):
        if not (-180 <= self.lnglat[0] <= 180 and -90 <= self.lnglat[1] <= 90):
            raise ValueError('经纬度无效')
        return self


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


class Credentials(Model):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class Registration(Credentials):
    claimOriginal: bool = False
    originalToken: str | None = Field(default=None, max_length=200)


class PasswordChange(Model):
    oldPassword: str = Field(min_length=1, max_length=200)
    newPassword: str = Field(min_length=1, max_length=200)


class ClaimRequest(Model):
    originalToken: str | None = Field(default=None, max_length=200)


class GroupCreate(Model):
    name: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=60)
    releaseId: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    teamId: str | None = Field(default=None, min_length=1, max_length=50)
    center: Point | None = None


class GroupJoin(Model):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=40)
    teamId: str | None = Field(default=None, min_length=1, max_length=50)


class SharedPosition(Model):
    lnglat: Point
    accuracyM: FiniteFloat = Field(ge=0, le=10000)
    headingDeg: FiniteFloat | None = Field(default=None, ge=0, le=360)
    timestampMs: FiniteFloat


class GroupSync(Model):
    sharing: bool = False
    position: SharedPosition | None = None
    after: int = Field(default=0, ge=0, le=10**9, strict=True)


class GroupMessage(Model):
    kind: Literal['text', 'pin', 'help', 'resolved']
    text: str | None = Field(default=None, max_length=200)
    lnglat: Point | None = None
    after: int = Field(default=0, ge=0, le=10**9, strict=True)


class GroupManage(Model):
    action: Literal['lock', 'unlock', 'remove', 'close']
    memberId: str | None = Field(default=None, pattern=r'^[0-9a-f]{8}$')


class PhotoExport(Model):
    format: Literal['pdf', 'long-image', 'images']


class PhotoFinish(Model):
    exportId: str = Field(pattern=r'^[0-9a-f]{32}$')


class SiteRequest(Model):
    name: str = Field(min_length=1, max_length=40)
    bbox: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]


class ProgressReport(Model):
    teamId: str = Field(min_length=1, max_length=50)
    deviceId: str = Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')
    revision: int = Field(ge=0, le=10**9, strict=True)
    completed: list[str] = Field(default_factory=list, max_length=60)
    checkIns: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=60)
    departed: list[str] = Field(default_factory=list, max_length=60)
    arrived: bool = False
    arrivedAt: FiniteFloat | None = None


class SessionRequest(Model):
    action: Literal['start', 'reset']


class NavigationRequest(Model):
    teamId: str = Field(min_length=1, max_length=50)
    lnglat: Point
    accuracyM: FiniteFloat = Field(ge=0, le=10000)
    timestampMs: FiniteFloat
    completedControlIds: list[str] = Field(default_factory=list, max_length=60)
    surfaceId: str | None = Field(default=None, min_length=1, max_length=50)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse(status_code=422, content={'detail': str(exc)})


@app.exception_handler(accounts.AuthError)
async def auth_error(request, exc):
    return JSONResponse(status_code=400, content={'detail': str(exc)})


@app.exception_handler(groups.Gone)
async def group_gone(request, exc):
    # The phone forgets this membership instead of retrying it.
    return JSONResponse(status_code=410, content={'detail': str(exc)})


@app.exception_handler(photos.PhotoConflict)
async def photo_conflict(request, exc):
    return JSONResponse(status_code=409, content={'detail': str(exc)})


@app.exception_handler(photos.PhotoStorageFull)
async def photo_storage_full(request, exc):
    return JSONResponse(status_code=507, content={'detail': str(exc)})


@app.exception_handler(store.WorkspaceChanged)
async def workspace_changed(request, exc):
    return JSONResponse(status_code=409, content={'detail': str(exc), 'accountChanged': True})


WRITES = ('POST', 'PUT', 'PATCH', 'DELETE')
OPEN_PATHS = re.compile(r'/api/(?:health|auth/[a-z-]+|groups(?:/.*)?)')
STUDENT_READS = re.compile(r'/api/releases/[0-9a-f]{32}(?:/(?:status|progress|overlay\.png|route-map|qr\.png|reference))?')
STUDENT_WRITES = re.compile(r'/api/releases/[0-9a-f]{32}/(?:navigate|progress)')
OWNER_PATHS = re.compile(r'/api/releases/[0-9a-f]{32}/(?:session|groups)')


def access_scope(method, path):
    """Who may call: anyone, the course's owner, or the workbench's owner."""
    if not path.startswith(('/api/', '/assets/maps/')) or OPEN_PATHS.fullmatch(path):
        return 'open'
    if (method in ('GET', 'HEAD') and STUDENT_READS.fullmatch(path)) or (method == 'POST' and STUDENT_WRITES.fullmatch(path)):
        return 'open'
    return 'owner' if OWNER_PATHS.fullmatch(path) else 'workbench'


def original_open():
    # Visitors without a session keep using the original workbench, under
    # its teacher token, until an account claims it.
    return accounts.legacy_owner() is None


@app.middleware('http')
async def origin_guard(request: Request, call_next):
    # Guard cross-origin writes. A signed-in account acts only on its own
    # workbench and needs no teacher token there; course reads stay public.
    if request.method in WRITES:
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            from urllib.parse import urlparse
            if urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse(status_code=403, content={'detail': '不允许跨站修改课程'})
    scope = access_scope(request.method, request.url.path)
    if scope != 'open':
        user = current_user(request)
        expected = request.headers.get('x-dirmap-workbench')
        if request.url.path.startswith('/assets/maps/'):
            expected = expected or request.query_params.get('workbench')
        if ((expected is not None and expected != accounts.workbench_scope(user))
                or (user and request.method in WRITES and request.headers.get('origin') and expected is None)):
            return JSONResponse(status_code=409, content={'detail': '登录账号或工作台已变化，请刷新页面后再操作', 'accountChanged': True})
    if scope != 'open' and current_user(request) is None:
        if not await anyio.to_thread.run_sync(original_open):
            return JSONResponse(status_code=401, content={'detail': '请先登录', 'login': True})
        # Students hold the course link, so its groups' positions and the
        # course list are the teacher's alone, as every change is.
        token = os.environ.get('DIRMAP_ADMIN_TOKEN')
        if token and (request.method in WRITES or request.url.path == '/api/releases' or scope == 'owner'):
            if not secrets.compare_digest(request.headers.get('x-dirmap-token', ''), token):
                return JSONResponse(status_code=401, content={'detail': '请输入教师管理口令'})
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if scope != 'open' or request.url.path.startswith(('/api/jobs', '/api/groups', '/api/auth')) or request.url.path.endswith(('/progress', '/groups')):
        response.headers['Cache-Control'] = 'no-store'
    return response


class AccountMiddleware:
    """Bind each API request to the signed-in account's workbench."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith(('/api/', '/assets/maps/')):
            return await self.app(scope, receive, send)
        token = Request(scope).cookies.get(accounts.COOKIE)
        user = await anyio.to_thread.run_sync(accounts.session_user, token) if token else None
        scope['dirmap.user'] = user
        guard = None
        if access_scope(scope['method'], scope['path']) != 'open':
            guard = (lambda: accounts.workspace_matches(user['id'], user['workspace'])) if user else (lambda: accounts.legacy_owner() is None)
        with store.using(user['workspace'] if user else store.LEGACY), store.guarded(guard):
            await self.app(scope, receive, send)


app.add_middleware(AccountMiddleware)


def current_user(request):
    return request.scope.get('dirmap.user')


def account_view(user):
    owner = accounts.legacy_owner()
    return {'user': {key: value for key, value in user.items() if key != 'workspace'} if user else None,
            'original': {'claimed': owner is not None, 'tokenRequired': bool(os.environ.get('DIRMAP_ADMIN_TOKEN')),
                         'hasData': None if owner else accounts.legacy_has_data()}}


def signed_in(request, token, user):
    response = JSONResponse(account_view(user))
    response.set_cookie(accounts.COOKIE, token, max_age=accounts.SESSION_SEC, httponly=True, samesite='lax',
                        secure=request.url.scheme == 'https', path='/')
    return response


def client_address(request):
    return request.client.host if request.client else 'unknown'


@app.get('/api/auth/me')
def me(request: Request):
    return account_view(current_user(request))


@app.post('/api/auth/register')
def register(body: Registration, request: Request):
    token, user = accounts.register(body.username, body.password, client_address(request),
                                    claim_legacy=body.claimOriginal, legacy_token=body.originalToken)
    return signed_in(request, token, user)


@app.post('/api/auth/login')
def login(body: Credentials, request: Request):
    token, user = accounts.login(body.username, body.password, client_address(request))
    return signed_in(request, token, user)


@app.post('/api/auth/logout')
def logout(request: Request):
    accounts.logout(request.cookies.get(accounts.COOKIE))
    response = JSONResponse(account_view(None))
    response.delete_cookie(accounts.COOKIE, path='/')
    return response


@app.post('/api/auth/password')
def change_password(body: PasswordChange, request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, '请先登录')
    accounts.change_password(user, body.oldPassword, body.newPassword, request.cookies.get(accounts.COOKIE, ''))
    return account_view(user)


@app.post('/api/auth/claim-original')
def claim_original(body: ClaimRequest, request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, '请先登录')
    return account_view(accounts.claim_legacy(user, body.originalToken))


@app.get('/api/health')
def health(lng: FiniteFloat | None = None, lat: FiniteFloat | None = None):
    from datetime import date
    from pygeomag import GeoMag
    today = date.today()
    decimal_year = today.year + (today-date(today.year, 1, 1)).days / ((date(today.year+1, 1, 1)-date(today.year, 1, 1)).days)
    # The compass corrects magnetic north at the course's own place.
    frame = Frame(lng, lat) if lng is not None and lat is not None and abs(lng) <= 180 and abs(lat) <= 85 else Frame()
    declination = None
    if 2025 <= decimal_year <= 2030:
        declination = GeoMag().calculate(glat=frame.lat, glon=frame.lng, alt=0, time=decimal_year).d
    deployment = store.read_json(store.ROOT/'deployment.json', {})
    return {'status': 'ok', 'version': '0.1.0', 'frame': frame.as_dict(),
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
    return {'maps': maps, 'legends': legends, 'sites': [sites.public(site) for site in sites.available()],
            'reference': {'crs': 'WGS84', 'name': '深圳大学粤海校区',
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
def reference(site: str = store.DEFAULT_SITE):
    return geojson(sites.reference(site))


@app.get('/api/sites')
def list_sites():
    return [sites.public(site) for site in sites.available()]


@app.post('/api/sites')
def add_site(request: SiteRequest, http_request: Request):
    site = sites.add(request.name, request.bbox)
    return {'site': sites.public(site), 'job': submit_job(http_request, 'site_reference', site['id'], {})}


@app.post('/api/sites/{site_id}/reference')
def refresh_site(site_id: str, http_request: Request):
    if sites.get(site_id).get('builtin'):
        raise ValueError('内置场地的参照随程序发布，无需下载')
    return submit_job(http_request, 'site_reference', site_id, {})


@app.delete('/api/sites/{site_id}')
def remove_site(site_id: str):
    sites.remove(site_id)
    return [sites.public(site) for site in sites.available()]


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
    queue, workspace = request.app.state.jobs, store.current_workspace()
    prior = queue.by_request(request_id, kind, asset_id, payload, workspace)
    if prior:
        return prior
    with store.LOCK:
        # A matching request may have committed while this server process
        # waited for the map lock; replay it before checking its old version.
        prior = queue.by_request(request_id, kind, asset_id, payload, workspace)
        if prior:
            return prior
        if expected_version is not None:
            state = store.get_state(asset_id)
            if not state or state['version'] != expected_version:
                raise HTTPException(409, '地图已更新，请刷新后重试')
        inputs = service.job_inputs(kind, asset_id, payload)
        return queue.submit(kind, asset_id, payload, inputs, request_id, workspace)


@app.post('/api/maps/{asset_id}/process')
def process_map(asset_id: str, request: Request, preserveRegistration: bool = False):
    payload = {'preserveRegistration': True} if preserveRegistration else {}
    return submit_job(request, 'process', asset_id, payload)


@app.post('/api/maps/{asset_id}/terrain')
def refresh_terrain(asset_id: str, request: TerrainRequest, http_request: Request):
    return submit_job(http_request, 'terrain', asset_id, request.model_dump(), request.version)


@app.get('/api/maps/{asset_id}/layer-candidates')
def layer_candidates(asset_id: str):
    state = store.get_state(asset_id)
    if not state or not state.get('registration'):
        raise ValueError('请先完成地图配准，再核对参照中的桥隧位置')
    return layers.reference_candidates(state, service.reference(asset_id, state))


@app.get('/api/jobs')
def recent_jobs(request: Request, assetId: str | None = None):
    return request.app.state.jobs.recent(assetId, workspace=store.current_workspace())


@app.get('/api/jobs/request/{request_id}')
def job_by_request(request_id: str, request: Request):
    job = request.app.state.jobs.by_request(request_id, workspace=store.current_workspace())
    if job is None:
        raise HTTPException(404, '未找到这次提交，请检查任务记录后重新操作')
    return job


@app.get('/api/jobs/{job_id}')
def job(job_id: str, request: Request):
    result = request.app.state.jobs.get(job_id, store.current_workspace())
    if result is None:
        raise HTTPException(404, '任务不存在，请检查任务记录和当前地图')
    return result


@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id: str, request: Request):
    result = request.app.state.jobs.cancel(job_id, store.current_workspace())
    if result is None:
        raise HTTPException(404, '任务不存在，请检查任务记录和当前地图')
    return result


@app.post('/api/maps/upload')
async def upload(file: UploadFile = File(...), siteId: str = store.DEFAULT_SITE):
    if Path(file.filename or '').suffix.lower() not in ('.jpg', '.jpeg', '.png'):
        raise ValueError('请上传 JPEG 或 PNG 地图')
    sites.get(siteId)
    if sum(not asset.get('builtin') for asset in store.catalog()) >= MAX_UPLOADS:
        raise ValueError(f'每个工作台最多导入 {MAX_UPLOADS} 张地图，请先删除不用的地图')
    if shutil.disk_usage(store.DATA).free < MIN_FREE_BYTES:
        raise ValueError('服务器存储空间不足，暂时不能导入新地图，请联系管理员')
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
             'filename': Path(file.filename).name, 'role': 'map', 'sha256': hashlib.sha256(payload).hexdigest(),
             'siteId': siteId}
    with store.LOCK:
        uploads = store.read_json(store.uploads_path(), [])
        uploads.append(asset)
        store.atomic_json(store.uploads_path(), uploads)
    return {k: v for k, v in asset.items() if k != 'path'}


@app.delete('/api/maps/{asset_id}')
def delete_map(asset_id: str):
    # Published courses keep their own copy of routes and the overlay; they
    # report themselves as replaced once their map is gone.
    with store.LOCK:
        if store.get_asset(asset_id).get('builtin'):
            raise ValueError('内置示例地图不能删除')
        uploads = store.read_json(store.uploads_path(), [])
        store.atomic_json(store.uploads_path(), [asset for asset in uploads if asset['id'] != asset_id])
        shutil.rmtree(store.asset_dir(asset_id), ignore_errors=True)
        service.forget_graph(asset_id)
    return {'deleted': asset_id}


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
                fields.update(key for key in ('label', 'pixel', 'approachPixel', 'surfaceId', 'radiusPx', 'role', 'serviceBaseSec', 'servicePerPersonSec')
                              if (key in old or key == 'surfaceId') and control.get(key) != old.get(key))
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
                if edit.get('surfaceId') == 'ground':
                    edit.pop('surfaceId')
                if not edit.get('separateGround'):
                    edit.pop('separateGround', None)
            state['edits'] = data['edits']
        for key in ('surfaces', 'portals'):
            if key in data:
                state[key] = data[key]
        for portal in state.get('portals', []):
            x, y = portal['pixel']
            if not (0 <= x < state['width'] and 0 <= y < state['height']):
                raise ValueError('换层入口超出地图')
        layers.validate(state.get('surfaces', []), state.get('portals', []), state['edits'], state['controls'])
        state.update(version=state['version']+1, reviewed=False, graphHash=None, updatedAt=time.time())
        service.project_controls(state)
        store.save_state(asset_id, state)
        service.forget_graph(asset_id)
        return state


@app.post('/api/maps/{asset_id}/anchors')
def anchors(asset_id: str, request: AnchorRequest, http_request: Request):
    return submit_job(http_request, 'anchors', asset_id, request.model_dump(), request.version)


@app.get('/api/maps/{asset_id}/field-checks')
def list_field_checks(asset_id: str):
    return field_checks.report(asset_id)


@app.post('/api/maps/{asset_id}/field-checks')
def add_field_check(asset_id: str, check: FieldCheck):
    return field_checks.add(asset_id, check.model_dump())


@app.delete('/api/maps/{asset_id}/field-checks/{check_id}')
def remove_field_check(asset_id: str, check_id: str):
    return field_checks.remove(asset_id, check_id)


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


@app.post('/api/plans/choose-team-count')
def choose_team_count(request: TeamCountChoiceRequest, http_request: Request):
    return submit_job(http_request, 'choose_teams', request.assetId, request.model_dump(exclude_none=True), request.version)


@app.post('/api/plans/refine')
def refine_plan(request: RefinePlanRequest, http_request: Request):
    return submit_job(http_request, 'refine_plan', request.assetId, request.model_dump(), request.version)


@app.post('/api/plans/confirm')
def confirm_plan(request: SavedPlanRequest, http_request: Request):
    return submit_job(http_request, 'confirm_plan', request.assetId, request.model_dump(), request.version)


@app.post('/api/releases')
def release(request: ReleaseRequest):
    if any(c not in '0123456789abcdef' for c in request.planId):
        raise ValueError('路线标识无效')
    with store.LOCK:
        state = store.get_state(request.assetId)
        plan = store.read_json(store.asset_dir(request.assetId)/f'plan-{request.planId}.json')
        if not state or not plan or not state['reviewed']:
            raise ValueError('请先核验地图并生成路线')
        selected = set((plan.get('request') or {}).get('controlIds', []))
        controls = {c['id']: c for c in state['controls']}
        if not plan.get('routes') or not selected or any(identifier not in controls or
                controls[identifier]['state'] != 'accepted' or not controls[identifier].get('calibrated') for identifier in selected):
            raise ValueError('所选路线仍有目标未核验，不能发布')
        if any(a.get('automatic') for a in plan.get('approaches', [])):
            raise ValueError('请先核对并保存建议的到访位置，再生成发布路线')
        if state['version'] != plan['version'] or state['graphHash'] != plan['graphHash']:
            raise ValueError('地图或通行状态已变化，请重新生成路线')
        if service.get_graph(state).content_hash != plan['graphHash']:
            raise ValueError('通行算法或数据已更新，请重新生成并核验路线')
        release_id = uuid.uuid4().hex
        site = sites.get(store.site_of(store.get_asset(request.assetId)))
        result = {'id': release_id, 'title': request.title, 'assetId': request.assetId, 'createdAt': time.time(),
                  'siteId': site['id'], 'siteName': site['name'], 'workspace': store.current_workspace(),
                  'plan': plan, 'controls': state['controls'], 'frame': state['frame'], 'extent': state['extent'],
                  'quality': state['quality'], 'review': state['review'], 'version': state['version'],
                  'routingSchema': ROUTING_SCHEMA}
        # Freeze derived raster together with the course. A later source edit
        # never mutates a previously issued student link.
        path = store.DATA/'releases'/release_id
        path.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store.artifact_path(state, 'overlay.png'), path/'overlay.png')
        try:
            store.atomic_json(path/'reference.json', geojson(sites.reference(site['id'])))
        except ValueError:
            pass  # a place drawn only by teacher anchors has no OSM reference
        store.atomic_json(path/'release.json', result)
        # Listing a workbench's courses reads this instead of every package.
        store.atomic_json(path/'meta.json', {key: result[key] for key in ('id', 'workspace', 'assetId', 'title', 'createdAt', 'siteId')})
        return {'id': release_id, 'url': '/?course='+release_id}


def release_owner(path):
    meta = store.read_json(path.parent/'meta.json') or store.read_json(path) or {}
    return meta.get('workspace', store.LEGACY), meta.get('assetId')


@app.get('/api/releases')
def list_releases(request: Request, assetId: str | None = None, limit: int = 20):
    # Course links are the students' access; only their workbench lists them.
    paths = sorted((path for path in (store.DATA/'releases').glob('*/release.json') if re.fullmatch(r'[0-9a-f]{32}', path.parent.name)),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    result = []
    for path in paths:
        if len(result) >= min(50, max(1, limit)):
            break
        workspace, asset = release_owner(path)
        if workspace != store.current_workspace() or (assetId and asset != assetId):
            continue
        release = load_release(path.parent.name)
        teams = progress.summary(release)['teams']
        result.append({'id': release['id'], 'title': release['title'], 'assetId': release['assetId'],
                       'createdAt': release['createdAt'], 'superseded': release['superseded'], 'url': '/?course='+release['id'],
                       'startedAt': progress.started_at(release['id']),
                       'teams': len(teams), 'startedTeams': sum(bool(team['completed']) for team in teams),
                       'finishedTeams': sum(team['finished'] for team in teams),
                       'lastReportAt': max((team['reportedAt'] for team in teams if team['reportedAt']), default=None)})
    return result


def load_release(release_id):
    """A published course, judged against its own workbench's current map."""
    if len(release_id) != 32 or any(c not in '0123456789abcdef' for c in release_id):
        raise HTTPException(404)
    value = store.read_json(store.DATA/'releases'/release_id/'release.json')
    if not value:
        raise HTTPException(404, '课程不存在')
    value.setdefault('workspace', store.LEGACY)
    with store.using(value['workspace']):
        state = store.get_state(value['assetId']) if store.valid_project(value['assetId']) else None
        value['superseded'] = (not state or state['version'] != value['version']
                               or state['graphHash'] != value['plan']['graphHash']
                               or state.get('terrainSchema') != service.TERRAIN_SCHEMA
                               or value.get('routingSchema') != ROUTING_SCHEMA
                               or service.registration_stale(state))
    return value


def owned_release(release_id):
    release = load_release(release_id)
    if release['workspace'] != store.current_workspace():
        raise HTTPException(403, '只有发布这门课程的账户可以操作')
    return release


@app.get('/api/releases/{release_id}')
def get_release(release_id: str):
    return {key: value for key, value in load_release(release_id).items() if key != 'workspace'}


@app.get('/api/releases/{release_id}/status')
def release_status(release_id: str):
    # Phones poll this; the full course is downloaded once when opened.
    release = load_release(release_id)
    return {'id': release['id'], 'superseded': release['superseded'], 'routingSchema': release.get('routingSchema'),
            'startedAt': progress.started_at(release_id), 'serverTime': time.time()}


@app.post('/api/releases/{release_id}/session')
def release_session(release_id: str, request: SessionRequest):
    # Staggered departures count from this moment on every phone.
    release = owned_release(release_id)
    if request.action == 'start' and release['superseded']:
        raise HTTPException(409, '课程已更新，不能为旧课程开始计时')
    progress.set_started(release_id, time.time() if request.action == 'start' else None)
    return release_status(release_id)


@app.get('/api/releases/{release_id}/qr.png')
def course_qr(release_id: str, origin: str):
    # Behind a proxy only the teacher's page knows the public address.
    if not re.fullmatch(r'https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?', origin):
        raise ValueError('课程地址无效')
    load_release(release_id)
    return Response(printing.course_qr(f'{origin}/?course={release_id}'), media_type='image/png',
                    headers={'Cache-Control': 'public, max-age=86400'})


@app.get('/api/releases/{release_id}/route-map')
def route_map(release_id: str, team: str):
    release = load_release(release_id)
    image = printing.route_map(release, team, store.DATA/'releases'/release_id/'overlay.png')
    return Response(image, media_type='image/jpeg', headers={'Cache-Control': 'public, max-age=3600'})


@app.get('/api/releases/{release_id}/progress')
def class_progress(release_id: str):
    return progress.summary(load_release(release_id))


@app.post('/api/releases/{release_id}/progress')
def report_progress(release_id: str, report: ProgressReport):
    release = load_release(release_id)
    if release['superseded']:
        raise HTTPException(409, '课程已更新，请使用教师提供的新链接')
    return progress.record(release, report.model_dump())


@app.post('/api/releases/{release_id}/navigate')
def navigate(release_id: str, request: NavigationRequest):
    release = load_release(release_id)
    if release['superseded']:
        raise HTTPException(409, '课程已更新，请使用教师提供的新课程链接')
    with store.using(release['workspace']):
        return service.navigate_course(release, request.model_dump())


@app.get('/api/releases/{release_id}/groups')
def release_groups(release_id: str):
    # Where the members of this course's groups are, for its own teacher.
    owned_release(release_id)
    return groups.for_release(release_id)


def group_member(request, group_id):
    if not re.fullmatch(r'[0-9a-f]{16}', group_id):
        raise HTTPException(404)
    return request.headers.get('x-dirmap-member', '')


@app.post('/api/groups')
def create_group(body: GroupCreate, request: Request):
    release = load_release(body.releaseId) if body.releaseId else None
    if release and release['superseded']:
        raise HTTPException(409, '课程已更新，请使用教师提供的新链接')
    return groups.create(body.name, client_address(request), body.title, release, body.teamId, body.center)


@app.post('/api/groups/join')
def join_group(body: GroupJoin, request: Request):
    return groups.join(body.code, body.name, client_address(request), body.teamId)


@app.post('/api/groups/{group_id}/sync')
def sync_group(group_id: str, body: GroupSync, request: Request):
    token = group_member(request, group_id)
    return groups.sync(group_id, token, body.sharing, body.position.model_dump() if body.position else None, body.after)


@app.post('/api/groups/{group_id}/messages')
def group_message(group_id: str, body: GroupMessage, request: Request):
    token = group_member(request, group_id)
    return groups.post(group_id, token, body.kind, body.text, body.lnglat, body.after)


@app.post('/api/groups/{group_id}/leave')
def leave_group(group_id: str, request: Request):
    groups.leave(group_id, group_member(request, group_id))
    return {'left': True}


@app.post('/api/groups/{group_id}/manage')
def manage_group(group_id: str, body: GroupManage, request: Request):
    return groups.manage(group_id, group_member(request, group_id), body.action, body.memberId)


@app.get('/api/groups/{group_id}/photos')
def group_photos(group_id: str, request: Request):
    return photos.album(group_id, group_member(request, group_id))


@app.put('/api/groups/{group_id}/photos/{control_id}')
def upload_group_photo(group_id: str, control_id: str, request: Request, file: Annotated[UploadFile, File()]):
    return photos.put(group_id, group_member(request, group_id), control_id,
                      request.headers.get('x-dirmap-photo-revision', ''), file.file,
                      request.headers.get('x-dirmap-photo-upload', ''))


@app.get('/api/groups/{group_id}/photos/{control_id}/image')
def group_photo_image(group_id: str, control_id: str, revision: str, request: Request, thumbnail: bool = False):
    image = photos.picture(group_id, group_member(request, group_id), control_id, revision, thumbnail)
    return Response(image, media_type='image/jpeg', headers={'Content-Encoding': 'identity'})


@app.delete('/api/groups/{group_id}/photos/{control_id}')
def delete_group_photo(group_id: str, control_id: str, request: Request):
    return photos.delete(group_id, group_member(request, group_id), control_id, request.headers.get('x-dirmap-photo-revision', ''))


@app.post('/api/groups/{group_id}/photos/export')
def export_group_photos(group_id: str, body: PhotoExport, request: Request):
    result = photos.export(group_id, group_member(request, group_id), body.format)
    return Response(result['content'], media_type=result['mediaType'], headers={
        'Content-Disposition': f'attachment; filename="{result["filename"]}"', 'Content-Encoding': 'identity',
        'X-DirMap-Photo-Export': result['exportId'], 'X-DirMap-Photo-Count': str(result['count']),
    })


@app.post('/api/groups/{group_id}/photos/finish')
def finish_group_photos(group_id: str, body: PhotoFinish, request: Request):
    return photos.finish(group_id, group_member(request, group_id), body.exportId)


@app.get('/api/releases/{release_id}/reference')
def release_reference(release_id: str):
    # The place's reference as published, so a course works offline and
    # without access to the teacher's workbench.
    load_release(release_id)
    path = store.DATA/'releases'/release_id/'reference.json'
    if not path.exists():
        return geojson(sites.reference(store.DEFAULT_SITE))
    return JSONResponse(store.read_json(path), headers={'Cache-Control': 'public, max-age=86400'})


@app.get('/api/releases/{release_id}/overlay.png')
def release_overlay(release_id: str):
    load_release(release_id)
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
