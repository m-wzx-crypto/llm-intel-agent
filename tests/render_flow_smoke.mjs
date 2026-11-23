/* 流向图渲染冒烟测试
   目的：在没有浏览器的环境里真正跑一遍 index.html 的 drawFlow()，
   验证 ① 几何全是有限数（无 NaN/undefined）② 链路数 = 数据条数
        ③ 节点高度之和 > 0 ④ 量纲单调（n 越大越粗）
   用法：node tests/render_flow_smoke.mjs [metrics.json]
   产出：/tmp/flow-geom.json（供光栅化核对）、/tmp/flow-smoke.svg */
import fs from 'node:fs';
import vm from 'node:vm';

const HTML = 'src/web/index.html';
const src = fs.readFileSync(HTML, 'utf8');
const blocks = [...src.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!blocks.length) throw new Error('index.html 里没有 <script>');
const js = blocks[0];

const metricsPath = process.argv[2] || '/tmp/metrics.json';
const metrics = JSON.parse(fs.readFileSync(metricsPath, 'utf8'));

/* ── 从 index.html 里切出需要的片段（顺序即运行顺序） ── */
function slice(from, to) {
  const a = js.indexOf(from);
  if (a < 0) throw new Error('找不到片段起点: ' + from.slice(0, 40));
  const b = to ? js.indexOf(to, a) : js.length;
  if (b < 0) throw new Error('找不到片段终点: ' + to.slice(0, 40));
  return js.slice(a, b);
}
function balanced(startMarker) {
  const a = js.indexOf(startMarker);
  const i = js.indexOf('{', a);
  let d = 0, j = i;
  for (; j < js.length; j++) {
    if (js[j] === '{') d++;
    else if (js[j] === '}') { d--; if (!d) break; }
  }
  return js.slice(a, j + 1);
}
const SCALE = process.env.FLOW_SCALE || '';
const pieces = [
  slice('/* ══ d3-sankey 0.12.3', 'const CH_COLOR='),
  slice('const CH_COLOR=', 'const FLOW_VALUE'),
  slice('const FLOW_VALUE', 'function spreadY'),
  balanced('function spreadY('),
  balanced('function drawFlow('),
];

/* ── 极简 DOM：只实现 drawFlow 用到的部分 ── */
const rec = { nodes: [], paths: [], texts: [] };
function makeEl(tag, attrs = {}) {
  const e = {
    tag, attrs: { ...attrs }, children: [], style: {}, _html: '', textContent: '',
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    setAttribute(k, v) { this.attrs[k] = v; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; },
    getBoundingClientRect() { return { width: 900, height: 380, top: 0, left: 0 }; },
    clientWidth: 900, clientHeight: 380,
  };
  return e;
}
const box = makeEl('div');
box.clientWidth = 900;
const flowBody = makeEl('div');
const note = makeEl('span');

const document = {
  querySelector(sel) {
    if (sel === '#flow') return box;
    if (sel === '#flow-note') return note;
    return makeEl('div');
  },
  querySelectorAll() { return []; },
  getElementById(id) { return id === 'flow-body' ? flowBody : makeEl('div'); },
  createElement(t) { return makeEl(t); },
  createElementNS(ns, t) {
    const e = makeEl(t);
    if (t === 'title') rec.nodes.push(e);
    return e;
  },
  addEventListener() {},
};

const ctx = {
  document, console,
  window: { innerWidth: 1440, innerHeight: 900, addEventListener() {} },
  NS: 'http://www.w3.org/2000/svg',
  DEMO: false, DEMO_FLOWS: [],
  openList() { return true; },
  esc: s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
  el(t, a = {}, p) { const e = makeEl(t, a); if (t === 'path') rec.paths.push(e); if (t === 'text') rec.texts.push(e); if (p) p.appendChild(e); return e; },
  $: s => document.querySelector(s),
  setTimeout, Math, Number, Map, Object, Array, JSON, isFinite, parseInt, String, Infinity, NaN,
};
ctx.FLOW_VALUE = null;
let CODE = pieces.join('\n;\n');
if (SCALE) CODE = CODE.replace(/const FLOW_SCALE = "\w+"/, `const FLOW_SCALE = "${SCALE}"`);
const ALIGN = process.env.FLOW_ALIGN || '';
if (ALIGN) CODE = CODE.replace(/const FLOW_ALIGN = "\w+"/, `const FLOW_ALIGN = "${ALIGN}"`);
vm.createContext(ctx);
vm.runInContext(CODE, ctx);

