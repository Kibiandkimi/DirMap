import { activePins, formatCode, memberSummary, mergeMessages, relativeTime } from './group-geo.ts';
import { teamColors, type CampusMap, type MapPerson } from './map';
import { escapeHtml as e, type GroupMember, type GroupMessage, type GroupState, type Pixel } from './types';

const MEMBERSHIP_KEY = 'dirmap-group-v1';
const NAME_KEY = 'dirmap-group-name';
const QUICK_MESSAGES = ['我到了', '等等我', '集合', '我先出发了'];
const SYNC_MS = 3000;
export const memberColor = (index: number) => teamColors[index % teamColors.length];

interface Membership { groupId: string; memberId: string; token: string; code: string; releaseId: string | null; sharing?: boolean }
class GroupRequestError extends Error { constructor(message: string, public status: number) { super(message); } }

async function request<T>(url: string, body?: object, token?: string): Promise<T> {
  const headers: Record<string, string> = {};
  if (body) headers['Content-Type'] = 'application/json';
  if (token) headers['X-DirMap-Member'] = token;
  const response = await fetch(url, { method: body ? 'POST' : 'GET', headers, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(15000) });
  if (!response.ok) {
    let message = `${response.status}`;
    try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch { /* no JSON body */ }
    throw new GroupRequestError(message, response.status);
  }
  return response.json() as Promise<T>;
}

function read<T>(key: string): T | null { try { return JSON.parse(localStorage.getItem(key) ?? 'null'); } catch { return null; } }
function write(key: string, value: unknown) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(value)); } catch { /* this page still works */ } }

export interface GroupOptions {
  root: HTMLElement;
  map: CampusMap;
  notify: (message: string, error?: boolean) => void;
  courseId: string | null;
  teamId: () => string | null;
  /** The page's own location fix while it follows the phone, if any. */
  ownPosition: () => GeolocationPosition | null;
  initialCode?: string | null;
}

/** One phone's membership of a group: its panel, map layer and sync loop. */
export class GroupPanel {
  private membership: Membership | null = read<Membership>(MEMBERSHIP_KEY);
  private state: GroupState | null = null;
  private messages: GroupMessage[] = [];
  private after = 0;
  private watch: number | null = null;
  private fix: GeolocationPosition | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private syncing = false;
  private picking = false;
  private loaded = false;
  private focused = false;
  private alerted = new Set<string>();

