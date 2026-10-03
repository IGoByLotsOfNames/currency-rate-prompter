"use strict";
(function () {
  const $ = function (id) { return document.getElementById(id); };
  const SVG = "http://www.w3.org/2000/svg";
  const PREFS = "crp.workspace.v1";
  const state = { csrf: null, currencies: [], savedCurrencies: [], catalogue: null, refreshingCatalogue: false, savingWatchlist: false, watchlist: [], cards: [], pair: null, mode: "demo", days: 30, dashboard: null, points: [], chartIndex: -1, chartLayout: null, loading: true, ready: false, refreshing: false, loadingHistory: false, converting: false, savingRule: false, loadId: 0, convertId: 0, direction: "counter_to_base", timer: null, autoMinutes: 0, nextRefresh: null, toastTimer: null, loadError: false };
  const pairKey = function (pair) { return pair ? pair.base + "/" + pair.counter : ""; };
  const validPair = function (pair) { return pair && /^[A-Z]{3}$/.test(pair.base) && /^[A-Z]{3}$/.test(pair.counter) && pair.base !== pair.counter; };
  function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
  function setText(id, text) { $(id).textContent = text === null || text === undefined || text === "" ? "—" : String(text); }
  function prefs() { try { const value = JSON.parse(localStorage.getItem(PREFS) || "{}"); return value && typeof value === "object" ? value : {}; } catch (_) { return {}; } }
  function savePrefs() { try { localStorage.setItem(PREFS, JSON.stringify({ mode: state.mode, pair: state.pair })); } catch (_) { /* Storage may be disabled; the current session still works. */ } }
  function displayRate(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value), magnitude = Math.abs(number);
    if (!Number.isFinite(number) || (number === 0 && /[1-9]/.test(String(value)))) return exactDecimal(value);
    if (magnitude > 0 && (magnitude < 1e-14 || magnitude >= 1e9)) return number.toExponential(5);
    const leadingPlaces = magnitude > 0 && magnitude < 1 ? -Math.floor(Math.log10(magnitude)) : 0;
    return new Intl.NumberFormat(undefined, {
      minimumFractionDigits: Math.max(2, leadingPlaces + 1),
      maximumFractionDigits: Math.max(6, leadingPlaces + 5)
    }).format(number);
  }
  function exactDecimal(value) {
    const raw = String(value);
    if (!/^\d+(?:\.\d+)?$/.test(raw)) return raw;
    const parts = raw.split(".");
    return parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (parts[1] ? "." + parts[1] : "");
  }
  function dateText(value, includeTime) {
    if (!value) return "—";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "—";
    const options = { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" };
    if (includeTime) { options.hour = "2-digit"; options.minute = "2-digit"; }
    return new Intl.DateTimeFormat(undefined, options).format(date) + (includeTime ? " UTC" : "");
  }
  function changeText(value) { const number = Number(value); return value === null || value === undefined || !Number.isFinite(number) ? "—" : (number > 0 ? "+" : "") + number.toFixed(2) + "%"; }
  function changeClass(value) { return value === null || value === undefined ? "" : Number(value) < 0 ? " down" : Number(value) > 0 ? " up" : ""; }
  function currencyName(code) { const found = state.currencies.concat(state.savedCurrencies).find(function (item) { return item.code === code; }); return found ? found.name : code; }
  function toast(message, error) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").className = "toast" + (error ? " error" : "");
    $("toast").hidden = false;
    state.toastTimer = setTimeout(function () { $("toast").hidden = true; }, error ? 8500 : 5000);
  }
  function showError(message) { $("app-error-text").textContent = message; $("app-error").hidden = false; }
  async function api(path, method, body) {
    const options = { method: method || "GET", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } };
    if (options.method !== "GET") {
      if (!state.csrf) throw new Error("Reload the local workspace before making changes.");
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRF-Token"] = state.csrf;
      options.body = JSON.stringify(body || {});
    }
    let response;
    try { response = await fetch(path, options); }
    catch (_) { throw new Error("Cannot reach the local app. Check that it is still running, then try again."); }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error("The local app returned an unreadable response. Please try again."); }
    if (!response.ok) throw new Error(data && typeof data.error === "string" ? data.error : "The request could not be completed.");
    return data;
  }
  function query(extra) {
    const values = Object.assign({ base: state.pair.base, counter: state.pair.counter, mode: state.mode }, extra || {});
    return new URLSearchParams(values).toString();
  }
  function resetConversion() {
    state.convertId += 1;
    setText("conversion-value", "—");
    setText("conversion-detail", state.dashboard && state.dashboard.latest ? "Enter an amount and calculate your estimate." : "A recorded quote is needed before calculating.");
  }
  function updateContext() {
    const pair = state.pair;
    $("mode-demo").setAttribute("aria-pressed", String(state.mode === "demo"));
    $("mode-reference").setAttribute("aria-pressed", String(state.mode === "reference"));
    $("mode-badge").textContent = state.mode === "demo" ? "Synthetic data" : "Daily reference";
    $("mode-badge").className = "badge " + (state.mode === "demo" ? "synthetic" : "reference");
    $("mode-note").textContent = state.mode === "demo" ? "Synthetic demo observations. No provider is contacted." : "Daily reference rates, not bank quotes. Fetch only on request.";
    $("history-button").hidden = state.mode !== "reference";
    if (!pair) {
      setText("pair-name", "YOUR SELECTED PAIR"); setText("rate-heading", "Choose a currency pair");
      setText("quote-unit", "Rate per 1 base currency"); setText("buying-note", "Add a pair to begin following its rate.");
      setText("rule-pair", "Add and select a pair first.");
      ["input-currency", "amount-unit", "output-currency", "threshold-unit"].forEach(function (id) { setText(id, "—"); });
      document.querySelectorAll("[data-direction]").forEach(function (button) { button.textContent = button.dataset.direction === "counter_to_base" ? "Counter → base" : "Base → counter"; });
      return;
    }
    setText("pair-name", currencyName(pair.base) + " / " + currencyName(pair.counter));
    setText("rate-heading", pair.base + " / " + pair.counter);
    setText("quote-unit", pair.counter + " per 1 " + pair.base);
    setText("buying-note", pair.base === "SGD" && pair.counter === "THB" ? "THB per 1 SGD · lower means fewer baht to buy SGD." : "Lower means fewer " + pair.counter + " to buy 1 " + pair.base + ".");
    setText("rule-pair", pair.base + " / " + pair.counter + " · " + (state.mode === "demo" ? "synthetic demo" : "daily reference"));
    setText("threshold-unit", pair.counter + "/" + pair.base);
    const from = state.direction === "counter_to_base" ? pair.counter : pair.base;
    const to = state.direction === "counter_to_base" ? pair.base : pair.counter;
    setText("input-currency", from); setText("amount-unit", from); setText("output-currency", to);
    document.querySelectorAll("[data-direction]").forEach(function (button) {
      const reverse = button.dataset.direction === "counter_to_base";
      button.textContent = (reverse ? pair.counter : pair.base) + " → " + (reverse ? pair.base : pair.counter);
      button.setAttribute("aria-pressed", String(button.dataset.direction === state.direction));
    });
  }
  function updateControls() {
    $("refresh-button").disabled = !state.ready || !state.pair || state.refreshing || state.loadingHistory;
    $("refresh-button").classList.toggle("busy", state.refreshing);
    $("refresh-button").querySelector("span").textContent = state.refreshing ? "Refreshing…" : "Refresh rates";
    $("history-button").disabled = !state.ready || !state.pair || state.loadingHistory || state.refreshing;
    $("history-button").textContent = state.loadingHistory ? "Loading history…" : "Load 90-day history";
    $("export-button").disabled = !state.ready || !state.pair || !state.dashboard || !state.dashboard.points.length;
    $("convert-fields").disabled = !state.ready || !state.pair || !state.dashboard || !state.dashboard.latest || state.converting;
    $("rule-fields").disabled = !state.ready || !state.pair || state.savingRule;
    $("add-pair-toggle").disabled = !state.ready;
    $("pair-select").disabled = !state.ready || !state.watchlist.length;
    $("auto-refresh").disabled = !state.ready;
    $("watchlist-fields").disabled = !state.ready || state.savingWatchlist;
    $("catalogue-refresh").disabled = !state.ready || state.refreshingCatalogue;
    $("catalogue-refresh").textContent = state.refreshingCatalogue ? "Refreshing currency list…" : "Refresh currency list";
  }
  function renderPairs() {
    const select = $("pair-select");
    select.replaceChildren();
    if (!state.watchlist.length) select.append(el("option", "", "Add a pair"));
    state.watchlist.forEach(function (pair) {
      const option = el("option", "", pairKey(pair)); option.value = pairKey(pair); select.append(option);
    });
    if (state.pair) select.value = pairKey(state.pair);
    updateContext(); updateControls();
  }
  function catalogueItems() {
    const items = state.currencies.map(function (item) { return { code: item.code, name: item.name, available: true }; });
    state.savedCurrencies.forEach(function (item) {
      if (!items.some(function (value) { return value.code === item.code; })) items.push({ code: item.code, name: item.name, available: false });
    });
    return items.sort(function (a, b) { return a.code.localeCompare(b.code); });
  }
  function renderCurrencyOptions(id, initial) {
    const select = $(id), selected = select.value || initial;
    const search = $(id + "-search").value.trim().toLocaleLowerCase();
    const items = catalogueItems();
    if (selected && !items.some(function (item) { return item.code === selected; })) items.push({ code: selected, name: selected, available: false });
    const matches = items.filter(function (item) { return (item.code + " " + item.name).toLocaleLowerCase().includes(search); });
    const retained = selected && !matches.some(function (item) { return item.code === selected; });
    const shown = retained ? items.filter(function (item) { return item.code === selected; }).concat(matches) : matches;
    select.replaceChildren();
    shown.forEach(function (item) {
      const suffix = !item.available ? " (unavailable)" : retained && item.code === selected ? " (selected)" : "";
      const option = el("option", "", item.code + " — " + item.name + suffix); option.value = item.code; select.append(option);
    });
    if (selected) select.value = selected;
    $(id + "-count").textContent = matches.length + (matches.length === 1 ? " match" : " matches") + (retained ? " · current selection retained" : "");
  }
  function populateCurrencies() {
    renderCurrencyOptions("add-base", "SGD");
    renderCurrencyOptions("add-counter", "THB");
  }
  function applyCatalogue(data) {
    if (!Array.isArray(data.currencies) || !data.currencies.length || data.currencies.some(function (item) { return !item || !/^[A-Z]{3}$/.test(item.code) || typeof item.name !== "string"; })) throw new Error("The currency list could not be read. Your previous list is still available.");
    state.currencies = data.currencies;
    state.savedCurrencies = Array.isArray(data.saved_currencies) ? data.saved_currencies : [];
    state.catalogue = data.catalogue || {};
    const origin = state.catalogue.source === "cached" ? "saved provider list" : "bundled provider list";
    const asOf = state.catalogue.as_of ? " · as of " + dateText(state.catalogue.as_of, false) : "";
    $("catalogue-status").textContent = state.currencies.length + " active currencies · " + origin + asOf;
    $("catalogue-note").textContent = "Provider coverage: pair availability and history may vary. Refresh the list only when you choose.";
    populateCurrencies();
  }
  async function refreshCatalogue() {
    if (!state.ready || state.refreshingCatalogue) return;
    state.refreshingCatalogue = true; updateControls();
    try {
      const data = await api("/api/currencies/refresh", "POST", {});
      applyCatalogue(data);
      await reloadSettings();
      toast("Currency list refreshed. Your selected pair and form choices are preserved.");
    } catch (error) { toast(error.message, true); }
    finally { state.refreshingCatalogue = false; updateControls(); }
  }
  function renderWatchlist() {
    const container = $("watchlist-cards"); container.replaceChildren();
    if (!state.watchlist.length) { container.append(el("div", "panel empty-card", "Your watchlist is empty. Add your first currency pair.")); return; }
    state.watchlist.forEach(function (pair) {
      const data = state.cards.find(function (card) { return pairKey(card) === pairKey(pair); }) || {};
      const selected = pairKey(pair) === pairKey(state.pair);
      const card = el("article", "panel watch-card" + (selected ? " selected" : ""));
      const button = el("button", "watch-select"); button.type = "button"; button.setAttribute("aria-label", "Select " + pairKey(pair)); if (selected) button.setAttribute("aria-current", "true");
      button.append(el("div", "watch-pair", pairKey(pair)), el("div", "watch-source", state.mode === "demo" ? "Synthetic demo" : "Daily reference"));
      const number = el("div", "watch-number", displayRate(data.rate)); number.append(el("span", "watch-unit", pair.counter + " per " + pair.base)); button.append(number);
      const bottom = el("div", "watch-bottom"); bottom.append(el("span", "change-value" + changeClass(data.change_percent), changeText(data.change_percent)), el("span", "", data.observed_at ? dateText(data.observed_at, false) : state.loading ? "Loading…" : "No recorded quote")); button.append(bottom);
      button.addEventListener("click", function () { selectPair(pair); });
      const remove = el("button", "icon-button watch-remove", "×"); remove.type = "button"; remove.setAttribute("aria-label", "Remove " + pairKey(pair) + " from watchlist"); remove.addEventListener("click", function () { removePair(pair, remove); });
      card.append(button, remove); container.append(card);
    });
  }
  function svgNode(tag, attributes, text) { const node = document.createElementNS(SVG, tag); Object.keys(attributes || {}).forEach(function (key) { node.setAttribute(key, String(attributes[key])); }); if (text !== undefined) node.textContent = text; return node; }
  function selectChartPoint(index, announce) {
    if (!state.points.length || !state.chartLayout) return;
    state.chartIndex = Math.max(0, Math.min(state.points.length - 1, index));
    const point = state.points[state.chartIndex], geometry = state.chartLayout;
    const x = geometry.x(point), y = geometry.y(Number(point.rate));
    const marker = $("chart-marker"), cursor = $("chart-cursor");
    if (marker) { marker.setAttribute("cx", x); marker.setAttribute("cy", y); }
    if (cursor) { cursor.setAttribute("x1", x); cursor.setAttribute("x2", x); }
    const description = dateText(point.observed_at, false) + " · " + displayRate(point.rate) + " " + state.pair.counter + " per " + state.pair.base;
    $("chart-readout").textContent = description;
    if (announce) $("history-chart").setAttribute("aria-label", "Rate history. " + description + ". Use left and right arrows to inspect observations.");
  }
  function renderChart(points) {
    const chart = $("history-chart"); chart.replaceChildren(); state.chartLayout = null;
    state.points = points.filter(function (point) { return Number.isFinite(Number(point.rate)) && Number.isFinite(new Date(point.observed_at).getTime()); }).slice().sort(function (a, b) { return new Date(a.observed_at) - new Date(b.observed_at); });
    const empty = $("chart-empty"); empty.hidden = state.points.length > 0;
    if (!state.points.length) {
      const title = state.loading ? "Loading your history" : state.loadError ? "History is unavailable" : state.mode === "reference" ? "Start with a reference quote" : "No observations yet";
      const detail = state.loading ? "Reading this workspace’s recorded observations." : state.loadError ? "Try again when the local app is available." : state.mode === "reference" ? "Refresh rates for the latest quote, or load 90-day history. Both are explicit requests." : "Refresh the synthetic demo or choose another pair.";
      empty.querySelector("strong").textContent = title; empty.querySelector("p").textContent = detail;
      chart.setAttribute("aria-label", title + ". " + detail); setText("chart-readout", "No observation selected."); return;
    }
    const left = 54, right = 705, top = 15, bottom = 198;
    const values = state.points.map(function (point) { return Number(point.rate); });
    const min = Math.min.apply(null, values), max = Math.max.apply(null, values);
    const padding = max === min ? (Math.abs(min) || 1) * .006 : (max - min) * .16;
    const low = min - padding, high = max + padding;
    const firstTime = new Date(state.points[0].observed_at).getTime(), lastTime = new Date(state.points[state.points.length - 1].observed_at).getTime();
    const x = function (point) { return firstTime === lastTime ? (left + right) / 2 : left + (new Date(point.observed_at).getTime() - firstTime) / (lastTime - firstTime) * (right - left); };
    const y = function (value) { return bottom - (value - low) / (high - low) * (bottom - top); };
    state.chartLayout = { x: x, y: y, left: left, right: right, top: top, bottom: bottom };
    for (let i = 0; i < 4; i += 1) {
      const value = low + (high - low) * i / 3, position = y(value);
      chart.append(svgNode("line", { x1: left, x2: right, y1: position, y2: position, class: "chart-grid" }), svgNode("text", { x: left - 10, y: position + 3, "text-anchor": "end", class: "chart-axis" }, displayRate(value)));
    }
    const coords = state.points.map(function (point) { return [x(point), y(Number(point.rate))]; });
    const line = coords.map(function (point, index) { return (index ? "L" : "M") + point[0].toFixed(2) + "," + point[1].toFixed(2); }).join(" ");
    if (coords.length > 1) chart.append(svgNode("path", { d: line + " L" + coords[coords.length - 1][0] + "," + bottom + " L" + coords[0][0] + "," + bottom + " Z", class: "chart-area" }));
    chart.append(svgNode("path", { d: line, class: "chart-line" }));
    const ticks = Array.from(new Set([0, Math.floor((state.points.length - 1) / 2), state.points.length - 1]));
    ticks.forEach(function (index) {
      const point = state.points[index], text = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", timeZone: "UTC" }).format(new Date(point.observed_at));
      chart.append(svgNode("text", { x: x(point), y: 224, "text-anchor": index === 0 ? "start" : index === state.points.length - 1 ? "end" : "middle", class: "chart-axis" }, text));
    });
    chart.append(svgNode("line", { id: "chart-cursor", x1: 0, x2: 0, y1: top, y2: bottom, class: "chart-cursor" }), svgNode("circle", { id: "chart-marker", cx: 0, cy: 0, r: 5, class: "chart-dot" }));
    selectChartPoint(state.points.length - 1, true);
  }
  function renderHistoryTable() {
    const body = $("history-rows"); body.replaceChildren();
    setText("history-count", state.points.length ? state.points.length + " observations" : "");
    if (!state.points.length) { const row = el("tr"); const cell = el("td", "table-empty", state.loading ? "Loading observations…" : "No observations for this period."); cell.colSpan = 2; row.append(cell); body.append(row); return; }
    state.points.forEach(function (point) { const row = el("tr"); row.append(el("td", "", dateText(point.observed_at, true)), el("td", "", String(point.rate) + " " + state.pair.counter + " per " + state.pair.base)); body.append(row); });
  }
  function renderRules() {
    const rules = state.dashboard && Array.isArray(state.dashboard.rules) ? state.dashboard.rules : [];
    const list = $("rules-list"); list.replaceChildren(); setText("rule-count", rules.length + (rules.length === 1 ? " target" : " targets"));
    if (!rules.length) {
      const empty = el("div", "empty-state"); empty.append(el("span", "empty-symbol", "⌁"), el("strong", "", state.loading ? "Loading targets" : "No targets yet"), el("p", "", state.loading ? "Reading your local rules." : "Create a target for this pair and data source.")); list.append(empty); return;
    }
    rules.forEach(function (rule) {
      const row = el("div", "rule-item" + (rule.enabled ? "" : " disabled"));
      const info = el("div", "rule-info");
      info.append(el("div", "rule-name", rule.name));
      const hours = Number(rule.cooldown_seconds) / 3600;
      info.append(el("div", "rule-description", (rule.direction === "at_or_below" ? "At or below " : "At or above ") + String(rule.threshold) + " " + rule.counter + " per " + rule.base + " · " + hours + "h cooldown"));
      const controls = el("div", "rule-controls");
      const toggle = el("button", "switch"); toggle.type = "button"; toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-checked", String(Boolean(rule.enabled))); toggle.setAttribute("aria-label", (rule.enabled ? "Disable " : "Enable ") + rule.name);
      toggle.addEventListener("click", function () { mutateRule("PATCH", { id: rule.id, enabled: !rule.enabled }, toggle, rule.enabled ? "Target paused." : "Target enabled."); });
      const remove = el("button", "icon-button", "×"); remove.type = "button"; remove.setAttribute("aria-label", "Delete target " + rule.name);
      remove.addEventListener("click", function () { mutateRule("DELETE", { id: rule.id }, remove, "Target removed. Earlier alerts remain in the journal."); });
      controls.append(toggle, remove); row.append(info, controls); list.append(row);
    });
  }
  function renderAlerts() {
    const alerts = state.dashboard && Array.isArray(state.dashboard.alerts) ? state.dashboard.alerts : [];
    const body = $("alert-rows"); body.replaceChildren(); setText("alert-count", alerts.length + (alerts.length === 1 ? " alert" : " alerts"));
    if (!alerts.length) { const row = el("tr"), cell = el("td", "table-empty", state.loading ? "Loading local alerts…" : "No local alerts recorded for this pair and source."); cell.colSpan = 4; row.append(cell); body.append(row); return; }
    alerts.forEach(function (alert) {
      const row = el("tr"); row.dataset.eventId = String(alert.id);
      const name = typeof alert.rule === "string" ? alert.rule : alert.rule && alert.rule.name ? alert.rule.name : "Earlier target";
      const status = el("td"); status.append(el("span", "journal-state" + (alert.delivered_at ? "" : " pending"), alert.delivered_at ? "Recorded locally" : "Queued locally"));
      row.append(el("td", "", name), el("td", "", displayRate(alert.rate) + " " + state.pair.counter), el("td", "", dateText(alert.queued_at, true)), status); body.append(row);
    });
  }
  function renderDashboard() {
    updateContext();
    const data = state.dashboard, latest = data && data.latest;
    setText("quote-value", latest ? displayRate(latest.rate) : "—");
    $("quote-value").title = latest ? String(latest.rate) : "";
    setText("quote-change", data ? changeText(data.change_percent) : "—");
    $("quote-change").className = "change-value" + changeClass(data ? data.change_percent : null);
    setText("observed-date", latest ? dateText(latest.observed_at, false) : "—");
    setText("quote-source", data ? data.source : state.mode === "demo" ? "Synthetic demo" : "Daily reference");
    setText("range-low", data ? displayRate(data.low) : "—"); setText("range-high", data ? displayRate(data.high) : "—");
    setText("last-refresh", data && data.last_refresh ? dateText(data.last_refresh, true) : "Not refreshed");
    $("freshness-badge").textContent = state.loading ? "Loading" : !latest ? "No quote yet" : state.mode === "demo" ? "Synthetic" : data.stale ? "Older observation" : "Reference observation";
    $("freshness-badge").className = "badge " + (state.mode === "demo" && latest ? "synthetic" : latest && data.stale ? "stale" : "neutral");
    $("dashboard-note").textContent = data && data.note ? data.note : state.mode === "demo" ? "Synthetic observations illustrate the tracker; they are not market history." : "Daily observations may be unchanged over weekends and holidays.";
    renderChart(data && Array.isArray(data.points) ? data.points : []); renderHistoryTable(); renderRules(); renderAlerts(); updateControls();
  }
  async function loadData() {
    const id = ++state.loadId;
    state.loading = true; state.loadError = false; state.dashboard = null; state.cards = [];
    renderDashboard(); renderWatchlist(); resetConversion();
    if (!state.pair) { state.loading = false; renderDashboard(); renderWatchlist(); return; }
    const dashboardPath = "/api/dashboard?" + query({ days: state.days });
    const watchlistPath = "/api/watchlist?" + new URLSearchParams({ mode: state.mode });
    try {
      const values = await Promise.all([api(dashboardPath), api(watchlistPath)]);
      if (id !== state.loadId) return;
      state.dashboard = values[0]; state.cards = Array.isArray(values[1].watchlist) ? values[1].watchlist : [];
      $("app-error").hidden = true;
    } catch (error) { if (id !== state.loadId) return; state.loadError = true; showError(error.message); }
    finally { if (id === state.loadId) { state.loading = false; renderDashboard(); renderWatchlist(); } }
  }
  function selectPair(pair) {
    if (pairKey(pair) === pairKey(state.pair)) return;
    state.pair = { base: pair.base, counter: pair.counter };
    state.direction = "counter_to_base"; $("rule-form").reset(); savePrefs(); renderPairs(); loadData(); armSchedule();
  }
  async function bootstrap() {
    $("app-error").hidden = true; state.ready = false; updateControls();
    try {
      const data = await api("/api/app");
      if (!data || typeof data.csrf_token !== "string" || !Array.isArray(data.currencies) || !Array.isArray(data.watchlist)) throw new Error("The local workspace configuration is incomplete.");
      applyCatalogue(data); state.csrf = data.csrf_token; state.watchlist = data.watchlist.filter(validPair);
      const saved = prefs(); state.mode = saved.mode === "reference" || saved.mode === "demo" ? saved.mode : data.mode === "reference" ? "reference" : "demo";
      state.pair = state.watchlist.find(function (pair) { return pairKey(pair) === pairKey(saved.pair); }) || state.watchlist[0] || null;
      state.ready = true; populateCurrencies(); renderPairs(); await loadData();
    } catch (error) { state.loading = false; state.loadError = true; renderDashboard(); showError(error.message); }
  }
  async function refreshRates(automatic) {
    if (!state.pair || state.refreshing || state.loadingHistory) return;
    const pair = Object.assign({}, state.pair), mode = state.mode;
    state.refreshing = true; updateControls();
    try {
      await api("/api/refresh", "POST", { base: pair.base, counter: pair.counter, mode: mode });
      if (pairKey(pair) === pairKey(state.pair) && mode === state.mode) await loadData();
      toast(mode === "demo" ? "Synthetic demo refreshed. No provider was contacted." : "Reference request completed. Daily rates may be unchanged.");
    } catch (error) {
      // A saved observation may survive a later journal failure. Read current local state only.
      if (pairKey(pair) === pairKey(state.pair) && mode === state.mode) await loadData();
      toast((automatic ? "Scheduled refresh: " : "") + error.message, true);
    }
    finally { state.refreshing = false; updateControls(); armSchedule(); }
  }
  async function loadReferenceHistory() {
    if (!state.pair || state.mode !== "reference" || state.loadingHistory || state.refreshing) return;
    const pair = Object.assign({}, state.pair);
    state.loadingHistory = true; updateControls();
    try {
      await api("/api/history", "POST", pair);
      if (pairKey(pair) === pairKey(state.pair) && state.mode === "reference") { state.days = 90; updateRange(); await loadData(); }
      toast("Reference history loaded. No retrospective alerts were created.");
    } catch (error) {
      // Imports commit per observation; an error may follow partial progress.
      if (pairKey(pair) === pairKey(state.pair) && state.mode === "reference") await loadData();
      toast(error.message, true);
    }
    finally { state.loadingHistory = false; updateControls(); armSchedule(); }
  }
  function armSchedule() {
    clearTimeout(state.timer); state.timer = null; state.nextRefresh = null;
    if (!state.autoMinutes) { setText("schedule-note", "Automatic refresh is off."); return; }
    if (document.hidden) { setText("schedule-note", "Auto-refresh paused while this tab is hidden."); return; }
    if (!state.pair) { setText("schedule-note", "Add a pair to use automatic refresh."); return; }
    state.nextRefresh = Date.now() + state.autoMinutes * 60000;
    const at = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(state.nextRefresh));
    setText("schedule-note", "Selected pair only · next request at " + at + ".");
    state.timer = setTimeout(function () { if (!document.hidden) refreshRates(true); else armSchedule(); }, state.autoMinutes * 60000);
  }
  async function reloadSettings() {
    const data = await api("/api/app"); applyCatalogue(data); state.csrf = data.csrf_token; state.watchlist = data.watchlist.filter(validPair);
    if (!state.watchlist.some(function (pair) { return pairKey(pair) === pairKey(state.pair); })) state.pair = state.watchlist[0] || null;
    savePrefs(); renderPairs(); armSchedule(); await loadData();
  }
  async function removePair(pair, button) {
    button.disabled = true;
    try { await api("/api/watchlist", "DELETE", pair); await reloadSettings(); toast(pairKey(pair) + " removed from the watchlist."); }
    catch (error) { toast(error.message, true); button.disabled = false; }
  }
  async function mutateRule(method, body, button, message) {
    button.disabled = true;
    try { await api("/api/rules", method, body); await loadData(); toast(message); }
    catch (error) { toast(error.message, true); button.disabled = false; }
  }
  function positiveDecimal(value) { return /^\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)) && Number(value) > 0; }
  async function convert(event) {
    event.preventDefault(); if (!state.pair || state.converting) return;
    const amount = $("convert-amount").value.trim(), fee = $("convert-fee").value.trim() || "0";
    if (!positiveDecimal(amount)) { toast("Enter a positive amount using digits and a decimal point.", true); $("convert-amount").focus(); return; }
    if (!/^\d+(?:\.\d+)?$/.test(fee) || Number(fee) > 100) { toast("Enter a fee from 0 to 100 percent.", true); $("convert-fee").focus(); return; }
    const id = ++state.convertId; const payload = { base: state.pair.base, counter: state.pair.counter, mode: state.mode, amount: amount, direction: state.direction, fee_percent: fee };
    state.converting = true; updateControls(); $("convert-button").textContent = "Calculating…";
    try {
      const data = await api("/api/convert", "POST", payload);
      if (id !== state.convertId) return;
      setText("conversion-value", exactDecimal(data.result));
      const target = data.direction === "counter_to_base" ? data.base : data.counter;
      setText("output-currency", target);
      setText("conversion-detail", "At " + String(data.rate) + " " + data.counter + " per " + data.base + " · fee estimate " + String(data.fee_percent) + "% · observed " + dateText(data.observed_at, false) + (data.mode === "demo" ? " · synthetic" : ""));
    } catch (error) { if (id === state.convertId) { setText("conversion-value", "—"); setText("conversion-detail", "The estimate could not be calculated."); toast(error.message, true); } }
    finally { state.converting = false; $("convert-button").textContent = "Calculate conversion →"; updateControls(); }
  }
  function updateRange() { document.querySelectorAll("[data-days]").forEach(function (button) { button.setAttribute("aria-pressed", String(Number(button.dataset.days) === state.days)); }); }
  $("catalogue-refresh").addEventListener("click", refreshCatalogue);
  ["add-base", "add-counter"].forEach(function (id) {
    $(id + "-search").addEventListener("input", function () { renderCurrencyOptions(id); });
    $(id).addEventListener("change", function () { renderCurrencyOptions(id); });
  });
  $("refresh-button").addEventListener("click", function () { refreshRates(false); });
  $("history-button").addEventListener("click", loadReferenceHistory);
  $("retry-button").addEventListener("click", function () { if (state.ready) loadData(); else bootstrap(); });
  ["demo", "reference"].forEach(function (mode) {
    $("mode-" + mode).addEventListener("click", function () { if (state.mode === mode) return; state.mode = mode; $("rule-form").reset(); savePrefs(); updateContext(); loadData(); armSchedule(); });
  });
  $("pair-select").addEventListener("change", function () { const pair = state.watchlist.find(function (item) { return pairKey(item) === $("pair-select").value; }); if (pair) selectPair(pair); });
  document.querySelectorAll("[data-days]").forEach(function (button) { button.addEventListener("click", function () { state.days = Number(button.dataset.days); updateRange(); loadData(); }); });
  document.querySelectorAll("[data-direction]").forEach(function (button) { button.addEventListener("click", function () { state.direction = button.dataset.direction; updateContext(); resetConversion(); }); });
  $("convert-form").addEventListener("submit", convert);
  ["convert-amount", "convert-fee"].forEach(function (id) { $(id).addEventListener("input", resetConversion); });
  $("auto-refresh").addEventListener("change", function () { const minutes = Number($("auto-refresh").value); state.autoMinutes = [0, 15, 60].includes(minutes) ? minutes : 0; armSchedule(); });
  document.addEventListener("visibilitychange", armSchedule);
  $("add-pair-toggle").addEventListener("click", function () { const show = $("add-pair-form").hidden; $("add-pair-form").hidden = !show; $("add-pair-toggle").setAttribute("aria-expanded", String(show)); if (show) $("add-base").focus(); });
  $("cancel-pair").addEventListener("click", function () { $("add-pair-form").hidden = true; $("add-pair-toggle").setAttribute("aria-expanded", "false"); $("add-pair-toggle").focus(); });
  $("add-pair-form").addEventListener("submit", async function (event) {
    event.preventDefault(); if (state.savingWatchlist) return;
    const pair = { base: $("add-base").value, counter: $("add-counter").value };
    if (!validPair(pair)) { toast("Choose two different currencies.", true); return; }
    state.savingWatchlist = true; updateControls();
    try { await api("/api/watchlist", "POST", pair); state.pair = pair; await reloadSettings(); $("add-pair-form").hidden = true; $("add-pair-toggle").setAttribute("aria-expanded", "false"); toast(pairKey(pair) + " added to your watchlist."); }
    catch (error) { toast(error.message, true); }
    finally { state.savingWatchlist = false; updateControls(); }
  });
  $("rule-form").addEventListener("submit", async function (event) {
    event.preventDefault(); if (!state.pair || state.savingRule) return;
    const name = $("rule-name").value.trim(), threshold = $("rule-threshold").value.trim(), seconds = Number($("rule-cooldown").value) * 3600;
    if (!name || !positiveDecimal(threshold) || !Number.isSafeInteger(seconds) || seconds < 0 || seconds > 31536000) { toast("Add a target name, a positive rate and a valid cooldown.", true); return; }
    const payload = { name: name, base: state.pair.base, counter: state.pair.counter, mode: state.mode, direction: $("rule-direction").value, threshold: threshold, cooldown_seconds: seconds };
    state.savingRule = true; updateControls();
    try { await api("/api/rules", "POST", payload); if (payload.base === state.pair?.base && payload.counter === state.pair?.counter && payload.mode === state.mode) $("rule-form").reset(); await loadData(); toast("Target created. New qualifying observations can record local alerts."); }
    catch (error) { toast(error.message, true); }
    finally { state.savingRule = false; updateControls(); }
  });
  $("export-button").addEventListener("click", function () {
    if (!state.pair) return;
    const anchor = document.createElement("a"); anchor.href = "/api/export?" + query(); anchor.download = state.pair.base + "-" + state.pair.counter + "-" + state.mode + ".csv"; document.body.append(anchor); anchor.click(); anchor.remove();
  });
  $("history-chart").addEventListener("pointermove", function (event) {
    if (!state.chartLayout || !state.points.length) return;
    const chart = $("history-chart"), point = chart.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
    const matrix = chart.getScreenCTM(); if (!matrix) return;
    const x = point.matrixTransform(matrix.inverse()).x;
    let nearest = 0, distance = Infinity;
    state.points.forEach(function (value, index) { const difference = Math.abs(state.chartLayout.x(value) - x); if (difference < distance) { nearest = index; distance = difference; } });
    selectChartPoint(nearest, false);
  });
  $("history-chart").addEventListener("keydown", function (event) {
    if (!state.points.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    selectChartPoint(event.key === "Home" ? 0 : event.key === "End" ? state.points.length - 1 : state.chartIndex + (event.key === "ArrowLeft" ? -1 : 1), true);
  });
  document.querySelectorAll(".nav-item").forEach(function (link) { link.addEventListener("click", function () { document.querySelectorAll(".nav-item").forEach(function (item) { item.classList.toggle("active", item === link); if (item === link) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current"); }); }); });
  bootstrap();
}());
