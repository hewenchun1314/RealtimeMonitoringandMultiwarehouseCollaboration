// Declarative acceptance actions. Only the fixed browser bridge performs work.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { setTimeout: delay } = require('node:timers/promises');

const FIELDS = {
  navigate: ['path'],
  click: ['selector'],
  input: ['selector', 'value'],
  'wait-for': ['selector'],
  'assert-visible': ['selector'],
  'assert-hidden': ['selector'],
  'assert-text': ['selector', 'text'],
  'assert-count': ['selector', 'count'],
  'assert-style': ['selector', 'property', 'value'],
};
const MAX_TIMEOUT = 30000;

function validStep(step) {
  if (!step || typeof step !== 'object' || !Object.hasOwn(FIELDS, step.action)) return false;
  const fields = FIELDS[step.action];
  const polled = step.action.startsWith('assert-') || step.action === 'wait-for';
  const allowed = [...fields, 'action', 'name', ...(polled ? ['timeout_ms'] : []),
    ...(step.action === 'wait-for' ? ['scroll_container'] : [])];
  if (Object.keys(step).some(key => !allowed.includes(key))) return false;
  if (step.scroll_container !== undefined &&
      (typeof step.scroll_container !== 'string' || !step.scroll_container.trim() || step.scroll_container.length > 4000)) return false;
  if (step.name !== undefined && (typeof step.name !== 'string' || !/^[a-z0-9-]{1,60}$/.test(step.name))) return false;
  if (step.timeout_ms !== undefined && (!Number.isInteger(step.timeout_ms) || step.timeout_ms < 0 ||
      step.timeout_ms > MAX_TIMEOUT)) return false;
  for (const field of fields) {
    const value = step[field];
    if (field === 'property') {
      if (typeof value !== 'string' || !/^[a-z][a-z0-9-]{0,63}$/.test(value)) return false;
    } else if (field === 'count') {
      if (!Number.isInteger(value) || value < 0 || value > 10000) return false;
    } else if (typeof value !== 'string' || value.length > 4000 ||
               (!['value', 'text'].includes(field) && !value.trim())) return false;
  }
  return step.action !== 'navigate' ||
    (step.path.startsWith('/') && !step.path.startsWith('//') && !/[\\\x00-\x1f]/.test(step.path));
}

function matches(selector, action, expected, property, scrollContainer) {
  const roots = new Set([document]);
  const found = new Set();
  for (const root of roots) {
    for (const el of root.querySelectorAll(selector)) found.add(el);
    for (const node of root.querySelectorAll('*')) {
      if (node.shadowRoot) roots.add(node.shadowRoot);
      if (node.tagName === 'IFRAME' && node.contentDocument) roots.add(node.contentDocument);
    }
  }
  const elements = [...found];
  const visible = element => {
    const style = getComputedStyle(element);
    return element.getClientRects().length > 0 && style.visibility !== 'hidden' && style.visibility !== 'collapse';
  };
  if (action === 'assert-count') return elements.length === expected;
  if (action === 'assert-style') {
    return elements.some(element => visible(element) && getComputedStyle(element).getPropertyValue(property) === expected);
  }
  if (action === 'assert-text') {
    return elements.some(element => visible(element) && element.textContent.trim() === expected);
  }
  if (scrollContainer && !elements.some(visible)) {
    for (const root of roots) {
      const pane = [...root.querySelectorAll(scrollContainer)].find(visible);
      if (pane && pane.scrollHeight > pane.clientHeight) {
        pane.scrollTop += Math.max(24, Math.floor(pane.clientHeight * 0.75));
        break;
      }
    }
    return false;
  }
  return action === 'assert-hidden' ? !elements.some(visible) : elements.some(visible);
}

async function runStep(browser, step, index) {
  const { action, selector, path, value, count, text, property, timeout_ms = 10000 } = step;
  if (action === 'navigate') return browser.navigate(path);
  if (action === 'click') return browser.click(selector);
  if (action === 'input') return browser.input(selector, value);

  const deadline = Date.now() + timeout_ms;
  const expression = '(' + matches.toString() + ')(' + [selector, action, text ?? count ?? value ?? null, property ?? null, step.scroll_container ?? null]
    .map(value => JSON.stringify(value)).join(',') + ')';
  do {
    if (await browser.evaluate(expression) === true) return;
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    await delay(Math.min(remaining, 250));
  } while (true);
  const check = step.name || (action + '-' + (index + 1));
  if (action === 'wait-for') {
    const error = new Error('Required scenario data or control was unavailable');
    error.stage = 'test-data';
    error.check = check;
    throw error;
  }
  const error = new assert.AssertionError({ message: 'Scenario assertion failed: ' + check,
                                            actual: false, expected: true, operator: action });
  error.check = check;
  throw error;
}

function validSteps(steps) {
  return Array.isArray(steps) && steps.length > 0 && steps.length <= 100 &&
    steps.every(validStep) && steps.some(step => step.action.startsWith('assert-'));
}

function scenarioCases(data) {
  if (!data || typeof data !== 'object' || Object.keys(data).length !== 1) return null;
  if (validSteps(data.steps)) return [{ name: 'scenario', steps: data.steps }];
  if (!Array.isArray(data.cases) || !data.cases.length || data.cases.length > 25 ||
      data.cases.some(item => !item || typeof item !== 'object' || Object.keys(item).length !== 2 ||
        typeof item.name !== 'string' || !/^[a-z0-9-]{1,60}$/.test(item.name) ||
        !validSteps(item.steps) || item.steps[0].action !== 'navigate') ||
      new Set(data.cases.map(item => item.name)).size !== data.cases.length) return null;
  return data.cases;
}

function loadScenario(file) {
  const document = JSON.parse(readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
  const cases = scenarioCases(document);
  if (!cases) throw new TypeError('A scenario requires supported declarative cases with assertions');
  return async ({ browser, verifyResources }) => {
    const results = [];
    let businessFailure;
    let blocker;
    for (const { name, steps } of cases) {
      try {
        for (const [index, step] of steps.entries()) {
          if (verifyResources && step.action.startsWith('assert-')) await verifyResources();
          await runStep(browser, step, index);
        }
        results.push({ name, status: 'passed' });
      } catch (error) {
        const failure = error instanceof assert.AssertionError;
        results.push({ name, status: failure ? 'failed' : 'blocked',
                       ...(error.check ? { check: error.check } : {}),
                       ...(error.stage ? { stage: error.stage } : {}) });
        if (failure) businessFailure ||= error;
        else blocker ||= error;
      }
    }
    const error = businessFailure || blocker;
    if (error) {
      error.scenario_cases = results;
      throw error;
    }
    return results;
  };
}

module.exports = { loadScenario };
