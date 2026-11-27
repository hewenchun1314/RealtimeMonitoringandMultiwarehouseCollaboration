const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { once } = require('node:events');
const { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } = require('node:fs');
const { createServer } = require('node:http');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');
const { gzipSync } = require('node:zlib');
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

test('post-merge acceptance rejects an old loaded resource even if a new fetch matches', async () => {
  let url;
  const digest = 'a'.repeat(64);
  const oldDigest = 'b'.repeat(64);
  const browser = {
    async navigate(value) { url = value; },
    async evaluate(expression) {
      if (expression.includes('workArea:')) return { url, workArea: 'QA' };
      if (expression.includes('arrayBuffer()')) return digest;
      if (expression.includes('serverTiming')) return { digest: oldDigest };
      return true;
    },
  };
  const resources = [{ app: 'shell', url: 'http://localhost:5000/index.js', sha256: digest }];
  await assert.rejects(checkPage(browser, { ...profile, resources, postMerge: true }, () => assert.fail('unexpected scenario')),
    { stage: 'candidate-resource', check: 'shell' });
});

test('post-merge acceptance uses the loaded response digest without a second request', async () => {
  let url;
  const digest = 'a'.repeat(64);
  const expectedUrl = 'http://localhost:5000/index.js';
  const window = { frames: { length: 0 }, performance: {
    getEntriesByName(target, kind) {
      assert.equal(target, expectedUrl);
      assert.equal(kind, 'resource');
      return [{ serverTiming: [{ name: 'zentao-resource', description: digest }] }];
    },
  } };
  const browser = {
    async navigate(value) { url = value; },
    async evaluate(expression) {
      if (expression.includes('workArea:')) return { url, workArea: 'QA' };
      if (expression.includes('arrayBuffer()')) assert.fail('separate request');
      if (expression.includes('serverTiming')) return runInNewContext(expression, { window });
      return true;
    },
  };
  const resources = [{ app: 'shell', url: expectedUrl, sha256: digest }];
  const result = await checkPage(browser, { ...profile, resources, postMerge: true }, async () => {});
  assert.deepEqual(result.candidate_resources, resources);
});

