/* nputweb 前端逻辑（原生 JS，无框架无构建）。

安全相关的三条硬规矩，改这个文件前先读：

1. token 走 Authorization header，**不用 cookie** → 浏览器不会自动带上，
   恶意网页也就无法借用户的身份发请求（CSRF 天然免疫）。
2. token 存 sessionStorage（关标签页即失效），**不存 localStorage**
   （后者跨标签页、重启后仍在，泄漏面更大）。
3. URL 里的 ?token= 只在加载时读一次，读走立刻 history.replaceState 抹掉，
   **之后所有请求都不再带 query** —— 否则它会留在浏览器历史、Referer 和各种日志里。
*/
'use strict';

const state = {
  token: '',
  maxChars: 5000,
  busy: false,
  healthTimer: null,
  healthFails: 0,      // 连续失败次数：够 3 次才允许写主状态栏
  healthBackoff: 0,    // 当前退避间隔（ms），0 = 没在退避
  engineStatus: '',    // 最近一次 health 的 status（'loading' / 'ready' …
};

// ---------------------------------------------------------------- 健康轮询节奏
// 为什么是 setTimeout 自调度而不是 setInterval：
// 间隔要随状态变（加载中 3s / 翻译中 1.5s / 空闲 10s），失败还要指数退避，
// setInterval 的节奏一旦定下就改不了。
//
// 下限是这样算出来的：后端 DEFAULT_RATE_PER_MIN 是 30，health 每秒都在扣配额的话
// （旧的 1.5s → 40 次/分钟）配额会被心跳自己吃光，真正的翻译请求只能拿到 429。
// 空闲 10s = 6 次/分钟，留足余量给翻译；忙碌时才回到 1.5s，因为进度要跟手。
const HEALTH_LOADING_MS = 3000;
const HEALTH_BUSY_MS = 1500;
const HEALTH_IDLE_MS = 10000;
const HEALTH_BACKOFF_MAX_MS = 30000;
const HEALTH_FAILS_BEFORE_STATUS = 3;

const el = (id) => document.getElementById(id);

/**
 * 源 / 目标两个语种选择器的三件套（输入框 / 候选列表 / 值存储）。
 *
 * 为什么两个选择器都还留着底下的 <select>：它是**唯一的值存储**。
 * runTranslate() / downloadOutput() 都是直接读 el('tgt-lang').value 的，
 * 留着它就完全不用动下游取值逻辑，回归风险最小；select 用 hidden 属性藏起来，
 * 不渲染也不进无障碍树，不会和 combobox 重复播报。
 *
 * 放在 boot() 之前声明：boot() 是顶层立即调用的，写到后面的 const 会落进 TDZ。
 */
const COMBO_DEFS = [
  { inputId: 'src-search', listId: 'src-listbox', selectId: 'src-lang' },
  { inputId: 'tgt-search', listId: 'tgt-listbox', selectId: 'tgt-lang' },
];

// createCombo() 建好后往里填，loadLanguages() / swapLanguages() 都要遍历它
const combos = [];

// ---------------------------------------------------------------- 启动
boot();

async function boot() {
  harvestTokenFromUrl();
  wireEvents();
  try {
    await loadLanguages();
  } catch (err) {
    setStatus(`语种列表加载失败：${err.message}`, 'error');
  }
  // 只踢一脚：pollHealth 结束时会自己 scheduleHealth()，别在这里再挂一个 timer
  pollHealth();
}

/**
 * 把 ?token= 从 URL 里取走。
 * 这是唯一一次容忍 token 出现在 GET 参数里 —— 之后立刻抹掉 URL 痕记，
 * 不然它会留在浏览器历史、Referer 和任何access log 里。
 */
function harvestTokenFromUrl() {
  const url = new URL(window.location.href);
  const fromQuery = url.searchParams.get('token');
  if (fromQuery) {
    state.token = fromQuery;
    sessionStorage.setItem('nputweb.token', fromQuery);
    url.searchParams.delete('token');
    history.replaceState(null, '', url.pathname + (url.search || ''));
  } else {
    state.token = sessionStorage.getItem('nputweb.token') || '';
  }
}

