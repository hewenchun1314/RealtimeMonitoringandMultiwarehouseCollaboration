const assert = require('node:assert/strict');
const { mkdtempSync, writeFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { test } = require('node:test');

function scenario(t, steps) {
  const directory = mkdtempSync(join(tmpdir(), 'agentlimb-scenario-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, 'scenario.json');
  writeFileSync(path, JSON.stringify({ steps }));
  return require('../scripts/agentlimb_scenario.cjs').loadScenario(path);
}

test('declared actions call browser operations and evaluate an escaped selector', async t => {
  const selector = '#item[data-name="hello"]';
  const run = scenario(t, [
    { action: 'navigate', path: '/data?tab=1' },
    { action: 'click', selector },
    { action: 'input', selector: '#filter', value: 'example' },
    { action: 'assert-text', selector, text: '结果', timeout_ms: 0 },
  ]);
  const calls = [];
  await run({ browser: {
    navigate: async path => calls.push(['navigate', path]),
    click: async css => calls.push(['click', css]),
    input: async (css, value) => calls.push(['input', css, value]),
    evaluate: async expression => {
      assert.ok(expression.includes(JSON.stringify(selector)));
      assert.ok(expression.includes(JSON.stringify('结果')));
      return true;
    },
  } });
  assert.deepEqual(calls, [
    ['navigate', '/data?tab=1'], ['click', selector], ['input', '#filter', 'example'],
  ]);
});

test('a visible title cannot pass a case when required page data is missing', async t => {
  const run = scenario(t, [{ action: 'navigate', path: '/plot' },
    { action: 'wait-for', selector: '.depth', name: 'crossplot-profile-depth', timeout_ms: 0 },
    { action: 'assert-visible', selector: 'h2' }]);
  const browser = { navigate: async () => {}, evaluate: async expression => !expression.includes('".depth"') };
  await assert.rejects(run({ browser }), error =>
    error.scenario_cases[0].status === 'blocked' && error.scenario_cases[0].check === 'crossplot-profile-depth');
  const results = await run({ browser: { ...browser, evaluate: async () => true } });
  assert.equal(results[0].status, 'passed');
});

test('a case can reveal data in a virtual list inside an iframe', async t => {
  const run = scenario(t, [
    { action: 'wait-for', selector: '.sample', scroll_container: '.list', timeout_ms: 2000 },
    { action: 'assert-count', selector: '.sample', count: 1 }]);
  const pane = { scrollHeight: 200, clientHeight: 40, scrollTop: 0, getClientRects: () => [1] };
  const sample = { getClientRects: () => [1] };
  const frame = { tagName: 'IFRAME', contentDocument: { querySelectorAll(selector) {
    if (selector === '.list') return [pane];
    return selector === '.sample' && pane.scrollTop > 0 ? [sample] : [];
  } } };
  const globals = { document: { querySelectorAll: selector => selector === '*' ? [frame] : [] },
    getComputedStyle: () => ({ visibility: 'visible' }) };
  const result = await run({ browser: { evaluate: async expression => require('node:vm').runInNewContext(expression, globals) } });
  assert.equal(result[0].status, 'passed');
});

test('unsupported script, navigation, and unexpected arguments are rejected', t => {
  for (const step of [
    { action: 'eval', expression: "require('node:fs')" },
    { action: 'navigate', path: '//outside.example/path' },
    { action: 'click', selector: '#control', script: 'code' },
  ]) assert.throws(() => scenario(t, [step]), TypeError);
});

test('assertion failure is a business failure and a wait timeout remains an environment blocker', async t => {
  const browser = { evaluate: async () => false };
  await assert.rejects(
    scenario(t, [{ action: 'assert-visible', selector: '#result', timeout_ms: 0 }])({ browser }),
    assert.AssertionError,
  );
  await assert.rejects(
    scenario(t, [{ action: 'wait-for', selector: '#test-record', timeout_ms: 0 },
      { action: 'assert-visible', selector: '#result', timeout_ms: 0 }])({ browser }),
    { stage: 'test-data' },
  );
});



test('independent cases continue after a failure and report the final result of each', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'agentlimb-cases-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const file = join(directory, 'cases.json');
  writeFileSync(file, JSON.stringify({ cases: [
    { name: 'first', steps: [{ action: 'navigate', path: '/one' },
      { action: 'assert-visible', selector: '#first', timeout_ms: 0 }] },
    { name: 'second', steps: [{ action: 'navigate', path: '/two' },
      { action: 'assert-visible', selector: '#second', timeout_ms: 0 }] },
  ] }));
  const run = require('../scripts/agentlimb_scenario.cjs').loadScenario(file);
  const visited = [];
  await assert.rejects(run({ browser: {
    navigate: async path => visited.push(path),
    evaluate: async expression => expression.includes('#second'),
  } }), error => {
    assert.deepEqual(error.scenario_cases.map(({ name, status }) => [name, status]),
      [['first', 'failed'], ['second', 'passed']]);
    return true;
  });
  assert.deepEqual(visited, ['/one', '/two']);
});


test('each page is attested before business assertions are allowed', async t => {
  const run = scenario(t, [
    { action: 'navigate', path: '/one' },
    { action: 'assert-visible', selector: '#one', timeout_ms: 0 },
    { action: 'navigate', path: '/two' },
    { action: 'assert-visible', selector: '#two', timeout_ms: 0 },
  ]);
  const calls = [];
  await run({ browser: {
    navigate: async path => calls.push(path),
    evaluate: async () => { calls.push('assert'); return true; },
  }, verifyResources: async () => calls.push('attest') });
  assert.deepEqual(calls, ['/one', 'attest', 'assert', '/two', 'attest', 'assert']);
});


test('scenario can assert computed font size and weight', async t => {
  const { runInNewContext } = require('node:vm');
  const run = scenario(t, [
    { action: 'assert-style', selector: 'h2', property: 'font-size', value: '14px', timeout_ms: 0 },
    { action: 'assert-style', selector: 'h2', property: 'font-weight', value: '400', timeout_ms: 0 },
  ]);
  const title = { getClientRects: () => [1] };
  const browser = { async evaluate(expression) {
    return runInNewContext(expression, {
      document: { querySelectorAll: selector => selector === 'h2' ? [title] : [] },
      getComputedStyle: () => ({ visibility: 'visible',
        getPropertyValue: name => ({ 'font-size': '14px', 'font-weight': '400' })[name] }),
    });
  } };
  assert.deepEqual(await run({ browser }), [{ name: 'scenario', status: 'passed' }]);
});
