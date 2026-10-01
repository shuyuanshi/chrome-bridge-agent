import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../extension/background.js", import.meta.url), "utf8");

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

async function harness(options = {}) {
  const tabs = new Map();
  const cookieCalls = [];
  const cspUpdates = [];
  const storageWrites = [];
  const failureStorageWrites = [];
  const websocketFrames = [];
  const clearedTimeouts = [];
  const warnings = [];
  const windowCreates = [];
  const windowUpdates = [];
  const windows = new Map([[1, { id: 1, type: "normal", state: "normal", focused: true }]]);
  let storedState = options.initialState ? plain(options.initialState) : null;
  let storedFailures = options.initialFailureQueue ? plain(options.initialFailureQueue) : [];
  let latestSocket = null;
  let nextTabId = 1;
  let nextWindowId = 2;
  let createCount = 0;
  let removeCount = 0;
  let onRemoved = async () => {};
  let onCreated = () => {};
  let onReplaced = () => {};
  let onWindowRemoved = () => {};

  function tabOrThrow(id) {
    const tab = tabs.get(Number(id));
    if (!tab) throw new Error(`No tab with id: ${id}.`);
    return tab;
  }

  async function emitRemoved(id) {
    tabs.delete(Number(id));
    await onRemoved(Number(id));
  }

  const chrome = {
    alarms: {
      create() {},
      onAlarm: { addListener() {} },
    },
    cookies: {
      async getAll(params) {
        cookieCalls.push(params);
        return [];
      },
    },
    debugger: {
      async attach() {},
      async detach() {},
      async sendCommand(target, method, params) {
        if (options.debuggerSendCommand) {
          return await options.debuggerSendCommand({ target, method, params });
        }
      },
    },
    declarativeNetRequest: {
      async updateDynamicRules() {},
      async updateSessionRules(update) {
        cspUpdates.push(plain(update));
      },
    },
    runtime: {
      getManifest: () => ({ version: options.manifestVersion || "test" }),
      reload() {},
    },
    scripting: {
      async executeScript() {
        if (options.executeScript) return await options.executeScript({ tabs });
        return [{ result: false }];
      },
    },
    storage: {
      session: {
        async get() {
          return storedState ? { state: plain(storedState) } : {};
        },
        async set(value) {
          storedState = plain(value.state);
          storageWrites.push(plain(value.state));
        },
      },
      local: {
        async get(key) {
          if (options.failureStorageGetError) throw new Error("failure storage get failed");
          return { [key]: plain(storedFailures) };
        },
        async set(value) {
          if (options.failureStorageSetError) throw new Error("failure storage set failed");
          storedFailures = plain(value.failureQueueV1 || []);
          failureStorageWrites.push(plain(storedFailures));
        },
      },
    },
    tabs: {
      async create(params = {}) {
        createCount += 1;
        const tab = {
          id: nextTabId++,
          url: params.url || "about:blank",
          status: "complete",
          windowId: params.windowId ?? 1,
          active: params.active ?? false,
          ...(params.openerTabId == null ? {} : { openerTabId: params.openerTabId }),
        };
        tabs.set(tab.id, tab);
        onCreated({ ...tab });
        return { ...tab };
      },
      async get(id) {
        if (options.get) return await options.get({ id, tabs, tabOrThrow });
        return { ...tabOrThrow(id) };
      },
      onRemoved: {
        addListener(listener) {
          onRemoved = listener;
        },
      },
      onCreated: {
        addListener(listener) {
          onCreated = listener;
        },
      },
      onReplaced: {
        addListener(listener) {
          onReplaced = listener;
        },
      },
      async query(query = {}) {
        return [...tabs.values()]
          .filter((tab) => query.windowId == null || tab.windowId === query.windowId)
          .filter((tab) => query.active == null || tab.active === query.active)
          .map((tab) => ({ ...tab }));
      },
      async remove(id) {
        removeCount += 1;
        if (options.remove) {
          return await options.remove({ id, tabs, onRemoved, emitRemoved });
        }
        tabOrThrow(id);
        await emitRemoved(id);
      },
      async move(id, { windowId }) {
        const tab = tabOrThrow(id);
        const oldWindowId = tab.windowId;
        tab.windowId = Number(windowId);
        tab.active = false;
        if (![...tabs.values()].some((candidate) => candidate.windowId === oldWindowId)) {
          windows.delete(oldWindowId);
          await onWindowRemoved(oldWindowId);
        }
        return { ...tab };
      },
      async update(id, update) {
        if (options.update) return await options.update({ id, update, tabs, tabOrThrow });
        const tab = tabOrThrow(id);
        if (update.active) {
          for (const sibling of tabs.values()) {
            if (sibling.windowId === tab.windowId) sibling.active = sibling.id === tab.id;
          }
        }
        Object.assign(tab, update, { status: "complete" });
        return { ...tab };
      },
    },
    windows: {
      async create(params = {}) {
        if (options.windowCreate) {
          return await options.windowCreate({ params, tabs, windows, tabOrThrow });
        }
        if (options.beforeWindowCreate) await options.beforeWindowCreate();
        windowCreates.push(plain(params));
        const window = {
          id: nextWindowId++,
          type: params.type || "normal",
          state: params.state || "normal",
          focused: params.focused ?? true,
        };
        if (window.focused) {
          for (const existing of windows.values()) existing.focused = false;
        }
        windows.set(window.id, window);
        createCount += 1;
        const tab = {
          id: nextTabId++,
          url: params.url || "about:blank",
          status: "complete",
          windowId: window.id,
          active: true,
          ...(params.openerTabId == null ? {} : { openerTabId: params.openerTabId }),
        };
        tabs.set(tab.id, tab);
        onCreated({ ...tab });
        return { ...window, tabs: [{ ...tab }] };
      },
      async get(id) {
        const window = windows.get(Number(id));
        if (!window) throw new Error(`No window with id: ${id}.`);
        return { ...window };
      },
      async getAll() {
        return [...windows.values()].map((window) => ({ ...window }));
      },
      async update(id, update) {
        const window = windows.get(Number(id));
        if (!window) throw new Error(`No window with id: ${id}.`);
        windowUpdates.push({ id: Number(id), update: plain(update) });
        if (update.focused) {
          for (const existing of windows.values()) existing.focused = false;
        }
        Object.assign(window, update);
        return { ...window };
      },
      onRemoved: {
        addListener(listener) {
          onWindowRemoved = listener;
        },
      },
    },
  };

  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;

    constructor() {
      this.readyState = FakeWebSocket.CONNECTING;
      latestSocket = this;
    }

    send(raw) {
      if (options.websocketSendError) throw new Error("websocket send failed");
      websocketFrames.push(JSON.parse(raw));
    }

    async open() {
      this.readyState = FakeWebSocket.OPEN;
      await this.onopen?.({});
    }

    async message(value) {
      await this.onmessage?.({ data: typeof value === "string" ? value : JSON.stringify(value) });
    }

    async error() {
      await this.onerror?.({ type: "error" });
    }

    async close(code = 1000, wasClean = true) {
      this.readyState = 3;
      await this.onclose?.({ code, wasClean });
    }
  }

  const context = vm.createContext({
    chrome,
    clearTimeout: options.clearTimeout || ((id) => {
      clearedTimeouts.push(id);
      clearTimeout(id);
    }),
    console: {
      log() {},
      warn(...args) {
        warnings.push(args.map(String).join(" "));
      },
      error() {},
    },
    setTimeout: options.setTimeout || setTimeout,
    WebSocket: FakeWebSocket,
  });
  vm.runInContext(source, context, { filename: "background.js" });
  await vm.runInContext("_hydrated", context);
  await vm.runInContext("_failureQueueHydrated", context);

  return {
    context,
    tabs,
    windows,
    cookieCalls,
    cspUpdates,
    storageWrites,
    failureStorageWrites,
    websocketFrames,
    clearedTimeouts,
    warnings,
    windowCreates,
    windowUpdates,
    emitRemoved,
    storedState: () => (storedState ? plain(storedState) : null),
    storedFailureQueue: () => plain(storedFailures),
    socket: () => latestSocket,
    cspTabIds: () => {
      const last = cspUpdates.at(-1);
      return last?.addRules?.[0]?.condition?.tabIds || [];
    },
    counts: () => ({ create: createCount, remove: removeCount }),
    emitWindowRemoved: async (id) => {
      windows.delete(Number(id));
      await onWindowRemoved(Number(id));
    },
    emitReplaced: async (addedTab, removedTabId) => {
      tabs.delete(Number(removedTabId));
      tabs.set(Number(addedTab.id), { ...addedTab });
      onReplaced(Number(addedTab.id), Number(removedTabId));
      await nextTurn();
    },
  };
}

