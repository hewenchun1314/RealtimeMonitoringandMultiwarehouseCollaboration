const { AssertionError } = require('node:assert');
const { spawn } = require('node:child_process');
const { readFileSync } = require('node:fs');
const { createInterface } = require('node:readline');
const { setTimeout: delay } = require('node:timers/promises');

class Blocked extends Error {
  constructor(stage, check) {
    super(stage);
    this.stage = stage;
    this.check = check;
  }
}

function connect(options) {
  const child = spawn(process.execPath, [options.host], {
    stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true,
    env: { ...process.env, AGENTLIMB_CLIENT_NAME: 'ZenTao E2E', AGENTLIMB_CLIENT_KIND: 'zentao-e2e' },
  });
  const pending = new Map();
  const lines = createInterface({ input: child.stdout });
  let nextId = 0;
  let closed = false;

  function disconnect() {
    closed = true;
    for (const response of pending.values()) response(null);
    pending.clear();
  }

  child.on('error', disconnect);
  child.on('exit', disconnect);
  child.stdin.on('error', disconnect);
  lines.on('line', line => {
    try {
      const message = JSON.parse(line);
      pending.get(message.id)?.(message);
    } catch {
      disconnect();
    }
  });

  function request(method, params) {
    if (closed) throw new Blocked('agentlimb-connection');
    const id = ++nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => pending.get(id)?.(null), 65000);
      pending.set(id, message => {
        clearTimeout(timer);
        pending.delete(id);
        if (!message || message.error) reject(new Blocked('agentlimb-connection'));
        else resolve(message.result);
      });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  return {
    async initialize() {
      await request('initialize', {
        protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'ZenTao E2E', version: '1' },
      });
      child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');
    },
    async call(name, args = {}) {
      const response = await request('tools/call', { name, arguments: args });
      if (response?.isError) {
        const detail = response.content?.[0]?.text || '';
        if (/not authorized/i.test(detail)) throw new Blocked('agentlimb-pairing');
        if (/no browser extension connected|browser disconnected/i.test(detail)) {
          throw new Blocked('agentlimb-connection');
        }
        throw new Blocked('agentlimb-tool', name);
      }
      try {
        const result = JSON.parse(response.content[0].text);
        if (result.ok === false) throw new Error();
        return result;
      } catch {
        if (name === 'say' && response?.content?.[0]?.type === 'text') return {};
        throw new Blocked('agentlimb-tool', name);
      }
    },
    close() {
      disconnect();
      lines.close();
      child.stdin.end();
      child.kill();
    },
  };
}

async function until(read, stage, check, timeout = 10000) {
  const deadline = Date.now() + timeout;
  do {
    const result = await read();
    if (result) return result;
    if (Date.now() >= deadline) break;
    await delay(350);
  } while (true);
  throw new Blocked(stage, check);
}

function composedMatches(selector) {
  const roots = new Set([document]);
  const matches = new Set();
  for (const root of roots) {
    for (const node of root.querySelectorAll(selector)) matches.add(node);
    for (const node of root.querySelectorAll('*')) {
      if (node.shadowRoot) roots.add(node.shadowRoot);
      if (node.tagName === 'IFRAME' && node.contentDocument) roots.add(node.contentDocument);
    }
  }
  return [...matches];
}

function pageState(username = '') {
  const user = composedMatches('.login-form-wrapper .large-username input')[0];
  const password = composedMatches('.login-form-wrapper input[type=password]')[0];
  const workArea = composedMatches('.work-area-selector .selector-trigger .label, .header-wrapper .project-name')[0];
  return { url: location.href, login: Boolean(password),
    autofilled: Boolean(user?.value === username && password?.value && username),
    workArea: workArea?.textContent.trim() };
}

function exactText(selector, text) {
  return composedMatches(selector).find(node =>
    node.getClientRects().length && getComputedStyle(node).visibility === 'visible' && node.textContent.trim() === text);
}