  constructor(private options: GroupOptions) {
    options.root.addEventListener('click', event => this.click(event));
    options.root.addEventListener('change', event => {
      if ((event.target as HTMLElement).id === 'group-share') void this.setSharing((event.target as HTMLInputElement).checked);
    });
    options.root.addEventListener('keydown', event => {
      if ((event.target as HTMLElement).id === 'group-text' && event.key === 'Enter') { event.preventDefault(); void this.send('text', this.input('group-text').value); }
      if ((event.target as HTMLElement).id === 'group-code' && event.key === 'Enter') { event.preventDefault(); void this.join(); }
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden && this.membership) void this.sync(); });
    this.renderShell();
    if (this.membership) {
      if (this.membership.sharing) void this.setSharing(true, false);
      void this.sync();
    }
  }

  get active() { return this.membership !== null; }
  get groupId() { return this.membership?.groupId ?? null; }

  private input(id: string) { return this.options.root.querySelector<HTMLInputElement>('#'+id)!; }
  private element(id: string) { return this.options.root.querySelector<HTMLElement>('#'+id); }

  private renderShell() {
    const root = this.options.root;
    if (!this.membership) {
      const name = read<string>(NAME_KEY) ?? '';
      root.innerHTML = `<p class="muted compact">用 6 位临时组队码与同伴共享位置、发消息和标记集合点。位置只在本组内共享，随时可以关闭${this.options.courseId ? '；课程小组的位置也会显示给本课教师' : ''}。</p>
        <label class="field-label">我的名字<input id="group-name" maxlength="20" autocomplete="nickname" value="${e(name)}" placeholder="组员看到的名字"></label>
        <div class="group-join"><input id="group-code" inputmode="numeric" maxlength="7" placeholder="6 位组队码" aria-label="组队码" value="${e(this.options.initialCode ?? '')}"><button class="soft-button" data-group-action="join">加入小组</button></div>
        <button class="primary-button wide" data-group-action="create">创建新小组</button>
        <p id="group-error" class="danger" role="status"></p>`;
      return;
    }
    root.innerHTML = `<div class="group-code-card"><span>组队码</span><strong id="group-code-text">${e(formatCode(this.membership.code))}</strong><button class="text-button" data-group-action="invite">复制邀请链接</button></div>
      <p id="group-meta" class="muted compact">正在连接小组…</p>
      <label class="check-label group-share"><input id="group-share" type="checkbox" ${this.watch !== null ? 'checked' : ''}>共享我的位置</label>
      <p id="group-share-status" class="muted compact"></p>
      <div id="group-help" class="group-help hidden" role="alert"></div>
      <div id="group-members" class="group-members"></div>
      <div class="group-quick">${QUICK_MESSAGES.map(text => `<button class="chip-button" data-group-quick="${e(text)}">${e(text)}</button>`).join('')}</div>
      <div class="group-send"><input id="group-text" maxlength="120" placeholder="发消息给小组" aria-label="小组消息"><button class="soft-button" data-group-action="send">发送</button></div>
      <div class="group-actions"><button class="text-button" data-group-action="pin">在地图上标记集合点</button><button id="group-help-button" class="danger-button" data-group-action="help">求助</button></div>
      <div id="group-messages" class="group-messages" aria-live="polite"></div>
      <div id="group-host" class="group-host"></div>
      <button class="text-button" data-group-action="leave">退出小组</button>`;
    this.update();
  }

  private click(event: Event) {
    const target = event.target as HTMLElement;
    const quick = target.closest<HTMLElement>('[data-group-quick]');
    if (quick) { void this.send('text', quick.dataset.groupQuick!); return; }
    const remove = target.closest<HTMLElement>('[data-group-remove]');
    if (remove) { void this.remove(remove.dataset.groupRemove!); return; }
    const locate = target.closest<HTMLElement>('[data-group-locate]');
    if (locate) {
      const member = this.state?.members.find(m => m.id === locate.dataset.groupLocate);
      if (member?.position) this.options.map.centerOn(member.position.lnglat); else this.options.notify('这位组员还没有共享位置');
      return;
    }
    const pin = target.closest<HTMLElement>('[data-group-pin]');
    if (pin) { const lnglat = JSON.parse(pin.dataset.groupPin!); this.options.map.centerOn(lnglat); return; }
    const action = target.closest<HTMLElement>('[data-group-action]')?.dataset.groupAction;
    if (action === 'create') void this.create();
    else if (action === 'join') void this.join();
    else if (action === 'send') void this.send('text', this.input('group-text').value);
    else if (action === 'invite') void this.invite();
    else if (action === 'pin') { this.picking = true; this.options.notify('在地图上点击集合位置'); }
    else if (action === 'help') void this.help();
    else if (action === 'resolved') void this.send('resolved');
    else if (action === 'leave') void this.leave();
    else if (action === 'lock' || action === 'unlock' || action === 'close') void this.manage(action);
  }

  private error(message: string) { const el = this.element('group-error'); if (el) el.textContent = message; else this.options.notify(message, true); }

  private name() {
    const name = this.input('group-name').value.trim();
    if (!name) throw new Error('请先填写组员看到的名字');
    write(NAME_KEY, name);
    return name;
  }

  private async create() {
    try {
      const fix = this.currentFix();
      const team = this.options.teamId();
      const joined = await request<GroupState & { groupId: string; memberId: string; token: string }>('/api/groups', {
        name: this.name(), releaseId: this.options.courseId, teamId: team, title: team ? `${team}小组` : null, center: fix?.lnglat ?? null });
      this.enter(joined);
    } catch (error) { this.error(error instanceof Error ? error.message : String(error)); }
  }

  private async join() {
    try {
      const code = this.input('group-code').value.replace(/\D/g, '');
      if (code.length !== 6) throw new Error('请输入 6 位组队码');
      const joined = await request<GroupState & { groupId: string; memberId: string; token: string }>('/api/groups/join', {
        code, name: this.name(), teamId: this.options.teamId() });
      this.enter(joined);
    } catch (error) { this.error(error instanceof Error ? error.message : String(error)); }
  }

  private enter(joined: GroupState & { groupId: string; memberId: string; token: string }) {
    this.membership = { groupId: joined.groupId, memberId: joined.memberId, token: joined.token, code: joined.group.code, releaseId: joined.group.releaseId };
    write(MEMBERSHIP_KEY, this.membership);
    // A course group is used from that course's page, which resumes it.
    if (joined.group.releaseId && joined.group.releaseId !== this.options.courseId) {
      location.href = `/?course=${encodeURIComponent(joined.group.releaseId)}`;
      return;
    }
    this.loaded = false; this.messages = []; this.after = 0;
    this.apply(joined);
    this.renderShell();
    this.options.notify(`已加入小组，组队码 ${formatCode(joined.group.code)}`);
    this.schedule(SYNC_MS);
  }

  private forget(message?: string) {
    this.stopSharing();
    write(MEMBERSHIP_KEY, null);
    this.membership = null; this.state = null; this.messages = []; this.after = 0; this.loaded = false;
    if (this.timer) clearTimeout(this.timer);
    this.options.map.setPeople([]);
    this.renderShell();
    if (message) this.options.notify(message, true);
  }

  private schedule(delay: number) {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.sync(), delay);
  }

  /** The freshest location this phone knows, if it is still useful. */
  private currentFix(): { lnglat: Pixel; accuracyM: number; headingDeg: number | null; timestampMs: number } | null {
    const candidates = [this.fix, this.options.ownPosition()].filter((p): p is GeolocationPosition => !!p && Date.now() - p.timestamp < 60000);
    const best = candidates.sort((a, b) => b.timestamp - a.timestamp)[0];
    if (!best) return null;
    const heading = best.coords.heading;
    return { lnglat: [best.coords.longitude, best.coords.latitude], accuracyM: best.coords.accuracy,
      headingDeg: heading !== null && Number.isFinite(heading) ? heading : null, timestampMs: best.timestamp };
  }

  async sync() {
    const membership = this.membership;
    if (!membership || this.syncing) return;
    if (document.hidden) { this.schedule(SYNC_MS * 3); return; }
    this.syncing = true;
    try {
      const sharing = this.watch !== null;
      const state = await request<GroupState>(`/api/groups/${membership.groupId}/sync`,
        { sharing, position: sharing ? this.currentFix() : null, after: this.after }, membership.token);
      if (this.membership !== membership) return;
      this.failures = 0;
      this.apply(state);
    } catch (error) {
      if (error instanceof GroupRequestError && error.status === 410) { this.forget(error.message); return; }
      this.failures++;
      const meta = this.element('group-meta'); if (meta) meta.textContent = '连接中断，正在重试…';
    } finally {
      this.syncing = false;
      if (this.membership) this.schedule(this.failures ? Math.min(15000, SYNC_MS * 2 ** this.failures) : SYNC_MS);
    }
  }

  private apply(state: GroupState) {
    const incoming = state.messages.filter(m => m.seq > this.after);
    this.state = state;
    this.messages = mergeMessages(this.messages, state.messages);
    this.after = Math.max(this.after, ...state.messages.map(m => m.seq));
    if (this.loaded) {
      for (const message of incoming) {
        if (message.memberId === state.me) continue;
        const text = message.kind === 'pin' ? `标记了集合点：${message.text ?? '集合点'}` : message.kind === 'help' ? '请求帮助！' : message.kind === 'resolved' ? '已解除求助' : message.text;
        this.options.notify(`${message.name ?? '组员'}：${text}`, message.kind === 'help');
      }
    }
    for (const member of state.members) {
      if (member.helpAt && member.id !== state.me && !this.alerted.has(member.id+member.helpAt)) {
        this.alerted.add(member.id+member.helpAt);
        if (this.loaded) navigator.vibrate?.([300, 120, 300]);
      }
    }
    this.loaded = true;
    // Without a course the map has nothing else to show: go to the group.
    if (!this.focused && !this.options.courseId) {
      const where = state.members.find(m => m.position)?.position?.lnglat ?? state.group.center;
      if (where) { this.options.map.centerOn(where); this.focused = true; }
    }
    this.update();
  }

  private update() {
    const state = this.state;
    if (!state || !this.membership) return;
    const now = state.serverTime, me = state.members.find(m => m.id === state.me);
    const mine = this.currentFix()?.lnglat ?? null;
    const meta = this.element('group-meta');
    if (meta) meta.textContent = `${state.group.title} · ${state.members.length} 人 · ${state.group.locked ? '已锁定，不接受新成员' : '可用组队码加入'} · 有效至 ${new Date(state.group.expiresAt * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
    const status = this.element('group-share-status');
    if (status) status.textContent = this.watch === null ? '未共享：组员看不到你的位置。' : mine ? `正在共享 · 精度 ±${Math.round(this.currentFix()!.accuracyM)} m` : '正在获取定位…';
    const helpers = state.members.filter(m => m.helpAt);
    const help = this.element('group-help');
    if (help) {
      help.classList.toggle('hidden', !helpers.length);
      help.innerHTML = helpers.map(m => m.id === state.me
        ? `<p><strong>你已发出求助</strong> · ${relativeTime(now - m.helpAt!)}<button class="soft-button" data-group-action="resolved">已解决</button></p>`
        : `<p><strong>${e(m.name)} 请求帮助</strong> · ${relativeTime(now - m.helpAt!)}${m.position ? `<button class="text-button" data-group-locate="${e(m.id)}">查看位置</button>` : '（未共享位置）'}</p>`).join('');
    }
    const helpButton = this.element('group-help-button');
    if (helpButton) helpButton.classList.toggle('hidden', !!me?.helpAt);
    const members = this.element('group-members');
    if (members) members.innerHTML = state.members.map(m => `<div class="group-member ${m.online ? '' : 'offline'}"><i style="background:${memberColor(m.color)}"></i><button class="group-member-name" data-group-locate="${e(m.id)}">${e(m.name)}${m.id === state.me ? '（我）' : ''}${m.host ? ' · 组长' : ''}${m.teamId ? `<small>${e(m.teamId)}</small>` : ''}</button><small>${m.id === state.me ? (m.sharing ? '正在共享' : '未共享') : e(memberSummary(m, mine, now))}</small>${me?.host && m.id !== state.me ? `<button class="text-button" data-group-remove="${e(m.id)}" aria-label="移出 ${e(m.name)}">移出</button>` : ''}</div>`).join('');
    const box = this.element('group-messages');
    if (box) {
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      box.innerHTML = this.messages.slice(-30).map(m => `<p class="group-message ${m.kind}"><b>${e(m.name ?? '组员')}</b> ${m.kind === 'pin' && m.lnglat ? `<button class="text-button" data-group-pin="${e(JSON.stringify(m.lnglat))}">⚑ ${e(m.text ?? '集合点')}</button>` : m.kind === 'help' ? '请求帮助' : m.kind === 'resolved' ? '已解除求助' : e(m.text)}<small>${new Date(m.at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</small></p>`).join('') || '<p class="muted compact">还没有消息。</p>';
      if (atBottom) box.scrollTop = box.scrollHeight;
    }
    const host = this.element('group-host');
    if (host) host.innerHTML = me?.host ? `<button class="text-button" data-group-action="${state.group.locked ? 'unlock' : 'lock'}">${state.group.locked ? '允许新成员加入' : '锁定小组'}</button><button class="text-button" data-group-action="close">解散小组</button>` : '';
    this.draw();
  }

  /** Members with a fix and recent pins; my own dot only if the page shows none. */
  draw() {
    const state = this.state;
    if (!state) return;
    const now = state.serverTime, own = this.options.ownPosition();
    const people: MapPerson[] = state.members.filter(m => m.position && (m.id !== state.me || !own)).map(m => ({
      id: m.id, lnglat: m.position!.lnglat, label: m.name, color: memberColor(m.color), accuracyM: m.position!.accuracyM,
      help: !!m.helpAt, self: m.id === state.me, stale: now - m.position!.at > 120 }));
    this.options.map.setPeople(people, activePins(this.messages, now).map(m => ({ lnglat: m.lnglat!, label: m.text ?? '集合点' })));
  }

  /** A map tap while choosing a meeting point; true when it was used. */
  pick(lnglat: Pixel): boolean {
    if (!this.picking) return false;
    this.picking = false;
    const label = prompt('集合点名称', '集合点');
    if (label !== null) void this.send('pin', label || '集合点', lnglat);
    return true;
  }

  private async send(kind: GroupMessage['kind'], text?: string, lnglat?: Pixel) {
    const membership = this.membership;
    if (!membership) return;
    if (kind === 'text' && !text?.trim()) return;
    try {
      const state = await request<GroupState>(`/api/groups/${membership.groupId}/messages`, { kind, text: text ?? null, lnglat: lnglat ?? null, after: this.after }, membership.token);
      if (kind === 'text') this.input('group-text').value = '';
      this.apply(state);
    } catch (error) {
      if (error instanceof GroupRequestError && error.status === 410) this.forget(error.message);
      else this.options.notify(error instanceof Error ? error.message : String(error), true);
    }
  }

  private async help() {
    if (!confirm('向小组发出求助？为便于组员找到你，会同时开启位置共享。')) return;
    if (this.watch === null) await this.setSharing(true);
    await this.send('help');
  }

  private async setSharing(on: boolean, announce = true) {
    if (!this.membership) return;
    if (on && this.watch === null) {
      if (!('geolocation' in navigator)) { this.options.notify('此浏览器不能定位', true); return; }
      this.watch = navigator.geolocation.watchPosition(position => { this.fix = position; this.update(); }, error => {
        this.options.notify(error.code === error.PERMISSION_DENIED ? '没有定位权限，组员看不到你的位置' : '暂时无法定位，稍后自动重试', true);
        if (error.code === error.PERMISSION_DENIED) this.stopSharing();
      }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
      if (announce) this.options.notify('已开始共享位置，关闭开关即停止');
    } else if (!on) this.stopSharing();
    this.membership.sharing = this.watch !== null;
    write(MEMBERSHIP_KEY, this.membership);
    const box = this.element('group-share') as HTMLInputElement | null; if (box) box.checked = this.watch !== null;
    void this.sync();
  }

  private stopSharing() {
    if (this.watch !== null) navigator.geolocation.clearWatch(this.watch);
    this.watch = null; this.fix = null;
    if (this.membership) { this.membership.sharing = false; write(MEMBERSHIP_KEY, this.membership); }
    const box = this.element('group-share') as HTMLInputElement | null; if (box) box.checked = false;
  }

  private async invite() {
    if (!this.membership) return;
    const link = `${location.origin}/?${this.membership.releaseId ? `course=${this.membership.releaseId}&` : ''}group=${this.membership.code}`;
    try { await navigator.clipboard.writeText(`加入我的定向小组，组队码 ${formatCode(this.membership.code)}：${link}`); this.options.notify('邀请链接已复制'); }
    catch { prompt('复制邀请链接', link); }
  }

  private async remove(memberId: string) {
    const member = this.state?.members.find(m => m.id === memberId);
    if (!member || !confirm(`把 ${member.name} 移出小组？`)) return;
    await this.manage('remove', memberId);
  }

  private async manage(action: 'lock' | 'unlock' | 'close' | 'remove', memberId?: string) {
    const membership = this.membership;
    if (!membership) return;
    if (action === 'close' && !confirm('解散后所有组员退出，位置和消息随之删除。确定吗？')) return;
    try {
      const result = await request<GroupState | { closed: true }>(`/api/groups/${membership.groupId}/manage`, { action, memberId: memberId ?? null }, membership.token);
      if ('closed' in result) { this.forget(); this.options.notify('小组已解散'); return; }
      this.apply(result);
    } catch (error) {
      if (error instanceof GroupRequestError && error.status === 410) this.forget(error.message);
      else this.options.notify(error instanceof Error ? error.message : String(error), true);
    }
  }

  private async leave() {
    const membership = this.membership;
    if (!membership || !confirm('退出后你的位置将从小组中删除。确定退出吗？')) return;
    try { await request(`/api/groups/${membership.groupId}/leave`, {}, membership.token); }
    catch { /* leaving locally still stops sharing; the group expires on its own */ }
    this.forget();
    this.options.notify('已退出小组');
  }
}

type OverviewGroup = { id: string; code: string; title: string; locked: boolean; members: GroupMember[]; recent: GroupMessage[] };

/** A course teacher's read-only view of every group opened from the course. */
export class GroupOverview {
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(private releaseId: string, private root: HTMLElement, private map: CampusMap) {}

  start() { void this.refresh(); this.timer = setInterval(() => { if (!document.hidden) void this.refresh(); }, 5000); }
  stop() { if (this.timer) clearInterval(this.timer); this.timer = null; this.map.setPeople([]); }

  private async refresh() {
    let data: { groups: OverviewGroup[]; serverTime: number };
    try { data = await request(`/api/releases/${this.releaseId}/groups`); }
    catch (error) {
      this.root.innerHTML = `<p class="muted compact">${error instanceof GroupRequestError && [401, 403].includes(error.status) ? '仅发布本课的教师登录后可查看各小组位置。' : '暂时无法读取小组位置，稍后自动重试。'}</p>`;
      if (error instanceof GroupRequestError && [401, 403].includes(error.status)) this.stop();
      return;
    }
    const now = data.serverTime;
    const people: MapPerson[] = [];
    data.groups.forEach((group, index) => {
      for (const member of group.members) if (member.position) people.push({ id: group.id+member.id, lnglat: member.position.lnglat,
        label: `${member.name}·${group.code.slice(-3)}`, color: memberColor(index), accuracyM: member.position.accuracyM,
        help: !!member.helpAt, stale: now - member.position.at > 120 });
    });
    const pins = data.groups.flatMap(group => activePins([...group.recent].reverse(), now).map(m => ({ lnglat: m.lnglat!, label: `${m.text ?? '集合点'}·${group.code.slice(-3)}` })));
    this.map.setPeople(people, pins);
    this.root.innerHTML = data.groups.length ? data.groups.map((group, index) => {
      const helpers = group.members.filter(m => m.helpAt);
      return `<div class="overview-group ${helpers.length ? 'help' : ''}"><strong><i style="background:${memberColor(index)}"></i>${e(group.title)} · ${e(formatCode(group.code))}</strong>${helpers.length ? `<p class="danger">求助：${helpers.map(m => e(m.name)).join('、')}</p>` : ''}<small>${group.members.map(m => `${e(m.name)}${m.position ? `（${relativeTime(now - m.position.at)}）` : m.online ? '（未共享）' : '（离线）'}`).join('、')}</small></div>`;
    }).join('') : '<p class="muted compact">还没有学生从本课创建小组。学生在课程页的“组队与位置共享”中创建或加入。</p>';
  }
}
