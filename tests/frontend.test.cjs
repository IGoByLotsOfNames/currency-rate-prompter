"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { JSDOM } = require("jsdom");

const staticRoot = path.join(__dirname, "../src/currency_prompter/static");
const html = readFileSync(path.join(staticRoot, "index.html"), "utf8");
const source = readFileSync(path.join(staticRoot, "app.js"), "utf8");
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, json: async () => JSON.parse(JSON.stringify(data)) });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
async function settle() { for (let index = 0; index < 6; index += 1) await new Promise(setImmediate); }

function currencies() {
  const values = [
    { code: "SGD", name: "Singapore dollar" }, { code: "THB", name: "Thai baht" },
    { code: "JPY", name: "Japanese yen" }, { code: "USD", name: "US dollar" },
    { code: "EUR", name: "Euro" }, { code: "GBP", name: "British pound" },
  ];
  for (let index = 0; values.length < 165; index += 1) {
    const code = "A" + String.fromCharCode(65 + Math.floor(index / 26)) + String.fromCharCode(65 + index % 26);
    values.push({ code, name: "Fixture currency " + code });
  }
  return values;
}

async function workspace(t, options = {}) {
  const dom = new JSDOM(html, { url: "http://localhost:8765", runScripts: "outside-only", pretendToBeVisual: true });
  t.after(() => { dom.window.close(); assert.deepEqual(errors, [], "No DOM error should be hidden by the test harness"); });
  const window = dom.window, document = window.document;
  const app = {
    csrf_token: "fixture-token-kept-in-memory", currencies: currencies(), saved_currencies: [],
    watchlist: [{ base: "SGD", counter: "THB" }, { base: "USD", counter: "SGD" }, { base: "EUR", counter: "SGD" }],
    mode: "demo", catalogue: { count: 165, as_of: "2026-10-03", source: "bundled", scope: "active", url: "https://example.invalid/catalogue" },
    ...options.app,
  };
  const calls = [], timers = new Map(), errors = [];
  let nextTimer = 0, hidden = false;
  window.setTimeout = (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; };
  window.clearTimeout = (id) => timers.delete(id);
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  window.addEventListener("error", (event) => errors.push(event.error || event.message));
  if (options.preferences) window.localStorage.setItem("crp.workspace.v1", JSON.stringify(options.preferences));
  const api = {
    app, calls, timers, errors, window, document, handler: null,
    element: (id) => document.getElementById(id),
    click: (id) => document.getElementById(id).click(),
    input(id, value) { this.element(id).value = value; this.element(id).dispatchEvent(new window.Event("input", { bubbles: true })); },
    change(id, value) { this.element(id).value = value; this.element(id).dispatchEvent(new window.Event("change", { bubbles: true })); },
    submit(id) { this.element(id).dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); },
    hide(value) { hidden = value; document.dispatchEvent(new window.Event("visibilitychange")); },
    scheduled() { return Array.from(timers.entries()).filter(([, timer]) => timer.delay >= 900000); },
    fireScheduled() {
      const entry = this.scheduled()[0]; assert.ok(entry, "Expected a pending automatic refresh");
      timers.delete(entry[0]); entry[1].callback();
    },
    dashboard(params, changes = {}) {
      const base = params.get("base"), counter = params.get("counter"), mode = params.get("mode");
      const rate = options.rate || (base === "SGD" ? "28.125000" : base === "EUR" ? "1.450000" : "1.320000");
      return {
        pair: { base, counter }, mode, source: mode === "demo" ? "Synthetic demo" : "Daily reference",
        latest: { rate, observed_at: "2026-09-30", received_at: "2026-10-03T01:00:00Z" },
        previous_rate: rate, change_percent: "0", low: rate, high: rate,
        points: [{ observed_at: "2026-09-29", rate }, { observed_at: "2026-09-30", rate }],
        rules: [], alerts: [], last_refresh: null, stale: false, note: "Fixture data only.", ...changes,
      };
    },
  };
  window.fetch = async (input, init) => {
    const url = new URL(input, "http://localhost:8765");
    assert.equal(url.origin, "http://localhost:8765", "No external fetch is permitted");
    const request = { path: url.pathname, params: url.searchParams, method: init.method, body: init.body ? JSON.parse(init.body) : null, headers: init.headers };
    calls.push(request);
    if (api.handler) { const result = await api.handler(request); if (result !== undefined) return result; }
    if (request.path === "/api/app") return response(app);
    if (request.path === "/api/dashboard") return response(api.dashboard(request.params));
    if (request.path === "/api/watchlist" && request.method === "GET") return response({ watchlist: app.watchlist.map((pair) => ({ ...pair, rate: options.rate || "28.125000", change_percent: "0", observed_at: "2026-09-30" })) });
    if (request.path === "/api/refresh" || request.path === "/api/history") return response({ ok: true });
    if (request.path === "/api/currencies/refresh") return response({ ok: true, currencies: app.currencies, saved_currencies: app.saved_currencies, catalogue: app.catalogue });
    if (request.path === "/api/rules") return response({ ok: true });
    if (request.path === "/api/watchlist" && request.method === "POST") { app.watchlist.push(request.body); return response({ ok: true }); }
    if (request.path === "/api/watchlist" && request.method === "DELETE") { app.watchlist = app.watchlist.filter((pair) => pair.base !== request.body.base || pair.counter !== request.body.counter); return response({ ok: true }); }
    throw new Error("Unexpected fake API request: " + request.method + " " + request.path);
  };
  require("node:vm").runInContext(source, dom.getInternalVMContext(), { filename: path.join(staticRoot, "app.js") });
  await settle();
  assert.deepEqual(errors, []);
  assert.equal(api.element("app-error").hidden, true, api.element("app-error-text").textContent);
  return api;
}

