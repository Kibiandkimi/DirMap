import Map from 'ol/Map.js';
import View from 'ol/View.js';
import TileLayer from 'ol/layer/Tile.js';
import ImageLayer from 'ol/layer/Image.js';
import VectorLayer from 'ol/layer/Vector.js';
import OSM from 'ol/source/OSM.js';
import VectorSource from 'ol/source/Vector.js';
import ImageStatic from 'ol/source/ImageStatic.js';
import Feature from 'ol/Feature.js';
import Point from 'ol/geom/Point.js';
import LineString from 'ol/geom/LineString.js';
import CircleGeometry from 'ol/geom/Circle.js';
import GeoJSON from 'ol/format/GeoJSON.js';
import { Circle, Fill, Stroke, Style, Text, RegularShape } from 'ol/style.js';
import { fromLonLat, toLonLat, transformExtent } from 'ol/proj.js';
import { ScaleLine, defaults } from 'ol/control.js';
import type { Anchor, Approach, Control, MapState, Pixel, Route, Surface } from './types';
import type { HeadingSample } from './heading';
import { routeArrows } from './route-arrows';
import { mapAssetUrl } from './workbench';

export const teamColors = ['#ec693f', '#416edb', '#9370ca', '#159386', '#bb8426', '#d95492', '#56974e', '#a5513f', '#54869f', '#887760'];
/** Someone shown on the map: a group member or, for a teacher, a student. */
export interface MapPerson { id: string; lnglat: Pixel; label: string; color: string; accuracyM?: number | null; help?: boolean; self?: boolean; stale?: boolean }
export class CampusMap {
  private base = new TileLayer({ source: new OSM(), opacity: .65 });
  private referenceSource = new VectorSource({ attributions: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>' });
  private routes = new VectorSource();
  private controls = new VectorSource();
  private location = new VectorSource();
  private endpoints = new VectorSource();
  private approaches = new VectorSource();
  private fieldChecks = new VectorSource();
  private people = new VectorSource();
  private picks = new VectorSource();
  private anchorPoints = new VectorSource();
  private direction = new VectorSource();
  private arrowsVisible = true;
  private directionVisible = true;
  private heading: HeadingSample | null = null;
  private position: { point: number[]; fresh: boolean } | null = null;
  private overlays: ImageLayer<ImageStatic>[] = [];
  private terrain: ImageLayer<ImageStatic> | null = null;
  map: Map;
  onClick: (lnglat: Pixel, controlId: string | null) => void = () => {};
  onDrag: () => void = () => {};

  constructor(target: HTMLElement) {
    const reference = new VectorLayer({ source: this.referenceSource, style: feature => {
      const kind = feature.get('kind');
      // Park greenery stays under paths and buildings drawn from the same data.
      if (kind === 'green') return new Style({ fill: new Fill({ color: '#dcebd0b3' }), zIndex: 0 });
      if (kind === 'waterway') return new Style({ stroke: new Stroke({ color: '#8cbdc9', width: 2 }), zIndex: 1 });
      if (kind === 'pitch') return new Style({ fill: new Fill({ color: '#e5ecd9' }), stroke: new Stroke({ color: '#bfccb4', width: .8 }), zIndex: 1 });
      if (kind === 'water') return new Style({ fill: new Fill({ color: '#b4dce5' }), stroke: new Stroke({ color: '#8cbdc9', width: 1 }), zIndex: 1 });
      if (kind === 'building') return new Style({ fill: new Fill({ color: '#d4dace' }), stroke: new Stroke({ color: '#adb8a9', width: .6 }), zIndex: 3 });
      const highway = feature.get('highway');
      return new Style({ stroke: new Stroke({ color: ['footway', 'path', 'steps'].includes(highway) ? '#cbd3c4' : '#fcfcf7', width: ['primary', 'secondary'].includes(highway) ? 6 : 2.4 }), zIndex: 2 });
    }});
    this.map = new Map({ target, layers: [this.base, reference,
      new VectorLayer({ source: this.routes, zIndex: 30 }), new VectorLayer({ source: this.controls, zIndex: 40 }),
      new VectorLayer({ source: this.endpoints, zIndex: 45 }), new VectorLayer({ source: this.approaches, zIndex: 46 }),
      new VectorLayer({ source: this.fieldChecks, zIndex: 47 }),
      new VectorLayer({ source: this.people, zIndex: 48 }),
      new VectorLayer({ source: this.anchorPoints, zIndex: 49 }),
      new VectorLayer({ source: this.direction, zIndex: 50 }),
      new VectorLayer({ source: this.location, zIndex: 51 }),
      new VectorLayer({ source: this.picks, zIndex: 60 })],
      view: new View({ center: fromLonLat([113.9304, 22.535]), zoom: 16.2, minZoom: 13, maxZoom: 21, enableRotation: true }),
      controls: defaults({ rotate: false }).extend([new ScaleLine({ units: 'metric' })]) });
    this.map.on('singleclick', event => {
      const feature = this.map.forEachFeatureAtPixel(event.pixel, feature => feature, { hitTolerance: 9 });
      this.onClick(toLonLat(event.coordinate) as Pixel, feature?.get('controlId') ?? null);
    });
    this.map.on('pointerdrag', () => this.onDrag());
  }

  setReference(data: object) { this.referenceSource.clear(); this.referenceSource.addFeatures(new GeoJSON().readFeatures(data, { featureProjection: 'EPSG:3857' })); }
  setStandardMap(visible: boolean) { this.base.setVisible(visible); }
  setControls(controls: Control[], surfaces: Surface[] = []) {
    this.controls.clear();
    const labels = Object.fromEntries(surfaces.map(s => [s.id, s.label]));
    for (const c of controls) {
      if (!c.lnglat || c.state === 'rejected') continue;
      const feature = new Feature({ geometry: new Point(fromLonLat(c.lnglat)), controlId: c.id });
      const color = c.required ? '#9c3fa6' : '#798773';
      feature.setStyle(new Style({ image: new Circle({ radius: 12, fill: new Fill({ color: '#fffffff2' }), stroke: new Stroke({ color, width: 2.5, lineDash: c.state === 'candidate' ? [5, 2] : undefined }) }),
        text: new Text({ text: c.label.replace('待核', '?')+(c.surfaceId && c.surfaceId !== 'ground' ? ' · '+(labels[c.surfaceId] ?? '另层') : ''), font: '600 12px sans-serif', fill: new Fill({ color }), offsetY: -23, stroke: new Stroke({ color: '#fff', width: 3 }) }) }));
      this.controls.addFeature(feature);
    }
  }
  setOverlays(states: MapState[], opacity: number, activeId: string) {
    for (const layer of this.overlays) this.map.removeLayer(layer);
    this.overlays = [];
    for (const state of [...states].sort((a, b) => Number(a.assetId === activeId) - Number(b.assetId === activeId))) {
      if (!state.extent || !state.registration || state.registrationStale) continue;
      const layer = new ImageLayer({ source: new ImageStatic({ url: mapAssetUrl(state, 'overlay.png'), imageExtent: state.extent, projection: 'EPSG:3857' }),
        opacity: state.assetId === activeId ? opacity : opacity * .6, zIndex: 10 });
      this.overlays.push(layer); this.map.addLayer(layer);
    }
  }
  setReleaseOverlay(id: string, extent: number[], opacity: number) {
    const layer = new ImageLayer({ source: new ImageStatic({ url: `/api/releases/${id}/overlay.png`, imageExtent: extent, projection: 'EPSG:3857' }), opacity, zIndex: 10 });
    this.overlays.push(layer); this.map.addLayer(layer); this.fit(extent);
  }
  setOpacity(opacity: number) { for (const layer of this.overlays) layer.setOpacity(opacity); }
  setTerrain(state: MapState | null, visible: boolean) {
    if (this.terrain) this.map.removeLayer(this.terrain);
    this.terrain = null;
    if (!state?.extent || state.registrationStale || !visible) return;
    this.terrain = new ImageLayer({ source: new ImageStatic({ url: mapAssetUrl(state, 'terrain-warp.png'), imageExtent: state.extent, projection: 'EPSG:3857' }), opacity: .8, zIndex: 20 });
    this.map.addLayer(this.terrain);
  }
  setRoutes(routes: Route[], selected: string | null = null) {
    this.routes.clear();
    routes.forEach((route, i) => {
      if (selected && route.teamId !== selected) return;
      const sections = route.surfaceSections ?? [{ surfaceId: 'ground', level: 0, startIndex: 0, endIndex: route.geometry.length-1 }];
      for (const section of sections) {
        const points = route.geometry.slice(section.startIndex, section.endIndex+1);
        if (points.length < 2) continue;
        const feature = new Feature(new LineString(points.map(p => fromLonLat(p))));
        const color = teamColors[i % teamColors.length];
        const lines = [new Style({ stroke: new Stroke({ color: '#ffffffe6', width: 7 }) }), new Style({ stroke: new Stroke({ color, width: 4, lineDash: section.surfaceId === 'ground' ? undefined : section.level < 0 ? [3, 6] : [12, 5] }) })];
        let lastResolution = 0, arrows: Style[] = [];
        feature.set('routeSection', true);
        feature.setStyle((_feature, resolution) => {
          if (!this.arrowsVisible) return lines;
          if (resolution !== lastResolution) {
            lastResolution = resolution;
            arrows = routeArrows(feature.getGeometry()!.getCoordinates(), resolution * 85).map(arrow => new Style({
              geometry: new Point(arrow.point), zIndex: 1,
              image: new RegularShape({ points: 3, radius: 7, rotation: arrow.rotation, rotateWithView: true,
                fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 2 }) }),
            }));
          }
          return [...lines, ...arrows];
        });
        this.routes.addFeature(feature);
      }
      for (const transition of route.transitions ?? []) {
        const marker = new Feature(new Point(fromLonLat(transition.lnglat)));
        marker.setStyle(new Style({ image: new Circle({ radius: 7, fill: new Fill({ color: '#fff' }), stroke: new Stroke({ color: teamColors[i % teamColors.length], width: 2 }) }), text: new Text({ text: transition.label, offsetY: 16, font: '11px sans-serif', fill: new Fill({ color: '#244d3b' }), stroke: new Stroke({ color: '#fff', width: 3 }) }) }));
        this.routes.addFeature(marker);
      }
    });
  }
  setRouteArrows(visible: boolean) { this.arrowsVisible = visible; this.routes.changed(); }
  setDirectionVisible(visible: boolean) { this.directionVisible = visible; this.drawDirection(); }
  setLocationHeading(sample: HeadingSample | null) {
    const next = sample && Number.isFinite(sample.degrees) ? sample : null;
    const unchanged = next?.degrees === this.heading?.degrees && next?.source === this.heading?.source && next?.reference === this.heading?.reference;
    this.heading = next;
    if (!unchanged) this.drawDirection();
  }
  private drawDirection() {
    if (!this.directionVisible || !this.heading || !this.position?.fresh) {
      // Sensors report missing headings at 20 Hz. Clearing even an empty
      // source invalidates OpenLayers and redraws the whole map every time.
      if (!this.direction.isEmpty()) this.direction.clear();
      return;
    }
    this.direction.clear();
    const point = this.position.point, sample = this.heading, angle = sample.degrees * Math.PI / 180;
    const color = sample.source === 'compass' ? '#246ed1' : '#187c6c';
    const marker = new Feature(new Point(point));
    marker.set('source', sample.source);
    marker.setStyle((_feature, resolution) => {
      const tip = [point[0] + Math.sin(angle) * resolution * 30, point[1] + Math.cos(angle) * resolution * 30];
      return [new Style({ geometry: new LineString([point, tip]), stroke: new Stroke({ color: '#fff', width: 6 }) }),
        new Style({ geometry: new LineString([point, tip]), stroke: new Stroke({ color, width: 3, lineDash: sample.source === 'course' ? [4, 3] : undefined }) }),
        new Style({ geometry: new Point(tip), image: new RegularShape({ points: 3, radius: 7, rotation: angle, rotateWithView: true, fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 2 }) }),
          text: new Text({ text: sample.source === 'compass' ? '手机指向' : '行进方向', offsetY: 16, font: '11px sans-serif', fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 3 }) }) })];
    });
    this.direction.addFeature(marker);
  }
  setPickPreview(lnglat: Pixel | null, label = '待确认', valid = true) {
    this.picks.clear();
    if (!lnglat) return;
    const color = valid ? '#e35e22' : '#c32c30';
    const feature = new Feature(new Point(fromLonLat(lnglat)));
    feature.setStyle(new Style({ image: new Circle({ radius: 11, fill: new Fill({ color: '#ffffff88' }), stroke: new Stroke({ color, width: 3 }) }),
      text: new Text({ text: `＋ ${label}`, offsetY: -24, font: 'bold 13px sans-serif', fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 4 }) }) }));
    this.picks.addFeature(feature);
  }
  setAnchors(anchors: Anchor[]) {
    this.anchorPoints.clear();
    anchors.forEach((anchor, i) => {
      const color = anchor.purpose === 'fit' ? '#126b93' : '#a26d13';
      const feature = new Feature(new Point(fromLonLat(anchor.lnglat)));
      feature.setStyle(new Style({ image: new Circle({ radius: 6, fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 2 }) }),
        text: new Text({ text: `${anchor.purpose === 'fit' ? '拟合' : '核查'} ${i + 1}`, offsetY: -17, font: '12px sans-serif', fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 3 }) }) }));
      this.anchorPoints.addFeature(feature);
    });
  }
  setApproaches(approaches: Approach[]) {
    this.approaches.clear();
    for (const a of approaches) {
      if (a.offsetM < .1) continue;
      const feature = new Feature({ geometry: new Point(fromLonLat(a.lnglat)), controlId: a.controlId });
      feature.setStyle(new Style({ image: new Circle({ radius: 5, fill: new Fill({ color: '#e58824' }), stroke: new Stroke({ color: '#fff', width: 2 }) }),
        text: new Text({ text: a.automatic ? '建议到访' : '到访', offsetY: 16, font: '11px sans-serif', fill: new Fill({ color: '#864809' }), stroke: new Stroke({ color: '#fff', width: 3 }) }) }));
      this.approaches.addFeature(feature);
    }
  }
  /** GPS check points: a line from the registered position to the phone's
   * fix, coloured against the median/P95 thresholds, or grey if not counted. */
  setFieldChecks(checks: { lnglat: Pixel; predictedLnglat: Pixel | null; errorM: number | null; usable: boolean }[]) {
    this.fieldChecks.clear();
    for (const check of checks) {
      const colour = !check.usable || check.errorM === null ? '#8c968a' : check.errorM <= 3 ? '#2f8a4f' : check.errorM <= 8 ? '#d08a1f' : '#c9352f';
      if (check.predictedLnglat) {
        const line = new Feature(new LineString([fromLonLat(check.predictedLnglat), fromLonLat(check.lnglat)]));
        line.setStyle(new Style({ stroke: new Stroke({ color: colour, width: 2, lineDash: [4, 4] }) }));
        this.fieldChecks.addFeature(line);
      }
      const point = new Feature(new Point(fromLonLat(check.lnglat)));
      point.setStyle(new Style({ image: new Circle({ radius: 5, fill: new Fill({ color: colour }), stroke: new Stroke({ color: '#fff', width: 2 }) }),
        text: check.errorM === null ? undefined : new Text({ text: `${check.errorM.toFixed(1)} m`, offsetY: -14, font: '11px sans-serif', fill: new Fill({ color: colour }), stroke: new Stroke({ color: '#fff', width: 3 }) }) }));
      this.fieldChecks.addFeature(point);
    }
  }
  setEndpoint(name: 'start' | 'finish', lnglat: Pixel | null) {
    const previous = this.endpoints.getFeatureById(name);
    if (previous) this.endpoints.removeFeature(previous);
    if (!lnglat) return;
    const feature = new Feature(new Point(fromLonLat(lnglat))); feature.setId(name);
    feature.setStyle(new Style({ image: new Circle({ radius: 10, fill: new Fill({ color: name === 'start' ? '#266b54' : '#253b30' }), stroke: new Stroke({ color: '#fff', width: 2 }) }),
      text: new Text({ text: name === 'start' ? '起' : '终', fill: new Fill({ color: '#fff' }), font: 'bold 11px sans-serif' }) }));
    this.endpoints.addFeature(feature);
  }
  setLocation(position: GeolocationPosition, follow: boolean, fresh = true) {
    this.location.clear();
    const color = fresh ? '#347fe5' : '#7b858e';
    const coords: Pixel = [position.coords.longitude, position.coords.latitude], point = fromLonLat(coords);
    const accuracy = new Feature(new CircleGeometry(point, position.coords.accuracy / Math.cos(coords[1] * Math.PI / 180)));
    accuracy.setStyle(new Style({ fill: new Fill({ color: color + '20' }), stroke: new Stroke({ color: color + '50', width: 1 }) }));
    const dot = new Feature(new Point(point));
    dot.setStyle(new Style({ image: new Circle({ radius: 7, fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 3 }) }) }));
    this.location.addFeatures([accuracy, dot]);
    this.position = { point, fresh }; this.drawDirection();
    if (follow) this.map.getView().setCenter(point);
  }
  /** Group members and meeting pins; people without a fix are not drawn. */
  setPeople(people: MapPerson[], pins: { lnglat: Pixel; label: string }[] = []) {
    this.people.clear();
    for (const pin of pins) {
      const feature = new Feature(new Point(fromLonLat(pin.lnglat)));
      feature.setStyle(new Style({ image: new Circle({ radius: 9, fill: new Fill({ color: '#f5c542' }), stroke: new Stroke({ color: '#5b4300', width: 2 }) }),
        text: new Text({ text: `⚑ ${pin.label}`, offsetY: -19, font: '600 12px sans-serif', fill: new Fill({ color: '#5b4300' }), stroke: new Stroke({ color: '#fff', width: 3 }) }) }));
      this.people.addFeature(feature);
    }
    for (const person of people) {
      const point = fromLonLat(person.lnglat), styles: Style[] = [];
      if (person.accuracyM && person.accuracyM > 8 && !person.self) {
        const halo = new Feature(new CircleGeometry(point, person.accuracyM / Math.cos(person.lnglat[1] * Math.PI / 180)));
        halo.setStyle(new Style({ fill: new Fill({ color: person.color + '18' }) }));
        this.people.addFeature(halo);
      }
      if (person.help) styles.push(new Style({ image: new Circle({ radius: 15, fill: new Fill({ color: '#d92d2033' }), stroke: new Stroke({ color: '#d92d20', width: 3 }) }) }));
      styles.push(new Style({ image: new Circle({ radius: person.self ? 6 : 8, fill: new Fill({ color: person.stale ? '#9aa39b' : person.color }), stroke: new Stroke({ color: '#fff', width: 2.5 }) }),
        text: person.self ? undefined : new Text({ text: person.help ? `${person.label} · 求助` : person.label, offsetY: 18, font: '600 12px sans-serif',
          fill: new Fill({ color: person.help ? '#b42318' : '#203a2e' }), stroke: new Stroke({ color: '#fff', width: 3 }) }) }));
      const feature = new Feature({ geometry: new Point(point), personId: person.id });
      feature.setStyle(styles);
      this.people.addFeature(feature);
    }
  }
  centerOn(lnglat: Pixel) { this.map.getView().animate({ center: fromLonLat(lnglat), duration: 300 }); }
  /** Fit a place's [west, south, east, north] range. */
  fitBounds(bbox: [number, number, number, number]) { this.fit(transformExtent(bbox, 'EPSG:4326', 'EPSG:3857')); }
  setHeading(degrees: number) { this.map.getView().setRotation(-degrees * Math.PI / 180); }
  northUp() { this.map.getView().animate({ rotation: 0, duration: 250 }); }
  fit(extent: number[]) { this.map.getView().fit(extent, { padding: [60, 60, 60, 60], duration: 400, maxZoom: 18 }); }
  destroy() { this.map.setTarget(undefined); }
}
