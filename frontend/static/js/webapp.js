"use strict";
// Telegram Mini App. Auth = Telegram's signed initData, verified server-side.
const tg = window.Telegram?.WebApp;
const CUR = document.body.dataset.currency || "UZS";
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  view: "overview",
  period: "day",
  anchor: startOfDay(new Date()), // any date inside the selected period
  tx: { page: 1, items: [], total: 0 },
  loadId: 0,
};

if (tg) {
  tg.ready();
  tg.expand();
  document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
  tg.setHeaderColor?.("secondary_bg_color");
  tg.setBackgroundColor?.("secondary_bg_color");
}
const haptic = () => tg?.HapticFeedback?.selectionChanged();

// ---------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "style") n.style.cssText = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
// Display formatting only — money math is exact on the server.
const fmt = (v) => Math.round(Math.abs(Number(v || 0))).toLocaleString("ru-RU");
const unit = (cur) => (cur === "UZS" ? "so'm" : cur);
const money = (v, cur = CUR, sign = "") => `${sign}${fmt(v)} ${unit(cur)}`;

const ICONS = {
  food: "🍔", transport: "🚕", shopping: "🛍️", housing: "🏠", "bills and utilities": "💡", entertainment: "🎬",
  health: "💊", education: "📚", transfers: "🔁", income: "💰", refunds: "↩️", cash: "🏧", other: "📦",
  restaurants: "🍽️", "fast food": "🍔", cafes: "☕", "food delivery": "🛵", taxi: "🚕", fuel: "⛽",
  "public transport": "🚌", groceries: "🛒", electronics: "📱", clothing: "👕", mobile: "📶", internet: "🌐",
  utilities: "💡", salary: "💼", p2p: "🔁", "own cards": "💳", "transfers received": "📥", "card top-up": "📥",
  cashback: "🎁", "atm withdrawal": "🏧", unclassified: "❔",
};
const icon = (name) => ICONS[(name || "").toLowerCase()] || "🏷️";

// ---------------------------------------------------------------- dates
function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
function ymd(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
function parseYmd(s) { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d || 1); }

function range() {
  const a = state.anchor;
  if (state.period === "day") return [a, a];
  if (state.period === "week") {
    const monday = addDays(a, -((a.getDay() + 6) % 7));
    return [monday, addDays(monday, 6)];
  }
  if (state.period === "month") return [new Date(a.getFullYear(), a.getMonth(), 1), new Date(a.getFullYear(), a.getMonth() + 1, 0)];
  return [new Date(a.getFullYear(), 0, 1), new Date(a.getFullYear(), 11, 31)];
}

function shift(dir) {
  const a = state.anchor;
  if (state.period === "day") state.anchor = addDays(a, dir);
  else if (state.period === "week") state.anchor = addDays(a, 7 * dir);
  else if (state.period === "month") state.anchor = new Date(a.getFullYear(), a.getMonth() + dir, 1);
  else state.anchor = new Date(a.getFullYear() + dir, 0, 1);
}

function rangeLabel() {
  const [s, e] = range();
  const today = startOfDay(new Date());
  const dm = { day: "numeric", month: "short" };
  if (state.period === "day") {
    if (+s === +today) return "Today";
    if (+s === +addDays(today, -1)) return "Yesterday";
    return s.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "long", year: s.getFullYear() !== today.getFullYear() ? "numeric" : undefined });
  }
  if (state.period === "week") {
    if (today >= s && today <= e) return "This week";
    return `${s.toLocaleDateString("en-GB", dm)} – ${e.toLocaleDateString("en-GB", dm)}`;
  }
  if (state.period === "month") return s.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  return String(s.getFullYear());
}

function rangeQuery() {
  const [s, e] = range();
  return `period=custom&start=${ymd(s)}&end=${ymd(e)}`;
}

function renderDatebar() {
  $("#date-label").textContent = rangeLabel();
  const [, e] = range();
  $("#next").disabled = e >= startOfDay(new Date());
}

