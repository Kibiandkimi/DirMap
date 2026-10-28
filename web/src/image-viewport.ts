import type { Pixel } from './types';

type Callbacks = {
  tap: (pixel: Pixel) => void;
  drawing: () => boolean;
  stroke: (phase: 'start' | 'move' | 'end' | 'cancel', pixel?: Pixel) => void;
  change: (scale: number) => void;
};

/** Image coordinates stay fixed while the viewport pans and zooms. A tap is
 * emitted on release only; drags, cancelled touches and pinches never pick. */
export class ImageViewport {
  private scale = 1;
  private offset: Pixel = [0, 0];
  private size: Pixel = [0, 0];
  private pointers = new Map<number, Pixel>();
  private start: Pixel = [0, 0];
  private last: Pixel = [0, 0];
  private gesture: 'tap' | 'pan' | 'draw' | 'pinch' = 'pan';
  private pinchDistance = 0;
  private fitted = true;
  private observer: ResizeObserver;
  private abort = new AbortController();

  constructor(private element: HTMLElement, private canvas: HTMLCanvasElement, private callbacks: Callbacks) {
    const options = { signal: this.abort.signal };
    element.addEventListener('pointerdown', this.down, options);
    element.addEventListener('pointermove', this.move, options);
    element.addEventListener('pointerup', this.up, options);
    element.addEventListener('pointercancel', this.cancel, options);
    element.addEventListener('lostpointercapture', this.cancel, options);
    element.addEventListener('wheel', this.wheel, { ...options, passive: false });
    element.addEventListener('keydown', this.key, options);
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(element);
    this.resize();
  }

