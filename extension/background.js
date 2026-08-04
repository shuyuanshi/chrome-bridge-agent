/**
 * Chrome Bridge - Background Service Worker
 *
 * 连接 Python bridge server（ws://localhost:9333），接收命令并执行：
 * - navigate / wait_for_load: chrome.tabs.update + 轮询 tab.status
 * - 所有页面内命令（evaluate / snapshot / click / fill / fetch …）:
 *   chrome.scripting.executeScript(world:"MAIN")，被 CSP 挡住时回落 CDP
 * - screenshot: chrome.debugger + Page.captureScreenshot（支持按元素裁剪）
 * - get_cookies: chrome.cookies.getAll
 * - browse_open/do/close: 持久标签页多步操作，可命名复用
 *
 * 没有 content script：页面侧逻辑全部走 MAIN world 注入，见 pageExecutor。
 */

const BRIDGE_URL = "ws://localhost:9333";
const CSP_RULE_ID = 9999;
const MAX_INLINE_CHARS = 4_000_000; // 更大的结果走 __cb_buf 分块读

let ws = null;
let _reconnectDelay = 1000;
let _reconnectTimer = null;

// ───────────────────────── 持久化状态 ─────────────────────────
// MV3 的 service worker 随时会被回收，纯内存状态会丢。tab_id 直接用
// Chrome 真实 tab id，即使 SW 重启也能 chrome.tabs.get 拿回来；
// 命名 session 与 CSP 白名单存 chrome.storage.session。

let _managedTabId = null;          // navigate / evaluate 默认用的后台标签页
const _sessions = new Map();       // name -> tabId
const _bridgeTabs = new Set();     // 由 bridge 创建的 tab id（CSP 规则作用域）

async function persistState() {
  try {
    await chrome.storage.session.set({
      state: {
        managedTabId: _managedTabId,
        sessions: [..._sessions.entries()],
        bridgeTabs: [..._bridgeTabs],
      },
    });
  } catch (e) {
    console.warn("[Chrome Bridge] state persist failed", e);
  }
}

// ───────────────────────── 结构化错误 ─────────────────────────
// 页面侧返回 {__bridge_error:{code,message,detail}}，这里转成带 .bridge 的
// Error，最终以 {"error":{code,...}} 发回 Python，客户端映射成具体异常类型。

function bridgeError(code, message, detail) {
  const err = new Error(message);
  err.bridge = { code, message };
  if (detail !== undefined) err.bridge.detail = detail;
  return err;
}

function unwrapPageResult(value) {
  if (value && typeof value === "object" && "__bridge_error" in value) {
    const e = value.__bridge_error;
    if (typeof e === "string") throw bridgeError("PAGE_ERROR", e);
    throw bridgeError(e.code || "PAGE_ERROR", e.message || "page error", e.detail);
  }
  return value;
}

// ───────────────────────── CSP 作用域 ─────────────────────────
// 老版本用 updateDynamicRules 对 <all_urls> 永久剥 CSP —— 那是给整个浏览
// 配置文件降级，且浏览器重启后依然生效。现在改成 session 规则 + tabIds，
// 只对 bridge 自己开的标签页生效（tabIds 条件仅 session 规则支持）。

const CSP_ACTION = {
  type: "modifyHeaders",
  responseHeaders: [
    { header: "content-security-policy", operation: "remove" },
    { header: "content-security-policy-report-only", operation: "remove" },
  ],
};

async function syncCspRule() {
  const tabIds = [..._bridgeTabs];
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds: [CSP_RULE_ID],
      addRules: tabIds.length
        ? [
            {
              id: CSP_RULE_ID,
              priority: 1,
              action: CSP_ACTION,
              condition: {
                urlFilter: "*",
                resourceTypes: ["main_frame", "sub_frame"],
                tabIds,
              },
            },
          ]
        : [],
    });
  } catch (e) {
    console.warn("[Chrome Bridge] CSP rule sync failed", e);
  }
}

// 一次性清理：干掉 <=1.0.3 留下的、对所有站点永久生效的持久化规则。
chrome.declarativeNetRequest
  .updateDynamicRules({ removeRuleIds: [CSP_RULE_ID] })
  .catch((e) => console.warn("[Chrome Bridge] legacy CSP rule cleanup failed", e));

// SW 每次激活都要把状态捞回来；命令处理前会 await 这个 promise。
const _hydrated = (async () => {
  try {
    const { state } = await chrome.storage.session.get("state");
    if (!state) return;
    _managedTabId = state.managedTabId ?? null;
    for (const [name, id] of state.sessions || []) _sessions.set(name, id);
    for (const id of state.bridgeTabs || []) _bridgeTabs.add(id);
    await syncCspRule();
  } catch (e) {
    console.warn("[Chrome Bridge] state hydrate failed", e);
  }
})();

// ───────────────────────── chrome.debugger 串行化 ─────────────────────────
// 同一个 tab 上并发 attach 会互相踩（"Another debugger is already attached"），
// 所以按 tab 排队，并把 DevTools 占用翻译成可识别的错误码。

const _dbgLocks = new Map();

function withDebugger(tabId, fn) {
  const prev = _dbgLocks.get(tabId) || Promise.resolve();
  const run = prev.then(async () => {
    const target = { tabId };
    try {
      await chrome.debugger.attach(target, "1.3");
    } catch (e) {
      const msg = String(e.message || e);
      if (/another debugger|already attached/i.test(msg)) {
        throw bridgeError(
          "DEBUGGER_BUSY",
          "another debugger is attached to this tab — close DevTools for it and retry",
        );
      }
      if (/cannot attach to this target/i.test(msg)) {
        // Raw Chrome text here is useless. The real cause is almost always a
        // page that never loaded (bad host, DNS failure) or a privileged URL.
        const tab = await chrome.tabs.get(tabId).catch(() => null);
        throw bridgeError(
          "RESTRICTED_URL",
          `cannot run code in this tab (${(tab && tab.url) || "unknown url"}) — the page ` +
            "failed to load, or it is a chrome:// / Web Store / error page. Check the URL.",
          { url: tab && tab.url },
        );
      }
      throw e;
    }
    try {
      return await fn(target, (method, params) =>
        chrome.debugger.sendCommand(target, method, params),
      );
    } finally {
      await chrome.debugger.detach(target).catch(() => {});
    }
  });
  const settled = run.then(
    () => {},
    () => {},
  );
  _dbgLocks.set(tabId, settled);
  // Drop the entry once this is the last queued user, so the map doesn't grow
  // one permanent promise per tab ever debugged.
  settled.then(() => {
    if (_dbgLocks.get(tabId) === settled) _dbgLocks.delete(tabId);
  });
  return run;
}

