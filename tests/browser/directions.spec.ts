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
    const styles = feature.getStyle()(feature, 1).slice(2);
    const direction = map.direction.getFeatures()[0], pointer = direction.getStyle()(direction, 1);
    return { arrows: styles.length, firstRotation: styles[0].getImage().getRotation(), lastRotation: styles.at(-1).getImage().getRotation(),
      rotates: styles.every((s: any) => s.getImage().getRotateWithView()), label: pointer[2].getText().getText(), line: pointer[0].getGeometry().getCoordinates() };
  });
  expect(rendered.arrows).toBeGreaterThan(4);
  expect(rendered.firstRotation).toBeCloseTo(Math.PI / 2); expect(rendered.lastRotation).toBeCloseTo(0);
  expect(rendered.rotates).toBe(true); expect(rendered.label).toBe('手机指向');
  expect(rendered.line[1][0] - rendered.line[0][0]).toBeCloseTo(30);
  expect(rendered.line[1][1]).toBeCloseTo(rendered.line[0][1]);
  await page.locator('#north-button').click();
  await expect.poll(() => page.evaluate(() => (window as any).dirmap.campusMap.direction.getFeatures().length)).toBe(1);
  await page.screenshot({ path: '/tmp/dirmap-route-and-position-arrows.png' });
  await page.evaluate(() => {
    (window as any).dirmap.sensors.onHeading({ degrees: 180, timestamp: Date.now(), source: 'course', reference: 'true' });
  });
  const course = await page.evaluate(() => {
    const map = (window as any).dirmap.campusMap, f = map.direction.getFeatures()[0], s = f.getStyle()(f, 1);
    return { source: f.get('source'), label: s[2].getText().getText(), dashed: s[1].getStroke().getLineDash(), line: s[0].getGeometry().getCoordinates() };
  });
  expect(course.source).toBe('course'); expect(course.label).toBe('行进方向'); expect(course.dashed).toEqual([4, 3]);
  expect(course.line[1][1] - course.line[0][1]).toBeCloseTo(-30);
  await page.evaluate(() => (window as any).dirmap.sensors.onLocationFreshness(false));
  expect(await page.evaluate(() => (window as any).dirmap.campusMap.direction.getFeatures().length)).toBe(0);
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
