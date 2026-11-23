/* Smart Retail Edge dashboard — vanilla JS, no build step, works offline. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const fmt = (n, d = 0) => n == null || isNaN(n) ? "–" : Number(n).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const money = n => n == null ? "–" : "$" + fmt(n, 0);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const SEV_COL = {CRITICAL: "#b3261e", HIGH: "#b8540b", MEDIUM: "#8a6d00", OK: "#2f6b3a", OVERSTOCK: "#4a5a8a"};

Chart.defaults.font.family = '-apple-system,"Segoe UI",system-ui,sans-serif';
Chart.defaults.font.size = 11;
Chart.defaults.color = "#6c6c65";
Chart.defaults.borderColor = "#ecebe6";
Chart.defaults.animation = false;

const S = {rows: [], kpis: {}, alerts: [], selected: null, sevFilter: "ALL", sort: null, feedPaused: false,
           rate: [], ordered: {}, manual: [], health: null, sim: null, mode: "sku"};

/* ---------------- navigation ---------------- */
const TITLES = {overview: "Overview", inventory: "Inventory & replenishment", predict: "Demand prediction",
                model: "Model performance", edge: "Edge node & MQTT"};
function show(view) {
  if (!TITLES[view]) view = "overview";
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + view));
  $$("nav a").forEach(a => a.classList.toggle("active", a.dataset.view === view));
  $("#viewTitle").textContent = TITLES[view];
  if (view === "model") loadModel();
  if (view === "edge") refreshEdge();
}
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
$$("[data-goto]").forEach(a => a.addEventListener("click", () => show(a.dataset.goto)));

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("#toast").appendChild(t);
  setTimeout(() => t.remove(), 3000);
  while ($("#toast").children.length > 2) $("#toast").firstChild.remove();
}

async function api(path, opts = {}) {
  const r = await fetch(path, {headers: {"content-type": "application/json"}, ...opts});
  let body = null;
  try { body = await r.json(); } catch { /* non-json */ }
  if (!r.ok) {
    const det = body && (body.details || body.detail || body.error);
    const msg = Array.isArray(det) ? det.map(d => (d.field ? d.field + ": " : "") + d.msg.replace("Value error, ", "")).join("\n") : (det || r.statusText);
    throw new Error(msg);
  }
  return body;
}

/* ---------------- websocket ---------------- */
let ws, wsDelay = 500;
function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { wsDelay = 500; setPill("#pillWs", true, "Live"); };
  ws.onclose = () => { setPill("#pillWs", false, "Reconnecting"); setTimeout(connect, wsDelay = Math.min(wsDelay * 2, 8000)); };
  ws.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "snapshot") onSnapshot(m);
    else if (m.type === "tick") onTick(m);
    else if (m.type === "feed") onFeed(m.items, m.rate);
    else if (m.type === "clock") { $("#simDate").textContent = m.data.sim_date; }
    else if (m.type === "manual_prediction") { S.manual.unshift(m.data); S.manual = S.manual.slice(0, 6); renderManualHist(); }
  };
}
function setPill(sel, up, text) { const p = $(sel); p.classList.toggle("up", up); p.classList.toggle("down", !up); p.lastChild.textContent = text; }

function onSnapshot(m) {
  S.rows = m.rows; S.kpis = m.kpis; S.alerts = m.alerts || []; S.health = m.health; S.sim = m.sim;
  fillSkuSelects();
  if (!S.selected) S.selected = (S.rows.find(r => r.rec && r.rec.severity === "CRITICAL") || S.rows.find(r => r.sku.startsWith("FOODS_3")) || S.rows[0]).sku;
  $("#skuSelect").value = S.selected;
  onFeed(m.feed || [], m.kpis.msg_rate, true);
  renderAll();
  renderHealth();
  loadSku();
}
function onTick(m) {
  S.rows = m.rows; S.kpis = m.kpis;
  (m.alerts || []).forEach(a => S.alerts.unshift(a));
  S.alerts = S.alerts.slice(0, 120);
  (m.alerts || []).filter(a => a.severity === "CRITICAL").slice(0, 2).forEach(a => toast(`CRITICAL · ${a.sku} — ${a.reason}`));
  renderAll();
  loadSku();
  if ($("#view-edge").classList.contains("active")) refreshEdge();
}