// ───────────────────────── 页面内执行（含 CDP 回落） ─────────────────────────

/**
 * 在 tab 的 MAIN world 里跑一个函数。
 * 先试 chrome.scripting.executeScript；被 CSP / 权限挡住时回落到
 * chrome.debugger 的 Runtime.evaluate。
 *
 * @returns {Promise<any>} 函数的返回值（已解包）
 */
async function runFunc(tabId, func, args) {
  const viaCdp = (why) => {
    console.log("[Chrome Bridge] executeScript unusable, trying CDP:", String(why).substring(0, 80));
    return withDebugger(tabId, async (_target, send) => {
      const expression = `(${func.toString()}).apply(null, ${JSON.stringify(args)})`;
      const res = await send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true,
      });
      if (res.exceptionDetails) {
        const desc =
          res.exceptionDetails.exception?.description ||
          res.exceptionDetails.text ||
          "CDP evaluation failed";
        throw bridgeError("JS_ERROR", desc);
      }
      return res.result.value;
    });
  };

  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func,
      args,
    });
    const value = results?.[0]?.result;
    // A strict CSP can make the injection vanish without throwing, which used
    // to surface as a silent `null`. pageExecutor always returns *something*,
    // so `undefined` means the script never ran. Only retry for tabs outside
    // the CSP-strip scope — a bridge-owned tab has no CSP to trip over, and
    // `evaluate("undefined")` there is a legitimate undefined.
    if (value === undefined && !_bridgeTabs.has(tabId)) {
      return await viaCdp("empty result on a tab outside the CSP-strip scope");
    }
    return value;
  } catch (e) {
    const msg = e.message || String(e);
    // Check "gone" first: "No frame with id" also matches the CSP pattern
    // below, and retrying a dead tab over CDP just yields a confusing error.
    if (/No tab with id|No frame with id/i.test(msg)) {
      throw bridgeError("TAB_GONE", `tab ${tabId} is gone: ${msg}`);
    }
    if (!/Cannot access|extension|blocked|prohibited|Forbidden|Frame with ID/i.test(msg)) {
      throw e;
    }
    return await viaCdp(msg);
  }
}

/** 在页面里跑一条 bridge 命令。 */
async function runInPage(tabId, method, params) {
  return unwrapPageResult(await runFunc(tabId, pageExecutor, [method, params]));
}

// ───────────────────────── WebSocket ─────────────────────────

function connect() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }

  ws = new WebSocket(BRIDGE_URL);

  ws.onopen = () => {
    console.log("[Chrome Bridge] connected to bridge server");
    _reconnectDelay = 1000;
    // The version lets the relay (and `status`) spot a stale service worker
    // that predates tab_id routing, instead of quietly using the wrong tab.
    ws.send(
      JSON.stringify({ role: "extension", version: chrome.runtime.getManifest().version }),
    );
  };

  ws.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    const socket = ws;
    let payload;
    try {
      const result = await handleCommand(msg);
      payload = { id: msg.id, result: result ?? null };
    } catch (err) {
      payload = {
        id: msg.id,
        error: err && err.bridge ? err.bridge : { code: "INTERNAL", message: String(err?.message || err) },
      };
    }
    // 命令可能跑了很久，期间连接可能已经换掉；对着死 socket send 会抛。
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify(payload));
      } catch (e) {
        console.warn("[Chrome Bridge] failed to deliver reply", e);
      }
    } else {
      console.warn("[Chrome Bridge] dropped reply for", msg.method, "— socket closed");
    }
  };

  ws.onclose = () => {
    // 指数退避 + 抖动：server 没起来时别每 3s 敲一次。
    const delay = Math.min(30000, _reconnectDelay) * (0.8 + Math.random() * 0.4);
    console.log(`[Chrome Bridge] disconnected, reconnecting in ${Math.round(delay)}ms`);
    _reconnectDelay = Math.min(30000, _reconnectDelay * 1.8);
    _reconnectTimer = setTimeout(connect, delay);
  };

  ws.onerror = (e) => {
    console.error("[Chrome Bridge] WebSocket error", e);
  };
}

// service worker 保活兜底
chrome.alarms.create("keepAlive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(() => {
  if (!ws || ws.readyState !== WebSocket.OPEN) connect();
});

// 标签页被用户关掉时清理登记，顺便收窄 CSP 规则
chrome.tabs.onRemoved.addListener(async (tabId) => {
  // The removal event can be what wakes the service worker, in which case the
  // maps are still empty — without this the cleanup silently does nothing.
  await _hydrated;
  let dirty = false;
  if (_bridgeTabs.delete(tabId)) dirty = true;
  if (_managedTabId === tabId) {
    _managedTabId = null;
    dirty = true;
  }
  for (const [name, id] of _sessions.entries()) {
    if (id === tabId) {
      _sessions.delete(name);
      dirty = true;
    }
  }
  if (dirty) {
    await syncCspRule();
    await persistState();
  }
});

// ───────────────────────── 命令路由 ─────────────────────────

const BROWSER_LEVEL = new Set([
  "reload_self",
  "get_cookies",
  "browse_open",
  "browse_close",
  "list_sessions",
  "list_tabs",
]);