test("startup and reference mode only read local state; catalogue refresh requires a click", async (t) => {
  const ui = await workspace(t);
  assert.equal(ui.element("add-base").options.length, 165);
  assert.match(ui.element("catalogue-status").textContent, /165 active currencies.*bundled.*2026/);
  assert.equal(ui.element("add-base").value, "SGD");
  assert.equal(ui.element("add-counter").value, "THB");
  assert.equal(ui.element("input-currency").textContent, "THB");
  assert.equal(ui.element("output-currency").textContent, "SGD");
  ui.click("mode-reference"); await settle();
  ui.change("auto-refresh", "15");
  assert.equal(ui.calls.filter((call) => call.method !== "GET").length, 0);
  assert.equal(ui.scheduled().length, 1);
  assert.doesNotMatch(ui.window.localStorage.getItem("crp.workspace.v1"), /fixture-token/);
});

test("independent code/name filters retain selection, report no matches, and reset cleanly", async (t) => {
  const ui = await workspace(t);
  ui.input("add-base-search", "jApAnEsE");
  assert.equal(ui.element("add-base").value, "SGD");
  assert.equal(ui.element("add-base").options.length, 2);
  assert.match(ui.element("add-base-count").textContent, /1 match.*retained/);
  assert.equal(ui.element("add-counter").options.length, 165);
  ui.change("add-base", "JPY");
  assert.equal(ui.element("add-base").options.length, 1);
  ui.input("add-base-search", "");
  assert.equal(ui.element("add-base").value, "JPY");
  assert.equal(ui.element("add-base").options.length, 165);
  ui.input("add-counter-search", "thai");
  assert.equal(ui.element("add-counter").value, "THB");
  assert.equal(ui.element("add-base").value, "JPY");
  ui.input("add-counter-search", "no such currency");
  assert.equal(ui.element("add-counter").options.length, 1);
  assert.equal(ui.element("add-counter").value, "THB");
  assert.match(ui.element("add-counter-count").textContent, /0 matches.*retained/);
});

test("catalogue success preserves current pair, mode, search, and form choices", async (t) => {
  const ui = await workspace(t);
  ui.click("mode-reference"); await settle();
  ui.change("pair-select", "USD/SGD"); await settle();
  ui.change("add-base", "JPY"); ui.input("add-base-search", "yen");
  ui.change("add-counter", "USD"); ui.input("add-counter-search", "dollar");
  ui.input("convert-amount", "12345678901234567890.01");
  ui.input("rule-name", "Keep this target draft"); ui.input("rule-threshold", "1.2");
  const pending = deferred();
  ui.handler = (request) => request.path === "/api/currencies/refresh" ? pending.promise : undefined;
  ui.click("catalogue-refresh"); ui.click("catalogue-refresh");
  assert.equal(ui.element("catalogue-refresh").disabled, true);
  assert.equal(ui.calls.filter((call) => call.path === "/api/currencies/refresh").length, 1);
  ui.app.currencies = ui.app.currencies.concat({ code: "ZZZ", name: "Additional fixture currency" });
  ui.app.catalogue = { ...ui.app.catalogue, source: "cached", count: 166 };
  pending.resolve(response({ ok: true, currencies: ui.app.currencies, catalogue: ui.app.catalogue, saved_currencies: [] }));
  await settle();
  assert.equal(ui.element("pair-select").value, "USD/SGD");
  assert.equal(ui.element("mode-reference").getAttribute("aria-pressed"), "true");
  assert.equal(ui.element("add-base").value, "JPY");
  assert.equal(ui.element("add-base-search").value, "yen");
  assert.equal(ui.element("add-counter").value, "USD");
  assert.equal(ui.element("convert-amount").value, "12345678901234567890.01");
  assert.equal(ui.element("rule-name").value, "Keep this target draft");
  assert.match(ui.element("catalogue-status").textContent, /166 active currencies.*saved provider/);
  const request = ui.calls.find((call) => call.path === "/api/currencies/refresh");
  assert.deepEqual(request.body, {});
  assert.equal(request.headers["X-CSRF-Token"], ui.app.csrf_token);
  assert.equal(ui.element("catalogue-refresh").disabled, false);
});