// ---------------------------------------------------------------- 事件
function wireEvents() {
  el('translate').addEventListener('click', () => runTranslate());
  el('clear').addEventListener('click', () => {
    el('input').value = '';
    updateCount();
    el('input').focus();
  });
  el('swap').addEventListener('click', swapLanguages);
  el('copy').addEventListener('click', copyOutput);
  el('download').addEventListener('click', downloadOutput);

  el('input').addEventListener('input', updateCount);

  for (const def of COMBO_DEFS) createCombo(def);
  // 点页面别处就收起下拉（点选项走的是 preventDefault，不会走到这里）
  document.addEventListener('mousedown', (e) => {
    for (const c of combos) {
      if (c.open && !c.input.contains(e.target) && !c.list.contains(e.target)) closeCombo(c);
    }
  });

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      runTranslate();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'i') {
      e.preventDefault();
      swapLanguages();
    }
  });
}

// ---------------------------------------------------------------- 语种
async function loadLanguages() {
  const data = await api('/api/languages');

  const src = el('src-lang');
  const tgt = el('tgt-lang');
  // 源语言额外有「自动检测」
  src.innerHTML = '';
  src.appendChild(option('auto', '自动检测'));
  fillSelect(src, data.common, 'zh');
  fillSelect(src, data.others, null);

  tgt.innerHTML = '';
  fillSelect(tgt, data.common, 'en');
  fillSelect(tgt, data.others, null);

  // <select> 填完才有候选可读：把 option 抄进 combobox，并把已选语言回显到输入框
  for (const c of combos) refreshComboItems(c);
}

function option(value, label) {
  const opt = document.createElement('option');
  opt.value = value;
  opt.textContent = label;
  return opt;
}

function fillSelect(select, items, selected) {
  for (const lang of items || []) {
    const opt = option(lang.code, `${lang.zh_name} · ${lang.en_name} (${lang.code})`);
    opt.dataset.search = `${lang.code} ${lang.zh_name} ${lang.en_name} ${lang.native} ${lang.prompt_name}`.toLowerCase();
    if (lang.code === selected) opt.selected = true;
    select.appendChild(opt);
  }
}

/* --------------------------------------------------------------- combobox
 *
 * 为什么不让 <select> 自己开门：
 * 1) 老方案里 input 和 select 是两个独立控件 —— 打字只是把 <option> 设成 hidden，
 *    输入框自己永远空着，用户既看不到打了什么，也看不到匹配结果；
 * 2) <option hidden> 在 Chromium 上有长期缺陷（crbug 139595：隐藏项不会让下拉列表
 *    resize），基于它的过滤方案本身就站不住。
 *
 * 现在的状态机很简单，只有两条：
 * - 关着：input.value = 已选语言的标签（回显），候选列表 hidden。
 * - 开着：input.value = 搜索词（框里清空，由列表承载候选），关掉时回退成回显。
 *   这样半截搜索词永远不会留在框里。
 */
function createCombo(def) {
  const c = {
    input: el(def.inputId),
    list: el(def.listId),
    select: el(def.selectId),
    items: [],   // 全部候选（loadLanguages 之后才有内容）
    shown: [],   // 当前过滤出来的候选
    active: -1,  // 高亮项在 shown 里的下标
    open: false,
  };

  // 开门一律先清空回显：开着的框里装的就是搜索词（placeholder 出来提示能打字），
  // 关着才装已选语言。两态分明，就不会出现「框里是回显、列表却按别的词过滤」。
  c.input.addEventListener('focus', () => { c.input.value = ''; openCombo(c, ''); });
  // Esc 收起后焦点还在 input 上，focus 不会再触发 —— 靠 click 重新开门
  c.input.addEventListener('click', () => {
    if (!c.open) { c.input.value = ''; openCombo(c, ''); }
  });
  c.input.addEventListener('input', () => openCombo(c, c.input.value));
  c.input.addEventListener('keydown', (e) => comboKeydown(c, e));
  c.input.addEventListener('blur', () => closeCombo(c));

  // 用 mousedown 而不是 click：先 preventDefault 挡掉 input 失焦，
  // 否则 blur 会把列表先关掉，click 就落在空气上了。
  c.list.addEventListener('mousedown', (e) => {
    const li = e.target.closest('.combo-option');
    if (!li || !li.dataset.value) return;
    e.preventDefault();
    chooseCombo(c, li.dataset.value);
  });
  // 鼠标移到哪就高亮到哪：hover 与键盘高亮始终只有一个，观感不会打架
  c.list.addEventListener('mouseover', (e) => {
    const li = e.target.closest('.combo-option');
    if (!li || !c.open) return;
    const idx = Number(li.dataset.index);
    if (Number.isInteger(idx) && idx !== c.active) setActive(c, idx, false);
  });

  combos.push(c);
  return c;
}