// ---------------------------------------------------------------- api
async function api(path) {
  const res = await fetch(path, { headers: { "X-Telegram-Init-Data": tg?.initData || "" } });
  if (res.status === 401) throw new Error("locked");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
function showError(e) {
  const box = $("#error");
  if (!e) { box.hidden = true; return; }
  box.hidden = false;
  box.textContent = e.message === "locked"
    ? "🔒 Open the bot, send /start and enter your access code. Then open this page again."
    : `⚠️ Couldn't load data (${e.message}). Close and open again.`;
}

// ---------------------------------------------------------------- caching
// Balance never depends on the period: load it once per app open. Period data is cached briefly and the
// other periods are prefetched, so switching Day/Week/Month/Year is instant.
const cache = new Map();
const CACHE_MS = 60_000;
function cached(url) {
  const hit = cache.get(url);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.promise;
  const promise = api(url).catch((e) => { cache.delete(url); throw e; });
  cache.set(url, { at: Date.now(), promise });
  return promise;
}
let cardsPromise = null;
const loadCards = () => (cardsPromise ||= api("/api/cards").catch((e) => { cardsPromise = null; throw e; }));

function prefetchOtherPeriods() {
  const keep = { period: state.period, anchor: state.anchor };
  for (const p of ["day", "week", "month", "year"]) {
    if (p === keep.period) continue;
    state.period = p;
    state.anchor = keep.anchor;
    cached(`/api/app/overview?${rangeQuery()}`).catch(() => {});
  }
  Object.assign(state, keep);
}

// ---------------------------------------------------------------- overview
function renderBalance(cards) {
  const totals = {};
  for (const c of cards) if (c.balance != null) totals[c.currency] = (totals[c.currency] || 0) + Number(c.balance);
  const cur = CUR in totals ? CUR : Object.keys(totals)[0];
  $("#balance").replaceChildren(cur ? fmt(totals[cur]) : "—", el("small", {}, unit(cur || CUR)));
  $("#cards").replaceChildren(...cards.map((c) =>
    el("span", { class: "chip" }, `•• ${c.label.split("*").pop()} `, el("b", {}, c.balance != null ? fmt(c.balance) : "—"))));
}

async function loadOverview(id) {
  loadCards().then(renderBalance).catch(() => {});
  const data = await cached(`/api/app/overview?${rangeQuery()}`);
  if (id !== state.loadId) return;
  $("#income").textContent = money(data.income);
  $("#spent").textContent = money(data.spent);
  renderCategories(data.categories);
  prefetchOtherPeriods();
}

function renderCategories(list) {
  const total = list.reduce((a, c) => a + Number(c.total), 0) || 1;
  const rows = list.map((c) => {
    const pct = Number(c.total) / total;
    const subs = c.subs || [];
    const li = el("li", { class: "cat" });
    const subList = subs.length > 1 || (subs.length === 1 && subs[0].subcategory !== "Unclassified")
      ? el("ul", { class: "subs", hidden: true }, subs.flatMap((s) => [
          el("li", {}, el("span", {}, `${icon(s.subcategory)} ${s.subcategory}`), el("span", {}, money(s.total))),
          ...(s.items || []).map((it) => el("li", { class: "item-row" },
            el("span", {}, `${it.item}${it.count > 1 ? ` ×${it.count}` : ""}`), el("span", {}, money(it.total)))),
        ]))
      : null;
    li.append(el("button", {
      class: "cat-row",
      onclick: () => { if (!subList) return; subList.hidden = !subList.hidden; li.classList.toggle("open", !subList.hidden); haptic(); },
    },
      el("span", { class: "icon" }, icon(c.name)),
      el("span", { class: "cat-mid" },
        el("div", { class: "cat-name" }, c.name, subList ? el("span", { class: "chev" }, "›") : null),
        el("div", { class: "cat-sub" }, `${c.count} ${c.count === 1 ? "payment" : "payments"}`)),
      el("span", { class: "cat-right" }, el("div", { class: "cat-amt" }, money(c.total)), el("div", { class: "cat-pct" }, `${Math.round(pct * 100)}%`)),
      el("span", { class: "bar" }, el("i", { style: `width:${Math.max(3, pct * 100)}%` }))));
    if (subList) li.append(subList);
    return li;
  });
  $("#cats").replaceChildren(...(rows.length ? rows : [el("li", { class: "empty" }, "No spending 🎉")]));
}

// ---------------------------------------------------------------- transactions
const TYPE = { expense: "Expense", income: "Income", transfer: "Transfer", refund: "Refund", cash_withdrawal: "Cash", other: "Other" };

async function loadTransactions(id, append = false) {
  const page = append ? state.tx.page + 1 : 1;
  const data = await cached(`/api/transactions?${rangeQuery()}&page=${page}&page_size=100`);
  if (id !== state.loadId) return;
  state.tx = { page, items: append ? state.tx.items.concat(data.items) : data.items, total: data.total };
  renderTransactions();
}

function txLine(x) {
  const isIn = x.direction === "in";
  const counted = x.type === "expense" || x.type === "income" || x.type === "refund";
  const label = x.item || (x.subcategory && x.subcategory !== "Unclassified" ? x.subcategory : x.category || "Other");
  const time = new Date(x.datetime).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const li = el("div", { class: "tx" });
  const detail = el("div", { class: "tx-detail", hidden: true },
    el("div", {}, el("span", {}, "Type"), el("span", {}, TYPE[x.type] || x.type)),
    el("div", {}, el("span", {}, "Category"), el("span", {}, [x.category, x.subcategory, x.item].filter(Boolean).join(" › ") || "—")),
    x.description ? el("div", {}, el("span", {}, "Note"), el("span", {}, x.description)) : null,
    ...(x.splits || []).map((p) => el("div", {},
      el("span", {}, p.kind === "debt_repay" ? "Debt repaid" : p.kind === "debt_new" ? "Loan"
        : [p.category, p.subcategory, p.item].filter(Boolean).join(" › ") || "Part"),
      el("span", {}, `${p.estimated ? "≈" : ""}${money(p.amount, x.currency)}`))),
    el("div", {}, el("span", {}, "Card"), el("span", {}, x.card || "Cash / manual")),
    el("div", {}, el("span", {}, "Time"), el("span", {}, new Date(x.datetime).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))),
    !counted ? el("div", {}, el("span", {}, "Counted"), el("span", {}, "No — transfer")) : null,
    x.needs_review ? el("div", {}, el("span", {}, "Status"), el("span", { class: "flag" }, "Needs check")) : null);
  li.append(
    el("button", { class: "tx-row", onclick: () => { detail.hidden = !detail.hidden; haptic(); } },
      el("span", { class: "icon" }, icon(label)),
      el("span", { style: "min-width:0" },
        el("div", { class: "tx-t" }, x.merchant || x.description || label),
        el("div", { class: "tx-s" }, `${time} · ${label}`, x.needs_review ? el("span", { class: "flag" }, " · check") : null)),
      el("span", { class: `tx-a ${!counted ? "muted" : isIn ? "in" : ""}` }, money(x.amount, x.currency, isIn ? "+" : "−"))),
    detail);
  return li;
}