test("catalogue failure leaves the displayed list and selections usable", async (t) => {
  const ui = await workspace(t);
  ui.change("add-base", "JPY"); ui.input("add-base-search", "yen");
  const before = Array.from(ui.element("add-base").options, (option) => [option.value, option.textContent]);
  const status = ui.element("catalogue-status").textContent;
  ui.handler = (request) => request.path === "/api/currencies/refresh" ? response({ error: "Currency provider did not respond." }, 503) : undefined;
  ui.click("catalogue-refresh"); await settle();
  assert.deepEqual(Array.from(ui.element("add-base").options, (option) => [option.value, option.textContent]), before);
  assert.equal(ui.element("catalogue-status").textContent, status);
  assert.equal(ui.element("add-base").value, "JPY");
  assert.match(ui.element("toast").textContent, /Currency provider did not respond/);
  assert.equal(ui.element("catalogue-refresh").disabled, false);
  ui.input("add-base-search", "");
  assert.equal(ui.element("add-base").options.length, 165);
});

test("saved unavailable currencies stay visible and a refreshed list does not silently replace a selected code", async (t) => {
  const ui = await workspace(t, {
    app: { saved_currencies: [{ code: "ZZZ", name: "Earlier fixture currency" }], watchlist: [{ base: "ZZZ", counter: "SGD" }] },
    preferences: { mode: "reference", pair: { base: "ZZZ", counter: "SGD" } },
  });
  assert.equal(ui.element("pair-select").value, "ZZZ/SGD");
  assert.match(ui.element("pair-name").textContent, /Earlier fixture currency/);
  const saved = Array.from(ui.element("add-base").options).find((option) => option.value === "ZZZ");
  assert.match(saved.textContent, /unavailable/);
  assert.match(ui.element("catalogue-status").textContent, /165 active/);
  ui.change("add-base", "JPY");
  ui.app.currencies = ui.app.currencies.filter((currency) => currency.code !== "JPY");
  ui.app.catalogue = { ...ui.app.catalogue, source: "cached", count: 164 };
  ui.click("catalogue-refresh"); await settle();
  assert.equal(ui.element("add-base").value, "JPY");
  assert.match(ui.element("add-base").selectedOptions[0].textContent, /unavailable/);
  assert.equal(ui.element("pair-select").value, "ZZZ/SGD");
});

test("unsupported pair errors preserve the user's choices and prevent duplicate submissions", async (t) => {
  const ui = await workspace(t);
  ui.click("add-pair-toggle"); ui.change("add-base", "JPY"); ui.change("add-counter", "USD");
  const pending = deferred();
  ui.handler = (request) => request.path === "/api/watchlist" && request.method === "POST" ? pending.promise : undefined;
  ui.submit("add-pair-form"); ui.submit("add-pair-form");
  ui.document.querySelector('[data-days="7"]').click(); await settle();
  assert.equal(ui.element("watchlist-fields").disabled, true);
  assert.equal(ui.calls.filter((call) => call.path === "/api/watchlist" && call.method === "POST").length, 1);
  pending.resolve(response({ error: "This currency pair is not supported by the provider." }, 400)); await settle();
  assert.match(ui.element("toast").textContent, /not supported by the provider/);
  assert.equal(ui.element("add-base").value, "JPY");
  assert.equal(ui.element("add-counter").value, "USD");
  assert.equal(ui.element("add-pair-form").hidden, false);
  assert.equal(ui.element("watchlist-fields").disabled, false);
});

