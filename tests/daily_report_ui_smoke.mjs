/* 日报弹窗渲染与早/晚报分流冒烟测试。 */
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync('src/web/index.html', 'utf8');
const js = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)][0][1];
const start = js.indexOf('const BRIEF_LABEL=');
const end = js.indexOf('loadDash();poll();', start);
if (start < 0 || end < 0) throw new Error('找不到早报脚本片段');
const escSource = js.match(/const esc=[^\n]+/)[0];
const code = escSource + '\n' + js.slice(start, end);

function makeEl(id) {
  const classes = new Set();
  const el = {
    id, children: [], dataset: {}, style: {}, textContent: '', disabled: false, _html: '', _cites: [], _docs: [],
    classList: {
      add(...xs) { xs.forEach(x => classes.add(x)); },
      remove(...xs) { xs.forEach(x => classes.delete(x)); },
      contains(x) { return classes.has(x); },
      toggle(x, on) { if (on) classes.add(x); else classes.delete(x); },
    },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    querySelectorAll(sel) {
      if (sel === '[data-doc]') {
        if (!this._docs.length) {
          this._docs = [...this._html.matchAll(/data-doc="(\d+)"/g)].map(m => {
            const b = makeEl('doc');
            b.dataset.doc = m[1];
            return b;
          });
        }
        return this._docs;
      }
      if (sel !== '[data-cite]') return [];
      if (!this._cites.length) {
        this._cites = [...this._html.matchAll(/data-cite="(L\d+)"/g)].map(m => {
          const b = makeEl('cite');
          b.dataset.cite = m[1];
          return b;
        });
      }
      return this._cites;
    },
    click() {},
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); this.children = []; this._cites = []; this._docs = []; },
  });
  return el;
}

const ids = ['brief-modal', 'bf-body', 'bf-title', 'bf-export', 'bf-close', 'btn-brief', 'btn-export'];
const els = Object.fromEntries(ids.map(id => [id, makeEl(id)]));
const document = {
  querySelector(sel) { return els[sel.slice(1)] || makeEl(sel); },
  getElementById(id) { return els[id] || makeEl(id); },
  addEventListener() {},
};
const store = new Map();
const calls = [];
const opened = [];
const details = [];
let routes = {};
const ctx = {
  document, console, Date, Math, Number, String, JSON, RegExp,
  window: { __status: { running: false }, addEventListener() {}, open(url) { opened.push(url); } },
  localStorage: {
    getItem(k) { return store.get(k) || null; },
    setItem(k, v) { store.set(k, String(v)); },
  },
  $: sel => document.querySelector(sel),
  openDetail(id) { details.push(id); },
  async apiGet(path, params) {
    const url = params
      ? path + '?' + new URLSearchParams(params).toString()
      : path;
    calls.push(url);
    return routes[url] || null;
  },
  async apiPost() { return { ok: true }; },
  setTimeout,
};
vm.createContext(ctx);
vm.runInContext(code, ctx);

const fails = [];
const check = (ok, msg) => { if (!ok) fails.push(msg); };
check(typeof ctx.renderDailyReport === 'function', '缺少 renderDailyReport');
check(typeof ctx.maybeBrief === 'function', '缺少 maybeBrief');
check(typeof ctx.exportBrief === 'function', '缺少 exportBrief');
check(ctx.briefDay(new Date('2026-11-24T18:00:00Z')) === '2026-11-25', '业务日期未统一为 UTC+8');
if (fails.length) {
  console.error(fails.join('\n'));
  process.exit(1);
}

