import { escapeHtml as e, type AccountState } from './types';
import { announceAccount } from './workbench';

export async function fetchAccount(): Promise<AccountState | null> {
  try { const response = await fetch('/api/auth/me', { signal: AbortSignal.timeout(15000) }); return response.ok ? await response.json() : null; }
  catch { return null; }
}

async function post(url: string, body: object): Promise<AccountState> {
  const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请检查用户名和密码');
  announceAccount(data as AccountState);
  return data as AccountState;
}

function dialog(title: string, body: string): HTMLDialogElement {
  const element = document.createElement('dialog');
  element.className = 'info-dialog account-dialog';
  element.innerHTML = `<div class="dialog-heading"><h2>${e(title)}</h2><button class="icon-button" data-close aria-label="关闭">×</button></div>${body}`;
  document.body.append(element);
  element.querySelector<HTMLButtonElement>('[data-close]')!.onclick = () => element.close();
  element.addEventListener('close', () => element.remove(), { once: true });
  element.showModal();
  return element;
}

const claimable = (state: AccountState) => !state.original.claimed && !!state.original.hasData;

/** Sign in or create an account; the page reloads into that account's workbench. */
export function openSignIn(state: AccountState, mode: 'login' | 'register' = 'login') {
  const box = dialog('登录 DirMap', `<div class="account-tabs" role="tablist"><button type="button" data-account-mode="login">登录</button><button type="button" data-account-mode="register">注册新账户</button></div>
    <form id="account-form" class="account-form">
      <label class="field-label">用户名<input id="account-name" autocomplete="username" required minlength="2" maxlength="32"></label>
      <label class="field-label">密码<input id="account-password" type="password" autocomplete="current-password" required minlength="8" maxlength="128"></label>
      <label class="field-label register-only">再次输入密码<input id="account-confirm" type="password" autocomplete="new-password" maxlength="128"></label>
      ${claimable(state) ? `<details class="register-only claim-original"><summary>我是服务器原工作台的教师</summary><label class="check-label"><input id="account-claim" type="checkbox">把原有的地图、路线和课程归入这个账户</label>${state.original.tokenRequired ? '<label class="field-label">原教师口令<input id="account-token" type="password" autocomplete="off"></label>' : ''}<p class="muted compact">只能认领一次。认领后，未登录的访客不能再打开原工作台。</p></details>` : ''}
      <p class="muted compact register-only">每个账户有独立的工作台：在自己的工作台中导入地图、识别、生成路线和发布课程都不需要教师口令。学生用课程链接或组队码即可，无需注册。</p>
      <p id="account-error" class="danger" role="status"></p>
      <button class="primary-button wide" type="submit" id="account-submit">登录</button>
    </form>`);
  const form = box.querySelector<HTMLFormElement>('#account-form')!;
  const field = (id: string) => box.querySelector<HTMLInputElement>('#'+id);
  const setMode = (next: 'login' | 'register') => {
    mode = next;
    box.querySelectorAll<HTMLElement>('[data-account-mode]').forEach(tab => tab.classList.toggle('active', tab.dataset.accountMode === mode));
    box.querySelectorAll<HTMLElement>('.register-only').forEach(el => el.classList.toggle('hidden', mode !== 'register'));
    field('account-password')!.autocomplete = mode === 'register' ? 'new-password' : 'current-password';
    box.querySelector('#account-submit')!.textContent = mode === 'register' ? '注册并进入我的工作台' : '登录';
    box.querySelector('.dialog-heading h2')!.textContent = mode === 'register' ? '注册 DirMap 账户' : '登录 DirMap';
    box.querySelector('#account-error')!.textContent = '';
  };
  box.querySelectorAll<HTMLElement>('[data-account-mode]').forEach(tab => tab.onclick = () => setMode(tab.dataset.accountMode as 'login' | 'register'));
  setMode(mode);
  form.onsubmit = async event => {
    event.preventDefault();
    const submit = box.querySelector<HTMLButtonElement>('#account-submit')!;
    submit.disabled = true;
    try {
      const username = field('account-name')!.value.trim(), password = field('account-password')!.value;
      if (mode === 'register') {
        if (password !== field('account-confirm')!.value) throw new Error('两次输入的密码不一致');
        await post('/api/auth/register', { username, password, claimOriginal: !!field('account-claim')?.checked, originalToken: field('account-token')?.value || null });
      } else await post('/api/auth/login', { username, password });
      location.reload();
    } catch (error) {
      box.querySelector('#account-error')!.textContent = error instanceof Error ? error.message : String(error);
      submit.disabled = false;
    }
  };
}