test("rate display preserves tiny nonzero rates and the history table retains exact source strings", async (t) => {
  for (const rate of ["0.0000000312345", "0.000000000000012345", "0.00000000000000000000312", "1234567890123.123456789", "0." + "0".repeat(329) + "1"]) {
    await t.test(rate.length > 60 ? "below JavaScript numeric range" : rate, async (subtest) => {
      const ui = await workspace(subtest, { rate });
      const shown = ui.element("quote-value").textContent;
      assert.notEqual(shown.replaceAll(",", ""), "0.00");
      assert.notEqual(shown, "—");
      assert.match(shown, /[1-9]/);
      assert.equal(ui.element("quote-value").title, rate);
      assert.ok(ui.element("history-rows").textContent.includes(rate + " THB per SGD"));
      assert.ok(ui.element("range-low").textContent.match(/[1-9]/));
    });
  }
});

test("conversion sends decimal strings, accepts 100% fees, and displays the exact returned amount", async (t) => {
  const ui = await workspace(t);
  const amount = "12345678901234567890.123456789";
  ui.handler = (request) => request.path === "/api/convert" ? response({ ...request.body, result: request.body.fee_percent === "100" ? "0" : "123456789012345678901.123400", rate: "28.125000", observed_at: "2026-09-30" }) : undefined;
  ui.input("convert-amount", amount); ui.input("convert-fee", "100");
  ui.submit("convert-form"); await settle();
  const request = ui.calls.find((call) => call.path === "/api/convert");
  assert.equal(request.body.amount, amount);
  assert.equal(request.body.fee_percent, "100");
  assert.equal(request.body.direction, "counter_to_base");
  assert.equal(ui.element("conversion-value").textContent, "0");
  ui.input("convert-fee", "0"); ui.submit("convert-form"); await settle();
  assert.equal(ui.element("conversion-value").textContent, "123,456,789,012,345,678,901.123400");
  assert.equal(ui.element("output-currency").textContent, "SGD");
  assert.equal(request.headers["X-CSRF-Token"], ui.app.csrf_token);
});

test("target save stays busy through a concurrent dashboard reload and ignores another submit", async (t) => {
  const ui = await workspace(t);
  const pending = deferred();
  ui.handler = (request) => request.path === "/api/rules" && request.method === "POST" ? pending.promise : undefined;
  ui.input("rule-name", "SGD buy target"); ui.input("rule-threshold", "25");
  ui.submit("rule-form"); ui.submit("rule-form");
  ui.document.querySelector('[data-days="7"]').click(); await settle();
  assert.equal(ui.element("rule-fields").disabled, true);
  assert.equal(ui.calls.filter((call) => call.path === "/api/rules" && call.method === "POST").length, 1);
  pending.resolve(response({ ok: true })); await settle();
  assert.equal(ui.element("rule-fields").disabled, false);
  assert.match(ui.element("toast").textContent, /Target created/);
});

test("scheduled refresh survives overlap with history loading and pauses while hidden", async (t) => {
  const ui = await workspace(t);
  ui.click("mode-reference"); await settle();
  ui.change("auto-refresh", "15");
  const pending = deferred();
  ui.handler = (request) => request.path === "/api/history" ? pending.promise : undefined;
  ui.click("history-button"); ui.fireScheduled(); await settle();
  assert.equal(ui.calls.filter((call) => call.path === "/api/refresh").length, 0);
  pending.resolve(response({ ok: true })); await settle();
  assert.equal(ui.scheduled().length, 1);
  assert.equal(ui.document.querySelector('[data-days="90"]').getAttribute("aria-pressed"), "true");
  ui.fireScheduled(); await settle();
  const refresh = ui.calls.filter((call) => call.path === "/api/refresh");
  assert.equal(refresh.length, 1);
  assert.deepEqual(refresh[0].body, { base: "SGD", counter: "THB", mode: "reference" });
  assert.equal(ui.scheduled().length, 1);
  ui.hide(true);
  assert.equal(ui.scheduled().length, 0);
  assert.match(ui.element("schedule-note").textContent, /paused/);
  ui.hide(false);
  assert.equal(ui.scheduled().length, 1);
  assert.equal(ui.calls.filter((call) => call.path === "/api/refresh").length, 1);
});

