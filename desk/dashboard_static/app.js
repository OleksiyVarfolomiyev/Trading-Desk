"use strict";

const POLL_MS = 5000;
const TABLES = ["performance", "accounts", "positions", "positions_stocks", "exposure", "wallets", "market_making", "strategies", "candidates", "mm_candidates"];
const sortState = {};          // table id -> {key, dir}; click a header to change it (the builders' order is the ranking)
let lastRendered = null;        // last_ok of the data currently drawn
let latest = null;              // latest /api/state document

// ---------------------------------------------------------------- formatting
const usdFormat = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const wholeUsdFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

function isNum(value) { return typeof value === "number" && isFinite(value); }

// Dollars rounded for reading, as desk/numbers.py: $0.42, $548, $2.11K, $12.3K, $2.53M, $1.2B.
function usd(value) {
  if (!isNum(value)) return "";
  const sign = value < 0 ? "-$" : "$", size = Math.abs(value), rounded = Number(size.toPrecision(3));
  for (const [scale, unit] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
    if (rounded >= scale) return sign + Number((rounded / scale).toPrecision(3)) + unit;
  }
  return sign + (size >= 10 ? Math.round(size).toLocaleString("en-US") : size.toFixed(2));
}
const signedUsd = usd;
// Whole dollars with thousands separators: $12,345 (no "+" on positive values).
function wholeUsd(value, signed = false) {
  if (!isNum(value)) return "n/a";
  const rounded = Math.round(value);
  const sign = rounded < 0 ? "-$" : "$";
  return sign + wholeUsdFormat.format(Math.abs(rounded));
}
// Shares and returns for reading (``digits`` is ignored; the size sets them), as desk/numbers.py ``pct``; positive
// values carry no "+" sign.
const readablePct = (value) => {  // as desk/numbers.py pct: 100%, 57%, 5.3%, 0.42%; trailing zeros dropped
  const number = value * 100, size = Math.abs(number);
  let text = size.toFixed(size >= 9.95 ? 0 : size >= 0.995 ? 1 : 2);
  if (text.includes(".")) text = text.replace(/0+$/, "").replace(/\.$/, "");
  return text === "0" ? "0%" : (number < 0 ? "-" : "") + text + "%";
};
function pct(value, digits = 1, signed = false) {
  return isNum(value) ? readablePct(value) : "";
}
function number(value) {
  if (!isNum(value)) return "";
  if (value !== 0 && Math.abs(value) < 0.001) return value.toExponential(3);
  return Number(value.toPrecision(6)).toLocaleString("en-US", { maximumFractionDigits: 6 });
}
function compactUsd(value) {
  if (!isNum(value)) return "n/a";
  const abs = Math.abs(value);
  const sign = value < 0 ? "-$" : "$";
  if (abs >= 1e6) return sign + (abs / 1e6).toFixed(2) + "M";
  if (abs >= 1e4) return sign + (abs / 1e3).toFixed(1) + "K";
  return sign + usdFormat.format(abs);
}

const FORMATS = {
  usd: { text: usd, num: true },
  susd: { text: signedUsd, num: true, signed: true },
  // Coloured by sign without a "+" prefix (holdings, values).
  vusd: { text: usd, num: true, signed: true },
  vpct: { text: (v) => pct(v, 1), num: true, signed: true },
  // Money at risk: red whenever above zero.
  rusd: { text: usd, num: true, tone: "neg" },
  rpct: { text: (v) => pct(v, 1), num: true, tone: "neg" },
  pct: { text: (v) => pct(v, 1), num: true },
  spct: { text: (v) => pct(v, 2, true), num: true, signed: true },
  // Leverage as a multiple of equity: 1.88×; signed for net (coloured, a short book negative).
  lev: { text: (v) => (isNum(v) ? v.toFixed(2) + "×" : ""), num: true },
  slev: { text: (v) => (isNum(v) ? v.toFixed(2) + "×" : ""), num: true, signed: true },
  num: { text: number, num: true },
  // Prices in USD at full precision (small coins need more than two decimals).
  price: { text: (v) => (isNum(v) ? "$" + number(v) : ""), num: true },
  num2: { text: (v) => (isNum(v) ? v.toFixed(2) : ""), num: true },
  // Basis points, coloured by sign (markouts).
  sbps: { text: (v) => (isNum(v) ? v.toFixed(1) : ""), num: true, signed: true },
  int: { text: (v) => (isNum(v) ? Math.round(v).toLocaleString("en-US") : ""), num: true },
  text: { text: (v) => (v === null || v === undefined ? "" : String(v)), num: false },
  // LONG green, SHORT red.
  side: { text: (v) => (v === null || v === undefined ? "" : String(v)), num: false, side: true },
};

function signClass(value) {
  if (!isNum(value) || value === 0) return "";
  return value > 0 ? "pos" : "neg";
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function ago(iso, now) {
  if (!iso) return "never";
  const seconds = Math.max(0, (now - new Date(iso)) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 90) return "1 min ago";
  if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
  if (seconds < 86400) return Math.round(seconds / 3600) + " h ago";
  return Math.round(seconds / 86400) + " d ago";
}
// The desk's clock (Eastern time, no zone name), as desk/numbers.py ``clock``: "10 am", "10:35 am".
function clock(iso) {
  if (!iso) return "";
  const at = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric",
    minute: "2-digit", hour12: true }).formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return `${at.hour}${at.minute === "00" ? "" : ":" + at.minute} ${String(at.dayPeriod).toLowerCase()}`;
}
function day(iso) { return iso ? String(iso).slice(0, 10) : ""; }
// A start date for reading, as desk/numbers.py ``since``: "May'25" once older than 60 days, else "Nov 3".
function sinceLabel(when) {
  const t = typeof when === "number" ? when : Date.parse(when);
  if (!isFinite(t)) return "";
  const d = new Date(t), month = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
  return Date.now() - t > 60 * 86400000 ? `${month}'${String(d.getUTCFullYear()).slice(2)}` : `${month} ${d.getUTCDate()}`;
}


// ---------------------------------------------------------------- feedback: what the reader does with the dashboard
// Every deliberate action (marking a highlight read or not important, hiding, minimizing or moving a section, changing
// the look) is a small event sent to the desk, so what is useful and what is noise can be read back later and the
// dashboard and the alerts improved from it (research/feedback_summary.py). Events wait in the browser until sent.
const FEEDBACK_QUEUE_KEY = "desk-feedback-queue";
const FEEDBACK_URL = window.DESK_SOURCE ? "/feedback" : "/api/feedback";
function feedback(kind, action, target, detail) {
  let queue = [];
  try { queue = JSON.parse(localStorage.getItem(FEEDBACK_QUEUE_KEY)) || []; } catch (e) {}
  queue.push({ at: new Date().toISOString(), kind, action, target: String(target || "").slice(0, 120),
    detail: detail ? String(detail).slice(0, 300) : undefined, view: document.documentElement.dataset.view || "tables",
    style: document.documentElement.dataset.style, mode: document.documentElement.dataset.mode });
  try { localStorage.setItem(FEEDBACK_QUEUE_KEY, JSON.stringify(queue.slice(-200))); } catch (e) {}
  flushFeedback();
}
let flushing = false;
async function flushFeedback() {
  if (flushing) return;
  let queue = [];
  try { queue = JSON.parse(localStorage.getItem(FEEDBACK_QUEUE_KEY)) || []; } catch (e) {}
  if (!queue.length) return;
  flushing = true;
  try {
    const response = await fetch(FEEDBACK_URL, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: queue }), credentials: "same-origin" });
    if (response.ok) {
      let rest = [];
      try { rest = (JSON.parse(localStorage.getItem(FEEDBACK_QUEUE_KEY)) || []).slice(queue.length); } catch (e) {}
      try { localStorage.setItem(FEEDBACK_QUEUE_KEY, JSON.stringify(rest)); } catch (e) {}
    }
  } catch (e) { /* offline or the endpoint is missing: kept for the next try */ }
  flushing = false;
}

// ---------------------------------------------------------------- tables
// ---------------------------------------------------------------- what the reader sees
// The builders (desk/dashboard.py, web/live.js) send complete tables; each view picks the columns a reader acts on,
// names them for the reader and formats them: [source column, shown name, format]. ``derive`` adds computed
// fields to a row first, ``keep`` drops rows, ``hover`` gives a row its tooltip.
const RISK_CAP = 0.33;  // desk/risk.py MAX_TOTAL_RISK_PCT
const ASSET_NAMES = { "crypto perp": "Crypto", "crypto spot": "Wallet tokens", "tradfi perp": "Stocks & commodities (HL)",
  "equity": "Stocks (IB)", "stock": "Stocks (IB)", "etf": "ETFs (IB)" };
const marketName = (symbol) => String(symbol || "").replace(/^[a-z0-9]+:/, "");  // "xyz:AMD" -> "AMD"
// The uPnL column reads in percent of cost (the desk's default) or in dollars: the $ / % switch in the top bar (as on HL).
let pnlUnit = "pct";
try { pnlUnit = localStorage.getItem("desk-unit") === "usd" ? "usd" : "pct"; } catch (e) {}
const HL_PAGE = "https://app.hyperliquid.xyz/trade/";
const hlLink = (market) => HL_PAGE + encodeURIComponent(String(market || "")).replace(/%3A/g, ":");
const stockLink = (symbol) => "https://finance.yahoo.com/quote/" + encodeURIComponent(String(symbol || ""));
const POSITION_VIEW = {
  derive: (r) => ({ Symbol: marketName(r.Symbol) }),
  // A symbol opens its page: HL's trade page for an HL market, a quote page for an IB stock (as on HL).
  link: (r) => (/^IB/.test(String(r.Account || "")) ? stockLink(r.Symbol) : hlLink(r.Symbol)),
  // No Side column: Value is signed and coloured (shorts negative, red), which already says the side.
  columns: [["Account"], ["Symbol"], ["Value"], ["uPnL (%)", "uPnL"], ["24h change (%)", "24h"],
    ["Funding (APR)"], ["Weight"]],
  // The crypto and the stocks tables sit one above the other: the same columns at the same widths, so Value, uPnL
  // and 24h line up (no column is collapsed or hidden in either).
  align: { "Account": 15, "Symbol": 18, "Value": 15, "uPnL": 13, "24h": 12, "Funding (APR)": 15, "Weight": 12 },
};
const VIEWS = {
  performance: { columns: [["Account"], ["Return 24h", "24h"], ["Return 7D", "7d"], ["Return 30D", "30d"],
    ["Return 90D", "90d"], ["Return 1Y", "1y"], ["Return since inception", "Since start"], ["Since", "Start"]] },
  accounts: {
    keep: (r) => !/wallet$/i.test(String(r.Account || "")),  // margin and leverage mean nothing for a wallet
    columns: [["Account"], ["% of desk"], ["Margin used / equity", "Margin used", "pct"], ["Gross / equity", "Gross", "lev"],
      ["Net / equity", "Net", "slev"], ["uPnL / equity", "uPnL", "spct"], ["Open risk", "Open risk", "rpct"]],
  },
  exposure: {
    derive: (r) => {
      const equity = isNum(r.Net) && isNum(r["Net / equity"]) && r["Net / equity"] ? r.Net / r["Net / equity"] : NaN;
      return { "Asset class": ASSET_NAMES[r["Asset class"]] || r["Asset class"],
        _long: equity ? r.Long / equity : NaN, _short: equity ? r.Short / equity : NaN };
    },
    columns: [["Asset class"], ["Net / equity", "Net", "slev"], ["Gross / equity", "Gross", "lev"], ["_long", "Long", "pct"],
      ["_short", "Short", "pct"]],
  },
  positions: POSITION_VIEW,
  positions_stocks: POSITION_VIEW,
  // Wallet tokens in the positions tables' layout: same columns, order and widths; their share of the desk where
  // the positions show their weight (tokens have no cost basis or funding, so those cells stay empty).
  wallets: {
    derive: (r) => ({ Account: r.Wallet, Symbol: r.Symbol }),
    hover: (r) => [r.Chain, r.Note].filter(Boolean).join(", "),
    columns: [["Account"], ["Symbol"], ["Value"], ["uPnL (%)", "uPnL"], ["24h change (%)", "24h"],
      ["Funding (APR)"], ["% of desk"]],
    align: { "Account": 15, "Symbol": 18, "Value": 15, "uPnL": 13, "24h": 12, "Funding (APR)": 15, "% of desk": 12 },
  },
  candidates: { link: (r) => hlLink(r.Coin), columns: [["Coin"], ["Basis"], ["vs BTC 30d"], ["vs BTC 7d"], ["From high 60d"], ["Funding 7d (APR)"],
    ["OI / volume"], ["Volume 24h"], ["Notes"]] },
  mm_candidates: { link: (r) => hlLink(r.Coin), columns: [["Coin"], ["Daily range 30d"], ["Longs pay 7d (APR)"], ["Own ladder edge per $1k"], ["Volume 24h"], ["Notes"]] },
  market_making: { derive: (r) => ({ Coin: marketName(r.Coin) }), link: (r) => hlLink(r.Coin),
    columns: [["Account"], ["Coin"], ["Side"], ["Status"], ["Since"], ["Ladder PnL"], ["Position vs base"], ["Fills"]] },
};

function columnsFor(view) {   // the uPnL column in dollars when the $ switch is on
  return pnlUnit === "usd" ? view.columns.map((column) => (column[0] === "uPnL (%)" ? ["uPnL", "uPnL"] : column)) : view.columns;
}
function applyView(id, spec, context) {
  const view = VIEWS[id];
  if (!view || !spec || !spec.columns || !spec.columns.length) return spec;
  const source = Object.fromEntries(spec.columns.map((column) => [column.key, column]));
  const shape = (row) => {
    if (!row) return row;
    const full = Object.assign({}, row, view.derive ? view.derive(row, context) : {});
    const out = {};
    columnsFor(view).forEach(([from, name]) => { out[name || from] = full[from]; });
    if (view.hover) out.__hover = view.hover(row);
    if (view.link) out.__link = view.link(row);
    return out;
  };
  // Derived fields start with _; an aligned table keeps every column, present in its data or not.
  const columns = columnsFor(view).filter(([from]) => view.align || from in source || from.startsWith("_"))
    .map(([from, name, fmt]) => ({ key: name || from, fmt: fmt || (source[from] || {}).fmt, emphasis: (source[from] || {}).emphasis }));
  const rows = spec.rows.filter((row) => !view.keep || view.keep(row)).map(shape);
  let total = spec.total ? shape(spec.total) : null;
  if (total && columns.length && !total[columns[0].key]) total[columns[0].key] = "Total";
  return Object.assign({}, spec, { columns, rows, total });
}