async function handleCommand(msg) {
  await _hydrated;
  const { method, params = {} } = msg;

  switch (method) {
    // ── 浏览器级 ──
    case "reload_self":
      chrome.runtime.reload();
      return { ok: true, message: "extension reloading..." };

    case "get_cookies":
      return await cmdGetCookies(params);

    case "browse_open":
      return await cmdBrowseOpen(params);

    case "browse_close":
      return await cmdBrowseClose(params);

    case "list_sessions":
      return [..._sessions.entries()].map(([name, tab_id]) => ({ name, tab_id: String(tab_id) }));

    case "list_tabs":
      return await cmdListTabs(params);

    case "activate_tab":
      return await cmdActivateTab(params);

    // ── 需要 tab 但不在页面里执行的 ──
    case "navigate":
      return await cmdNavigate(params);

    case "wait_for_load":
      return await cmdWaitForLoad(params);

    case "screenshot_element":
    case "screenshot":
      return await cmdScreenshot(params);

    case "set_file_input":
      return await cmdSetFileInput(params);

    case "cdp_mouse":
      return await cmdCdpMouse(params);

    // ── 等待类命令：轮询必须留在 service worker 里 ──
    // Chrome 会把后台标签页的 setTimeout 节流到分钟级，页面内的轮询循环会
    // 慢到不可用。SW 不受这个节流，所以由它来打点、每次 executeScript 探一下。
    case "wait_for_selector": {
      const tab = await resolveTab(params);
      await waitForSelector(tab.id, params.selector, params.timeout || 30000);
      return true;
    }

    case "wait_dom_stable": {
      const tab = await resolveTab(params);
      return await cmdWaitDomStable(tab, params);
    }

    case "browse_do":
      return await cmdBrowseDo(params);

    case "browse_and_eval":
      return await cmdBrowseAndEval(params);

    // ── 其余全部在页面 MAIN world 里执行 ──
    default: {
      if (BROWSER_LEVEL.has(method)) throw bridgeError("BAD_REQUEST", `unrouted method: ${method}`);
      const tab = await resolveTab(params);
      if (params.wait_selector) {
        await waitForSelector(tab.id, params.wait_selector, params.wait_timeout || 15000);
      }
      if (method === "page_fetch" && params.max_inline == null) params.max_inline = MAX_INLINE_CHARS;
      return await runInPage(tab.id, method, params);
    }
  }
}

// ───────────────────────── Tab 解析 ─────────────────────────

/**
 * 每个命令都可以带 tab_id（或 session 名）来指定作用的标签页；
 * 不带就用共享的 managed tab。这一层是 click_element / wait_for_selector /
 * screenshot 等能在 browse session 里使用的原因。
 */
async function resolveTab(params = {}) {
  const ref = params.tab_id ?? params.session ?? null;
  if (ref === null || ref === "") return await getOrOpenManagedTab();

  if (typeof ref === "string" && _sessions.has(ref)) {
    return await getTabOrThrow(_sessions.get(ref), ref);
  }
  const numeric = Number(ref);
  if (!Number.isFinite(numeric)) {
    throw bridgeError("TAB_GONE", `unknown tab or session: ${ref}`);
  }
  return await getTabOrThrow(numeric, ref);
}

async function getTabOrThrow(tabId, label) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) {
    throw bridgeError(
      "TAB_GONE",
      `tab ${label} no longer exists (closed, or the browser restarted)`,
      { tab_id: String(tabId) },
    );
  }
  return tab;
}

async function getOrOpenManagedTab() {
  if (_managedTabId !== null) {
    const existing = await chrome.tabs.get(_managedTabId).catch(() => null);
    if (existing) return existing;
  }
  const tab = await createBridgeTab(null);
  _managedTabId = tab.id;
  await persistState();
  return tab;
}

/**
 * 开一个后台标签页。
 *
 * `chrome.tabs.create` 不带 windowId 时打到「当前窗口」，而 Chrome 在没有
 * 窗口的时候（用户关掉了所有窗口，但 "continue running background apps"
 * 让 service worker 还活着）会直接抛 "No current window" —— 整座桥就废了。
 * 这种情况下退回到任意一个普通窗口，实在没有就自己开一个不抢焦点的。
 */
async function newBackgroundTab() {
  try {
    return await chrome.tabs.create({ url: "about:blank", active: false });
  } catch (e) {
    if (!/no current window|no window with id/i.test(String(e.message || e))) throw e;
  }

  const windows = await chrome.windows.getAll({ windowTypes: ["normal"] }).catch(() => []);
  if (windows.length) {
    return await chrome.tabs.create({
      url: "about:blank",
      active: false,
      windowId: windows[0].id,
    });
  }

  // 一个窗口都没有：自己开一个，不抢焦点。新窗口自带一个标签页，直接用它。
  const created = await chrome.windows.create({
    url: "about:blank",
    focused: false,
    state: "minimized",
  });
  const tabs = created && created.tabs ? created.tabs : await chrome.tabs.query({ windowId: created.id });
  if (!tabs || !tabs.length) {
    throw bridgeError(
      "NO_BROWSER_WINDOW",
      "Chrome has no window to open a tab in. Open a Chrome window and retry.",
    );
  }
  return tabs[0];
}

/**
 * 建一个属于 bridge 的后台标签页。
 * 先开 about:blank 并登记进 CSP 白名单，再导航 —— 否则目标页的响应头
 * 已经到了，CSP 规则来不及生效。
 */
async function createBridgeTab(url) {
  const tab = await newBackgroundTab();
  _bridgeTabs.add(tab.id);
  await syncCspRule();
  await persistState();
  if (url && url !== "about:blank") {
    await chrome.tabs.update(tab.id, { url });
  }
  return tab;
}

// ───────────────────────── 导航 ─────────────────────────

/** 忽略末尾斜杠的 URL 相等判断（"https://x.com" vs "https://x.com/"）。 */
function sameUrl(a, b) {
  if (!a || !b) return false;
  return a === b || a.replace(/\/+$/, "") === b.replace(/\/+$/, "");
}

/**
 * 导航一个标签页并等它稳定。
 *
 * 只有目标 URL 与当前 URL 不同才要求「URL 变过」；重新加载同一个地址时
 * URL 不会变，那种情况只能等 status，否则就是白等到超时。
 */
async function navigateTab(tab, url, { timeout = 60000, settle_ms = 0 } = {}) {
  const before = tab.url;
  await chrome.tabs.update(tab.id, { url });
  // 给 Chrome 一点时间把 status 翻成 loading，否则会读到上一页的 complete。
  await sleep(250);
  await waitForTabComplete(tab.id, {
    timeout,
    changedFrom: sameUrl(before, url) ? null : before,
  });
  if (settle_ms > 0) await sleep(settle_ms);
  return await chrome.tabs.get(tab.id);
}

async function cmdNavigate({ url, timeout = 60000, settle_ms = 0, ...rest }) {
  const tab = await resolveTab(rest);
  await navigateTab(tab, url, { timeout, settle_ms });
  return null;
}

