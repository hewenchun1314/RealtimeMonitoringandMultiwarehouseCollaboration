/**
 * 用 Node + 假 DOM 验证 demo.html 里的项目过滤脚本（改模版后跑一遍，防回归）。
 * 从 demo.html 真实解析出分组/行结构，再执行页面里的那段 JS，断言过滤结果。
 */
const fs = require('fs');
const path = require('path');

const file = path.join(__dirname, 'demo.html');
const html = fs.readFileSync(file, 'utf8');

// 1. 抽取页面脚本
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) throw new Error('没找到 <script>');
const code = scriptMatch[1];

// 2. 解析分组结构
const sections = [];
const secRe = /<section class="card group">([\s\S]*?)<\/section>/g;
let m;
while ((m = secRe.exec(html)) !== null) {
  const body = m[1];
  const count = parseInt((body.match(/class="count">(\d+)</) || [])[1], 10);
  const pids = [...body.matchAll(/<tr class="[^"]*" data-pid="([^"]*)"/g)].map((x) => x[1]);
  sections.push({ count, pids });
}

// 3. 新增卡片里的 li[data-pid]
const newLis = [...html.matchAll(/<li data-pid="([^"]*)"/g)].map((x) => x[1]);

function el(tag, { pid, isSection } = {}) {
  return {
    tagName: tag,
    _pid: pid,
    style: {},
    getAttribute(n) {
      return n === 'data-pid' ? this._pid : null;
    },
    querySelectorAll(sel) {
      if (sel === 'tbody tr') return this._rows || [];
      return [];
    },
    querySelector(sel) {
      if (sel === '.count') return this._countEl;
      return null;
    },
  };
}

const fakeSections = sections.map((s) => {
  const sec = el('SECTION');
  sec._rows = s.pids.map((pid) => el('TR', { pid }));
  sec._countEl = { textContent: String(s.count) };
  return sec;
});

const allPidEls = [];
fakeSections.forEach((s) => allPidEls.push(...s._rows));
newLis.forEach((pid) => allPidEls.push(el('LI', { pid })));

let changeHandler = null;
const sel = {
  value: '',
  addEventListener(ev, fn) {
    if (ev === 'change') changeHandler = fn;
  },
};
const info = { textContent: '' };

global.document = {
  getElementById(id) {
    if (id === 'pf') return sel;
    if (id === 'pinfo') return info;
    return null;
  },
  querySelectorAll(sel2) {
    if (sel2 === 'section.group') return fakeSections;
    if (sel2 === '[data-pid]') return allPidEls;
    return [];
  },
};

// 4. 执行页面脚本（含首次 apply）
eval(code);

function snapshot() {
  return {
    info: info.textContent,
    visibleRows: allPidEls.filter((e) => e.tagName === 'TR' && e.style.display !== 'none').length,
    visibleNew: allPidEls.filter((e) => e.tagName === 'LI' && e.style.display !== 'none').length,
    sections: fakeSections.map((s) => ({
      shown: s.style.display !== 'none',
      count: s._countEl.textContent,
    })),
  };
}

let failed = 0;
function check(label, cond, got) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}${cond ? '' : '  -> ' + JSON.stringify(got)}`);
  if (!cond) failed++;
}

const total = allPidEls.filter((e) => e.tagName === 'TR').length;
const s0 = snapshot();
check('初始显示全部行', s0.visibleRows === total, s0);
check('初始筛选提示为空', s0.info === '', s0.info);
check('初始所有分组可见', s0.sections.every((x) => x.shown), s0.sections);

// 逐个项目验证
const projects = [...new Set(allPidEls.filter((e) => e.tagName === 'TR').map((e) => e.getAttribute('data-pid')))];
console.log('项目集合:', projects);

for (const pid of projects) {
  sel.value = pid;
  changeHandler();
  const st = snapshot();
  const expect = allPidEls.filter((e) => e.tagName === 'TR' && e.getAttribute('data-pid') === pid).length;
  const expectNew = newLis.filter((x) => x === pid).length;
  check(`项目 ${pid} 行数=${expect}`, st.visibleRows === expect, st);
  check(`项目 ${pid} 新增条目=${expectNew}`, st.visibleNew === expectNew, st);
  check(`项目 ${pid} 提示文案`, st.info === `已筛选 ${expect} 条`, st.info);
  const secOk = st.sections.every((s, i) => {
    const secPids = sections[i].pids;
    const vis = secPids.filter((x) => x === pid).length;
    return s.shown === vis > 0 && String(s.count) === String(vis);
  });
  check(`项目 ${pid} 分组显隐与计数`, secOk, st.sections);
}

// 回到全部
sel.value = '';
changeHandler();
const sAll = snapshot();
check('清空筛选恢复全部', sAll.visibleRows === total && sAll.info === '', sAll);

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