// ---------------------------------------------------------------- tables drawn as charts
// ?view=charts draws every table as a chart (?view=tables returns): the same builders, rows and formats, so the two
// views always agree. Percent and money axes follow the cell format; hover carries the table's own text.
const CHART_VIEWS = {
  performance: { kind: "grouped", label: "Account", series: ["24h", "7d", "30d", "90d", "Since start"], by: "window" },
  accounts: { kind: "grouped", label: "Account", series: ["Gross", "Net"] },
  exposure: { kind: "exposure", label: "Asset class", long: "Long", short: "Short", net: "Net" },
  positions: { kind: "bars", label: "Symbol", value: "Value", text: "uPnL" },
  positions_stocks: { kind: "bars", label: "Symbol", value: "Value", text: "uPnL" },
  wallets: { kind: "bars", label: "Symbol", value: "Value", text: "uPnL" },
  market_making: { kind: "bars", label: "Coin", value: "Ladder PnL", text: "Fills" },
};
const LABEL_FONT = (size = 12, color) => ({ size, color: color || token("--ink-2") });   // the value next to each bar's end
const BAR_CAP = 3;   // the longest a bar is drawn, in times the second largest
const INSIDE_INK = () => (document.documentElement.dataset.mode === "light" ? "#ffffff" : token("--ink"));   // names inside bars: white on the light page
// A name goes inside its bar only when the bar is long enough, in pixels, to hold it (13px text, about 7.5px a character).
function nameFits(share, name, host, rightMargin) {
  const plotPx = Math.max(200, (host.clientWidth || 600) - 20 - rightMargin) * 0.85;   // the longest bar's room, with padding
  return share * plotPx >= String(name).length * 7.5 + 16;
}
const isChartView = () => document.documentElement.dataset.view === "charts";

function chartAxis(format) {  // multiplier and axis look for a cell format
  if (["pct", "spct", "vpct", "rpct"].includes(format)) return { mult: 100, ticksuffix: "%" };
  if (["lev", "slev"].includes(format)) return { mult: 1, ticksuffix: "×", tickformat: ".1f" };
  if (["usd", "susd", "vusd", "rusd"].includes(format)) return { mult: 1, tickformat: "$~s" };
  return { mult: 1 };
}

function chartPlan(id, spec) {
  const preset = CHART_VIEWS[id];
  const formatOf = (key) => (spec.columns.find((column) => column.key === key) || {}).fmt;
  if (preset) return Object.assign({ columns: spec.columns }, preset);
  // Any other table: its first column names the bars, its key (or first) number column is the bar.
  const numeric = spec.columns.filter((column) => (FORMATS[column.fmt] || FORMATS.text).num);
  const key = numeric.find((column) => column.emphasis && column.emphasis !== "none") || numeric[0];
  return key ? { kind: "bars", label: spec.columns[0].key, value: key.key, text: null } : null;
}

function renderTableChart(id, spec) {
  const host = document.getElementById("t-" + id);
  const plan = chartPlan(id, spec);
  if (typeof Plotly === "undefined" || !plan) return false;
  const rows = spec.rows.slice();
  const formatOf = (key) => (spec.columns.find((column) => column.key === key) || {}).fmt;
  const text = (key, value) => (FORMATS[formatOf(key)] || FORMATS.text).text(value);
  const labels = rows.map((row) => String(row[plan.label] ?? ""));
  // A symbol held in two accounts gets its account in the label.
  const name = (row, index) => labels.filter((l) => l === labels[index]).length > 1 && row.Account
    ? `${labels[index]} (${row.Account})` : labels[index];
  // A category axis merges equal names onto one row (bars and labels would pile up): every row gets its own name.
  const names = rows.map(name);
  const seen = {};
  names.forEach((n, i) => { seen[n] = (seen[n] || 0) + 1; if (seen[n] > 1) names[i] = `${n} #${seen[n]}`; });
  const colors = [1, 2, 3, 4, 5].map((i) => token("--series-" + i));
  const good = token("--good-text") || token("--good"), bad = token("--bad-text") || token("--critical");
  let traces = [], layout = {}, height = 0;
  if (plan.kind === "grouped") {
    const series = plan.series.filter((key) => rows.some((row) => isNum(row[key])));
    const axis = chartAxis(formatOf(series[0]));
    if (plan.by === "window") {   // one group per window, one bar per account inside it
      traces = rows.map((row, i) => ({ type: "bar", orientation: "h", name: names[i], y: series,
        x: series.map((key) => (isNum(row[key]) ? row[key] * axis.mult : null)), marker: { color: colors[i % colors.length] },
        text: series.map((key) => text(key, row[key])), textposition: "outside", cliponaxis: false,
        textfont: LABEL_FONT(12, series.map((key) => (row[key] < 0 ? bad : good))),
        hovertext: series.map((key) => `${names[i]}, ${key}: ${text(key, row[key])}`), hoverinfo: "text+y" }));
      layout = { barmode: "group", xaxis: Object.assign({ zeroline: true }, axis) };
      height = Math.max(220, series.length * (rows.length * 16 + 18) + 80);
    } else traces = series.map((key, i) => ({ type: "bar", orientation: "h", name: key, y: names,
      x: rows.map((row) => (isNum(row[key]) ? row[key] * axis.mult : null)), marker: { color: colors[i % colors.length] },
      text: rows.map((row) => text(key, row[key])), textposition: "outside", cliponaxis: false,
      textfont: LABEL_FONT(12, rows.map((row) => (row[key] < 0 ? bad : good))),
      hovertext: rows.map((row) => `${key}: ${text(key, row[key])}`), hoverinfo: "text+y" }));
    if (plan.by !== "window") {
      layout = { barmode: "group", xaxis: Object.assign({ zeroline: true }, axis) };
      height = Math.max(180, rows.length * (series.length * 14 + 14) + 80);
    }
  } else if (plan.kind === "exposure") {
    const axis = chartAxis(formatOf(plan.long));
    const size = (row, key) => (isNum(row[key]) ? Math.abs(row[key]) : 0);
    const biggest = Math.max(...rows.map((row) => Math.max(size(row, plan.long), size(row, plan.short)))) || 1;
    // Each class's name goes inside its larger bar when that bar has room, else it joins that bar's figure.
    const carrier = rows.map((row) => (size(row, plan.long) >= size(row, plan.short) ? plan.long : plan.short));
    const roomy = rows.map((row, i) => nameFits(size(row, carrier[i]) / biggest, names[i], host, 90));
    const bar = (key, color, title) => ({ type: "bar", orientation: "h", name: title, y: names, marker: { color },
      x: rows.map((row) => (isNum(row[key]) ? Math.abs(row[key]) * axis.mult * (key === plan.short ? -1 : 1) : null)),
      text: rows.map((row, i) => (carrier[i] === key && !roomy[i] ? `${names[i]} · ` : "") + text(key, row[key])),
      textposition: "outside", cliponaxis: false, textfont: LABEL_FONT(12, color),
      hovertext: rows.map((row) => `${title}: ${text(key, row[key])}`), hoverinfo: "text+y" });
    traces = [bar(plan.long, good, "Long"), bar(plan.short, bad, "Short"),
      { type: "scatter", mode: "markers+text", name: "Net", y: names, textposition: "middle right",
        textfont: LABEL_FONT(12, rows.map((row) => (row[plan.net] < 0 ? bad : good))),
        text: rows.map((row) => text(plan.net, row[plan.net])), marker: { color: token("--ink"), symbol: "diamond", size: 10 },
        x: rows.map((row) => (isNum(row[plan.net]) ? row[plan.net] * axis.mult : null)),
        hovertext: rows.map((row) => `Net: ${text(plan.net, row[plan.net])}`), hoverinfo: "text+y" }];
    traces.forEach((trace) => { trace.showlegend = false; });
    const inside = rows.map((row, i) => (roomy[i] ? { x: 0, y: names[i], text: names[i], showarrow: false,
      xanchor: carrier[i] === plan.short ? "right" : "left", xshift: carrier[i] === plan.short ? -6 : 6,
      font: { size: 13, color: INSIDE_INK() } } : null)).filter(Boolean);
    layout = { barmode: "relative", xaxis: Object.assign({ zeroline: true }, axis), annotations: inside, insideLabels: true,
      showlegend: false, margin: { r: 90 } };
    height = Math.max(180, rows.length * 44 + 80);
  } else {  // one bar per row, green above zero, red below, ordered by size
    const order = rows.map((row, index) => index).filter((index) => isNum(rows[index][plan.value]))
      .sort((a, b) => Math.abs(rows[b][plan.value]) - Math.abs(rows[a][plan.value]));
    const axis = chartAxis(formatOf(plan.value));
    // One holding can dwarf the rest (a $2K token among dust): a bar is drawn at most BAR_CAP times the second largest,
    // marked "»" with its true value in the label, so the small ones stay readable.
    const sizes = order.map((i) => Math.abs(rows[i][plan.value]));
    const cap = sizes.length > 2 ? sizes[1] * BAR_CAP : Infinity;
    const drawn = (i) => Math.sign(rows[i][plan.value]) * Math.min(Math.abs(rows[i][plan.value]), cap);
    const clipped = (i) => Math.abs(rows[i][plan.value]) > cap;
    const biggest = Math.max(...order.map((i) => Math.abs(drawn(i)))) || 1;
    const fits = (i) => nameFits(Math.abs(drawn(i)) / biggest, names[i], host, plan.text ? 130 : 90);
    const figures = (i) => [text(plan.value, rows[i][plan.value]), plan.text ? text(plan.text, rows[i][plan.text]) : ""]
      .filter(Boolean).join(" · ");
    traces = [{ type: "bar", orientation: "h", y: order.map((i) => names[i]), x: order.map((i) => drawn(i) * axis.mult),
      marker: { color: order.map((i) => (rows[i][plan.value] < 0 ? bad : good)) }, showlegend: false,
      // A short bar has no room for its name inside: the name joins the figures at its end instead of overlapping them.
      text: order.map((i) => (clipped(i) ? "» " : "") + (fits(i) ? figures(i) : `${names[i]} · ${figures(i)}`)),
      textposition: "outside", cliponaxis: false,
      textfont: LABEL_FONT(13, order.map((i) => (rows[i][plan.value] < 0 ? bad : good))),
      hovertext: order.map((i) => `${plan.value}: ${text(plan.value, rows[i][plan.value])}`
        + (plan.text ? `, ${plan.text}: ${text(plan.text, rows[i][plan.text])}` : "")), hoverinfo: "text+y" }];
    // The row's name sits inside its bar, from the zero line outwards (left of zero for a short), not on the axis.
    const inside = order.filter(fits).map((i) => {
      const value = rows[i][plan.value] * axis.mult;
      return { x: 0, y: names[i], text: names[i], showarrow: false, xanchor: value < 0 ? "right" : "left",
        xshift: value < 0 ? -6 : 6, font: { size: 13, color: INSIDE_INK() } };
    });
    layout = { xaxis: Object.assign({ zeroline: true }, axis), annotations: inside, insideLabels: true };
    height = Math.max(200, order.length * 34 + 80);   // positions read large: a tall row per bar
    // Room at the right for the longest end label (a short bar's label carries its name too).
    layout.margin = { r: Math.min(320, Math.max(plan.text ? 130 : 90, Math.max(...traces[0].text.map((t) => t.length)) * 7 + 24)) };
  }
  host.replaceChildren();
  const node = el("div", { class: "chart", id: "tc-" + id, style: `height:${height}px` });
  host.append(node);
  const margin = Object.assign({ l: 90, r: 90, t: plan.kind === "grouped" ? 30 : 8, b: 36 }, layout.margin || {});
  const base = baseLayout(Object.assign({ hovermode: "closest" }, layout, { margin }));
  // Values sit at the bar ends: no x labels, grid, zero line or axis line.
  base.xaxis = Object.assign({}, base.xaxis, layout.xaxis || {}, { showspikes: false, showticklabels: false, showgrid: false,
    zeroline: false, showline: false, ticks: "" });
  // Every row gets its label: a category axis with one tick per bar (Plotly would otherwise thin crowded labels).
  const shown = traces[0] && traces[0].y ? traces[0].y : names;
  base.yaxis = Object.assign({}, base.yaxis, { type: "category", autorange: "reversed", automargin: true, showgrid: false, zeroline: false, showline: false,
    tickmode: "array", tickvals: shown, ticktext: shown, tickfont: { color: token("--ink-2"), size: plan.kind === "bars" ? 13 : 11 } });
  if (layout.insideLabels) {   // names are in the bars: no axis labels; the left margin only holds a short's end text
    base.yaxis.showticklabels = false;
    const shorts = traces.some((trace) => trace.type === "bar" && (trace.x || []).some((value) => value < 0));
    base.margin = Object.assign({}, base.margin, { l: shorts ? 150 : 12 });
    delete base.insideLabels;
  }
  Plotly.newPlot(node, traces, base, CONFIG).then(markNumericNotes);
  return true;
}

