import { localScales } from './types';
import type { HeightGrid } from './elevation-types';

const palette = [[66, 127, 107], [136, 164, 111], [205, 191, 128], [171, 116, 79], [116, 77, 61]];
function colour(t: number) {
  const value = Math.max(0, Math.min(.99999, t)) * (palette.length - 1), index = Math.floor(value), weight = value - index;
  return palette[index].map((v, i) => (v * (1 - weight) + palette[index + 1][i] * weight) / 255);
}
function multiply(a: Float32Array, b: Float32Array) {
  const result = new Float32Array(16);
  for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) for (let k = 0; k < 4; k++) result[c * 4 + r] += a[k * 4 + r] * b[c * 4 + k];
  return result;
}
function normalized(v: number[]) { const length = Math.hypot(...v) || 1; return v.map(x => x / length); }
const cross = (a: number[], b: number[]) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a: number[], b: number[]) => a.reduce((sum, x, i) => sum + x * b[i], 0);

/** Small native WebGL mesh; no globe SDK, tile download or bitmap terrain package. */
export class ElevationView {
  private gl: WebGLRenderingContext | null;
  private program: WebGLProgram | null = null;
  private mesh: WebGLBuffer | null = null;
  private indexes: WebGLBuffer | null = null;
  private points: WebGLBuffer | null = null;
  private indexCount = 0;
  private pointCount = 0;
  private grid: HeightGrid | null = null;
  private raster: HTMLCanvasElement | null = null;
  private mode: '3d' | 'map' = '3d';
  private yaw = -.6;
  private pitch = .6;
  private distance = 1.5;
  private exaggeration = 4;
  private showPoints = false;
  private observer: ResizeObserver;
  private touches = new Map<number, [number, number]>();
  private imageBounds = [0, 0, 1, 1];
  onInspect: (value: { lng: number; lat: number; height: number | null; support: number | null } | null) => void = () => {};

