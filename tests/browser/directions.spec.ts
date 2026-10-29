import { test, expect } from './fixtures';

test('route and own-heading arrows respect direction, map rotation, freshness and saved switches', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.control-row').first()).toBeVisible();
  const rendered = await page.evaluate(() => {
    const { campusMap: map, sensors } = (window as any).dirmap;
    map.setRoutes([{ teamId: '测试队', geometry: [[113.93, 22.535], [113.934, 22.535], [113.934, 22.537]], people: 2 }]);
    map.map.getView().setRotation(Math.PI / 4);
    sensors.onPosition({ timestamp: Date.now(), coords: { longitude: 113.932, latitude: 22.535, accuracy: 6 } });
    sensors.onHeading({ degrees: 90, timestamp: Date.now(), source: 'compass', reference: 'true' });
    const feature = map.routes.getFeatures().find((f: any) => f.get('routeSection'));
    // Chevrons: the middle vertex leads the two arms in the travel direction.
    const pointing = (style: any) => { const [left, tip, right] = style.getGeometry().getCoordinates();
      return [tip[0] - (left[0] + right[0]) / 2, tip[1] - (left[1] + right[1]) / 2]; };
    const styles = feature.getStyle()(feature, 1).slice(2);
    const direction = map.direction.getFeatures()[0], beam = direction.getStyle()(direction, 1);
    const point = map.location.getFeatures().find((f: any) => f.getGeometry().getType() === 'Point').getGeometry().getCoordinates();
    const centre = (ring: number[][]) => { const open = ring.slice(0, -1);
      return [open.reduce((a, p) => a + p[0], 0) / open.length - point[0], open.reduce((a, p) => a + p[1], 0) / open.length - point[1]]; };
    return { arrows: styles.length, first: pointing(styles[0]), last: pointing(styles.at(-1)),
      white: styles.every((s: any) => s.getStroke().getColor() === '#fff' && !s.getImage()),
      beams: beam.length, beamCentre: centre(beam[0].getGeometry().getCoordinates()[0]),
      texts: beam.filter((s: any) => s.getText()).length, dotShown: !!map.location.getFeatures().find((f: any) => f.getGeometry().getType() === 'Point').getStyle()() };
  });
  expect(rendered.arrows).toBeGreaterThan(4); expect(rendered.white).toBe(true);
  expect(rendered.first[0]).toBeGreaterThan(1); expect(Math.abs(rendered.first[1])).toBeLessThan(1e-6);
  expect(rendered.last[1]).toBeGreaterThan(1); expect(Math.abs(rendered.last[0])).toBeLessThan(1e-6);
  // The heading is a beam to the east of the dot, with no words on the map.
  expect(rendered.beams).toBe(3); expect(rendered.texts).toBe(0); expect(rendered.dotShown).toBe(true);
  expect(rendered.beamCentre[0]).toBeGreaterThan(10); expect(Math.abs(rendered.beamCentre[1])).toBeLessThan(1e-6);
  await page.locator('#north-button').click();
  await expect.poll(() => page.evaluate(() => (window as any).dirmap.campusMap.direction.getFeatures().length)).toBe(1);
  // OpenLayers keeps the previous vector batch during the north-up animation.
  // Wait for the final frame before judging the visible direction marker.
  await expect.poll(() => page.evaluate(() => (window as any).dirmap.campusMap.map.getView().getAnimating())).toBe(false);
  await page.evaluate(() => (window as any).dirmap.campusMap.map.renderSync());
  await page.screenshot({ path: '/tmp/dirmap-route-and-position-arrows.png' });
  await page.evaluate(() => {
    (window as any).dirmap.sensors.onHeading({ degrees: 180, timestamp: Date.now(), source: 'course', reference: 'true' });
  });
  const course = await page.evaluate(() => {
    const map = (window as any).dirmap.campusMap, f = map.direction.getFeatures()[0], s = f.getStyle()(f, 1);
    const point = map.location.getFeatures().find((f: any) => f.getGeometry().getType() === 'Point').getGeometry().getCoordinates();
    const tip = s[0].getGeometry().getCoordinates()[0][0];
    return { source: f.get('source'), shapes: s.length, texts: s.filter((x: any) => x.getText()).length,
      tip: [tip[0] - point[0], tip[1] - point[1]], dotShown: !!map.location.getFeatures().find((f: any) => f.getGeometry().getType() === 'Point').getStyle()() };
  });
  // Walking direction turns the dot itself into an arrow, pointing south here.
  expect(course.source).toBe('course'); expect(course.shapes).toBe(1); expect(course.texts).toBe(0);
  expect(course.tip[1]).toBeCloseTo(-12); expect(Math.abs(course.tip[0])).toBeLessThan(1e-6); expect(course.dotShown).toBe(false);
  await page.evaluate(() => (window as any).dirmap.campusMap.map.renderSync());
  await page.screenshot({ path: '/tmp/dirmap-walking-arrow.png' });
  await page.evaluate(() => (window as any).dirmap.sensors.onLocationFreshness(false));
  expect(await page.evaluate(() => (window as any).dirmap.campusMap.direction.getFeatures().length)).toBe(0);
  expect(await page.evaluate(() => !!(window as any).dirmap.campusMap.location.getFeatures().find((f: any) => f.getGeometry().getType() === 'Point').getStyle()())).toBe(true);
  expect(await page.evaluate(() => (window as any).dirmap.campusMap.location.getFeatures().length)).toBe(2);
  await page.locator('#route-arrows').uncheck(); await page.locator('#location-direction').uncheck();
  const styles = await page.evaluate(() => {
    const f = (window as any).dirmap.campusMap.routes.getFeatures().find((f: any) => f.get('routeSection'));
    return f.getStyle()(f, 1).length;
  });
  expect(styles).toBe(2);
  await page.reload();
  await expect(page.locator('#route-arrows')).not.toBeChecked();
  await expect(page.locator('#location-direction')).not.toBeChecked();
});