async function evaluate(state, expression) {
  return await vm.runInContext(expression, state.context);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function nextTurn() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function testTransientTabTeardownRetriesUntilDeadline() {
  let failures = 4;
  const state = await harness({
    async update({ id, update, tabs, tabOrThrow }) {
      if (failures > 0) {
        failures -= 1;
        tabs.delete(Number(id));
        throw new Error(`No tab with id: ${id}.`);
      }
      const tab = tabOrThrow(id);
      Object.assign(tab, update, { status: "complete" });
      return { ...tab };
    },
    setTimeout(callback) {
      return setTimeout(callback, 0);
    },
  });

  const result = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://example.com", timeout:2000, settle_ms:0})',
  );
  assert.equal(result.tab_id, "5");
  assert.deepEqual(state.counts(), { create: 5, remove: 0 });
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [5]);
  assert.deepEqual(state.cspTabIds(), [5]);
  assert.deepEqual(state.storedState().bridgeTabs, [5]);
}

async function testNonTransientCreationFailureClosesTheTab() {
  const state = await harness({
    async update() {
      throw new Error("Invalid URL");
    },
  });

  await assert.rejects(
    evaluate(state, 'cmdBrowseOpen({url:"not a url", timeout:1000, settle_ms:0})'),
    /Invalid URL/,
  );
  assert.deepEqual(state.counts(), { create: 1, remove: 1 });
  assert.equal(state.tabs.size, 0);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), []);
  assert.deepEqual(state.cspTabIds(), []);
  assert.deepEqual(state.storedState().bridgeTabs, []);
}

async function testFrameLossOnALiveTabDoesNotRetry() {
  const state = await harness({
    async executeScript() {
      throw new Error("No frame with id 7");
    },
  });

  await assert.rejects(
    evaluate(
      state,
      'cmdBrowseOpen({url:"https://example.com", timeout:1000, settle_ms:0, wait_selector:"#x"})',
    ),
    /No frame with id/,
  );
  assert.deepEqual(state.counts(), { create: 1, remove: 1 });
  assert.deepEqual(state.cspTabIds(), []);
  assert.deepEqual(state.storedState().bridgeTabs, []);
}

async function testPendingRemovalStaysTrackedUntilOnRemoved() {
  const removal = deferred();
  const state = await harness({
    async remove() {
      return await removal.promise;
    },
    setTimeout(callback, delay) {
      return setTimeout(callback, delay === 2000 ? 0 : delay);
    },
  });
  const tab = await evaluate(state, 'createBridgeTab("about:blank")');
  const discarded = await evaluate(state, `discardBridgeTab(${tab.id})`);

  assert.equal(discarded.confirmed, null);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [tab.id]);
  assert.deepEqual(state.cspTabIds(), [tab.id]);
  assert.deepEqual(state.storedState().bridgeTabs, [tab.id]);

  await state.emitRemoved(tab.id);
  removal.resolve();
  await nextTurn();
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), []);
  assert.deepEqual(state.cspTabIds(), []);
  assert.deepEqual(state.storedState().bridgeTabs, []);
}

async function testRefusedRemovalStaysTracked() {
  const state = await harness({
    async remove() {
      throw new Error("Tabs cannot be edited right now");
    },
  });
  const tab = await evaluate(state, 'createBridgeTab("about:blank")');
  const discarded = await evaluate(state, `discardBridgeTab(${tab.id})`);

  assert.equal(discarded.ok, false);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [tab.id]);
  assert.equal(state.tabs.has(tab.id), true);
  assert.deepEqual(state.cspTabIds(), [tab.id]);
  assert.deepEqual(state.storedState().bridgeTabs, [tab.id]);
  assert.ok(state.warnings.some((warning) => warning.includes("tabs.remove refused")));
}

async function testDelayedRemovalRefusalIsLoggedAndStaysTracked() {
  const removal = deferred();
  const state = await harness({
    async remove() {
      return await removal.promise;
    },
    setTimeout(callback, delay) {
      return setTimeout(callback, delay === 2000 ? 0 : delay);
    },
  });
  const tab = await evaluate(state, 'createBridgeTab("about:blank")');
  const discarded = await evaluate(state, `discardBridgeTab(${tab.id})`);
  assert.equal(discarded.confirmed, null);

  removal.reject(new Error("Tabs cannot be edited right now"));
  await nextTurn();
  assert.ok(state.warnings.some((warning) => warning.includes("tabs.remove refused")));
  assert.equal(state.tabs.has(tab.id), true);
  assert.deepEqual(state.cspTabIds(), [tab.id]);
  assert.deepEqual(state.storedState().bridgeTabs, [tab.id]);
}