test("late dashboard results cannot overwrite a more recent pair selection", async (t) => {
  const ui = await workspace(t), pending = deferred();
  let oldRequest;
  ui.handler = (request) => {
    if (request.path === "/api/dashboard" && request.params.get("base") === "USD") { oldRequest = request; return pending.promise; }
  };
  ui.change("pair-select", "USD/SGD"); await settle();
  ui.change("pair-select", "EUR/SGD"); await settle();
  const currentQuote = ui.element("quote-value").textContent;
  pending.resolve(response(ui.dashboard(oldRequest.params, { source: "Old result that must not render", latest: { rate: "999", observed_at: "2026-09-30" } })));
  await settle();
  assert.equal(ui.element("rate-heading").textContent, "EUR / SGD");
  assert.equal(ui.element("quote-value").textContent, currentQuote);
  assert.doesNotMatch(ui.element("quote-source").textContent, /Old result/);
});

test("late converter responses are discarded after the pair changes", async (t) => {
  const ui = await workspace(t), pending = deferred();
  ui.handler = (request) => request.path === "/api/convert" ? pending.promise : undefined;
  ui.input("convert-amount", "100"); ui.submit("convert-form");
  ui.change("pair-select", "EUR/SGD"); await settle();
  pending.resolve(response({ result: "777", direction: "counter_to_base", base: "SGD", counter: "THB", rate: "28", fee_percent: "0", observed_at: "2026-09-30", mode: "demo" }));
  await settle();
  assert.equal(ui.element("conversion-value").textContent, "—");
  assert.equal(ui.element("input-currency").textContent, "SGD");
  assert.equal(ui.element("output-currency").textContent, "EUR");
});

test("external text stays literal and a deleted target's historical journal entry remains visible", async (t) => {
  const ui = await workspace(t);
  const malicious = '<img src=x onerror="window.__executed=true">';
  ui.app.currencies[0].name = malicious;
  let rules = [{ id: "rule-1", name: malicious, base: "SGD", counter: "THB", direction: "at_or_below", threshold: "25", cooldown_seconds: 3600, enabled: true }];
  ui.handler = (request) => {
    if (request.path === "/api/rules" && request.method === "PATCH") { rules[0].enabled = request.body.enabled; return response({ ok: true }); }
    if (request.path === "/api/rules" && request.method === "DELETE") { rules = []; return response({ ok: true }); }
    if (request.path === "/api/dashboard") return response(ui.dashboard(request.params, {
      rules,
      alerts: [{ id: "historical-1", rule: malicious, rate: "25.123456789", queued_at: "2026-09-30T00:00:00Z", delivered_at: "2026-09-30T00:00:00Z" }],
    }));
  };
  ui.click("catalogue-refresh"); await settle();
  ui.document.querySelector("#rules-list .switch").click(); await settle();
  assert.equal(ui.document.querySelector("#rules-list .switch").getAttribute("aria-checked"), "false");
  ui.document.querySelector("#rules-list .icon-button").click(); await settle();
  assert.ok(ui.element("add-base").textContent.includes(malicious));
  assert.ok(ui.element("alert-rows").textContent.includes(malicious));
  assert.equal(ui.element("rule-count").textContent, "0 targets");
  assert.equal(ui.element("alert-count").textContent, "1 alert");
  assert.equal(ui.document.querySelectorAll("img").length, 0);
  assert.equal(ui.window.__executed, undefined);
  ui.element("history-chart").dispatchEvent(new ui.window.KeyboardEvent("keydown", { key: "Home", bubbles: true }));
  assert.match(ui.element("chart-readout").textContent, /29/);
  ui.element("history-chart").dispatchEvent(new ui.window.KeyboardEvent("keydown", { key: "End", bubbles: true }));
  assert.match(ui.element("chart-readout").textContent, /30/);
});


test("removing the last watchlist pair clears quote context and adding a pair restores it", async (t) => {
  const ui = await workspace(t);
  for (let remaining = 3; remaining > 0; remaining -= 1) {
    assert.equal(ui.document.querySelectorAll(".watch-remove").length, remaining);
    ui.document.querySelector(".watch-remove").click(); await settle();
  }
  assert.equal(ui.element("pair-select").disabled, true);
  assert.equal(ui.element("convert-fields").disabled, true);
  assert.equal(ui.element("rule-fields").disabled, true);
  assert.equal(ui.element("quote-value").textContent, "—");
  assert.equal(ui.element("input-currency").textContent, "—");
  assert.equal(ui.element("output-currency").textContent, "—");
  assert.match(ui.element("watchlist-cards").textContent, /watchlist is empty/);
  ui.click("add-pair-toggle"); ui.submit("add-pair-form"); await settle();
  assert.equal(ui.element("pair-select").value, "SGD/THB");
  assert.equal(ui.element("convert-fields").disabled, false);
  assert.equal(ui.element("input-currency").textContent, "THB");
  assert.equal(ui.element("output-currency").textContent, "SGD");
});