// A displayed zero amount ($0.00, -$0.00, 0%, 0.00) is an empty cell; a whole-number 0 (a count) stays.
const ZERO_TEXT = /^-?(\$0(\.0+)?|0(\.0+)?%|0\.0+)$/;
function renderTable(id, spec) {
  const original = spec;
  const host = document.getElementById("t-" + id);
  if (!host) return;
  host.replaceChildren();
  // An empty table has nothing to act on: its card is hidden until it has rows.
  const card = host.closest(".card");
  const empty = !spec || !spec.columns || !spec.columns.length || !(spec.rows || []).length;
  if (card) card.hidden = empty;
  if (empty) return;
  if (isChartView() && renderTableChart(id, spec)) return;   // ?view=charts: the table drawn as a chart
  // A text column with the same value in every row is said once, above the table; an empty one is left out.
  const shared = [];
  const align = (VIEWS[id] || {}).align;
  if (spec.rows.length > 1 && !align) {
    spec = Object.assign({}, spec, { columns: spec.columns.filter((column, i) => {
      const format = FORMATS[column.fmt] || FORMATS.text;
      if (i === 0) return true;
      if (format.num) {  // a number column empty or zero in every row says nothing: left out
        return spec.rows.some((row) => isNum(row[column.key]) && !ZERO_TEXT.test(format.text(row[column.key])));
      }
      const texts = new Set(spec.rows.map((row) => format.text(row[column.key])));
      const [only] = texts;
      if (texts.size !== 1 || (spec.total && ![only, ""].includes(format.text(spec.total[column.key])))) return true;
      if (only) shared.push(`${column.key}: ${only}`);
      return false;
    }) });
  }
  const state = sortState[id];
  let rows = spec.rows.slice();
  if (state) {
    rows.sort((a, b) => {
      const x = a[state.key], y = b[state.key];
      if (isNum(x) && isNum(y)) return state.dir * (x - y);
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return state.dir * String(x).localeCompare(String(y));
    });
  }
  const head = el("tr", {}, spec.columns.map((column) => {
    const format = FORMATS[column.fmt] || FORMATS.text;
    const sorted = state && state.key === column.key ? (state.dir > 0 ? "ascending" : "descending") : "none";
    const th = el("th", { class: [format.num ? "num" : "", column.emphasis ? "key" : ""].filter(Boolean).join(" "),
      "aria-sort": sorted, tabindex: "0", title: "Click to sort", text: column.key });
    const toggle = () => {
      const current = sortState[id];
      const dir = current && current.key === column.key ? -current.dir : (format.num ? -1 : 1);
      sortState[id] = { key: column.key, dir };
      renderTable(id, original);
    };
    th.addEventListener("click", toggle);
    th.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } });
    return th;
  }));
  // Key columns (desk/emphasis.py): tinted, with the standout value of each picked by its mode.
  const standout = {};
  spec.columns.forEach((column) => {
    if (!column.emphasis || column.emphasis === "none") return;
    let best = null, bestScore = null;
    rows.forEach((row, index) => {
      const value = row[column.key];
      if (!isNum(value)) return;
      const score = column.emphasis === "abs" ? Math.abs(value) : column.emphasis === "max" ? value : -value;
      if (bestScore === null || score > bestScore) { best = index; bestScore = score; }
    });
    standout[column.key] = best;
  });
  const cell = (row, column, index) => {
    const format = FORMATS[column.fmt] || FORMATS.text;
    const value = row[column.key];
    const tone = format.signed ? signClass(value)
      : format.side ? (value === "LONG" ? "pos" : value === "SHORT" ? "neg" : "")
      : format.tone && isNum(value) && value > 0 ? format.tone : "";
    const key = column.emphasis ? (index !== undefined && standout[column.key] === index ? "key key-top" : "key") : "";
    const classes = [format.num ? "num" : "", tone, key].filter(Boolean).join(" ");
    const text = format.text(value);
    // Any figure in a cell (a note with "$1.5K", a text column of amounts) is hidden with the numbers, not only number columns.
    const withDigits = column.key !== "Symbol" && column.key !== "Coin" && /\d/.test(text) ? " digits" : "";
    if (row.__link && (column.key === "Symbol" || column.key === "Coin") && text) {   // the symbol opens its page
      const link = el("a", { class: "symbol-link", href: row.__link, target: "_blank", rel: "noopener noreferrer", text });
      return el("td", { class: classes }, [link]);
    }
    if (!format.num && KIND_WORDS.test(text)) { KIND_WORDS.lastIndex = 0; return el("td", { class: classes }, kindMarks(text)); }
    KIND_WORDS.lastIndex = 0;
    return el("td", { class: classes + withDigits, text: ZERO_TEXT.test(text) ? "" : text });
  };
  const body = el("tbody", {}, rows.map((row, index) => el("tr", row.__hover ? { title: row.__hover } : {},
    spec.columns.map((column) => cell(row, column, index)))));
  const parts = [el("thead", {}, head), body];
  if (spec.total && spec.rows.length > 1) {  // the total of a single row only repeats it
    parts.push(el("tfoot", {}, el("tr", { class: "total" }, spec.columns.map((column) => cell(spec.total, column)))));
  }
  if (shared.length) host.append(el("p", { class: "table-note", text: shared.join(" · ") }));
  if (align) {  // fixed widths shared with the tables it is aligned with
    parts.unshift(el("colgroup", {}, spec.columns.map((column) => el("col", { style: `width:${align[column.key] || 10}%` }))));
  }
  host.append(el("table", align ? { class: "aligned" } : {}, parts));
}

// ---------------------------------------------------------------- tiles
function tile(label, value, sub, extra) {  // extra.title: the definition, on hover only
  return el("div", { class: "tile", "data-label": label, title: (extra && extra.title) || "" }, [
    el("div", { class: "label", text: label }),
    el("div", { class: "value " + (extra && extra.valueClass ? extra.valueClass : ""), text: value }),
    extra && extra.line && extra.line.text ? el("div", { class: "line " + (extra.line.cls || ""), text: extra.line.text,
      title: extra.line.title || "" }) : null,
    sub ? el("div", { class: "sub", text: sub }) : null,
  ]);
}

// "24h $123 (1.2%)", net of deposits, withdrawals and transfers; per-account parts on hover.
function dayChangeLine(day) {
  if (!day || !isNum(day.change)) return { text: "24h change n/a", cls: "muted" };
  // The 24h change as a %; the dollars, per account, are on hover.
  const parts = Object.entries(day.parts || {}).map(([name, value]) => `${name}: ${usd(value)}`);
  const missing = (day.missing || []).length ? [`Not included: ${day.missing.join(", ")}`] : [];
  return {
    // A change that rounds to 0% says nothing: no line (the dollars stay on hover of the value).
    text: isNum(day.pct) && pct(day.pct) === "0%" ? "" : `24h ${isNum(day.pct) ? pct(day.pct) : usd(day.change)}${missing.length ? " *" : ""}`,
    cls: signClass(day.change),
    title: [`24h ${usd(day.change)}, net of deposits, withdrawals and transfers.`, ...parts, ...missing].join("\n"),
  };
}