async function testRemovalRejectionAfterTabVanishesIsSuccess() {
  const state = await harness({
    async remove({ id, tabs }) {
      tabs.delete(Number(id));
      throw new Error(`No tab with id: ${id}.`);
    },
  });
  const tab = await evaluate(state, 'createBridgeTab("about:blank")');
  const discarded = await evaluate(state, `discardBridgeTab(${tab.id})`);

  assert.deepEqual(plain(discarded), { ok: true, confirmed: true });
  assert.deepEqual(state.cspTabIds(), []);
  assert.deepEqual(state.storedState().bridgeTabs, []);
  assert.deepEqual(state.warnings, []);
}

async function testFailedOpenSurfacesCleanupFailureWithTabId() {
  const state = await harness({
    async executeScript() {
      throw new Error("No frame with id 7");
    },
    async remove() {
      throw new Error("Tabs cannot be edited right now");
    },
  });

  await assert.rejects(
    evaluate(
      state,
      'cmdBrowseOpen({url:"https://example.com", timeout:1000, settle_ms:0, wait_selector:"#x"})',
    ),
    (error) => {
      assert.equal(error.bridge.code, "TAB_CLEANUP_FAILED");
      assert.equal(error.bridge.detail.tab_id, "1");
      assert.equal(error.bridge.detail.original_error.code, "TAB_GONE");
      assert.equal(error.bridge.detail.cleanup_status, "REFUSED");
      assert.match(error.bridge.detail.cleanup_error, /cannot be edited/);
      return true;
    },
  );
  assert.equal(state.tabs.has(1), true);
  assert.deepEqual(state.cspTabIds(), [1]);
  assert.deepEqual(state.storedState().bridgeTabs, [1]);
}

async function testFailedOpenSurfacesUnconfirmedCleanupWithTabId() {
  const removal = deferred();
  const state = await harness({
    async executeScript() {
      throw new Error("No frame with id 7");
    },
    async remove() {
      return await removal.promise;
    },
    setTimeout(callback, delay) {
      return setTimeout(callback, delay === 2000 ? 0 : delay);
    },
  });

  await assert.rejects(
    evaluate(
      state,
      'cmdBrowseOpen({url:"https://example.com", timeout:1000, settle_ms:0, wait_selector:"#x"})',
    ),
    (error) => {
      assert.equal(error.bridge.code, "TAB_CLEANUP_FAILED");
      assert.equal(error.bridge.detail.tab_id, "1");
      assert.equal(error.bridge.detail.cleanup_status, "UNCONFIRMED");
      assert.equal(error.bridge.detail.cleanup_error, null);
      return true;
    },
  );
  assert.equal(state.tabs.has(1), true);
  assert.deepEqual(state.cspTabIds(), [1]);
  assert.deepEqual(state.storedState().bridgeTabs, [1]);

  await state.emitRemoved(1);
  removal.resolve();
  await nextTurn();
  assert.deepEqual(state.cspTabIds(), []);
  assert.deepEqual(state.storedState().bridgeTabs, []);
}

async function testImmediateCloseRefusalRestoresNamedRoute() {
  const state = await harness({
    async remove() {
      throw new Error("Tabs cannot be edited right now");
    },
  });
  const opened = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:1000, settle_ms:0})',
  );

  await assert.rejects(
    evaluate(state, `cmdBrowseClose({tab_id:"${opened.tab_id}"})`),
    /Chrome refused to close the tab/,
  );
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["legacy\u001fdash", 1]]);
  assert.deepEqual(state.cspTabIds(), [1]);
  assert.deepEqual(state.storedState(), {
    schemaVersion: 2,
    managedTabs: [],
    sessions: [["legacy", "dash", 1]],
    bridgeTabs: [1],
    tabScopes: [[1, "legacy"]],
    automationWindowId: 2,
  });
}

async function testDelayedCloseRefusalRestoresNamedRoute() {
  const removal = deferred();
  const state = await harness({
    async remove() {
      return await removal.promise;
    },
    setTimeout(callback, delay) {
      return setTimeout(callback, delay === 2000 ? 0 : delay);
    },
  });
  const opened = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  const closed = await evaluate(state, `cmdBrowseClose({tab_id:"${opened.tab_id}"})`);
  assert.equal(closed.confirmed, null);
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), []);

  removal.reject(new Error("Tabs cannot be edited right now"));
  await nextTurn();
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["legacy\u001fdash", 1]]);
  assert.deepEqual(state.storedState().sessions, [["legacy", "dash", 1]]);
  assert.ok(state.warnings.some((warning) => warning.includes("tabs.remove refused")));
}

async function testRefusalDoesNotOverwriteConcurrentNamedReplacement() {
  const removal = deferred();
  const removalStarted = deferred();
  const replacementStarted = deferred();
  const finishReplacement = deferred();
  let updates = 0;
  const state = await harness({
    async update({ id, update, tabOrThrow }) {
      updates += 1;
      if (updates === 2) {
        replacementStarted.resolve();
        await finishReplacement.promise;
      }
      const tab = tabOrThrow(id);
      Object.assign(tab, update, { status: "complete" });
      return { ...tab };
    },
    async remove() {
      removalStarted.resolve();
      return await removal.promise;
    },
    setTimeout(callback, delay) {
      return setTimeout(callback, delay === 2000 ? 0 : delay);
    },
  });
  const first = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  const closing = evaluate(state, `cmdBrowseClose({tab_id:"${first.tab_id}"})`);
  await removalStarted.promise;
  const closed = await closing;
  assert.equal(closed.confirmed, null);
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), []);

  const replacing = evaluate(
    state,
    'cmdBrowseOpen({url:"https://two.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  await replacementStarted.promise;
  removal.reject(new Error("Tabs cannot be edited right now"));
  finishReplacement.resolve();
  const replacement = await replacing;
  await nextTurn();

  assert.equal(replacement.tab_id, "2");
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["legacy\u001fdash", 2]]);
  assert.deepEqual(state.cspTabIds(), [1, 2]);
  assert.deepEqual(state.storedState().sessions, [["legacy", "dash", 2]]);
}

