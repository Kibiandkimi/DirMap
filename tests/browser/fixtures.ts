import { test as base } from '@playwright/test';

export { expect, type Page } from '@playwright/test';

// Public basemap tiles are only decoration in these checks. An unreachable
// tile server would otherwise hold the page load event and make results
// depend on the network, so only the local test servers are reachable.
export const test = base.extend({
  context: async ({ context }, use) => {
    await context.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname), route => route.abort('internetdisconnected'));
    await use(context);
  },
});