/**
 * 从 <select> 的 <option> 里抄出候选。
 * 匹配用的 haystack 直接复用 fillSelect 写在 dataset.search 上的那份
 * （代码 / 中文名 / 英文名 / native / prompt_name 的小写拼接），
 * 所以「中文」「chinese」「zh」「日本語」都搜得到，不用另建索引。
 */
function refreshComboItems(c) {
  c.items = Array.from(c.select.options).map((opt) => ({
    value: opt.value,
    label: opt.textContent,
    search: opt.dataset.search || opt.textContent.toLowerCase(),
  }));
  c.shown = c.items;
  c.active = -1;
  syncComboText(c);
}

function openCombo(c, query) {
  c.open = true;
  c.list.removeAttribute('hidden');
  c.input.setAttribute('aria-expanded', 'true');
  applyFilter(c, query);
}

function closeCombo(c) {
  c.open = false;
  c.active = -1;
  c.list.setAttribute('hidden', '');
  c.input.setAttribute('aria-expanded', 'false');
  c.input.removeAttribute('aria-activedescendant');
  syncComboText(c);
}

function applyFilter(c, query) {
  const q = (query || '').trim().toLowerCase();
  c.shown = q ? c.items.filter((it) => it.search.includes(q)) : c.items;

  // 有过滤词就高亮第一条（回车直接选中它）；没有就高亮当前已选的那条
  const current = c.shown.findIndex((it) => it.value === c.select.value);
  c.active = current >= 0 ? current : (q && c.shown.length ? 0 : -1);

  renderCombo(c);
}

function renderCombo(c) {
  c.list.textContent = '';

  if (!c.shown.length) {
    // 空态也占一个 option 位，键盘和读屏都不会撞进「什么都没有」的列表
    const empty = document.createElement('li');
    empty.className = 'combo-option is-empty';
    empty.setAttribute('role', 'option');
    empty.setAttribute('aria-disabled', 'true');
    empty.textContent = '无匹配';
    c.list.appendChild(empty);
    c.input.removeAttribute('aria-activedescendant');
    return;
  }

  c.shown.forEach((it, i) => {
    const li = document.createElement('li');
    li.className = 'combo-option';
    li.id = `${c.list.id}-opt-${i}`;
    li.dataset.index = String(i);
    li.dataset.value = it.value;
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', String(it.value === c.select.value));
    li.textContent = it.label;
    c.list.appendChild(li);
  });
  paintActive(c);
}

/** 只搬 .active 类和 aria-activedescendant，不重建 DOM ——
 *  重建会让鼠标下的 li 换新，可能连环触发 mouseover。 */
function paintActive(c) {
  const nodes = c.list.children;
  for (let i = 0; i < nodes.length; i++) {
    nodes[i].classList.toggle('active', i === c.active);
    if (i === c.active) c.input.setAttribute('aria-activedescendant', nodes[i].id);
  }
  if (c.active < 0) c.input.removeAttribute('aria-activedescendant');
}

function setActive(c, idx, scroll) {
  if (!c.shown.length) {
    c.active = -1;
    paintActive(c);
    return;
  }
  c.active = Math.max(0, Math.min(idx, c.shown.length - 1));
  paintActive(c);
  if (scroll) {
    const node = c.list.children[c.active];
    if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' });
  }
}

function chooseCombo(c, value) {
  // 只认当前可见项：过滤态下绝不把被筛掉的语言设成选中值（否则下拉显示空白）
  if (!c.shown.some((it) => it.value === value)) return;
  c.select.value = value;
  closeCombo(c);
}

function syncComboText(c) {
  const it = c.items.find((x) => x.value === c.select.value);
  c.input.value = it ? it.label : '';
}

function comboKeydown(c, e) {
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (c.open) setActive(c, c.active + 1, true);
    else reopen(c);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (c.open) setActive(c, c.active - 1, true);
    else reopen(c);
  } else if (e.key === 'Enter') {
    // Ctrl/Cmd+Enter 是全局的「翻译」，这里必须放行 —— 否则焦点在语种框里时
    // 下拉一开就把翻译快捷键吃掉了
    if (!c.open || e.ctrlKey || e.metaKey) return;
    e.preventDefault();   // 拦住，别冒到 document 级快捷键上
    const it = c.shown[c.active] || c.shown[0];
    if (it) chooseCombo(c, it.value);
  } else if (e.key === 'Escape') {
    if (!c.open) return;
    e.preventDefault();
    closeCombo(c);        // 回退成回显，半截搜索词不留在框里
  } else if (e.key === 'Tab') {
    closeCombo(c);        // 不 preventDefault，焦点照常往下走
  } else if (!c.open && (e.key === 'Backspace' || e.key === 'Delete')) {
    // 关着的时候按退格：先清空回显再开门，否则会把「中文 · Chinese (zh)」当搜索词
    e.preventDefault();
    reopen(c);
  } else if (!c.open && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    // 关着的时候直接打字（典型是 Esc 收起后接着打）：先清空回显，
    // 让接下来的按键只落下这一个字符，而不是「回显 + 字符」这种搜不出东西的词
    c.input.value = '';
  }
}