function renderTiles(k) {
  const r = k.returns || {};
  const mw = k.money_weighted || {};
  const tiles = [
    // Read left to right: what needs attention (drawdown, leverage) before the results.
    tile("Drawdown now", pct(k.drawdown, 1), k.drawdown < 0 ? `peak ${sinceLabel(k.peak_date)}` : "at peak",
      { valueClass: k.drawdown <= -0.10 ? "neg" : "" }),
    tile("Gross leverage", isNum(k.gross_leverage) ? k.gross_leverage.toFixed(2) + "×" : "n/a",
      isNum(k.net_leverage) ? `net ${k.net_leverage.toFixed(2)}×` : "", { title: "Of trading equity." }),

    tile("Desk value", wholeUsd(k.desk_value), "", { line: dayChangeLine(k.day_change) }),
    tile("Return 30D", pct(r["30D"], 1, true),
      `7D ${pct(r["7D"], 1, true) || "n/a"} · 90D ${pct(r["90D"], 1, true) || "n/a"}`,
      { valueClass: signClass(r["30D"]) }),
    // The two returns since start, with the dollars under them: money-weighted (what the money made, a loss on a
    // small balance and a gain on a large one net out) and time-weighted (the strategy, deposits aside).
    tile("MW return", pct(mw.return, 1, true) || "n/a",
      [isNum(mw.pnl) ? `PnL ${usd(mw.pnl)}` : "", isNum(k.unrealized) ? `uPnL ${usd(k.unrealized)}` : ""].filter(Boolean).join(" · "),
      { valueClass: signClass(mw.return), title: "Money-weighted return since start, HL + IB; PnL net of deposits and withdrawals." }),
    tile("TW return", pct(r.since_start, 1, true), `since ${sinceLabel(k.since)}`,
      { valueClass: signClass(r.since_start), title: "Time-weighted return since start, wallets included." }),  ];
  arrangeTiles(document.getElementById("tiles"), tiles);
}
// The key-number tiles are windows too: drag one onto another to swap, minimize to label and value, save as an image, hide
// (the Sections menu brings hidden ones back). Order, minimized and hidden are remembered with the layout.
function arrangeTiles(host, tiles) {
  const state = layoutState();
  const hidden = new Set(state.tileHidden || []), minimized = new Set(state.tileMin || []), order = state.tileOrder || [];
  const rank = (tile) => { const i = order.indexOf(tile.dataset.label); return i < 0 ? 1e6 : i; };
  const shown = tiles.filter((tile) => !hidden.has(tile.dataset.label)).sort((a, b) => rank(a) - rank(b));
  const remember = (change) => { const s = layoutState(); change(s); saveLayout(s); };
  shown.forEach((tile) => {
    const label = tile.dataset.label;
    tile.classList.toggle("tile-min", minimized.has(label));
    const tool = (cls, title, text, action) => {
      const button = el("button", { type: "button", class: "card-tool " + cls, title, "aria-label": title + ": " + label, text });
      button.addEventListener("click", (event) => { event.stopPropagation(); action(); });
      return button;
    };
    tile.append(el("span", { class: "card-tools tile-tools" }, [
      tool("card-minimize", "Minimize", "–", () => {
        const on = !tile.classList.contains("tile-min");
        tile.classList.toggle("tile-min", on);
        remember((s) => { s.tileMin = (s.tileMin || []).filter((x) => x !== label).concat(on ? [label] : []); });
        feedback("tile", on ? "minimize" : "restore", label);
      }),
      tool("card-save", "Save as image", "⤓", () => saveTile(tile, label)),
      tool("card-hide", "Hide", "×", () => {
        remember((s) => { s.tileHidden = (s.tileHidden || []).concat([label]); });
        feedback("tile", "hide", label);
        tile.remove();
        drawSectionsMenu();
      }),
    ]));
    tile.addEventListener("mousedown", (event) => { tile.draggable = !event.target.closest("button, a, input"); });
    tile.addEventListener("dragstart", (event) => {
      if (!tile.draggable) { event.preventDefault(); return; }
      event.dataTransfer.setData("text/plain", "tile:" + label);
      event.dataTransfer.effectAllowed = "move";
      tile.classList.add("dragging");
    });
    tile.addEventListener("dragend", () => { tile.draggable = false; tile.classList.remove("dragging"); host.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target")); });
    tile.addEventListener("dragover", (event) => { event.preventDefault(); tile.classList.add("drop-target"); });
    tile.addEventListener("dragleave", () => tile.classList.remove("drop-target"));
    tile.addEventListener("drop", (event) => {
      event.preventDefault();
      tile.classList.remove("drop-target");
      const [kind, from] = event.dataTransfer.getData("text/plain").split(/:(.*)/s);
      const moving = kind === "tile" && [...host.children].find((node) => node.dataset.label === from);
      if (!moving || moving === tile) return;
      const mine = [...host.children], a = mine.indexOf(moving), b = mine.indexOf(tile);
      if (a < b) tile.after(moving); else tile.before(moving);   // the dragged tile takes the target's place
      remember((s) => { s.tileOrder = [...host.children].map((node) => node.dataset.label); });
      feedback("tile", "move", from, "before " + label);
    });
  });
  host.replaceChildren(...shown);
}

function renderAccountStatus(rows, now) {
  const host = document.getElementById("accounts-status");
  // Only an account that cannot be read gets a chip: a healthy connection says nothing.
  host.replaceChildren(...(rows || []).filter((row) => !row.ok).map((row) => el("span", { class: "chip", title: row.problem || "" }, [
    el("span", { class: "icon " + (row.ok ? "good" : "bad"), "aria-hidden": "true", text: row.ok ? "✓" : "!" }),
    el("span", { text: `${row.name}: ${row.ok ? "read" : "unreadable" + (row.last_seen ? ", last read " + ago(row.last_seen, now) : "")}` }),
  ])));
}

// How loud a highlight is (desk/notebook_highlights.py ``severity``): "critical" rows call for action now and sit
// first in a red band; "watch" rows get an amber marker.
const CRITICAL_TITLES = /cut gross|margin guard|not reporting|over cap|liquidat|^major hack$|^squeeze/i;
const WATCH_TITLES = /^alt rally$|^stock dip$|near cap|^hack$|^burn approaching$|^burn$|^unlock$/i;
function severity(title, text) {
  if (CRITICAL_TITLES.test(title) || (/^regime$/i.test(title) && /\bhigh\b/.test(text))) return "critical";
  if (WATCH_TITLES.test(title) || /^regime$/i.test(title)) return "watch";
  return "";
}

// Highlights can be answered: "✓" marks one read (it disappears until its text changes), "×" says it is not important (that
// area is not shown again). Both are remembered in the browser; the Sections menu restores them.
const HIGHLIGHTS_KEY = "desk-highlights";
function highlightState() {
  try { return Object.assign({ read: {}, deleted: [] }, JSON.parse(localStorage.getItem(HIGHLIGHTS_KEY)) || {}); }
  catch (e) { return { read: {}, deleted: [] }; }
}
function saveHighlightState(state) {
  try { localStorage.setItem(HIGHLIGHTS_KEY, JSON.stringify(state)); } catch (e) {}
}
function redrawHighlights() {
  if (!latest || !latest.data) return;
  const hl = latest.data.highlights || {}, hover = hl.hover || {};
  renderHighlights("hl-well", hl.well, hover);
  renderHighlights("hl-attention", hl.attention, hover);
  if (typeof drawSectionsMenu === "function") drawSectionsMenu();
}
function renderHighlights(id, rows, hover = {}) {
  const host = document.getElementById(id);
  const state = highlightState();
  // Areas the reader has dismissed over and over (desk/usage_model.py) are folded away, never a critical row.
  const quiet = new Set((((latest && latest.data && latest.data.usage_model) || {}).quiet) || []);
  const open = host.dataset.showQuiet === "1";
  const answered = (rows || []).filter(([area, text]) => !state.deleted.includes(area) && !state.read[area + "|" + text]);
  const folded = answered.filter(([area, text]) => quiet.has(area) && severity(area, text) !== "critical");
  const shown = open ? answered : answered.filter((row) => !folded.includes(row));
  if (!shown.length && !folded.length) {
    host.replaceChildren(el("li", {}, el("span", { class: "empty", text: "Nothing to report." })));
    return;
  }
  const rank = { critical: 0, watch: 1, "": 2 };
  // A dashboard line, not a sentence: one trailing period goes (several sentences keep theirs)
  const tidy = (text) => (/\.\s+\S/.test(String(text)) ? String(text) : String(text).replace(/\.\s*$/, ""));
  const ordered = shown.map(([area, text]) => [area, tidy(text), severity(area, text)])
    .sort((a, b) => rank[a[2]] - rank[b[2]]);  // stable: same-level rows keep their order
  const foot = folded.length ? [el("li", {}, [el("button", { type: "button", class: "quiet-toggle", title: "Areas you have dismissed several times: " + [...new Set(folded.map((r) => r[0]))].join(", "),
    text: open ? "hide quiet" : `${folded.length} quiet`, onclick: null })])] : [];
  if (foot.length) foot[0].firstChild.addEventListener("click", () => { host.dataset.showQuiet = open ? "0" : "1"; redrawHighlights(); });
  host.replaceChildren(...ordered.map(([area, text, level]) => {
    const act = (label, symbol, change) => {
      const button = el("button", { type: "button", class: "hl-act", title: label, "aria-label": `${label}: ${area}`, text: symbol });
      button.addEventListener("click", () => { const s = highlightState(); change(s); saveHighlightState(s); redrawHighlights(); });
      return button;
    };
    return el("li", Object.assign(level ? { class: level } : {}, hover[area] ? { title: hover[area] } : {}), [
      el("span", { class: "area", text: (level === "critical" ? "⚠ " : "") + area }), el("span", { class: "text" }, linkSymbols(text).flatMap((part) => (typeof part === "string" ? kindMarks(part) : [part]))),
      el("span", { class: "hl-actions" }, [
        act("Read: hide until it changes", "✓", (s) => { s.read[area + "|" + text] = Date.now(); feedback("highlight", "read", area, text); }),
        act("Not important: stop showing this", "×", (s) => { if (!s.deleted.includes(area)) s.deleted.push(area); feedback("highlight", "not important", area, text); }),
      ]),
    ]);
  }), ...foot);
}

// ---------------------------------------------------------------- charts
function token(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function withAlpha(hex, alpha) {
  const value = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${alpha})`;
}

function baseLayout(extra) {
  const ink = token("--ink-2"), muted = token("--muted"), grid = token("--grid"), axis = token("--axis");
  return Object.assign({
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: token("--font") || "system-ui, -apple-system, Segoe UI, sans-serif", size: 12, color: ink },
    margin: { l: 56, r: 72, t: 30, b: 36 },
    hovermode: "x unified",
    hoverlabel: { bgcolor: token("--surface"), bordercolor: axis, font: { color: token("--ink") } },
    legend: { orientation: "h", x: 0, y: 1, yanchor: "bottom", font: { color: ink } },   // above the plot, inside the top margin
    xaxis: { gridcolor: grid, linecolor: axis, tickfont: { color: muted }, showspikes: true,
             spikemode: "across", spikethickness: 1, spikecolor: axis, spikedash: "solid" },
    yaxis: { gridcolor: grid, linecolor: axis, zerolinecolor: axis, tickfont: { color: muted } },
  }, extra);
}

const CONFIG = { displayModeBar: false, responsive: true };

function endLabels(series, format) {
  return series.filter((s) => s.y.length).map((s) => ({
    x: s.x[s.x.length - 1], y: s.y[s.y.length - 1], xanchor: "left", xshift: 6, showarrow: false,
    text: format(s.y[s.y.length - 1]), font: { color: token("--ink-2"), size: 11 },
  }));
}

// End labels closer than a line apart (a stacked total over its top layer, two lines ending near each other) are
// pushed apart vertically, in rendered pixels, keeping their order; each stays as close to its line as it can.
const END_LABEL_GAP_PX = 14;
function spreadEndLabels(id) {
  const gd = document.getElementById(id);
  const axis = gd && gd._fullLayout && gd._fullLayout.yaxis;
  if (!axis || !axis.range || !axis._length) return;
  const [low, high] = axis.range.map(Number);
  const scale = axis._length / (high - low || 1);
  const annotations = (gd.layout.annotations || []).map((a) => Object.assign({}, a));
  const ends = annotations.filter((a) => a.xshift === 6 && a.xanchor === "left" && typeof a.y === "number")
    .map((a) => ({ a, at: (a.y - low) * scale })).sort((p, q) => q.at - p.at);
  let changed = false;
  ends.forEach((label, i) => {
    label.place = i && ends[i - 1].place - label.at < END_LABEL_GAP_PX ? ends[i - 1].place - END_LABEL_GAP_PX : label.at;
  });
  ends.forEach((label) => {
    const shift = Math.round(label.place - label.at);
    if ((label.a.yshift || 0) !== shift) { label.a.yshift = shift; changed = true; }
  });
  if (changed) Plotly.relayout(gd, { annotations });
}

function renderCharts(charts) {
  if (typeof Plotly === "undefined" || !charts) return;
  const colors = [1, 2, 3, 4, 5].map((i) => token("--series-" + i));
  const value = (charts.value || []).map((s, i) => ({
    type: "scatter", mode: "lines", name: s.name, x: s.x, y: s.y, stackgroup: "value",
    line: { width: 2, color: colors[i % colors.length] }, fillcolor: withAlpha(colors[i % colors.length], 0.18),
    hovertemplate: "%{fullData.name}: $%{y:,.0f}<extra></extra>",
  }));
  // Not stacked: it covers only the days wallet values are known (recorded, or rebuilt before
  // recording began), never implying zero outside them.
  const withWallets = charts.value_with_wallets;
  if (withWallets && withWallets.x.length) {
    const note = withWallets.note ? `<br><span style="font-size:11px">${withWallets.note}</span>` : "";
    value.push({ type: "scatter", mode: "lines", name: withWallets.name, x: withWallets.x, y: withWallets.y,
      line: { width: 2, dash: "dot", color: token("--ink-2") },
      hovertemplate: `%{fullData.name}: $%{y:,.0f}${note}<extra></extra>` });
  }
  // Collapse stretches with no trading on any account, as the HL account charts do, and say so.
  const idle = charts.value_idle || [];
  const month = (d) => new Date(d + "T00:00:00Z").toLocaleString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
  // Short dollar ticks ($10k), so the axis labels fit; the end labels and hover carry the detail.
  const valueLayout = baseLayout({ yaxis: Object.assign(baseLayout().yaxis, { tickformat: "$~s" }) });
  if (idle.length) {
    valueLayout.xaxis = Object.assign(valueLayout.xaxis, { rangebreaks: idle.map((p) => ({ bounds: [p[0], p[1]] })) });
    valueLayout.margin = Object.assign({}, valueLayout.margin, { b: 56 });
    valueLayout.annotations = [{
      xref: "paper", yref: "paper", x: 0.5, y: -0.16, xanchor: "center", yanchor: "top", showarrow: false,
      text: idle.map((p) => `Inactive ${month(p[0])} to ${month(p[1])} (${p[2]} days hidden)`).join(" · "),
      font: { color: token("--muted"), size: 11 },
    }];
  }
  // End values, as on the return chart: each stacked layer's own value at the top of its layer, then the
  // line with wallets. Labels sit on the plot's right edge (the layers can end on different days).
  const money = usd;  // end values rounded for reading ($11.3K), as everywhere on the desk
  let stackTop = 0;
  const valueEnds = value.filter((trace) => trace.y && trace.y.length).map((trace) => {
    const own = trace.y[trace.y.length - 1];
    const y = trace.stackgroup ? (stackTop += own) : own;
    return { x: trace.x[trace.x.length - 1], y, xanchor: "left", xshift: 6, showarrow: false, text: money(own),
             font: { color: trace.line.color, size: 11 } };
  });
  valueLayout.annotations = (valueLayout.annotations || []).concat(valueEnds);
  valueLayout.margin = Object.assign({}, valueLayout.margin, { r: 88 });
  Plotly.react("chart-value", value, valueLayout, CONFIG).then(() => { spreadEndLabels("chart-value"); markNumericNotes(); });

  const twr = (charts.twr || []).map((s, i) => ({
    type: "scatter", mode: "lines", name: s.name, x: s.x, y: s.y,
    line: { width: 2, color: colors[i % colors.length], shape: "linear" },
    hovertemplate: "%{fullData.name}: %{y:.1f}%<extra></extra>",
  }));
  // % since the start; the desk line ends at the "TW return" tile.
  const labels = endLabels(charts.twr || [], (v) => readablePct(v / 100));
  const twrLayout = baseLayout({
    annotations: labels,
    shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0, line: { color: token("--axis"), width: 1 } }],
  });
  twrLayout.yaxis = Object.assign({}, twrLayout.yaxis, { ticksuffix: "%" });
  Plotly.react("chart-twr", twr, twrLayout, CONFIG).then(() => { spreadEndLabels("chart-twr"); markNumericNotes(); });
}

// ---------------------------------------------------------------- page
function renderBanners(doc, now) {
  const host = document.getElementById("banners");
  const banners = [];
  const status = doc.status || {};
  // Unreadable accounts are shown by the account status chips (the error is in each chip's tooltip),
  // so they get no banner of their own.
  if (status.error) {
    banners.push(el("div", { class: "banner warning", role: "alert" }, [
      el("span", { class: "icon warn", "aria-hidden": "true", text: "!" }),
      el("div", {}, [el("strong", { text: `Last refresh failed (${clock(status.last_attempt)}). ` }),
        el("span", { text: `${status.error}. ` + (status.last_ok ? `Showing data from ${ago(status.last_ok, now)}.` : "") })]),
    ]));
  } else if (status.last_ok && (now - new Date(status.last_ok)) / 1000 > 3 * (status.interval_s || 60) + 120) {
    banners.push(el("div", { class: "banner warning", role: "alert" }, [
      el("span", { class: "icon warn", "aria-hidden": "true", text: "!" }),
      el("div", { text: `Data is ${ago(status.last_ok, now)}; the refresh may be stuck.` }),
    ]));
  }
  host.replaceChildren(...banners);
}

// The wallets card sits under the shorter of the two position tables, so the pair fills the screen evenly.
function balanceColumns() {
  const grid = document.getElementById("positions-grid");
  const wallets = document.getElementById("t-wallets");
  const card = wallets && wallets.closest(".card");
  if (!grid || !card) return;
  const [left, right] = grid.querySelectorAll(":scope > .stack");
  if (!left || !right || layoutState().order) return;   // a layout the reader arranged is left as arranged
  const piece = card.parentElement && card.parentElement.classList.contains("slot") ? card.parentElement : card;
  piece.remove();
  (right.offsetHeight < left.offsetHeight ? right : left).appendChild(piece);
}

function render(doc) {
  const now = new Date(doc.server_time || Date.now());
  renderBanners(doc, now);
  const data = doc.data;
  if (!data) return;
  document.getElementById("loading").hidden = true;
  document.getElementById("content").hidden = false;
  renderAccountStatus(data.accounts_status, now);
  if (lastRendered === doc.status.last_ok) return;   // same data: keep tables, sort and hover as they are
  lastRendered = doc.status.last_ok;
  renderTiles(data.kpis || {});
  const hover = (data.highlights && data.highlights.hover) || {};  // headlines around a big move, per alert title
  renderHighlights("hl-well", data.highlights && data.highlights.well, hover);
  renderHighlights("hl-attention", data.highlights && data.highlights.attention, hover);
  const context = { deskValue: (data.kpis || {}).desk_value };
  for (const id of TABLES) renderTable(id, applyView(id, data.tables && data.tables[id], context));
  balanceColumns();
  renderCharts(data.charts);
  renderPublished(data.tiles || []);
  const notes = document.getElementById("notes");  // developer notes: not on the page (the band shows real problems)
  if (notes) notes.replaceChildren(...(data.notes || []).map((note) => el("li", { text: note })));
}

// The web copy (web/live.js) builds the same document in the browser and sets DESK_SOURCE.
async function load() {
  try {
    if (window.DESK_SOURCE) {
      latest = await window.DESK_SOURCE();
      render(latest);
      return;
    }
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    latest = await response.json();
    render(latest);
  } catch (error) {
    // The page shows no status unless something is wrong: this goes in the top band's warnings.
    const message = window.DESK_SOURCE ? `Could not read the desk data (${error.message || error}).`
      : "Dashboard server not reachable (is python -m desk.dashboard running?)";
    document.getElementById("banners").replaceChildren(el("div", { class: "banner warning", role: "alert" }, [
      el("span", { class: "icon warn", "aria-hidden": "true", text: "!" }), el("div", { text: message })]));
  }
}

function poll() {          // the one polling loop
  load().finally(() => setTimeout(poll, POLL_MS));
}

// Redraw charts in the other theme's colors when the system theme changes.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (latest && latest.data) renderCharts(latest.data.charts);
});

// ---------------------------------------------------------------- look switches
// Three switches in the top bar: Numbers or Charts (the tables drawn as charts), a sun or moon for light or dark, and the
// style: Indie (the desk's own), HL (Hyperliquid) or Apple. Every style has a light and a dark version. The choices are
// remembered in the browser; the page's head script sets them before first paint (and reads ?mode= ?style= ?view=).
const VIEW_CHOICES = [["tables", "Numbers (tables)", "table"], ["charts", "Charts", "chart"]];
const STYLE_CHOICES = [["indie", "Indie", "#898781"], ["hl", "HL", "#50d2c1"], ["apple", "Apple", "#0a84ff"]];
function lookTheme(mode, style) {   // the CSS theme for a style and mode
  return style === "indie" ? mode : style === "hl" ? (mode === "dark" ? "hl" : "hl-light") : "apple-" + mode;
}
function icon(kind) {   // 16px line icons, drawn in the button's text colour
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16"); svg.setAttribute("width", "16"); svg.setAttribute("height", "16");
  svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("aria-hidden", "true");
  const paths = {
    table: ["M2 3.5h12v9H2z", "M2 7h12", "M2 10h12", "M6.5 3.5v9"],
    chart: ["M3 13V8", "M7 13V4", "M11 13V9", "M14 13V6.5", "M2 13.5h13"],
    sun: ["M8 5.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6z", "M8 1.5v1.6", "M8 12.9v1.6", "M1.5 8h1.6", "M12.9 8h1.6",
      "M3.4 3.4l1.1 1.1", "M11.5 11.5l1.1 1.1", "M3.4 12.6l1.1-1.1", "M11.5 4.5l1.1-1.1"],
    moon: ["M13 9.6A5.5 5.5 0 0 1 6.4 3a5.5 5.5 0 1 0 6.6 6.6z"],
    report: ["M4 1.8h5.5L12.5 5v9.2H4z", "M9.5 1.8V5h3", "M6 8h4.5", "M6 10.5h4.5"],
    pulse: ["M1.5 8.5h3l1.8-5 3.2 9 1.8-4h3.2"],
  }[kind];
  paths.forEach((d) => { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); svg.append(p); });
  return svg;
}
function setUnit(unit) {
  pnlUnit = unit;
  try { localStorage.setItem("desk-unit", unit); } catch (e) {}
  feedback("look", "unit", unit);
  drawModes();
  redrawLook("view");
}
function setLook(kind, value) {
  const root = document.documentElement;
  root.dataset[kind] = value;
  root.dataset.theme = lookTheme(root.dataset.mode || "dark", root.dataset.style || "indie");
  try { localStorage.setItem("desk-" + kind, value); } catch (e) {}
  feedback("look", kind, value);
  drawModes();
  redrawLook(kind);
}
function drawModes() {
  const host = document.getElementById("modes");
  if (!host) return;
  const root = document.documentElement.dataset;
  const mode = root.mode === "light" ? "light" : "dark", style = root.style || "indie";
  const views = el("div", { class: "switch", role: "group", "aria-label": "Numbers or charts" });
  VIEW_CHOICES.forEach(([value, name, shape]) => {
    const button = el("button", { type: "button", class: "icon-button", title: name, "aria-label": name,
      "aria-pressed": value === (root.view === "charts" ? "charts" : "tables") ? "true" : "false" }, [icon(shape)]);
    button.addEventListener("click", () => setLook("view", value));
    views.append(button);
  });
  const next = mode === "dark" ? "light" : "dark";   // the icon shows where a click goes: the sun in dark mode, the moon in light
  const toggle = el("div", { class: "switch", role: "group", "aria-label": "Light or dark" }, [
    el("button", { type: "button", class: "icon-button", title: `Switch to ${next} mode`, "aria-label": `Switch to ${next} mode`,
      "aria-pressed": "false" }, [icon(next === "light" ? "sun" : "moon")])]);
  toggle.firstChild.addEventListener("click", () => setLook("mode", next));
  const styles = el("div", { class: "switch", role: "group", "aria-label": "Style" });
  STYLE_CHOICES.forEach(([value, name, dot]) => {
    const button = el("button", { type: "button", class: "style-button", title: name, "aria-pressed": value === style ? "true" : "false",
      "aria-label": name + " style", style: `--dot:${dot}` }, [el("i")]);
    button.addEventListener("click", () => setLook("style", value));
    styles.append(button);
  });
  const units = el("div", { class: "switch", role: "group", "aria-label": "uPnL in percent or dollars" });
  [["pct", "%"], ["usd", "$"]].forEach(([value, name]) => {
    const button = el("button", { type: "button", class: "unit-button", title: value === "pct" ? "uPnL in % of cost" : "uPnL in dollars",
      "aria-pressed": value === pnlUnit ? "true" : "false", text: name });
    button.addEventListener("click", () => setUnit(value));
    units.append(button);
  });
  const report = el("button", { type: "button", class: "icon-button report-button", title: "Report: a printable page of the desk", "aria-label": "Report" }, [icon("report")]);
  const tickerOn = tickerState().on;
  const strip = el("button", { type: "button", class: "icon-button strip-button", title: tickerOn ? "Hide the price strip" : "Show live prices on top",
    "aria-label": "Live price strip", "aria-pressed": tickerOn ? "true" : "false" }, [icon("pulse")]);
  strip.addEventListener("click", () => { const s = tickerState(); s.on = !s.on; saveTicker(s); feedback("look", "price strip", s.on ? "on" : "off"); drawModes(); refreshTicker(); });
  report.addEventListener("click", openReport);
  host.replaceChildren(views, units, toggle, styles, strip, report);
}
// A look change redraws only what it changes, from the data already loaded (render() skips unchanged data): the view
// switch redraws the tables; mode and style are CSS for the tables, so only the Plotly charts (which read the colours and
// font when drawn) are redrawn, and the table charts when the charts view is on.
function redrawLook(kind) {
  if (!latest || !latest.data) return;
  const data = latest.data, context = { deskValue: (data.kpis || {}).desk_value };
  if (kind === "view" || isChartView()) {
    for (const id of TABLES) renderTable(id, applyView(id, data.tables && data.tables[id], context));
    if (kind === "view") balanceColumns();
  }
  if (kind !== "view") { renderCharts(data.charts); renderPublished(data.tiles || []); }
}
drawModes();

// ---------------------------------------------------------------- sections: drag to reorder, hide and show
// Every card sits in a "slot" (a wrapper that takes no space); dragging a card by its grip onto another swaps the two
// places. A card is hidden with its eye button and brought back from the Sections menu. Order and hidden cards are
// remembered in the browser (touch screens have no drag: use the menu there).
const LAYOUT_KEY = "desk-layout";
const LAYOUT_VERSION = 2;   // 2: the page leads with what is happening; an arrangement saved on the old order is dropped
function layoutState() {
  let state = {};
  try { state = JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {}; } catch (e) { return {}; }
  if (state.version !== LAYOUT_VERSION) {   // keep hidden and minimized choices, not slot positions of the old page
    delete state.order;
    state.version = LAYOUT_VERSION;
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(state)); } catch (e) {}
  }
  return state;
}
function saveLayout(state) {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(state)); } catch (e) {}
}
const cardId = (card) => { const inner = card.querySelector("[id]"); return inner ? inner.id : ""; };
const cardTitle = (card) => {   // the heading without its icon, grip and buttons
  const h = card.querySelector("h2");
  if (!h) return "";
  const copy = h.cloneNode(true);
  copy.querySelectorAll(".card-grip, .card-tools, .icon").forEach((node) => node.remove());
  return copy.textContent.trim();
};

const CARDS = {};   // card id -> its element
// A card becomes a window: it sits in a slot, drags by its body, and has minimize, save, full screen and hide.
function adoptCard(card) {
    if (!card.parentElement || !card.parentElement.classList.contains("slot")) {
      const slot = el("div", { class: "slot" });
      card.replaceWith(slot);
      slot.append(card);
    }
    CARDS[cardId(card)] = card;
    const title = card.querySelector("h2");
    if (!title || card.dataset.adopted) return;
    markKinds(title);
    card.dataset.adopted = "1";
    const grip = el("span", { class: "card-grip", title: "Drag anywhere on the tile (outside the chart) to move it", "aria-label": "Move " + cardTitle(card), text: "⠿" });
    // The whole tile drags, except from a chart, a field, a button or a link (those keep their own mouse behaviour).
    card.addEventListener("mousedown", (event) => {
      card.draggable = !event.target.closest(".js-plotly-plot, input, textarea, select, button, a, .card-full");
    });
    card.addEventListener("dragstart", (event) => {
      if (!card.draggable || card.classList.contains("card-full")) { event.preventDefault(); return; }
      event.dataTransfer.setData("text/plain", cardId(card));
      event.dataTransfer.effectAllowed = "move";
      try { event.dataTransfer.setDragImage(card, 24, 14); } catch (e) {}
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => {
      card.draggable = false;
      card.classList.remove("dragging");
      document.querySelectorAll("#content .drop-target").forEach((node) => node.classList.remove("drop-target"));
    });
    // Window buttons: minimize to the title bar, full screen, close (hide; the Sections menu brings it back).
    const tool = (cls, label, text, action) => {
      const button = el("button", { type: "button", class: "card-tool " + cls, title: label, "aria-label": label + ": " + cardTitle(card), text });
      button.addEventListener("click", action);
      return button;
    };
    const tools = el("span", { class: "card-tools" }, [
      tool("card-minimize", "Minimize", "–", () => setMinimized(cardId(card), !card.classList.contains("card-min"))),
      tool("card-save", "Save as image", "⤓", () => saveTile(card)),
      tool("card-fullscreen", "Full screen", "⤢", () => setFull(cardId(card), !card.classList.contains("card-full"))),
      tool("card-hide", "Hide", "×", () => setHidden(cardId(card), true)),
    ]);
    title.prepend(grip);
    title.append(tools);
    card.addEventListener("dragover", (event) => { event.preventDefault(); event.dataTransfer.dropEffect = "move"; card.classList.add("drop-target"); });
    card.addEventListener("dragleave", () => card.classList.remove("drop-target"));
    card.addEventListener("drop", (event) => {
      event.preventDefault();
      card.classList.remove("drop-target");
      const moving = CARDS[event.dataTransfer.getData("text/plain")];
      if (!moving || moving === card || Boolean(moving.dataset.published || moving.dataset.asset) !== Boolean(card.dataset.published || card.dataset.asset)) return;   // published tiles swap among themselves
      const from = moving.parentElement, to = card.parentElement;
      to.append(moving);
      from.append(card);
      saveOrder();
      feedback("section", "move", cardTitle(moving), "swapped with " + cardTitle(card));
      if (latest && latest.data) renderCharts(latest.data.charts);   // a chart that changed width is redrawn to fit
    });
}
function setupSections() {
  const content = document.getElementById("content");
  if (!content) return;
  [...content.querySelectorAll(".card")].forEach(adoptCard);
  const byId = CARDS;
  const state = layoutState();
  const slots = [...content.querySelectorAll(".slot")].filter((slot) => !slot.closest(".feed"));
  if (state.order) state.order.forEach((id, i) => { if (byId[id] && slots[i]) slots[i].append(byId[id]); });
  (state.hidden || []).forEach((id) => { if (byId[id]) byId[id].classList.add("card-off"); });
  (state.minimized || []).forEach((id) => { if (byId[id]) byId[id].classList.add("card-min"); });
  document.addEventListener("keydown", (event) => {
    const open = content.querySelector(".card-full");
    if (event.key === "Escape" && open) setFull(cardId(open), false);
  });
  buildSectionsMenu(byId);
}

// ---------------------------------------------------------------- published tiles: charts and tables pushed from the notebooks
// desk/tile_feed.py publishes a Plotly figure or a table; it appears here as a tile (top of the page, under the key numbers)
// with the reason it was published, and is a window like the others. The look (colours, font) is the dashboard's own.
const PUBLISHED_AXES = ["xaxis", "yaxis", "xaxis2", "yaxis2"];
function renderPublished(tiles) {
  const content = document.getElementById("content");
  if (!content) return;
  // What changes with the market (relevance 0.5 and up) sits at the top; report material (closed-trade returns, models,
  // history) goes to the bottom of the page, where it is there when wanted and out of the way when trading.
  let feed = document.getElementById("feed"), later = document.getElementById("feed-later");
  if (!feed) {
    feed = el("section", { class: "grid two feed", id: "feed", "aria-label": "Published from the notebooks" });
    content.insertBefore(feed, document.getElementById("tiles").nextSibling);
  }
  if (!later) {
    later = el("section", { class: "grid two feed", id: "feed-later", "aria-label": "Reports" });
    content.append(later);
  }
  const wanted = new Set(tiles.map((tile) => tile.key));
  [...content.querySelectorAll(".feed .card[data-published]")].forEach((card) => {
    if (wanted.has(card.dataset.published)) return;
    delete CARDS[cardId(card)];
    card.closest(".slot").remove();
  });
  tiles.forEach((tile) => {
    let card = [...content.querySelectorAll(".feed .card[data-published]")].find((node) => node.dataset.published === tile.key);
    const home = Number(tile.relevance) >= 0.5 ? feed : later;
    if (!card) {
      const inner = tile.kind === "chart" ? el("div", { class: "chart", id: "pub-" + tile.key }) : el("div", { class: "table-wrap", id: "t-pub-" + tile.key });
      card = el("div", { class: "card published", "data-published": tile.key }, [
        el("h2", { title: tile.reason || "", text: tile.title }), inner]);   // why it is here: on hover, not a caption
      home.append(card);
      adoptCard(card);
      applyCardState(card);
    }
    if (tile.kind === "chart" && typeof Plotly !== "undefined") {
      const base = baseLayout({ hovermode: "closest", margin: { l: 56, r: 24, t: 30, b: 40 }, colorway: [1, 2, 3, 4, 5].map((i) => token("--series-" + i)) });
      const layout = Object.assign({}, base, Object.fromEntries(Object.entries(tile.layout || {}).filter(([key]) => !PUBLISHED_AXES.includes(key))));
      PUBLISHED_AXES.forEach((key) => { if (base[key] || (tile.layout || {})[key]) layout[key] = Object.assign({}, base[key] || {}, (tile.layout || {})[key] || {}); });
      Plotly.react("pub-" + tile.key, tile.data || [], layout, CONFIG).then(markNumericNotes);
    } else if (tile.kind === "table") {
      renderTable("pub-" + tile.key, { columns: tile.columns || [], rows: tile.rows || [] });
    }
  });
  feed.hidden = !feed.querySelector(".card:not(.card-off)");
  later.hidden = !later.querySelector(".card:not(.card-off)");
}
function applyCardState(card) {   // the remembered minimized and hidden state of a card that was created after load
  const state = layoutState(), id = cardId(card);
  if ((state.hidden || []).includes(id)) card.classList.add("card-off");
  if ((state.minimized || []).includes(id)) card.classList.add("card-min");
}


// ---------------------------------------------------------------- an asset as a tile: price, your position, what to do about it
// A symbol in a highlight (or the strip) opens the asset as a tile at the top of the page: price and 7-day chart (HL markets),
// your position in it, links to its page, and the sensible actions for where it stands. The action buttons do not trade:
// they copy the order as a sentence, to paste to Claude, which runs it with the usual confirmation (or the pop-up).
function knownSymbols() {
  const found = new Set(), tables = (latest && latest.data && latest.data.tables) || {};
  ["positions", "positions_stocks", "wallets"].forEach((id) => ((tables[id] || {}).rows || []).forEach((row) => row.Symbol && found.add(marketName(row.Symbol))));
  ["market_making", "candidates", "mm_candidates"].forEach((id) => ((tables[id] || {}).rows || []).forEach((row) => row.Coin && found.add(marketName(row.Coin))));
  return found;
}

// Crypto and stocks are a colour, not a word: an orange dot for crypto, a blue one for stocks and tradfi, wherever the
// words would appear (titles, highlights, table cells). The word stays for screen readers and shows on hover.
const KIND_WORDS = /\b(crypto|stocks and tradfi|stocks & commodities|stocks|tradfi)\b/gi;
function kindMarks(text) {   // each crypto or stocks part becomes one tinted run: the dot and what it says, to the next part
  const source = String(text), found = [];
  KIND_WORDS.lastIndex = 0;
  let match;
  while ((match = KIND_WORDS.exec(source)) !== null) found.push({ at: match.index, end: match.index + match[0].length, word: match[1] });
  KIND_WORDS.lastIndex = 0;
  if (!found.length) return [source];
  const parts = [];
  if (found[0].at > 0) parts.push(source.slice(0, found[0].at));
  found.forEach((item, i) => {
    const next = i + 1 < found.length ? found[i + 1].at : source.length;
    const stop = source.slice(item.end, next).search(/[;.](\s|$)/);   // a sentence or list break ends the run
    const end = stop >= 0 ? item.end + stop : next;
    const crypto = /crypto/i.test(item.word);
    const words = source.slice(item.end, end).replace(/^[:\s]+/, "").replace(/,\s*$/, "");
    parts.push(el("span", { class: "kind-run " + (crypto ? "kind-crypto" : "kind-stocks"), title: item.word }, [
      el("span", { class: "kind " + (crypto ? "kind-crypto" : "kind-stocks"), role: "img", "aria-label": item.word }), words]));
    const between = source.slice(end, next);
    if (between) parts.push(between);
    else if (i + 1 < found.length) parts.push(" ");   // two runs side by side keep a gap
  });
  // "Open positions: crypto" reads "Open positions ●": no colon before a bare dot
  return parts.map((part, i) => (typeof part === "string" && i + 1 < parts.length && typeof parts[i + 1] !== "string" ? part.replace(/:\s*$/, " ") : part));
}
function markKinds(node) {   // replace the words in a heading's text nodes, keeping its buttons and icons
  [...node.childNodes].forEach((child) => {
    if (child.nodeType === 3 && KIND_WORDS.test(child.textContent)) {
      KIND_WORDS.lastIndex = 0;
      child.replaceWith(...kindMarks(child.textContent.replace(/:\s*$/, " ")));
    }
    KIND_WORDS.lastIndex = 0;
  });
}

function linkSymbols(text) {   // the text with each known symbol as a button that opens its tile
  const symbols = [...knownSymbols()].filter((s) => s.length >= 2).sort((a, b) => b.length - a.length);
  if (!symbols.length) return [text];
  const escaped = symbols.map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp("(^|[^A-Za-z0-9])(" + escaped.join("|") + ")(?![A-Za-z0-9])", "g");
  const parts = [];
  let last = 0, match;
  while ((match = pattern.exec(text)) !== null) {
    const start = match.index + match[1].length;
    if (start > last) parts.push(text.slice(last, start));
    const symbol = match[2];
    const button = el("button", { type: "button", class: "symbol-chip", text: symbol });
    button.addEventListener("click", (event) => { event.stopPropagation(); openAsset(symbol); });
    parts.push(button);
    last = start + symbol.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}
function assetRows(symbol) {   // your rows in this asset, from the positions tables
  const tables = (latest && latest.data && latest.data.tables) || {}, name = marketName(symbol).toLowerCase();
  const rows = [];
  ["positions", "positions_stocks", "wallets"].forEach((id) => ((tables[id] || {}).rows || []).forEach((row) => {
    if (row.Symbol && marketName(row.Symbol).toLowerCase() === name) rows.push(Object.assign({ _table: id }, row));
  }));
  return rows;
}
function assetAdvice(rows) {   // where it stands, and what is sensible
  const held = rows.filter((row) => isNum(row.Value) && row._table !== "wallets");
  if (!held.length) return { line: "Not held.", actions: [["Open a position", "short or long it: say which and how big"]] };
  const value = held.reduce((sum, row) => sum + row.Value, 0), pnl = held.reduce((sum, row) => sum + (row.uPnL || 0), 0);
  const costs = held.reduce((sum, row) => sum + Math.abs(row.Value - (row.uPnL || 0)), 0), share = costs ? pnl / costs : NaN;
  const side = value < 0 ? "short" : "long", name = marketName(held[0].Symbol);
  const line = `${side}, ${usd(Math.abs(value))}, uPnL ${isNum(share) ? pct(share) : "n/a"}`;
  const trim = [`Trim a third`, `trim ${name} by a third`, "reduce", 1 / 3], close = [`Close`, `close ${name}`, "close", 1],
    add = [`Add a third`, `add a third to the ${name} ${side}`, "add", 1 / 3];
  const actions = share >= 0.1 ? [trim, close, add] : share <= -0.1 ? [close, trim, add] : [trim, add, close];
  // On the web dashboard an HL position's actions run through the daemon (plan, then confirm); IB and the local page copy.
  const hl = held.find((row) => /^HL /.test(String(row.Account || "")));
  return { line, actions, lead: share >= 0.1 ? "Winner: take part of it" : share <= -0.1 ? "Loser: decide" : "",
    target: hl && window.DESK_SOURCE ? { coin: hl.Symbol, wallet: String(hl.Account).slice(-4) } : null };
}
async function openAsset(symbol) {
  const name = marketName(symbol), key = "asset-" + name.replace(/[^A-Za-z0-9]/g, "_");
  feedback("asset", "open", name);
  let feed = document.getElementById("feed");
  if (!feed) { renderPublished([]); feed = document.getElementById("feed"); }
  feed.hidden = false;
  let card = feed.querySelector(`.card[data-asset="${key}"]`);
  if (card) { card.classList.remove("card-off", "card-min"); card.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  else {
    card = el("div", { class: "card asset-card", "data-asset": key }, [el("h2", { text: name }), el("div", { class: "asset-body", id: key })]);
    feed.prepend(card);
    adoptCard(card);
  }
  const body = card.querySelector(".asset-body");
  const rows = assetRows(symbol), advice = assetAdvice(rows);
  const external = rows.some((row) => /^IB/.test(String(row.Account || ""))) ? stockLink(name) : hlLink(rows[0] && rows[0].Symbol || symbol);
  const copy = (sentence, button) => {
    const done = () => { button.textContent = "copied: paste to Claude"; setTimeout(() => drawAsset(), 2500); feedback("asset", "action", name, sentence); };
    if (navigator.clipboard) navigator.clipboard.writeText(sentence).then(done, done); else done();
  };
  function drawAsset() {
    body.replaceChildren(
      el("div", { class: "asset-line" }, [el("span", { class: "asset-price", id: key + "-price" }), el("span", { text: advice.line })]),
      el("div", { class: "chart asset-chart", id: key + "-chart", hidden: true }),
      advice.lead ? el("div", { class: "asset-lead", text: advice.lead }) : null,
      el("div", { class: "asset-actions" }, [
        ...advice.actions.map(([label, sentence, change, fraction], i) => {
          const runs = advice.target && change;
          const b = el("button", { type: "button", class: "asset-action" + (i === 0 && advice.lead ? " lead" : ""),
            title: runs ? `Plan it, then confirm: ${sentence}` : "Copies: " + sentence, text: label });
          b.addEventListener("click", () => (runs ? runAction(label, change, fraction, advice.target) : copy(sentence, b)));
          return b;
        }),
        el("a", { class: "asset-action", href: external, target: "_blank", rel: "noopener noreferrer", title: "Open the asset's page", text: "Page ↗" })]));
    const strip = el("button", { type: "button", class: "asset-action", title: "Show it in the price strip", text: "+ strip" });
    strip.addEventListener("click", () => { const s = tickerState(); if (!s.assets.includes(name)) s.assets.push(name); s.on = true; saveTicker(s); refreshTicker(); drawModes(); strip.textContent = "added"; });
    body.querySelector(".asset-actions").append(strip);
  }
  drawAsset();
  // HL markets: live price and 7 days of hourly closes, with the entry price marked.
  try {
    await loadUniverse();
    const list = document.getElementById("tick-universe");
    const hlName = list && [...list.options].map((o) => o.value).find((value) => marketName(value).toLowerCase() === name.toLowerCase());
    if (!hlName) return;
    const contexts = await tickerContexts([hlName]);
    const context = contexts[hlName];
    if (context) {
      const change = context.previous ? context.price / context.previous - 1 : NaN;
      const price = document.getElementById(key + "-price");
      if (price) price.textContent = `${priceText(context.price)} ${isNum(change) ? pct(change) : ""} · `;
    }
    const end = Date.now(), dex = hlName.includes(":") ? hlName.split(":")[0] : "";
    const response = await fetch(HL_INFO, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ type: "candleSnapshot", req: { coin: hlName.split(":").pop(), interval: "1h", startTime: end - 7 * 86400000, endTime: end } }, dex ? { dex } : {})) });
    const candles = await response.json();
    const chart = document.getElementById(key + "-chart");
    if (!chart || !candles.length || typeof Plotly === "undefined") return;
    chart.hidden = false;
    const entry = rows.map((row) => row["Avg price"]).find(isNum);
    const layout = baseLayout({ hovermode: "x unified", margin: { l: 48, r: 12, t: 6, b: 28 }, showlegend: false });
    layout.shapes = entry ? [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: entry, y1: entry, line: { color: token("--muted"), dash: "dot", width: 1 } }] : [];
    layout.xaxis = Object.assign({}, layout.xaxis, { showspikes: false });
    Plotly.newPlot(chart, [{ type: "scatter", mode: "lines", x: candles.map((c) => new Date(c.t)), y: candles.map((c) => Number(c.c)),
      line: { color: token("--series-1"), width: 1.6 }, hovertemplate: "%{y}<extra></extra>" }], layout, CONFIG);
  } catch (e) { /* no chart: the facts and actions are there */ }
}


// ---------------------------------------------------------------- learning from use: what is read, what is never touched
// Each tile counts the clicks and the seconds the pointer rests on it (kept in the browser, one hourly line per tile sent
// to the feedback log). A tile that has been on the page for a week and was not touched in two is offered in the Sections
// menu as "rarely used: hide?" (and a dot on the menu icon says so): the dashboard shrinks to what is actually used.
const USAGE_KEY = "desk-usage";
const USAGE_WINDOW_DAYS = 14, USAGE_MIN_AGE_DAYS = 7;
function usageState() { try { return JSON.parse(localStorage.getItem(USAGE_KEY)) || {}; } catch (e) { return {}; } }
function saveUsage(state) { try { localStorage.setItem(USAGE_KEY, JSON.stringify(state)); } catch (e) {} }
function noteUse(id, kind, amount) {
  if (!id) return;
  const state = usageState(), now = Date.now();
  const entry = state[id] || (state[id] = { first: now, clicks: 0, seconds: 0, last: 0, sent: 0 });
  entry[kind] += amount;
  entry.last = now;
  if (now - entry.sent > 3600_000) {   // one line an hour per tile is enough to learn from
    entry.sent = now;
    feedback("use", kind === "clicks" ? "click" : "hover", id, `${entry.clicks} clicks, ${Math.round(entry.seconds)} s in all`);
  }
  saveUsage(state);
}
function watchUse() {
  const content = document.getElementById("content");
  if (!content) return;
  content.addEventListener("click", (event) => {
    const card = event.target.closest && event.target.closest(".card, .tile");
    if (!card || event.target.closest(".card-tools, .tile-tools, .card-grip")) return;   // moving a tile around is not using it
    noteUse(card.classList.contains("tile") ? "tile:" + card.dataset.label : cardId(card), "clicks", 1);
  });
  let over = null, since = 0;
  const leave = () => { if (over && Date.now() - since > 1500) noteUse(over, "seconds", (Date.now() - since) / 1000); over = null; };
  content.addEventListener("mouseover", (event) => {
    const card = event.target.closest && event.target.closest(".card, .tile");
    const id = card ? (card.classList.contains("tile") ? "tile:" + card.dataset.label : cardId(card)) : null;
    if (id === over) return;
    leave();
    over = id; since = Date.now();
  });
  content.addEventListener("mouseleave", leave);
}
function rarelyUsed() {   // tiles shown for a week or more and not touched in the last two
  const state = usageState(), now = Date.now(), day = 86400000, out = [];
  document.querySelectorAll("#content .card:not(.card-off)").forEach((card) => {
    const id = cardId(card);
    if (!id || card.dataset.asset || card.dataset.published || /^hl-/.test(id)) return;   // highlights are never pushed away
    const entry = state[id] || (state[id] = { first: now, clicks: 0, seconds: 0, last: 0, sent: 0 });
    if (now - entry.first >= USAGE_MIN_AGE_DAYS * day && now - entry.last >= USAGE_WINDOW_DAYS * day) out.push({ card, id, age: Math.round((now - entry.first) / day) });
  });
  saveUsage(state);
  return out;
}


// ---------------------------------------------------------------- run an action: plan (the daemon's dry run), then confirm
// One dialog: the plan with its notices (cap, funding, blocked) on top, then Confirm or Cancel. Confirm sends it live; the
// result shows in the same dialog and the page refreshes. The server keeps a log of every confirmed action.
function actionDialog(title) {
  document.querySelectorAll(".action-dialog").forEach((node) => node.remove());
  const box = el("div", { class: "action-box", role: "dialog", "aria-modal": "true", "aria-label": title });
  const overlay = el("div", { class: "action-dialog" }, [box]);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) overlay.remove(); });
  document.addEventListener("keydown", function escape(event) { if (event.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", escape); } });
  document.body.append(overlay);
  return { overlay, box };
}
function planLines(text, coin) {   // what matters in the daemon's output: the coin's lines and any notice
  const name = marketName(coin).toLowerCase();
  const lines = String(text).split("\n").map((line) => line.trim()).filter(Boolean);
  const notices = lines.filter((line) => /BLOCKED|cap|funding|kill switch|refused|could not|stale|No open/i.test(line));
  const mine = lines.filter((line) => line.toLowerCase().includes(name) && !notices.includes(line));
  return { notices, mine: mine.slice(-8) };
}
async function runAction(label, change, fraction, target) {
  const coin = target.coin, title = `${label}: ${marketName(coin)} (HL ${target.wallet})`;
  feedback("action", "plan", marketName(coin), label);
  const { overlay, box } = actionDialog(title);
  box.replaceChildren(el("h3", { text: title }), el("p", { class: "muted", text: "Planning with live data…" }));
  const post = (body) => fetch("/action", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
    body: JSON.stringify(Object.assign({ change, coin, wallet: target.wallet, fraction }, body)) }).then((r) => r.json());
  let plan;
  try { plan = await post({ stage: "plan" }); } catch (e) { plan = { error: String(e) }; }
  if (plan.error || !plan.token) {
    const { notices, mine } = planLines(plan.plan || plan.error || "", coin);
    box.replaceChildren(el("h3", { text: title }), el("p", { class: "neg", text: "Nothing can be sent." }),
      ...notices.concat(mine).map((line) => el("p", { class: "action-line", text: line })), closeButton(overlay));
    return;
  }
  const { notices, mine } = planLines(plan.plan, coin);
  const confirm = el("button", { type: "button", class: "asset-action lead", text: `Confirm: ${label.toLowerCase()} ${marketName(coin)}` });
  const cancel = el("button", { type: "button", class: "asset-action", text: "Cancel" });
  cancel.addEventListener("click", () => { feedback("action", "cancel", marketName(coin), label); overlay.remove(); });
  confirm.addEventListener("click", async () => {
    confirm.disabled = true; confirm.textContent = "Sending…";
    feedback("action", "confirm", marketName(coin), label);
    let result;
    try { result = await post({ stage: "confirm", token: plan.token }); } catch (e) { result = { error: String(e) }; }
    const lines = planLines(result.result || result.error || "", coin);
    box.replaceChildren(el("h3", { text: title }),
      el("p", { class: result.code === 0 ? "pos" : "neg", text: result.code === 0 ? "Done." : "Not completed." }),
      ...lines.notices.map((line) => el("p", { class: "action-line notice", text: line })),
      ...lines.mine.map((line) => el("p", { class: "action-line", text: line })), closeButton(overlay));
    lastRendered = null; load();
  });
  box.replaceChildren(el("h3", { text: title }),
    ...notices.map((line) => el("p", { class: "action-line notice", text: line })),
    ...mine.map((line) => el("p", { class: "action-line", text: line })),
    el("div", { class: "asset-actions" }, [confirm, cancel]));
}
function closeButton(overlay) {
  const button = el("button", { type: "button", class: "asset-action", text: "Close" });
  button.addEventListener("click", () => overlay.remove());
  return el("div", { class: "asset-actions" }, [button]);
}

function saveOrder() {
  const state = layoutState();
  state.order = [...document.querySelectorAll("#content .slot")].filter((slot) => !slot.closest(".feed")).map((slot) => cardId(slot.firstElementChild));
  saveLayout(state);
}
function setHidden(id, hidden) {
  const card = document.getElementById(id) && document.getElementById(id).closest(".card");
  if (!card) return;
  card.classList.toggle("card-off", hidden);
  feedback("section", hidden ? "hide" : "show", cardTitle(card));
  const state = layoutState();
  state.hidden = [...document.querySelectorAll("#content .card-off")].map(cardId);
  saveLayout(state);
  drawSectionsMenu();
  if (!hidden && latest && latest.data) renderCharts(latest.data.charts);   // a chart drawn while hidden has no size
}
// Save a tile as a PNG (as Plotly's camera does): a tile with a chart is saved through Plotly, on the card's own colour;
// a table or highlight list is drawn from its page styles into an image (an SVG with the card's HTML inside).
async function saveTile(card, titleOverride) {
  const title = titleOverride || cardTitle(card) || "tile";
  const name = `${title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}-${new Date().toISOString().slice(0, 10)}`;
  feedback("section", "save image", title);
  const chart = card.querySelector(".js-plotly-plot");
  if (chart && typeof Plotly !== "undefined") {
    const background = token("--surface");
    try {
      await Plotly.relayout(chart, { paper_bgcolor: background, plot_bgcolor: background });
      await Plotly.downloadImage(chart, { format: "png", filename: name, scale: 2 });
    } finally {
      Plotly.relayout(chart, { paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)" });
    }
    return;
  }
  const rect = card.getBoundingClientRect();
  const copy = card.cloneNode(true);
  const inline = (from, to) => {   // the computed look of every element, so the image needs no stylesheet
    const style = getComputedStyle(from);
    to.setAttribute("style", Array.from(style).map((property) => `${property}:${style.getPropertyValue(property)}`).join(";"));
    Array.from(from.children).forEach((child, i) => inline(child, to.children[i]));
  };
  inline(card, copy);
  copy.querySelectorAll(".card-tools, .card-grip").forEach((node) => node.remove());
  copy.style.margin = "0";
  const xml = new XMLSerializer().serializeToString(copy);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${rect.width}" height="${rect.height}">`
    + `<foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml">${xml}</div></foreignObject></svg>`;
  const image = new Image();
  image.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = rect.width * 2; canvas.height = rect.height * 2;
    const context = canvas.getContext("2d");
    context.scale(2, 2);
    context.fillStyle = token("--surface");
    context.fillRect(0, 0, rect.width, rect.height);
    context.drawImage(image, 0, 0);
    canvas.toBlob((blob) => {
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = name + ".png";
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    });
  };
  image.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}
function setMinimized(id, minimized) {
  const card = document.getElementById(id) && document.getElementById(id).closest(".card");
  if (!card) return;
  card.classList.toggle("card-min", minimized);
  feedback("section", minimized ? "minimize" : "restore", cardTitle(card));
  const state = layoutState();
  state.minimized = [...document.querySelectorAll("#content .card-min")].map(cardId);
  saveLayout(state);
  if (!minimized && latest && latest.data) renderCharts(latest.data.charts);   // a chart drawn while collapsed has no size
}
function setFull(id, full) {   // full screen: the card fills the window above the page (Esc or the button returns it)
  const card = document.getElementById(id) && document.getElementById(id).closest(".card");
  if (!card) return;
  document.querySelectorAll("#content .card-full").forEach((other) => { if (other !== card) other.classList.remove("card-full"); });
  card.classList.toggle("card-full", full);
  feedback("section", full ? "full screen" : "leave full screen", cardTitle(card));
  card.classList.remove("card-min");   // a full screen card is open
  document.documentElement.classList.toggle("has-full", full);
  if (typeof Plotly !== "undefined") {   // charts inside change size
    card.querySelectorAll(".js-plotly-plot").forEach((node) => { try { Plotly.Plots.resize(node); } catch (e) {} });
  }
  if (latest && latest.data) setTimeout(() => renderCharts(latest.data.charts), 0);
}
function buildSectionsMenu(byId) {
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  const wrap = el("div", { class: "sections", id: "sections" });
  const button = el("button", { type: "button", class: "icon-button sections-button", id: "sections-button", title: "Sections: show or hide",
    "aria-label": "Sections: show or hide", "aria-expanded": "false" });
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  ["viewBox:0 0 16 16", "width:16", "height:16", "fill:none", "stroke:currentColor", "stroke-width:1.5", "stroke-linecap:round"]
    .forEach((pair) => { const [k, v] = pair.split(":"); svg.setAttribute(k, v); });
  ["M2.5 4h11", "M2.5 8h11", "M2.5 12h11"].forEach((d) => { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); svg.append(p); });
  button.append(svg);
  const menu = el("div", { class: "sections-menu", id: "sections-menu", hidden: true });
  wrap.append(button, menu);
  bar.append(wrap);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden = !menu.hidden;
    button.setAttribute("aria-expanded", menu.hidden ? "false" : "true");
  });
  document.addEventListener("click", (event) => {
    if (!wrap.contains(event.target)) { menu.hidden = true; button.setAttribute("aria-expanded", "false"); }
  });
  drawSectionsMenu();
}
let menuQuery = "";
function drawSectionsMenu() {
  const menu = document.getElementById("sections-menu");
  if (!menu) return;
  let search = document.getElementById("sections-search");
  if (!search) {
    search = el("input", { type: "search", id: "sections-search", class: "sections-search", placeholder: "Search tiles and analytics", "aria-label": "Search tiles and analytics", autocomplete: "off" });
    search.addEventListener("input", () => { menuQuery = search.value; fillMenuLists(); });
  }
  let lists = document.getElementById("sections-lists");
  if (!lists) lists = el("div", { id: "sections-lists" });
  menu.replaceChildren(search, lists);
  fillMenuLists();
}
function menuMatches(text) {
  const words = menuQuery.toLowerCase().split(/\s+/).filter(Boolean);
  return words.every((word) => String(text).toLowerCase().includes(word));
}
function fillMenuLists() {
  const lists = document.getElementById("sections-lists");
  if (!lists) return;
  const heading = (text) => el("div", { class: "sections-head", text });
  const present = [...document.querySelectorAll("#content .slot")].map((slot) => slot.firstElementChild).filter(Boolean);
  const rows = present.filter((card) => menuMatches(cardTitle(card))).map((card) => {
    const box = el("input", { type: "checkbox", id: "show-" + cardId(card) });
    box.checked = !card.classList.contains("card-off");
    box.addEventListener("change", () => setHidden(cardId(card), !box.checked));
    return el("label", { class: "sections-row" }, [box, el("span", { text: cardTitle(card).trim() })]);
  });
  // Available to add: key-number tiles that were hidden, and the notebook sections that can be pulled onto the dashboard.
  const addable = [];
  (layoutState().tileHidden || []).filter(menuMatches).forEach((label) => {
    const add = el("button", { type: "button", class: "sections-add", title: "Show " + label, "aria-label": "Show " + label, text: "+" });
    add.addEventListener("click", () => { const s = layoutState(); s.tileHidden = (s.tileHidden || []).filter((x) => x !== label); saveLayout(s);
      feedback("tile", "show", label); if (latest && latest.data) renderTiles(latest.data.kpis || {}); drawSectionsMenu(); });
    addable.push(el("div", { class: "sections-row" }, [el("span", { text: label + " (key number)" }), add]));
  });
  const pulled = new Set([...document.querySelectorAll(".feed .card[data-published]")].map((card) => card.dataset.published));
  const attention = ((latest && latest.data && latest.data.highlights && latest.data.highlights.attention) || []).map((row) => row.join(" ")).join(" ").toLowerCase();
  const catalog = ((latest && latest.data && latest.data.catalog) || []).filter((entry) => menuMatches(entry.title + " " + entry.description + " " + entry.keywords))
    .map((entry) => ({ entry, score: entry.keywords.split(/\s+/).filter((word) => word && attention.includes(word)).length }))
    .sort((a, b) => b.score - a.score);
  catalog.forEach(({ entry, score }) => {
    const made = [...pulled].some((key) => key.startsWith(entry.id.replace(":", "-")));
    const button = el("button", { type: "button", class: "sections-add", title: made ? "On the dashboard" : "Pull onto the dashboard", "aria-label": "Add " + entry.title, text: made ? "✓" : "+" });
    if (!made) button.addEventListener("click", () => pullTile(entry, button));
    addable.push(el("div", { class: "sections-row", title: entry.description || entry.call }, [
      el("span", { text: (score ? "★ " : "") + entry.title }), button]));
  });
  const rare = rarelyUsed().filter(({ card }) => menuMatches(cardTitle(card)));
  const suggestions = rare.length ? [heading("Rarely used: hide?"), ...rare.map(({ card, id, age }) => {
    const hide = el("button", { type: "button", class: "sections-add", title: `Not touched in ${USAGE_WINDOW_DAYS} days (on the page ${age} days): hide it`, "aria-label": "Hide " + cardTitle(card), text: "×" });
    hide.addEventListener("click", () => { feedback("use", "hide unused", id); setHidden(id, true); });
    return el("div", { class: "sections-row" }, [el("span", { text: cardTitle(card) }), hide]);
  })] : [];
  const button = document.getElementById("sections-button");
  if (button) button.classList.toggle("has-suggestion", rarelyUsed().length > 0);
  lists.replaceChildren(...suggestions, heading("On the dashboard"), ...rows, ...(rows.length ? [] : [el("div", { class: "sections-empty", text: "No match" })]),
    heading(`Available to add${menuQuery ? "" : ` (${addable.length})`}`), ...addable, ...(addable.length ? [] : [el("div", { class: "sections-empty", text: "Nothing more to add" })]),
    ...menuFooter());
}
async function pullTile(entry, button) {   // the local dashboard builds the section on this Mac and publishes it
  if (window.DESK_SOURCE) { button.title = "Pulling needs the local dashboard on the Mac (python -m desk.dashboard)"; button.textContent = "!"; return; }
  feedback("tile", "pull", entry.title);
  button.textContent = "…"; button.disabled = true;
  try {
    await fetch("/api/tiles/pull", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: entry.id }) });
    for (let tries = 0; tries < 90; tries++) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const status = (await (await fetch("/api/tiles/status")).json())[entry.id] || "";
      if (status.startsWith("done")) { button.textContent = "✓"; lastRendered = null; load(); return; }
      if (status.startsWith("failed") || status.startsWith("nothing")) { button.textContent = "!"; button.title = status; button.disabled = false; return; }
    }
  } catch (e) { button.textContent = "!"; button.disabled = false; }
}
function menuFooter() {
  const state = highlightState(), hidden = state.deleted.length + Object.keys(state.read).length;
  const out = [];
  if (hidden) {
    const restore = el("button", { type: "button", class: "sections-reset", text: `Restore ${hidden} hidden highlight${hidden === 1 ? "" : "s"}` });
    restore.addEventListener("click", () => { saveHighlightState({ read: {}, deleted: [] }); feedback("highlight", "restore", "all"); redrawHighlights(); });
    out.push(restore);
  }
  const reset = el("button", { type: "button", class: "sections-reset", text: "Reset layout" });
  reset.addEventListener("click", () => { try { localStorage.removeItem(LAYOUT_KEY); } catch (e) {} location.reload(); });
  out.push(reset);
  return out;
}