/* ---------------- overview ---------------- */
function renderAll() { renderKpis(); renderQueue(); renderMini(); renderInventory(); }

function renderKpis() {
  const k = S.kpis, sev = k.severity || {};
  $("#simDate").textContent = k.sim_date || "–";
  $("#dayIdx").textContent = k.day_index ?? 0;
  $("#kDemand").innerHTML = fmt(k.pred_units_7d) + " <small>units</small>";
  $("#kRevenue").textContent = money(k.pred_revenue_7d) + " forecast revenue · " + (k.skus || 0) + " SKUs";
  $("#kCrit").textContent = sev.CRITICAL || 0; $("#kHigh").textContent = sev.HIGH || 0;
  $("#kMed").textContent = sev.MEDIUM || 0; $("#kOk").textContent = (sev.OK || 0) + (sev.OVERSTOCK ? ` · ${sev.OVERSTOCK} overstock` : "");
  $("#kRisk").textContent = money(k.revenue_at_risk);
  $("#kPOs").textContent = fmt(k.open_pos); $("#kOnOrder").textContent = fmt(k.on_order_units); $("#kLead").textContent = k.lead_time_days;
  const a = k.accuracy || {};
  if (a.n) {
    $("#kWape").innerHTML = fmt(a.wape * 100, 1) + "% <small>model</small>";
    $("#kWapeS").textContent = `naive last-week ${fmt(a.naive_wape * 100, 1)}% · ${fmt(a.n)} settled forecasts`;
  }
  const l = k.latency || {};
  $("#kLat").innerHTML = fmt(l.p50_ms, 1) + " <small>ms p50</small>";
  $("#kLatS").textContent = `p95 ${fmt(l.p95_ms, 1)} ms · ${fmt(l.count)} inference calls`;
  $("#pillRate").textContent = fmt(k.msg_rate, 1) + " msg/s";
  $("#autoReorder").checked = !!k.auto_reorder;
  if (S.sim) $("#simInfo").textContent = `replaying held-out data at ${S.sim.speed_s_per_day}s per store day`;
}

function coverCell(rec) {
  if (!rec || rec.days_of_cover == null) return '<span class="muted">–</span>';
  const c = rec.days_of_cover, pct = Math.min(c / 21, 1) * 100, lead = Math.min((S.kpis.lead_time_days || 3) / 21, 1) * 100;
  return `<div class="cover"><div class="bar"><i style="width:${pct}%;background:${SEV_COL[rec.severity] || "#999"}"></i><b style="left:${lead}%"></b></div><span>${fmt(c, 1)} d</span></div>`;
}
const badge = s => `<span class="badge b-${s || "UNKNOWN"}">${s || "NO DATA"}</span>`;

function orderBtn(r) {
  const rec = r.rec || {};
  if (r.on_order > 0) return `<button class="btn sm ordered" disabled>${fmt(r.on_order)} on order</button>`;
  if (!rec.order_qty) return "";
  return `<button class="btn sm" data-order="${r.sku}" data-qty="${rec.order_qty}">Order ${fmt(rec.order_qty)}</button>`;
}

function renderQueue() {
  const q = S.rows.filter(r => r.rec && ["CRITICAL", "HIGH", "MEDIUM"].includes(r.rec.severity));
  $("#queueCount").textContent = q.length + " items";
  $("#queue").innerHTML = q.slice(0, 30).map(r => `
    <div class="qi">${badge(r.rec.severity)}
      <div><span class="sku">${r.sku}</span> <span class="meta">· ${fmt(r.on_hand)} on hand · fc ${fmt(r.forecast.pred_7d)}/7d</span></div>
      ${orderBtn(r) || '<span class="muted">—</span>'}
      <div class="why">${esc(r.rec.reason)}</div></div>`).join("") || '<div class="empty">All products covered for the next 7 days.</div>';
}

function renderMini() {
  const rows = S.rows.slice(0, 9);
  $("#miniTable tbody").innerHTML = rows.map(r => `
    <tr data-sku="${r.sku}" class="${r.sku === S.selected ? "sel" : ""}"><td class="sku">${r.sku}</td>
    <td class="r">${fmt(r.on_hand)}</td><td class="r">${fmt(r.forecast?.pred_7d)}</td><td>${coverCell(r.rec)}</td>
    <td>${badge(r.rec?.severity)}</td><td class="r">${orderBtn(r)}</td></tr>`).join("");
}