async function cmdWaitForLoad({ timeout = 60000, ...rest }) {
  const tab = await resolveTab(rest);
  await waitForTabComplete(tab.id, { timeout });
  return null;
}

/**
 * 等标签页加载完成。
 *
 * 老实现用 `expectedUrl.slice(0, 20)` 做前缀匹配，既会被 SSO 跳转骗过，
 * 又会在事件先于监听触发时漏掉；而且 poll 链在 resolve 之后还在跑。
 * 现在纯轮询 + settled 标志，并且在给了 changedFrom 时要求 URL 真的变过。
 */
async function waitForTabComplete(tabId, { timeout = 60000, changedFrom = null } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw bridgeError("TAB_GONE", `tab ${tabId} was closed while loading`);
    const urlSettled = !changedFrom || tab.url !== changedFrom;
    if (tab.status === "complete" && urlSettled) return tab;
    if (Date.now() > deadline) {
      throw bridgeError(
        "NAV_TIMEOUT",
        `page did not finish loading within ${timeout}ms (status=${tab.status}, url=${tab.url})`,
        { url: tab.url, status: tab.status },
      );
    }
    await sleep(150);
  }
}

/** Poll the DOM size from the service worker until it stops changing. */
async function cmdWaitDomStable(tab, { timeout = 10000, interval = 500 }) {
  const started = Date.now();
  let last = -1;
  for (;;) {
    const size = await runFunc(tab.id, () => (document.body ? document.body.innerHTML.length : 0), []);
    if (size === last && size > 0) return { stable: true, waited_ms: Date.now() - started };
    last = size;
    if (Date.now() - started >= timeout) return { stable: false, waited_ms: Date.now() - started };
    await sleep(interval);
  }
}

async function waitForSelector(tabId, selector, timeout) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const found = await runFunc(tabId, (sel) => !!document.querySelector(sel), [selector]);
    if (found) return true;
    if (Date.now() > deadline) {
      throw bridgeError("ELEMENT_NOT_FOUND", `timed out waiting for ${selector}`, { selector });
    }
    await sleep(200);
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ───────────────────────── 截图 ─────────────────────────

/**
 * 真正的元素截图。
 *
 * 老实现调 captureVisibleTab，截的是「用户当前看的那个标签页」而不是被驱动
 * 的后台标签页，selector / padding 直接丢弃 —— 既拿不到目标，又会把用户的
 * 无关页面泄进 agent 的上下文。现在走 CDP Page.captureScreenshot + clip。
 */
async function cmdScreenshot({ selector = null, padding = 0, full_page = false, ...rest }) {
  const tab = await resolveTab(rest);

  let clip = null;
  if (selector) {
    const rect = await runInPage(tab.id, "__element_rect", { selector, padding });
    clip = { x: rect.x, y: rect.y, width: rect.width, height: rect.height, scale: 1 };
  }

  const data = await withDebugger(tab.id, async (_t, send) => {
    await send("Page.enable", {});
    if (!clip && full_page) {
      // captureBeyondViewport alone no longer expands to the document, so a
      // full-page shot needs an explicit clip built from the content size —
      // otherwise you silently get just the viewport.
      const metrics = await send("Page.getLayoutMetrics", {});
      const size = metrics.cssContentSize || metrics.contentSize;
      if (size) clip = { x: 0, y: 0, width: size.width, height: size.height, scale: 1 };
    }
    const shot = await send("Page.captureScreenshot", {
      format: "png",
      // 只有裁剪或整页截图才需要越过视口渲染；否则就截当前视口。
      captureBeyondViewport: !!(clip || full_page),
      ...(clip ? { clip } : {}),
    });
    return shot.data;
  });

  if (!data) throw bridgeError("SCREENSHOT_FAILED", "Chrome returned an empty screenshot");
  return { data, clip };
}

// ───────────────────────── Cookies ─────────────────────────

async function cmdGetCookies({ domain = "" } = {}) {
  return domain ? await chrome.cookies.getAll({ domain }) : await chrome.cookies.getAll({});
}

/**
 * Bring a tab to the front (and focus its window), returning whatever was
 * active before so the caller can put it back.
 *
 * Needed because Chrome will not deliver CDP `Input.dispatchMouseEvent`
 * press/release to a background tab — see cmdCdpMouse.
 */
async function cmdActivateTab({ restore_to = null, ...rest }) {
  if (restore_to) {
    const prev = await chrome.tabs.get(Number(restore_to)).catch(() => null);
    if (prev) {
      await chrome.tabs.update(prev.id, { active: true });
      await chrome.windows.update(prev.windowId, { focused: true }).catch(() => {});
    }
    return { restored: Boolean(prev) };
  }

  const tab = await resolveTab(rest);
  const [previous] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
  return {
    activated: String(tab.id),
    previous: previous && previous.id !== tab.id ? String(previous.id) : null,
  };
}

async function cmdListTabs({ url_contains = "" } = {}) {
  const tabs = await chrome.tabs.query({});
  return tabs
    .filter((t) => !url_contains || (t.url || "").includes(url_contains))
    .map((t) => ({
      tab_id: String(t.id),
      // `active` is per *window*, so a tab is only backgrounded by activating
      // a sibling in the same window — hence exposing the window id.
      window_id: String(t.windowId),
      url: t.url,
      title: t.title,
      active: t.active,
      bridge_owned: _bridgeTabs.has(t.id),
    }));
}

// ──────────────────── Keep-alive browse tab 管理 ────────────────────