async function testImmediateRefusalDoesNotWaitForFailingReplacement() {
  const oldRemoval = deferred();
  const oldRemovalStarted = deferred();
  const replacementStarted = deferred();
  const failReplacement = deferred();
  let updates = 0;
  const state = await harness({
    async update({ id, update, tabOrThrow }) {
      updates += 1;
      if (updates === 2) {
        replacementStarted.resolve();
        await failReplacement.promise;
        throw new Error("replacement navigation failed");
      }
      const tab = tabOrThrow(id);
      Object.assign(tab, update, { status: "complete" });
      return { ...tab };
    },
    async remove({ id, emitRemoved }) {
      if (Number(id) === 1) {
        oldRemovalStarted.resolve();
        return await oldRemoval.promise;
      }
      await emitRemoved(id);
    },
  });
  const first = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  const closing = evaluate(state, `cmdBrowseClose({tab_id:"${first.tab_id}"})`);
  await oldRemovalStarted.promise;
  await nextTurn();
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), []);

  const replacement = evaluate(
    state,
    'cmdBrowseOpen({url:"https://two.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  await replacementStarted.promise;
  oldRemoval.reject(new Error("Tabs cannot be edited right now"));
  const closeOutcome = await Promise.race([
    closing.then(
      () => null,
      (error) => error,
    ),
    new Promise((resolve) => setTimeout(() => resolve("timed out"), 100)),
  ]);
  assert.notEqual(closeOutcome, "timed out");
  assert.equal(closeOutcome.bridge.code, "TAB_CLOSE_FAILED");

  failReplacement.resolve();
  await assert.rejects(replacement, /replacement navigation failed/);
  await nextTurn();

  assert.equal(state.tabs.has(1), true);
  assert.equal(state.tabs.has(2), false);
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["legacy\u001fdash", 1]]);
  assert.deepEqual(plain(await evaluate(state, "[..._sessionOpenLocks]")), []);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [1]);
  assert.deepEqual(state.cspTabIds(), [1]);
  assert.deepEqual(state.storedState(), {
    schemaVersion: 2,
    managedTabs: [],
    sessions: [["legacy", "dash", 1]],
    bridgeTabs: [1],
    tabScopes: [[1, "legacy"]],
    automationWindowId: 2,
  });
}

async function testNavigateUsesOneDeadlineAcrossStartupAndLoad() {
  const state = await harness();
  const tab = await evaluate(state, 'createBridgeTab("about:blank")');

  await assert.rejects(
    evaluate(
      state,
      `navigateTab(${JSON.stringify(plain(tab))}, "https://example.com", {timeout:20})`,
    ),
    (error) => {
      assert.equal(error.bridge.code, "NAV_TIMEOUT");
      assert.equal(error.bridge.detail.phase, "waiting for navigation to start");
      return true;
    },
  );
}

async function testNewBackgroundTabUsesDedicatedMinimizedWindow() {
  const state = await harness();
  const first = await evaluate(state, "newBackgroundTab()");
  const second = await evaluate(state, "newBackgroundTab()");

  assert.equal(first.windowId, 2);
  assert.equal(second.windowId, 2);
  assert.deepEqual(state.windowCreates, [
    { url: "about:blank", type: "normal", focused: false, state: "minimized" },
  ]);
  assert.equal(state.windows.get(1).focused, true);
  assert.equal(state.windows.get(2).focused, false);
  assert.equal(state.windows.get(2).state, "minimized");
}

async function testNamedSessionRecoversOnlyAfterItsTabVanishes() {
  let updates = 0;
  const state = await harness({
    async update({ id, update, tabs, tabOrThrow }) {
      updates += 1;
      if (updates === 2) {
        tabs.delete(Number(id));
        throw new Error(`No tab with id: ${id}.`);
      }
      const tab = tabOrThrow(id);
      Object.assign(tab, update, { status: "complete" });
      return { ...tab };
    },
  });

  const first = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:2000, settle_ms:0})',
  );
  const recovered = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://two.example", name:"dash", timeout:2000, settle_ms:0})',
  );

  assert.equal(first.tab_id, "1");
  assert.equal(recovered.tab_id, "2");
  assert.equal(recovered.status, "ready");
  assert.deepEqual(state.counts(), { create: 2, remove: 0 });
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["legacy\u001fdash", 2]]);
}

async function testSameUrlNamedReuseRecoversIfTabVanishesBeforeReturn() {
  let gets = 0;
  const state = await harness({
    async get({ id, tabs, tabOrThrow }) {
      gets += 1;
      if (gets === 4) {
        tabs.delete(Number(id));
        throw new Error(`No tab with id: ${id}.`);
      }
      return { ...tabOrThrow(id) };
    },
  });
  const first = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:2000, settle_ms:0})',
  );
  const recovered = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:2000, settle_ms:0})',
  );

  assert.equal(first.tab_id, "1");
  assert.equal(recovered.tab_id, "2");
  assert.equal(recovered.status, "ready");
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["legacy\u001fdash", 2]]);
}

async function testConcurrentSameNameOpenCreatesOneTab() {
  const firstUpdate = deferred();
  const allowUpdate = deferred();
  let updates = 0;
  const state = await harness({
    async update({ id, update, tabOrThrow }) {
      updates += 1;
      if (updates === 1) {
        firstUpdate.resolve();
        await allowUpdate.promise;
      }
      const tab = tabOrThrow(id);
      Object.assign(tab, update, { status: "complete" });
      return { ...tab };
    },
  });

  const first = evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:2000, settle_ms:0})',
  );
  await firstUpdate.promise;
  const second = evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:2000, settle_ms:0})',
  );
  allowUpdate.resolve();
  const [opened, reused] = await Promise.all([first, second]);

  assert.equal(opened.tab_id, "1");
  assert.equal(reused.tab_id, "1");
  assert.equal(reused.status, "reused");
  assert.deepEqual(state.counts(), { create: 1, remove: 0 });
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [1]);
  assert.deepEqual(state.cspTabIds(), [1]);
  assert.deepEqual(state.storedState().sessions, [["legacy", "dash", 1]]);
  assert.deepEqual(plain(await evaluate(state, "[..._sessionOpenLocks]")), []);
}

async function testTimedOutNamedWaiterPreservesFifoOrder() {
  const firstUpdate = deferred();
  const allowFirst = deferred();
  let updates = 0;
  const state = await harness({
    async update({ id, update, tabOrThrow }) {
      updates += 1;
      if (updates === 1) {
        firstUpdate.resolve();
        await allowFirst.promise;
      }
      const tab = tabOrThrow(id);
      Object.assign(tab, update, { status: "complete" });
      return { ...tab };
    },
  });

  const first = evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  await firstUpdate.promise;
  const timedOut = evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:30, settle_ms:0})',
  );
  const third = evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  let thirdSettled = false;
  third.then(
    () => {
      thirdSettled = true;
    },
    () => {
      thirdSettled = true;
    },
  );

  await assert.rejects(timedOut, (error) => {
    assert.equal(error.bridge.code, "NAV_TIMEOUT");
    assert.equal(error.bridge.detail.phase, "waiting for named session lock");
    return true;
  });
  await nextTurn();
  assert.equal(thirdSettled, false);
  assert.deepEqual(state.counts(), { create: 1, remove: 0 });

  allowFirst.resolve();
  const [opened, reused] = await Promise.all([first, third]);
  assert.equal(opened.tab_id, "1");
  assert.equal(reused.tab_id, "1");
  assert.equal(reused.status, "reused");
  assert.deepEqual(state.counts(), { create: 1, remove: 0 });
  assert.deepEqual(plain(await evaluate(state, "[..._sessionOpenLocks]")), []);
}

