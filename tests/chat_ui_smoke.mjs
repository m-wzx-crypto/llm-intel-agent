/* 聊天交互冒烟测试：直接执行 index.html 中的 ask() 与 pushMsg()。 */
import fs from 'node:fs';
import vm from 'node:vm';

const src = fs.readFileSync('src/web/index.html', 'utf8');
function extractFunction(marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数: ' + marker);
  const open = src.indexOf('{', start);
  let depth = 0;
  let end = open;
  for (; end < src.length; end++) {
    if (src[end] === '{') depth++;
    if (src[end] === '}' && --depth === 0) break;
  }
  return src.slice(start, end + 1);
}

function element() {
  return {
    style: {}, value: '', disabled: false, textContent: '', innerHTML: '',
    children: [], removed: false,
    appendChild(child) { this.children.push(child); },
    remove() { this.removed = true; },
  };
}
const nodes = {
  '#chat-empty': element(), '#ask': element(), '#send': element(),
  '#msgs': element(),
};
const messages = [];
const openedDetails = [];
const openedUrls = [];
let chatTimeout = 0;
const ctx = {
  console,
  document: { createElement: element, querySelectorAll: () => [] },
  $: id => nodes[id],
  pushMsg: (role, text) => messages.push({ role, text }),
  apiPost: async (_path, _body, _fallback, ms) => { chatTimeout = ms; return null; },
  NET_DOWN: 1,
  esc: value => String(value == null ? '' : value),
  openDetail: id => openedDetails.push(id),
  window: { open: url => openedUrls.push(url) },
};
vm.createContext(ctx);
vm.runInContext(extractFunction('async function ask(q){'), ctx);
await ctx.ask('测试超时');

const failures = [];
if (nodes['#send'].disabled) failures.push('超时后发送按钮仍被禁用');
if (chatTimeout !== 150000) failures.push(`聊天超时应为 150000ms，实际 ${chatTimeout}ms`);
if (nodes['#msgs'].children.some(child => !child.removed && child.innerHTML.includes('dots')))
  failures.push('超时后加载动画未清理');

vm.runInContext(extractFunction('function pushMsg('), ctx);
ctx.pushMsg('ai', '回答', [
  { id: 7, title: '本地证据' },
  { title: '联网证据', url: 'https://example.com/article' },
  { title: '危险引用', url: 'javascript:alert(1)' },
  { title: '文件引用', url: 'file:///etc/passwd' },
]);
const buttons = [];
(function collect(node) {
  if (node.className === 'cite') buttons.push(node);
  (node.children || []).forEach(collect);
})(nodes['#msgs']);
buttons.forEach(button => button.onclick());
if (openedDetails.length !== 1 || openedDetails[0] !== 7)
  failures.push(`本地引用应打开详情 7，实际 ${JSON.stringify(openedDetails)}`);
if (openedUrls.length !== 1 || openedUrls[0] !== 'https://example.com/article')
  failures.push(`联网引用应打开原 URL，实际 ${JSON.stringify(openedUrls)}`);

if (failures.length) {
  console.error('FAIL ' + failures.join('；'));
  process.exit(1);
}
console.log('PASS 聊天超时恢复与引用点击');