// ---------------------------------------------------------------- price strip: chosen assets with live prices on top
// An option (Sections menu): a thin strip under the title bar with the assets you pick, price and 24h change from HL's
// public info API every few seconds; click a chip to open the asset's HL page. Off by default (the first screen stays
// as it is); the choice and the assets are remembered in the browser.
const TICKER_KEY = "desk-ticker";
const HL_INFO = "https://api.hyperliquid.xyz/info";
function tickerState() {
  try { return Object.assign({ on: false, assets: ["BTC", "ETH", "SOL"] }, JSON.parse(localStorage.getItem(TICKER_KEY)) || {}); }
  catch (e) { return { on: false, assets: ["BTC", "ETH", "SOL"] }; }
}
function saveTicker(state) { try { localStorage.setItem(TICKER_KEY, JSON.stringify(state)); } catch (e) {} }
const lastPrices = {};
const priceText = (value) => (value >= 1000 ? value.toLocaleString("en-US", { maximumFractionDigits: 1 }) : String(Number(value.toPrecision(5))));
async function tickerContexts(assets) {   // name -> {price, previous} for the main DEX and, when asked for, the xyz DEX
  const out = {};
  const read = async (dex) => {
    const response = await fetch(HL_INFO, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ type: "metaAndAssetCtxs" }, dex ? { dex } : {})) });
    const [meta, contexts] = await response.json();
    meta.universe.forEach((asset, i) => { out[asset.name] = { price: Number(contexts[i].markPx), previous: Number(contexts[i].prevDayPx) }; });
  };
  const jobs = [read("")];
  if (assets.some((asset) => asset.startsWith("xyz:"))) jobs.push(read("xyz"));
  await Promise.all(jobs.map((job) => job.catch(() => null)));
  return out;
}
// Suggestions for the "+ asset" field: every HL market (main DEX and the xyz stocks and commodities), loaded once.
let universeLoaded = false;
async function loadUniverse() {
  if (universeLoaded) return;
  universeLoaded = true;
  const names = [];
  for (const dex of ["", "xyz"]) {
    try {
      const response = await fetch(HL_INFO, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ type: "meta" }, dex ? { dex } : {})) });
      (await response.json()).universe.forEach((asset) => { if (!asset.isDelisted) names.push(asset.name); });
    } catch (e) { universeLoaded = false; }
  }
  let list = document.getElementById("tick-universe");
  if (!list) { list = el("datalist", { id: "tick-universe" }); document.body.append(list); }
  list.replaceChildren(...names.sort().map((name) => el("option", { value: name })));
}
async function refreshTicker() {
  const state = tickerState();
  let strip = document.getElementById("ticker");
  if (!state.on) { if (strip) strip.hidden = true; return; }
  if (!strip) {
    strip = el("div", { class: "ticker", id: "ticker", "aria-label": "Live prices" });
    const bar = document.querySelector(".topbar");
    bar.after(strip);
  }
  strip.hidden = false;
  const contexts = await tickerContexts(state.assets);
  const adding = strip.querySelector(".tick-input");
  if (adding && document.activeElement === adding) return;   // do not redraw under a field being typed in
  const addBox = el("span", { class: "tick-add" }, [el("input", { type: "text", class: "tick-input", placeholder: "+ asset", list: "tick-universe",
    autocomplete: "off", "aria-label": "Add an asset to the price strip", size: "8" })]);
  addBox.firstChild.addEventListener("focus", loadUniverse);
  const addAsset = () => {
    const name = addBox.firstChild.value.trim();
    if (!name) return;
    const s = tickerState();
    const list = document.getElementById("tick-universe");   // the market's own spelling (kPEPE, xyz:AMD) when it is known
    const known = list && [...list.options].find((option) => option.value.toLowerCase() === name.toLowerCase());
    const asset = known ? known.value : name.includes(":") ? name.replace(/^([A-Za-z0-9]+):(.*)$/, (m, dex, coin) => dex.toLowerCase() + ":" + coin.toUpperCase()) : name.toUpperCase();
    if (!s.assets.includes(asset)) s.assets.push(asset);
    addBox.firstChild.value = "";
    saveTicker(s); feedback("look", "price strip add", asset); refreshTicker();
  };
  addBox.firstChild.addEventListener("keydown", (event) => { if (event.key === "Enter") addAsset(); });
  addBox.firstChild.addEventListener("change", () => {   // a suggestion picked from the list adds at once
    const value = addBox.firstChild.value.trim();
    const list = document.getElementById("tick-universe");
    if (list && [...list.options].some((option) => option.value.toLowerCase() === value.toLowerCase())) addAsset();
  });
  const dropper = (asset) => {
    const x = el("button", { type: "button", class: "tick-x", title: "Remove " + asset, "aria-label": "Remove " + asset, text: "×" });
    x.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); const s = tickerState(); s.assets = s.assets.filter((a) => a !== asset); saveTicker(s); refreshTicker(); });
    return x;
  };
  strip.replaceChildren(...state.assets.map((asset) => {
    const context = contexts[asset];
    if (!context || !isFinite(context.price)) return el("a", { class: "tick", href: hlLink(asset), target: "_blank", rel: "noopener noreferrer" },
      [el("b", { text: marketName(asset) }), el("span", { class: "muted", text: "n/a" }), dropper(asset)]);
    const change = context.previous ? context.price / context.previous - 1 : NaN;
    const direction = lastPrices[asset] === undefined || lastPrices[asset] === context.price ? "" : context.price > lastPrices[asset] ? "up" : "down";
    lastPrices[asset] = context.price;
    return el("a", { class: "tick " + direction, href: hlLink(asset), target: "_blank", rel: "noopener noreferrer", title: asset }, [
      el("b", { text: marketName(asset) }), el("span", { class: "price", text: priceText(context.price) }),
      el("span", { class: "change " + signClass(change), text: isNum(change) ? pct(change) : "" }), dropper(asset)]);
  }).concat([addBox]));
}
setInterval(refreshTicker, 5000);
refreshTicker();
function tickerMenu() {   // the Sections menu's part: on or off, and the assets
  const state = tickerState();
  const box = el("input", { type: "checkbox", id: "ticker-on" });
  box.checked = state.on;
  box.addEventListener("change", () => { const s = tickerState(); s.on = box.checked; saveTicker(s); feedback("look", "price strip", s.on ? "on" : "off"); refreshTicker(); });
  const field = el("input", { type: "text", class: "ticker-input", placeholder: "Add asset (BTC, xyz:AMD)", "aria-label": "Add an asset to the price strip" });
  const add = () => {
    const name = field.value.trim();
    if (!name) return;
    const s = tickerState();
    const asset = name.includes(":") ? name.toLowerCase().replace(/^([a-z0-9]+):(.*)$/, (m, dex, coin) => dex + ":" + coin.toUpperCase()) : name.toUpperCase();
    if (!s.assets.includes(asset)) s.assets.push(asset);
    s.on = true;
    saveTicker(s); feedback("look", "price strip add", asset);
    field.value = "";
    drawSectionsMenu(); refreshTicker();
  };
  field.addEventListener("keydown", (event) => { if (event.key === "Enter") add(); });
  const chips = state.assets.map((asset) => {
    const remove = el("button", { type: "button", class: "ticker-remove", title: "Remove " + asset, "aria-label": "Remove " + asset, text: "×" });
    remove.addEventListener("click", () => { const s = tickerState(); s.assets = s.assets.filter((a) => a !== asset); saveTicker(s); drawSectionsMenu(); refreshTicker(); });
    return el("span", { class: "ticker-chip" }, [marketName(asset), remove]);
  });
  return [el("label", { class: "sections-row" }, [box, el("span", { text: "Live prices on top" })]),
    el("div", { class: "ticker-edit" }, [field, el("div", { class: "ticker-chips" }, chips)])];
}


