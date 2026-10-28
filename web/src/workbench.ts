import type { AccountState, MapState } from './types';

const CHANNEL = 'dirmap-account-change-v1';
let current: string | null = null;

export function accountScope(account: AccountState): string {
  return account.user ? `${account.user.id}:${account.user.ownsOriginalWorkbench ? 'original' : 'personal'}` : 'original-anonymous';
}

export function setWorkbench(account: AccountState) { current = accountScope(account); }
export function workbenchScope() { return current; }
export function workbenchKey(name: string) {
  if (current === null) throw new Error('工作台尚未初始化');
  return `dirmap-workbench-v2:${current}:${name}`;
}

/** This query partitions private image caches; authorization still uses the session. */
export function mapAssetUrl(state: MapState, name: string, versioned = true) {
  const query = new URLSearchParams();
  if (versioned) query.set('v', String(state.version));
  if (current !== null) query.set('workbench', current);
  return `/assets/maps/${encodeURIComponent(state.assetId)}/${name}?${query}`;
}

export function announceAccount(account: AccountState) {
  const message = { scope: accountScope(account), nonce: `${Date.now()}:${Math.random()}` };
  if (message.scope !== current) { try { sessionStorage.removeItem('dirmap-admin-token'); } catch { /* storage disabled */ } }
  try { localStorage.setItem(CHANNEL, JSON.stringify(message)); } catch { /* storage may be disabled */ }
  try { const channel = new BroadcastChannel(CHANNEL); channel.postMessage(message); channel.close(); } catch { /* use storage events instead */ }
}

export function watchAccountChanges(reload: () => void) {
  let changing = false;
  const receive = (value: unknown) => {
    const scope = (value as { scope?: unknown } | null)?.scope;
    if (typeof scope === 'string' && scope !== current && !changing) {
      changing = true;
      try { sessionStorage.removeItem('dirmap-admin-token'); } catch { /* storage disabled */ }
      reload();
    }
  };
  window.addEventListener('storage', event => {
    if (event.key === CHANNEL && event.newValue) { try { receive(JSON.parse(event.newValue)); } catch { /* another client version */ } }
  });
  try {
    const channel = new BroadcastChannel(CHANNEL);
    channel.onmessage = event => receive(event.data);
    window.addEventListener('pagehide', () => channel.close(), { once: true });
  } catch { /* storage events cover browsers without BroadcastChannel */ }
}
