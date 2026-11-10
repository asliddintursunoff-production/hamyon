"use strict";
// Personal finance dashboard. All untrusted text (merchants, descriptions) is set via textContent.

const CURRENCY = document.body.dataset.currency || "UZS";
const state = { tab: "overview", categories: [], charts: {}, tx: { page: 1, sort: "datetime", order: "desc", q: "", review: false }, editing: null };

// ---------- helpers
const $ = (sel) => document.querySelector(sel);
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
function money(v, cur = CURRENCY, signed = false) {
  if (v == null) return "—";
  const n = Number(v); // display only; all calculations happen server-side with exact decimals
  const s = Math.abs(n).toLocaleString("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  const sign = n < 0 ? "−" : signed && n > 0 ? "+" : "";
  return `${sign}${s} ${cur}`;
}
function fmtDate(iso, withTime = true) {
  const d = new Date(iso);
  return withTime ? d.toLocaleString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })
                  : d.toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric" });
}
function setStatus(t) { $("#status").textContent = t || ""; }
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-Requested-With": "finance-dashboard", ...(opts.headers || {}) },
    credentials: "same-origin",
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  return data;
}

function filterParams() {
  const p = new URLSearchParams();
  const period = $("#f-period").value;
  p.set("period", period);
  if (period === "custom") {
    if ($("#f-start").value) p.set("start", $("#f-start").value);
    if ($("#f-end").value) p.set("end", $("#f-end").value);
  }
  for (const [id, key] of [["#f-card", "card_id"], ["#f-type", "type"], ["#f-category", "category_id"]]) {
    if ($(id).value) p.set(key, $(id).value);
  }
  return p;
}

// ---------- charts
const typeLabels = { expense: "Expense", income: "Income", transfer: "Transfer", refund: "Refund", cash_withdrawal: "Cash withdrawal", other: "Other" };