  private minimum() { return Math.min(this.size[0] / this.canvas.width, this.size[1] / this.canvas.height) * .96; }
  private local(event: MouseEvent): Pixel {
    const rect = this.element.getBoundingClientRect();
    return [event.clientX - rect.left - this.element.clientLeft, event.clientY - rect.top - this.element.clientTop];
  }
  private pixel(point: Pixel): Pixel { return [(point[0] - this.offset[0]) / this.scale, (point[1] - this.offset[1]) / this.scale]; }
  private within(point: Pixel) { return point[0] >= 0 && point[1] >= 0 && point[0] < this.canvas.width && point[1] < this.canvas.height; }
  private clampedPixel(point: Pixel): Pixel {
    const pixel = this.pixel(point);
    return [Math.max(0, Math.min(this.canvas.width - 1, pixel[0])), Math.max(0, Math.min(this.canvas.height - 1, pixel[1]))];
  }
  private resize() {
    const center = this.pixel([this.size[0] / 2, this.size[1] / 2]);
    this.size = [this.element.clientWidth, this.element.clientHeight];
    if (!this.size.every(n => n > 0)) return;
    if (this.fitted) this.fit();
    else { this.offset = [this.size[0] / 2 - center[0] * this.scale, this.size[1] / 2 - center[1] * this.scale]; this.render(); }
  }
  private render() {
    // Leave room to move a point away from a finger, without losing the sheet.
    for (const i of [0, 1]) {
      const length = (i ? this.canvas.height : this.canvas.width) * this.scale;
      const margin = Math.min(80, this.size[i] / 3);
      this.offset[i] = Math.max(margin - length, Math.min(this.size[i] - margin, this.offset[i]));
    }
    this.canvas.style.transform = `translate(${this.offset[0]}px, ${this.offset[1]}px) scale(${this.scale})`;
    this.callbacks.change(this.scale);
  }
  fit() {
    this.fitted = true;
    this.scale = this.minimum();
    this.offset = [(this.size[0] - this.canvas.width * this.scale) / 2, (this.size[1] - this.canvas.height * this.scale) / 2];
    this.render();
  }
  zoom(factor: number, at: Pixel = [this.size[0] / 2, this.size[1] / 2]) {
    const pixel = this.pixel(at);
    this.fitted = false;
    this.scale = Math.max(this.minimum() / 2, Math.min(Math.max(8, this.minimum() * 16), this.scale * factor));
    this.offset = [at[0] - pixel[0] * this.scale, at[1] - pixel[1] * this.scale];
    this.render();
  }
  private pan(from: Pixel, to: Pixel) {
    this.fitted = false;
    this.offset = [this.offset[0] + to[0] - from[0], this.offset[1] + to[1] - from[1]];
    this.render();
  }
  private pair() {
    const [a, b] = [...this.pointers.values()];
    return { center: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] as Pixel, distance: Math.hypot(a[0] - b[0], a[1] - b[1]) };
  }
  private down = (event: PointerEvent) => {
    if (event.button !== 0 && event.button !== 1) return;
    event.preventDefault();
    this.element.focus({ preventScroll: true });
    const point = this.local(event);
    this.pointers.set(event.pointerId, point);
    this.element.setPointerCapture(event.pointerId);
    if (this.pointers.size === 1) {
      this.start = this.last = point;
      this.gesture = this.callbacks.drawing() && event.button === 0 && this.within(this.pixel(point)) ? 'draw' : event.button === 1 ? 'pan' : 'tap';
      if (this.gesture === 'draw') this.callbacks.stroke('start', this.clampedPixel(point));
    } else {
      if (this.gesture === 'draw') this.callbacks.stroke('cancel');
      this.gesture = 'pinch';
      const pair = this.pair(); this.last = pair.center; this.pinchDistance = pair.distance;
    }
  };
  private move = (event: PointerEvent) => {
    if (!this.pointers.has(event.pointerId)) return;
    const point = this.local(event);
    this.pointers.set(event.pointerId, point);
    if (this.pointers.size > 1) {
      const pair = this.pair();
      this.pan(this.last, pair.center);
      if (this.pinchDistance > 0) this.zoom(pair.distance / this.pinchDistance, pair.center);
      this.last = pair.center; this.pinchDistance = pair.distance;
    } else if (this.gesture === 'draw') this.callbacks.stroke('move', this.clampedPixel(point));
    else {
      if (this.gesture === 'tap' && Math.hypot(point[0] - this.start[0], point[1] - this.start[1]) > 5) this.gesture = 'pan';
      if (this.gesture !== 'tap') { this.pan(this.last, point); this.last = point; }
    }
  };
  private finish(event: PointerEvent, cancelled: boolean) {
    if (!this.pointers.has(event.pointerId)) return;
    const point = this.local(event), pixel = this.pixel(point);
    if (this.pointers.size === 1) {
      if (this.gesture === 'draw') this.callbacks.stroke(cancelled ? 'cancel' : 'end', this.clampedPixel(point));
      else if (!cancelled && this.gesture === 'tap' && Math.hypot(point[0] - this.start[0], point[1] - this.start[1]) <= 5 && this.within(pixel)) this.callbacks.tap(pixel);
    }
    this.pointers.delete(event.pointerId);
    if (this.pointers.size === 1) { this.gesture = 'pan'; this.last = [...this.pointers.values()][0]; }
    else if (this.pointers.size > 1) { const pair = this.pair(); this.last = pair.center; this.pinchDistance = pair.distance; }
    if (this.element.hasPointerCapture(event.pointerId)) this.element.releasePointerCapture(event.pointerId);
  }
  private up = (event: PointerEvent) => this.finish(event, false);
  private cancel = (event: PointerEvent) => this.finish(event, true);
  private wheel = (event: WheelEvent) => {
    event.preventDefault();
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.size[1] : 1);
    this.zoom(Math.exp(-Math.max(-400, Math.min(400, delta)) * .002), this.local(event));
  };
  private key = (event: KeyboardEvent) => {
    if (event.key === '+' || event.key === '=') this.zoom(1.4);
    else if (event.key === '-') this.zoom(1 / 1.4);
    else if (event.key === '0') this.fit();
    else if (event.key.startsWith('Arrow')) this.pan([0, 0], [event.key === 'ArrowLeft' ? 40 : event.key === 'ArrowRight' ? -40 : 0, event.key === 'ArrowUp' ? 40 : event.key === 'ArrowDown' ? -40 : 0]);
    else return;
    event.preventDefault();
  };
  destroy() { this.abort.abort(); this.observer.disconnect(); }
}