function spark(vals, w = 84, h = 22) {
  if (!vals || !vals.length) return "";
  const max = Math.max(...vals, 1), step = w / (vals.length - 1 || 1);
  const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - 2 - (v / max) * (h - 4)).toFixed(1)}`).join(" ");
  return `<svg class="spark" width="${w}" height="${h}"><polyline points="${pts}" fill="none" stroke="#1f5f5b" stroke-width="1.2"/></svg>`;
}

function renderInventory() {
  const term = $("#search").value.trim().toUpperCase(), cat = $("#catFilter").value;
  let rows = S.rows.filter(r => (S.sevFilter === "ALL" || r.rec?.severity === S.sevFilter) && (!cat || r.category === cat) && (!term || r.sku.includes(term)));
  if (S.sort) {
    const [k, dir] = S.sort, key = {sku: r => r.sku, dept: r => r.department, price: r => r.unit_price, on_hand: r => r.on_hand ?? -1, on_order: r => r.on_order,
      last_7: r => r.last_7, pred: r => r.forecast?.pred_7d ?? 0, cover: r => r.rec?.days_of_cover ?? 1e9, rop: r => r.rec?.reorder_point ?? 0,
      sev: r => ["CRITICAL", "HIGH", "MEDIUM", "OVERSTOCK", "OK"].indexOf(r.rec?.severity)}[k];
    rows = [...rows].sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * dir);
  }
  $("#invCount").textContent = `${rows.length} of ${S.rows.length} products`;
  $("#invTable tbody").innerHTML = rows.map(r => {
    const f = r.forecast || {}, rec = r.rec || {};
    return `<tr data-sku="${r.sku}"><td class="sku">${r.sku}</td><td>${esc(r.dept_label)}</td><td class="r">$${fmt(r.unit_price, 2)}</td>
      <td class="r">${fmt(r.on_hand)}</td><td class="r">${r.on_order ? fmt(r.on_order) : '<span class="muted">0</span>'}</td><td>${spark(r.spark)}</td>
      <td class="r">${fmt(r.last_7)}</td><td class="r"><b>${fmt(f.pred_7d)}</b> <span class="muted">${fmt(f.p10)}–${fmt(f.p90)}</span></td>
      <td>${coverCell(rec)}</td><td class="r">${fmt(rec.reorder_point)}</td><td>${badge(rec.severity)}</td>
      <td class="r">${rec.order_qty ? fmt(rec.order_qty) : '<span class="muted">0</span>'}</td><td>${orderBtn(r)}</td></tr>`;
  }).join("");
}
$("#sevChips").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; S.sevFilter = b.dataset.sev; $$("#sevChips button").forEach(x => x.classList.toggle("on", x === b)); renderInventory(); });
$("#catFilter").addEventListener("change", renderInventory);
$("#search").addEventListener("input", renderInventory);
$$("#invTable th[data-k]").forEach(th => th.addEventListener("click", () => { const k = th.dataset.k; S.sort = S.sort && S.sort[0] === k ? [k, -S.sort[1]] : [k, 1]; renderInventory(); }));

document.addEventListener("click", async e => {
  const b = e.target.closest("[data-order]");
  if (b) {
    e.stopPropagation(); b.disabled = true;
    try {
      const po = await api(`/stock/${b.dataset.order}/reorder`, {method: "POST", body: JSON.stringify({qty: +b.dataset.qty})});
      toast(`${po.po} sent over MQTT · ${po.sku} × ${po.qty}, ETA ${po.lead_time_days} days`);
      b.textContent = "Ordered"; b.classList.add("ordered");
    } catch (err) { toast("Order failed: " + err.message, true); b.disabled = false; }
    return;
  }
  const tr = e.target.closest("tr[data-sku]");
  if (tr) { S.selected = tr.dataset.sku; $("#skuSelect").value = S.selected; if (!$("#view-overview").classList.contains("active")) location.hash = "overview"; renderMini(); loadSku(); }
});

$("#autoReorder").addEventListener("change", async e => {
  try { await api("/settings", {method: "POST", body: JSON.stringify({auto_reorder: e.target.checked})}); toast("Auto-reorder " + (e.target.checked ? "on" : "off")); }
  catch (err) { toast(err.message, true); }
});

/* ---------------- SKU chart ---------------- */
let skuChart, skuLoading = false;
function fillSkuSelects() {
  const opts = S.rows.map(r => r.sku).sort().map(s => `<option>${s}</option>`).join("");
  $("#skuSelect").innerHTML = opts; $("#pSku").innerHTML = opts;
}
$("#skuSelect").addEventListener("change", e => { S.selected = e.target.value; renderMini(); loadSku(); });

async function loadSku() {
  if (!S.selected || skuLoading) return;
  skuLoading = true;
  try {
    const d = await api("/stock/" + S.selected);
    drawSku(d);
  } catch (e) { /* transient */ } finally { skuLoading = false; }
}
function drawSku(d) {
  const hist = d.history.slice(-75), trail = new Map(d.trail.map(t => [t[0], t]));
  const labels = hist.map(h => h[0].slice(5));
  // extend 7 days into the future for the latest forecast
  const last = d.forecast;
  const fut = [];
  if (last) { const base = new Date(last.origin + "T00:00:00"); for (let i = 1; i <= 7; i++) { const x = new Date(base); x.setDate(x.getDate() + i); fut.push(x.toISOString().slice(5, 10)); } }
  const L = labels.concat(fut);
  const rate = hist.map(h => trail.has(h[0]) ? trail.get(h[0])[1] / 7 : null).concat(fut.map(() => last ? last.pred_7d / 7 : null));
  const lo = hist.map(h => trail.has(h[0]) ? trail.get(h[0])[2] / 7 : null).concat(fut.map(() => last ? last.p10 / 7 : null));
  const hi = hist.map(h => trail.has(h[0]) ? trail.get(h[0])[3] / 7 : null).concat(fut.map(() => last ? last.p90 / 7 : null));
  const sold = hist.map(h => h[1]).concat(fut.map(() => null));
  const avg28 = hist.map((h, i) => i >= 27 ? hist.slice(i - 27, i + 1).reduce((a, b) => a + b[1], 0) / 28 : null).concat(fut.map(() => null));
  const data = {labels: L, datasets: [
    {type: "bar", label: "Units sold", data: sold, backgroundColor: "#c9d3d1", borderWidth: 0, barPercentage: .9, categoryPercentage: 1, order: 3},
    {type: "line", label: "p90", data: hi, borderWidth: 0, pointRadius: 0, fill: "+1", backgroundColor: "#1f5f5b1f", order: 2, spanGaps: true},
    {type: "line", label: "p10", data: lo, borderWidth: 0, pointRadius: 0, fill: false, order: 2, spanGaps: true},
    {type: "line", label: "Forecast daily rate", data: rate, borderColor: "#1f5f5b", borderWidth: 1.8, pointRadius: 0, tension: .25, order: 1, spanGaps: true},
    {type: "line", label: "28-day average", data: avg28, borderColor: "#b8540b", borderDash: [4, 3], borderWidth: 1.2, pointRadius: 0, order: 1},
  ]};
  const futureStart = labels.length - .5;
  const opts = {responsive: true, maintainAspectRatio: false, interaction: {mode: "index", intersect: false},
    plugins: {legend: {position: "bottom", labels: {boxWidth: 10, filter: i => !["p10", "p90"].includes(i.text)}},
      tooltip: {filter: i => !["p10", "p90"].includes(i.dataset.label), callbacks: {label: c => `${c.dataset.label}: ${fmt(c.parsed.y, 1)}`}}},
    scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 12}}, y: {beginAtZero: true, title: {display: true, text: "units / day"}}}};
  const futPlugin = {id: "fut", beforeDraw(c) { const x = c.scales.x, a = c.chartArea; if (!fut.length) return; const px = x.getPixelForValue(futureStart);
    c.ctx.save(); c.ctx.fillStyle = "#f6f6f2"; c.ctx.fillRect(px, a.top, a.right - px, a.bottom - a.top); c.ctx.fillStyle = "#8a8a82"; c.ctx.font = "11px system-ui"; c.ctx.fillText("next 7 days", px + 6, a.top + 12); c.ctx.restore(); }};
  if (skuChart) { skuChart.data = data; skuChart.update(); }
  else skuChart = new Chart($("#skuChart"), {data, options: opts, plugins: [futPlugin]});
  const r = d.rec || {};
  $("#skuMeta").innerHTML = `${esc(d.dept_label)} · $${fmt(d.unit_price, 2)} · ${badge(r.severity)}`;
}

/* ---------------- feed ---------------- */
const FEED_MAX = 160;
function feedRow(e, isNew) {
  const t = new Date(e.t * 1000).toTimeString().slice(0, 8);
  const p = typeof e.payload === "object" ? JSON.stringify(e.payload) : String(e.payload);
  return `<div class="fe${isNew ? " new" : ""}" data-topic="${esc(e.topic)}"><span class="t">${t}</span><span class="d-${e.dir}">${e.dir === "in" ? "SUB" : "PUB"}</span><span>${esc(e.topic.replace(/^retail\/[^/]+\//, ""))}</span><span class="p">${esc(p)}</span></div>`;
}
let lastRateSample = 0;
function onFeed(items, rate, initial = false) {
  const now = Date.now();
  if (rate != null && now - lastRateSample > 1000) { lastRateSample = now; S.rate.push([new Date().toTimeString().slice(3, 8), rate]); S.rate = S.rate.slice(-180); if (rateChart) drawRate(); }
  if (!items.length) return;
  const mini = $("#feedMini");
  // keep the overview feed readable: skip shelf readings there
  const show = items.filter(e => !/\/shelf\//.test(e.topic)).slice(-12);
  mini.insertAdjacentHTML("afterbegin", show.reverse().map(e => feedRow(e, !initial && show.length < 6)).join(""));
  while (mini.children.length > 40) mini.lastChild.remove();
  $("#feedRate").textContent = fmt(rate, 1) + " msg/s";
  if (!S.feedPaused) {
    const full = $("#feedFull"), f = $("#feedFilter").value.trim();
    const sel = items.filter(e => !f || e.topic.includes(f)).slice(-40).reverse();
    full.insertAdjacentHTML("afterbegin", sel.map(e => feedRow(e, !initial && sel.length < 6)).join(""));
    while (full.children.length > FEED_MAX) full.lastChild.remove();
  }
}
$("#feedPause").addEventListener("click", e => { S.feedPaused = !S.feedPaused; e.target.textContent = S.feedPaused ? "Resume" : "Pause"; });

/* ---------------- predict ---------------- */
const DEPTS = {FOODS: ["FOODS_1", "FOODS_2", "FOODS_3"], HOBBIES: ["HOBBIES_1", "HOBBIES_2"], HOUSEHOLD: ["HOUSEHOLD_1", "HOUSEHOLD_2"]};
function fillDepts() { $("#pDept").innerHTML = DEPTS[$("#pCat").value].map(d => `<option>${d}</option>`).join(""); }
$("#pCat").addEventListener("change", fillDepts); fillDepts();
$("#modeSeg").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return; S.mode = b.dataset.mode;
  $$("#modeSeg button").forEach(x => x.classList.toggle("on", x === b));
  $(".mode-sku").classList.toggle("hidden", S.mode !== "sku"); $(".mode-manual").classList.toggle("hidden", S.mode === "sku");
  const f = $("#predForm"); f.unit_price.placeholder = S.mode === "sku" ? "current" : "required"; if (S.mode === "manual" && !f.unit_price.value) f.unit_price.value = "1.98";
});
$("#pSku").addEventListener("change", () => {
  const r = S.rows.find(x => x.sku === $("#pSku").value); const f = $("#predForm");
  if (r) { f.unit_price.placeholder = "$" + fmt(r.unit_price, 2) + " (current)"; f.current_stock.placeholder = r.on_hand != null ? fmt(r.on_hand) + " (shelf sensor)" : "shelf sensor"; }
});

$("#predForm").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target, body = {}, err = $("#formErr"); err.textContent = "";
  const num = n => f[n].value === "" ? undefined : Number(f[n].value);
  if (S.mode === "sku") body.sku = f.sku.value;
  else { body.category = f.category.value; body.department = f.department.value; body.last_day = num("last_day"); body.last_7_days = num("last_7_days"); body.last_28_days = num("last_28_days"); }
  ["unit_price", "ref_price", "current_stock", "lead_time_days"].forEach(n => { const v = num(n); if (v !== undefined) body[n] = v; });
  const oo = num("on_order"); if (oo) body.on_order = oo;
  if (f.date.value) body.date = f.date.value;
  // client-side checks mirror the server's (server is still the authority)
  const probs = [];
  if (S.mode === "manual") {
    ["last_day", "last_7_days", "last_28_days"].forEach(n => { if (body[n] === undefined || body[n] < 0) probs.push(`${n.replace(/_/g, " ")} must be a number ≥ 0`); });
    if (!probs.length && !(body.last_day <= body.last_7_days && body.last_7_days <= body.last_28_days)) probs.push("Sales must satisfy: yesterday ≤ last 7 days ≤ last 28 days");
    if (!(body.unit_price > 0)) probs.push("Unit price is required for a new product");
  }
  if (probs.length) { err.textContent = probs.join("\n"); return; }
  $("#predBtn").disabled = true; $("#predStatus").textContent = "running on edge…";
  const t0 = performance.now();
  try {
    const res = await api("/predict", {method: "POST", body: JSON.stringify(body)});
    renderResult(res, body, performance.now() - t0);
    $("#predStatus").textContent = "";
  } catch (ex) { err.textContent = ex.message; $("#predStatus").textContent = ""; }
  finally { $("#predBtn").disabled = false; }
});

