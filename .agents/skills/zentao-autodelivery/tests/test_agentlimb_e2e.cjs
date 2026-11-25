const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { mkdtempSync, rmSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');
const { checkPage, runSession, scenarioResult } = require('../scripts/agentlimb_e2e.cjs');

const profile = {
  browser: 'ZenTao-E2E', baseUrl: 'http://localhost:5000', loginPath: '/login',
  platformPath: '/platform', entryPath: '/overview', username: 'test', workArea: 'QA',
  checks: [{ name: 'wells', text: '井列表' }],
};

test('an unavailable test profile never opens a tab', async () => {
  const calls = [];
  const call = async name => {
    calls.push(name);
    return { connection: { activeBrowserProfile: { id: 'personal', label: 'Personal' } } };
  };
  await assert.rejects(runSession(call, { browser: 'ZenTao-E2E' }), { stage: 'browser-profile' });
  assert.deepEqual(calls, ['ping']);
});

test('a business assertion asks for code repair', () => {
  assert.deepEqual(scenarioResult(new assert.AssertionError({ message: 'the chart stayed empty' })),
    { status: 'failed', stage: 'acceptance', repair_required: true });
});

test('an ordinary browser error cannot request code repair', () => {
  assert.deepEqual(scenarioResult(new Error('browser disconnected')),
    { status: 'blocked', stage: 'scenario', repair_required: false });
});

test('preflight runs through the MCP protocol and closes only its own tab', () => {
  const directory = mkdtempSync(join(tmpdir(), 'agentlimb-e2e-'));
  const host = join(directory, 'host.cjs');
  writeFileSync(host, `
    const assert = require('node:assert/strict');
    const rl = require('node:readline').createInterface({ input: process.stdin });
    let url, closed = false;
    rl.on('line', line => {
      const request = JSON.parse(line);
      if (request.id == null) return;
      const reply = result => console.log(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }));
      if (request.method === 'initialize') return reply({});
      const { name, arguments: args } = request.params;
      let data = { ok: true };
      if (name === 'ping') {
        data.connection = { activeBrowserProfile: { id: 'test-profile', label: 'ZenTao-E2E' } };
      } else {
        assert.equal(args.browser, 'test-profile');
        if (name !== 'tab_new' && name !== 'say') assert.equal(args.tabId, 41);
        if (name === 'tab_new') data.tab = { id: 41 };
        if (name === 'navigate') url = args.url;
        if (name === 'javascript_eval') data.result = args.expression.includes('workArea:') ? { url, workArea: 'QA' } : true;
        if (name === 'tab_close') closed = true;
        if (name === 'say') assert.equal(closed, true);
      }
      reply({ content: [{ type: 'text', text: JSON.stringify(data) }] });
    });
  `);
  try {
    const result = spawnSync(process.execPath, [require.resolve('../scripts/agentlimb_e2e.cjs')], {
      input: JSON.stringify({ ...profile, host }), encoding: 'utf8', timeout: 5000,
    });
    assert.equal(result.status, 0, result.stderr || result.stdout);
    assert.deepEqual(JSON.parse(result.stdout), { status: 'passed', stage: 'preflight', checks: ['wells'] });
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test('the runner does not start business assertions on a different candidate asset', async () => {
  let url;
  const browser = {
    async navigate(value) { url = value; },
    async evaluate(expression) {
      if (expression.includes('workArea:')) return { url, workArea: 'QA' };
      if (expression.includes('arrayBuffer()')) return 'b'.repeat(64);
      return true;
    },
  };
  const resources = [{ app: 'shell', url: 'http://localhost:5000/index.js', sha256: 'a'.repeat(64) }];
  await assert.rejects(checkPage(browser, { ...profile, resources }, () => assert.fail('unexpected scenario')),
    { stage: 'candidate-resource', check: 'shell' });
});

test('expired local session can submit Chrome autofill without reading credentials', async () => {
  let url;
  let submitted = 0;
  const platform = profile.baseUrl + profile.platformPath;
  const login = profile.baseUrl + profile.loginPath;
  const browser = {
    async navigate(value) { url = !submitted && value !== login ? login : value; },
    async evaluate(expression) {
      if (!expression.includes('workArea:')) {
        if (expression.includes('.large-button')) {
          submitted += 1;
          url = platform;
        }
        return true;
      }
      return url === login ? { url, login: true, autofilled: true } : { url, workArea: 'QA' };
    },
  };
  assert.equal((await checkPage(browser, profile)).status, 'passed');
  assert.equal(submitted, 1);
});

test('the existing Chrome session reaches the entry with Wujie controls in nested roots', async () => {
  let url;
  const visible = text => ({ textContent: text, getClientRects: () => [1] });
  const area = visible('QA');
  const wells = visible('井列表');
  const nested = { querySelectorAll: selector => selector === '*' || selector === 'body *' ? [area, wells]
    : selector.includes('.project-name') ? [area] : [], querySelector: () => null };
  const host = { ...visible(''), shadowRoot: nested };
  const top = { querySelectorAll: selector => selector === '*' ? [host] : [], querySelector: () => null };
  const browser = {
    async navigate(value) { url = value; },
    async evaluate(expression) {
      return runInNewContext(expression, { document: top, location: { href: url },
        getComputedStyle: () => ({ visibility: 'visible' }) });
    },
  };
  const result = await checkPage(browser, profile);
  assert.equal(result.status, 'passed');
  assert.deepEqual(result.checks, ['wells']);
});

test('a page that mounts only in the foreground is checked and the original tab is restored', async () => {
  let active = 9;
  let url;
  const actions = [];
  const call = async (name, args = {}) => {
    if (name === 'ping') return { connection: { activeBrowserProfile: { id: 'test', label: 'ZenTao-E2E' } } };
    if (name === 'tab_new') return { tab: { id: 41, windowId: 5 } };
    if (name === 'navigate') url = args.url;
    if (name === 'javascript_eval') {
      if (args.expression.includes('workArea:')) return { result: { url, workArea: 'QA' } };
      if (args.expression === 'document.visibilityState') return { result: active === 41 ? 'visible' : 'hidden' };
      return { result: active === 41 };
    }
    if (name === 'tabs_context') {
      if (args.tabId) return { tab: { id: args.tabId, active: active === args.tabId } };
      return { tabs: [{ id: 9, windowId: 5, active: active === 9 }] };
    }
    if (name === 'tab_activate') active = args.tabId;
    if (name === 'tab_activate' || name === 'tab_close') actions.push([name, args.tabId]);
    return {};
  };
  const result = await runSession(call, profile);
  assert.equal(result.status, 'passed');
  assert.deepEqual(actions, [['tab_activate', 41], ['tab_activate', 9], ['tab_close', 41]]);
});