test('phone direction markers keep the geographic bearing when the map turns and zooms', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.control-row', { state: 'attached' });
  const state = await (await page.request.get('/api/maps/szu-1')).json();
  await page.evaluate(async state => {
    const { campusMap: map, sensors } = (window as any).dirmap;
    map.setOverlays([state], .8, state.assetId);
    map.setRoutes([{ teamId: '测试队', geometry: [[113.932, 22.535], [113.933, 22.535], [113.933, 22.536]], people: 2 }]);
    sensors.onPosition({ timestamp: Date.now(), coords: { longitude: 113.9325, latitude: 22.535, accuracy: 6 } });
    const point = map.location.getFeatures().find((f: any) => f.getGeometry().getType() === 'Point').getGeometry().getCoordinates();
    map.map.getView().cancelAnimations(); map.map.getView().setCenter(point);
    await new Promise<void>(resolve => { map.map.once('rendercomplete', () => resolve()); map.map.render(); });
  }, state);
  // Screen-space checks: a correct world bearing must rotate on the screen,
  // while the marker's apparent size must stay the same when zooming.
  for (const source of ['compass', 'course']) {
    let firstReach: number | null = null;
    for (const zoom of [17, 19]) for (const rotation of [0, Math.PI / 2, -Math.PI / 4]) {
      const shown = await page.evaluate(({ source, zoom, rotation }) => {
        const { campusMap: map, sensors } = (window as any).dirmap;
        const view = map.map.getView(); view.setZoom(zoom); view.setRotation(rotation);
        sensors.onHeading({ degrees: 90, timestamp: Date.now(), source, reference: 'true' });
        map.map.renderSync();
        const point = map.location.getFeatures().find((f: any) => f.getGeometry().getType() === 'Point').getGeometry().getCoordinates();
        const screen = (p: number[]) => map.map.getPixelFromCoordinate(p);
        const origin = screen(point), east = screen([point[0] + view.getResolution() * 30, point[1]]);
        const direction = map.direction.getFeatures()[0], styles = direction.getStyle()(direction, view.getResolution());
        const vertices = styles.flatMap((s: any) => s.getGeometry().getCoordinates()[0].slice(0, -1));
        const centroid = screen([vertices.reduce((sum: number, p: number[]) => sum + p[0], 0) / vertices.length,
          vertices.reduce((sum: number, p: number[]) => sum + p[1], 0) / vertices.length]);
        const actual = [centroid[0] - origin[0], centroid[1] - origin[1]], expected = [east[0] - origin[0], east[1] - origin[1]];
        const length = Math.hypot(...actual), expectedLength = Math.hypot(...expected);
        // A walker arrow has most of its area behind its tip; use the tip's
        // bearing, while a compass beam's centre must be in front of us.
        const target = source === 'course' ? screen(vertices[0]) : centroid;
        const vector = [target[0] - origin[0], target[1] - origin[1]];
        return { aligned: (vector[0] * expected[0] + vector[1] * expected[1]) / (Math.hypot(...vector) * expectedLength),
          reach: Math.max(...vertices.map((p: number[]) => Math.hypot(screen(p)[0] - origin[0], screen(p)[1] - origin[1]))),
          beamInFront: source !== 'compass' || length > 10,
          textOnMap: styles.some((s: any) => !!s.getText()) };
      }, { source, zoom, rotation });
      expect(shown.aligned).toBeCloseTo(1, 6); expect(shown.beamInFront).toBe(true);
      expect(shown.textOnMap).toBe(false);
      firstReach ??= shown.reach;
      expect(shown.reach).toBeCloseTo(firstReach, 4);
      expect(shown.reach).toBeGreaterThan(10); expect(shown.reach).toBeLessThan(60);
    }
    await page.screenshot({ path: `/tmp/dirmap-phone-${source}.png` });
  }
});
