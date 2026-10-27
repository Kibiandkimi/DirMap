import { localScales, type GroupMember, type GroupMessage, type Pixel } from './types.ts';

/** East/north metres from a to b on the ellipsoid, as the server measures them. */
function offset(a: Pixel, b: Pixel): Pixel {
  const scale = localScales([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
  return [(b[0] - a[0]) * scale[0], (b[1] - a[1]) * scale[1]];
}

/** Distance in metres between two WGS84 [lng, lat] points within a place. */
export function metresBetween(a: Pixel, b: Pixel): number {
  return Math.hypot(...offset(a, b));
}

/** Bearing from a to b, degrees clockwise from true north. */
export function bearingDeg(a: Pixel, b: Pixel): number {
  const [east, north] = offset(a, b);
  return (Math.atan2(east, north) * 180 / Math.PI + 360) % 360;
}

const DIRECTIONS = ['北', '东北', '东', '东南', '南', '西南', '西', '西北'];
export function directionName(degrees: number): string {
  return DIRECTIONS[Math.round((((degrees % 360) + 360) % 360) / 45) % 8];
}

export function relativeTime(seconds: number): string {
  if (seconds < 10) return '刚刚';
  if (seconds < 60) return `${Math.floor(seconds)} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}

export const formatCode = (code: string) => code.length === 6 ? `${code.slice(0, 3)} ${code.slice(3)}` : code;

/** Messages by sequence number, without repeats, newest `keep` retained. */
export function mergeMessages(existing: GroupMessage[], incoming: GroupMessage[], keep = 50): GroupMessage[] {
  const bySeq = new Map(existing.map(m => [m.seq, m]));
  for (const message of incoming) bySeq.set(message.seq, message);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq).slice(-keep);
}

/** The newest meeting pins still worth showing on the map. */
export function activePins(messages: GroupMessage[], now: number, maxAgeSec = 3600, limit = 3): GroupMessage[] {
  return messages.filter(m => m.kind === 'pin' && m.lnglat && now - m.at <= maxAgeSec).slice(-limit);
}

/** How a member appears to me: distance and direction when both are located. */
export function memberSummary(member: GroupMember, mine: Pixel | null, now: number): string {
  if (!member.sharing || !member.position) return member.online ? '未共享位置' : `离线 · ${relativeTime(now - member.seenAt)}`;
  const age = relativeTime(Math.max(0, now - member.position.at));
  if (!mine) return `已共享 · ${age}`;
  const metres = metresBetween(mine, member.position.lnglat);
  const where = metres < 15 ? '就在附近' : `${directionName(bearingDeg(mine, member.position.lnglat))} ${metres >= 1000 ? `${(metres / 1000).toFixed(2)} km` : `${Math.round(metres)} m`}`;
  return `${where} · ${age}`;
}
