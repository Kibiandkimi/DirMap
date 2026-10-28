import { escapeHtml as e } from './types';
import { clearDrafts, draftPrefix, loadDrafts, preparePhoto, removeDraft, saveDraft, type PhotoDraft } from './photo-drafts';

interface Membership { groupId: string; memberId: string; token: string; releaseId: string | null }
interface Photo { revision: string; width: number; height: number; bytes: number; author: string; uploadedAt: number }
interface Album {
  groupId: string; releaseId: string | null; title: string | null; groupTitle: string; expiresAt: number;
  revision: string; count: number; controls: { id: string; label: string; photo: Photo | null }[];
}
class PhotoError extends Error { constructor(message: string, public status: number) { super(message); } }

/** Shared originals live on the server. Only pending uploads live on-device. */
export class GroupPhotoAlbum {
  private album: Album | null = null;
  private drafts: PhotoDraft[] = [];
  private abort = new AbortController();
  private dialog: HTMLDialogElement | null = null;
  private exportDialog: HTMLDialogElement | null = null;
  private urls = new Map<string, string>();
  private loadingImages = new Set<string>();
  private refreshing = false;
  private uploading = false;
  private uploadingControl: string | null = null;
  private preparing = false;
  private destroyed = false;
  private focusControl: string | null = null;
  private retry: ReturnType<typeof setTimeout> | null = null;
  private status = '';
  private cacheKey: string;
  private prefix: string;
  private ready: Promise<void>;

