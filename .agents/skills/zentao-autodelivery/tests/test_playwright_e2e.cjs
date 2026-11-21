const assert = require('node:assert/strict');
const { test } = require('node:test');
const { checkPage } = require('../scripts/playwright_e2e.cjs');

const options = {
  baseUrl: 'http://localhost:5000',
  entryPath: '/result-management',
  loginPath: '/login',
  platformPath: '/platform',
  credentials: { username: 'test-account', password: 'test-only-password' },
  checks: [{ name: 'test-well', text: 'Test Well A' }],
};

function fakePage({ showsWell = true, rejectsLogin = false, redirects = false } = {}) {
  let url;
  const actions = [];
  const page = {
    url: () => url,
    async goto(target) {
      url = redirects ? 'https://other.example/login' : target;
      actions.push('navigate');
      return { ok: () => true };
    },
    locator(selector) {
      return {
        async fill() { actions.push(`fill:${selector}`); },
        async click() {
          actions.push('submit');
          if (!rejectsLogin) url = 'http://localhost:5000/platform';
        },
      };
    },
    async waitForURL(predicate) {
      if (!predicate(new URL(url))) throw new Error('Unexpected current URL');
    },
    getByText() {
      return { first: () => ({ async waitFor() { if (!showsWell) throw new Error('No visible text'); } }) };
    },
  };
  return { page, actions };
}

test('checks authenticated entry and required test data on the same browser page', async () => {
  const { page } = fakePage();
  const result = await checkPage(page, options);
  assert.equal(result.stage, 'preflight');
  assert.deepEqual(result.checks, ['test-well']);
});

test('keeps login and test-data failures distinguishable', async () => {
  await assert.rejects(checkPage(fakePage({ rejectsLogin: true }).page, options), { stage: 'login' });
  await assert.rejects(checkPage(fakePage({ showsWell: false }).page, options), { stage: 'test-data', check: 'test-well' });
});

test('does not submit credentials after an unexpected redirect', async () => {
  const { page, actions } = fakePage({ redirects: true });
  await assert.rejects(checkPage(page, options), { stage: 'login' });
  assert.deepEqual(actions, ['navigate']);
});

test('does not pass preflight with a login and entry only', async () => {
  await assert.rejects(checkPage(fakePage().page, { ...options, checks: [] }), { stage: 'test-data-profile' });
});

test('waits for the asynchronous work-area list before opening its menu', async () => {
  const { page } = fakePage();
  const locator = page.locator;
  let areasReady = false;
  let opened = false;
  let selected = false;
  page.locator = selector => {
    if (selector === '.work-area-selector .selector-trigger') {
      return {
        async waitFor() {},
        locator: () => ({ innerText: async () => 'Other area' }),
        async getAttribute() { return opened ? 'true' : 'false'; },
        async click() { assert.equal(areasReady, true); opened = true; },
        getByText: () => ({ async waitFor() { assert.equal(selected, true); } }),
      };
    }
    if (selector === '.work-area-selector-dropdown') {
      return {
        getByRole: () => ({
          async waitFor({ state }) {
            if (state === 'attached') areasReady = true;
            else assert.equal(opened, true);
          },
          async click() { assert.equal(opened, true); selected = true; },
        }),
      };
    }
    return locator(selector);
  };
  const result = await checkPage(page, { ...options, workArea: 'E2E area' });
  assert.equal(result.stage, 'preflight');
});

test('passes the same independent page to the formal acceptance scenario', async () => {
  const { page } = fakePage();
  const result = await checkPage(page, options, async context => {
    assert.deepEqual(Object.keys(context), ['page']);
    assert.equal(context.page, page);
  });
  assert.equal(result.stage, 'acceptance');
});