const fixtureDay = new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
const fixtureTaken = new Date();
fixtureTaken.setHours(18, 0, 0, 0);
const fixtureTime = fixtureTaken.toISOString();
const report = {
  report_date: fixtureDay, generated_at: fixtureTime, mode: 'rule',
  headline: '模型标题 <script>',
  executive_summary: '执行摘要 [L1]',
  sections: [
    { id: 'key_events', title: '关键事件', bullets: ['事件结论 [L1]'] },
    { id: 'trends', title: '趋势与结构变化', bullets: ['趋势结论'] },
    { id: 'risks', title: '风险与反证', bullets: ['代理指标风险'] },
    { id: 'watchlist', title: '后续观察', bullets: ['继续观察'] },
  ],
  evidence: [{ citation: 'L1', id: 7, title: '证据标题', source: '测试源', t_valid: '2026-11-25T01:00:00Z', url: 'https://example.test/7', summary: '证据摘要' }],
  data_quality: { independent_events: 1, reposts_excluded: 2, proxy_note: '媒体报道频次代理', source_health: { total: 3, unavailable: 1, parse_broken: 0, issues: [{ name: '故障源' }] } },
};
ctx.renderDailyReport({ report, reports: [{ report_date: report.report_date }], date: report.report_date });
const bodyHtml = els['bf-body'].innerHTML;
check(bodyHtml.includes('模型标题 &lt;script&gt;'), '标题未转义');
check(!bodyHtml.includes('<script>'), '标题存在 XSS 注入');
check(bodyHtml.includes('规则报告'), '缺少规则模式标识');
check(bodyHtml.includes('媒体报道频次代理'), '缺少代理指标说明');
check(bodyHtml.includes('故障源'), '缺少信源异常说明');
const cite = els['bf-body'].querySelectorAll('[data-cite]')[0];
check(!!cite, '引用未渲染为可点击元素');
if (cite) cite.onclick();
check(details.includes(7), '本地引用未下钻到 openDetail(7)');
const evidence = els['bf-body'].querySelectorAll('[data-doc]')[0];
check(!!evidence, '证据标题未渲染为可点击元素');
if (evidence) evidence.onclick();
check(details.includes(7), '证据标题未下钻到 openDetail(7)');
ctx.exportBrief();
check(opened.includes('/api/daily-report/export?date=' + fixtureDay), '导出未走日报 Markdown 接口');

const digest = {
  meta: { snapshot_id: 9, taken_at: fixtureTime, window_days: 1, items_in_window: 1,
          last_run: { status: 'ok', new_items: 1, sources_ok: 1, sources_bad: 0, note: 'slot=morning' } },
  panels: { zhipu: [{ id: 1, title: '晚报条目', summary: '摘要', source: '测试源', t: '2026-11-25T08:00:00Z', url: 'https://example.test/1' }] },
};
routes = {
  '/api/digest?days=1': digest,
  ['/api/daily-report?date=' + fixtureDay]: { report, reports: [], date: report.report_date },
};
store.clear(); calls.length = 0; ctx.window.__status = { running: false };
await ctx.maybeBrief();
check(calls.some(p => p.startsWith('/api/daily-report?date=')), '晨间自动弹窗未读取日报');
check(els['bf-body'].innerHTML.includes('模型标题'), '晨间自动弹窗未渲染日报');

store.clear(); calls.length = 0;
routes['/api/digest?days=1'] = { ...digest, meta: { ...digest.meta, last_run: { ...digest.meta.last_run, note: 'slot=evening' } } };
await ctx.maybeBrief();
check(!calls.some(p => p.startsWith('/api/daily-report')), '晚间弹窗错误读取日报');
check(els['bf-title'].textContent.includes('晚报'), '晚间弹窗未保留晚报');
check(els['bf-body'].innerHTML.includes('晚报条目'), '晚间弹窗未渲染 digest 条目');

store.clear(); calls.length = 0;
await els['btn-brief'].onclick();
check(calls.some(p => p.startsWith('/api/daily-report?date=')), '手动按钮未读取日报');
check(els['brief-modal'].classList.contains('on'), '手动按钮未打开弹窗');

if (fails.length) {
  console.error(fails.join('\n'));
  process.exit(1);
}
console.log('日报 UI：渲染/转义/引用下钻/导出/晨晚分流/手动打开 全部通过');
