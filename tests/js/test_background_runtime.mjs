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
  const warnings = [];
  let storedState = options.initialState ? plain(options.initialState) : null;
  let nextTabId = 1;
  let createCount = 0;
  let removeCount = 0;
  let onRemoved = async () => {};

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
      async sendCommand() {},
    },
    declarativeNetRequest: {
      async updateDynamicRules() {},
      async updateSessionRules(update) {
        cspUpdates.push(plain(update));
      },
    },
    runtime: {
      getManifest: () => ({ version: "test" }),
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
    },
    tabs: {
      async create() {
        createCount += 1;
        const tab = {
          id: nextTabId++,
          url: "about:blank",
          status: "complete",
          windowId: 1,
          active: false,
        };
        tabs.set(tab.id, tab);
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
      async query() {
        return [...tabs.values()].map((tab) => ({ ...tab }));
      },
      async remove(id) {
        removeCount += 1;
        if (options.remove) {
          return await options.remove({ id, tabs, onRemoved, emitRemoved });
        }
        tabOrThrow(id);
        await emitRemoved(id);
      },
      async update(id, update) {
        if (options.update) return await options.update({ id, update, tabs, tabOrThrow });
        const tab = tabOrThrow(id);
        Object.assign(tab, update, { status: "complete" });
        return { ...tab };
      },
    },
    windows: {
      async create() {
        throw new Error("unexpected windows.create");
      },
      async getAll() {
        return [{ id: 1 }];
      },
      async update() {},
    },
  };

  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;

    constructor() {
      this.readyState = FakeWebSocket.CONNECTING;
    }
  }

  const context = vm.createContext({
    chrome,
    clearTimeout,
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

  return {
    context,
    tabs,
    cookieCalls,
    cspUpdates,
    storageWrites,
    warnings,
    emitRemoved,
    storedState: () => (storedState ? plain(storedState) : null),
    cspTabIds: () => {
      const last = cspUpdates.at(-1);
      return last?.addRules?.[0]?.condition?.tabIds || [];
    },
    counts: () => ({ create: createCount, remove: removeCount }),
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
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["dash", 1]]);
  assert.deepEqual(state.cspTabIds(), [1]);
  assert.deepEqual(state.storedState(), {
    managedTabId: null,
    sessions: [["dash", 1]],
    bridgeTabs: [1],
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
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["dash", 1]]);
  assert.deepEqual(state.storedState().sessions, [["dash", 1]]);
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
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["dash", 2]]);
  assert.deepEqual(state.cspTabIds(), [1, 2]);
  assert.deepEqual(state.storedState().sessions, [["dash", 2]]);
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
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["dash", 1]]);
  assert.deepEqual(plain(await evaluate(state, "[..._sessionOpenLocks]")), []);
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [1]);
  assert.deepEqual(state.cspTabIds(), [1]);
  assert.deepEqual(state.storedState(), {
    managedTabId: null,
    sessions: [["dash", 1]],
    bridgeTabs: [1],
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

async function testNewBackgroundTabFallsBackToAnExistingWindow() {
  const state = await harness();
  await evaluate(
    state,
    `globalThis.createArgs = [];
     const realCreate = chrome.tabs.create.bind(chrome.tabs);
     chrome.tabs.create = async (params) => {
       globalThis.createArgs.push(params);
       if (globalThis.createArgs.length === 1) throw new Error("No current window");
       return await realCreate(params);
     };
     chrome.windows.getAll = async () => [{id: 7}];`,
  );

  const tab = await evaluate(state, "newBackgroundTab()");
  const createArgs = plain(await evaluate(state, "globalThis.createArgs"));
  assert.equal(tab.id, 1);
  assert.deepEqual(createArgs, [
    { url: "about:blank", active: false },
    { url: "about:blank", active: false, windowId: 7 },
  ]);
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
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["dash", 2]]);
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
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["dash", 2]]);
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
  assert.deepEqual(state.storedState().sessions, [["dash", 1]]);
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
  assert.deepEqual(plain(await evaluate(state, "[..._sessions.entries()]")), [["dash", 2]]);
  assert.deepEqual(state.cspTabIds(), [1, 2]);
  const closed = await closing;
  assert.equal(closed.confirmed, null);
  assert.deepEqual(state.storedState(), {
    managedTabId: null,
    sessions: [["dash", 2]],
    bridgeTabs: [1, 2],
  });

  await state.emitRemoved(1);
  removal.resolve();
  await nextTurn();
  assert.deepEqual(plain(await evaluate(state, "[..._bridgeTabs]")), [2]);
  assert.deepEqual(state.cspTabIds(), [2]);
  assert.deepEqual(state.storedState(), {
    managedTabId: null,
    sessions: [["dash", 2]],
    bridgeTabs: [2],
  });
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
await testNewBackgroundTabFallsBackToAnExistingWindow();
await testNamedSessionRecoversOnlyAfterItsTabVanishes();
await testSameUrlNamedReuseRecoversIfTabVanishesBeforeReturn();
await testConcurrentSameNameOpenCreatesOneTab();
await testTimedOutNamedWaiterPreservesFifoOrder();
await testNamedReuseSharesTheOpenDeadline();
await testPendingNamedCloseReopensWithoutReusingDoomedTab();
await testCookieScopeIsStrictAndExplicit();