async function cmdBrowseOpen({ url, timeout = 60000, name = null, settle_ms = 2000, wait_selector = null }) {
  // 命名 session：同名已有活标签页就复用，不再每跑一次泄漏一个标签页。
  if (name && _sessions.has(name)) {
    const existing = await chrome.tabs.get(_sessions.get(name)).catch(() => null);
    if (existing) {
      // 已经停在目标地址就什么都不做 —— 复用的意义就在于此。
      const fresh = sameUrl(existing.url, url)
        ? existing
        : await navigateTab(existing, url, { timeout, settle_ms });
      if (wait_selector) await waitForSelector(fresh.id, wait_selector, timeout);
      return { tab_id: String(fresh.id), url: fresh.url, status: "reused", name };
    }
    _sessions.delete(name);
  }

  const tab = await createBridgeTab(url);
  // 标签页是先建成 about:blank 再导航的（为了让 CSP 规则先生效），所以要
  // 明确等 URL 真的离开 about:blank，不能一看到 complete 就返回。
  await waitForTabComplete(tab.id, {
    timeout,
    changedFrom: url && url !== "about:blank" ? "about:blank" : null,
  });
  if (settle_ms > 0) await sleep(settle_ms);
  if (wait_selector) await waitForSelector(tab.id, wait_selector, timeout);

  if (name) {
    _sessions.set(name, tab.id);
    await persistState();
  }
  const updated = await chrome.tabs.get(tab.id);
  return { tab_id: String(tab.id), url: updated.url, status: "ready", name };
}

async function cmdBrowseDo({ tab_id, expression, wait_selector = null, wait_timeout = 15000 }) {
  const tab = await resolveTab({ tab_id });
  if (wait_selector) await waitForSelector(tab.id, wait_selector, wait_timeout);
  return await runInPage(tab.id, "evaluate", { expression });
}

async function cmdBrowseClose({ tab_id }) {
  const tab = await resolveTab({ tab_id }).catch(() => null);
  if (!tab) return { closed: false };

  // Don't block on the removal. chrome.tabs.remove() only settles once the tab
  // — and, when it was the last one, its entire window — has finished tearing
  // down, which measurably takes longer than the caller's deadline. The tab
  // does close; awaiting it just turned a success into a spurious TIMEOUT.
  // onRemoved does the real bookkeeping either way.
  const removal = chrome.tabs.remove(tab.id).then(
    () => ({ ok: true }),
    (e) => ({ ok: false, error: String((e && e.message) || e) }),
  );
  // Report a refusal instead of swallowing it: Chrome rejects tabs.remove
  // outright in some states ("Tabs cannot be edited right now"), and claiming
  // {closed: true} there leaves the caller with a tab it thinks is gone.
  removal.then((r) => {
    if (!r.ok) console.warn("[Chrome Bridge] tabs.remove refused:", r.error);
  });
  const outcome = await Promise.race([
    removal,
    new Promise((resolve) => setTimeout(() => resolve(null), 2000)),
  ]);
  if (outcome && !outcome.ok) {
    throw bridgeError("TAB_CLOSE_FAILED", `Chrome refused to close the tab: ${outcome.error}`);
  }
  const confirmed = outcome ? true : null;

  _bridgeTabs.delete(tab.id);
  if (_managedTabId === tab.id) _managedTabId = null;
  for (const [name, id] of _sessions.entries()) if (id === tab.id) _sessions.delete(name);
  await syncCspRule();
  await persistState();
  return { closed: true, confirmed };
}

async function cmdBrowseAndEval({ url, expression, wait_selector = null, timeout = 30000 }) {
  const opened = await cmdBrowseOpen({ url, timeout, wait_selector });
  try {
    return await cmdBrowseDo({ tab_id: opened.tab_id, expression });
  } finally {
    await cmdBrowseClose({ tab_id: opened.tab_id });
  }
}

// ───────────────────────── 文件上传（CDP） ─────────────────────────

async function cmdSetFileInput({ selector, files, ...rest }) {
  const tab = await resolveTab(rest);
  return await withDebugger(tab.id, async (_t, send) => {
    const { root } = await send("DOM.getDocument", { depth: 0 });
    const { nodeId } = await send("DOM.querySelector", { nodeId: root.nodeId, selector });
    if (!nodeId) throw bridgeError("ELEMENT_NOT_FOUND", `file input not found: ${selector}`, { selector });
    await send("DOM.setFileInputFiles", { nodeId, files });
    return null;
  });
}

// ─────────────── CDP 原生鼠标（受信任输入：click / drag） ───────────────

async function cmdCdpMouse({
  action = "click",
  x,
  y,
  x2,
  y2,
  button = "left",
  steps = 12,
  activate = true,
  ...rest
}) {
  const tab = await resolveTab(rest);
  const nap = (ms) => new Promise((r) => setTimeout(r, ms));

  // Chrome always delivers CDP mouseMoved to a background tab, but
  // mousePressed/mouseReleased only arrive if that tab's renderer still has a
  // live surface — a click on a tab that has been backgrounded for a while
  // silently goes nowhere. Raising the tab makes it deterministic; we put back
  // whatever the user was looking at. `activate: false` opts out.
  let restoreTo = null;
  if (activate && !tab.active) {
    const [previous] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
    if (previous && previous.id !== tab.id) restoreTo = previous;
    await chrome.tabs.update(tab.id, { active: true }).catch(() => {});
    await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
    await nap(120); // let the compositor present a frame before we click it
  }

  try {
    return await runCdpMouse(tab, { action, x, y, x2, y2, button, steps }, nap);
  } finally {
    if (restoreTo) {
      await chrome.tabs.update(restoreTo.id, { active: true }).catch(() => {});
      await chrome.windows.update(restoreTo.windowId, { focused: true }).catch(() => {});
    }
  }
}

async function runCdpMouse(tab, { action, x, y, x2, y2, button, steps }, nap) {
  return await withDebugger(tab.id, async (_t, send) => {
    if (action === "move") {
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    } else if (action === "click" || action === "rightclick") {
      const b = action === "rightclick" ? "right" : button;
      const mask = b === "right" ? 2 : 1;
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
      await nap(30);
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: b, buttons: mask, clickCount: 1 });
      await nap(40);
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: b, buttons: 0, clickCount: 1 });
    } else if (action === "drag") {
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
      await nap(120);
      // 先在起点附近抖一下，越过拖拽阈值、触发 dragstart
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x + 4, y: y + 4, button: "left", buttons: 1 });
      await nap(60);
      for (let i = 1; i <= steps; i++) {
        const ix = x + ((x2 - x) * i) / steps;
        const iy = y + ((y2 - y) * i) / steps;
        await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: ix, y: iy, button: "left", buttons: 1 });
        await nap(45);
      }
      // 到目标后停留 + 微移，让 dragover 在落点稳定注册
      for (let s = 0; s < 4; s++) {
        await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x2 + (s % 2 ? 1 : -1), y: y2, button: "left", buttons: 1 });
        await nap(90);
      }
      await nap(160);
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: x2, y: y2, button: "left", buttons: 0, clickCount: 1 });
    } else {
      throw bridgeError("BAD_REQUEST", `unknown cdp_mouse action: ${action}`);
    }
    return { ok: true, action };
  });
}