async function testNamedReuseSharesTheOpenDeadline() {
  const state = await harness();
  await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  await evaluate(
    state,
    `globalThis.selectorBudget = null;
     waitForSelector = async (_tabId, _selector, timeout) => {
       globalThis.selectorBudget = timeout;
       return true;
     };`,
  );

  const reused = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://two.example", name:"dash", timeout:500, settle_ms:30, wait_selector:"#ready"})',
  );
  const selectorBudget = await evaluate(state, "globalThis.selectorBudget");
  assert.equal(reused.status, "reused");
  assert.ok(selectorBudget > 0 && selectorBudget < 240, selectorBudget);
}

async function testPendingNamedCloseReopensWithoutReusingDoomedTab() {
  const removal = deferred();
  const removalStarted = deferred();
  const state = await harness({
    async remove() {
      removalStarted.resolve();
      return await removal.promise;
    },
    setTimeout(callback, delay) {
      return setTimeout(callback, delay === 2000 ? 50 : delay);
    },
  });
  const first = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  const closing = evaluate(state, `cmdBrowseClose({tab_id:"${first.tab_id}"})`);
  await removalStarted.promise;

  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), []);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [1]);
  assert.deepEqual(state.cspTabIds(), [1]);

  const reopened = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://two.example", name:"dash", timeout:1000, settle_ms:0})',
  );
  assert.equal(reopened.tab_id, "2");
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["legacy\u001fdash", 2]]);
  assert.deepEqual(state.cspTabIds(), [1, 2]);
  const closed = await closing;
  assert.equal(closed.confirmed, null);
  assert.deepEqual(state.storedState(), {
    schemaVersion: 2,
    managedTabs: [],
    sessions: [["legacy", "dash", 2]],
    bridgeTabs: [1, 2],
    tabScopes: [[1, "legacy"], [2, "legacy"]],
    automationWindowId: 2,
  });

  await state.emitRemoved(1);
  removal.resolve();
  await nextTurn();
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [2]);
  assert.deepEqual(state.cspTabIds(), [2]);
  assert.deepEqual(state.storedState(), {
    schemaVersion: 2,
    managedTabs: [],
    sessions: [["legacy", "dash", 2]],
    bridgeTabs: [2],
    tabScopes: [[2, "legacy"]],
    automationWindowId: 2,
  });
}

async function testScopesIsolateManagedTabsAndNamedSessions() {
  const state = await harness();
  const managedA = await evaluate(state, 'getOrOpenManagedTab("scope-a")');
  const managedB = await evaluate(state, 'getOrOpenManagedTab("scope-b")');
  assert.notEqual(managedA.id, managedB.id);
  assert.deepEqual(plain(await evaluate(state, "[..._managedTabs.entries()]")), [
    ["scope-a", managedA.id],
    ["scope-b", managedB.id],
  ]);

  const namedA = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://one.example", name:"dash", scope_id:"scope-a", timeout:1000, settle_ms:0})',
  );
  const namedB = await evaluate(
    state,
    'cmdBrowseOpen({url:"https://two.example", name:"dash", scope_id:"scope-b", timeout:1000, settle_ms:0})',
  );
  assert.notEqual(namedA.tab_id, namedB.tab_id);
  assert.deepEqual(plain(await evaluate(state, 'listSessions("scope-a")')), [
    { name: "dash", tab_id: namedA.tab_id },
  ]);
  assert.deepEqual(plain(await evaluate(state, 'listSessions("scope-b")')), [
    { name: "dash", tab_id: namedB.tab_id },
  ]);
}

async function testNumericSessionNamesAreRejected() {
  const state = await harness();
  await assert.rejects(
    evaluate(
      state,
      'cmdBrowseOpen({url:"about:blank", name:"123", scope_id:"scope-a", timeout:1000, settle_ms:0})',
    ),
    (error) => error.bridge.code === "BAD_REQUEST",
  );
  assert.equal(state.tabs.size, 0);
}

async function testLegacyStateHydratesIntoTheReservedScope() {
  const state = await harness({
    initialState: {
      managedTabId: 7,
      sessions: [["dash", 8]],
      bridgeTabs: [7, 8],
      automationWindowId: 2,
    },
  });
  assert.deepEqual(plain(await evaluate(state, "[..._managedTabs.entries()]")), [["legacy", 7]]);
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [
    ["legacy\u001fdash", 8],
  ]);
  assert.deepEqual(plain(await evaluate(state, "[..._tabScopes.entries()]")), [
    [7, "legacy"],
    [8, "legacy"],
  ]);
}

async function testScopedCleanupPreservesUserAndOtherScopeTabs() {
  const state = await harness();
  const userTab = await evaluate(
    state,
    'chrome.tabs.create({url:"https://user.example", active:true, windowId:1})',
  );
  const tabA = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  const tabB = await evaluate(state, 'createBridgeTab("about:blank", "scope-b")');

  const result = plain(await evaluate(state, 'cmdCloseOwnedTabs({scope_id:"scope-a"})'));
  assert.deepEqual(result.confirmed, [String(tabA.id)]);
  assert.deepEqual(result.refused, []);
  assert.equal(state.tabs.has(tabA.id), false);
  assert.equal(state.tabs.has(tabB.id), true);
  assert.equal(state.tabs.has(userTab.id), true);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [tabB.id]);
}

async function testCleanupNeverClosesAUserWindow() {
  const state = await harness();
  const owned = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  await evaluate(state, `chrome.tabs.move(${owned.id}, {windowId:1, index:-1})`);

  const protectedResult = plain(
    await evaluate(state, 'cmdCloseOwnedTabs({scope_id:"scope-a"})'),
  );
  assert.equal(protectedResult.refused.length, 1);
  assert.match(protectedResult.refused[0].error, /tab from a user or foreground window/);
  assert.equal(state.windows.has(1), true);
  assert.equal(state.tabs.has(owned.id), true);

  const userTab = await evaluate(
    state,
    'chrome.tabs.create({url:"https://user.example", active:true, windowId:1})',
  );
  const stillProtected = plain(await evaluate(state, 'cmdCloseOwnedTabs({scope_id:"scope-a"})'));
  assert.equal(stillProtected.refused.length, 1);
  assert.equal(state.tabs.has(owned.id), true);
  assert.equal(state.tabs.has(userTab.id), true);
  assert.equal(state.windows.has(1), true);
}

async function testCleanupWaitsForAnInflightCreate() {
  const windowGate = deferred();
  const state = await harness({ beforeWindowCreate: () => windowGate.promise });
  const opening = evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  await nextTurn();

  let cleanupSettled = false;
  const cleanup = evaluate(state, 'cmdCloseOwnedTabs({scope_id:"scope-a"})').finally(() => {
    cleanupSettled = true;
  });
  await nextTurn();
  assert.equal(cleanupSettled, false, "cleanup returned while a scope create was in flight");

  windowGate.resolve();
  await assert.rejects(opening, (error) => error.bridge.code === "SCOPE_CLOSING");
  const result = plain(await cleanup);
  assert.deepEqual(result.remaining, []);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), []);
}