  constructor(private root: HTMLElement, private member: Membership, private notify: (message: string, error?: boolean) => void) {
    this.prefix = draftPrefix(member.groupId, member.memberId);
    this.cacheKey = 'dirmap-album:' + this.prefix;
    try {
      const cached = JSON.parse(localStorage.getItem(this.cacheKey) ?? 'null') as Album | null;
      if (cached?.groupId === member.groupId && cached.expiresAt * 1000 > Date.now()) this.album = cached;
    } catch { /* no cached catalogue */ }
    this.renderSummary();
    this.ready = loadDrafts(this.prefix).then(drafts => { if (!this.destroyed) { this.drafts = drafts; this.render(); } }).catch(() => {
      this.status = '无法读取本机待上传照片，请检查浏览器存储权限。'; this.renderSummary();
    });
    void this.ready.then(() => this.refresh());
    window.addEventListener('online', () => { void this.refresh(); void this.flush(); }, { signal: this.abort.signal });
  }
  private endpoint(suffix = '') { return `/api/groups/${this.member.groupId}/photos${suffix}`; }
  private async request(suffix: string, options: RequestInit = {}, timeout = 20000) {
    const headers = new Headers(options.headers); headers.set('X-DirMap-Member', this.member.token);
    const response = await fetch(this.endpoint(suffix), { ...options, headers,
      signal: AbortSignal.any([this.abort.signal, AbortSignal.timeout(timeout), ...(options.signal ? [options.signal] : [])]) });
    if (!response.ok) {
      let message = `操作失败（${response.status}），请重试`;
      try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch { /* interrupted response */ }
      throw new PhotoError(message, response.status);
    }
    return response;
  }
  private async json<T>(suffix: string, body: object): Promise<T> {
    return (await this.request(suffix, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }, 120000)).json();
  }
  syncHint(revision?: string) {
    if (revision && revision !== this.album?.revision) void this.refresh();
  }
  private apply(album: Album) {
    if (this.destroyed) return;
    this.album = album;
    try { localStorage.setItem(this.cacheKey, JSON.stringify(album)); } catch { /* online use is still available */ }
    const keep = new Set([...album.controls.flatMap(c => c.photo ? [c.photo.revision] : []), ...this.drafts.map(d => d.localId)]);
    for (const [key, url] of this.urls) if (!keep.has(key)) { URL.revokeObjectURL(url); this.urls.delete(key); }
    this.render();
  }
  async refresh() {
    if (this.destroyed || this.refreshing || !this.member.releaseId) return;
    this.refreshing = true;
    try {
      const album = await (await this.request('')).json() as Album;
      this.status = ''; this.apply(album);
    } catch (error) {
      if (!this.destroyed) { this.status = error instanceof PhotoError ? error.message : '暂时离线：新照片会先保存在本机，联网后上传到小组。'; this.render(); }
    } finally { this.refreshing = false; }
    await this.flush();
  }
  private renderSummary() {
    if (this.destroyed) return;
    if (!this.member.releaseId) {
      this.root.innerHTML = '<p class="muted compact">从课程页创建小组后，可共享检查点照片并整组导出。</p>'; return;
    }
    this.root.innerHTML = `<button class="soft-button wide" data-open-album>小组打点照片 · ${this.album?.count ?? 0} 张${this.drafts.length ? ` · 本机待上传 ${this.drafts.length} 张` : ''}</button><p class="muted compact">组员共用每点一张照片，可替换；整组导出 PDF、长图或多图 ZIP。</p>${this.status ? `<p class="photo-status" role="status">${e(this.status)}</p>` : ''}`;
    this.root.querySelector<HTMLButtonElement>('[data-open-album]')!.onclick = () => this.open();
  }
  open(controlId?: string) {
    if (!this.member.releaseId) { this.notify('请从课程页创建小组后拍照打点'); return; }
    this.focusControl = controlId ?? null;
    if (this.dialog?.isConnected) { this.render(); return; }
    const dialog = document.createElement('dialog'); dialog.className = 'photo-dialog';
    dialog.innerHTML = '<div class="dialog-heading"><h2>小组打点照片</h2><button class="icon-button" data-photo-close aria-label="关闭小组照片">×</button></div><p id="photo-album-title" class="muted compact"></p><p id="photo-album-status" class="photo-status" role="status"></p><div id="photo-grid" class="photo-grid"></div><div class="photo-footer"><button id="refresh-photos" class="soft-button">刷新照片</button><button id="export-photos" class="primary-button">导出小组照片</button></div><p class="muted compact">上传中的照片暂存在此设备；联网后自动上传。小组闲置 6 小时或创建 24 小时后自动清理，请及时导出。导出并确认已保存后可立即清空。</p><input id="checkpoint-photo-file" type="file" accept="image/*" hidden>';
    this.dialog = dialog; document.body.append(dialog); dialog.showModal();
    dialog.querySelector<HTMLButtonElement>('[data-photo-close]')!.onclick = () => dialog.close();
    dialog.addEventListener('close', () => { dialog.remove(); if (this.dialog === dialog) this.dialog = null; }, { once: true });
    dialog.querySelector<HTMLButtonElement>('#refresh-photos')!.onclick = () => void this.refresh();
    dialog.querySelector<HTMLButtonElement>('#export-photos')!.onclick = () => this.export();
    const input = dialog.querySelector<HTMLInputElement>('#checkpoint-photo-file')!;
    input.onchange = () => {
      const file = input.files?.[0], id = input.dataset.controlId; input.value = '';
      if (file && id) void this.add(id, file);
    };
    dialog.querySelector('#photo-grid')!.addEventListener('click', event => {
      const button = (event.target as HTMLElement).closest<HTMLButtonElement>('button[data-photo-action]');
      if (!button) return;
      const id = button.dataset.control!, action = button.dataset.photoAction;
      if (action === 'camera' || action === 'choose') {
        input.dataset.controlId = id;
        if (action === 'camera') input.setAttribute('capture', 'environment'); else input.removeAttribute('capture');
        input.click();
      } else if (action === 'delete') void this.delete(id);
      else if (action === 'discard') void this.discard(id);
      else if (action === 'retry') void this.retryDraft(id);
      else if (action === 'preview') void this.preview(id);
    });
    this.render(); void this.ready.then(() => this.refresh());
  }
  private render() {
    this.renderSummary();
    const dialog = this.dialog;
    if (!dialog || this.destroyed) return;
    dialog.querySelector('#photo-album-title')!.textContent = this.album ? `${this.album.groupTitle} · ${this.album.title} · 已上传 ${this.album.count} 张` : '正在读取检查点…';
    dialog.querySelector('#photo-album-status')!.textContent = this.preparing ? '正在压缩并暂存照片…' : this.uploading ? '正在上传到小组…' : this.status || (this.drafts.length ? `本机有 ${this.drafts.length} 张待上传。导出包含小组已上传的照片。` : '每张导出照片都带检查点编号水印。');
    const grid = dialog.querySelector('#photo-grid')!;
    grid.innerHTML = this.album?.controls.map(c => {
      const draft = this.drafts.find(d => d.controlId === c.id), picture = c.photo, key = draft?.localId ?? picture?.revision;
      if (draft && !this.urls.has(draft.localId)) this.urls.set(draft.localId, URL.createObjectURL(draft.blob));
      const url = key ? this.urls.get(key) : null;
      return `<article class="checkpoint-photo${draft ? ' pending' : ''}" data-photo-id="${e(c.id)}"><strong>检查点 ${e(c.label)}</strong><button class="photo-preview" data-photo-action="preview" data-control="${e(c.id)}" aria-label="查看检查点 ${e(c.label)} 照片" ${key ? '' : 'disabled'}>${key ? `<img ${url ? `src="${url}"` : ''} data-photo-key="${e(key)}" data-photo-control="${e(c.id)}" alt="检查点 ${e(c.label)} 照片" loading="lazy">` : '<span>尚未上传</span>'}</button><small>${draft ? e(draft.error ?? '已暂存本机，等待上传') : picture ? `${e(picture.author)} · ${new Date(picture.uploadedAt * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : '小组成员均可上传'}</small><div class="photo-actions"><button class="soft-button" data-photo-action="camera" data-control="${e(c.id)}" ${this.preparing ? 'disabled' : ''}>${picture || draft ? '重拍' : '拍照'}</button><button class="soft-button" data-photo-action="choose" data-control="${e(c.id)}" ${this.preparing ? 'disabled' : ''}>${picture || draft ? '替换照片' : '选择照片'}</button>${draft ? `<button class="text-button" data-photo-action="retry" data-control="${e(c.id)}" ${this.uploading ? 'disabled' : ''}>${draft.conflict ? '替换小组当前照片' : '重试上传'}</button><button class="text-button danger" data-photo-action="discard" data-control="${e(c.id)}" ${this.uploading ? 'disabled' : ''}>删除本机待传</button>` : picture ? `<button class="text-button danger" data-photo-action="delete" data-control="${e(c.id)}">删除</button>` : ''}</div></article>`;
    }).join('') ?? '';
    for (const button of Array.from(grid.querySelectorAll<HTMLButtonElement>('[data-photo-action="camera"], [data-photo-action="choose"]'))) {
      button.disabled = this.preparing || button.dataset.control === this.uploadingControl;
    }
    dialog.querySelector<HTMLButtonElement>('#export-photos')!.disabled = !this.album?.count || this.preparing || this.uploading || !!this.drafts.length;
    if (this.focusControl) {
      const selected = Array.from(grid.querySelectorAll<HTMLElement>('[data-photo-id]')).find(el => el.dataset.photoId === this.focusControl);
      if (selected) { selected.classList.add('highlight'); selected.scrollIntoView({ block: 'center' }); this.focusControl = null; }
    }
    void this.thumbnails();
  }
  private async thumbnails() {
    if (!this.dialog || !this.album || !navigator.onLine) return;
    // Small thumbnails load sequentially to avoid a classroom-sized burst.
    for (const control of this.album.controls) {
      const picture = control.photo;
      if (!picture || this.urls.has(picture.revision) || this.loadingImages.has(picture.revision)) continue;
      this.loadingImages.add(picture.revision);
      try {
        const response = await this.request(`/${encodeURIComponent(control.id)}/image?thumbnail=true&revision=${picture.revision}`);
        const blob = await response.blob();
        if (this.destroyed || this.album?.controls.find(c => c.id === control.id)?.photo?.revision !== picture.revision) continue;
        const url = URL.createObjectURL(blob); this.urls.set(picture.revision, url);
        for (const img of Array.from(this.dialog?.querySelectorAll<HTMLImageElement>('img[data-photo-key]') ?? [])) {
          if (img.dataset.photoKey === picture.revision) img.src = url;
        }
      } catch { /* retry on refresh; the photo remains available to the group */ }
      finally { this.loadingImages.delete(picture.revision); }
    }
  }
  private async add(controlId: string, file: File) {
    if (this.destroyed || this.preparing || !this.album || this.uploadingControl === controlId) return;
    this.preparing = true; this.render();
    try {
      await this.ready;
      if (this.destroyed) return;
      const revision = this.album.controls.find(c => c.id === controlId)?.photo?.revision ?? '';
      const blob = await preparePhoto(file);
      if (this.destroyed) return;
      const draft: PhotoDraft = { key: this.prefix + controlId, localId: crypto.randomUUID(), controlId, revision, blob,
        createdAt: Date.now(), expiresAt: Date.now() + 24 * 3600000 };
      await saveDraft(draft); // Persist before starting any request.
      if (this.destroyed) { await removeDraft(draft); return; }
      this.drafts = [...this.drafts.filter(d => d.controlId !== controlId), draft];
      this.status = navigator.onLine ? '' : '已暂存本机，联网后会自动上传到小组。';
    } catch (error) { this.status = error instanceof Error ? error.message : '无法暂存照片，请检查设备空间'; }
    finally { this.preparing = false; this.render(); }
    await this.flush();
  }
  private async flush() {
    if (this.destroyed || this.uploading || !navigator.onLine) return;
    if (!this.drafts.some(d => !d.conflict && !d.retryRequired)) return;
    if (this.retry) { clearTimeout(this.retry); this.retry = null; }
    this.uploading = true; this.render();
    try {
      while (!this.destroyed && navigator.onLine) {
        // Include other checkpoints selected while the previous upload ran.
        const draft = this.drafts.find(d => !d.conflict && !d.retryRequired);
        if (!draft) break;
        this.uploadingControl = draft.controlId; this.render();
        const body = new FormData(); body.append('file', draft.blob, 'checkpoint.jpg');
        try {
          const result = await this.request('/' + encodeURIComponent(draft.controlId), { method: 'PUT', body, headers: {
            'X-DirMap-Photo-Revision': draft.revision, 'X-DirMap-Photo-Upload': draft.localId.replaceAll('-', ''),
          } }, 120000);
          const album = await result.json() as Album;
          await removeDraft(draft);
          this.drafts = this.drafts.filter(d => d.localId !== draft.localId);
          this.status = '照片已上传到小组'; this.apply(album);
        } catch (error) {
          if (this.destroyed) return;
          draft.error = error instanceof PhotoError ? error.message : '上传未完成，照片已保存在本机，稍后自动重试。';
          draft.conflict = error instanceof PhotoError && error.status === 409;
          draft.retryRequired = error instanceof PhotoError &&
            ((error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) || error.status === 507);
          // Do not overwrite a newer selection of this checkpoint.
          if (this.drafts.some(d => d.localId === draft.localId)) await saveDraft(draft);
          this.status = draft.error;
          if (draft.retryRequired) { void this.refresh(); continue; }
          if (!this.retry) this.retry = setTimeout(() => { this.retry = null; void this.flush(); }, 15000);
          break;
        }
      }
    } finally { this.uploading = false; this.uploadingControl = null; this.render(); }
  }
  private async retryDraft(controlId: string) {
    const draft = this.drafts.find(d => d.controlId === controlId);
    if (!draft) return;
    if (draft.conflict) {
      if (!confirm('用本机这张照片替换小组当前照片？')) return;
      await this.refresh();
      draft.revision = this.album?.controls.find(c => c.id === controlId)?.photo?.revision ?? '';
    }
    draft.conflict = false; draft.retryRequired = false; delete draft.error;
    await saveDraft(draft); await this.flush();
  }
  private async discard(controlId: string) {
    const draft = this.drafts.find(d => d.controlId === controlId);
    if (!draft || !confirm('删除这张尚未上传的本机照片？小组现有照片会保留。')) return;
    await removeDraft(draft); this.drafts = this.drafts.filter(d => d.localId !== draft.localId);
    const url = this.urls.get(draft.localId); if (url) URL.revokeObjectURL(url); this.urls.delete(draft.localId);
    this.render();
  }
  private async delete(controlId: string) {
    const control = this.album?.controls.find(c => c.id === controlId);
    if (!control?.photo || !confirm(`删除小组检查点 ${control.label} 的照片？所有组员将不再看到它。`)) return;
    try {
      const result = await this.request('/' + encodeURIComponent(controlId), { method: 'DELETE', headers: { 'X-DirMap-Photo-Revision': control.photo.revision } });
      this.status = '已删除照片'; this.apply(await result.json());
    } catch (error) { this.status = String(error); this.render(); void this.refresh(); }
  }
  private async preview(controlId: string) {
    const control = this.album?.controls.find(c => c.id === controlId), draft = this.drafts.find(d => d.controlId === controlId);
    if (!control || (!control.photo && !draft)) return;
    try {
      const blob = draft?.blob ?? await (await this.request(`/${encodeURIComponent(controlId)}/image?revision=${control.photo!.revision}`)).blob();
      if (this.destroyed) return;
      const url = URL.createObjectURL(blob), dialog = document.createElement('dialog'); dialog.className = 'photo-preview-dialog';
      dialog.innerHTML = `<div class="dialog-heading"><h2>检查点 ${e(control.label)}</h2><button class="icon-button" aria-label="关闭照片">×</button></div><img src="${url}" alt="检查点 ${e(control.label)}"><p class="muted compact">导出时会加上检查点编号水印。</p>`;
      document.body.append(dialog); dialog.showModal();
      dialog.querySelector('button')!.onclick = () => dialog.close();
      dialog.addEventListener('close', () => { URL.revokeObjectURL(url); dialog.remove(); }, { once: true });
      this.abort.signal.addEventListener('abort', () => dialog.close(), { once: true });
    } catch (error) { this.notify(error instanceof Error ? error.message : '无法加载照片', true); }
  }
  private export() {
    if (!this.album?.count || this.drafts.length || this.uploading) return;
    const dialog = document.createElement('dialog'); dialog.className = 'info-dialog photo-export-dialog'; this.exportDialog = dialog;
    dialog.innerHTML = `<div class="dialog-heading"><h2>导出小组照片</h2><button class="icon-button" aria-label="关闭照片导出">×</button></div><p>导出小组已上传的全部照片，每张带检查点编号水印。</p><label class="field-label">导出格式<select id="photo-export-format"><option value="pdf">PDF · 每张照片一页</option><option value="long-image">长图 · 全部照片纵向拼接</option><option value="images">多图 · ZIP 内每点一张 JPG</option></select></label><p class="muted compact">长图在照片较多时会缩小；需要保持清晰可选择 PDF 或多图。</p><button id="make-photo-export" class="primary-button wide">生成并下载</button><p id="photo-export-status" role="status"></p><a id="download-photos" class="text-link hidden">重新下载</a><button id="share-photos" class="soft-button wide hidden">分享文件到其他应用</button><div id="finish-photo-export" class="hidden"><p class="muted">先打开导出文件确认完整，并保存或提交到其他平台。结束后会删除小组中本次导出的照片；之后新上传或替换的照片会保留。</p><button id="finish-photos" class="danger-button wide">已保存，结束并清空已导出照片</button></div>`;
    document.body.append(dialog); dialog.showModal();
    const abort = new AbortController();
    let downloadUrl: string | null = null, exportId: string | null = null;
    dialog.querySelector<HTMLButtonElement>('.dialog-heading button')!.onclick = () => dialog.close();
    dialog.addEventListener('close', () => {
      abort.abort(); dialog.remove(); if (this.exportDialog === dialog) this.exportDialog = null;
      if (downloadUrl) { const url = downloadUrl; setTimeout(() => URL.revokeObjectURL(url), 30000); }
    }, { once: true });
    const status = dialog.querySelector('#photo-export-status')!;
    dialog.querySelector<HTMLButtonElement>('#make-photo-export')!.onclick = async () => {
      const button = dialog.querySelector<HTMLButtonElement>('#make-photo-export')!;
      button.disabled = true; exportId = null; status.textContent = '正在生成，请稍候…';
      dialog.querySelector('#finish-photo-export')!.classList.add('hidden');
      dialog.querySelector('#download-photos')!.classList.add('hidden');
      dialog.querySelector('#share-photos')!.classList.add('hidden');
      try {
        const format = dialog.querySelector<HTMLSelectElement>('#photo-export-format')!.value;
        const result = await this.request('/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ format }), signal: abort.signal }, 120000);
        const blob = await result.blob(); if (abort.signal.aborted || this.destroyed) return;
        exportId = result.headers.get('X-DirMap-Photo-Export');
        if (!exportId || !blob.size) throw new Error('下载未完整返回，请重试；小组照片仍保留。');
        const filename = result.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] ?? `dirmap-photos.${format === 'pdf' ? 'pdf' : format === 'images' ? 'zip' : 'jpg'}`;
        if (downloadUrl) URL.revokeObjectURL(downloadUrl); downloadUrl = URL.createObjectURL(blob);
        const link = dialog.querySelector<HTMLAnchorElement>('#download-photos')!;
        link.href = downloadUrl; link.download = filename; link.classList.remove('hidden'); link.click();
        status.textContent = `已生成 ${result.headers.get('X-DirMap-Photo-Count')} 张照片的文件。若未开始下载，可点击“重新下载”。`;
        dialog.querySelector('#finish-photo-export')!.classList.remove('hidden');
        const file = new File([blob], filename, { type: blob.type }), share = dialog.querySelector<HTMLButtonElement>('#share-photos')!;
        share.classList.toggle('hidden', !navigator.canShare?.({ files: [file] }));
        share.onclick = async () => {
          try { await navigator.share({ files: [file], title: '小组检查点照片' }); }
          catch { status.textContent = '分享未完成，照片仍保留；也可以下载后自行提交。'; }
        };
      } catch (error) { if (!abort.signal.aborted) status.textContent = error instanceof PhotoError ? error.message : '导出未完成，小组照片仍保留。请检查连接后重试。'; }
      finally { button.disabled = false; }
    };
    dialog.querySelector<HTMLButtonElement>('#finish-photos')!.onclick = async () => {
      if (!exportId) return;
      const button = dialog.querySelector<HTMLButtonElement>('#finish-photos')!; button.disabled = true;
      try {
        const result = await this.json<{ deleted: number; album: Album }>('/finish', { exportId });
        this.apply(result.album);
        this.notify(`已清空 ${result.deleted} 张已导出照片${result.album.count ? `，保留 ${result.album.count} 张后续照片` : ''}`);
        dialog.close();
      } catch (error) { status.textContent = error instanceof Error ? error.message : '清理未完成，请重试'; button.disabled = false; }
    };
  }
  destroy(clear = false) {
    this.destroyed = true; this.abort.abort(); if (this.retry) clearTimeout(this.retry);
    this.dialog?.close(); this.exportDialog?.close();
    for (const url of this.urls.values()) URL.revokeObjectURL(url); this.urls.clear();
    if (clear) { void clearDrafts(this.prefix).catch(() => {}); try { localStorage.removeItem(this.cacheKey); } catch { /* storage unavailable */ } }
  }
}