// ═══════════════════════ 页面侧执行器（MAIN world） ═══════════════════════
/**
 * 所有页面内命令的唯一实现。被序列化后注入页面，**不能引用外部变量**。
 *
 * 返回 {__bridge_error:{code,message}} 表示失败；background 侧会转成带
 * 错误码的异常发回 Python。
 */
function pageExecutor(method, params) {
  const P = params || {};

  // ── 通用工具 ──
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const fail = (code, message, detail) => ({ __bridge_error: { code, message, detail } });

  function q(selector, root) {
    return (root || document).querySelector(selector);
  }

  function need(selector) {
    const el = q(selector);
    if (!el) return fail("ELEMENT_NOT_FOUND", `element not found: ${selector}`, { selector });
    return el;
  }

  const isErr = (v) => v && typeof v === "object" && "__bridge_error" in v;

  /**
   * Slice without ever cutting a surrogate pair in half.
   *
   * JS strings are UTF-16, Python strings are code points: a chunk ending on a
   * lone high surrogate arrives in Python as an unpaired code point, and
   * concatenating the pieces can never put the character back together.
   */
  function safeSlice(text, start, length) {
    let end = Math.min(text.length, start + length);
    const code = text.charCodeAt(end - 1);
    if (end < text.length && code >= 0xd800 && code <= 0xdbff) end -= 1;
    if (end <= start) end = Math.min(text.length, start + 2); // never stall
    return text.slice(start, end);
  }

  /** React/Vue 会给 input 装一个 value 追踪器：直接赋 el.value 会连追踪器一起
   *  更新，框架就认为「没变化」，于是表单看着填好了、提交却是空的。必须走
   *  原型链上的原生 setter。 */
  function setNativeValue(el, value) {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
  }

  function fireInput(el, { blur = false } = {}) {
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    if (blur) el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function visible(el) {
    if (!el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  }

  function accessibleName(el) {
    const byLabel = el.getAttribute && el.getAttribute("aria-label");
    if (byLabel) return byLabel.trim();
    const labelledBy = el.getAttribute && el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => n.textContent.trim());
      if (parts.length) return parts.join(" ");
    }
    if (el.labels && el.labels.length) return el.labels[0].textContent.trim();
    for (const attr of ["placeholder", "title", "alt", "name"]) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) return v.trim();
    }
    const text = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
    return text.slice(0, 80);
  }

  const CHECKABLE = /^(checkbox|radio|switch|menuitemcheckbox|menuitemradio)$/;

  const INTERACTIVE =
    'a[href],button,input,select,textarea,summary,[contenteditable="true"],' +
    '[role="button"],[role="link"],[role="radio"],[role="checkbox"],[role="combobox"],' +
    '[role="option"],[role="tab"],[role="menuitem"],[role="switch"],[role="textbox"],' +
    "[onclick],[tabindex]:not([tabindex='-1'])";

  /** 深度遍历，顺带穿透 open shadow root。 */
  function collect(root, selector, out, budget) {
    if (out.length >= budget) return;
    let matches = [];
    try {
      matches = Array.from(root.querySelectorAll(selector));
    } catch {
      matches = [];
    }
    for (const el of matches) {
      if (out.length >= budget) return;
      out.push(el);
    }
    const all = root.querySelectorAll("*");
    for (const el of all) {
      if (el.shadowRoot) collect(el.shadowRoot, selector, out, budget);
    }
  }

  switch (method) {
    // ── 求值 ──
    case "evaluate": {
      try {
        // eslint-disable-next-line no-new-func
        return Function(`"use strict"; return (${P.expression})`)();
      } catch (e) {
        return fail("JS_ERROR", `${e.message}`, { stack: String(e.stack || "").slice(0, 2000) });
      }
    }

    case "evaluate_function": {
      try {
        // eslint-disable-next-line no-new-func
        const fn = Function(`"use strict"; return (function(){${P.body}})`)();
        return fn.apply(null, P.args || []);
      } catch (e) {
        return fail("JS_ERROR", `${e.message}`, { stack: String(e.stack || "").slice(0, 2000) });
      }
    }

    // ── 查询 ──
    case "has_element":
      return q(P.selector) !== null;

    case "get_elements_count":
      return document.querySelectorAll(P.selector).length;

    case "get_element_text": {
      const el = q(P.selector);
      return el ? el.textContent : null;
    }

    case "get_element_attribute": {
      const el = q(P.selector);
      return el ? el.getAttribute(P.attr) : null;
    }

    case "get_scroll_top":
      return window.pageYOffset || document.documentElement.scrollTop || 0;

    case "get_viewport_height":
      return window.innerHeight;

    case "get_url":
      return window.location.href;

    case "get_text": {
      const el = P.selector ? q(P.selector) : document.body;
      if (!el) return fail("ELEMENT_NOT_FOUND", `element not found: ${P.selector}`, { selector: P.selector });
      const text = el.innerText || "";
      const start = P.start || 0;
      const length = P.length || text.length;
      const slice = safeSlice(text, start, length);
      // `next` is the cursor in UTF-16 units. Python's len() counts code
      // points, so a client that computed this itself would drift by one per
      // astral character (any emoji) and duplicate or split the tail.
      return { text: slice, next: start + slice.length, total: text.length };
    }

    case "get_html": {
      const el = P.selector ? q(P.selector) : document.documentElement;
      if (!el) return fail("ELEMENT_NOT_FOUND", `element not found: ${P.selector}`, { selector: P.selector });
      return el.outerHTML;
    }

    case "__element_rect": {
      const el = need(P.selector);
      if (isErr(el)) return el;
      el.scrollIntoView({ block: "center", inline: "center" });
      const r = el.getBoundingClientRect();
      const pad = P.padding || 0;
      if (r.width <= 0 || r.height <= 0) {
        return fail("SCREENSHOT_FAILED", `element has no size, nothing to capture: ${P.selector}`, {
          selector: P.selector,
        });
      }
      // Clamping the origin at 0 must shrink the box, not shift it.
      const x = r.left + window.scrollX - pad;
      const y = r.top + window.scrollY - pad;
      return {
        x: Math.max(0, x),
        y: Math.max(0, y),
        width: r.width + pad * 2 + Math.min(0, x),
        height: r.height + pad * 2 + Math.min(0, y),
      };
    }

    // ── 等待 ──
    case "wait_dom_stable": {
      const timeout = P.timeout || 10000;
      const interval = P.interval || 500;
      return new Promise((resolve) => {
        let last = -1;
        const start = Date.now();
        (function tick() {
          const size = document.body ? document.body.innerHTML.length : 0;
          if (size === last && size > 0) {
            resolve({ stable: true, waited_ms: Date.now() - start });
            return;
          }
          last = size;
          if (Date.now() - start >= timeout) {
            resolve({ stable: false, waited_ms: Date.now() - start });
            return;
          }
          setTimeout(tick, interval);
        })();
      });
    }

    case "wait_for_selector": {
      const timeout = P.timeout || 30000;
      const start = Date.now();
      return new Promise((resolve) => {
        (function tick() {
          if (document.querySelector(P.selector)) {
            resolve(true);
            return;
          }
          if (Date.now() - start >= timeout) {
            resolve(fail("ELEMENT_NOT_FOUND", `timed out waiting for ${P.selector}`, { selector: P.selector }));
            return;
          }
          setTimeout(tick, 200);
        })();
      });
    }

    // ── 快照 / ref 操作 ──
    case "snapshot": {
      const budget = P.limit || 200;
      const root = P.root ? q(P.root) : document;
      if (!root) return fail("ELEMENT_NOT_FOUND", `root not found: ${P.root}`, { selector: P.root });

      const raw = [];
      collect(root, INTERACTIVE, raw, budget * 6);

      const seen = new Set();
      const nodes = [];
      const items = [];
      const filter = (P.filter || "").toLowerCase();

      for (const el of raw) {
        if (nodes.length >= budget) break;
        if (seen.has(el)) continue;
        seen.add(el);
        if (!visible(el)) continue;

        const name = accessibleName(el);
        if (filter && !name.toLowerCase().includes(filter)) continue;

        const rect = el.getBoundingClientRect();
        const tag = el.tagName.toLowerCase();
        const item = {
          ref: nodes.length,
          tag,
          role: el.getAttribute("role") || (tag === "input" ? el.type || "text" : tag),
          name,
          x: Math.round(rect.left + rect.width / 2),
          y: Math.round(rect.top + rect.height / 2),
        };
        if ("value" in el && typeof el.value === "string" && el.value) {
          // Never surface secrets: the snapshot goes straight into an LLM's
          // context and the transcript. A visible, autofilled password field
          // on an SSO page is the common case, not an exotic one.
          const type = (el.type || "").toLowerCase();
          const auto = (el.getAttribute("autocomplete") || "").toLowerCase();
          const secret =
            type === "password" ||
            type === "hidden" ||
            /current-password|new-password|one-time-code|^cc-/.test(auto);
          item.value = secret ? "***" : el.value.slice(0, 120);
        }
        // .checked is a boolean on *every* input, so gate on the role or the
        // snapshot claims a text box is "unchecked".
        if (typeof el.checked === "boolean" && CHECKABLE.test(item.role)) item.checked = el.checked;
        if (el.disabled) item.disabled = true;
        if (tag === "select") {
          item.options = Array.from(el.options)
            .slice(0, 40)
            .map((o) => o.value || o.text);
        }
        if (tag === "a" && el.href) item.href = el.href;
        nodes.push(el);
        items.push(item);
      }

      const id = (self.crypto && self.crypto.randomUUID && self.crypto.randomUUID()) || String(Date.now());
      window.__cb_refs = { id, nodes };
      return { snapshot_id: id, url: location.href, title: document.title, elements: items };
    }

    case "act_ref": {
      const store = window.__cb_refs;
      if (!store) return fail("STALE_REF", "no snapshot on this page — call snapshot() first");
      if (P.snapshot_id && P.snapshot_id !== store.id) {
        return fail("STALE_REF", "snapshot is stale (the page produced a newer one)");
      }
      const el = store.nodes[P.ref];
      if (!el) return fail("STALE_REF", `no element with ref ${P.ref}`, { ref: P.ref });
      if (!el.isConnected) return fail("STALE_REF", `ref ${P.ref} was removed from the DOM`, { ref: P.ref });

      switch (P.action || "click") {
        case "click":
          el.scrollIntoView({ block: "center" });
          if (el.focus) el.focus();
          el.click();
          return null;
        case "fill":
          if (el.isContentEditable) {
            el.focus();
            document.execCommand("selectAll", false, null);
            document.execCommand("insertText", false, P.text || "");
            return null;
          }
          el.focus();
          setNativeValue(el, P.text || "");
          fireInput(el, { blur: P.blur === true });
          return null;
        case "check":
          if (el.checked !== (P.checked !== false)) el.click();
          return null;
        case "select":
          setNativeValue(el, P.text);
          fireInput(el);
          return null;
        case "hover": {
          const r = el.getBoundingClientRect();
          const cx = r.left + r.width / 2;
          const cy = r.top + r.height / 2;
          el.dispatchEvent(new MouseEvent("mouseover", { clientX: cx, clientY: cy, bubbles: true }));
          el.dispatchEvent(new MouseEvent("mousemove", { clientX: cx, clientY: cy, bubbles: true }));
          return null;
        }
        case "focus":
          el.focus();
          return null;
        case "scroll_into_view":
          el.scrollIntoView({ block: "center" });
          return null;
        case "text":
          return el.innerText || el.textContent || "";
        default:
          return fail("BAD_REQUEST", `unknown act_ref action: ${P.action}`);
      }
    }

    // ── 带会话的 fetch（复用浏览器登录态） ──
    case "page_fetch": {
      return (async () => {
        const headers = Object.assign({}, P.headers || {});
        if (P.csrf_from) {
          const src = q(P.csrf_from);
          if (src) {
            const token = src.getAttribute("content") || src.value || src.textContent;
            if (token) headers[P.csrf_header || "x-csrf-token"] = token.trim();
          }
        }
        let res;
        try {
          res = await fetch(P.url, {
            method: P.method || "GET",
            credentials: "include",
            headers,
            ...(P.body !== undefined && P.body !== null ? { body: P.body } : {}),
          });
        } catch (e) {
          return fail("FETCH_FAILED", `fetch failed: ${e.message}`, { url: P.url });
        }
        const body = await res.text();
        const meta = { status: res.status, ok: res.ok, url: res.url, length: body.length };
        if (body.length > (P.max_inline || 4000000)) {
          window.__cb_buf = body;
          return Object.assign(meta, { buffered: true });
        }
        return Object.assign(meta, { body });
      })();
    }

    case "read_buffer": {
      const buf = window.__cb_buf || "";
      const start = P.start || 0;
      const length = P.length || 1000000;
      const chunk = safeSlice(buf, start, length);
      // `next` in UTF-16 units — see the note on get_text.
      return { chunk, next: start + chunk.length, total: buf.length };
    }

    // ── 交互 ──
    case "click_element": {
      const el = need(P.selector);
      if (isErr(el)) return el;
      el.scrollIntoView({ block: "center" });
      if (el.focus) el.focus();
      el.click();
      return null;
    }

    case "input_text": {
      const el = need(P.selector);
      if (isErr(el)) return el;
      el.focus();
      setNativeValue(el, P.text);
      fireInput(el, { blur: P.blur === true });
      return null;
    }

    case "input_content_editable": {
      const el = need(P.selector);
      if (isErr(el)) return el;
      return (async () => {
        el.focus();
        document.execCommand("selectAll", false, null);
        document.execCommand("delete", false, null);
        await sleep(80);
        const lines = String(P.text).split("\n");
        for (let i = 0; i < lines.length; i++) {
          if (lines[i]) document.execCommand("insertText", false, lines[i]);
          if (i < lines.length - 1) {
            // insertParagraph 才能在 contenteditable 里真正插入换行
            document.execCommand("insertParagraph", false, null);
            await sleep(30);
          }
        }
        return null;
      })();
    }

    case "select_option": {
      const el = need(P.selector);
      if (isErr(el)) return el;
      setNativeValue(el, P.value);
      fireInput(el);
      return null;
    }

    case "scroll_by":
      window.scrollBy(P.x || 0, P.y || 0);
      return null;

    case "scroll_to":
      window.scrollTo(P.x || 0, P.y || 0);
      return null;

    case "scroll_to_bottom":
      window.scrollTo(0, document.body.scrollHeight);
      return null;

    case "scroll_element_into_view": {
      const el = need(P.selector);
      if (isErr(el)) return el;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      return null;
    }

    case "scroll_nth_element_into_view": {
      const els = document.querySelectorAll(P.selector);
      const el = els[P.index];
      if (!el) return fail("ELEMENT_NOT_FOUND", `no element ${P.selector}[${P.index}]`, { selector: P.selector });
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      return null;
    }

    case "dispatch_wheel_event": {
      const target = P.selector ? q(P.selector) || document.documentElement : document.documentElement;
      target.dispatchEvent(
        new WheelEvent("wheel", { deltaY: P.deltaY || 0, deltaMode: 0, bubbles: true, cancelable: true }),
      );
      return null;
    }

    case "mouse_move":
      document.dispatchEvent(new MouseEvent("mousemove", { clientX: P.x, clientY: P.y, bubbles: true }));
      return null;

    case "mouse_click": {
      const el = document.elementFromPoint(P.x, P.y);
      if (el) {
        for (const t of ["mousedown", "mouseup", "click"]) {
          el.dispatchEvent(new MouseEvent(t, { clientX: P.x, clientY: P.y, bubbles: true }));
        }
      }
      return null;
    }

    case "press_key": {
      const active = document.activeElement || document.body;
      const inCE = active.isContentEditable;
      if (inCE && P.key === "Enter") {
        document.execCommand("insertParagraph", false, null);
        return null;
      }
      if (inCE && P.key === "ArrowDown") {
        const sel = window.getSelection();
        if (sel && active.childNodes.length) {
          sel.selectAllChildren(active);
          sel.collapseToEnd();
        }
        return null;
      }
      const keyMap = {
        Enter: { key: "Enter", code: "Enter", keyCode: 13 },
        ArrowDown: { key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
        ArrowUp: { key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
        Escape: { key: "Escape", code: "Escape", keyCode: 27 },
        Tab: { key: "Tab", code: "Tab", keyCode: 9 },
        Backspace: { key: "Backspace", code: "Backspace", keyCode: 8 },
      };
      const info = keyMap[P.key] || { key: P.key, code: P.key, keyCode: 0 };
      active.dispatchEvent(new KeyboardEvent("keydown", Object.assign({ bubbles: true }, info)));
      active.dispatchEvent(new KeyboardEvent("keyup", Object.assign({ bubbles: true }, info)));
      return null;
    }

    case "type_text": {
      return (async () => {
        const active = document.activeElement || document.body;
        const inCE = active.isContentEditable;
        for (const char of P.text) {
          if (inCE) {
            document.execCommand("insertText", false, char);
          } else {
            active.dispatchEvent(new KeyboardEvent("keydown", { key: char, bubbles: true }));
            active.dispatchEvent(new KeyboardEvent("keypress", { key: char, bubbles: true }));
            active.dispatchEvent(new KeyboardEvent("keyup", { key: char, bubbles: true }));
          }
          await sleep(P.delayMs || 50);
        }
        return null;
      })();
    }

    case "remove_element": {
      const el = q(P.selector);
      if (el) el.remove();
      return null;
    }

    case "hover_element": {
      const el = need(P.selector);
      if (isErr(el)) return el;
      const rect = el.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      el.dispatchEvent(new MouseEvent("mouseover", { clientX: x, clientY: y, bubbles: true }));
      el.dispatchEvent(new MouseEvent("mousemove", { clientX: x, clientY: y, bubbles: true }));
      return null;
    }

    case "select_all_text": {
      const el = need(P.selector);
      if (isErr(el)) return el;
      el.focus();
      if (el.select) el.select();
      else document.execCommand("selectAll");
      return null;
    }

    default:
      return fail("BAD_REQUEST", `unknown page command: ${method}`);
  }
}

// ───────────────────────── Bootstrap ─────────────────────────

connect();