function renderResult(r, body, rtt) {
  $("#resEmpty").classList.add("hidden"); $("#resBody").classList.remove("hidden");
  $("#resMeta").textContent = `${r.model_version} · model ${fmt(r.latency_ms, 2)} ms · round-trip ${fmt(rtt, 0)} ms`;
  $("#rPred").innerHTML = fmt(r.pred_7d) + ' <small style="font-size:14px;color:#8a8a82">units</small>';
  $("#rRate").textContent = `${r.sku || "new product"} · from ${r.origin} · ≈ ${fmt(r.daily_rate, 1)} units/day`;
  const max = Math.max(r.p90 * 1.15, 1);
  $("#rBand").style.left = (r.p10 / max * 100) + "%"; $("#rBand").style.width = ((r.p90 - r.p10) / max * 100) + "%";
  $("#rMark").style.left = (r.pred_7d / max * 100) + "%";
  $("#rLo").textContent = "p10 " + fmt(r.p10); $("#rHi").textContent = "p90 " + fmt(r.p90);
  const rec = r.recommendation;
  $("#rRec").innerHTML = rec ? `${badge(rec.severity)}<div class="det"><b>${esc(rec.reason)}</b><br>on hand ${fmt(rec.on_hand)} · on order ${fmt(rec.on_order)} · safety stock ${fmt(rec.safety_stock)} · reorder point ${fmt(rec.reorder_point)} · order-up-to ${fmt(rec.order_up_to)}${rec.days_of_cover != null ? ` · ${fmt(rec.days_of_cover, 1)} days of cover` : ""}</div><div style="text-align:right"><div class="muted">order</div><div class="q">${fmt(rec.order_qty)}</div></div>`
    : `<span class="badge b-UNKNOWN">NO STOCK</span><div class="det">Enter current stock to get a replenishment recommendation.</div><div></div>`;
  $("#rFeat").innerHTML = Object.entries(r.features).map(([k, v]) => `<div><span>${k}</span><b>${typeof v === "number" ? fmt(v, Number.isInteger(v) ? 0 : 3) : v}</b></div>`).join("");
  $("#rCurl").textContent = `curl -s -X POST http://${location.host}/predict \\\n  -H 'content-type: application/json' \\\n  -d '${JSON.stringify(body)}'`;
}
function renderManualHist() {
  $("#rHist tbody").innerHTML = S.manual.map(m => `<tr><td class="sku">${m.sku || "manual"}</td><td class="r">${fmt(m.pred_7d)}</td><td class="r muted">${fmt(m.p10)}–${fmt(m.p90)}</td><td>${m.recommendation ? badge(m.recommendation.severity) : ""}</td><td class="r muted">${fmt(m.latency_ms, 1)} ms</td></tr>`).join("");
}