function renderTransactions() {
  const { items, total } = state.tx;
  const box = $("#tx-list");
  $("#tx-summary").textContent = total ? `${total} transaction${total === 1 ? "" : "s"}` : "";
  if (!items.length) {
    box.replaceChildren(el("div", { class: "day-group" }, el("p", { class: "empty" }, "No transactions")));
    $("#tx-more").hidden = true;
    return;
  }
  const groups = new Map();
  for (const x of items) {
    const k = ymd(new Date(x.datetime));
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(x);
  }
  const today = startOfDay(new Date());
  box.replaceChildren(...[...groups].map(([k, list]) => {
    const d = parseYmd(k);
    const title = +d === +today ? "Today" : +d === +addDays(today, -1) ? "Yesterday"
      : d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
    const spent = list.filter((x) => x.type === "expense").reduce((a, x) => a + Number(x.amount), 0);
    return el("section", { class: "day-group" },
      el("div", { class: "day-head" }, el("span", {}, title), el("span", {}, spent ? money(spent, CUR, "−") : "")),
      list.map(txLine));
  }));
  $("#tx-more").hidden = items.length >= total;
}

// ---------------------------------------------------------------- debts
async function loadDebts(id) {
  const d = await api("/api/debts");
  if (id !== state.loadId) return;
  $("#d-owed").textContent = money(d.totals.owed_to_me, d.currency);
  $("#d-owe").textContent = money(d.totals.i_owe, d.currency);
  const net = Number(d.totals.net);
  $("#d-net").textContent = net > 0 ? `Overall others owe you ${money(net, d.currency)}`
    : net < 0 ? `Overall you owe ${money(net, d.currency)}` : d.people.length ? "Overall you are even" : "";
  const rows = d.people.map((p) => {
    const n = Number(p.net);
    const li = el("li", { class: "person" });
    const hist = el("ul", { class: "history", hidden: true });
    const sub = n > 0 ? "owes you" : n < 0 ? "you owe" : "settled";
    li.append(
      el("button", { class: "person-row", onclick: () => toggleHistory(p.id, hist, p.name) },
        el("span", { class: "avatar" }, (p.name || "?").slice(0, 1).toUpperCase()),
        el("span", {}, el("div", { class: "p-name" }, p.name), el("div", { class: "p-sub" }, sub)),
        el("span", { class: `p-amt ${n > 0 ? "in" : n < 0 ? "out" : ""}` }, money(Math.abs(n), p.currency))),
      hist);
    return li;
  });
  $("#people").replaceChildren(...(rows.length ? rows : [el("li", { class: "empty" }, "No open debts")]));
  if (state.openFirstPerson && rows.length) { state.openFirstPerson = false; rows[0].querySelector(".person-row").click(); }
}