/** 关着的时候重新开门：清空框、展开全量候选。 */
function reopen(c) {
  c.input.value = '';
  openCombo(c, '');
}

function swapLanguages() {
  const src = el('src-lang');
  const tgt = el('tgt-lang');
  const srcVal = src.value;
  const tgtVal = tgt.value;
  src.value = tgtVal === 'auto' ? 'auto' : tgtVal;
  tgt.value = srcVal === 'auto' ? 'en' : srcVal;

  // 交换后必须同步两个 combobox：老代码只换 <select> 的值，
  // 过滤态下换出来的值可能是被筛掉的项（下拉就空白），输入框还留着半截搜索词。
  // 这里先把 shown 复位成全集再关，保证换上去的值一定在可见项里。
  for (const c of combos) {
    c.shown = c.items;
    closeCombo(c);
  }
}

// ---------------------------------------------------------------- 健康轮询
/**
 * 下一次心跳的间隔：忙碌 1.5s（进度要跟手）、引擎加载中 3s、空闲 10s。
 * 翻译那一路的 setBusy() 会在状态翻转时重排，所以这里不必担心「已经挂了 10s 才开工」。
 */
function nextHealthDelay() {
  if (state.busy) return HEALTH_BUSY_MS;
  if (state.engineStatus === 'loading') return HEALTH_LOADING_MS;
  return HEALTH_IDLE_MS;
}

/** 排下一次心跳。先 clearTimeout —— 保证任何时刻只存在一条链，不会两条并行。 */
function scheduleHealth(delayMs) {
  clearTimeout(state.healthTimer);
  state.healthTimer = setTimeout(pollHealth, delayMs);
}

/**
 * health 失败**不写主状态栏**，只把设备徽标置成不可达态。
 *
 * 为什么：心跳是秒级的，一次失败就把状态栏刷红，会把刚翻译成功的提示冲掉，
 * 用户以为翻译挂了于是反复刷新 —— 刷新 abort 掉 in-flight 请求，
 * 服务端就刷 ConnectionResetError（这是 nputweb 那个 P0 的次生伤害）。
 * 真正的「服务不可达」要**连续 3 次**都失败才报。
 *
 * 视觉效果由 style.css 提供，这里只负责挂/摘 class（类名：unreachable）。
 */
function setDeviceReachable(reachable) {
  const badge = el('device-badge');
  badge.classList.toggle('unreachable', !reachable);
  if (!reachable) {
    badge.textContent = '设备不可达';
    badge.title = '健康检查失败，正在按退避策略重试…';
  }
}

async function pollHealth() {
  let delay;
  try {
    const data = await api('/api/health');
    // 成功一次就复位：退避清零、失败计数清零、徽标恢复
    state.healthFails = 0;
    state.healthBackoff = 0;
    state.engineStatus = data.status || '';
    setDeviceReachable(true);

    const device = data.device || '—';
    el('device-badge').textContent = device;
    el('device-badge').title = `设备链：${(data.devices || []).join(' → ')}${
      (data.degraded || []).length ? `（已降级：${data.degraded.join(',')}）` : ''}`;

    if (state.busy) {
      const p = data.progress || {};
      if (p.active && p.total > 1) {
        setStatus(`翻译中 ${p.done}/${p.total} 段`, 'busy');
        setProgress((p.done / p.total) * 100, false);
      } else if (data.waiting > 0) {
        setStatus(`排队中，前面还有 ${data.waiting} 个请求`, 'busy');
        setProgress(0, true);
      }
    } else if (data.status === 'loading') {
      el('device-badge').textContent = `${device}（加载中）`;
    }
    delay = nextHealthDelay();
  } catch (err) {
    state.healthFails += 1;
    setDeviceReachable(false);
    if (!state.busy && state.healthFails >= HEALTH_FAILS_BEFORE_STATUS) {
      setStatus(`服务不可达：${err.message}`, 'error');
    }
    // 指数退避 ×2，上限 30s；服务真挂了也别拿 1.5s 的节奏去砸它
    state.healthBackoff = state.healthBackoff
      ? Math.min(state.healthBackoff * 2, HEALTH_BACKOFF_MAX_MS)
      : HEALTH_IDLE_MS;
    delay = state.healthBackoff;
  }
  // 唯一的调度点：成功走正常节奏，失败走退避，两条路都只排一次
  scheduleHealth(delay);
}

