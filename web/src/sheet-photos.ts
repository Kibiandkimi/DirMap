import { escapeHtml as e, type SheetPhoto } from './types';

export function showSheetPhotos(initialPhotos: SheetPhoto[], url: (photo: SheetPhoto) => string, controlId?: string,
  saveLabel?: (photo: SheetPhoto, label: string) => Promise<SheetPhoto[]>) {
  const dialog = document.createElement('dialog');
  dialog.className = 'sheet-photo-dialog'; dialog.setAttribute('aria-label', '原图参考照片');
  let photos = initialPhotos;
  let selected = controlId ? photos.find(p => p.controlId === controlId)?.id ?? null : null;
  const order = () => [...photos].sort((a, b) => (a.label ?? '\uffff').localeCompare(b.label ?? '\uffff', 'zh-CN', { numeric: false }) || a.id.localeCompare(b.id));
  const title = (photo: SheetPhoto) => photo.label ? `目标 ${photo.label}` : '编号待核对';
  function render() {
    const sorted = order(), photo = photos.find(p => p.id === selected), index = sorted.findIndex(p => p.id === selected);
    dialog.innerHTML = `<div class="dialog-heading"><h2>${photo ? e(title(photo)) : '原图参考照片'}</h2><button class="icon-button" data-close aria-label="关闭参考照片">×</button></div>
      <p class="muted compact">原图提供的打卡背景，供现场核对。</p>${photo ? `
        <div class="sheet-photo-view"><img src="${e(url(photo))}" alt="${e(title(photo))} 的原图参考照片"></div>
        <div class="sheet-photo-actions"><button class="soft-button" data-previous ${index <= 0 ? 'disabled' : ''}>上一张</button><button class="text-button" data-album>全部 ${photos.length} 张</button><button class="soft-button" data-next ${index >= sorted.length - 1 ? 'disabled' : ''}>下一张</button></div>
        ${!photo.controlId ? '<p class="muted">尚未匹配到唯一目标，请核对照片与目标编号。</p>' : ''}
        ${saveLabel ? `<form class="sheet-photo-label"><label>目标编号 <input aria-label="参考照片目标编号" maxlength="50" value="${e(photo.label ?? '')}"></label><button class="soft-button">保存编号</button><span role="status"></span></form>` : ''}` : `
        <div class="sheet-photo-grid">${sorted.map(p => `<button class="sheet-photo-card" data-photo="${e(p.id)}"><img src="${e(url(p))}" alt="${e(title(p))} 的原图参考照片" loading="lazy"><span>${e(title(p))}</span></button>`).join('')}</div>`}`;
    dialog.querySelector<HTMLButtonElement>('[data-close]')!.onclick = () => dialog.close();
    dialog.querySelector<HTMLButtonElement>('[data-album]')?.addEventListener('click', () => { selected = null; render(); });
    dialog.querySelector<HTMLButtonElement>('[data-previous]')?.addEventListener('click', () => { selected = sorted[index-1]?.id ?? selected; render(); });
    dialog.querySelector<HTMLButtonElement>('[data-next]')?.addEventListener('click', () => { selected = sorted[index+1]?.id ?? selected; render(); });
    dialog.querySelectorAll<HTMLButtonElement>('[data-photo]').forEach(button => button.onclick = () => { selected = button.dataset.photo!; render(); });
    const form = dialog.querySelector<HTMLFormElement>('form');
    if (form && photo && saveLabel) form.onsubmit = async event => {
      event.preventDefault(); const button = form.querySelector('button')!; button.disabled = true;
      try { photos = await saveLabel(photo, form.querySelector('input')!.value); render(); }
      catch (error) { form.querySelector('[role="status"]')!.textContent = (error as Error).message; button.disabled = false; }
    };
  }
  render(); document.body.append(dialog); dialog.showModal();
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
}

export function referencePhotoButton(photos: SheetPhoto[], controlId: string) {
  const photo = photos.find(p => p.controlId === controlId);
  return photo ? `<button class="text-button" data-reference-photo="${e(controlId)}" aria-label="目标 ${e(photo.label)} 的参考照片">参考图</button>` : '';
}