async function testCleanupWaitsForTheWholeBrowseOpenLifecycle() {
  const selectorGate = deferred();
  const state = await harness();
  state.context.selectorGate = selectorGate.promise;
  await evaluate(
    state,
    "waitForSelector = async () => { await globalThis.selectorGate; return true; }",
  );
  const opening = evaluate(
    state,
    'cmdBrowseOpen({url:"about:blank", wait_selector:"#ready", scope_id:"scope-a", timeout:1000, settle_ms:0})',
  );
  while (state.tabs.size === 0) await nextTurn();

  let cleanupSettled = false;
  const cleanup = evaluate(state, 'cmdCloseOwnedTabs({scope_id:"scope-a"})').finally(() => {
    cleanupSettled = true;
  });
  await nextTurn();
  assert.equal(cleanupSettled, false, "cleanup returned before browse_open settled");

  selectorGate.resolve();
  await assert.rejects(
    opening,
    (error) => error.bridge?.code === "SCOPE_CLOSING" || /No tab with id/.test(error.message),
  );
  const result = plain(await cleanup);
  assert.deepEqual(result.remaining, []);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), []);
}

async function testDelayedChildIsDiscardedWhileParentCloseIsPending() {
  const parentRemoval = deferred();
  let parentId = null;
  const state = await harness({
    setTimeout(callback) {
      return setTimeout(callback, 0);
    },
    async remove({ id, emitRemoved }) {
      if (id === parentId) {
        await parentRemoval.promise;
      }
      await emitRemoved(id);
    },
  });
  const parent = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  parentId = parent.id;

  const result = plain(await evaluate(state, 'cmdCloseOwnedTabs({scope_id:"scope-a"})'));
  assert.deepEqual(result.pending, [String(parent.id)]);
  assert.equal(result.failure_id, undefined);
  assert.deepEqual(state.storedFailureQueue(), []);
  const child = await evaluate(
    state,
    `chrome.tabs.create({url:"https://child.example", active:false, windowId:${parent.windowId}, openerTabId:${parent.id}})`,
  );
  await nextTurn();
  await nextTurn();
  assert.equal(state.tabs.has(child.id), false, "late child escaped draining cleanup");
  await assert.rejects(
    evaluate(state, 'createBridgeTab("about:blank", "scope-a")'),
    (error) => error.bridge.code === "SCOPE_CLOSING",
  );

  parentRemoval.resolve();
  await nextTurn();
  await nextTurn();
  const reopened = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  assert.equal(state.tabs.has(reopened.id), true);
}

async function testCloseRejectsUserTabsAndOtherScopes() {
  const state = await harness();
  const userTab = await evaluate(
    state,
    'chrome.tabs.create({url:"https://user.example", active:true, windowId:1})',
  );
  const tabB = await evaluate(state, 'createBridgeTab("about:blank", "scope-b")');

  await assert.rejects(
    evaluate(state, `cmdBrowseClose({tab_id:"${userTab.id}", scope_id:"scope-a"})`),
    (error) => error.bridge.code === "TAB_NOT_OWNED",
  );
  await assert.rejects(
    evaluate(state, `cmdBrowseClose({tab_id:"${tabB.id}", scope_id:"scope-a"})`),
    (error) => error.bridge.code === "TAB_SCOPE_MISMATCH",
  );
  assert.equal(state.tabs.has(userTab.id), true);
  assert.equal(state.tabs.has(tabB.id), true);
}

async function testOtherScopesCannotTargetOwnedTabs() {
  const state = await harness();
  const userTab = await evaluate(
    state,
    'chrome.tabs.create({url:"https://user.example", active:true, windowId:1})',
  );
  const tabB = await evaluate(state, 'createBridgeTab("about:blank", "scope-b")');

  await assert.rejects(
    evaluate(state, `resolveTab({tab_id:"${tabB.id}", scope_id:"scope-a"})`),
    (error) => error.bridge.code === "TAB_SCOPE_MISMATCH",
  );
  const attachedUserTab = await evaluate(
    state,
    `resolveTab({tab_id:"${userTab.id}", scope_id:"scope-a"})`,
  );
  assert.equal(attachedUserTab.id, userTab.id);
}

async function testChildTabsInheritScopeAndCleanup() {
  const state = await harness();
  const parent = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  const child = await evaluate(
    state,
    `chrome.tabs.create({url:"https://child.example", active:false, windowId:${parent.windowId}, openerTabId:${parent.id}})`,
  );
  await nextTurn();
  assert.equal(await evaluate(state, `_tabScopes.get(${child.id})`), "scope-a");

  await evaluate(state, 'cmdCloseOwnedTabs({scope_id:"scope-a"})');
  assert.equal(state.tabs.has(parent.id), false);
  assert.equal(state.tabs.has(child.id), false);
}

async function testPopupChildIsRehomedIntoTheMinimizedAutomationWindow() {
  const state = await harness();
  const parent = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  const popup = await evaluate(
    state,
    `chrome.windows.create({url:"https://popup.example", state:"normal", focused:true, openerTabId:${parent.id}})`,
  );
  const childId = popup.tabs[0].id;
  const popupWindowId = popup.id;
  await nextTurn();
  await nextTurn();

  assert.equal(state.tabs.get(childId).windowId, parent.windowId);
  assert.equal(state.windows.has(popupWindowId), false);
  assert.equal(state.windows.get(parent.windowId).state, "minimized");
  assert.equal(state.windows.get(parent.windowId).focused, false);
}

async function testTabReplacementTransfersOwnershipAndRoutes() {
  const state = await harness();
  const original = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  await evaluate(
    state,
    `_managedTabs.set("scope-a", ${original.id}); _sessions.set(sessionKey("scope-a", "dash"), ${original.id})`,
  );
  const replacement = { ...original, id: 99, url: "https://replacement.example" };
  await state.emitReplaced(replacement, original.id);

  assert.equal(await evaluate(state, '_managedTabs.get("scope-a")'), 99);
  assert.equal(await evaluate(state, '_sessions.get(sessionKey("scope-a", "dash"))'), 99);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [99]);
  assert.equal(await evaluate(state, "_tabScopes.get(99)"), "scope-a");
  await evaluate(state, 'cmdCloseOwnedTabs({scope_id:"scope-a"})');
  assert.equal(state.tabs.has(99), false);
}