// ---------------------------------------------------------------- report (as IB's Reports): a printable page of the desk
// "Report" opens one page with the key numbers, the highlights and every table as they are on screen now, light and ready
// to print or save as a PDF from the browser (Ctrl/Cmd+P).
function reportHtml() {
  const escape = (text) => String(text === undefined || text === null ? "" : text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const data = latest.data, now = new Date(latest.server_time || Date.now());
  const stamp = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", dateStyle: "long", timeStyle: "short" }).format(now);
  const tiles = [...document.querySelectorAll("#tiles .tile")].map((tile) => {
    const text = (selector) => (tile.querySelector(selector) || {}).textContent || "";
    return `<div class="k"><div class="l">${escape(text(".label"))}</div><div class="v">${escape(text(".value"))}</div><div class="s">${escape(text(".sub"))}</div></div>`;
  }).join("");
  const rows = (list) => (list || []).map(([area, text]) => `<li><b>${escape(area)}</b> ${escape(text)}</li>`).join("") || "<li>Nothing to report.</li>";
  const context = { deskValue: (data.kpis || {}).desk_value };
  const titles = { performance: "Performance", strategies: "Performance by strategy", accounts: "Accounts", exposure: "Exposure by asset class",
    positions: "Open positions: crypto", wallets: "Wallets holdings", positions_stocks: "Open positions: stocks and tradfi",
    candidates: "Short candidates", market_making: "Market making", mm_candidates: "Market-making candidates" };
  const tables = TABLES.map((id) => {
    const spec = applyView(id, data.tables && data.tables[id], context);
    if (!spec || !spec.columns || !spec.columns.length || !(spec.rows || []).length) return "";
    const cell = (row, column) => { const f = FORMATS[column.fmt] || FORMATS.text; const t = f.text(row[column.key]); return `<td class="${f.num ? "n" : ""}">${escape(ZERO_TEXT.test(t) ? "" : t)}</td>`; };
    const body = spec.rows.map((row) => `<tr>${spec.columns.map((column) => cell(row, column)).join("")}</tr>`).join("");
    const foot = spec.total && spec.rows.length > 1 ? `<tr class="t">${spec.columns.map((column) => cell(spec.total, column)).join("")}</tr>` : "";
    return `<h2>${escape(titles[id] || id)}</h2><table><thead><tr>${spec.columns.map((column) => `<th>${escape(column.key)}</th>`).join("")}</tr></thead><tbody>${body}${foot}</tbody></table>`;
  }).join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Trading desk report, ${escape(stamp)}</title>
<style>
  body { font: 12px/1.45 -apple-system, "Segoe UI", system-ui, sans-serif; color: #111; margin: 24px auto; max-width: 980px; padding: 0 16px; }
  h1 { font-size: 20px; margin: 0 0 2px; } .as { color: #666; margin-bottom: 14px; }
  h2 { font-size: 13px; margin: 18px 0 6px; border-bottom: 1px solid #ccc; padding-bottom: 3px; }
  .keys { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; } .k { border: 1px solid #ddd; border-radius: 6px; padding: 6px 8px; }
  .l { color: #666; font-size: 11px; } .v { font-size: 16px; font-weight: 600; } .s { color: #666; font-size: 10px; }
  ul { margin: 4px 0; padding-left: 18px; } li { margin: 2px 0; }
  table { border-collapse: collapse; width: 100%; font-size: 11px; page-break-inside: auto; } tr { page-break-inside: avoid; }
  th, td { border-bottom: 1px solid #e4e4e4; padding: 3px 6px; text-align: left; white-space: nowrap; } th { color: #555; font-weight: 600; }
  td.n { text-align: right; font-variant-numeric: tabular-nums; } tr.t td { font-weight: 700; border-top: 1px solid #999; }
  .bar { margin-bottom: 12px; } button { font: inherit; padding: 4px 12px; cursor: pointer; }
  @media print { .bar { display: none; } body { margin: 0; max-width: none; } }
</style></head><body>
<div class="bar"><button onclick="window.print()">Print / save as PDF</button></div>
<h1>Trading desk report</h1><div class="as">${escape(stamp)} ET · from the dashboard's data</div>
<div class="keys">${tiles}</div>
<h2>What stands out</h2><ul>${rows(data.highlights && data.highlights.well)}</ul>
<h2>Needs attention</h2><ul>${rows(data.highlights && data.highlights.attention)}</ul>
${tables}
</body></html>`;
}
function openReport() {
  if (!latest || !latest.data) return;
  if (document.documentElement.classList.contains("private") && !confirm("The numbers are hidden on the dashboard. Include them in the report?")) return;
  feedback("report", "open", "dashboard");
  const page = window.open("", "_blank");
  if (!page) return;
  page.document.open();
  page.document.write(reportHtml());
  page.document.close();
}

// ---------------------------------------------------------------- privacy: click a number and the numbers hide (as in IB)
// Click any number (a tile value, a table figure, a highlight) to blur every number on the page; click one again to
// show them. Remembered in the browser; the eye next to the title does the same.
function markNumericNotes() {   // chart notes that carry a number (end values) blur with the rest; names do not
  document.querySelectorAll(".js-plotly-plot .annotation-text").forEach((node) => node.classList.toggle("numeric-note", /\d/.test(node.textContent)));
}
function setPrivate(on) {
  document.documentElement.classList.toggle("private", on);
  try { localStorage.setItem("desk-private", on ? "1" : "0"); } catch (e) {}
  const eye = document.getElementById("privacy-eye");
  if (eye) { eye.setAttribute("aria-pressed", on ? "true" : "false"); eye.title = on ? "Show numbers" : "Hide numbers"; }
  markNumericNotes();
  feedback("look", "numbers", on ? "hidden" : "shown");
}
document.addEventListener("click", (event) => {
  if (event.target.closest && event.target.closest(".tile .value, .tile .sub, td.num, td.digits, tfoot td, .js-plotly-plot .textpoint, .js-plotly-plot .bartext")) {
    setPrivate(!document.documentElement.classList.contains("private"));
  }
});
(function addEye() {
  const title = document.querySelector(".topbar h1");
  if (!title) return;
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  [["viewBox", "0 0 16 16"], ["width", "16"], ["height", "16"], ["fill", "none"], ["stroke", "currentColor"], ["stroke-width", "1.5"],
    ["stroke-linecap", "round"], ["aria-hidden", "true"]].forEach(([k, v]) => svg.setAttribute(k, v));
  ["M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z", "M8 6.2a1.8 1.8 0 1 0 0 3.6 1.8 1.8 0 0 0 0-3.6z"]
    .forEach((d) => { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); svg.append(p); });
  const eye = el("button", { type: "button", class: "icon-button privacy-eye", id: "privacy-eye", "aria-label": "Hide or show numbers" }, [svg]);
  eye.addEventListener("click", () => setPrivate(!document.documentElement.classList.contains("private")));
  title.after(eye);
  setPrivate(document.documentElement.classList.contains("private"));
})();

watchUse();
setupSections();   // after every const above is defined (the Sections menu reads the price strip's)
poll();
