const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { join } = require('node:path');

class CheckFailed extends Error {
  constructor(stage, check) {
    super(stage);
    this.stage = stage;
    this.check = check;
  }
}

function matches(url, expected) {
  return url.origin === expected.origin && url.pathname === expected.pathname && url.search === expected.search;
}

async function openPage(page, expected, stage) {
  try {
    const response = await page.goto(expected.href);
    if (!response?.ok() || !matches(new URL(page.url()), expected)) throw new Error();
  } catch {
    throw new CheckFailed(stage);
  }
}

async function checkPage(page, options, scenario) {
  const { baseUrl, loginPath, platformPath, entryPath, credentials, checks, workArea } = options;
  const login = new URL(loginPath, baseUrl);
  const platform = new URL(platformPath, baseUrl);
  await openPage(page, login, 'login');
  try {
    await page.locator('.login-form-wrapper .large-username input').fill(credentials.username);
    await page.locator('.login-form-wrapper input[type=password]').fill(credentials.password);
    await page.locator('.login-form-wrapper .large-button').click();
    await page.waitForURL(url => matches(url, platform));
  } catch {
    throw new CheckFailed('login');
  }
  if (workArea) {
    let check = 'work-area-trigger';
    try {
      const trigger = page.locator('.work-area-selector .selector-trigger');
      await trigger.waitFor({ state: 'visible' });
      if ((await trigger.locator('.label').innerText()).trim() !== workArea) {
        check = 'work-area-listed';
        const item = page.locator('.work-area-selector-dropdown')
          .getByRole('menuitem', { name: workArea, exact: true, includeHidden: true });
        await item.waitFor({ state: 'attached' });
        if ((await trigger.locator('.label').innerText()).trim() !== workArea) {
          check = 'work-area-menu';
          if (await trigger.getAttribute('aria-expanded') !== 'true') await trigger.click();
          try {
            await item.waitFor({ state: 'visible', timeout: 2500 });
          } catch {
            if (await trigger.getAttribute('aria-expanded') === 'false') await trigger.press('Enter');
          }
          await item.click();
        }
      }
      check = 'work-area-selection';
      await trigger.getByText(workArea, { exact: true }).waitFor({ state: 'visible' });
    } catch {
      throw new CheckFailed('work-area', check);
    }
  }
  await openPage(page, new URL(entryPath, baseUrl), 'entry');
  if (!checks?.length) throw new CheckFailed('test-data-profile');
  for (const { name, text, action = 'visible' } of checks) {
    try {
      const target = page.getByText(text, { exact: true }).first();
      if (action === 'click') await target.click();
      else if (action === 'expand') {
        const node = page.getByRole('treeitem').filter({ has: target }).first();
        await node.waitFor({ state: 'visible' });
        if (await node.getAttribute('aria-expanded') !== 'true') {
          await node.locator('.el-tree-node__expand-icon').first().click();
        }
      }
      else await target.waitFor({ state: 'visible' });
    } catch {
      throw new CheckFailed('test-data', name);
    }
  }
  if (scenario) {
    try {
      await scenario({ page });
    } catch {
      throw new CheckFailed('acceptance');
    }
  }
  return { stage: scenario ? 'acceptance' : 'preflight', checks: checks.map(({ name }) => name) };
}

async function main(options) {
  let browser;
  try {
    const requireFromFrontend = createRequire(join(options.workspace, 'package.json'));
    const { chromium } = requireFromFrontend('playwright');
    browser = await chromium.launch({ channel: options.channel, headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(12000);
    page.setDefaultNavigationTimeout(20000);
    if (!options.credentials) {
      await openPage(page, new URL(options.loginPath, options.baseUrl), 'login');
      return { status: 'blocked', stage: 'credentials', browser: browser.version() };
    }
    const scenario = options.scenario ? require(options.scenario) : undefined;
    const result = await checkPage(page, options, scenario);
    return { status: 'passed', ...result, browser: browser.version() };
  } catch (error) {
    return { status: 'blocked', stage: error.stage || (browser ? 'configuration' : 'browser'), check: error.check };
  } finally {
    if (browser) await browser.close();
  }
}

module.exports = { checkPage, main };

if (require.main === module) {
  main(JSON.parse(readFileSync(0, 'utf8'))).then(result => {
    process.stdout.write(`${JSON.stringify(result)}\n`);
    process.exitCode = result.status === 'passed' ? 0 : 1;
  });
}