async function testForegroundingFailsClosedByDefault() {
  const state = await harness();
  const tab = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');

  await assert.rejects(
    evaluate(
      state,
      `cmdCdpMouse({action:"click", x:1, y:1, tab_id:"${tab.id}", scope_id:"scope-a"})`,
    ),
    (error) => error.bridge.code === "FOREGROUND_REQUIRED",
  );
  await assert.rejects(
    evaluate(state, `cmdActivateTab({tab_id:"${tab.id}", scope_id:"scope-a"})`),
    (error) => error.bridge.code === "FOREGROUND_REQUIRED",
  );
  assert.equal(state.windowUpdates.some(({ update }) => update.focused === true), false);
  assert.equal(state.windows.get(1).focused, true);
}

async function testForegroundTrustedInputHoldsTheAutomationWindowLock() {
  const pressStarted = deferred();
  const releasePress = deferred();
  const state = await harness({
    async debuggerSendCommand({ method, params }) {
      if (method === "Input.dispatchMouseEvent" && params.type === "mousePressed") {
        pressStarted.resolve();
        await releasePress.promise;
      }
    },
  });
  const tab = await evaluate(state, 'createBridgeTab("about:blank", "scope-a")');
  state.windows.get(tab.windowId).state = "normal";
  state.windows.get(tab.windowId).focused = true;
  state.windows.get(1).focused = false;

  const clicking = evaluate(
    state,
    `cmdCdpMouse({action:"click", x:1, y:1, tab_id:"${tab.id}", scope_id:"scope-a"})`,
  );
  await pressStarted.promise;
  let openSettled = false;
  const opening = evaluate(state, 'createBridgeTab("about:blank", "scope-b")').finally(() => {
    openSettled = true;
  });
  await nextTurn();
  assert.equal(openSettled, false, "tab creation minimized the window during trusted input");
  assert.equal(state.windows.get(tab.windowId).state, "normal");

  releasePress.resolve();
  await clicking;
  await opening;
  assert.equal(state.windows.get(tab.windowId).state, "minimized");
}

async function testCookieScopeIsStrictAndExplicit() {
  const state = await harness();

  await evaluate(state, 'cmdGetCookies({domain:" example.com "})');
  await evaluate(state, "cmdGetCookies({all_domains:true})");
  assert.deepEqual(plain(state.cookieCalls), [{ domain: "example.com" }, {}]);

  await assert.rejects(evaluate(state, "cmdGetCookies({})"), /cookie scope required/);
  await assert.rejects(
    evaluate(state, 'cmdGetCookies({domain:"example.com", all_domains:true})'),
    /mutually exclusive/,
  );
  await assert.rejects(
    evaluate(state, 'cmdGetCookies({all_domains:"true"})'),
    /cookie scope required/,
  );
  await assert.rejects(
    evaluate(state, 'cmdGetCookies({domain:"", all_domains:true})'),
    /cookie scope required/,
  );
  await assert.rejects(evaluate(state, "cmdGetCookies({domain:42})"), /cookie scope required/);
}

async function testCommandFailureIsPersistedAndFlushedWithoutSensitiveInputs() {
  const canary = "PRIVATE_URL_https://secret.example/?cookie=TOPSECRET";
  const state = await harness({
    async executeScript() {
      return [{ result: { __bridge_error: { code: "JS_ERROR", message: canary, detail: { stack: canary } } } }];
    },
  });
  await state.socket().open();

  await state.socket().message({
    id: "request-1",
    method: "evaluate",
    params: { expression: canary },
  });
  await evaluate(state, "_failureQueueTail");
  await evaluate(state, "_failureFlushTail");

  const stored = state.storedFailureQueue();
  const batches = state.websocketFrames.filter((frame) => frame.type === "failure_batch");
  assert.equal(stored.length, 1);
  assert.equal(stored[0].code, "JS_ERROR");
  assert.equal(stored[0].operation, "evaluate");
  assert.equal(JSON.stringify(stored).includes(canary), false);
  assert.equal(JSON.stringify(batches).includes(canary), false);
  const reply = state.websocketFrames.find((frame) => frame.id === "request-1");
  assert.equal(reply.error.failure_id, stored[0].event_id);
}

async function testRelayDownQueueSurvivesRestartAndAckClearsIt() {
  const first = await harness();
  await evaluate(
    first,
    'recordExtensionFailure("RELAY_UNREACHABLE", "extension_connection", "connect", {retryable:true})',
  );
  const persisted = first.storedFailureQueue();
  assert.equal(persisted.length, 1);

  const second = await harness({ initialFailureQueue: persisted });
  await second.socket().open();
  await evaluate(second, "_failureFlushTail");
  const batch = second.websocketFrames.find((frame) => frame.type === "failure_batch");
  assert.equal(batch.events.length, 1);
  assert.equal(batch.events[0].event_id, persisted[0].event_id);

  await second.socket().message({ type: "failure_ack", event_ids: [persisted[0].event_id] });
  await evaluate(second, "_failureQueueTail");
  assert.deepEqual(second.storedFailureQueue(), []);
}

async function testEmptyAckSchedulesBoundedRetryUntilTheQueueIsDurable() {
  const scheduled = [];
  const cleared = [];
  const state = await harness({
    setTimeout(callback, delay) {
      scheduled.push({ callback, delay });
      return scheduled.length;
    },
    clearTimeout(id) {
      cleared.push(id);
    },
  });
  await evaluate(
    state,
    'recordExtensionFailure("JS_ERROR", "evaluate", "command", {retryable:false})',
  );
  const [event] = state.storedFailureQueue();
  await state.socket().open();

  await state.socket().message({
    type: "failure_ack",
    event_ids: [],
    retry_after_ms: 60_000,
  });
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].delay, 60_000);
  assert.equal(await evaluate(state, "_failureRetryTimer !== null"), true);

  await state.socket().message({ type: "failure_ack", event_ids: [event.event_id] });
  assert.equal(await evaluate(state, "_failureRetryTimer"), null);
  assert.deepEqual(cleared, [1]);
}

async function testMalformedStructuredRelayFrameIsJournaled() {
  const state = await harness();
  await state.socket().open();
  await state.socket().message(null);
  await evaluate(state, "_failureQueueTail");

  const stored = state.storedFailureQueue();
  assert.equal(stored.length, 1);
  assert.equal(stored[0].code, "PROTOCOL_INVALID_FRAME");
  assert.equal(stored[0].operation, "protocol");
}

async function testExpectedRelayIdleDoesNotFloodFailureQueue() {
  const neverConnected = await harness();
  await neverConnected.socket().error();
  await neverConnected.socket().close(1006, false);
  assert.deepEqual(neverConnected.storedFailureQueue(), []);

  const outage = await harness();
  await outage.socket().open();
  await outage.socket().error();
  await outage.socket().close(1006, false);
  await evaluate(outage, "_failureQueueTail");
  assert.equal(outage.storedFailureQueue().length, 1);
  assert.equal(outage.storedFailureQueue()[0].code, "RELAY_UNREACHABLE");

  const cleanStop = await harness();
  await cleanStop.socket().open();
  await cleanStop.socket().close(1000, true);
  await cleanStop.socket().error();
  assert.deepEqual(cleanStop.storedFailureQueue(), []);
}