async function toggleHistory(personId, box, name) {
  haptic();
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  if (box.childElementCount) return;
  box.append(el("li", { class: "empty" }, "Loading…"));
  try {
    const h = await api(`/api/debts/people/${personId}`);
    renderHistory(box, h.events, name);
  } catch (err) {
    box.replaceChildren(el("li", { class: "empty" }, "Couldn't load history"));
  }
}

function historyItem(e, name) {
  const d = new Date(e.date);
  const date = d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: d.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined });
  const mine = e.direction === "i_owe_them";
  const cancelled = e.status === "cancelled";
  let icon, title, cls, badge = null;
  if (e.type === "debt") {
    icon = mine ? "↓" : "↑";
    title = mine ? "You borrowed" : `${name} borrowed`;
    cls = mine ? "owe" : "owed";
    if (cancelled) badge = el("span", { class: "badge" }, "cancelled");
    else if (e.status === "settled") badge = el("span", { class: "badge ok" }, "Paid off ✓");
    else badge = el("span", { class: `badge ${mine ? "owe" : "owed"}` }, `${money(e.outstanding, e.currency)} left`);
  } else {
    icon = "↩";
    title = mine ? "You paid back" : `${name} paid back`;
    cls = "pay";
    if (cancelled) badge = el("span", { class: "badge" }, "cancelled");
  }
  const why = e.type === "debt" ? (e.purpose || e.description) : e.purpose ? `for ${e.purpose}` : e.description;
  return el("li", { class: `ev ${cls} ${cancelled ? "cancelled" : ""}` },
    el("span", { class: "ev-icon" }, icon),
    el("div", { class: "ev-body" },
      el("div", { class: "ev-top" }, el("span", { class: "ev-title" }, title), el("span", { class: "ev-amt" }, money(e.amount, e.currency))),
      why ? el("div", { class: "ev-why" }, why) : null,
      el("div", { class: "ev-meta" }, el("span", {}, date), badge)));
}