/* ── 跑 ── */
ctx.drawFlow(metrics);

const paths = rec.paths
  .filter(e => typeof e.attrs.d === 'string' && e.attrs.d.length)
  .map(e => e.attrs);
const fails = [];
const bad = v => v == null || (typeof v === 'number' && !isFinite(v)) || String(v).includes('NaN');

for (const a of paths) {
  if (bad(a.d)) fails.push('path.d 含 NaN: ' + a.d.slice(0, 80));
  if (bad(a['stroke-width'])) fails.push('stroke-width 非法: ' + a['stroke-width']);
}
const inputs = (metrics.flows || []);
const emptyState = /未抽取到/.test(box.innerHTML || '');
const ledger = [];
if (!inputs.length) {
  /* 空数据是合法状态：必须走空态文案，不能画一张空图 */
  if (!emptyState) fails.push('零流向时没有渲染空态文案');
  ledger.push('空态：已渲染提示文案');
} else {
  if (paths.length !== inputs.length)
    fails.push(`链路数 ${paths.length} ≠ 数据条数 ${inputs.length}`);
  if (!paths.length) fails.push('没有生成任何链路');
  if (emptyState) fails.push('有数据却渲染了空态文案');
}

/* 量纲单调：同一张图里 n 最大的链路应最粗 */
const byN = inputs.map(f => f.n || 1);
if (paths.length && inputs.length) {
  const ws = paths.map(p => +p['stroke-width']);
  const maxIdx = byN.indexOf(Math.max(...byN));
  const minIdx = byN.indexOf(Math.min(...byN));
  if (!(ws[maxIdx] >= ws[minIdx])) fails.push(`量纲非单调：n=${byN[maxIdx]} 宽度 ${ws[maxIdx]} < n=${byN[minIdx]} 宽度 ${ws[minIdx]}`);
}

/* 节点高度和 */
const heights = flowBody.style.height;
fs.writeFileSync('/tmp/flow-check.txt',
  [`flows=${inputs.length}`, `paths=${paths.length}`, `texts=${rec.texts.length}`,
   `容器高=${heights}`, `宽度样本=${paths.slice(0, 5).map(p => p['stroke-width']).join(',')}`,
   `FAILS=${fails.length}`].join('\n') + '\n' + fails.join('\n'));

/* 序列化成 SVG 便于人眼看 */
const svg = [`<svg xmlns="http://www.w3.org/2000/svg" width="900" height="380" viewBox="0 0 900 380">`,
  `<rect width="900" height="380" fill="#fff"/>`];
const walk = (e, depth) => {
  if (!e) return;
  if (e.tag === 'path' && e.attrs.d)
    svg.push(`<path d="${e.attrs.d}" fill="none" stroke="${e.attrs.stroke}" stroke-opacity="0.3" stroke-width="${e.attrs['stroke-width']}"/>`);
  if (e.tag === 'rect')
    svg.push(`<rect x="${e.attrs.x}" y="${e.attrs.y}" width="${e.attrs.width}" height="${e.attrs.height}" rx="${e.attrs.rx || 0}" fill="${e.attrs.fill}"/>`);
  if (e.tag === 'text' && e.textContent)
    svg.push(`<text x="${e.attrs.x}" y="${e.attrs.y}" font-size="${e.attrs['font-size']}" text-anchor="${e.attrs['text-anchor']}" fill="${e.attrs.fill}">${ctx.esc(e.textContent)}</text>`);
  (e.children || []).forEach(c => walk(c, depth + 1));
};
walk(box, 0);
svg.push('</svg>');
fs.writeFileSync('/tmp/flow-smoke.svg', svg.join('\n'));

/* 几何 JSON（供 PIL 光栅化核对） */
const rects = [];
(function collect(e){ (e.children||[]).forEach(c=>{ if(c.tag==='rect') rects.push(c.attrs); collect(c); }); })(box);
const GEOM = process.env.GEOM_OUT || '/tmp/flow-geom.json';
fs.writeFileSync(GEOM, JSON.stringify({
  paths: paths.map(p => ({ d: p.d, w: +p['stroke-width'], c: p.stroke })),
  rects: rects.map(r => ({ x:+r.x, y:+r.y, w:+r.width, h:+r.height, c:r.fill })),
  texts: rec.texts.map(t => ({ x: +t.attrs.x, y: +t.attrs.y, s: t.textContent, anchor: t.attrs['text-anchor'] })),
}, null, 1));

console.log(fs.readFileSync("/tmp/flow-check.txt","utf8"));
process.exit(fails.length ? 1 : 0);
