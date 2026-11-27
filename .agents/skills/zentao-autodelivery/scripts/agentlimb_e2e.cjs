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
  const cases = Array.isArray(error.scenario_cases) ? { scenario_cases: error.scenario_cases } : {};
  if (error instanceof AssertionError) return { status: 'failed', stage: 'acceptance', repair_required: true,
    ...(error.check ? { check: error.check } : {}), ...cases };
  return { status: 'blocked', stage: error.stage || 'scenario', repair_required: false,
    ...(error.check ? { check: error.check } : {}), ...cases };
}

function loadedDigest(target, expectedGateway) {
  const frames = [window];
  const observed = new Set();
  for (const frame of frames) {
    try {
      for (let index = 0; index < frame.frames.length; index += 1) frames.push(frame.frames[index]);
      for (const entry of frame.performance.getEntriesByName(target, 'resource')) {
        const timing = entry.serverTiming.find(metric => metric.name === 'zentao-resource');
        const gateway = entry.serverTiming.find(metric => metric.name === 'zentao-gateway');
        observed.add(expectedGateway && gateway?.description !== expectedGateway ? null : timing?.description || null);
      }
    } catch {
      // A different-origin frame has its own inaccessible timing timeline.
    }
  }
  return observed.size ? { digest: observed.size === 1 ? [...observed][0] : null } : null;
}