function scenarioResult(error) {
  if (error instanceof AssertionError) return { status: 'failed', stage: 'acceptance', repair_required: true };
  return { status: 'blocked', stage: error.stage || 'scenario', repair_required: false,
    ...(error.check ? { check: error.check } : {}) };
}

async function checkPage(browser, options, scenario) {
  const { baseUrl, loginPath, platformPath, entryPath, workArea, username, checks, resources } = options;
  if (!checks?.length) throw new Blocked('test-data-profile');
  const platform = new URL(platformPath, baseUrl).href;
  const login = new URL(loginPath, baseUrl).href;
  const entry = new URL(entryPath, baseUrl).href;
  const evaluate = expression => browser.evaluate(`(() => { const composedMatches = ${composedMatches}; return (${expression}); })()`);
  const inspect = () => evaluate(`(${pageState})(${JSON.stringify(username)})`);
  const click = async selector => {
    const clicked = await evaluate(`(() => { const target = composedMatches(${JSON.stringify(selector)})[0]; if (!target) return false; target.click(); return true; })()`);
    return clicked;
  };
  await browser.navigate(entry);
  let submitted = false;
  let checkedLoginPage = false;
  let returnedToEntry = false;
  let session;
  try {
    await until(async () => {
      session = await inspect();
      if (session.url === entry && !session.login && (!workArea || session.workArea)) return true;
      if (session.url === login && session.autofilled && !submitted) {
        submitted = await click('.login-form-wrapper .large-button');
      } else if (session.url === platform && !session.login && !returnedToEntry) {
        returnedToEntry = true;
        await browser.navigate(entry);
      } else if (session.url === entry && session.login && !checkedLoginPage) {
        checkedLoginPage = true;
        await browser.navigate(login);
      }
      return false;
    }, 'entry', undefined, 20000);
  } catch (error) {
    if (error.stage === 'entry' && session && (session.url === login || session.login ||
        new URL(session.url).origin !== new URL(baseUrl).origin)) throw new Blocked('waiting-login');
    throw error;
  }

  if (workArea && session.workArea !== workArea) {
    await browser.navigate(platform);
    await until(() => click('.work-area-selector .selector-trigger'), 'work-area', 'work-area-trigger');
    const item = `(${exactText})('.work-area-selector-dropdown [role="menuitem"]',${JSON.stringify(workArea)})`;
    await until(() => evaluate(`Boolean(${item})`), 'work-area', 'work-area-menu');
    await evaluate(`(() => { ${item}.click(); return true; })()`);
    await until(async () => (await inspect()).workArea === workArea, 'work-area', 'work-area-selection');
    await browser.navigate(entry);
  }

  const authenticatedLocation = async () => {
    const state = await inspect();
    if (state.login || state.url === login || new URL(state.url).origin !== new URL(baseUrl).origin) {
      throw new Blocked('waiting-login');
    }
    return state.url;
  };
  const requireEntry = async () => (await authenticatedLocation()) === entry;
  await until(requireEntry, 'entry');

  const passedChecks = [];
  let foregroundRetry = false;
  for (const { name, action = 'visible', text } of checks) {
    const query = `(${exactText})('*',${JSON.stringify(text)})`;
    const waitForControl = () => until(async () => (await requireEntry()) && evaluate(`Boolean(${query})`), 'test-data', name);
    try {
      await waitForControl();
    } catch (error) {
      if (error.stage !== 'test-data' || foregroundRetry || !browser.activate ||
          await browser.evaluate('document.visibilityState') !== 'hidden') throw error;
      await browser.activate();
      foregroundRetry = true;
      await waitForControl();
    }
    if (action !== 'visible') {
      await evaluate(`(() => { const node = ${query}; if (${JSON.stringify(action)} !== 'expand' || node.closest('[aria-expanded]')?.getAttribute('aria-expanded') !== 'true') node.click(); return true; })()`);
    }
    passedChecks.push(name);
  }

  const verifiedResources = [];
  for (const { app, url, sha256 } of resources || []) {
    const target = JSON.stringify(url);
    await until(() => browser.evaluate(`performance.getEntriesByName(${target}, 'resource').length > 0`), 'candidate-resource', app);
    const actual = await browser.evaluate(`(async () => {
      const response = await fetch(${target}, { cache: 'no-store', redirect: 'error' });
      if (!response.ok) return null;
      return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await response.arrayBuffer())),
        byte => byte.toString(16).padStart(2, '0')).join('');
    })()`);
    if (actual !== sha256) throw new Blocked('candidate-resource', app);
    verifiedResources.push({ app, url, sha256 });
  }
  if (scenario) {
    try {
      await scenario({ browser });
    } catch (error) {
      await authenticatedLocation();
      return { ...scenarioResult(error), checks: passedChecks, candidate_resources: verifiedResources };
    }
    await authenticatedLocation();
  }
  return { status: 'passed', stage: scenario ? 'acceptance' : 'preflight', checks: passedChecks,
    ...(foregroundRetry ? { foreground_retry: true } : {}),
    ...(scenario ? { candidate_resources: verifiedResources } : {}) };
}