function baseOptions(horizontal = false, legend = false) {
  const grid = css("--grid"), ink = css("--text-secondary");
  return {
    responsive: true, maintainAspectRatio: false, indexAxis: horizontal ? "y" : "x",
    interaction: { mode: horizontal ? "nearest" : "index", intersect: false },
    plugins: {
      legend: { display: legend, labels: { color: ink, boxWidth: 12, usePointStyle: true } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label ? c.dataset.label + ": " : ""}${money(horizontal ? c.parsed.x : c.parsed.y)}` } },
    },
    scales: {
      x: { grid: { color: horizontal ? grid : "transparent" }, ticks: { color: ink, maxRotation: 0, autoSkip: true } },
      y: { grid: { color: horizontal ? "transparent" : grid }, ticks: { color: ink }, beginAtZero: true },
    },
  };
}
function bar(id, labels, datasets, { horizontal = false, legend = false } = {}) {
  if (!window.Chart) return;
  state.charts[id]?.destroy();
  const ctx = document.getElementById(id);
  state.charts[id] = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets: datasets.map((d) => ({ borderRadius: 4, borderSkipped: "start", maxBarThickness: 28, categoryPercentage: 0.8, barPercentage: 0.9, ...d })) },
    options: baseOptions(horizontal, legend),
  });
}

// ---------- overview
function kpi(label, value, sub, cls) {
  return el("div", { class: "kpi" }, el("div", { class: "label" }, label), el("div", { class: `value ${cls || ""}` }, value), sub ? el("div", { class: "sub" }, sub) : null);
}

async function loadOverview() {
  const p = filterParams();
  const [ov, an] = await Promise.all([api(`/api/overview?${p}`), api(`/api/analytics?${p}`)]);
  const s = ov.summary;
  const totals = Object.entries(ov.cards_total).map(([c, v]) => money(v, c)).join(" · ") || "unknown";
  const trend = ov.trend.change_pct != null ? `${Number(ov.trend.change_pct) > 0 ? "▲" : "▼"} ${Math.abs(ov.trend.change_pct)}% vs previous period` : "";
  const net = Number(s.net_cash_flow);
  $("#kpis").replaceChildren(
    kpi("Income", money(s.income)),
    kpi("Expenses", money(s.expenses), s.refunds !== "0.00" ? `refunds ${money(s.refunds)}` : null),
    kpi("Net cash flow", money(s.net_cash_flow, CURRENCY, true), null, net > 0 ? "pos" : net < 0 ? "neg" : ""),
    kpi("Avg daily spending", money(s.avg_daily_spending), trend),
    kpi("Cards total", totals),
    kpi("Owed to me", money(ov.debts.owed_to_me)),
    kpi("I owe", money(ov.debts.i_owe)),
    kpi("Transfers out", money(s.transfers_out), `${s.needs_review} need review`),
  );
  $("#cards").replaceChildren(...ov.cards.map((c) => el("div", { class: "card-item" },
    el("div", { class: "bank" }, `${c.bank_name || "HUMO"} *${c.last4}`),
    el("div", { class: "bal" }, c.balance != null ? money(c.balance, c.currency) : "unknown"),
    el("div", { class: "muted" }, c.synced_at ? `updated ${fmtDate(c.synced_at)}` : "never synced"))));

  const blue = css("--series-1"), orange = css("--series-2"), aqua = css("--series-3");
  const cats = an.by_category.slice(0, 12);
  bar("ch-category", cats.map((c) => c.name), [{ label: "Spent", data: cats.map((c) => Number(c.total)), backgroundColor: blue }], { horizontal: true });
  bar("ch-daily", an.daily.map((d) => d.date.slice(5)), [{ label: "Spent", data: an.daily.map((d) => Number(d.total)), backgroundColor: blue }]);
  bar("ch-monthly", an.monthly.map((m) => m.month), [
    { label: "Income", data: an.monthly.map((m) => Number(m.income)), backgroundColor: aqua },
    { label: "Expenses", data: an.monthly.map((m) => Number(m.expenses)), backgroundColor: orange },
  ], { legend: true });
  bar("ch-cards", an.cards.map((c) => c.label), [{ label: "Expenses", data: an.cards.map((c) => Number(c.expenses)), backgroundColor: blue }]);
  const tm = an.top_merchants;
  bar("ch-merchants", tm.map((m) => m.merchant.length > 28 ? m.merchant.slice(0, 27) + "…" : m.merchant), [{ label: "Spent", data: tm.map((m) => Number(m.total)), backgroundColor: blue }], { horizontal: true });

  $("#largest").replaceChildren(
    el("tr", {}, el("th", {}, "Date"), el("th", {}, "Merchant"), el("th", { class: "num" }, "Amount")),
    ...(an.largest.length ? an.largest.map((t) => el("tr", {}, el("td", {}, fmtDate(t.datetime, false)), el("td", {}, t.merchant || "—"), el("td", { class: "num" }, money(t.amount))))
                          : [el("tr", {}, el("td", { colspan: 3, class: "muted" }, "No expenses in this period"))]));
  $("#subcategories").replaceChildren(
    el("tr", {}, el("th", {}, "Category"), el("th", {}, "Subcategory"), el("th", { class: "num" }, "Count"), el("th", { class: "num" }, "Total")),
    ...an.by_subcategory.map((r) => el("tr", {}, el("td", {}, r.category), el("td", {}, r.subcategory), el("td", { class: "num" }, r.count), el("td", { class: "num" }, money(r.total)))));
}

// ---------- transactions
async function loadTransactions() {
  const p = filterParams();
  const t = state.tx;
  p.set("page", t.page); p.set("sort", t.sort); p.set("order", t.order);
  if (t.q) p.set("q", t.q);
  if (t.review) p.set("needs_review", "true");
  const data = await api(`/api/transactions?${p}`);
  const rows = data.items.map((x) => {
    const sign = x.direction === "out" ? "−" : "+";
    const cat = [x.category, x.subcategory].filter(Boolean).join(" / ") || "—";
    const conf = x.ai_confidence != null ? `${Math.round(Number(x.ai_confidence) * 100)}%` : x.classification;
    return el("tr", {},
      el("td", {}, fmtDate(x.datetime)),
      el("td", {}, x.merchant || x.description || "—", x.merchant && x.description ? el("div", { class: "muted" }, x.description) : null),
      el("td", { class: `num ${x.direction === "in" ? "pos" : ""}` }, `${sign}${money(x.amount, x.currency)}`),
      el("td", {}, typeLabels[x.type] || x.type),
      el("td", {}, cat),
      el("td", {}, x.card || "manual"),
      el("td", {}, el("span", { class: `badge ${x.needs_review ? "review" : ""}` }, x.needs_review ? `⚠ review · ${conf}` : conf)),
      el("td", {}, el("button", { class: "btn small", onclick: () => openEdit(x) }, "Edit")));
  });
  $("#tx-table tbody").replaceChildren(...(rows.length ? rows : [el("tr", {}, el("td", { colspan: 8, class: "muted" }, "No transactions"))]));
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  $("#tx-page").textContent = `Page ${data.page} / ${pages} · ${data.total} transactions`;
  $("#tx-prev").disabled = data.page <= 1;
  $("#tx-next").disabled = data.page >= pages;
}

function fillSubcategories(catId, selected) {
  const cat = state.categories.find((c) => String(c.id) === String(catId));
  $("#e-subcategory").replaceChildren(el("option", { value: "" }, "(none)"),
    ...(cat ? cat.children.map((c) => el("option", { value: c.id, selected: String(c.id) === String(selected) }, c.name)) : []));
}
function openEdit(x) {
  state.editing = x;
  $("#edit-summary").textContent = `${fmtDate(x.datetime)} · ${x.merchant || x.description || ""} · ${money(x.amount, x.currency)}`;
  $("#e-type").value = x.type;
  $("#e-category").replaceChildren(...state.categories.map((c) => el("option", { value: c.id, selected: c.id === x.category_id }, c.name)));
  fillSubcategories($("#e-category").value, x.subcategory_id);
  $("#e-description").value = x.description || "";
  $("#e-remember").checked = false;
  $("#e-remember").parentElement.hidden = !x.merchant;
  $("#e-reclassify").hidden = !x.can_reclassify;
  $("#edit-dialog").showModal();
}
async function saveEdit(ev) {
  ev.preventDefault();
  const x = state.editing;
  try {
    await api(`/api/transactions/${x.id}`, { method: "PATCH", body: JSON.stringify({
      transaction_type: $("#e-type").value,
      category_id: Number($("#e-category").value) || null,
      subcategory_id: Number($("#e-subcategory").value) || null,
      description: $("#e-description").value,
      apply_to_merchant: $("#e-remember").checked,
    }) });
    $("#edit-dialog").close();
    setStatus("Saved");
    await reload();
  } catch (e) { alert(e.message); }
}
async function reclassify() {
  try {
    await api(`/api/transactions/${state.editing.id}/reclassify`, { method: "POST" });
    $("#edit-dialog").close();
    setStatus("Queued for AI classification — refresh in a minute");
  } catch (e) { alert(e.message); }
}

// ---------- debts
async function loadDebts() {
  const d = await api("/api/debts");
  $("#debt-kpis").replaceChildren(
    kpi("Owed to me", money(d.totals.owed_to_me, d.currency)),
    kpi("I owe", money(d.totals.i_owe, d.currency)),
    kpi("Net position", money(d.totals.net, d.currency, true), null, Number(d.totals.net) >= 0 ? "pos" : "neg"));
  $("#debt-people").replaceChildren(
    el("tr", {}, el("th", {}, "Person"), el("th", { class: "num" }, "Owes me"), el("th", { class: "num" }, "I owe"), el("th", { class: "num" }, "Net")),
    ...(d.people.length ? d.people.map((p) => el("tr", { class: "clickable", onclick: () => loadPerson(p.id) },
      el("td", {}, p.name), el("td", { class: "num" }, money(p.they_owe_me, p.currency)), el("td", { class: "num" }, money(p.i_owe_them, p.currency)),
      el("td", { class: `num ${Number(p.net) >= 0 ? "pos" : "neg"}` }, money(p.net, p.currency, true))))
      : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, "No open debts"))]));
}
async function loadPerson(id) {
  const h = await api(`/api/debts/people/${id}`);
  $("#debt-history-title").textContent = `History — ${h.person.name}`;
  const rows = h.events.map((e) => {
    const cancelled = e.status === "cancelled";
    const what = e.type === "debt"
      ? (e.direction === "i_owe_them" ? "I borrowed" : "They borrowed") + (e.status === "open" ? ` · left ${money(e.outstanding, e.currency)}` : e.status === "settled" ? " · settled" : "")
      : (e.direction === "i_owe_them" ? "I repaid" : "They repaid");
    const btn = cancelled ? null : el("button", { class: "btn small", onclick: async () => {
      if (!confirm("Cancel this entry? It stays in the audit history.")) return;
      try {
        await api(e.type === "debt" ? `/api/debts/${e.id}/cancel` : `/api/debt-payments/${e.id}/cancel`, { method: "POST" });
        await loadDebts(); await loadPerson(id);
      } catch (err) { alert(err.message); }
    } }, "Cancel");
    return el("tr", { class: cancelled ? "strike" : "" }, el("td", {}, fmtDate(e.date, false)), el("td", {}, what, e.description ? el("div", { class: "muted" }, e.description) : null),
      el("td", { class: "num" }, money(e.amount, e.currency)), el("td", {}, btn));
  });
  $("#debt-history").replaceChildren(el("table", { class: "table compact" }, ...rows));
}

// ---------- wiring
async function reload() {
  setStatus("Loading…");
  try {
    if (state.tab === "overview") await loadOverview();
    else if (state.tab === "transactions") await loadTransactions();
    else await loadDebts();
    setStatus("");
  } catch (e) { setStatus(`⚠ ${e.message}`); }
}

async function init() {
  const [cats, cards] = await Promise.all([api("/api/categories"), api("/api/cards")]);
  state.categories = cats;
  $("#f-category").append(...cats.flatMap((c) => [el("option", { value: c.id }, c.name), ...c.children.map((s) => el("option", { value: s.id }, `   ${c.name} / ${s.name}`))]));
  $("#f-card").append(...cards.map((c) => el("option", { value: c.id }, c.label)));

  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    state.tab = b.dataset.tab;
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${state.tab}`));
    reload();
  }));
  ["#f-card", "#f-type", "#f-category", "#f-start", "#f-end"].forEach((id) => $(id).addEventListener("change", () => { state.tx.page = 1; reload(); }));
  $("#f-period").addEventListener("change", () => {
    $("#custom-range").hidden = $("#f-period").value !== "custom";
    state.tx.page = 1;
    if ($("#f-period").value !== "custom" || ($("#f-start").value && $("#f-end").value)) reload();
  });
  let t;
  $("#tx-search").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { state.tx.q = e.target.value.trim(); state.tx.page = 1; reload(); }, 300); });
  $("#tx-review").addEventListener("change", (e) => { state.tx.review = e.target.checked; state.tx.page = 1; reload(); });
  document.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    state.tx.order = state.tx.sort === key && state.tx.order === "desc" ? "asc" : "desc";
    state.tx.sort = key;
    document.querySelectorAll("th.sortable").forEach((h) => (h.textContent = h.textContent.replace(/ [▾▴]$/, "")));
    th.textContent += state.tx.order === "desc" ? " ▾" : " ▴";
    reload();
  }));
  $("#tx-prev").addEventListener("click", () => { state.tx.page--; reload(); });
  $("#tx-next").addEventListener("click", () => { state.tx.page++; reload(); });
  $("#e-category").addEventListener("change", (e) => fillSubcategories(e.target.value));
  $("#edit-form").addEventListener("submit", (e) => { if (e.submitter?.value === "cancel") return; saveEdit(e); });
  $("#e-reclassify").addEventListener("click", reclassify);
  $("#refresh-balances").addEventListener("click", async (e) => {
    const b = e.currentTarget; b.disabled = true; setStatus("Requesting balances from HUMO…");
    try { await api("/api/cards/refresh", { method: "POST" }); setStatus("Balances refreshed"); await reload(); }
    catch (err) { setStatus(`⚠ ${err.message}`); }
    finally { b.disabled = false; }
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => state.tab === "overview" && loadOverview());
  await reload();
}

document.addEventListener("DOMContentLoaded", () => init().catch((e) => setStatus(`⚠ ${e.message}`)));
