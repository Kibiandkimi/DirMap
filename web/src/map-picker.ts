import type { CampusMap } from './map';
import { escapeHtml as e, type Pixel } from './types';

export class MapPointPicker {
  private resolve: ((point: Pixel | null) => void) | null = null;
  private point: Pixel | null = null;
  private validate: (point: Pixel) => string | null = () => null;
  get active() { return this.resolve !== null; }

  constructor(private map: CampusMap, private element: HTMLElement, private onBegin: () => void) {
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && this.active) { event.preventDefault(); this.cancel(); }
    });
  }
  choose(title: string, options: { initial?: Pixel | null; validate?: (point: Pixel) => string | null; back?: () => void } = {}): Promise<Pixel | null> {
    this.cancel(); this.onBegin();
    this.point = options.initial ?? null;
    this.validate = options.validate ?? (() => null);
    this.element.innerHTML = `<strong>${e(title)}</strong><span id="map-pick-position"></span><small>可拖动、缩放，重复点选调整位置。</small><div class="map-pick-actions"><button id="confirm-map-point" class="primary-button">确认选点</button><button id="cancel-map-point" class="soft-button">取消</button>${options.back ? '<button id="back-image-point" class="soft-button">重选原图位置</button>' : ''}</div>`;
    this.element.classList.remove('hidden');
    this.element.querySelector<HTMLButtonElement>('#confirm-map-point')!.onclick = () => {
      if (this.point && !this.validate(this.point)) this.finish(this.point);
    };
    this.element.querySelector<HTMLButtonElement>('#cancel-map-point')!.onclick = () => this.cancel();
    this.element.querySelector<HTMLButtonElement>('#back-image-point')?.addEventListener('click', () => { this.cancel(); options.back?.(); });
    this.render();
    return new Promise(resolve => { this.resolve = resolve; });
  }
  pick(point: Pixel) {
    if (!this.active) return false;
    this.point = point; this.render(); return true;
  }
  private render() {
    const error = this.point ? this.validate(this.point) : null;
    this.map.setPickPreview(this.point, error ? '范围外' : '待确认', !error);
    this.element.querySelector('#map-pick-position')!.textContent = this.point
      ? `${this.point[1].toFixed(6)}°N, ${this.point[0].toFixed(6)}°E${error ? ` · ${error}` : ' · 已标记'}` : '请在地图上点选位置';
    this.element.querySelector<HTMLButtonElement>('#confirm-map-point')!.disabled = !this.point || !!error;
  }
  private finish(point: Pixel | null) {
    const resolve = this.resolve;
    this.resolve = null; this.point = null; this.map.setPickPreview(null);
    this.element.classList.add('hidden'); resolve?.(point);
  }
  cancel() { if (this.active) this.finish(null); }
}