/** The signed-in account: password, claiming the original workbench, sign out. */
export function openAccount(state: AccountState, notify: (message: string, error?: boolean) => void) {
  const user = state.user!;
  const box = dialog('我的账户', `<p><strong>${e(user.username)}</strong>${user.ownsOriginalWorkbench ? ' · 原工作台' : ' · 独立工作台'}</p>
    <p class="muted compact">工作台中的地图、计算记录和已发布课程只属于这个账户。</p>
    <details><summary>修改密码</summary><form id="password-form" class="account-form"><label class="field-label">原密码<input id="old-password" type="password" autocomplete="current-password" required></label><label class="field-label">新密码<input id="new-password" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label><button class="soft-button wide" type="submit">保存新密码</button><p class="muted compact">保存后，其他设备上的登录将退出。</p></form></details>
    ${claimable(state) && !user.ownsOriginalWorkbench ? `<details><summary>认领服务器原工作台</summary><p class="muted compact">把原有的地图、路线和课程归入本账户，本账户新建的空工作台随之替换。只适用于尚未修改地图、计算路线或留下处理记录的账户。</p>${state.original.tokenRequired ? '<label class="field-label">原教师口令<input id="claim-token" type="password" autocomplete="off"></label>' : ''}<button id="claim-original" class="soft-button wide">认领原工作台</button></details>` : ''}
    <p id="account-error" class="danger" role="status"></p>
    <button id="sign-out" class="text-button wide">退出登录</button>`);
  const error = (message: string) => { box.querySelector('#account-error')!.textContent = message; };
  box.querySelector<HTMLFormElement>('#password-form')!.onsubmit = async event => {
    event.preventDefault();
    try {
      await post('/api/auth/password', { oldPassword: box.querySelector<HTMLInputElement>('#old-password')!.value, newPassword: box.querySelector<HTMLInputElement>('#new-password')!.value });
      box.close(); notify('密码已更新');
    } catch (failure) { error(failure instanceof Error ? failure.message : String(failure)); }
  };
  const claim = box.querySelector<HTMLButtonElement>('#claim-original');
  if (claim) claim.onclick = async () => {
    if (!confirm('认领后，本账户将使用原工作台，访客不能再匿名打开它。确定认领吗？')) return;
    try { await post('/api/auth/claim-original', { originalToken: box.querySelector<HTMLInputElement>('#claim-token')?.value || null }); location.reload(); }
    catch (failure) { error(failure instanceof Error ? failure.message : String(failure)); }
  };
  box.querySelector<HTMLButtonElement>('#sign-out')!.onclick = async () => {
    try { await post('/api/auth/logout', {}); location.reload(); }
    catch (failure) { error(failure instanceof Error ? failure.message : String(failure)); }
  };
}

/** Header control: who is signed in, or a way to sign in. */
export function renderAccountChip(container: HTMLElement, state: AccountState | null, notify: (message: string, error?: boolean) => void) {
  if (!state) { container.innerHTML = ''; return; }
  container.innerHTML = state.user
    ? `<button class="account-chip" id="account-open" title="账户与工作台">${e(state.user.username)}</button>`
    : '<button class="account-chip" id="account-open">登录 / 注册</button>';
  container.querySelector<HTMLButtonElement>('#account-open')!.onclick = () => state.user ? openAccount(state, notify) : openSignIn(state);
}

/** In place of the workbench when it needs an account. */
export function signInGate(state: AccountState) {
  return `<section class="login-gate"><p class="eyebrow">ORIENTEERING CLASSROOM</p><h1>登录后使用<br>课程工作台<span>。</span></h1>
    <p>每位老师和同学都有自己的工作台：导入地图照片、自动识别配准、生成路线并发布课程，互不影响，也不需要教师口令。</p>
    <div class="login-actions"><button class="primary-button" data-sign-in="login">登录</button><button class="soft-button" data-sign-in="register">注册新账户</button></div>
    <div class="quality-card"><strong>参加课程或小组？</strong><p class="muted compact">课程链接和组队码都不需要账户。输入同伴给你的 6 位组队码即可共享位置。</p><a class="text-link" href="/?group=">输入组队码 →</a></div>
    <p><a class="text-link" href="/elevation">查看共享地形（无需登录）↗</a></p>
    ${claimable(state) ? '<p class="muted compact">服务器上还有原工作台的数据。原教师注册时可以认领它。</p>' : ''}</section>`;
}