/* ---------------- model ---------------- */
let modelLoaded = false;
async function loadModel() {
  if (modelLoaded) return;
  try {
    const [m, d] = await Promise.all([api("/model-info?sample=true"), api("/data-info")]);
    modelLoaded = true; drawModel(m, d);
  } catch (e) { toast("Could not load model info: " + e.message, true); }
}
function drawModel(m, d) {
  const names = {random_forest: "Random Forest (deployed)", decision_tree: "Decision Tree", moving_avg_28: "28-day moving average", naive_last_7: "Naive: last 7 days"};
  const base = m.metrics_test.moving_avg_28.rmse;
  $("#metricsTable tbody").innerHTML = Object.entries(m.metrics_test).map(([k, v]) => `<tr${k === "random_forest" ? ' class="sel"' : ""}><td>${names[k] || k}</td><td class="r">${fmt(v.mae, 2)}</td><td class="r">${fmt(v.rmse, 2)}</td><td class="r">${fmt(v.r2, 3)}</td><td class="r">${fmt(v.wape * 100, 1)}%</td><td class="r">${k === "moving_avg_28" ? "—" : (v.rmse < base ? "−" : "+") + fmt(Math.abs(1 - v.rmse / base) * 100, 1) + "%"}</td></tr>`).join("");
  $("#splitInfo").textContent = `train → ${m.split.train_end} · test ${m.split.test_start} → ${m.split.test_end} · ${fmt(m.split.test_rows)} rows never seen in training`;
  const s = m.test_sample, pts = s.actual.map((a, i) => ({x: a, y: s.predicted[i]})), mx = Math.max(...s.actual, ...s.predicted);
  new Chart($("#scatter"), {type: "scatter", data: {datasets: [
    {label: "Random Forest: predicted vs actual (7-day units)", data: pts, pointRadius: 2, backgroundColor: "#1f5f5b66"},
    {type: "line", label: "perfect forecast", data: [{x: 0, y: 0}, {x: mx, y: mx}], borderColor: "#b8540b", borderDash: [4, 3], borderWidth: 1, pointRadius: 0}]},
    options: {maintainAspectRatio: false, plugins: {legend: {position: "bottom", labels: {boxWidth: 10}}}, scales: {x: {title: {display: true, text: "actual units (next 7 days)"}}, y: {title: {display: true, text: "predicted"}}}}});
  const fi = Object.entries(m.feature_importance).slice(0, 14);
  new Chart($("#fiChart"), {type: "bar", data: {labels: fi.map(f => f[0]), datasets: [{data: fi.map(f => f[1]), backgroundColor: "#1f5f5b", borderWidth: 0, barPercentage: .7}]},
    options: {indexAxis: "y", maintainAspectRatio: false, plugins: {legend: {display: false}}, scales: {x: {grid: {color: "#f0efea"}}, y: {grid: {display: false}, ticks: {font: {family: "ui-monospace,Menlo,monospace", size: 10.5}}}}}});
  $("#deptTable tbody").innerHTML = Object.entries(m.metrics_by_category).map(([k, v]) => `<tr><td class="sku">${k}</td><td class="r">${fmt(v.mae, 1)}</td><td class="r">${fmt(v.wape * 100, 1)}%</td><td class="r">${fmt(v.r2, 3)}</td><td class="r">${fmt(v.n)}</td></tr>`).join("");
  const hp = m.hyperparameters.random_forest;
  const card = [["Version", m.version], ["Algorithm", m.algorithm], ["Target", "units, next 7 days"], ["Trees / leaf / max_features", `${hp.n_estimators} / ${hp.min_samples_leaf} / ${hp.max_features}`],
    ["Max depth", hp.max_depth ?? "none"], ["Selected by", "3-fold time-series CV"], ["Features", m.features.length], ["Train rows", fmt(m.split.train_rows)],
    ["Fit time", m.fit_seconds + " s"], ["Single-row latency p50", m.latency_ms_single_row.p50 + " ms"], ["Dataset", m.dataset.name], ["Seed", m.seed]];
  $("#cardTable").innerHTML = card.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("");
  const cl = [["Store", d.store], ["Products in store", fmt(d.store_products)], ["Sold on ≥80% of days", fmt(d.eligible_products)], ["Selected (7 per dept)", d.products],
    ["Date range", `${d.date_min} → ${d.date_max}`], ["Daily rows", fmt(d.daily_rows)], ["Missing prices filled", fmt(d.missing_price_rows)],
    ["Outliers capped (p99.9)", fmt(d.capped_outliers)], ["Store-closed days flagged", (d.store_closed_days || []).length]];
  $("#cleanTable").innerHTML = cl.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("");
}

