/** Unsent photos belong to this group membership on this device. */
export interface PhotoDraft {
  key: string; localId: string; controlId: string; revision: string; blob: Blob;
  createdAt: number; expiresAt: number; error?: string; conflict?: boolean; retryRequired?: boolean;
}
export const draftPrefix = (groupId: string, memberId: string) => `${groupId}:${memberId}:`;

async function database() {
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('dirmap-photo-drafts', 1);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore('drafts', { keyPath: 'key' });
      store.createIndex('expiresAt', 'expiresAt');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function transaction<T>(run: (store: IDBObjectStore, result: (value: T) => void) => void): Promise<T> {
  const db = await database();
  return new Promise<T>((resolve, reject) => {
    let value: T;
    const tx = db.transaction('drafts', 'readwrite');
    tx.oncomplete = () => { db.close(); resolve(value); };
    tx.onabort = tx.onerror = () => { db.close(); reject(tx.error ?? new Error('无法暂存照片，请检查设备存储空间')); };
    run(tx.objectStore('drafts'), result => { value = result; });
  });
}

export function loadDrafts(prefix: string) {
  return transaction<PhotoDraft[]>((store, result) => {
    const expired = store.index('expiresAt').openCursor(IDBKeyRange.upperBound(Date.now()));
    expired.onsuccess = () => { const cursor = expired.result; if (cursor) { cursor.delete(); cursor.continue(); } };
    const request = store.getAll(IDBKeyRange.bound(prefix, prefix + '\uffff'));
    request.onsuccess = () => result((request.result as PhotoDraft[]).filter(d => d.expiresAt > Date.now()));
  });
}
export function saveDraft(draft: PhotoDraft) {
  return transaction<void>(store => { store.put(draft); });
}
export function removeDraft(draft: PhotoDraft) {
  return transaction<void>(store => {
    const request = store.get(draft.key);
    request.onsuccess = () => { if (request.result?.localId === draft.localId) store.delete(draft.key); };
  });
}
export async function clearDrafts(prefix: string) {
  return transaction<void>(store => { store.delete(IDBKeyRange.bound(prefix, prefix + '\uffff')); });
}

export async function preparePhoto(file: File): Promise<Blob> {
  if (file.size > 20 * 1024 ** 2) throw new Error('照片最大 20 MiB，请选择较小的照片');
  const url = URL.createObjectURL(file), image = new Image();
  try {
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve(); image.onerror = () => reject(new Error('无法读取照片，请使用 JPEG、PNG 或 WebP 图片'));
      image.src = url;
    });
    const ratio = Math.min(1, 1600 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * ratio)); canvas.height = Math.max(1, Math.round(image.naturalHeight * ratio));
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('浏览器暂时无法处理照片，请重新打开页面');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    // Drawing uses the browser's EXIF orientation; re-encoding strips GPS and
    // keeps offline drafts and network uploads small. The server revalidates.
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('无法压缩照片，请重试')), 'image/jpeg', .86));
  } finally { URL.revokeObjectURL(url); image.src = ''; }
}