async function testPersistedFailureQueueIsBoundedExpiredAndSanitized() {
  const now = Date.now();
  const canary = "PRIVATE_https://secret.example/?token=hidden";
  const events = [
    {
      event_id: "f".repeat(32),
      occurred_at_ms: now - 8 * 24 * 60 * 60 * 1000,
      code: "JS_ERROR",
      operation: "evaluate",
      phase: "command",
    },
  ];
  for (let index = 0; index < 105; index += 1) {
    events.push({
      event_id: index.toString(16).padStart(32, "0"),
      occurred_at_ms: now,
      code: index === 104 ? canary : "JS_ERROR",
      operation: index === 104 ? canary : "evaluate",
      phase: "command",
      message: canary,
      params: { url: canary },
    });
  }

  const state = await harness({ initialFailureQueue: events });
  const stored = state.storedFailureQueue();
  assert.equal(stored.length, 100);
  assert.equal(JSON.stringify(stored).includes(canary), false);
  assert.ok(JSON.stringify(stored).length <= 64 * 1024);
  assert.equal(stored.at(-1).code, "INTERNAL");
  assert.equal(stored.at(-1).operation, "unknown");

  const expiryOnly = await harness({
    initialFailureQueue: [
      {
        event_id: "e".repeat(32),
        occurred_at_ms: now - 8 * 24 * 60 * 60 * 1000,
        code: "JS_ERROR",
        operation: "evaluate",
        phase: "command",
      },
      {
        event_id: "d".repeat(32),
        occurred_at_ms: now,
        code: "JS_ERROR",
        operation: "evaluate",
        phase: "command",
      },
    ],
  });
  assert.deepEqual(
    expiryOnly.storedFailureQueue().map((event) => event.event_id),
    ["d".repeat(32)],
  );
}

async function testHydrationPreservesTheVersionThatActuallyFailed() {
  const now = Date.now();
  const state = await harness({
    manifestVersion: "2.2.0",
    initialFailureQueue: [
      {
        event_id: "d".repeat(32),
        occurred_at_ms: now,
        code: "JS_ERROR",
        operation: "evaluate",
        phase: "command",
        extension_version: "2.1.0",
      },
    ],
  });

  assert.equal(state.storedFailureQueue()[0].extension_version, "2.1.0");
}

async function testRepeatedFailuresUseImmutableIdsAndStorageFailureFallsBackSafely() {
  const state = await harness();
  await evaluate(
    state,
    'recordExtensionFailure("RELAY_UNREACHABLE", "extension_connection", "connect", {retryable:true})',
  );
  await evaluate(
    state,
    'recordExtensionFailure("RELAY_UNREACHABLE", "extension_connection", "connect", {retryable:true})',
  );
  const stored = state.storedFailureQueue();
  assert.equal(stored.length, 2);
  assert.notEqual(stored[0].event_id, stored[1].event_id);
  assert.equal(stored[0].count, 1);
  assert.equal(stored[1].count, 1);

  await state.socket().open();
  await state.socket().message({ type: "failure_ack", event_ids: [stored[0].event_id] });
  await evaluate(state, "_failureQueueTail");
  assert.deepEqual(
    state.storedFailureQueue().map((event) => event.event_id),
    [stored[1].event_id],
  );
  await state.socket().message({ type: "failure_ack", event_ids: [stored[1].event_id] });
  assert.deepEqual(state.storedFailureQueue(), []);

  const broken = await harness({ failureStorageSetError: true });
  const id = await evaluate(
    broken,
    'recordExtensionFailure("STATE_PERSIST_FAILED", "state", "persist", {retryable:true})',
  );
  assert.equal(typeof id, "string");
  assert.equal(await evaluate(broken, "_failureMemoryFallback.length"), 1);
}

await testTransientTabTeardownRetriesUntilDeadline();
await testNonTransientCreationFailureClosesTheTab();
await testFrameLossOnALiveTabDoesNotRetry();
await testPendingRemovalStaysTrackedUntilOnRemoved();
await testRefusedRemovalStaysTracked();
await testDelayedRemovalRefusalIsLoggedAndStaysTracked();
await testRemovalRejectionAfterTabVanishesIsSuccess();
await testFailedOpenSurfacesCleanupFailureWithTabId();
await testFailedOpenSurfacesUnconfirmedCleanupWithTabId();
await testImmediateCloseRefusalRestoresNamedRoute();
await testDelayedCloseRefusalRestoresNamedRoute();
await testRefusalDoesNotOverwriteConcurrentNamedReplacement();
await testImmediateRefusalDoesNotWaitForFailingReplacement();
await testNavigateUsesOneDeadlineAcrossStartupAndLoad();
await testNewBackgroundTabUsesDedicatedMinimizedWindow();
await testNamedSessionRecoversOnlyAfterItsTabVanishes();
await testSameUrlNamedReuseRecoversIfTabVanishesBeforeReturn();
await testConcurrentSameNameOpenCreatesOneTab();
await testTimedOutNamedWaiterPreservesFifoOrder();
await testNamedReuseSharesTheOpenDeadline();
await testPendingNamedCloseReopensWithoutReusingDoomedTab();
await testScopesIsolateManagedTabsAndNamedSessions();
await testNumericSessionNamesAreRejected();
await testLegacyStateHydratesIntoTheReservedScope();
await testScopedCleanupPreservesUserAndOtherScopeTabs();
await testCleanupNeverClosesAUserWindow();
await testCleanupWaitsForAnInflightCreate();
await testCleanupWaitsForTheWholeBrowseOpenLifecycle();
await testDelayedChildIsDiscardedWhileParentCloseIsPending();
await testCloseRejectsUserTabsAndOtherScopes();
await testOtherScopesCannotTargetOwnedTabs();
await testChildTabsInheritScopeAndCleanup();
await testPopupChildIsRehomedIntoTheMinimizedAutomationWindow();
await testTabReplacementTransfersOwnershipAndRoutes();
await testForegroundingFailsClosedByDefault();
await testForegroundTrustedInputHoldsTheAutomationWindowLock();
await testCookieScopeIsStrictAndExplicit();
await testCommandFailureIsPersistedAndFlushedWithoutSensitiveInputs();
await testRelayDownQueueSurvivesRestartAndAckClearsIt();
await testEmptyAckSchedulesBoundedRetryUntilTheQueueIsDurable();
await testMalformedStructuredRelayFrameIsJournaled();
await testExpectedRelayIdleDoesNotFloodFailureQueue();
await testPersistedFailureQueueIsBoundedExpiredAndSanitized();
await testHydrationPreservesTheVersionThatActuallyFailed();
await testRepeatedFailuresUseImmutableIdsAndStorageFailureFallsBackSafely();