async function runSession(call, options, scenario) {
  const { connection } = await call('ping');
  const profiles = connection?.browsers ?? [connection?.activeBrowserProfile].filter(Boolean);
  const matches = profiles.filter(profile => profile.authorized !== false &&
    (profile.id === options.browser || profile.label === options.browser));
  if (matches.length !== 1 || !matches[0].id) throw new Blocked('browser-profile');
  const browserId = matches[0].id;
  const { tab } = await call('tab_new', { url: 'about:blank', browser: browserId });
  if (!Number.isInteger(tab?.id)) throw new Blocked('browser-tab');
  const send = (name, args) => call(name, { ...args, tabId: tab.id, browser: browserId });
  let originalTab;
  const browser = Object.freeze({
    async evaluate(expression) { return (await send('javascript_eval', { expression })).result; },
    navigate(url) {
      const destination = new URL(url, options.baseUrl);
      if (destination.origin !== options.baseUrl) throw new Blocked('entry');
      return send('navigate', { url: destination.href });
    },
    click: selector => send('computer', { action: 'click', selector }),
    input: (selector, value) => send('form_input', { selector, value }),
    wait: args => send('wait', args),
    async activate() {
      const { tabs } = await call('tabs_context', { browser: browserId });
      originalTab = tabs.find(candidate => candidate.active && candidate.windowId === tab.windowId)?.id;
      await send('tab_activate');
    },
  });
  try {
    return await checkPage(browser, options, scenario);
  } finally {
    try {
      if (Number.isInteger(originalTab) && originalTab !== tab.id && (await send('tabs_context')).tab?.active) {
        // The original tab may have closed while the test was running.
        await call('tab_activate', { tabId: originalTab, browser: browserId }).catch(() => {});
      }
    } finally {
      try {
        await send('tab_close');
      } finally {
        await call('say', { text: '禅道 E2E 浏览器步骤结束。', browser: browserId });
      }
    }
  }
}

async function main() {
  const options = JSON.parse(readFileSync(0, 'utf8'));
  const client = connect(options);
  try {
    const scenario = options.scenario ? require(options.scenario) : undefined;
    if (options.scenario && typeof scenario !== 'function') throw new Blocked('scenario-profile');
    await client.initialize();
    return await runSession(client.call, options, scenario);
  } catch (error) {
    return scenarioResult(error);
  } finally {
    client.close();
  }
}

if (require.main === module) {
  main().then(result => {
    process.stdout.write(JSON.stringify(result) + '\n');
    process.exitCode = Number(result.status !== 'passed');
  }).catch(() => {
    process.stdout.write('{"status":"blocked","stage":"runner-configuration"}\n');
    process.exitCode = 1;
  });
}

module.exports = { checkPage, runSession, scenarioResult };