// ---------------------------------------------------------------- 翻译
async function runTranslate() {
  if (state.busy) return;
  const text = el('input').value;
  if (!text.trim()) {
    setStatus('请输入要翻译的内容', 'warn');
    el('input').focus();
    return;
  }
  if (text.length > state.maxChars) {
    setStatus(`输入 ${text.length} 字符，超过上限 ${state.maxChars}`, 'error');
    return;
  }

  setBusy(true);
  setStatus('提交中…', 'busy');
  setProgress(0, true);

  const started = performance.now();
  try {
    const data = await api('/api/translate', {
      method: 'POST',
      body: JSON.stringify({
        text,
        target: el('tgt-lang').value,
        source: el('src-lang').value,
        newline: el('newline').value,
      }),
    });
    el('output').value = data.text || '';
    const secs = ((performance.now() - started) / 1000).toFixed(2);
    const speed = data.chars_per_second ? ` · ${data.chars_per_second} 字符/s` : '';
    setStatus(
      `${data.device || '?'} · ${data.segments || 1} 段 · ${secs}s${speed}` +
      `${data.cached ? ' · 缓存命中' : ''}`,
      data.failed ? 'warn' : 'ok',
    );
    if (data.failed) {
      toast(`有 ${data.failed} 段未译出，已保留原文`);
    }
  } catch (err) {
    setStatus(`翻译失败：${err.message}`, 'error');
  } finally {
    setBusy(false);
    setProgress(100, false);
    setTimeout(() => el('progress').setAttribute('hidden', ''), 400);
  }
}

// ---------------------------------------------------------------- 输出动作
async function copyOutput() {
  const text = el('output').value;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制');
  } catch (err) {
    setStatus(`复制失败：${err.message}`, 'error');
  }
}

/**
 * 下载 .txt：**浏览器端 Blob 生成，完全不经过服务端**。
 * 服务端不做任何写文件的动作 —— 第 9 条安全措施（无文件上传 / 无写入）。
 */
function downloadOutput() {
  const text = el('output').value;
  if (!text) return;
  const blob = new Blob(['\ufeff', text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const code = el('tgt-lang').value;
  a.href = url;
  a.download = `nputweb-${code}-${Date.now()}.txt`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  toast('已生成下载');
}

// ---------------------------------------------------------------- 小工具
async function api(path, init) {
  const headers = { Accept: 'application/json' };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (init && init.body) headers['Content-Type'] = 'application/json; charset=utf-8';

  let res;
  try {
    res = await fetch(path, { ...(init || {}), headers });
  } catch (err) {
    throw new Error('网络不可达');
  }

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body && body.error ? body.error.message : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return res.json();
}

function setBusy(busy) {
  state.busy = busy;
  el('translate').disabled = busy;
  el('translate').textContent = busy ? '翻译中…' : '翻译';
  el('progress').removeAttribute('hidden');
  // 忙碌状态一翻转就重排心跳：不这么做的话，点下「翻译」后还得干等上一次排好的
  // 10s 空闲间隔走完才有第一条进度 —— 翻译中段数提示会明显迟钝。
  // scheduleHealth 内部先 clearTimeout，所以这里不会多出第二条链。
  scheduleHealth(nextHealthDelay());
}

function setStatus(text, tone) {
  const node = el('status');
  node.textContent = text;
  if (tone) node.dataset.tone = tone;
}

function setProgress(pct, indeterminate) {
  const fill = el('progress-fill');
  fill.classList.toggle('indeterminate', !!indeterminate);
  if (!indeterminate) fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
}

function updateCount() {
  const n = el('input').value.length;
  const node = el('char-count');
  node.textContent = `${n} / ${state.maxChars}`;
  node.classList.toggle('warn', n > state.maxChars * 0.9 && n <= state.maxChars);
  node.classList.toggle('over', n > state.maxChars);
}

let toastTimer = null;
function toast(text) {
  const node = el('toast');
  node.textContent = text;
  node.removeAttribute('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.setAttribute('hidden', ''), 2200);
}
