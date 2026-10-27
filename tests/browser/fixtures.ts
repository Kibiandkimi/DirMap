import { test as base, type BrowserContext } from '@playwright/test';

export { expect, type Page } from '@playwright/test';

// Public basemap tiles are only decoration in these checks. An unreachable
// tile server would otherwise hold the page load event and make results
// depend on the network, so only the local test servers are reachable.
export async function localOnly(context: BrowserContext) {
  await context.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname), route => route.abort('internetdisconnected'));
  return context;
}

export const test = base.extend({
  context: async ({ context }, use) => {
    await use(await localOnly(context));
  },
});