function loadedLaunch(proof, expectedGateway) {
  const frames = [window];
  const observations = [];
  for (const frame of frames) {
    try {
      for (let index = 0; index < frame.frames.length; index += 1) frames.push(frame.frames[index]);
      for (const entry of frame.performance.getEntriesByType('resource')) {
        const address = new URL(entry.name);
        const port = Number(address.port || (address.protocol === 'https:' ? 443 : 80));
        if (!['http:', 'https:'].includes(address.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(address.hostname)
            || !proof.ports.includes(port)) continue;
        const timings = entry.serverTiming || [];
        const mark = timings.find(metric => metric.name === 'zentao-launch')?.description;
        if (!mark && !/\.(?:[cm]?js|css|vue|tsx?)(?:$|[?#])/i.test(entry.name)) continue;
        const gateway = timings.find(metric => metric.name === 'zentao-gateway')?.description;
        const app = proof.apps.find(candidate => mark === proof.token + '.' + candidate);
        observations.push({ url: entry.name, port, app: !expectedGateway || gateway === expectedGateway ? app : null });
      }
    } catch {
      // Different-origin frames may have inaccessible timing entries.
    }
  }
  const resources = [];
  for (const app of proof.apps) {
    const current = observations.filter(row => row.app === app);
    if (!current.length) return { missing: [app] };
    const ports = new Set(current.map(row => row.port));
    if (observations.some(row => ports.has(row.port) && !row.app)) return { stale: app };
    resources.push({ app, url: current[0].url });
  }
  return { resources };
}

async function checkPage(browser, options, scenario) {
  const { baseUrl, loginPath, platformPath, entryPath, workArea, username, checks, resources, postMerge, launchProof } = options;
  if (!launchProof && !checks?.length) throw new Blocked('test-data-profile');
  const platform = new URL(platformPath, baseUrl).href;
  const login = new URL(loginPath, baseUrl).href;
  const entry = new URL(entryPath, baseUrl).href;
  const evaluate = expression => browser.evaluate(`(() => { const composedMatches = ${composedMatches}; return (${expression}); })()`);
  const inspect = () => evaluate(`(${pageState})(${JSON.stringify(username)})`);
  const click = async selector => {
    const clicked = await evaluate(`(() => { const target = composedMatches(${JSON.stringify(selector)})[0]; if (!target) return false; target.click(); return true; })()`);
    return clicked;
  };
  if (options.workAreaPath) {
    const areaPage = new URL(options.workAreaPath, baseUrl).href;
    await browser.navigate(login);
    let submitted = false;
    let redirected = false;
    await until(async () => {
      const state = await inspect();
      if (state.url === areaPage && !state.login) {
        return evaluate("composedMatches('.work-area-list-wrapper .el-table__body').length > 0");
      }
      if (state.login && state.url === login && !submitted) {
        if (!state.autofilled) {
          let credentials;
          try { credentials = JSON.parse(readFileSync(options.loginFile, 'utf8').replace(/^\uFEFF/, '')); }
          catch { throw new Blocked('waiting-login'); }
          if (credentials.username !== username || !credentials.password) throw new Blocked('waiting-login');
          const filled = await evaluate(`(() => {
            if (location.href !== ${JSON.stringify(login)}) return false;
            const user = composedMatches('.login-form-wrapper .large-username input')[0];
            const password = composedMatches('.login-form-wrapper input[type=password]')[0];
            if (!user || !password) return false;
            for (const [field, value] of [[user,${JSON.stringify(username)}],[password,${JSON.stringify(credentials.password)}]]) {
              field.value = value;
              field.dispatchEvent(new Event('input', { bubbles: true }));
            }
            return true;
          })()`).catch(() => false);
          if (!filled) return false;
        }
        submitted = await click('.login-form-wrapper .large-button');
      } else if (!state.login && state.url === platform && !redirected) {
        redirected = true;
        await browser.navigate(areaPage);
      }
      return false;
    }, 'waiting-login', undefined, 30000);
    const rowName = '.work-area-list-wrapper .el-table__body tbody tr td:first-child span';
    const match = `(${exactText})(${JSON.stringify(rowName)},${JSON.stringify(workArea)})`;
    await until(() => evaluate(`(() => { const node = ${match}; if (!node) return false; node.click(); return true; })()`),
      'work-area', 'work-area-row');
    await until(() => evaluate(`Boolean((${exactText})('.work-area-list-wrapper tr.current-row td:first-child span',${JSON.stringify(workArea)}))`),
      'work-area', 'work-area-selection');
    // ResizeObserver-backed lists can initialize at height zero in a background tab.
    if (scenario && browser.activate && await browser.evaluate('document.visibilityState') === 'hidden') {
      await browser.activate();
    }
  }
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

  if (options.workAreaPath) {
    await until(async () => (await inspect()).workArea === workArea, 'work-area', 'business-page-work-area');
  } else if (workArea && session.workArea !== workArea) {
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

  const verifyResources = async () => {
    if (launchProof) {
      const query = '(' + loadedLaunch + ')(' + JSON.stringify(launchProof) + ',' +
        JSON.stringify(options.gatewayHash || '') + ')';
      let app = launchProof.apps[0];
      const observed = await until(async () => {
        const result = await browser.evaluate(query);
        if (result?.stale) throw new Blocked('candidate-resource', result.stale);
        if (result?.missing) app = result.missing[0];
        return result?.resources;
      }, 'candidate-resource').catch(error => {
        if (!error.check) error.check = app;
        throw error;
      });
      return observed;
    }
    const verified = [];
    for (const { app, url, sha256 } of resources || []) {
      const target = JSON.stringify(url);
      let actual;
      if (postMerge) {
        const gateway = JSON.stringify(options.gatewayHash || '');
        const measurement = await until(() => browser.evaluate(`(${loadedDigest})(${target},${gateway})`), 'candidate-resource', app);
        actual = measurement.digest;
      } else {
        await until(() => browser.evaluate(`performance.getEntriesByName(${target}, 'resource').length > 0`), 'candidate-resource', app);
        actual = await browser.evaluate(`(async () => {
          const response = await fetch(${target}, { cache: 'no-store', redirect: 'error' });
          if (!response.ok) return null;
          return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await response.arrayBuffer())),
            byte => byte.toString(16).padStart(2, '0')).join('');
        })()`);
      }
      if (actual !== sha256) throw new Blocked('candidate-resource', app);
      verified.push({ app, url, sha256 });
    }
    return verified;
  };
  const evidence = launchProof ? { version_attestation: 'launch' } :
    { candidate_resources: await verifyResources() };
  if (options.workAreaPath) evidence.work_area = workArea;
  let scenarioCases;
  if (scenario) {
    let scenarioError;
    try {
      scenarioCases = await scenario({ browser, ...(postMerge ? { verifyResources } : {}) });
    } catch (error) {
      scenarioError = error;
    }
    await authenticatedLocation();
    if (launchProof && scenarioError?.stage === 'candidate-resource') return scenarioResult(scenarioError);
    if (postMerge) evidence.candidate_resources = await verifyResources();
    if (scenarioError) return { ...scenarioResult(scenarioError), checks: passedChecks, ...evidence };
  }
  return { status: 'passed', stage: scenario ? 'acceptance' : 'preflight', checks: passedChecks,
    ...(options.workAreaPath ? { work_area: workArea } : {}),
    ...(foregroundRetry ? { foreground_retry: true } : {}),
    ...(Array.isArray(scenarioCases) ? { scenario_cases: scenarioCases } : {}),
    ...(scenario ? evidence : {}) };
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
    async click(selector) {
      // The host's CSS click addresses the top document. Micro-frontend controls
      // can live in an iframe or shadow root even though they are visible.
      const nested = await this.evaluate(`(() => {
        const nodes = (${composedMatches})(${JSON.stringify(selector)});
        const el = nodes.find(node => node.getClientRects().length && getComputedStyle(node).visibility === 'visible');
        if (!el || el.getRootNode() === document) return false;
        el.click();
        return true;
      })()`);
      if (!nested) await send('computer', { action: 'click', selector });
    },
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
    let scenario;
    if (options.scenario) {
      try {
        scenario = options.postMerge ? require('./agentlimb_scenario.cjs').loadScenario(options.scenario) : require(options.scenario);
      } catch (error) {
        if (error instanceof TypeError && options.postMerge) throw new Blocked('scenario-profile');
        throw error;
      }
      if (typeof scenario !== 'function') throw new Blocked('scenario-profile');
    }
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