/* ---------------- edge ---------------- */
let rateChart, latChart;
function drawRate() {
  const data = {labels: S.rate.map(r => r[0]), datasets: [{data: S.rate.map(r => r[1]), borderColor: "#2b6c9a", backgroundColor: "#2b6c9a18", fill: true, pointRadius: 0, borderWidth: 1.5, tension: .3}]};
  if (rateChart) { rateChart.data = data; rateChart.update(); }
  else rateChart = new Chart($("#rateChart"), {type: "line", data, options: {maintainAspectRatio: false, plugins: {legend: {display: false}}, scales: {x: {ticks: {maxTicksLimit: 8}, grid: {display: false}}, y: {beginAtZero: true}}}});
}
async function refreshEdge() {
  if (!rateChart) drawRate();
  try {
    const [h, m] = await Promise.all([api("/health"), api("/metrics/edge")]);
    S.health = h; S.sim = m.sim; renderHealth();
    const l = m.latency, c = m.kpis.counters || {};
    $("#eLat").innerHTML = `${fmt(l.p50_ms, 1)} <small>/</small> ${fmt(l.p95_ms, 1)} <small>ms</small>`;
    $("#eMsgs").innerHTML = `${fmt(c.mqtt_in || 0)} <small>/</small> ${fmt(c.mqtt_out || 0)}`;
    $("#eRej").textContent = `${fmt(c.rejected || 0)} malformed rejected · ${fmt(c.dropped_inbox || 0)} dropped`;
    $("#eMem").innerHTML = `${fmt(h.rss_mb, 0)} <small>MB RSS</small>`;
    $("#eUp").textContent = `uptime ${Math.floor(h.uptime_s / 60)} min · ${h.platform}`;
    $("#eLost").innerHTML = m.sim ? fmt(m.sim.lost_units) + " <small>units</small>" : "–";
    const data = {labels: l.recent.map((_, i) => i + 1), datasets: [{data: l.recent, backgroundColor: "#1f5f5b", borderWidth: 0, barPercentage: 1, categoryPercentage: .9}]};
    if (latChart) { latChart.data = data; latChart.update(); }
    else latChart = new Chart($("#latChart"), {type: "bar", data, options: {maintainAspectRatio: false, plugins: {legend: {display: false}}, scales: {x: {display: false}, y: {beginAtZero: true, title: {display: true, text: "ms"}}}}});
  } catch (e) { /* transient */ }
}
function renderHealth() {
  const h = S.health; if (!h) return;
  setPill("#pillMqtt", h.mqtt_connected, h.mqtt_connected ? "MQTT connected" : "MQTT down");
  $("#sfModel").textContent = h.model_version; $("#sfBroker").textContent = h.broker; $("#sfHost").textContent = h.platform.split("·")[0];
}
const TOPICS = [
  ["retail/store-001/pos/{sku}", "store → edge", 0, "POS sale line: qty, price"],
  ["retail/store-001/shelf/{sku}", "store → edge", 0, "shelf sensor: units on hand"],
  ["retail/store-001/clock", "store → edge", 1, "day open / day close → triggers batch inference"],
  ["retail/store-001/delivery/{sku}", "store → edge", 1, "goods received against a PO"],
  ["retail/store-001/edge/forecast/{sku}", "edge → all", "0 · retained", "7-day forecast + p10/p90"],
  ["retail/store-001/edge/alert/{sku}", "edge → all", "0 · retained", "severity, reorder point, order qty"],
  ["retail/store-001/edge/cmd/reorder/{sku}", "edge → store", 1, "purchase order to the supplier/dock"],
  ["retail/store-001/edge/status", "edge → all", "1 · retained · LWT", "online / offline (last will)"],
];
$("#topicMap").innerHTML = TOPICS.map(t => `<tr><td>${t[0]}</td><td>${t[1]}</td><td>${t[2]}</td><td>${t[3]}</td></tr>`).join("");

show(location.hash.slice(1) || "overview");
connect();