function renderHistory(box, events, name) {
  const newestFirst = [...events].sort((a, b) => new Date(b.date) - new Date(a.date) || (a.type === "payment" ? -1 : 1));
  const active = newestFirst.filter((e) => e.status !== "cancelled");
  const cancelled = newestFirst.filter((e) => e.status === "cancelled");
  const items = active.map((e) => historyItem(e, name));
  if (!items.length) items.push(el("li", { class: "empty" }, "No history"));
  if (cancelled.length) {
    const more = el("li", { class: "ev-more" });
    const btn = el("button", { onclick: () => { more.replaceWith(...cancelled.map((e) => historyItem(e, name))); haptic(); } },
      `Show ${cancelled.length} cancelled`);
    more.append(btn);
    items.push(more);
  }
  box.replaceChildren(...items);
}

// ---------------------------------------------------------------- load / navigation
async function load() {
  const id = ++state.loadId;
  renderDatebar();
  document.body.classList.add("loading");
  try {
    if (state.view === "overview") await loadOverview(id);
    else if (state.view === "transactions") await loadTransactions(id);
    else await loadDebts(id);
    if (id === state.loadId) showError(null);
  } catch (e) {
    if (id === state.loadId) showError(e);
  } finally {
    if (id === state.loadId) document.body.classList.remove("loading");
  }
}

$$("#periods button").forEach((b) => b.addEventListener("click", () => {
  if (b.classList.contains("on")) return;
  $$("#periods button").forEach((x) => x.classList.toggle("on", x === b));
  state.period = b.dataset.period;
  state.anchor = startOfDay(new Date());
  haptic();
  load();
}));
$("#prev").addEventListener("click", () => { shift(-1); haptic(); load(); });
$("#next").addEventListener("click", () => { shift(1); haptic(); load(); });

// Tap the date to jump to a specific day/week/month; years use the arrows.
function openPicker(input) {
  try { input.showPicker(); } catch { input.focus(); input.click(); }
}
$("#date-label").addEventListener("click", () => {
  const today = ymd(startOfDay(new Date()));
  if (state.period === "month") {
    const p = $("#pick-month");
    p.max = today.slice(0, 7);
    p.value = ymd(state.anchor).slice(0, 7);
    openPicker(p);
  } else if (state.period === "day" || state.period === "week") {
    const p = $("#pick-date");
    p.max = today;
    p.value = ymd(state.anchor);
    openPicker(p);
  }
});
$("#pick-date").addEventListener("change", (e) => { if (e.target.value) { state.anchor = parseYmd(e.target.value); load(); } });
$("#pick-month").addEventListener("change", (e) => { if (e.target.value) { state.anchor = parseYmd(`${e.target.value}-01`); load(); } });

$$("#tabbar button").forEach((b) => b.addEventListener("click", () => {
  if (b.classList.contains("on")) return;
  $$("#tabbar button").forEach((x) => x.classList.toggle("on", x === b));
  state.view = b.dataset.view;
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${state.view}`));
  $("#top").hidden = state.view === "debts"; // debts are not tied to a period
  window.scrollTo(0, 0);
  haptic();
  load();
}));
$("#tx-more").addEventListener("click", async () => {
  const id = state.loadId;
  try { await loadTransactions(id, true); } catch (e) { showError(e); }
});

// Optional deep link: /app?view=transactions&period=week
function applyStartParams() {
  const q = new URLSearchParams(location.search);
  const period = q.get("period");
  if (["day", "week", "month", "year"].includes(period)) {
    state.period = period;
    $$("#periods button").forEach((x) => x.classList.toggle("on", x.dataset.period === period));
  }
  state.openFirstPerson = q.get("open") === "1";
  const view = q.get("view");
  if (["overview", "transactions", "debts"].includes(view)) {
    state.view = view;
    $$("#tabbar button").forEach((x) => x.classList.toggle("on", x.dataset.view === view));
    $$(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
    $("#top").hidden = view === "debts";
  }
}

document.addEventListener("DOMContentLoaded", () => { applyStartParams(); load(); });