test('the startup witness fingerprints the decoded bytes of a chunked asset', async () => {
  const { witnessResponse } = require('../scripts/agentlimb_resource_witness.cjs');
  const content = Buffer.from('console.log("actual resource")');
  const compressed = gzipSync(content);
  const origin = 'http://localhost:5000';
  const server = createServer((request, response) => {
    witnessResponse(response, origin);
    response.writeHead(200, { 'Content-Type': 'text/javascript', 'Content-Encoding': 'gzip' });
    response.write(compressed.subarray(0, 7));
    response.end(compressed.subarray(7));
  }).listen(0, '127.0.0.1');
  try {
    await once(server, 'listening');
    const response = await fetch(`http://127.0.0.1:${server.address().port}/asset.js`);
    const digest = createHash('sha256').update(content).digest('hex');
    assert.equal(response.headers.get('server-timing'), `zentao-resource;desc="${digest}"`);
    assert.equal(response.headers.get('timing-allow-origin'), origin);
    assert.equal(await response.text(), content.toString());
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
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

test('automatic version proof observes both apps from the current launch without refetching', async () => {
  let url;
  const token = 'a'.repeat(32);
  const asset = (app, port, launched = token) => ({
    name: 'http://localhost:' + port + '/' + app + '.js',
    serverTiming: [{ name: 'zentao-launch', description: launched + '.' + app }],
  });
  const assets = [asset('shell', 5000), asset('charts', 30043)];
  const browser = {
    async navigate(value) { url = value; },
    async evaluate(expression) {
      if (expression.includes('workArea:')) return { url, workArea: 'QA' };
      if (expression.includes('fetch(')) assert.fail('separate request');
      if (expression.includes('serverTiming')) {
        return runInNewContext(expression, { URL, window: {
          frames: [], performance: { getEntriesByType: () => assets },
        } });
      }
      return true;
    },
  };
  const options = { ...profile, postMerge: true, checks: [],
    launchProof: { token, apps: ['shell', 'charts'], ports: [5000, 30043] } };
  const outcome = await checkPage(browser, options, async ({ verifyResources }) => {
    assert.equal((await verifyResources()).length, 2);
  });
  assert.deepEqual(Array.from(outcome.candidate_resources, resource => resource.app), ['shell', 'charts']);
  assets.push(asset('charts', 30043, 'older-launch'));
  const stale = await checkPage(browser, options, async ({ verifyResources }) => {
    await verifyResources();
    assert.fail('verified a stale cached asset');
  });
  assert.deepEqual(stale, { status: 'blocked', stage: 'candidate-resource', repair_required: false, check: 'charts' });
});

test('automatic version proof never starts assertions if the candidate app is missing', async () => {
  let url;
  const browser = {
    async navigate(value) { url = value; },
    async evaluate(expression) {
      if (expression.includes('workArea:')) return { url, workArea: 'QA' };
      if (expression.includes('serverTiming')) return { missing: ['charts'] };
      return true;
    },
  };
  const missing = await checkPage(browser, { ...profile, postMerge: true,
    launchProof: { token: 'a'.repeat(32), apps: ['charts'], ports: [30043] },
  }, async ({ verifyResources }) => {
    await verifyResources();
    assert.fail('verified absent application');
  });
  assert.deepEqual(missing, { status: 'blocked', stage: 'candidate-resource', repair_required: false, check: 'charts' });
});

test('the current launch marks JavaScript responses without buffering or changing their bytes', async () => {
  const { stampLaunchResponse } = require('../scripts/agentlimb_resource_witness.cjs');
  const token = 'a'.repeat(32);
  const server = createServer((request, response) => {
    stampLaunchResponse(response, 'http://localhost:5000', token + '.charts', '');
    response.writeHead(200, { 'Content-Type': request.url === '/app.js' ? 'text/javascript' : 'text/html',
      'Server-Timing': 'compile;dur=2', 'Cache-Control': 'public' });
    response.write('window.');
    response.end('charts = true');
  }).listen(0, '127.0.0.1');
  try {
    await once(server, 'listening');
    const origin = 'http://127.0.0.1:' + server.address().port;
    const response = await fetch(origin + '/app.js');
    assert.equal(response.headers.get('server-timing'), 'compile;dur=2, zentao-launch;desc="' + token + '.charts"');
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal(await response.text(), 'window.charts = true');
    const html = await fetch(origin + '/index.html');
    assert.equal(html.headers.get('server-timing'), 'compile;dur=2');
    await html.text();
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});


test('inherited preload attests only the app served from the isolated checkout', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'agentlimb-launch-'));
  t.after(() => rmSync(directory, { recursive: true }));
  const checkout = join(directory, 'app');
  mkdirSync(checkout);
  const probe = createServer().listen(0, '127.0.0.1');
  await once(probe, 'listening');
  const { port } = probe.address();
  await new Promise(resolve => probe.close(resolve));
  const token = 'a'.repeat(32);
  const script = [
    "const { createServer } = require('node:http');",
    "const server = createServer((req,res) => { res.setHeader('Content-Type','text/javascript'); res.end('ok'); });",
    "server.listen(" + port + ", '127.0.0.1', async () => {",
    "  const reply = await fetch('http://127.0.0.1:" + port + "/app.js', { headers: { 'If-None-Match': 'old' } });",
    "  await reply.text();",
    "  console.log(reply.headers.get('server-timing')); server.close();",
    "});",
  ].join('\n');
  const env = { ...process.env, ZENTAO_E2E_WITNESS: JSON.stringify({
    origin: 'http://localhost:5000', resources: [],
    launch: { token, ports: [port], roots: { [realpathSync.native(checkout)]: 'charts' } },
  }) };
  const witness = require.resolve('../scripts/agentlimb_resource_witness.cjs');
  function run(cwd) {
    const processResult = spawnSync(process.execPath, ['--require', witness, '-e', script], {
      cwd, env, encoding: 'utf8', timeout: 5000,
    });
    assert.equal(processResult.status, 0, processResult.stderr || String(processResult.error));
    return processResult.stdout.trim();
  }
  assert.equal(run(checkout), 'zentao-launch;desc="' + token + '.charts"');
  assert.equal(run(directory), 'null');
});