  constructor(private canvas: HTMLCanvasElement, private flat: HTMLCanvasElement) {
    this.gl = canvas.getContext('webgl', { alpha: false, antialias: true, preserveDrawingBuffer: true });
    if (this.gl) {
      try { this.program = this.initialize(this.gl); }
      catch { this.gl = null; }
    }
    if (!this.gl) this.mode = 'map';
    canvas.style.touchAction = 'none';
    canvas.addEventListener('pointerdown', event => {
      canvas.setPointerCapture(event.pointerId); this.touches.set(event.pointerId, [event.clientX, event.clientY]);
    });
    canvas.addEventListener('pointermove', event => {
      const previous = this.touches.get(event.pointerId);
      if (!previous) return;
      const old = [...this.touches.values()];
      this.touches.set(event.pointerId, [event.clientX, event.clientY]);
      const current = [...this.touches.values()];
      if (current.length === 1) { this.yaw -= (event.clientX - previous[0]) * .008; this.pitch = Math.max(.15, Math.min(1.5, this.pitch + (event.clientY - previous[1]) * .006)); }
      else {
        const span = (p: [number, number][]) => Math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1]);
        if (span(current) > 5) this.distance = Math.max(.7, Math.min(5, this.distance * span(old) / span(current)));
      }
      this.draw();
    });
    for (const kind of ['pointerup', 'pointercancel', 'lostpointercapture']) canvas.addEventListener(kind, event => this.touches.delete((event as PointerEvent).pointerId));
    canvas.addEventListener('wheel', event => { event.preventDefault(); this.distance = Math.max(.7, Math.min(5, this.distance * Math.exp(event.deltaY * .001))); this.draw(); }, { passive: false });
    canvas.addEventListener('keydown', event => {
      const actions: Record<string, () => void> = { ArrowLeft: () => this.yaw += .15, ArrowRight: () => this.yaw -= .15,
        ArrowUp: () => this.pitch = Math.min(1.5, this.pitch + .1), ArrowDown: () => this.pitch = Math.max(.15, this.pitch - .1),
        '+': () => this.distance = Math.max(.7, this.distance * .9), '=': () => this.distance = Math.max(.7, this.distance * .9),
        '-': () => this.distance = Math.min(5, this.distance * 1.1), r: () => this.reset() };
      if (actions[event.key]) { event.preventDefault(); actions[event.key](); this.draw(); }
    });
    flat.addEventListener('pointermove', event => this.inspect(event));
    flat.addEventListener('click', event => this.inspect(event));
    this.observer = new ResizeObserver(() => this.draw()); this.observer.observe(canvas.parentElement!);
    this.setMode(this.mode);
  }
  get supports3D() { return !!this.gl; }
  setMode(mode: '3d' | 'map') { this.mode = mode === '3d' && this.gl ? '3d' : 'map'; this.canvas.hidden = this.mode !== '3d'; this.flat.hidden = this.mode !== 'map'; this.draw(); }
  setExaggeration(value: number) { this.exaggeration = value; this.buildMesh(); this.draw(); }
  setPoints(show: boolean) { this.showPoints = show; this.draw(); }
  reset() { this.yaw = -.6; this.pitch = .6; this.distance = 1.5; this.draw(); }
  setGrid(grid: HeightGrid | null) { this.grid = grid; this.raster = null; this.indexCount = 0; this.onInspect(null); if (grid) { this.buildRaster(); this.buildMesh(); } this.draw(); }
  private initialize(gl: WebGLRenderingContext) {
    const shader = (type: number, source: string) => {
      const value = gl.createShader(type)!; gl.shaderSource(value, source); gl.compileShader(value);
      if (!gl.getShaderParameter(value, gl.COMPILE_STATUS)) throw new Error('地形绘制初始化失败'); return value;
    };
    const vertex = shader(gl.VERTEX_SHADER, `attribute vec3 a_position; attribute vec3 a_normal; attribute vec3 a_color;
      uniform mat4 u_camera; uniform float u_point_size; varying vec3 v_color; varying vec3 v_normal;
      void main(){gl_Position=u_camera*vec4(a_position,1.0);gl_PointSize=u_point_size;v_color=a_color;v_normal=a_normal;}`);
    const fragment = shader(gl.FRAGMENT_SHADER, `precision mediump float; varying vec3 v_color; varying vec3 v_normal;
      void main(){float light=0.5+0.5*max(0.0,dot(normalize(v_normal),normalize(vec3(-0.6,1.0,-0.4))));gl_FragColor=vec4(v_color*light,1.0);}`);
    const program = gl.createProgram()!; gl.attachShader(program, vertex); gl.attachShader(program, fragment); gl.linkProgram(program);
    gl.deleteShader(vertex); gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('地形绘制初始化失败');
    return program;
  }
  private buildMesh() {
    const gl = this.gl, grid = this.grid;
    if (!gl || !grid) return;
    for (const buffer of [this.mesh, this.indexes, this.points]) if (buffer) gl.deleteBuffer(buffer);
    const stride = Math.max(1, Math.ceil(Math.max(grid.rows, grid.columns) / 240));
    const xs = Array.from({ length: Math.floor((grid.columns - 1) / stride) + 1 }, (_, i) => i * stride);
    const ys = Array.from({ length: Math.floor((grid.rows - 1) / stride) + 1 }, (_, i) => i * stride);
    const size = Math.max(grid.columns - 1, grid.rows - 1) * grid.spacingM;
    const middle = ((grid.minHeightM ?? 0) + (grid.maxHeightM ?? 0)) / 2;
    const range = Math.max(1, (grid.maxHeightM ?? 0) - (grid.minHeightM ?? 0));
    const vertices: number[] = [], triangles: number[] = [];
    const height = (x: number, y: number) => grid.height[Math.max(0, Math.min(grid.rows - 1, y)) * grid.columns + Math.max(0, Math.min(grid.columns - 1, x))];
    for (const y of ys) for (const x of xs) {
      const h = height(x, y) ?? middle;
      const dx = ((height(x + 1, y) ?? h) - (height(x - 1, y) ?? h)) / (2 * grid.spacingM);
      const dy = ((height(x, y + 1) ?? h) - (height(x, y - 1) ?? h)) / (2 * grid.spacingM);
      vertices.push((x - (grid.columns - 1) / 2) * grid.spacingM / size, (h - middle) * this.exaggeration / size,
        -(y - (grid.rows - 1) / 2) * grid.spacingM / size,
        ...normalized([-dx * this.exaggeration, 1, dy * this.exaggeration]), ...colour((h - (grid.minHeightM ?? 0)) / range));
    }
    for (let y = 0; y < ys.length - 1; y++) for (let x = 0; x < xs.length - 1; x++) {
      let valid = true;
      for (let yy = ys[y]; yy <= ys[y + 1] && valid; yy++) for (let xx = xs[x]; xx <= xs[x + 1]; xx++) if (height(xx, yy) === null) { valid = false; break; }
      if (valid) { const a = y * xs.length + x, b = a + 1, c = a + xs.length, d = c + 1; triangles.push(a, c, b, b, c, d); }
    }
    this.mesh = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, this.mesh); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);
    this.indexes = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indexes); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(triangles), gl.STATIC_DRAW);
    this.indexCount = triangles.length;
    const scales = localScales(grid.frame.origin), pointVertices: number[] = [];
    for (const [lng, lat, h] of grid.observations) {
      const x = (lng - grid.frame.origin[0]) * scales[0] - grid.origin[0], y = (lat - grid.frame.origin[1]) * scales[1] - grid.origin[1];
      pointVertices.push((x - (grid.columns - 1) * grid.spacingM / 2) / size, (h - middle) * this.exaggeration / size + .002,
        -(y - (grid.rows - 1) * grid.spacingM / 2) / size, 0, 1, 0, .14, .32, .28);
    }
    this.points = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, this.points); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pointVertices), gl.STATIC_DRAW);
    this.pointCount = pointVertices.length / 9;
  }
  private buildRaster() {
    const grid = this.grid!;
    const raster = document.createElement('canvas'); raster.width = grid.columns; raster.height = grid.rows;
    const ctx = raster.getContext('2d')!, image = ctx.createImageData(grid.columns, grid.rows);
    const range = Math.max(1, (grid.maxHeightM ?? 0) - (grid.minHeightM ?? 0));
    for (let y = 0; y < grid.rows; y++) for (let x = 0; x < grid.columns; x++) {
      const h = grid.height[y * grid.columns + x]; if (h === null) continue;
      const value = (xx: number, yy: number) => grid.height[Math.max(0, Math.min(grid.rows - 1, yy)) * grid.columns + Math.max(0, Math.min(grid.columns - 1, xx))] ?? h;
      const normal = normalized([-(value(x + 1, y) - value(x - 1, y)) / (2 * grid.spacingM), 1, (value(x, y + 1) - value(x, y - 1)) / (2 * grid.spacingM)]);
      const light = .65 + .35 * Math.max(0, dot(normal, normalized([-.6, 1, -.4]))), c = colour((h - (grid.minHeightM ?? 0)) / range);
      const at = ((grid.rows - 1 - y) * grid.columns + x) * 4;
      image.data.set([...c.map(v => Math.round(v * light * 255)), 255], at);
    }
    ctx.putImageData(image, 0, 0); this.raster = raster;
  }
  private contours(ctx: CanvasRenderingContext2D, left: number, top: number, scale: number) {
    const grid = this.grid!, step = ((grid.maxHeightM ?? 0) - (grid.minHeightM ?? 0)) > 60 ? 10 : 2;
    ctx.strokeStyle = '#304a393d'; ctx.lineWidth = .65;
    for (let level = Math.ceil((grid.minHeightM ?? 0) / step) * step; level <= (grid.maxHeightM ?? 0); level += step) {
      ctx.beginPath();
      for (let y = 0; y < grid.rows - 1; y++) for (let x = 0; x < grid.columns - 1; x++) {
        const corners = [[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1]], heights = corners.map(([xx, yy]) => grid.height[yy * grid.columns + xx]);
        if (heights.some(h => h === null)) continue;
        const intersections: number[][] = [];
        for (let k = 0; k < 4; k++) { const next = (k + 1) % 4, a = heights[k]!, b = heights[next]!;
          if ((a < level) !== (b < level)) { const t = (level - a) / (b - a); intersections.push([corners[k][0] + (corners[next][0] - corners[k][0]) * t, corners[k][1] + (corners[next][1] - corners[k][1]) * t]); } }
        for (let k = 0; k + 1 < intersections.length; k += 2) {
          ctx.moveTo(left + intersections[k][0] * scale, top + (grid.rows - 1 - intersections[k][1]) * scale);
          ctx.lineTo(left + intersections[k + 1][0] * scale, top + (grid.rows - 1 - intersections[k + 1][1]) * scale);
        }
      }
      ctx.stroke();
    }
  }
  private geographicContext(ctx: CanvasRenderingContext2D, left: number, top: number, scale: number) {
    const grid = this.grid!, scales = localScales(grid.frame.origin);
    ctx.save(); ctx.beginPath(); ctx.rect(left, top, (grid.columns - 1) * scale, (grid.rows - 1) * scale); ctx.clip();
    for (const feature of grid.context?.features ?? []) {
      const polygon = feature.geometry.type === 'Polygon';
      const paths: number[][][] = polygon ? feature.geometry.coordinates as number[][][] : [feature.geometry.coordinates as number[][]];
      const kind = feature.properties.kind;
      ctx.beginPath();
      for (const points of paths) {
        for (const [index, [lng, lat]] of points.entries()) {
          const x = left + ((lng - grid.frame.origin[0]) * scales[0] - grid.origin[0]) / grid.spacingM * scale;
          const y = top + (grid.rows - 1 - ((lat - grid.frame.origin[1]) * scales[1] - grid.origin[1]) / grid.spacingM) * scale;
          if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        if (polygon) ctx.closePath();
      }
      ctx.lineWidth = polygon ? .65 : kind === 'waterway' ? 2 : 2.5;
      ctx.strokeStyle = kind === 'water' || kind === 'waterway' ? '#a4c3cb' : polygon ? '#c2ccb9' : '#ffffff';
      if (polygon) { ctx.fillStyle = kind === 'water' ? '#c4dce0' : kind === 'building' ? '#d5dccf' : '#e2ebd8'; ctx.fill(); }
      ctx.stroke();
    }
    ctx.restore();
  }
  private inspect(event: PointerEvent | MouseEvent) {
    const grid = this.grid; if (!grid) return;
    const box = this.flat.getBoundingClientRect(), [left, top, width, height] = this.imageBounds;
    const x = (event.clientX - box.left - left) / width * (grid.columns - 1), y = (1 - (event.clientY - box.top - top) / height) * (grid.rows - 1);
    if (x < 0 || y < 0 || x > grid.columns - 1 || y > grid.rows - 1) { this.onInspect(null); return; }
    const at = Math.round(y) * grid.columns + Math.round(x), scales = localScales(grid.frame.origin);
    this.onInspect({ lng: grid.frame.origin[0] + (grid.origin[0] + x * grid.spacingM) / scales[0],
      lat: grid.frame.origin[1] + (grid.origin[1] + y * grid.spacingM) / scales[1], height: grid.height[at], support: grid.support[at] });
  }
  private draw() {
    const canvas = this.mode === '3d' ? this.canvas : this.flat, box = canvas.parentElement!.getBoundingClientRect();
    const ratio = Math.min(2, devicePixelRatio || 1), width = Math.max(1, Math.round(box.width)), height = Math.max(1, Math.round(box.height));
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    if (this.mode === 'map') {
      const ctx = this.flat.getContext('2d')!; ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
      if (!this.grid || !this.raster) return;
      const grid = this.grid, scale = Math.min((width - 48) / (grid.columns - 1), (height - 48) / (grid.rows - 1));
      const w = (grid.columns - 1) * scale, h = (grid.rows - 1) * scale, left = (width - w) / 2, top = (height - h) / 2;
      this.imageBounds = [left, top, w, h]; ctx.imageSmoothingEnabled = true;
      this.geographicContext(ctx, left, top, scale);
      // Raster pixels are centred on the same grid nodes as contours and picks.
      ctx.globalAlpha = this.grid.context?.features.length ? .84 : 1;
      ctx.drawImage(this.raster, left - scale / 2, top - scale / 2, grid.columns * scale, grid.rows * scale); ctx.globalAlpha = 1; this.contours(ctx, left, top, scale);
      if (this.showPoints) { const scales = localScales(grid.frame.origin); ctx.fillStyle = '#163f37b3';
        for (const [lng, lat] of grid.observations) { const x = ((lng - grid.frame.origin[0]) * scales[0] - grid.origin[0]) / grid.spacingM;
          const y = ((lat - grid.frame.origin[1]) * scales[1] - grid.origin[1]) / grid.spacingM;
          ctx.beginPath(); ctx.arc(left + x * scale, top + (grid.rows - 1 - y) * scale, 1.5, 0, Math.PI * 2); ctx.fill(); } }
      ctx.fillStyle = '#385649'; ctx.font = '600 12px sans-serif'; ctx.fillText('北 ↑', width - 42, 22);
      return;
    }
    const gl = this.gl, program = this.program; if (!gl || !program) return;
    gl.viewport(0, 0, canvas.width, canvas.height); gl.clearColor(.918, .941, .914, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (!this.grid || !this.mesh) return;
    gl.enable(gl.DEPTH_TEST); gl.useProgram(program);
    // A portrait phone sees less across; step back so the whole area fits.
    const range = this.distance * Math.max(1, 1.2 * height / width);
    const eye = [range * Math.cos(this.pitch) * Math.sin(this.yaw), range * Math.sin(this.pitch), range * Math.cos(this.pitch) * Math.cos(this.yaw)];
    const z = normalized(eye), x = normalized(cross([0, 1, 0], z)), y = cross(z, x);
    const view = new Float32Array([x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0, -dot(x, eye), -dot(y, eye), -dot(z, eye), 1]);
    const f = 1 / Math.tan(Math.PI / 8), near = .05, far = 20;
    const projection = new Float32Array([f / (width / height), 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1, 0, 0, 2 * far * near / (near - far), 0]);
    gl.uniformMatrix4fv(gl.getUniformLocation(program, 'u_camera'), false, multiply(projection, view));
    const attributes = (buffer: WebGLBuffer | null) => { gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      for (const [i, name] of ['a_position', 'a_normal', 'a_color'].entries()) { const location = gl.getAttribLocation(program, name); gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location, 3, gl.FLOAT, false, 36, i * 12); } };
    attributes(this.mesh); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indexes); gl.uniform1f(gl.getUniformLocation(program, 'u_point_size'), 1); gl.drawElements(gl.TRIANGLES, this.indexCount, gl.UNSIGNED_SHORT, 0);
    if (this.showPoints && this.points) { attributes(this.points); gl.uniform1f(gl.getUniformLocation(program, 'u_point_size'), 3 * ratio); gl.drawArrays(gl.POINTS, 0, this.pointCount); }
  }
}
