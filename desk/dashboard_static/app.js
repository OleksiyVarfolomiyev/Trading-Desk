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
    style: document.documentElement.dataset.style, mode: document.documentElement.dataset.mode, screen: document.documentElement.dataset.screen });
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
  // The 24h figure is the position's own move (a short whose price fell gained): the highlights' winners and losers, the
  // chart's bars and this column are green and red together, and the total row (the book's gain) is in the same terms.
  derive: (r) => ({ Symbol: marketName(r.Symbol),
    "24h change (%)": r.Symbol && isNum(r["24h change (%)"]) && isNum(r.Value) && r.Value < 0 ? -r["24h change (%)"] : r["24h change (%)"] }),   // the total row has no symbol: it is already the book's gain
  size: (r) => Math.abs(r.Value),   // rows and bars run largest position first
  // A symbol opens its page: HL's trade page for an HL market, a quote page for an IB stock (as on HL).
  link: (r) => (/^IB/.test(String(r.Account || "")) ? stockLink(r.Symbol) : hlLink(r.Symbol)),
  // No Side column: Value is signed and coloured (shorts negative, red), which already says the side.
  // % first; dollars on hover (the row's pop-up); the account is the desk's business
  hover: (r) => [isNum(r.Value) ? usd(r.Value) : "", isNum(r.uPnL) ? `${usd(r.uPnL)} uPnL` : ""].filter(Boolean).join(" · "),
  columns: [["Symbol"], ["24h change (%)", "D"], ["uPnL (%)", "uPnL"], ["Funding (APR)"], ["Weight"]],
  // The crypto and the stocks tables sit one above the other: the same columns at the same widths, so Value, uPnL
  // and 24h line up (no column is collapsed or hidden in either).
  align: { "Symbol": 20, "D": 20, "uPnL": 20, "Funding (APR)": 20, "Weight": 20 },
};
const VIEWS = {
  performance: {
    // One window at a time, selected in the table toolbar.
    columnsNow: () => {
      const window = { "1D": ["Return 24h", "D"], "1W": ["Return 7D", "W"], "1M": ["Return 30D", "M"], All: ["Return since inception", "since start"] }[currentScale("performance")];
      return [["Account", " "], window];
    },
    columns: [["Account", " "], ["Return 24h", "D"], ["Return 7D", "W"], ["Return 30D", "M"],
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
    hover: (r) => [isNum(r.Value) ? usd(r.Value) : "", r.Chain, r.Note].filter(Boolean).join(" · "),
    columns: [["Symbol"], ["24h change (%)", "D"], ["uPnL (%)", "uPnL"], ["Funding (APR)"], ["% of desk"]],
    align: { "Symbol": 20, "D": 20, "uPnL": 20, "Funding (APR)": 20, "% of desk": 20 },
  },
  // The "vs BTC" figure over one period at a time (the card's chips: 1D, 1W, 1M), named with its period; the rest as before.
  candidates: { link: (r) => hlLink(r.Coin),
    columnsNow: () => {
      const scale = currentScale("candidates");
      const pick = { "1D": ["vs BTC 24h", "vs BTC D"], "1W": ["vs BTC 7d", "vs BTC W"], "1M": ["vs BTC 30d", "vs BTC M"], All: ["vs BTC 30d", "vs BTC M"] }[scale];
      return [["Coin"], pick, ["Corr BTC 30d", "Corr BTC"], ["From high 60d"], ["Funding 7d (APR)", "Funding W (APR)"], ["OI / volume"], ["Volume 24h", "Volume D"], ["Notes"]];
    },
    columns: [["Coin"], ["Basis"], ["vs BTC 30d"], ["vs BTC 7d"], ["Corr BTC 30d"], ["From high 60d"], ["Funding 7d (APR)"],
    ["OI / volume"], ["Volume 24h"], ["Notes"]] },
  mm_candidates: { link: (r) => hlLink(r.Coin), columns: [["Coin"], ["Daily range 30d"], ["Longs pay 7d (APR)"], ["Own ladder edge per $1k"], ["Volume 24h"], ["Notes"]] },
  // Live ladders only (stopped ones are research); the ladder's result as a % of what it traded, the rest on hover.
  market_making: { derive: (r) => ({ Coin: marketName(r.Coin), _return: isNum(r["Ladder PnL"]) && r.Volume ? r["Ladder PnL"] / r.Volume : NaN }),
    keep: (r) => !/stopped/i.test(String(r.Status || "")),
    link: (r) => hlLink(r.Coin),
    hover: (r) => [isNum(r["Ladder PnL"]) ? usd(r["Ladder PnL"]) : "", isNum(r.Fills) ? `${r.Fills} fills` : "", r.Since ? `since ${r.Since}` : "",
      r.Status || ""].filter(Boolean).join(" · "),
    columns: [["Coin"], ["_return", "Return", "spct"]] },
};

function columnsFor(view) {   // the uPnL column in dollars when the $ switch is on; a view can choose its own columns
  if (view.columnsNow) return view.columnsNow();
  return pnlUnit === "usd" ? view.columns.map((column) => (column[0] === "uPnL (%)" ? ["uPnL", "uPnL"] : column)) : view.columns;
}
// Wallet tokens sit in the crypto positions table (the wallet is where the money sits, not a separate book).
function withWallets(id, tables) {
  const spec = tables[id];
  if (id !== "positions" || !spec || !tables.wallets || !(tables.wallets.rows || []).length) return spec;
  const extra = tables.wallets.rows.map((row) => ({ Symbol: row.Symbol, Value: row.Value, "24h change (%)": row["24h change (%)"],
    "uPnL (%)": row["uPnL (%)"], uPnL: row.uPnL, Account: row.Wallet || row.Account, _wallet: true }));
  return Object.assign({}, spec, { rows: spec.rows.concat(extra) });
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
    if (view.size) out.__size = view.size(full);
    out.__raw = { Account: full.Account, Value: full.Value, Symbol: full.Symbol, Coin: full.Coin };   // what a row's actions need
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
  performance: { kind: "grouped", label: " ", series: ["D", "W", "M", "90d", "Since start"], by: "window" },
  accounts: { kind: "grouped", label: "Account", series: ["Gross", "Net"] },
  exposure: { kind: "exposure", books: true, label: "Asset class", long: "Long", short: "Short", net: "Net" },
  positions: { kind: "bars", label: "Symbol", value: "uPnL", text: null, zeroCentered: true },
  positions_stocks: { kind: "bars", label: "Symbol", value: "uPnL", text: null, zeroCentered: true },
  wallets: { kind: "bars", label: "Symbol", value: "% of desk", text: null },
  market_making: { kind: "bars", label: "Coin", value: "Return", text: null },
  strategies: { kind: "bookstack", label: "Strategy" },   // two bars, crypto and stocks, each stacked by strategy
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

// Cascaded drawing: a chart below the first screen is drawn when it scrolls into view (the first screen comes first).
const lazyCharts = new Map();   // host element -> the draw waiting for it
const lazyObserver = typeof IntersectionObserver !== "undefined" ? new IntersectionObserver((entries) => entries.forEach((entry) => {
  if (!entry.isIntersecting || !lazyCharts.has(entry.target)) return;
  const draw = lazyCharts.get(entry.target);
  lazyCharts.delete(entry.target);
  lazyObserver.unobserve(entry.target);
  draw();
}), { rootMargin: "300px" }) : null;
function whenVisible(host, draw) {
  const box = host.getBoundingClientRect();
  if (!lazyObserver || (box.top < window.innerHeight + 300 && box.bottom > -300)) { draw(); return; }
  lazyCharts.set(host, draw);
  lazyObserver.observe(host);
}
// A bar's colour is its asset class (crypto orange, stocks blue); the side is the direction it points and the sign colour of its figure.
function kindColor(id, row) {
  const stocks = id === "positions_stocks" || /^pub-desk_stocks/.test(id) || String(row.Symbol || row.Coin || "").includes(":") || /^(equity|stock|etf|tradfi)/i.test(String(row["Asset class"] || ""));
  return token(stocks ? "--kind-stocks" : "--kind-crypto");   // a tile pulled from the stocks notebook is the stocks book: blue
}

// Bars as list rows (the highlights' design): a name, a bar with its figures, and what can be done with the row on hover (trim or
// close a position, refuse, bookmark or act on a candidate, open its page). Plain HTML, so every row is actionable.
function kindClass(id, row) {
  const stocks = id === "positions_stocks" || /^pub-desk_stocks/.test(id) || String(row.Symbol || row.Coin || "").includes(":") || /^(equity|stock|etf|tradfi)/i.test(String(row["Asset class"] || ""));
  return stocks ? "stocks" : "crypto";
}
function renderBarList(id, spec, plan, host) {
  const rows = spec.rows.slice();
  const formatOf = (key) => (spec.columns.find((column) => column.key === key) || {}).fmt;
  const text = (key, value) => (FORMATS[formatOf(key)] || FORMATS.text).text(value);
  let list = rows.filter((row) => isNum(row[plan.value]));
  if (id in CANDIDATE_TABLES) {
    const s = candidateState(), week = 7 * 86400000;
    list = list.filter((row) => !(s.refused[row.Coin] && Date.now() - s.refused[row.Coin] < week));
    list.sort((a, b) => Number(s.starred.includes(b.Coin)) - Number(s.starred.includes(a.Coin)));
  } else {
    const bySize = list.some((row) => isNum(row.__size));
    list.sort((a, b) => (bySize ? (b.__size || 0) - (a.__size || 0) : Math.abs(b[plan.value]) - Math.abs(a[plan.value])));
  }
  const sizes = list.map((row) => Math.abs(row[plan.value])).sort((a, b) => b - a);
  const cap = sizes.length > 2 ? sizes[1] * BAR_CAP : Infinity;   // one dominant bar is drawn at most 3x the second
  const biggest = Math.min(sizes[0] || 1, cap) || 1;
  const labelCounts = new Map();
  list.forEach((row) => { const label = String(row[plan.label] ?? ""); labelCounts.set(label, (labelCounts.get(label) || 0) + 1); });
  const labelSeen = new Map();
  const windowName = { "1D": "D", D: "D", "7D": "W", W: "W", "30D": "M", M: "M", "90D": "Q", Q: "Q", "1Y": "Y", "365D": "Y", Y: "Y" };
  const rowLabel = (row) => {
    const label = String(row[plan.label] ?? "");
    // This published dashboard tile compares the same book leg over several windows.
    // Include the window on every bar so its category axis identifies each result.
    if (id === "pub-desk_crypto-picks-vs-btc" && row.Window) {
      return `${label} · ${windowName[row.Window] || row.Window}`;
    }
    if (labelCounts.get(label) <= 1) return label;
    let suffix = row.Window ? ` · ${windowName[row.Window] || row.Window}` : row.Account ? ` (${row.Account})` : "";
    if (!suffix) { const seen = (labelSeen.get(label) || 1) + 1; labelSeen.set(label, seen); suffix = ` #${seen}`; }
    return label + suffix;
  };
  const mixed = plan.zeroCentered
    || (list.some((row) => row[plan.value] < 0) && list.some((row) => row[plan.value] > 0));
  const body = el("div", { class: "barlist" + (plan.zeroCentered ? " zero-centered" : "") + (id.startsWith("pub-") ? " wide-labels" : "") }, list.map((row) => {
    const value = row[plan.value], share = Math.min(Math.abs(value), cap) / biggest, kind = kindClass(id, row);
    const label = rowLabel(row), extra = plan.text ? row[plan.text] : null;
    const clipped = Math.abs(value) > cap, width = Math.max(2, share * (mixed ? 50 : 100));
    const fill = el("span", { class: "bfill kind-bg-" + kind, style: mixed ? (value < 0 ? `right:50%;width:${width}%` : `left:50%;width:${width}%`) : `left:0;width:${width}%` });
    const endStyle = mixed
      ? (value < 0 ? `right:max(0px,min(${50 + width}%,calc(100% - 88px)))` : `left:max(0px,min(${50 + width}%,calc(100% - 88px)))`)
      : `left:max(0px,min(calc(${width}% + 4px),calc(100% - 88px)))`;
    const figures = [[plan.value, value], ...(plan.text && plan.text !== plan.value ? [[plan.text, extra]] : [])]
      .filter(([, number]) => number !== null && number !== undefined && number !== "");
    const end = figures.length ? el("span", { class: "bfig", style: endStyle }, [
      ...(clipped ? [el("span", { class: signClass(value), text: "» " })] : []),
      ...figures.flatMap(([key, number], index) => [
        ...(index ? [el("span", { text: " · " })] : []),
        el("span", {
          class: isNum(number) ? signClass(number) : "",
          style: isNum(number) && number !== 0 ? `color:var(${number > 0 ? "--good-text" : "--bad-text"})` : "",
          text: text(key, number),
        }),
      ]),
    ]) : null;
    const heldSide = (row.__raw && isNum(row.__raw.Value)) ? (row.__raw.Value < 0 ? "short" : "long") : null;   // colour by side, so no long or short word
    const market = (row.__raw && (row.__raw.Symbol || row.__raw.Coin)) || row[plan.label];
    const name = isAssetColumn(plan.label)
      ? assetButton(market, friendlyName(label), "bname name-" + (heldSide || kind))
      : el("span", { class: "bname name-" + (heldSide || kind), text: friendlyName(label) });
    const notable = [isNum(row["D"]) && Math.abs(row["D"]) >= 0.05 ? `${text("D", row["D"])} D` : "", isNum(row["Funding (APR)"]) && Math.abs(row["Funding (APR)"]) >= 0.5 ? `${text("Funding (APR)", row["Funding (APR)"])} funding` : ""].filter(Boolean).join(" · ");
    const acts = el("span", { class: "bacts" }, []);
    if (id in CANDIDATE_TABLES) candidateRowButtons(id, row).forEach((b) => acts.append(b));
    else if (window.DESK_SOURCE && /^HL /.test(String((row.__raw || {}).Account || "")) && isNum((row.__raw || {}).Value)) {
      [["Trim ⅓", "reduce", 1 / 3], ["Raise ⅓", "add", 1 / 3], ["Close", "close", 1]].forEach(([word, change, fraction]) => {
        const b = el("button", { type: "button", class: "row-act", title: `${word}: ${label}`, "aria-label": `${word} ${label}`, text: change === "close" ? "✕" : change === "add" ? "▲" : "⅓" });
        b.addEventListener("click", (event) => { event.stopPropagation(); runAction(word, change, fraction, { coin: row.__raw.Symbol, wallet: String(row.__raw.Account).slice(-4) }); });
        acts.append(b);
      });
    }
    const raw = row.__raw || {};
    if (!(id in CANDIDATE_TABLES) && !/^HL /.test(String(raw.Account || "")) && id !== "market_making" && isNum(raw.Value)) {   // IB stocks: copy the order
      [["⅓", `trim ${label} by a third`, `Trim ⅓: ${label} (copied for Claude)`], ["✕", `close ${label}`, `Close ${label} (copied for Claude)`]].forEach(([symbol, sentence, tip]) => {
        const b = el("button", { type: "button", class: "row-act", title: tip, "aria-label": tip, text: symbol });
        b.addEventListener("click", (event) => { event.stopPropagation(); copyOrder(b, sentence); });
        acts.append(b);
      });
    }
    if (id === "market_making" && window.DESK_SOURCE) {
      const wallet = String(raw.Account || "").trim().slice(-4);
      if (/^[0-9a-fA-F]{4}$/.test(wallet)) {
        const b = el("button", { type: "button", class: "row-act", title: "Stop this ladder", "aria-label": `Stop the ${label} ladder`, text: "⏹" });
        b.addEventListener("click", (event) => { event.stopPropagation(); runLadder("stop", raw.Coin || label, wallet); });
        acts.append(b);
      }
    }
    if (id === "mm_candidates" && window.DESK_SOURCE) {
      const b = el("button", { type: "button", class: "row-act", title: "Start a neutral ladder", "aria-label": `Start a neutral ladder on ${label}`, text: "▶" });
      b.addEventListener("click", (event) => { event.stopPropagation(); runLadder("neutral", row.Coin, "c248"); });
      acts.append(b);
    }
    if (row.__link) acts.append(el("a", { class: "row-act symbol-link", href: row.__link, target: "_blank", rel: "noopener noreferrer", title: "Open its page", text: "↗" }));
    const line = el("div", { class: "brow" + (notable && !plan.zeroCentered ? " notable" : ""),
      title: plan.zeroCentered ? "" : notable || row.__hover || "" }, [name, el("span", { class: "btrack" }, [fill, end]), acts]);
    gestures(line, id in CANDIDATE_TABLES ? {
      dislike: () => { const s = candidateState(); s.refused[row.Coin] = Date.now(); saveCandidates(s); feedback("candidate", "refuse", row.Coin, id); if (lastSpecs[id]) renderTable(id, lastSpecs[id]); },
      like: () => { const s = candidateState(); if (!s.starred.includes(row.Coin)) s.starred.push(row.Coin); saveCandidates(s); feedback("candidate", "bookmark", row.Coin, id); if (lastSpecs[id]) renderTable(id, lastSpecs[id]); },
    } : {
      dislike: () => { feedback("position", "dislike", label, id); line.style.opacity = ""; line.style.transform = ""; },   // a position stays; the verdict is logged
      like: () => feedback("position", "like", label, id),
    });
    return line;
  }));
  host.replaceChildren(body);
  return true;
}

function renderTableChart(id, spec, override) {
  const host = document.getElementById("t-" + id);
  const plan = override || chartPlan(id, spec);
  if (typeof Plotly === "undefined" || !plan) return false;
  if (plan.kind === "bars") return renderBarList(id, spec, plan, host);   // plain rows with actions on hover, not a Plotly chart
  const rows = spec.rows.slice();
  const formatOf = (key) => (spec.columns.find((column) => column.key === key) || {}).fmt;
  const text = (key, value) => (FORMATS[formatOf(key)] || FORMATS.text).text(value);
  const labels = rows.map((row) => String(row[plan.label] ?? ""));
  // A label that repeats gets what tells its rows apart: the window (Net · M, Long · W) or, for a symbol held in two
  // accounts, the account.
  const name = (row, index) => {
    if (labels.filter((l) => l === labels[index]).length <= 1) return labels[index];
    if (row.Window) return `${labels[index]} · ${row.Window}`;
    return row.Account ? `${labels[index]} (${row.Account})` : labels[index];
  };
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
    if (plan.by === "window") {   // one group per window, one bar per book (crypto, stocks) inside it, in the book's colour
      const book = (row) => String(row.Book || row.Account || row[plan.label] || (row.__raw || {}).Account || "");
      const kept = rows.map((row, i) => [row, i]).filter(([row]) => /^(crypto|stocks)$/i.test(book(row)));
      traces = kept.map(([row, i]) => ({ type: "bar", orientation: "h", name: names[i], y: series, base: 0, showlegend: false,
        x: series.map((key) => (isNum(row[key]) ? row[key] * axis.mult : null)),
        marker: { color: /crypto/i.test(book(row)) ? token("--kind-crypto") : token("--kind-stocks") },
        text: series.map((key) => text(key, row[key])), textposition: "outside", cliponaxis: false,
        outsidetextfont: LABEL_FONT(12, series.map((key) => (row[key] < 0 ? bad : good))),
        hoverinfo: "skip" }));   // the value is on the bar
    layout = { barmode: "group", xaxis: Object.assign({ zeroline: true, autorange: true, rangemode: "tozero" }, axis), margin: { l: 48, r: 40, t: 2, b: 6 } };
      height = Math.max(220, series.length * (rows.length * 16 + 18) + 80);
    } else traces = series.map((key, i) => ({ type: "bar", orientation: "h", name: key, y: names,
      x: rows.map((row) => (isNum(row[key]) ? row[key] * axis.mult : null)), marker: { color: colors[i % colors.length] },
      text: rows.map((row) => text(key, row[key])), textposition: "outside", cliponaxis: false,
      outsidetextfont: LABEL_FONT(12, rows.map((row) => (row[key] < 0 ? bad : good))),
      hoverinfo: "skip" }));   // the value is on the bar
    if (plan.by !== "window") {
      layout = { barmode: "group", xaxis: Object.assign({ zeroline: true }, axis) };
      height = Math.max(180, rows.length * (series.length * 14 + 14) + 80);
    }
  } else if (plan.kind === "bookstack") {   // the one performance tile: a bar per book, its segments the strategies
    const list = rows.filter((row) => row.Strategy && !/^total$/i.test(String(row.Strategy)));
    const axis = chartAxis(formatOf("Crypto"));
    const books = [["Crypto", token("--kind-crypto")], ["Stocks", token("--kind-stocks")]];
    traces = list.filter((row) => books.some(([book]) => isNum(row[book]) && row[book] !== 0)).map((row, i) => {
      const shade = (hex, k) => withAlpha(hex.startsWith("#") ? hex : "#888888", 1 - 0.12 * k);
      return { type: "bar", orientation: "h", name: String(row.Strategy), y: books.map(([book]) => book), showlegend: false,
        x: books.map(([book]) => (isNum(row[book]) ? row[book] * axis.mult : 0)),
        marker: { color: books.map(([, color]) => shade(color, i)), line: { color: token("--surface"), width: 1.5 } },
        text: books.map(([book]) => (isNum(row[book]) && row[book] !== 0 ? `${row.Strategy} ${text(book, row[book])}` : "")),
        textposition: "inside", insidetextanchor: "middle", insidetextfont: { size: 9, color: "#ffffff" },
        hovertext: books.map(([book]) => (isNum(row[book]) && row[book] !== 0 ? `${row.Strategy} ${text(book, row[book])}` : "")), hoverinfo: "text" };
    });
    layout = { barmode: "relative", bargap: 0.1, hovermode: "closest", xaxis: Object.assign({ zeroline: true }, axis), uniformtext: { mode: "show", minsize: 8 } };
    height = 170;
    names.splice(0, names.length, ...books.map(([book]) => book));
  } else if (plan.kind === "stacked" && !plan.series.some((key) => rows.some((row) => isNum(row[key]) && row[key] !== 0))) {
    return renderTableChart(id, spec, { kind: "bars", label: plan.label, value: "Total", text: null });   // no split yet: the totals
  } else if (plan.kind === "stacked") {   // one bar per row, its parts stacked in the book colours
    const series = plan.series.filter((key) => rows.some((row) => isNum(row[key]) && row[key] !== 0));
    const axis = chartAxis(formatOf(series[0] || plan.series[0]));
    const colour = (key) => (/crypto/i.test(key) ? token("--kind-crypto") : token("--kind-stocks"));
    traces = series.map((key) => ({ type: "bar", orientation: "h", name: key, y: names, marker: { color: colour(key) },
      x: rows.map((row) => (isNum(row[key]) ? row[key] * axis.mult : null)),
      text: rows.map((row) => (isNum(row[key]) && row[key] !== 0 ? text(key, row[key]) : "")), textposition: "inside", insidetextanchor: "middle",
      textfont: LABEL_FONT(11, "#ffffff"), hoverinfo: "skip", showlegend: false }));
    layout = { barmode: "relative", xaxis: Object.assign({ zeroline: true }, axis), uniformtext: { mode: "show", minsize: 8 } };
    height = Math.max(160, rows.length * 34 + 70);
  } else if (plan.kind === "exposure" && plan.books) {   // two bars, crypto and stocks: net exposure to equity, in their colours
    const sum = (list, key) => list.reduce((total, row) => total + (isNum(row[key]) ? row[key] : 0), 0);
    const crypto = rows.filter((row) => /crypto/i.test(String(row["Asset class"]))), stocks = rows.filter((row) => !/crypto/i.test(String(row["Asset class"])));
    const data = [["Crypto", sum(crypto, plan.net), token("--kind-crypto")], ["Stocks", sum(stocks, plan.net), token("--kind-stocks")]].filter(([, v]) => v !== 0);
    const axis = chartAxis(formatOf(plan.net));
    traces = [{ type: "bar", orientation: "h", y: data.map(([n]) => n), x: data.map(([, v]) => v * axis.mult), marker: { color: data.map(([, , c]) => c) },
      text: data.map(([, v]) => text(plan.net, v)), textposition: "outside",
      outsidetextfont: LABEL_FONT(13), cliponaxis: false, hoverinfo: "skip", showlegend: false }];
    layout = { xaxis: Object.assign({ zeroline: true }, axis) };
    height = Math.max(120, data.length * 52 + 30);
    names.splice(0, names.length, ...data.map(([n]) => n));
  } else if (plan.kind === "exposure") {
    const axis = chartAxis(formatOf(plan.long));
    const size = (row, key) => (isNum(row[key]) ? Math.abs(row[key]) : 0);
    const biggest = Math.max(...rows.map((row) => Math.max(size(row, plan.long), size(row, plan.short)))) || 1;
    // Each class's name goes inside its larger bar when that bar has room, else it joins that bar's figure.
    const carrier = rows.map((row) => (size(row, plan.long) >= size(row, plan.short) ? plan.long : plan.short));
    const roomy = rows.map((row, i) => nameFits(size(row, carrier[i]) / biggest, names[i], host, 44));
    const bar = (key, color, title) => ({ type: "bar", orientation: "h", name: title, y: names, marker: { color },
      x: rows.map((row) => (isNum(row[key]) ? Math.abs(row[key]) * axis.mult * (key === plan.short ? -1 : 1) : null)),
      text: rows.map((row, i) => (carrier[i] === key && !roomy[i] ? `${names[i]} · ` : "") + text(key, row[key])),
      textposition: "outside", cliponaxis: false, outsidetextfont: LABEL_FONT(12, color),
      hoverinfo: "skip" });   // the value is on the bar
    traces = [bar(plan.long, good, "Long"), bar(plan.short, bad, "Short")];
    traces.forEach((trace) => { trace.showlegend = false; });
    const inside = rows.map((row, i) => (roomy[i] ? { x: 0, y: names[i], text: names[i], showarrow: false,
      xanchor: carrier[i] === plan.short ? "right" : "left", xshift: carrier[i] === plan.short ? -6 : 6,
      font: { size: 13, color: INSIDE_INK() } } : null)).filter(Boolean);
    layout = { barmode: "relative", xaxis: Object.assign({ zeroline: true }, axis), annotations: inside, insideLabels: true,
      showlegend: false, margin: { r: 36 } };
    height = Math.max(180, rows.length * 44 + 80);
  } else {  // one bar per row, green above zero, red below, ordered by size
    const bySize = rows.some((row) => isNum(row.__size));
    const order = rows.map((row, index) => index).filter((index) => isNum(rows[index][plan.value]))
      .sort((a, b) => bySize ? (rows[b].__size || 0) - (rows[a].__size || 0) : Math.abs(rows[b][plan.value]) - Math.abs(rows[a][plan.value]));
    const axis = chartAxis(formatOf(plan.value));
    // One holding can dwarf the rest (a $2K token among dust): a bar is drawn at most BAR_CAP times the second largest,
    // marked "»" with its true value in the label, so the small ones stay readable.
    const sizes = order.map((i) => Math.abs(rows[i][plan.value]));
    const cap = sizes.length > 2 ? sizes[1] * BAR_CAP : Infinity;
    const drawn = (i) => Math.sign(rows[i][plan.value]) * Math.min(Math.abs(rows[i][plan.value]), cap);
    const clipped = (i) => Math.abs(rows[i][plan.value]) > cap;
    const biggest = Math.max(...order.map((i) => Math.abs(drawn(i)))) || 1;
    const figures = (i) => [text(plan.value, rows[i][plan.value]), plan.text ? text(plan.text, rows[i][plan.text]) : ""]
      .filter(Boolean).join(" · ");
    const fits = (i) => nameFits(Math.abs(drawn(i)) / biggest, names[i] + "   " + figures(i), host, plan.text ? 72 : 44);
    traces = [{ type: "bar", orientation: "h", y: order.map((i) => names[i]), x: order.map((i) => drawn(i) * axis.mult),
      marker: { color: order.map((i) => kindColor(id, rows[i])) }, showlegend: false,
      // A short bar has no room for its name inside: the name joins the figures at its end instead of overlapping them.
      text: order.map((i) => (clipped(i) ? "» " : "") + (fits(i) ? figures(i) : `${names[i]} · ${figures(i)}`)),
      textposition: "outside", cliponaxis: false,
      outsidetextfont: LABEL_FONT(13, order.map((i) => (rows[i][plan.value] < 0 ? bad : good))),
      ...(() => {   // a pop-up only where there is something to see: a big 24h move or a funding rate that matters
        const notable = (row) => [
          isNum(row["D"]) && Math.abs(row["D"]) >= 0.05 ? `${text("D", row["D"])} D` : "",
          isNum(row["Funding (APR)"]) && Math.abs(row["Funding (APR)"]) >= 0.5 ? `${text("Funding (APR)", row["Funding (APR)"])} funding` : "",
        ].filter(Boolean).join(" · ");
        const lines = order.map((i) => notable(rows[i]));
        return lines.some(Boolean) ? { hovertext: lines, hoverinfo: "text", marker: { color: order.map((i) => kindColor(id, rows[i])),
          line: { color: order.map((i) => (lines[order.indexOf(i)] ? token("--warning") : "rgba(0,0,0,0)")), width: 1.5 } } } : { hoverinfo: "skip" };
      })() }];
    // The row's name sits inside its bar, from the zero line outwards (left of zero for a short), not on the axis.
    const inside = order.filter(fits).map((i) => {
      const value = rows[i][plan.value] * axis.mult;
      return { x: 0, y: names[i], text: names[i], showarrow: false, xanchor: value < 0 ? "right" : "left",
        xshift: value < 0 ? -6 : 6, font: { size: 13, color: INSIDE_INK() } };
    });
    layout = { xaxis: Object.assign({ zeroline: true }, axis), annotations: inside, insideLabels: true };
    height = Math.max(90, order.length * 28 + 14);
    // Room at the right for the longest end label (a short bar's label carries its name too).
    layout.margin = { r: Math.min(76, Math.max(plan.text ? 48 : 28, Math.max(...traces[0].text.map((t) => t.length)) * 5 + 6)) };
  }
  host.replaceChildren();
  const node = el("div", { class: "chart", id: "tc-" + id, style: `height:${height}px` });
  host.append(node);
  const margin = Object.assign({ l: 40, r: 32, t: 2, b: 6 }, layout.margin || {});
  const base = baseLayout(Object.assign({ hovermode: "closest" }, layout, { margin }));
  // Values sit at the bar ends: no x labels, grid, zero line or axis line.
  base.xaxis = Object.assign({}, base.xaxis, layout.xaxis || {}, { showspikes: false, showticklabels: false, showgrid: false,
    zeroline: false, showline: false, ticks: "" });
  // Every row gets its label: a category axis with one tick per bar (Plotly would otherwise thin crowded labels).
  const shown = traces[0] && traces[0].y ? traces[0].y : names;
  base.yaxis = Object.assign({}, base.yaxis, { type: "category", autorange: "reversed", automargin: true, showgrid: false, zeroline: false, showline: false,
    tickmode: "array", tickvals: shown, ticktext: shown, tickfont: { color: token("--ink-2"), size: plan.kind === "bars" ? 13 : 11 } });
  if (plan.kind === "exposure" && plan.books) Object.assign(base.yaxis, { showticklabels: false, tickvals: [], ticktext: [], automargin: false });
  if (layout.insideLabels) {   // names are in the bars: no axis labels; the left margin only holds a short's end text
    base.yaxis.showticklabels = false;
    const shorts = traces.some((trace) => trace.type === "bar" && (trace.x || []).some((value) => value < 0));
    base.margin = Object.assign({}, base.margin, { l: shorts ? 150 : 12 });
    delete base.insideLabels;
  } else if (plan.kind === "bookstack" || (plan.kind === "exposure" && plan.books)) {   // the bar colours distinguish the two books
    base.yaxis.showticklabels = false;
    base.margin = Object.assign({}, base.margin, { l: 12 });
  }
  traces = traces.map(colorHover);
  whenVisible(host, () => Plotly.newPlot(node, traces, base, CONFIG).then(markNumericNotes));
  return true;
}

// A displayed zero amount ($0.00, -$0.00, 0%, 0.00) is an empty cell; a whole-number 0 (a count) stays.
const ZERO_TEXT = /^-?(\$0(\.0+)?|0(\.0+)?%|0\.0+)$/;
// Candidates are answered in place: × refuse (hidden for a week), ☆ bookmark (onto the price strip, starred), ▶ act
// (plan the entry, then confirm; web dashboard). Every answer is logged, so the screener learns what is refused.
const CANDIDATE_KEY = "desk-candidates";
const CANDIDATE_TABLES = { candidates: "short", mm_candidates: null };
function candidateState() { try { return Object.assign({ refused: {}, starred: [] }, JSON.parse(localStorage.getItem(CANDIDATE_KEY)) || {}); } catch (e) { return { refused: {}, starred: [] }; } }
function saveCandidates(state) { try { localStorage.setItem(CANDIDATE_KEY, JSON.stringify(state)); } catch (e) {} }
function candidateActions(id, row) {
  const coin = row.Coin, side = CANDIDATE_TABLES[id], state = candidateState();
  const button = (text, label, action, on) => {
    const b = el("button", { type: "button", class: "row-act" + (on ? " on" : ""), "aria-label": `${label} ${coin}`, title: label, text });
    b.addEventListener("click", (event) => { event.stopPropagation(); action(); });
    return b;
  };
  const starred = state.starred.includes(coin);
  return el("td", { class: "row-actions" }, candidateButtons(id, row, button, starred, side, coin));
}
function candidateButtons(id, row, button, starred, side, coin) {
  return ([
    button("×", "Refuse", () => { const s = candidateState(); s.refused[coin] = Date.now(); saveCandidates(s); feedback("candidate", "refuse", coin, id);
      if (lastSpecs[id]) renderTable(id, lastSpecs[id]); }),
    button(starred ? "★" : "☆", starred ? "Bookmarked" : "Bookmark", () => {
      const s = candidateState(), t = tickerState();
      s.starred = starred ? s.starred.filter((c) => c !== coin) : s.starred.concat([coin]);
      if (!starred && !t.assets.includes(coin)) { t.assets.push(coin); t.on = true; saveTicker(t); refreshTicker(); drawModes(); }
      saveCandidates(s); feedback("candidate", starred ? "unbookmark" : "bookmark", coin, id);
      if (lastSpecs[id]) renderTable(id, lastSpecs[id]);
    }, starred),
    ...(side ? [button("▶", window.DESK_SOURCE ? `Act: ${side} ${coin}` : `Act: copy "${side} ${coin}"`, () => {
      feedback("candidate", "act", coin, id);
      if (window.DESK_SOURCE) runAction(side === "short" ? "Short" : "Long", side, 1, { coin, wallet: "07f2" });
      else if (navigator.clipboard) navigator.clipboard.writeText(`${side} ${coin}`);
    })] : []),
  ]);
}
function candidateRowButtons(id, row) {   // the same answers as a list row's buttons
  const coin = row.Coin, side = CANDIDATE_TABLES[id], starred = candidateState().starred.includes(coin);
  const button = (text, label, action, on) => {
    const b = el("button", { type: "button", class: "row-act" + (on ? " on" : ""), "aria-label": `${label} ${coin}`, title: label, text });
    b.addEventListener("click", (event) => { event.stopPropagation(); action(); });
    return b;
  };
  return candidateButtons(id, row, button, starred, side, coin);
}
const filterState = {};   // table id -> text: only rows that contain it are shown
const lastSpecs = {};     // table id -> the last spec drawn, so a filter redraws without new data
function signalMarks(value) {
  const signals = [
    [/kelly long/i, "K", "--series-5", "Kelly model leans long"],
    [/low correlation/i, "ρ", "--series-1", "Low correlation with BTC"],
    [/crowded long/i, "L", "--warning", "Crowded longs: longs are paying funding"],
    [/crowded short/i, "S", "--warning", "Crowded shorts: shorts are paying funding"],
    [/crowded/i, "F", "--warning", "Crowded funding"],
    [/breakout risk/i, "⚡", "--kind-crypto", "Breakout risk: price is near a 60-day range edge"],
    [/large size/i, "↗", "--bad-text", "Large size: trades of this size lost money in desk history"],
    [/weak side/i, "↓", "--bad-text", "Weak side in desk trade history"],
    [/weak market/i, "!", "--bad-text", "Weak historical result for this market"],
    [/(stress|refused|cut to)/i, "!", "--bad-text", "Stress scenario warning"],
  ];
  const notes = String(value || "").split(/;\s*/).filter(Boolean);
  const marks = [], used = new Set();
  notes.forEach((note) => {
    const signal = signals.find(([pattern]) => pattern.test(note));
    if (signal) {
      const key = signal[1] + signal[3];
      if (used.has(key)) return;
      used.add(key);
      marks.push(el("span", { class: "signal-mark", style: `color:var(${signal[2]})`, title: signal[3], "aria-label": signal[3], tabindex: "0", text: signal[1] }));
    } else {
      marks.push(el("span", { class: "signal-mark signal-other", title: note, "aria-label": note, tabindex: "0", text: "•" }));
    }
  });
  return marks;
}

function tableWindow(id, host, spec) {
  const windows = [...new Set(spec.rows.map((row) => row.Window).filter((window) => TABLE_WINDOWS.includes(window)))];
  const previous = host.previousElementSibling;
  const toolbar = previous?.classList.contains("table-periods") ? previous : null;
  if (windows.length < 2) {
    toolbar?.remove();
    return spec;
  }
  windows.sort((a, b) => TABLE_WINDOWS.indexOf(a) - TABLE_WINDOWS.indexOf(b));
  const key = "table:" + id;
  const selected = windows.includes(scaleChoices[key]) ? scaleChoices[key] : "";
  const controls = toolbar || el("div", { class: "scale-chips table-periods", role: "group", "aria-label": "Table period" });
  if (controls.dataset.windows !== windows.join(",")) {
    controls.dataset.windows = windows.join(",");
    controls.replaceChildren(...["", ...windows].map((window) => {
      const button = el("button", { type: "button", class: "scale-chip", "data-window": window, text: window || "Compare" });
      button.addEventListener("click", () => {
        scaleChoices[key] = window;
        saveScaleChoices();
        feedback("table", "period", id, window || "Compare");
        renderTable(id, lastSpecs[id]);
      });
      return button;
    }));
  }
  if (!toolbar) host.before(controls);
  for (const button of controls.children) {
    const active = button.dataset.window === selected;
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", String(active));
  }
  return selected ? { ...spec, rows: spec.rows.filter((row) => row.Window === selected), total: null } : spec;
}

// The reversed RdBu scale used by the portfolio notebook’s correlation matrix.
const CORRELATION_COLORS = [
  [5, 48, 97], [33, 102, 172], [67, 147, 195], [146, 197, 222], [209, 229, 240],
  [247, 247, 247], [253, 219, 199], [244, 165, 130], [214, 96, 77], [178, 24, 43], [103, 0, 31],
];
function correlationColor(value) {
  const range = CORRELATION_COLORS.length - 1;
  const stop = (Math.max(-1, Math.min(1, value)) + 1) * range / 2;
  const lower = Math.min(Math.floor(stop), range - 1);
  const rgb = CORRELATION_COLORS[lower].map((channel, i) =>
    Math.round(channel + (CORRELATION_COLORS[lower + 1][i] - channel) * (stop - lower)));
  return `rgb(${rgb.join(",")})`;
}

function renderTable(id, spec) {
  const original = spec;
  lastSpecs[id] = spec;
  const host = document.getElementById("t-" + id);
  if (!host) return;
  host.replaceChildren();
  // An empty table has nothing to act on: its card is hidden until it has rows.
  const card = host.closest(".card");
  const numbers = spec && spec.columns ? spec.columns.filter((c) => (FORMATS[c.fmt] || FORMATS.text).num) : [];
  const allZero = numbers.length && (spec.rows || []).every((row) => numbers.every((c) => !isNum(row[c.key]) || Math.abs(row[c.key]) < 1e-9));
  const empty = !spec || !spec.columns || !spec.columns.length || !(spec.rows || []).length || allZero;
  if (card) card.hidden = empty;
  if (empty) return;
  if (id.startsWith("pub-")) spec = tableWindow(id, host, spec);
  if ((isChartView() || id === "strategies") && renderTableChart(id, spec)) return;   // the performance tile is always the stacked bars   // ?view=charts: the table drawn as a chart
  // A text column with the same value in every row is said once, above the table; an empty one is left out.
  const shared = [];
  const align = (VIEWS[id] || {}).align;
  if (spec.rows.length > 1 && !align) {
    spec = Object.assign({}, spec, { columns: spec.columns.filter((column, i) => {
      const format = FORMATS[column.fmt] || FORMATS.text;
      if (i === 0 || /^leg$/i.test(column.key)) return true;
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
  if (!state && rows.some((row) => isNum(row.__size))) rows.sort((a, b) => (b.__size || 0) - (a.__size || 0));   // largest position first
  if (id in CANDIDATE_TABLES) {
    const s = candidateState(), week = 7 * 86400000;
    rows = rows.filter((row) => !(s.refused[row.Coin] && Date.now() - s.refused[row.Coin] < week));
    rows.sort((a, b) => Number(s.starred.includes(b.Coin)) - Number(s.starred.includes(a.Coin)));
  }
  const query = (filterState[id] || "").trim().toLowerCase();
  if (query) rows = rows.filter((row) => Object.entries(row).some(([key, value]) => !key.startsWith("__") && String(value ?? "").toLowerCase().includes(query)));
  if (state) {
    rows.sort((a, b) => {
      const x = a[state.key], y = b[state.key];
      if (isNum(x) && isNum(y)) return state.dir * (x - y);
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return state.dir * String(x).localeCompare(String(y));
    });
  }
  const weightColumn = spec.columns.find((column) => /^weight$/i.test(column.key));
  const positionTable = id === "positions" || id === "positions_stocks";
  const tables = latest?.data?.tables;
  const weightRows = positionTable
    ? [...spec.rows, ...(tables?.positions?.rows || []), ...(tables?.positions_stocks?.rows || [])]
    : spec.rows;
  const maxWeight = weightColumn ? weightRows.reduce((max, row) => {
    const weight = row[weightColumn.key];
    return isNum(weight) ? Math.max(max, Math.abs(weight)) : max;
  }, 0) : 0;
  const head = el("tr", {}, spec.columns.map((column) => {
    const format = FORMATS[column.fmt] || FORMATS.text;
    const sorted = state && state.key === column.key ? (state.dir > 0 ? "ascending" : "descending") : "none";
    const th = el("th", { class: [format.num ? "num" : "", column.emphasis ? "key" : ""].filter(Boolean).join(" "),
      "aria-sort": sorted, tabindex: "0",
      title: column === weightColumn ? "Full bar = largest weight. Click to sort." : "Click to sort", text: column.key });
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
  // Key columns (desk/emphasis.py): mark the standout value picked by each column’s mode.
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
    const text = format.text(value);
    const leg = /^leg$/i.test(column.key) ? text.trim().toLowerCase() : "";
    const legColor = /^(long|short|net)$/.test(leg) ? "name-" + leg : "";
    const classes = [format.num ? "num" : "", tone, key, legColor].filter(Boolean).join(" ");
    // Any figure in a cell (a note with "$1.5K", a text column of amounts) is hidden with the numbers, not only number columns.
    const withDigits = !isAssetColumn(column.key) && /\d/.test(text) ? " digits" : "";
    if (column === weightColumn && isNum(value) && index !== undefined) {
      const share = maxWeight > 0 ? 100 * Math.abs(value) / maxWeight : 0;
      const kind = positionTable ? " kind-bg-" + kindClass(id, row) : "";
      return el("td", { class: classes + " weight-cell", title: text }, [
        el("span", { class: "weight-track", role: "img", "aria-label": `Weight ${text}` }, [
          el("span", { class: "weight-fill" + kind, style: `width:${share}%` }),
        ]),
      ]);
    }
    if (/^corr(?:elation)?\s+(?:to\s+)?btc(?:\s+30d)?$/i.test(column.key) && isNum(value)) {
      return el("td", { class: classes + withDigits + " correlation-cell", style: `--correlation-color:${correlationColor(value)}`, text });
    }
    if (isAssetColumn(column.key) && text && !/^(?:net\/)?total$/i.test(text) && !ZERO_TEXT.test(text)) {
      const market = (row.__raw && row.__raw[column.key]) || value;
      return el("td", { class: classes }, [assetButton(market, marketName(text),
        "symbol-chip table-symbol name-" + (symbolSide(market) || symbolKind(market)))]);
    }
    if (/^notes?$/i.test(column.key)) return el("td", { class: classes + withDigits + " signal-notes" }, signalMarks(value));
    if (!format.num && KIND_WORDS.test(text)) { KIND_WORDS.lastIndex = 0; return el("td", { class: classes }, kindMarks(text)); }
    KIND_WORDS.lastIndex = 0;
    return el("td", { class: classes + withDigits, text: ZERO_TEXT.test(text) ? "" : text });
  };
  const body = el("tbody", {}, rows.map((row, index) => el("tr", row.__hover ? { title: row.__hover } : {},
    spec.columns.map((column) => cell(row, column, index)).concat(id in CANDIDATE_TABLES && row.Coin ? [candidateActions(id, row)] : []))));
  if (id in CANDIDATE_TABLES) head.append(el("th", { class: "row-actions" }));
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
// The last 24 hours of the open positions by book: the price move on what was held (value now against value a day ago).
function dayByBook(tables) {
  const part = (id) => {
    const rows = ((tables[id] || {}).rows || []).filter((row) => isNum(row.Value) && isNum(row["24h change (%)"]) && row["24h change (%)"] > -1);
    const before = rows.map((row) => row.Value / (1 + row["24h change (%)"]));
    const base = before.reduce((sum, v) => sum + Math.abs(v), 0);
    const change = rows.reduce((sum, row, i) => sum + row.Value - before[i], 0);
    return { change, pct: base ? change / base : NaN };
  };
  return { crypto: part("positions"), stocks: part("positions_stocks") };
}
// A period's price move on what is held: value now against value then, over what was held then (the 24h change from the
// tables; 7 and 30 days from the moves the page builds, positions without history left out).
function periodMove(tables, scale) {
  const moves = (latest && latest.data && latest.data.moves) || {};
  const days = { "1W": "7", "1M": "30" }[scale];
  let change = 0, base = 0;
  ["positions", "positions_stocks"].forEach((id) => ((tables[id] || {}).rows || []).forEach((row) => {
    if (!isNum(row.Value)) return;
    const move = scale === "1D" ? row["24h change (%)"] : (moves[marketName(row.Symbol)] || {})[days];
    if (!isNum(move) || move <= -1) return;
    const before = row.Value / (1 + move);
    change += row.Value - before;
    base += Math.abs(before);
  }));
  return base ? change / base : NaN;
}
function bookShares(tables) {   // uPnL on cost per book, as coloured runs: what is traded now apart from the stock holdings
  const part = (id) => {
    const rows = ((tables[id] || {}).rows || []).filter((row) => isNum(row.uPnL) && isNum(row.Value) && isNum(row["uPnL (%)"]));
    const cost = rows.reduce((sum, row) => sum + Math.abs(row.Value - row.uPnL), 0);
    return cost ? rows.reduce((sum, row) => sum + row.uPnL, 0) / cost : NaN;
  };
  const crypto = part("positions"), stocks = part("positions_stocks");
  return [isNum(crypto) ? `crypto ${pct(crypto, 1, true)}` : "", isNum(stocks) ? `stocks ${pct(stocks, 1, true)}` : ""].filter(Boolean).join(" ");
}
function tile(label, value, sub, extra) {  // extra.title: the definition, on hover only
  return el("div", { class: "tile", "data-label": (extra && extra.key) || label, title: (extra && extra.title) || "" }, [
    el("div", { class: "label", text: label }),
    el("div", { class: "value " + (extra && extra.valueClass ? extra.valueClass : ""), text: value }),
    extra && extra.line && extra.line.text ? el("div", { class: "line " + (extra.line.cls || ""), text: extra.line.text,
      title: extra.line.title || "" }) : null,
    sub ? el("div", { class: "sub" }, kindMarks(sub)) : null,
    extra && extra.chips ? extra.chips : null,
    extra && extra.visual ? extra.visual : null,
  ]);
}

// "24h $123 (1.2%)", net of deposits, withdrawals and transfers; per-account parts on hover.
function dayChangeLine(day) {
  if (!day || !isNum(day.change)) return { text: "D change n/a", cls: "muted" };
  // The 24h change as a %; the dollars, per account, are on hover.
  const parts = Object.entries(day.parts || {}).map(([name, value]) => `${name}: ${usd(value)}`);
  const missing = (day.missing || []).length ? [`Not included: ${day.missing.join(", ")}`] : [];
  return {
    // A change that rounds to 0% says nothing: no line (the dollars stay on hover of the value).
    text: isNum(day.pct) && pct(day.pct) === "0%" ? "" : `D ${isNum(day.pct) ? pct(day.pct) : usd(day.change)}${missing.length ? " *" : ""}`,
    cls: signClass(day.change),
    title: [`D ${usd(day.change)}, net of deposits, withdrawals and transfers.`, ...parts, ...missing].join("\n"),
  };
}

function renderTiles(k, theses = []) {
  const r = k.returns || {};
  const mw = k.money_weighted || {};
  const times = (value) => (isNum(value) ? `${Number(value.toFixed(2))}x` : "n/a");   // 1.6x, 1.33x: no trailing zeros
  const tables = (latest && latest.data && latest.data.tables) || {};
  // uPnL on cost, as the positions tables count it (priced rows of both books)
  const priced = ["positions", "positions_stocks"].flatMap((id) => ((tables[id] || {}).rows || []))
    .filter((row) => isNum(row.uPnL) && isNum(row.Value) && isNum(row["uPnL (%)"]));
  const cost = priced.reduce((sum, row) => sum + Math.abs(row.Value - row.uPnL), 0);
  const upnl = priced.reduce((sum, row) => sum + row.uPnL, 0);
  const risk = ((tables.accounts || {}).total || {})["Open risk"];
  const day = k.day_change || {};
  // Drawdown from the money put in (what an investment agreement limits), not from a past peak: trading equity against
  // its net deposits; none while the money made is positive.
  const invested = (k.trading_equity || k.desk_value) - (mw.pnl || 0);
  const drawdown = isNum(mw.pnl) && invested > 0 ? Math.min(0, mw.pnl / invested) : NaN;
  // Number first, a short label after; what is happening now, each tile with its call to act: today and the open
  // positions (manage them), the risk room under the cap (can a trade be added), exposure, the money, and the agreement's
  // drawdown last. Returns over windows are history: the performance table below has them.
  const tiles = [
    (() => {   // the chosen window: the day's change, or the desk's return over 7 or 30 days, or since the start
      const scale = currentScale("tile"), r = k.returns || {};
      const value = scale === "1D" ? day.pct : scale === "1W" ? r["7D"] : scale === "1M" ? r["30D"] : r.since_start;
      const label = { "1D": "D", "1W": "W", "1M": "M", All: "all" }[scale];
      const byBook = dayByBook(tables);
      const title = scale === "1D" ? [`${usd(day.change)} in the day`, isNum(byBook.crypto.change) ? `crypto ${usd(byBook.crypto.change)} (${pct(byBook.crypto.pct, 1, true)})` : "",
        isNum(byBook.stocks.change) ? `stocks ${usd(byBook.stocks.change)} (${pct(byBook.stocks.pct, 1, true)})` : ""].filter(Boolean).join("\n") : "";
      return tile(label, isNum(value) ? pct(value, 1, true) : "n/a", "", { key: "Today", valueClass: signClass(value), title, chips: scaleChips("tile") });
    })(),
    (() => {   // uPnL on cost, and its change over the period: 1D, 1W or 1M by its own chips on hover
      const scale = currentScale("upnl"), change = scale === "All" ? NaN : periodMove(tables, scale);
      const label = { "1D": "D", "1W": "W", "1M": "M" }[scale];
      return tile("uPnL", cost ? pct(upnl / cost, 1, true) : "n/a", isNum(change) ? `${pct(change, 1, true)} ${label}` : bookShares(tables),
        { key: "uPnL", valueClass: signClass(upnl), chips: scaleChips("upnl"), title: isNum(k.unrealized) ? `${usd(k.unrealized)} uPnL` : "" });
    })(),
    (() => {   // risk against the cap: the room left, or how far over
      const used = isNum(risk) ? risk / RISK_CAP : NaN;
      const word = !isNum(used) ? "n/a" : used <= 1 ? `${pct(1 - used)} room` : `${pct(used - 1)} over`;
      return tile("risk", word, "", { key: "Risk", valueClass: used > 1 ? "neg" : "",
        title: isNum(risk) ? `Open risk ${pct(risk)} of equity against the 33% cap` : "" });
    })(),
    (() => {   // the room under the stress limit: the worst shock's loss against the 35% where adding entries is blocked (25% warns)
      const rows = Object.entries(k.stress || {}).filter(([, v]) => isNum(v)).sort((a, b) => a[1] - b[1]);   // mirrors desk/stress.py
      if (!rows.length) return tile("stress", "n/a", "", { key: "Stress" });
      const [name, worst] = rows[0], loss = Math.max(0, -worst), room = STRESS_BLOCK - loss;
      return tile("stress", room > 0 ? `${pct(room)} room` : `${pct(-room)} over`, `${name} ${pct(worst, 1, true)}`,
        { key: "Stress", valueClass: loss >= STRESS_WARN ? "neg" : "",
          title: rows.map(([n, v]) => `${n} ${pct(v, 1, true)}`).join("\n") });
    })(),
    (() => {   // the thesis clock: how long the open trades have been held, each with the thesis it was opened on
      const rows = theses || [];
      if (!rows.length) return tile("held", "n/a", "", { key: "Held" });
      const days = rows.map((r) => r.days).sort((a, b) => a - b), median = days[Math.floor(days.length / 2)];
      const bare = rows.filter((r) => !r.thesis).length;
      return tile("held", `${Math.round(median)}d`, bare ? `${bare} without thesis` : "", { key: "Held", valueClass: "",
        title: rows.map((r) => `${r.coin} ${String(r.side).toLowerCase()} ${Math.round(r.days)}d: ${r.thesis || "no thesis written"}`).join("\n") });
    })(),
    tile("gross", times(k.gross_leverage), isNum(k.net_leverage) ? `${times(k.net_leverage)} net` : "",
      { key: "Gross leverage" }),
    tile("", wholeUsd(k.desk_value), "", { key: "Desk value" }),
    // Extra key numbers, off until added with the row's "+": history and counts, for when they are wanted on top.
    tile("30D", pct((k.returns || {})["30D"], 1, true), "", { key: "Return 30D", valueClass: signClass((k.returns || {})["30D"]) }),
    tile("MW", pct(mw.return, 1, true) || "n/a", "", { key: "MW return", valueClass: signClass(mw.return) }),
    tile("TW", pct((k.returns || {}).since_start, 1, true), "", { key: "TW return", valueClass: signClass((k.returns || {}).since_start) }),
    tile("positions", isNum(k.positions) ? String(k.positions) : "n/a", "", { key: "Positions" }),
    // Key numbers of the account and its trades: off until added with the row's "+" (hit rate and the rest are the HL fills', every wallet).
    (() => { const total = (tables.accounts || {}).total || {}, net = k.net_leverage;
      return tile("net", isNum(net) ? pct(net, 0, true) : "n/a", "", { key: "Net exposure", valueClass: "", title: "Net exposure over trading equity" }); })(),
    (() => { const total = (tables.accounts || {}).total || {}, left = total["Available / equity"];
      return tile("margin", isNum(left) ? pct(left, 0) : "n/a", "", { key: "Margin left", valueClass: isNum(left) && left < 0.2 ? "neg" : "", title: "Available margin over equity" }); })(),
    (() => { const used = isNum(risk) ? risk / RISK_CAP : NaN;
      return tile("risk used", isNum(used) ? pct(used, 0) : "n/a", "", { key: "Risk used", valueClass: used > 1 ? "neg" : "", title: "Open risk as a share of the 33% cap" }); })(),
    (() => { const s = k.trade_stats || {};
      return tile("hit", isNum(s.hit_rate) ? pct(s.hit_rate, 0) : "n/a", "", { key: "Hit rate", title: "Share of closes in profit (HL perps, all wallets)" }); })(),
    (() => { const s = k.trade_stats || {};
      return tile("W / L", isNum(s.avg_win) && isNum(s.avg_loss) ? `${pct(s.avg_win, 1)} / ${pct(s.avg_loss, 1)}` : "n/a", "", { key: "Avg W/L", title: "Average winning and losing close, as a share of its notional" }); })(),
    (() => { const s = k.trade_stats || {};
      return tile("EV", isNum(s.ev) ? pct(s.ev, 1, true) : "n/a", "", { key: "EV/close", valueClass: signClass(s.ev), title: "Expected value of a close, as a share of its notional" }); })(),
    (() => { const s = k.trade_stats || {};
      return tile("liq", isNum(s.liquidations) ? String(s.liquidations) : "n/a", isNum(s.closes) ? `of ${s.closes} closes` : "", { key: "Liquidations", valueClass: s.liquidations > 0 ? "neg" : "" }); })(),
    tile("DD", isNum(drawdown) ? pct(drawdown, 1) : "n/a", "",
      { key: "DD", valueClass: drawdown <= -0.10 ? "neg" : "", title: isNum(invested) ? `From the ${usd(invested)} put in, net of withdrawals` : "" }),
  ];
  arrangeTiles(document.getElementById("tiles"), tiles);
}
// The key-number tiles are windows too: drag one onto another to swap, minimize to label and value, save as an image, hide
// (the Sections menu brings hidden ones back). Order, minimized and hidden are remembered with the layout.
const STRESS_WARN = 0.25, STRESS_BLOCK = 0.35;   // desk/stress.py: a scenario loss past 25% warns, past 35% blocks added entries
const EXTRA_TILES = ["Return 30D", "MW return", "TW return", "Positions"];
const EXTRA_TILES_2 = ["Net exposure", "Margin left", "Risk used", "Hit rate", "Avg W/L", "EV/close", "Liquidations"];   // off until added
let tileAddMenuOpen = false, tileAddMenuAway = null;
function arrangeTiles(host, tiles) {
  let state = layoutState();
  if (!state.tileExtras) {   // the extra key numbers start hidden, once per browser
    state.tileHidden = [...new Set([...(state.tileHidden || []), ...EXTRA_TILES])];
    state.tileExtras = true;
    saveLayout(state);
    state = layoutState();
  }
  if (!state.tileExtras2) {   // the account and trade numbers added on 2026-11-17 start hidden too
    state.tileHidden = [...new Set([...(state.tileHidden || []), ...EXTRA_TILES_2])];
    state.tileExtras2 = true;
    saveLayout(state);
    state = layoutState();
  }
  if (!state.tilesShown) {   // Stress, Held and Maker are on from the start (they were hidden for a day), once per browser
    state.tileHidden = (state.tileHidden || []).filter((name) => !["Stress", "Held", "Maker"].includes(name));
    state.tilesShown = true;
    saveLayout(state);
    state = layoutState();
  }
  const hidden = new Set(state.tileHidden || []), minimized = new Set(state.tileMin || []), order = state.tileOrder || [];
  const rank = (tile) => { const i = order.indexOf(tile.dataset.label); return i < 0 ? 1e6 : i; };
  // A value that rounds to 0% says nothing (desk rule): that tile is left out until it has something to say.
  const says = (tile) => !/^-?0%$/.test((tile.querySelector(".value") || {}).textContent || "");
  const shown = tiles.filter((tile) => !hidden.has(tile.dataset.label) && says(tile)).sort((a, b) => rank(a) - rank(b));
  const remember = (change) => { const s = layoutState(); change(s); saveLayout(s); };
  shown.forEach((tile) => {
    const label = tile.dataset.label;
    tile.classList.toggle("tile-min", minimized.has(label));
    const tool = (cls, title, text, action) => {
      const button = el("button", { type: "button", class: "card-tool " + cls, title, "aria-label": title + ": " + label, text });
      button.addEventListener("click", (event) => { event.stopPropagation(); action(); });
      return button;
    };
    tile.append(el("div", { class: "card-foot tile-foot" }, thumbs(label, "tile")));
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
    tile.addEventListener("dragover", (event) => {
      event.preventDefault();
      const box = tile.getBoundingClientRect(), left = event.clientX < box.left + box.width / 2;
      host.querySelectorAll(".drop-left, .drop-right").forEach((n) => n.classList.remove("drop-left", "drop-right"));
      tile.classList.add(left ? "drop-left" : "drop-right");
    });
    tile.addEventListener("dragleave", () => tile.classList.remove("drop-left", "drop-right"));
    tile.addEventListener("drop", (event) => {
      event.preventDefault();
      host.querySelectorAll(".drop-left, .drop-right").forEach((n) => n.classList.remove("drop-left", "drop-right"));
      const [kind, from] = event.dataTransfer.getData("text/plain").split(/:(.*)/s);
      const moving = kind === "tile" && [...host.children].find((node) => node.dataset.label === from);
      if (!moving || moving === tile) return;
      const box = tile.getBoundingClientRect();
      if (event.clientX < box.left + box.width / 2) tile.before(moving); else tile.after(moving);   // into the position pointed at
      remember((s) => { s.tileOrder = [...host.children].map((node) => node.dataset.label); });
      feedback("tile", "move", from, "before " + label);
    });
  });
  // The add menu lists available key numbers; highlighted items are currently shown and toggle off when clicked.
  const menu = () => el("div", { class: "more-menu tile-menu" }, tiles.filter(says).map((tile) => {
    const label = tile.dataset.label, selected = !hidden.has(label);
    const row = el("div", { class: "sections-row tile-pick" + (selected ? " is-picked" : ""), role: "checkbox", tabindex: "0",
      "aria-checked": String(selected) }, [el("b", { text: tile.querySelector(".value").textContent }),
      el("span", { text: tile.querySelector(".label").textContent || label })]);
    const toggle = (event) => {
      event.stopPropagation();
      remember((s) => { s.tileHidden = (s.tileHidden || []).filter((name) => name !== label).concat(selected ? [label] : []); });
      feedback("tile", selected ? "hide" : "add", label);
      if (latest && latest.data) { renderTiles(latest.data.kpis || {}, latest.data.theses); drawSectionsMenu(); }
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(event); } });
    return row;
  }));
  const plus = el("button", { type: "button", class: "tile-add", "aria-label": "Add a key number", text: "+" });
  plus.addEventListener("click", (event) => {
    event.stopPropagation();
    tileAddMenuOpen = !tileAddMenuOpen;
    const wrap = plus.closest(".tile-add-wrap");
    if (tileAddMenuOpen) {
      document.querySelectorAll(".more-menu").forEach((node) => node.remove());
      plus.after(menu());
      if (!tileAddMenuAway) {
        tileAddMenuAway = (e) => {
          if (!e.target.closest(".tile-add-wrap")) {
            tileAddMenuOpen = false;
            document.querySelectorAll(".tile-menu").forEach((node) => node.remove());
            document.removeEventListener("click", tileAddMenuAway); tileAddMenuAway = null;
          }
        };
        setTimeout(() => document.addEventListener("click", tileAddMenuAway), 0);
      }
    } else {
      wrap.querySelectorAll(".tile-menu").forEach((node) => node.remove());
      if (tileAddMenuAway) { document.removeEventListener("click", tileAddMenuAway); tileAddMenuAway = null; }
    }
  });
  const addWrap = el("div", { class: "tile-add-wrap" }, [plus]);
  if (tileAddMenuOpen) addWrap.append(menu());
  host.replaceChildren(...shown, addWrap);
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
const CRITICAL_TITLES = /cut gross|margin guard|margin reserve breached|not reporting|over cap|liquidat|^major hack$|^squeeze|^stress:.*\(block\)$|^data flow down$|^unreadable accounts$/i;
const WATCH_TITLES = /^alt rally$|^stock dip$|^equity sell[- ]off$|^stress:.*\(warn\)$|^available margin low$|near cap|^hack$|^burn approaching$|^burn$|^unlock$/i;
function severity(title, text) {
  if (CRITICAL_TITLES.test(title) || (/^regime$/i.test(title) && /\bhigh\b/.test(text))) return "critical";
  if (WATCH_TITLES.test(title) || /^regime$/i.test(title)) return "watch";
  return "";
}

// Highlights can be answered: "✓" marks one read (it disappears until its text changes), "×" says it is not important (that
// area is not shown again). Both are remembered in the browser; the Sections menu restores them.
const HIGHLIGHTS_KEY = "desk-highlights";
// "Read" hides a line until something significant changes: the words (symbols, signs) differ, or a figure has moved by more than
// a point or a quarter of its size. A refresh that only nudges the numbers does not bring it back.
const lineShape = (text) => String(text).replace(/-?\d+(?:\.\d+)?/g, "#");
const lineNumbers = (text) => (String(text).match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
function isRead(state, area, text) {
  const mine = lineNumbers(text), shape = lineShape(text);
  return Object.entries(state.read).some(([key, entry]) => {
    if (!key.startsWith(area + "|")) return false;
    if (typeof entry !== "object") return key === area + "|" + text;   // an older entry: the exact line only
    if (entry.shape !== shape || entry.nums.length !== mine.length) return false;
    return mine.every((value, i) => Math.abs(value - entry.nums[i]) <= Math.max(1, Math.abs(entry.nums[i]) * 0.25));
  });
}
function highlightState() {
  try { return Object.assign({ read: {}, deleted: [] }, JSON.parse(localStorage.getItem(HIGHLIGHTS_KEY)) || {}); }
  catch (e) { return { read: {}, deleted: [] }; }
}
function saveHighlightState(state) {
  try { localStorage.setItem(HIGHLIGHTS_KEY, JSON.stringify(state)); } catch (e) {}
  // The reviewed state belongs to the dashboard account, so desktop and phone share it.
  const url = window.DESK_SOURCE ? "/highlight_state" : "/api/highlight_state";
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
    body: JSON.stringify(state) }).catch(() => {});
}
let highlightStateLoaded = false;
async function loadHighlightState() {
  if (highlightStateLoaded) return;
  highlightStateLoaded = true;
  try {
    const response = await fetch(window.DESK_SOURCE ? "/highlight_state" : "/api/highlight_state", { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) return;
    const shared = await response.json();
    if (shared && typeof shared === "object" && shared.read && Array.isArray(shared.deleted)) {
      const local = highlightState();
      const merged = { read: Object.assign({}, shared.read, local.read), deleted: [...new Set([...(shared.deleted || []), ...(local.deleted || [])])] };
      try { localStorage.setItem(HIGHLIGHTS_KEY, JSON.stringify(merged)); } catch (e) {}
      if (JSON.stringify(merged) !== JSON.stringify(shared)) {
        const url = window.DESK_SOURCE ? "/highlight_state" : "/api/highlight_state";
        fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify(merged) }).catch(() => {});
      }
    }
  } catch (e) { /* retain this browser's saved state while offline */ }
}
function redrawHighlights() {
  if (!latest || !latest.data) return;
  const hl = latest.data.highlights || {}, hover = hl.hover || {};
  for (const kind of ["crypto", "stocks", "desk"]) {
    renderHighlights(`hl-${kind}-well`, highlightsForKind(hl.well, kind), hover);
    renderHighlights(`hl-${kind}-attention`, highlightsForKind(hl.attention, kind), hover);
  }
  renderAlertStrip(hl.attention, hover);
  if (typeof drawSectionsMenu === "function") drawSectionsMenu();
}
function highlightsForKind(rows, kind) {
  const tables = (latest && latest.data && latest.data.tables) || {};
  const symbols = (ids) => new Set(ids.flatMap((id) => ((tables[id] || {}).rows || []).flatMap((row) => {
    const value = String(row.Symbol || row.Coin || "").toUpperCase();
    return value ? [value, value.split(":").pop()] : [];
  })));
  const cryptoSymbols = symbols(["positions", "movers", "wallets"]), stockSymbols = symbols(["positions_stocks", "movers_stocks"]);
  const escaped = (symbol) => symbol.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const contains = (text, set) => [...set].some((symbol) => symbol && new RegExp(`(^|[^A-Z0-9])${escaped(symbol)}($|[^A-Z0-9])`, "i").test(text));
  return (rows || []).flatMap(([area, source]) => {
    const text = String(source), tagged = [...text.matchAll(/\b(crypto|stocks(?:\s+and\s+tradfi)?|tradfi)\b\s*:?/gi)];
    const hasCrypto = /\bcrypto\b|\bBTC\b|\bETH\b/i.test(text) || contains(text, cryptoSymbols);
    const hasStocks = /\bstocks?\b|\btradfi\b|\bSPY\b|\bS&P\s*500\b|\bQQQ\b/i.test(text) || contains(text, stockSymbols);
    if (hasCrypto && hasStocks && tagged.some((match) => /^crypto$/i.test(match[1])) && tagged.some((match) => /^(stocks?|tradfi)/i.test(match[1]))) {
      const prefix = text.slice(0, tagged[0].index).trim();
      const pieces = tagged.map((match, i) => ({
        kind: /^crypto$/i.test(match[1]) ? "crypto" : "stocks",
        text: text.slice(match.index, i + 1 < tagged.length ? tagged[i + 1].index : text.length).trim(),
      })).filter((piece) => piece.kind === kind);
      if (kind === "desk") return /\d/.test(prefix) ? [[area, prefix]] : [];
      return pieces.map((piece) => [area, [prefix, piece.text].filter(Boolean).join(" ")]);
    }
    if (hasCrypto && !hasStocks) return kind === "crypto" ? [[area, text]] : [];
    if (hasStocks && !hasCrypto) return kind === "stocks" ? [[area, text]] : [];
    if (hasCrypto || hasStocks) return kind === "desk" ? [[area, text]] : [];
    return kind === "desk" ? [[area, text]] : [];
  });
}
const VENUES = /\b(xyz|flx|vntl|hyna|km|abcd|cash|para|mkts|io):/gi;
const cleanLine = (text) => String(text).replace(VENUES, "").replace(/\bSP500\b/g, "S&P500")   // the venue is the desk's business
  .replace(/\s*\([^)]*\)/g, "")                                                          // no parentheses: that is explanation
  .replace(/(\d+(?:\.\d+)?%)\s+down\b/g, "-$1").replace(/(\d+(?:\.\d+)?%)\s+up\b/g, "$1")   // "1.9% down" is a red 1.9%
const sentences = (text) => String(text).split(/(?<=[.;?])\s+(?=[A-Z])/).map((part) => part.trim()).filter(Boolean);   // each thought its own line

// Gestures on a line or a row: swipe left = hide it / dislike it, double tap (or double click) = like it. The swipe is for touch
// and pen (a mouse keeps its drag and click behaviour); a line that was swiped slides away before it goes.
function gestures(node, { dislike, like }) {
  node.style.touchAction = "manipulation";   // a double tap is a double tap, not a zoom
  let startX = 0, startY = 0, tracking = false;
  node.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1 || event.target.closest("button, a, input")) return;
    tracking = true; startX = event.touches[0].clientX; startY = event.touches[0].clientY;
  }, { passive: true });
  node.addEventListener("touchmove", (event) => {
    if (!tracking) return;
    const dx = event.touches[0].clientX - startX, dy = event.touches[0].clientY - startY;
    if (Math.abs(dy) > Math.abs(dx)) { tracking = false; node.style.transform = ""; return; }   // a scroll, not a swipe
    if (dx < 0) node.style.transform = `translateX(${Math.max(dx, -140)}px)`;
  }, { passive: true });
  node.addEventListener("touchend", (event) => {
    if (!tracking) return;
    tracking = false;
    const dx = (event.changedTouches[0] || {}).clientX - startX;
    if (dx < -70) { node.style.transition = "transform .15s, opacity .15s"; node.style.transform = "translateX(-100%)"; node.style.opacity = "0"; setTimeout(dislike, 150); }
    else node.style.transform = "";
  });
  node.addEventListener("dblclick", (event) => {
    if (event.target.closest("button, a, input")) return;
    event.preventDefault();
    like();
  });
}

// The Research screen has no highlights card: what needs attention (critical and watch rows) shows in a strip above the tiles there.
function renderAlertStrip(rows, hover = {}) {
  const host = document.getElementById("hl-alert");
  if (!host) return;
  renderHighlights("hl-alert", (rows || []).filter(([area, text]) => severity(area, text) !== ""), hover);
  host.hidden = !host.children.length;
}
function alertResponseButton(area, text, monitor) {
  const equitySelloff = /equity sell[- ]off|stock (?:sell[- ]off|dip)/i.test(`${area} ${text}`);
  const accountAlert = /margin|not reporting|liquidat|data flow down|unreadable accounts/i.test(area);
  const button = el("button", { type: "button", class: "hl-act alert-response", text: "▶",
    title: "Response options", "aria-label": `Response options for ${area}` });
  button.addEventListener("click", () => {
    const title = equitySelloff ? "Equity sell-off" : area;
    const { overlay, box } = actionDialog(title);
    const option = (label, action) => {
      const choice = el("button", { type: "button", class: "asset-action", text: label });
      choice.addEventListener("click", () => {
        feedback("highlight", "response", area, label);
        overlay.remove();
        action();
      });
      return choice;
    };
    const review = (id) => {
      const card = document.getElementById(id)?.closest(".card");
      if (!card || card.hidden) {
        if (id !== "t-exposure") review("t-exposure");
        else notify([], "Exposure data is unavailable");
        return;
      }
      setScreen("now");
      if (card.classList.contains("card-off")) setHidden(id, false);
      if (card.classList.contains("card-min")) setMinimized(id, false);
      card.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    box.replaceChildren(el("h3", { text: title }), el("div", { class: "asset-actions" }, [
      ...(equitySelloff ? [
        option("Review stock exposure", () => review("t-positions_stocks")),
        option("Review long trims", reviewStockTrims),
      ] : [
        option(accountAlert ? "Review accounts" : "Review exposure", () => review(accountAlert ? "t-accounts" : "t-exposure")),
        ...(accountAlert ? [option("Refresh data", refreshAlertData)] : []),
      ]),
      option("Monitor for changes", monitor),
    ]), closeButton(overlay));
    box.querySelector("button")?.focus();
  });
  return button;
}

function reviewStockTrims() {
  const title = "Trim stock and TradFi longs by one third";
  const { overlay, box } = actionDialog(title);
  const rows = (latest?.data?.tables?.positions_stocks?.rows || [])
    .filter((row) => row.Symbol && isNum(row.Value) && row.Value > 0)
    .sort((a, b) => b.Value - a.Value);
  const options = rows.map((row) => {
    const hl = /^HL /.test(String(row.Account || ""));
    const name = marketName(row.Symbol);
    const sentence = `trim the ${row.Symbol} long in ${row.Account} by a third`;
    const plan = window.DESK_SOURCE && hl;
    const trim = el("button", { type: "button", class: "asset-action", text: "⅓",
      title: plan ? "Plan trim" : "Copy trim", "aria-label": `${plan ? "Plan" : "Copy"} trim for ${name} in ${row.Account}` });
    trim.addEventListener("click", () => {
      if (plan) {
        overlay.remove();
        runAction("Trim ⅓", "reduce", 1 / 3, { coin: row.Symbol, wallet: String(row.Account).slice(-4) });
      } else {
        copyOrder(trim, sentence);
      }
    });
    return el("div", { class: "alert-position" }, [
      el("span", { text: `${name} · ${row.Account}` }), trim,
    ]);
  });
  box.replaceChildren(el("h3", { text: title }),
    ...(options.length ? options : [el("p", { class: "muted", text: "No stock or TradFi longs to trim." })]),
    closeButton(overlay));
  box.querySelector("button")?.focus();
}

async function refreshAlertData() {
  if (!window.DESK_SOURCE) {
    try {
      const response = await fetch("/api/refresh", { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch (error) {
      notify([String(error)], "Could not request refresh");
    }
  }
  await load();
}

function renderHighlights(id, rows, hover = {}) {
  const host = document.getElementById(id);
  const state = highlightState();
  // Areas the reader has dismissed over and over (desk/usage_model.py) are folded away, never a critical row.
  const quiet = new Set((((latest && latest.data && latest.data.usage_model) || {}).quiet) || []);
  const open = host.dataset.showQuiet === "1";
  const answered = (rows || []).filter(([area, text]) => !state.deleted.includes(area) && !isRead(state, area, text))
    .flatMap(([area, text]) => sentences(cleanLine(text)).map((part, i) => [area, part, text]));   // [area, line, the whole text it came from]
  const folded = answered.filter(([area, text]) => quiet.has(area) && severity(area, text) !== "critical");
  const shown = open ? answered : answered.filter((row) => !folded.includes(row));
  if (!shown.length && !folded.length) {   // one card holds both lists: say it once, on the first list, when both are empty
    const match = id.match(/^hl-(crypto|stocks|desk)-(attention|well)$/);
    const otherRows = match && latest && latest.data && latest.data.highlights
      ? highlightsForKind(latest.data.highlights[match[2] === "attention" ? "well" : "attention"], match[1]) : [];
    host.replaceChildren(...(match && match[2] === "attention" && !otherRows.length
      ? [el("li", {}, el("span", { class: "empty", text: "Nothing to report" }))] : []));
    return;
  }
  const rank = { critical: 0, watch: 1, "": 2 };
  // A dashboard line, not a sentence: one trailing period goes (several sentences keep theirs)
  const tidy = (text) => (/\.\s+\S/.test(String(text)) ? String(text) : String(text).replace(/\.\s*$/, ""));
  const ordered = shown.map(([area, text, whole]) => [area, tidy(text), severity(area, whole), whole])
    .sort((a, b) => rank[a[2]] - rank[b[2]]);  // stable: same-level rows keep their order
  const foot = folded.length ? [el("li", {}, [el("button", { type: "button", class: "quiet-toggle", title: "Areas you have dismissed several times: " + [...new Set(folded.map((r) => r[0]))].join(", "),
    text: open ? "hide quiet" : `${folded.length} quiet`, onclick: null })])] : [];
  if (foot.length) foot[0].firstChild.addEventListener("click", () => { host.dataset.showQuiet = open ? "0" : "1"; redrawHighlights(); });
  numberContext = { positive: id.endsWith("-well") };
  host.replaceChildren(...ordered.map(([area, text, level, whole]) => {
    const act = (label, symbol, change) => {
      const button = el("button", { type: "button", class: "hl-act", title: label, "aria-label": `${label}: ${area}`, text: symbol });
      button.addEventListener("click", () => { const s = highlightState(); change(s); saveHighlightState(s); redrawHighlights(); });
      return button;
    };
    const markRead = (s) => {
      s.read[area + "|" + whole] = { at: Date.now(), shape: lineShape(whole), nums: lineNumbers(whole) };
      feedback("highlight", "read", area, text);
    };
    const monitor = () => { const s = highlightState(); markRead(s); saveHighlightState(s); redrawHighlights(); };
    // No titles on the lines: the line says what it is; the area name is on hover (and the critical ones carry their band)
    const item = el("li", Object.assign(level ? { class: level } : {}, hover[area] ? { title: hover[area] } : {}), [   // a pop-up only for the headlines around a big move
      el("span", { class: "text" }, [
        ...lineNodes(text, area !== "uPnL" && !/^uPnL\b/.test(text)),
        level ? alertResponseButton(area, text, monitor) : null,
      ]),   // the uPnL lines carry no background
      el("span", { class: "hl-actions" }, [
        ...symbolActions(whole),   // act on the symbols it names: run it (plan, then confirm) or copy the order
        act("Read: hide until it changes", "✓", markRead),
        act("Useful", "👍", () => { feedback("highlight", "useful", area, text); }),   // stays; tells the model this area earns its place
        act("Not important: stop showing this", "👎", (s) => { if (!s.deleted.includes(area)) s.deleted.push(area); feedback("highlight", "not important", area, text); }),
      ]),
    ]);
    gestures(item, {
      dislike: () => { const s = highlightState(); if (!s.deleted.includes(area)) s.deleted.push(area); saveHighlightState(s); feedback("highlight", "not important", area, text); redrawHighlights(); },
      like: () => feedback("highlight", "useful", area, text),
    });
    return item;
  }), ...foot);
}

// ---------------------------------------------------------------- charts
function token(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function withAlpha(hex, alpha) {
  const value = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${alpha})`;
}

const DATE_TICK_FORMATS = [
  { dtickrange: [null, 86400000], value: "%H:%M" },
  { dtickrange: [86400000, "M1"], value: "%b %d" },
  { dtickrange: ["M1", "M12"], value: "%b'%y" },
  { dtickrange: ["M12", null], value: "%Y" },
];
function baseLayout(extra) {
  const ink = token("--ink-2"), muted = token("--muted"), grid = token("--grid"), axis = token("--axis");
  return Object.assign({
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: token("--font") || "system-ui, -apple-system, Segoe UI, sans-serif", size: 12, color: ink },
    margin: { l: 40, r: 32, t: 6, b: 22 },
    hovermode: "x unified",
    hoverlabel: { bgcolor: token("--surface"), bordercolor: axis, font: { color: token("--ink") } },
    // The chart tools: no band behind them, icons in the page's muted ink, the active one in its full ink.
    modebar: { bgcolor: "rgba(0,0,0,0)", color: muted, activecolor: token("--ink"), orientation: "h" },
    legend: { orientation: "h", x: 0, y: 1, yanchor: "bottom", font: { color: ink } },   // above the plot, inside the top margin
    xaxis: { gridcolor: grid, linecolor: axis, tickfont: { color: muted }, showspikes: true,
             spikemode: "across", spikethickness: 1, spikecolor: axis, spikedash: "solid", tickformatstops: DATE_TICK_FORMATS },
    yaxis: { gridcolor: grid, linecolor: axis, zerolinecolor: axis, tickfont: { color: muted } },
  }, extra);
}

// Chart tools on hover (zoom, pan, reset, save), as Plotly's own bar; a click on a legend item hides or shows that series.
// Plotly's own tool bar is off: drag zooms, and the tile's hover tools carry reset and pan (save is the tile's save).
const CONFIG = { displayModeBar: false, displaylogo: false, responsive: true };
function colorHover(trace) {
  const color = trace.line && trace.line.color || trace.marker && trace.marker.color;
  if (typeof color !== "string" && !Array.isArray(color)) return trace;
  trace.hoverlabel = Object.assign({}, trace.hoverlabel, { bgcolor: token("--surface"), bordercolor: color,
    font: Object.assign({}, trace.hoverlabel && trace.hoverlabel.font, { color, weight: 600 }) });
  return trace;
}
function endLabelBox() {
  const surface = token("--surface");
  return { bgcolor: surface, bordercolor: surface, borderpad: 2, opacity: 1 };
}

function endLabels(series, format) {
  return series.filter((s) => s.y.length).map((s) => ({
    xref: "paper", x: 1, y: s.y[s.y.length - 1], xanchor: "right", xshift: -4, showarrow: false,
    text: format(s.y[s.y.length - 1]), font: { color: token("--ink-2"), size: 11 }, ...endLabelBox(),
  }));
}

// End labels closer than a line apart (a stacked total over its top layer, two lines ending near each other) are
// pushed apart vertically, in rendered pixels, keeping their order; each stays as close to its line as it can.
const END_LABEL_GAP_PX = 18;
function spreadEndLabels(id) {
  const gd = document.getElementById(id);
  const axis = gd && gd._fullLayout && gd._fullLayout.yaxis;
  if (!axis || !axis.range || !axis._length) return;
  const [low, high] = axis.range.map(Number);
  const scale = axis._length / (high - low || 1);
  const annotations = (gd.layout.annotations || []).map((a) => Object.assign({}, a));
  const ends = annotations.filter((a) => a.xshift === -4 && a.xanchor === "right" && typeof a.y === "number")
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
  const value = (charts.value || []).map((s, i) => colorHover({
    type: "scatter", mode: "lines", name: s.name, x: s.x, y: s.y, stackgroup: "value",
    line: { width: 2, color: colors[i % colors.length] }, fillcolor: withAlpha(colors[i % colors.length], 0.18),
    hovertemplate: "%{fullData.name}: $%{y:,.0f}<extra></extra>",
  }));
  // Not stacked: it covers only the days wallet values are known (recorded, or rebuilt before
  // recording began), never implying zero outside them.
  const withWallets = charts.value_with_wallets;
  if (withWallets && withWallets.x.length) {
    const note = withWallets.note ? `<br><span style="font-size:11px">${withWallets.note}</span>` : "";
    value.push(colorHover({ type: "scatter", mode: "lines", name: withWallets.name, x: withWallets.x, y: withWallets.y,
      line: { width: 2, dash: "dot", color: token("--ink-2") },
      hovertemplate: `%{fullData.name}: $%{y:,.0f}${note}<extra></extra>` }));
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
    return { xref: "paper", x: 1, y, xanchor: "right", xshift: -4, showarrow: false, text: money(own),
             font: { color: trace.line.color, size: 11 }, ...endLabelBox() };
  });
  valueLayout.annotations = (valueLayout.annotations || []).concat(valueEnds);
  valueLayout.margin = Object.assign({}, valueLayout.margin, { r: 32 });
  Plotly.react("chart-value", value, valueLayout, CONFIG).then(() => { spreadEndLabels("chart-value"); markNumericNotes(); });

  // The desk is the dotted reference line; the crypto and stocks books in their colours; the benchmarks thin.
  // The desk is the solid line (its crypto and stocks parts in their colours, solid); the benchmarks are dashed and dotted.
  const lineFor = (s, i) => /^crypto/i.test(s.name) ? { width: 1.8, color: token("--kind-crypto") }
    : /^stocks/i.test(s.name) ? { width: 1.8, color: token("--kind-stocks") }
    : /^(total|desk)/i.test(s.name) ? { width: 2.6, color: token("--ink") }
    : /^btc/i.test(s.name) ? { width: 1.4, dash: "dot", color: token("--kind-crypto") }
    : { width: 1.4, dash: "dot", color: token("--kind-stocks") };
  const twr = (charts.twr || []).map((s, i) => colorHover({
    type: "scatter", mode: "lines", name: s.name, x: s.x, y: s.y,
    line: Object.assign({ shape: "linear" }, lineFor(s, i)),
    customdata: s.y.map((v) => readablePct(v / 100)),
    hovertemplate: "%{fullData.name}: %{customdata}<extra></extra>",
  }));
  // % since the start; the desk line ends at the "TW return" tile.
  const shortName = (name) => (/^(total|desk)/i.test(name) ? "desk" : String(name));
  const drawn = (charts.twr || []).map((s, i) => [s, twr[i]]).filter(([s]) => s.y.length);
  const labels = endLabels(charts.twr || [], (v) => readablePct(v / 100)).map((label, i) => Object.assign(label, {
    text: `${shortName(drawn[i][0].name)} ${label.text}`, font: { size: 11, color: drawn[i][1].line.color || token("--ink-2") } }));
  const twrLayout = baseLayout({
    showlegend: false,   // the colours and the end labels name the lines
    annotations: labels,
    shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0, line: { color: token("--axis"), width: 1 } }],
  });
  twrLayout.yaxis = Object.assign({}, twrLayout.yaxis, { ticksuffix: "%" });
  Plotly.react("chart-twr", twr, twrLayout, CONFIG).then(() => { spreadEndLabels("chart-twr"); markNumericNotes(); zoomCharts(); drawChartScales(); });
}

// ---------------------------------------------------------------- page
function retryDataButton() {
  const button = el("button", { type: "button", class: "asset-action", text: "Retry refresh" });
  button.addEventListener("click", refreshAlertData);
  return button;
}

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
      retryDataButton(),
    ]));
  } else if (status.last_ok && (now - new Date(status.last_ok)) / 1000 > 3 * (status.interval_s || 60) + 120) {
    banners.push(el("div", { class: "banner warning", role: "alert" }, [
      el("span", { class: "icon warn", "aria-hidden": "true", text: "!" }),
      el("div", { text: `Data is ${ago(status.last_ok, now)}; the refresh may be stuck.` }),
      retryDataButton(),
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
  if (!data) {
    if (doc.status && doc.status.error) document.getElementById("loading").hidden = true;
    return;
  }
  document.getElementById("loading").hidden = true;
  document.getElementById("content").hidden = false;
  renderAccountStatus(data.accounts_status, now);
  if (lastRendered === doc.status.last_ok) return;   // same data: keep tables, sort and hover as they are
  lastRendered = doc.status.last_ok;
  renderTiles(data.kpis || {}, data.theses);
  redrawHighlights();
  const context = { deskValue: (data.kpis || {}).desk_value };
  for (const id of TABLES) renderTable(id, applyView(id, withWallets(id, data.tables || {}), context));
  balanceColumns();
  renderCharts(data.charts);
  renderPublished(data.tiles || []);
  drawChartScales();
  const notes = document.getElementById("notes");  // developer notes: not on the page (the band shows real problems)
  if (notes) notes.replaceChildren(...(data.notes || []).map((note) => el("li", { text: note })));
}

// The web copy (web/live.js) builds the same document in the browser and sets DESK_SOURCE.
async function load() {
  try {
    if (!highlightStateLoaded) loadHighlightState().then(() => {
      if (latest && latest.data) redrawHighlights();
    });
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
      el("span", { class: "icon warn", "aria-hidden": "true", text: "!" }), el("div", { text: message }), retryDataButton()]));
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
// style: Indie (the desk's own), HL (Hyperliquid), Apple or monochrome. Every style has a light and a dark version. The choices are
// remembered in the browser; the page's head script sets them before first paint (and reads ?mode= ?style= ?view=).
const VIEW_CHOICES = [["tables", "Numbers (tables)", "table"], ["charts", "Charts", "chart"]];
const STYLE_CHOICES = [["indie", "Indie", "#d95926"], ["hl", "HL", "#50d2c1"], ["apple", "Apple", "#0a84ff"], ["mono", "Monochrome", "linear-gradient(90deg, #d9d9d9 50%, #555 50%)"]];
function lookTheme(mode, style) {   // the CSS theme for a style and mode
  return style === "indie" ? mode : style === "hl" ? (mode === "dark" ? "hl" : "hl-light")
    : style === "mono" ? "mono-" + mode : "apple-" + mode;
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
// Time scale, as on TradingView: 1D, 1W, 1M or all. Left on auto it follows how long the open trades have been held (a
// book of day trades opens on the day, conviction trades held for weeks on the month); a click fixes it, a click on the
// fixed one returns it to auto. It sets the "now" tile's window and zooms the value charts.
const SCALES = [["1D", 1], ["1W", 7], ["1M", 30], ["All", null]];
// Each element has its own scale: the "now" tile and the value charts. Left alone it follows how long the open trades have
// been held; hovering the element shows its scales, a click fixes one, a click on the fixed one returns it to auto.
let scaleChoices = {};
try { scaleChoices = JSON.parse(localStorage.getItem("desk-scales")) || {}; } catch (e) {}
function autoScale() {
  const days = latest && latest.data && latest.data.horizon_days;
  return !isNum(days) ? "1D" : days < 2 ? "1D" : days < 10 ? "1W" : days < 60 ? "1M" : "All";
}
function currentScale(element = "tile") { const choice = scaleChoices[element] || "auto"; return choice === "auto" ? (element === "tile" ? "1D" : autoScale()) : choice; }
function saveScaleChoices() {
  try { localStorage.setItem("desk-scales", JSON.stringify(scaleChoices)); } catch (e) {}
}
function setScale(element, scale) {
  scaleChoices[element] = (scaleChoices[element] || "auto") === scale ? "auto" : scale;
  saveScaleChoices();
  feedback("look", "scale " + element, scaleChoices[element]);
  if (!latest || !latest.data) return;
  if (element === "tile" || element === "upnl") renderTiles(latest.data.kpis || {}, latest.data.theses);
  else if (element === "candidates") { renderTable("candidates", applyView("candidates", latest.data.tables.candidates, {})); drawChartScales(); }
  else if (element === "performance") { renderTable("performance", applyView("performance", latest.data.tables.performance, { deskValue: (latest.data.kpis || {}).desk_value })); drawChartScales(); }
  else { zoomCharts(); drawChartScales(); }
}
function scaleChips(element) {   // the element's own scale buttons, shown on its hover
  const active = currentScale(element);
  return el("span", { class: "scale-chips" }, SCALES.filter(([name]) => name !== active).map(([name]) => {
    const chip = el("button", { type: "button", class: "scale-chip", text: { "1D": "D", "1W": "W", "1M": "M" }[name] || name });
    chip.addEventListener("click", (event) => { event.stopPropagation(); setScale(element, name); });
    return chip;
  }));
}
function drawChartScales() {
  ["performance", "candidates"].forEach((element) => {
    const host = document.getElementById("t-" + element);
    if (!host) return;
    const old = host.previousElementSibling;
    const controls = old?.classList.contains("table-periods") ? old
      : el("div", { class: "scale-chips table-periods", role: "group", "aria-label": "Table period" });
    if (controls !== old) {
      host.before(controls);
      for (const [name] of SCALES) {
        if (element === "candidates" && name === "All") continue;
        const label = { "1D": "D", "1W": "W", "1M": "M" }[name] || name;
        const button = el("button", { type: "button", class: "scale-chip", "data-window": name, text: label });
        button.addEventListener("click", () => setScale(element, name));
        controls.append(button);
      }
    }
    const scale = currentScale(element);
    const selected = element === "candidates" && scale === "All" ? "1M" : scale;
    for (const button of controls.children) {
      const active = button.dataset.window === selected;
      button.classList.toggle("on", active);
      button.setAttribute("aria-pressed", String(active));
    }
  });
  ["chart-value", "chart-twr"].forEach((id) => {
    const node = document.getElementById(id), title = node && node.closest(".card") && node.closest(".card").querySelector("h2");
    if (!title) return;
    const old = title.querySelector(".scale-chips");
    const chips = scaleChips("charts");
    if (old) old.replaceWith(chips); else { const tools = title.querySelector(".card-tools"); if (tools) tools.before(chips); else title.append(chips); }
  });
}
// The return chart over a window: every curve starts at 0% on the window's first day (what each did over the window, not
// since its own start). All: the curves as they were drawn.
function rebaseReturns(node, days) {
  node._orig = node._orig || node.data.map((trace) => ({ x: trace.x.slice(), y: trace.y.slice() }));
  const start = days ? Date.now() - days * 86400000 : null;
  let baseTime = start;
  const ys = node._orig.map((trace) => {
    if (!start) return trace.y;
    // the base is the last point at or before the window's start (daily points: yesterday's close for a day), so a
    // short window shows the move from there to now, not a flat line
    let first = -1;
    trace.x.forEach((x, i) => { if (new Date(x).getTime() <= start) first = i; });
    if (first < 0) first = trace.x.findIndex((x) => new Date(x).getTime() >= start);
    if (first < 0) return trace.y;
    baseTime = Math.min(baseTime, new Date(trace.x[first]).getTime());
    const base = 1 + trace.y[first] / 100;
    return trace.y.map((y) => Math.round(10000 * ((1 + y / 100) / base - 1)) / 100);
  });
  Plotly.restyle(node, { y: ys });
  const notes = (node.layout.annotations || []).map((note) => Object.assign({}, note));
  const drawn = ys.map((y, i) => [y, i]).filter(([y]) => y.length);
  notes.filter((note) => note.xshift === -4).forEach((note, k) => {
    const [y, i] = drawn[k] || [];
    if (!y) return;
    note.y = y[y.length - 1];
    note.text = note.text.replace(/-?[\d.]+%$/, readablePct(y[y.length - 1] / 100));
  });
  Plotly.relayout(node, { annotations: notes });
  return baseTime;
}

// The return chart over a day or a week at hourly resolution (daily points make a straight line): the HL wallets' PnL over their
// value at the window's start, BTC and the S&P 500 perp, each from the window's first hour. A month or more keeps the daily curves.
function drawIntraday(node, days) {
  const data = (latest && latest.data && latest.data.intraday) || {};
  const start = Date.now() - days * 86400000;
  const lines = [];
  const rebased = (points, pick) => {
    const rows = points.filter(([t]) => t >= start);
    if (rows.length < 3) return null;
    const base = pick(rows[0]);
    return { x: rows.map(([t]) => new Date(t)), y: rows.map((row) => Math.round(10000 * (row.length === 3 ? (row[1] - rows[0][1]) / rows[0][2] : pick(row) / base - 1)) / 100) };
  };
  const desk = rebased(data.desk || [], () => 1);
  if (desk) lines.push(["desk", desk, { width: 2.6, color: token("--ink") }]);
  const btc = rebased(data.BTC || [], (row) => row[1]);
  if (btc) lines.push(["BTC", btc, { width: 1.4, dash: "dot", color: colorFor("BTC") }]);
  const sp = rebased(data["S&P500"] || [], (row) => row[1]);
  if (sp) lines.push(["S&P500", sp, { width: 1.4, dash: "dot", color: colorFor("S&P500") }]);
  if (!lines.length) return false;
  const traces = lines.map(([name, line, style]) => colorHover({ type: "scatter", mode: "lines", name, x: line.x, y: line.y, line: Object.assign({ shape: "linear" }, style),
    customdata: line.y.map((v) => readablePct(v / 100)), hovertemplate: "%{fullData.name}: %{customdata}<extra></extra>" }));
  const layout = Object.assign({}, node.layout, { annotations: lines.map(([name, line, style]) => ({ xref: "paper", x: 1, y: line.y[line.y.length - 1], xanchor: "right", xshift: -4,
    showarrow: false, text: `${name} ${readablePct(line.y[line.y.length - 1] / 100)}`, font: { size: 11, color: style.color }, ...endLabelBox() })), xaxis: Object.assign({}, node.layout.xaxis, { autorange: true, range: null }),
    yaxis: Object.assign({}, node.layout.yaxis, { autorange: true, range: null }) });
  node._orig = null;
  node._intraday = true;
  Plotly.react(node, traces, layout, CONFIG).then(() => { spreadEndLabels(node.id); markNumericNotes(); });
  return true;
}
// The value chart also needs hourly points at D/W; zooming its daily samples only makes a flat line.
function drawIntradayValue(node, days) {
  const accounts = (latest && latest.data && latest.data.intraday && latest.data.intraday.accounts) || [];
  const start = Date.now() - days * 86400000;
  const source = (latest.data.charts && latest.data.charts.value) || [];
  const traces = accounts.map((account, i) => {
    const rows = (account.points || []).filter(([t, value]) => t >= start && isNum(value));
    if (rows.length < 3) return null;
    const match = source.findIndex((series) => series.name === account.name);
    const color = token(`--series-${(match < 0 ? i : match) % 5 + 1}`);
    return colorHover({ type: "scatter", mode: "lines", name: account.name, x: rows.map(([t]) => new Date(t)), y: rows.map(([, value]) => value),
      stackgroup: "value", line: { width: 2, color }, fillcolor: withAlpha(color, 0.18),
      hovertemplate: "%{fullData.name}: $%{y:,.0f}<extra></extra>" });
  }).filter(Boolean);
  if (!traces.length) return false;
  const annotations = [];
  let stack = 0;
  traces.forEach((trace) => {
    const own = trace.y[trace.y.length - 1]; stack += own;
    annotations.push({ xref: "paper", x: 1, y: stack, xanchor: "right", xshift: -4, showarrow: false, text: usd(own),
      font: { color: trace.line.color, size: 11 }, ...endLabelBox() });
  });
  const layout = Object.assign({}, node.layout, { annotations,
    xaxis: Object.assign({}, node.layout.xaxis, { autorange: true, range: null, rangebreaks: [] }),
    yaxis: Object.assign({}, node.layout.yaxis, { autorange: true, range: null }),
    margin: Object.assign({}, node.layout.margin, { r: 32 }) });
  node._orig = null; node._intraday = true;
  Plotly.react(node, traces, layout, CONFIG).then(() => { spreadEndLabels(node.id); markNumericNotes(); });
  return true;
}
const colorFor = (name) => (name === "BTC" ? token("--kind-crypto") : token("--kind-stocks"));

function zoomCharts() {   // the value charts show the chosen window (All: the whole history)
  if (typeof Plotly === "undefined") return;
  const days = (SCALES.find(([name]) => name === currentScale("charts")) || [])[1];
  ["chart-value", "chart-twr"].forEach((id) => {
    const node = document.getElementById(id);
    if (!node || !node.data) return;
    if (id === "chart-twr" && days && days <= 7) { if (drawIntraday(node, days)) return; }          // a day or a week: hourly
    else if (id === "chart-value" && days && days <= 7) { if (drawIntradayValue(node, days)) return; }
    else if ((id === "chart-twr" || id === "chart-value") && node._intraday) { node._intraday = false; if (latest && latest.data) renderCharts(latest.data.charts); return; }   // back to the daily curves
    const rebasedFrom = id === "chart-twr" ? rebaseReturns(node, days) : null;
    if (!days) { Plotly.relayout(node, { "xaxis.autorange": true, "yaxis.autorange": true }).then(() => spreadEndLabels(id)); return; }
    const end = Date.now(), start = Math.min(end - days * 86400000, rebasedFrom || Infinity);
    const ys = node.data.flatMap((trace) => (trace.x || []).map((x, i) => [new Date(x).getTime(), trace.y && trace.y[i]]))
      .filter(([t, y]) => t >= start && isNum(y)).map(([, y]) => y);
    const update = { "xaxis.range": [new Date(start).toISOString(), new Date(end).toISOString()] };
    if (ys.length) { const low = Math.min(...ys), high = Math.max(...ys), pad = (high - low) * 0.08 || Math.abs(high) * 0.01 || 1; update["yaxis.range"] = [low - pad, high + pad]; }
    Plotly.relayout(node, update).then(() => spreadEndLabels(id));   // the axis changed: end labels that now touch are spread again
  });
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
// Trade bar: asset, side, an optional strategy tag, then ▶ plans the entry and asks ✓ / ✗ (runAction). On the local page
// the order is copied for Claude, as everywhere else the dashboard cannot trade.
let tradeBarNode = null, tradeSide = "long";
function tradeAssets() {
  const tables = (latest && latest.data && latest.data.tables) || {};
  const names = new Set();
  for (const id of ["positions", "positions_stocks", "candidates", "mm_candidates"])
    for (const row of ((tables[id] || {}).rows || [])) { const n = row.Symbol || row.Coin; if (n) names.add(String(n)); }
  return [...names].sort();
}
function tradeBar() {
  if (tradeBarNode) { tradeBarNode.querySelector("datalist").replaceChildren(...tradeAssets().map((n) => el("option", { value: n }))); return tradeBarNode; }
  const asset = el("input", { class: "trade-input", type: "text", placeholder: "asset", autocomplete: "off",
    spellcheck: "false", "aria-label": "Asset" });
  const suggestions = el("div", { class: "trade-suggestions", role: "listbox", "aria-label": "Tradable assets", hidden: true });
  const refreshSuggestions = () => {
    const query = asset.value.trim().toLowerCase();
    const assets = new Map();
    tradeAssets().forEach((name) => assets.set(name.toLowerCase(), universeMap[name.toLowerCase()] || name));
    Object.keys(universeMap).filter((name) => name === friendlyName(universeMap[name]).toLowerCase())
      .forEach((name) => assets.set(name, universeMap[name]));
    const names = [...assets.keys()]
      .filter((name) => !query || name.startsWith(query) || name.includes(query)).sort();
    suggestions.replaceChildren(...(names.length ? names.map((name) => {
      const market = assets.get(name);
      const button = el("button", { type: "button", role: "option", text: friendlyName(market) });
      button.addEventListener("mousedown", (event) => event.preventDefault());
      button.addEventListener("click", () => { asset.value = friendlyName(market); suggestions.hidden = true; asset.focus(); });
      return button;
    }) : [el("div", { class: "trade-suggestion-empty", text: query ? "No matching assets" : "Loading markets…" })]));
    suggestions.hidden = false;
  };
  asset.addEventListener("focus", () => { refreshSuggestions(); loadUniverse().then(refreshSuggestions); });
  asset.addEventListener("input", refreshSuggestions);
  asset.addEventListener("keydown", (event) => { if (event.key === "Escape") suggestions.hidden = true; });
  asset.addEventListener("blur", () => setTimeout(() => { suggestions.hidden = true; }, 120));
  const sides = [["long", "▲", "Long"], ["short", "▼", "Short"]].map(([value, glyph, name]) => {
    const b = el("button", { type: "button", class: "trade-side " + value, text: glyph, title: name, "aria-label": name, "aria-pressed": String(value === tradeSide) });
    b.addEventListener("click", () => { tradeSide = value; sides.forEach((x) => x.setAttribute("aria-pressed", String(x === b))); });
    return b;
  });
  const strat = el("select", { class: "trade-input", "aria-label": "Strategy", title: "Strategy tag (optional)" },
    [["", "strat"], ["directional", "directional"], ["conviction", "conviction"], ["hedge", "hedge"], ["fade", "fade"]]
      .map(([value, text]) => el("option", { value, text })));
  const go = el("button", { type: "button", class: "trade-go", text: "▶", title: "Plan the entry", "aria-label": "Plan the entry" });
  const run = () => {
    const raw = asset.value.trim();
    if (!raw) { asset.focus(); return; }
    const coin = universeMap[raw.toLowerCase()] || (/^[a-z0-9]+:/i.test(raw) ? raw.toLowerCase().replace(/^([a-z0-9]+):(.*)$/, (m, d, c) => d + ":" + c.toUpperCase()) : raw.toUpperCase());
    feedback("trade", "plan", coin, tradeSide + (strat.value ? " " + strat.value : ""));
    if (window.DESK_SOURCE) runAction(tradeSide === "short" ? "Short" : "Long", tradeSide, 1, { coin, wallet: "07f2", label: strat.value || undefined });
    else if (navigator.clipboard) navigator.clipboard.writeText(`${tradeSide} ${coin}${strat.value ? " as " + strat.value : ""}`);
  };
  go.addEventListener("click", run);
  asset.addEventListener("keydown", (event) => { if (event.key === "Enter") run(); });
  tradeBarNode = el("div", { class: "trade-bar", role: "group", "aria-label": "Trade" },
    [el("div", { class: "trade-asset-wrap" }, [asset, suggestions]), ...sides, strat, go, el("datalist", { id: "trade-assets" }, tradeAssets().map((n) => el("option", { value: n })))]);
  return tradeBarNode;
}
function drawModes() {
  const host = document.getElementById("modes");
  if (!host) return;
  const root = document.documentElement.dataset;
  const mode = root.mode === "light" ? "light" : "dark", style = root.style || "indie";
  const views = el("div", { class: "switch", role: "group", "aria-label": "Numbers or charts" });
  VIEW_CHOICES.filter(([value]) => value !== (root.view === "charts" ? "charts" : "tables")).forEach(([value, name, shape]) => {   // only the other view
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
  STYLE_CHOICES.filter(([value]) => value !== style).forEach(([value, name, dot]) => {   // only the other styles
    const button = el("button", { type: "button", class: "style-button", title: name, "aria-pressed": value === style ? "true" : "false",
      "aria-label": name + " style", style: `--dot:${dot}` }, [el("i")]);
    button.addEventListener("click", () => setLook("style", value));
    styles.append(button);
  });
  const units = el("div", { class: "switch", role: "group", "aria-label": "uPnL in percent or dollars" });
  [["pct", "%"], ["usd", "$"]].filter(([value]) => value !== pnlUnit).forEach(([value, name]) => {   // only the other unit
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
  host.replaceChildren(tradeBar(), views, units, toggle, styles, strip, report);
  host.querySelectorAll(".switch").forEach((group) => group.classList.add("choices"));
}
// A look change redraws only what it changes, from the data already loaded (render() skips unchanged data): the view
// switch redraws the tables; mode and style are CSS for the tables, so only the Plotly charts (which read the colours and
// font when drawn) are redrawn, and the table charts when the charts view is on.
function redrawLook(kind) {
  if (kind !== "view") {
    document.querySelectorAll(".asset-chart").forEach((chart) => chart._candleSeries?.applyOptions(candleColors()));
  }
  if (!latest || !latest.data) return;
  const data = latest.data, context = { deskValue: (data.kpis || {}).desk_value };
  if (kind === "view" || isChartView()) {
    for (const id of TABLES) renderTable(id, applyView(id, withWallets(id, data.tables || {}), context));
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
const LAYOUT_VERSION = 7;   // 3: now first, history and the account view last; an arrangement saved on an older page is dropped
function layoutState() {
  let state = {};
  try { state = JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {}; } catch (e) { return {}; }
  if (state.version !== LAYOUT_VERSION) {   // keep hidden and minimized choices, not slot positions of the old page
    delete state.order;
    if ((state.version || 0) < 6 && state.hidden) state.hidden = state.hidden.filter((id) => id !== "chart-value");   // 6: it lives on the Research screen
    if ((state.version || 0) < 7) {   // 7: the stocks positions tile is back, level with the crypto one in the next column; market making is research
      if (state.hidden) state.hidden = state.hidden.filter((id) => id !== "t-positions_stocks");
      Object.values(state.cols || {}).forEach((columns) => {
        const from = columns.findIndex((ids) => ids.includes("t-positions"));
        if (from < 0) return;
        columns.forEach((ids) => { const k = ids.indexOf("t-positions_stocks"); if (k >= 0) ids.splice(k, 1); });
        const into = columns.length > 1 ? (from < columns.length - 1 ? from + 1 : from - 1) : from;
        const at = Math.min(columns[into].length, columns[from].indexOf("t-positions"));
        columns[into].splice(at, 0, "t-positions_stocks");
      });
    }
    if ((state.version || 0) < 5) { delete state.cols; delete state.widths; delete state.history; }   // 5: news left, numbers in the middle, charts right
    state.version = LAYOUT_VERSION;
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(state)); } catch (e) {}
  }
  return state;
}
function saveLayout(state) {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(state)); } catch (e) {}
}
const cardId = (card) => { const inner = card.querySelector("[id]"); return inner ? inner.id : ""; };
const cardTitle = (card) => {   // the heading without its icon, grip and buttons (the untitled highlights card is "Highlights")
  const h = card.querySelector("h2");
  if (!h) return "";
  const copy = h.cloneNode(true);
  copy.querySelectorAll(".card-grip, .card-tools, .icon").forEach((node) => node.remove());
  return copy.textContent.trim();
};

// Thumbs up and down on every tile: what is useful and what is not, logged for the usage model (kept per tile, in the browser too).
function thumbs(name, kind) {
  const state = (() => { try { return JSON.parse(localStorage.getItem("desk-thumbs")) || {}; } catch (e) { return {}; } })();
  const make = (symbol, verdict) => {
    const button = el("button", { type: "button", class: "card-tool thumb" + (state[name] === verdict ? " on" : ""), "aria-label": `${verdict}: ${name}`, title: verdict, text: symbol });
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const now = (() => { try { return JSON.parse(localStorage.getItem("desk-thumbs")) || {}; } catch (e) { return {}; } })();
      now[name] = now[name] === verdict ? undefined : verdict;
      try { localStorage.setItem("desk-thumbs", JSON.stringify(now)); } catch (e) {}
      feedback(kind, verdict, name);
      button.parentElement.querySelectorAll(".thumb").forEach((b) => b.classList.remove("on"));
      if (now[name]) button.classList.add("on");
    });
    return button;
  };
  return [make("👍", "useful"), make("👎", "not useful")];
}
let draggingCard = null, grabOffset = 0;

// Resize: hovering the bottom edge of a tile shows a grip; dragging it sets the tile's height (kept per tile, a double click
// returns it to its natural height). Charts inside follow; a table scrolls inside.
const CHART_MIN_HEIGHT = 380;   // a line chart tile is never shorter than this, whatever size it was left at
const isChartCard = (card) => Boolean(card.querySelector("#chart-value, #chart-twr"));
// A line chart is as tall as the tile above it in its column when that tile has a size of its own (never under the minimum), unless
// the chart's own height was set.
function matchChartHeights() {
  document.querySelectorAll("#columns > .col").forEach((col) => {
    [...col.children].forEach((slot, i, slots) => {
      const card = slot.firstElementChild;
      if (!card || !isChartCard(card) || (layoutState().heights || {})[cardId(card)]) return;
      const above = i > 0 ? slots[i - 1].firstElementChild : null;
      const size = above && above.dataset.sized ? parseFloat(above.style.getPropertyValue("--card-h")) : 0;
      applyCardHeight(card, size ? Math.max(size, CHART_MIN_HEIGHT) : 0);
    });
  });
}
function applyCardHeight(card, height) {
  if (height && isChartCard(card)) height = Math.max(height, CHART_MIN_HEIGHT);
  if (height) { card.dataset.sized = "1"; card.style.setProperty("--card-h", height + "px"); }
  else { delete card.dataset.sized; card.style.removeProperty("--card-h"); }
  if (typeof Plotly !== "undefined") card.querySelectorAll(".js-plotly-plot").forEach((node) => { try { Plotly.Plots.resize(node); } catch (e) {} });
}
function addResizeHandle(card) {
  const handle = el("div", { class: "card-resize", title: "Drag to resize" });
  handle.addEventListener("mousedown", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const startY = event.clientY, startHeight = card.getBoundingClientRect().height;
    let frame = null;
    const move = (e) => {
      const height = Math.max(70, Math.round(startHeight + e.clientY - startY));
      card.dataset.sized = "1";
      card.style.setProperty("--card-h", height + "px");
      if (!frame) frame = requestAnimationFrame(() => { frame = null; card.querySelectorAll(".js-plotly-plot").forEach((node) => { try { Plotly.Plots.resize(node); } catch (x) {} }); });
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      const height = Math.round(card.getBoundingClientRect().height), state = layoutState();
      state.heights = Object.assign({}, state.heights, { [cardId(card)]: height });
      saveLayout(state);
      applyCardHeight(card, height);
      feedback("section", "resize", cardTitle(card), String(height));
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  handle.addEventListener("dblclick", () => {
    const state = layoutState();
    if (state.heights) delete state.heights[cardId(card)];
    saveLayout(state);
    applyCardHeight(card, 0);
  });
  card.append(handle);
  const saved = (layoutState().heights || {})[cardId(card)];
  if (saved) applyCardHeight(card, saved);
}


// Column widths: the right edge of a tile is a grip too; dragging it moves the boundary between its column and the next one
// (the previous one for the last column). Widths are kept per number of columns; a double click levels them.
function applyColumnWidths(host) {
  const count = host.children.length, widths = (layoutState().widths || {})[count];
  [...host.children].forEach((col, i) => { col.style.flex = `${widths && widths[i] ? widths[i] : 1} 1 0`; });
}
function addColumnHandle(card) {
  const handle = el("div", { class: "card-resize-x", title: "Drag to resize the column" });
  handle.addEventListener("mousedown", (event) => {
    const col = card.closest(".col"), host = col && col.parentElement;
    if (!host) return;
    event.preventDefault();
    event.stopPropagation();
    const cols = [...host.children], i = cols.indexOf(col), j = i < cols.length - 1 ? i + 1 : i - 1;
    if (j < 0) return;
    const startX = event.clientX, sizes = cols.map((c) => c.getBoundingClientRect().width), pair = sizes[i] + sizes[j];
    const MIN = 220;
    const move = (e) => {
      const delta = (e.clientX - startX) * (j > i ? 1 : -1);   // a last column's right edge works against the one before it
      const mine = Math.min(Math.max(sizes[i] + (j > i ? delta : -delta), MIN), pair - MIN);
      const next = sizes.slice();
      next[i] = mine; next[j] = pair - mine;
      cols.forEach((c, k) => { c.style.flex = `${next[k]} 1 0`; });
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      const state = layoutState(), grows = cols.map((c) => Math.round(c.getBoundingClientRect().width));
      state.widths = Object.assign({}, state.widths, { [cols.length]: grows });
      saveLayout(state);
      feedback("section", "resize column", cardTitle(card), grows.join(","));
      if (latest && latest.data) renderCharts(latest.data.charts);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  handle.addEventListener("dblclick", () => {
    const col = card.closest(".col"), host = col && col.parentElement, state = layoutState();
    if (!host) return;
    if (state.widths) delete state.widths[host.children.length];
    saveLayout(state);
    applyColumnWidths(host);
    if (latest && latest.data) renderCharts(latest.data.charts);
  });
  card.append(handle);
}

const CARDS = {};   // card id -> its element
// A card becomes a window: it sits in a slot, drags by its body, and has minimize, save, full screen and hide.
// The whole tile drags, except from a chart, a field, a button or a link (those keep their own mouse behaviour).
function wireDrag(card) {
  card.addEventListener("mousedown", (event) => {
    card.draggable = !event.target.closest(".js-plotly-plot, input, textarea, select, button, a, .card-full, .card-resize, .card-resize-x");
  });
  card.addEventListener("dragstart", (event) => {
    if (!card.draggable || card.classList.contains("card-full")) { event.preventDefault(); return; }
    draggingCard = card;
    grabOffset = event.clientY - card.getBoundingClientRect().top;
    event.dataTransfer.setData("text/plain", cardId(card));
    event.dataTransfer.effectAllowed = "move";
    try { event.dataTransfer.setDragImage(card, 24, 14); } catch (e) {}
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => {
    draggingCard = null;
    card.draggable = false;
    card.classList.remove("dragging");
    document.querySelectorAll("#content .drop-target").forEach((node) => node.classList.remove("drop-target"));
  });
}
function adoptCard(card) {
    if (!card.parentElement || !card.parentElement.classList.contains("slot")) {
      const slot = el("div", { class: "slot" });
      card.replaceWith(slot);
      slot.append(card);
    }
    CARDS[cardId(card)] = card;
    const title = card.querySelector("h2");
    if (card.dataset.adopted) return;
    card.dataset.adopted = "1";
    card.dataset.screens = screensFor(card);
    if (!title) { wireDrag(card); addResizeHandle(card); addColumnHandle(card); return; }   // an untitled tile (the highlights) moves and resizes too
    if (card.querySelector("#t-market_making, #t-mm_candidates")) {   // a two-sided ladder: bids below, asks above, the price between
      const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
      [["viewBox", "0 0 16 16"], ["width", "16"], ["height", "16"], ["fill", "none"], ["stroke", "currentColor"], ["stroke-width", "1.5"], ["stroke-linecap", "round"], ["class", "card-icon"]].forEach(([k, v]) => svg.setAttribute(k, v));
      ["M3 3.5h10", "M4.5 6h7", "M3 8h10", "M4.5 10h7", "M3 12.5h10"].forEach((d) => { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); svg.append(p); });
      title.prepend(svg);
    }
    // The name shows on hover: most cards say what they are by what they hold (and the dots by their colour).
    [...title.childNodes].forEach((node) => {
      if (node.nodeType === 3 && node.textContent.trim()) node.replaceWith(el("span", { class: "card-name", text: node.textContent }));
    });
    const grip = el("span", { class: "card-grip", title: "Drag anywhere on the tile (outside the chart) to move it", "aria-label": "Move " + cardTitle(card), text: "⠿" });
    wireDrag(card);
    // Window buttons: minimize to the title bar, full screen, close (hide; the Sections menu brings it back).
    const tool = (cls, label, text, action) => {
      const button = el("button", { type: "button", class: "card-tool " + cls, title: label, "aria-label": label + ": " + cardTitle(card), text });
      button.addEventListener("click", action);
      return button;
    };
    const table = card.querySelector(".table-wrap[id^='t-']");
    const filter = table ? tool("card-filter", "Filter rows", "⌕", () => {
      const id = table.id.slice(2);
      let box = title.querySelector(".card-filter-box");
      if (box) { box.remove(); filterState[id] = ""; if (lastSpecs[id]) renderTable(id, lastSpecs[id]); return; }
      box = el("input", { type: "search", class: "card-filter-box", placeholder: "filter", "aria-label": "Filter rows" });
      box.addEventListener("input", () => { filterState[id] = box.value; if (lastSpecs[id]) renderTable(id, lastSpecs[id]); });
      box.addEventListener("keydown", (event) => { if (event.key === "Escape") { box.value = ""; filterState[id] = ""; box.remove(); if (lastSpecs[id]) renderTable(id, lastSpecs[id]); } });
      title.querySelector(".card-tools").before(box);
      box.focus();
      feedback("section", "filter", cardTitle(card));
    }) : null;
    const chartTools = () => {   // for a tile with a chart: reset the view, switch dragging between zoom and pan
      const charts = () => [...card.querySelectorAll(".js-plotly-plot")];
      if (!card.querySelector(".chart")) return [];
      if (card.querySelector("#chart-twr")) {   // the return chart is actionable: more or less of each asset class
        const kindTool = (kind, change, symbol, label) => { const b = tool("card-kind kind-btn-" + kind, label, symbol, () => runClassAction(kind, change, 1 / 3)); return b; };
        const pan = tool("card-pan", "Pan (drag moves) / zoom (drag selects)", "✥", () => {
          const panning = !pan.classList.contains("on");
          pan.classList.toggle("on", panning);
          charts().forEach((node) => Plotly.relayout(node, { dragmode: panning ? "pan" : "zoom" }));
        });
        return [kindTool("crypto", "reduce", "▾", "Reduce crypto by a third"), kindTool("crypto", "add", "▴", "Add to crypto by a third"),
          kindTool("stocks", "reduce", "▾", "Reduce stocks by a third"), kindTool("stocks", "add", "▴", "Add to stocks by a third"),
          tool("card-reset", "Reset view", "⟲", () => charts().forEach((node) => Plotly.relayout(node, { "xaxis.autorange": true, "yaxis.autorange": true }))), pan];
      }
      const reset = tool("card-reset", "Reset view", "⟲", () => charts().forEach((node) =>
        Plotly.relayout(node, { "xaxis.autorange": true, "yaxis.autorange": true })));
      const pan = tool("card-pan", "Pan (drag moves) / zoom (drag selects)", "✥", () => {
        const panning = !pan.classList.contains("on");
        pan.classList.toggle("on", panning);
        charts().forEach((node) => Plotly.relayout(node, { dragmode: panning ? "pan" : "zoom" }));
      });
      return [reset, pan];
    };
    const more = tool("card-more", "Add a layer: more on this", "+", () => openMoreMenu(card, more));
    addResizeHandle(card);
    addColumnHandle(card);
    card.append(el("div", { class: "card-foot" }, thumbs(cardTitle(card), "section")));   // bottom right, on hover: clear of the heading
    const tools = el("span", { class: "card-tools" }, [
      more,
      ...chartTools(),
      ...(filter ? [filter] : []),
      tool("card-minimize", "Minimize", "–", () => setMinimized(cardId(card), !card.classList.contains("card-min"))),
      tool("card-save", "Save as image", "⤓", () => saveTile(card)),
      tool("card-fullscreen", "Full screen", "⤢", () => setFull(cardId(card), !card.classList.contains("card-full"))),
      tool("card-hide", "Hide", "×", () => setHidden(cardId(card), true)),
    ]);
    title.prepend(grip);
    title.append(tools);

}

// Dropping puts the tile where the line shows: the pointer picks the nearest tile and the side of it (above or below it in
// a column), a line marks the spot, and the tile goes there: between two others, or at either end. Not only onto a tile.
function nearestCard(x, y, skip) {
  let best = null, bestDistance = Infinity;
  document.querySelectorAll("#content .card").forEach((card) => {
    if (card === skip || card.classList.contains("card-off") || card.closest(".card-full")) return;
    const box = card.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const dx = Math.max(box.left - x, 0, x - box.right), dy = Math.max(box.top - y, 0, y - box.bottom);
    const distance = Math.hypot(dx, dy);
    if (distance < bestDistance) { bestDistance = distance; best = { card, box }; }
  });
  return best;
}
// Explicit columns: as many as the screen holds (420 px each). A tile belongs to a column and stays there; dropping a tile in
// another column puts it at the spot between that column's tiles and the others do not move. Arrangements are kept per
// number of columns.
const COLUMN_WIDTH = 420;
function columnCount(content) { return Math.max(1, Math.min(4, Math.floor((content.clientWidth || window.innerWidth) / COLUMN_WIDTH))); }
const columnSlots = (content) => [...content.querySelectorAll(".slot")].filter((slot) => !slot.closest(".feed"));
function currentColumns(content) { return [...content.querySelectorAll("#columns > .col")].map((col) => [...col.children].map((slot) => cardId(slot.firstElementChild))); }
function buildColumns(content, saved) {
  const count = columnCount(content), state = layoutState();
  let host = document.getElementById("columns");
  const slots = columnSlots(content), byCard = new Map(slots.map((slot) => [cardId(slot.firstElementChild), slot]));
  const flat = saved ? saved.flat() : (state.order || slots.map((slot) => cardId(slot.firstElementChild)));
  const known = (state.cols || {})[count];
  if (!host) {
    host = el("div", { id: "columns" });
    const anchor = (content.querySelector(".highlights-card") || {}).parentElement || content.querySelector(".tiles");
    anchor.after(host);
  }
  host.replaceChildren(...Array.from({ length: count }, () => el("div", { class: "col" })));
  const cols = [...host.children], placed = new Set();
  const put = (id, index) => { const slot = byCard.get(id); if (!slot || placed.has(id)) return; placed.add(id); cols[index].append(slot); };
  if (known && known.length === count) known.forEach((ids, index) => ids.forEach((id) => put(id, index)));
  else if (count === 1) flat.forEach((id) => put(id, 0));
  else {   // no arrangement for this width yet: the news (highlights) on the left, the numbers in the middle, the charts on the right
    const middle = Array.from({ length: Math.max(1, count - 2) }, (_, k) => k + 1).filter((k) => k < count);
    const heights = cols.map(() => 0);
    flat.forEach((id) => {
      const slot = byCard.get(id);
      if (!slot) return;
      const card = slot.firstElementChild, isNews = Boolean(card.classList.contains("highlights-card")), isChart = /^chart-/.test(id);
      const pool = isNews ? [0] : isChart ? [count - 1] : count === 2 ? [0, 1] : middle;
      const index = pool.reduce((best, k) => (heights[k] < heights[best] ? k : best), pool[0]);
      heights[index] += isNews ? 3 : isChart ? 2 : 1;
      put(id, index);
    });
  }
  slots.forEach((slot) => {   // new tiles go to the shortest column
    const id = cardId(slot.firstElementChild);
    if (placed.has(id)) return;
    placed.add(id);
    cols.reduce((best, c) => (c.scrollHeight < best.scrollHeight ? c : best)).append(slot);
  });
  host.dataset.count = String(count);
  applyColumnWidths(host);
  const gaps = layoutState().gaps || {};
  slots.forEach((slot) => { const card = slot.firstElementChild, gap = gaps[cardId(card)]; card.style.marginTop = gap ? gap + "px" : ""; });
  matchChartHeights();
}
function wireCardDrop(content) {
  const clear = () => content.querySelectorAll(".drop-before, .drop-after, .drop-end").forEach((n) => n.classList.remove("drop-before", "drop-after", "drop-end"));
  // Where the pointer would drop: a column and the tile it goes before (null: the end of that column).
  const target = (x, y) => {
    const cols = [...content.querySelectorAll("#columns > .col")];
    if (!cols.length) return null;
    const col = cols.reduce((best, c) => { const r = c.getBoundingClientRect(); const d = Math.max(r.left - x, 0, x - r.right); return d < best.d ? { c, d } : best; }, { c: cols[0], d: Infinity }).c;
    const cards = [...col.children].map((slot) => slot.firstElementChild).filter((card) => card && card !== draggingCard && !card.classList.contains("card-off"));
    const before = cards.find((card) => { const r = card.getBoundingClientRect(); return y < r.top + r.height / 2; }) || null;
    return { col, before };
  };
  content.addEventListener("dragover", (event) => {
    if (!draggingCard) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    clear();
    if (draggingCard.dataset.published || draggingCard.dataset.asset) {   // published tiles: beside the nearest one
      const hit = nearestCard(event.clientX, event.clientY, draggingCard);
      if (hit) hit.card.classList.add(event.clientY < hit.box.top + hit.box.height / 2 ? "drop-before" : "drop-after");
      return;
    }
    const spot = target(event.clientX, event.clientY);
    if (!spot) return;
    if (spot.before) spot.before.classList.add("drop-before"); else spot.col.classList.add("drop-end");
  });
  content.addEventListener("dragleave", (event) => { if (!content.contains(event.relatedTarget)) clear(); });
  content.addEventListener("drop", (event) => {
    if (!draggingCard) return;
    event.preventDefault();
    clear();
    const moving = draggingCard;
    if (moving.dataset.published || moving.dataset.asset) {
      const hit = nearestCard(event.clientX, event.clientY, moving);
      if (!hit || !(hit.card.dataset.published || hit.card.dataset.asset)) return;
      if (event.clientY < hit.box.top + hit.box.height / 2) hit.card.parentElement.before(moving.parentElement); else hit.card.parentElement.after(moving.parentElement);
    } else {
      const spot = target(event.clientX, event.clientY);
      if (!spot) return;
      // Free vertical placement: the tile's top is where it was dropped; the space above it, from the tile before (or the
      // column's top), is kept as a gap (a small one snaps shut).
      const cards = [...spot.col.children].map((slot) => slot.firstElementChild).filter((card) => card && card !== moving && !card.classList.contains("card-off"));
      const index = spot.before ? cards.indexOf(spot.before) : cards.length, previous = cards[index - 1] || null;
      const top = event.clientY - grabOffset;
      let edge = previous ? previous.getBoundingClientRect().bottom : spot.col.getBoundingClientRect().top;
      const oldCol = moving.closest(".col");
      if (oldCol === spot.col && moving.getBoundingClientRect().top < edge) edge -= moving.getBoundingClientRect().height + 4 + (parseFloat(moving.style.marginTop) || 0);   // it leaves its place above
      let gap = Math.round(top - edge - 4);
      if (gap < 12) gap = 0;
      moving.style.marginTop = gap ? gap + "px" : "";
      const state = layoutState();
      state.gaps = Object.assign({}, state.gaps, { [cardId(moving)]: gap });
      saveLayout(state);
      if (spot.before) spot.before.parentElement.before(moving.parentElement); else spot.col.append(moving.parentElement);
    }
    saveOrder();
    feedback("section", "move", cardTitle(moving), "column drop");
    if (latest && latest.data) renderCharts(latest.data.charts);
  });
}
function setupSections() {
  const content = document.getElementById("content");
  if (!content) return;
  [...content.querySelectorAll(".card")].forEach(adoptCard);
  wireCardDrop(content);
  const byId = CARDS;
  const state = layoutState();
  buildColumns(content, null);
  let width = columnCount(content), timer = null;
  window.addEventListener("resize", () => {   // another number of columns: the arrangement for it (or the order dealt out again)
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (columnCount(content) === width) return;
      const now = currentColumns(content);
      saveOrder();
      width = columnCount(content);
      buildColumns(content, now);
      if (latest && latest.data) renderCharts(latest.data.charts);
    }, 200);
  });
  if (!state.defaultsOff) {   // cards hidden until asked for (the account views: backend detail), once per browser
    const ids = [...content.querySelectorAll(".card[data-default-off]")].map(cardId);
    state.hidden = [...new Set([...(state.hidden || []), ...ids])];
    state.defaultsOff = true;
    saveLayout(state);
  }
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
// Published (notebook) tiles follow the desk's rules. Titles: a repeated last segment is dropped ("Crypto: Performance: Performance" ->
// "Crypto: Performance"); a table that shares its title with its chart says what it adds. Tables: percent and money columns
// read as numbers (coloured by sign), the label columns first, then percentages, then money, then the rest. Line charts of
// returns: no legend (the line ends say who is who), benchmarks dotted, and D W M Y ALL chips on top of the plot.
function publishedTitles(tiles) {
  const clean = (title) => {
    const parts = String(title).split(": ");
    while (parts.length > 1 && parts[parts.length - 1].toLowerCase() === parts[parts.length - 2].toLowerCase()) parts.pop();
    return parts.join(": ");
  };
  const names = new Map(tiles.map((tile) => [tile.key,
    /^(?:hl[-_: ]+)?top[-_ ]+(?:pnl[-_ ]+contributors|w\/l)$/i.test(clean(tile.title)) ||
      /(?:^|[-:])top-pnl-contributors$/.test(tile.key) ? "Top W/L" : clean(tile.title)]));
  const charts = new Set(tiles.filter((tile) => tile.kind === "chart").map((tile) => names.get(tile.key)));
  tiles.forEach((tile) => {
    if (tile.kind === "table" && charts.has(names.get(tile.key))) {
      const first = ((tile.columns || [])[0] || {}).key;
      names.set(tile.key, `${names.get(tile.key)}: ${first === "Window" ? "by window" : "detail"}`);
    }
  });
  return names;
}
const CELL = /^\s*(-?)\s*(\$?)\s*(-?)([\d,]*\.?\d+)\s*([KMB]?)\s*(%?)\s*$/;
function parseCell(text) {   // "-1.8%", "$1.21K", "-$244" -> { value, kind }; "" -> {}; anything else null
  const t = String(text ?? "").trim();
  if (!t) return {};
  const m = CELL.exec(t);
  if (!m) return null;
  const sign = m[1] === "-" || m[3] === "-" ? -1 : 1, scale = { "": 1, K: 1e3, M: 1e6, B: 1e9 }[m[5]];
  const number = parseFloat(m[4].replace(/,/g, "")) * scale * sign;
  if (m[6]) return { value: number / 100, kind: "pct" };
  return m[2] ? { value: number, kind: "usd" } : { value: number, kind: "plain" };
}
const TABLE_WINDOWS = ["D", "W", "M", "Q", "Y", "Active days", "Since start", "All"];
const WINDOW_LETTERS = { "1D": "D", "7D": "W", "30D": "M", "90D": "Q", "1Y": "Y", "365D": "Y" };   // the desk's period letters
function publishedTable(tile) {
  const columns = tile.columns || [];
  const rows = (tile.rows || []).map((row) => (row.Window in WINDOW_LETTERS ? Object.assign({}, row, { Window: WINDOW_LETTERS[row.Window] }) : row));
  const kinds = columns.map((column) => {
    const seen = new Set();
    for (const row of rows) { const cell = parseCell(row[column.key]); if (cell === null) return "text"; if (cell.kind) seen.add(cell.kind); }
    return seen.size === 1 ? [...seen][0] : "text";
  });
  const fmt = { pct: "vpct", usd: "vusd", plain: "text", text: "text" };
  const out = columns.map((column, i) => ({ key: column.key, fmt: fmt[kinds[i]], kind: kinds[i] }));
  const rank = (c) => (c.kind === "pct" ? 1 : c.kind === "usd" ? 2 : 3);
  const lead = [];
  for (const c of out) { if (c.kind === "text" && lead.length < 2) lead.push(c); else break; }   // the labels, up to two
  const rest = out.filter((c) => !lead.includes(c)).sort((a, b) => rank(a) - rank(b));
  const numeric = new Map(out.map((c) => [c.key, c.kind]));
  const converted = rows.map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => {
    const kind = numeric.get(key), cell = kind === "pct" || kind === "usd" ? parseCell(value) : null;
    return [key, cell && cell.kind ? cell.value : value];
  })));
  return { columns: lead.concat(rest).map(({ key, fmt: f }) => ({ key, fmt: f })), rows: converted };
}
// Plotly 6 saves numbers as {dtype, bdata} (base64 of a typed array): decoded here, so the page can rebase and label them.
function decodeNumbers(value) {
  if (Array.isArray(value)) return value;
  if (!value || !value.bdata) return Array.from(value || []);
  const types = { f8: Float64Array, f4: Float32Array, i1: Int8Array, u1: Uint8Array, i2: Int16Array, u2: Uint16Array, i4: Int32Array, u4: Uint32Array };
  const bytes = Uint8Array.from(atob(value.bdata), (c) => c.charCodeAt(0));
  const Type = types[value.dtype] || Float64Array;
  return Array.from(new Type(bytes.buffer, 0, Math.floor(bytes.length / Type.BYTES_PER_ELEMENT)));
}
const BENCHMARK_NAME = /^(BTC|ETH|SPY|S&P|SP500|QQQ|Same \$ in)/i;
function isReturnSeries(traces) {
  return traces.some((t) => t.type === "scatter" && Array.isArray(t.x) && t.x.length > 10 && /^\d{4}-\d{2}-\d{2}/.test(String(t.x[0])));
}
function isTimeSeriesChart(traces) {
  return traces.some((trace) => {
    const xs = decodeNumbers(trace.x || []);
    if (xs.length < 3) return false;
    const dates = xs.filter((x) => typeof x === "string" && /^\d{4}-\d{2}-\d{2}/.test(x)
      || typeof x === "number" && x > 1e9 && x < 1e14);
    return dates.length >= Math.min(3, Math.ceil(xs.length * 0.7));
  });
}
function decoratePublishedTraces(traces, layout) {
  const colors = [1, 2, 3, 4, 5].map((i) => token("--series-" + i));
  let copy = traces.map((trace) => Object.assign({}, trace, { x: decodeNumbers(trace.x), y: decodeNumbers(trace.y) }));
  // The ahead and behind segments of "Picks added" are one line (two coloured halves that end where the other starts read as a stray line).
  const ahead = copy.findIndex((t) => /^Picks added: ahead$/i.test(String(t.name))), behind = copy.findIndex((t) => /^Picks added: behind$/i.test(String(t.name)));
  if (ahead >= 0 && behind >= 0) {
    const y = copy[ahead].y.map((v, k) => (Number.isFinite(v) ? v : copy[behind].y[k])), last = [...y].reverse().find(Number.isFinite);
    const good = token("--good-text") || token("--good"), bad = token("--bad-text") || token("--critical");
    copy[ahead] = Object.assign({}, copy[ahead], { name: "Picks added", y, line: { color: last >= 0 ? good : bad, dash: "dot" } });
    copy = copy.filter((_, k) => k !== behind);
  }
  // The book's own curve starts where it first moves (months of a flat line before its first position say nothing); every
  // curve is cut there and rebased to 0%, so the benchmarks start level with the book.
  const main = copy.find((t) => t.type === "scatter" && !BENCHMARK_NAME.test(String(t.name || "")));
  if (main) {
    const moved = main.y.findIndex((v, k) => k > 0 && Number.isFinite(v) && Math.abs(v - main.y[0]) > 1e-9);
    const from = moved > 0 ? new Date(main.x[moved - 1]).getTime() : null;
    if (from) copy = copy.map((trace) => {
      const keep = trace.x.map((x, k) => (new Date(x).getTime() >= from ? k : -1)).filter((k) => k >= 0);
      if (!keep.length) return trace;
      const x = keep.map((k) => trace.x[k]), y = keep.map((k) => trace.y[k]);
      if (trace.type !== "scatter") return Object.assign({}, trace, { x, y });
      const base = 1 + (y.find(Number.isFinite) || 0) / 100;
      return Object.assign({}, trace, { x, y: y.map((v) => (Number.isFinite(v) ? Math.round(10000 * ((1 + v / 100) / base - 1)) / 100 : v)) });
    });
  }
  copy.forEach((trace) => { if (trace.type === "scatter" && BENCHMARK_NAME.test(String(trace.name || ""))) trace.line = Object.assign({}, trace.line, { dash: "dot" }); });
  layout.showlegend = false;
  delete layout.legend;
  layout.margin = Object.assign({}, layout.margin, { r: 28, t: 30 });
  if (layout.yaxis2) layout.yaxis2 = Object.assign({}, layout.yaxis2, { showticklabels: false, title: { text: "" } });   // the daily bars: the end labels own the right edge
  const lastAt = (trace) => { for (let k = trace.y.length - 1; k >= 0; k--) if (Number.isFinite(trace.y[k])) return k; return -1; };
  layout.annotations = copy.map((trace, i) => [trace, i]).filter(([trace]) => trace.type === "scatter" && lastAt(trace) >= 0)
    .map(([trace, i]) => ({ x: 1, y: trace.y[lastAt(trace)], xref: "paper", yref: "y", showarrow: false, xanchor: "right", xshift: -4,
      text: `${String(trace.name || "").replace(/ \(on equity\)/, "").replace(/^Same \$ in /, "")} ${readablePct(trace.y[lastAt(trace)] / 100)}`,
      font: { size: 11, color: (trace.line && trace.line.color) || colors[i % colors.length] }, ...endLabelBox() }));
  return copy;
}
// D/W/M/ALL controls sit with the chart title; return series rebase, other time charts zoom to the selected window.
function addPeriodChips(node) {
  const periods = [["D", 1], ["W", 7], ["M", 30], ["ALL", 0]];
  const key = "published:" + node.id;
  const current = () => {
    try { return JSON.parse(localStorage.getItem("desk-published-periods") || "{}")[key] || "ALL"; } catch (e) { return "ALL"; }
  };
  const row = node._chips || el("span", { class: "scale-chips published-period-chips" });
  row.replaceChildren(...periods.filter(([label]) => label !== current()).map(([label, days]) => {
    const button = el("button", { type: "button", class: "scale-chip", text: label });
    button.addEventListener("click", () => {
      const choices = (() => { try { return JSON.parse(localStorage.getItem("desk-published-periods") || "{}"); } catch (e) { return {}; } })();
      choices[key] = label;
      try { localStorage.setItem("desk-published-periods", JSON.stringify(choices)); } catch (e) {}
      row.querySelectorAll(".scale-chip").forEach((b) => b.classList.toggle("on", b === button));
      feedback("chart", "period", node.id, label);
      if (node._returnSeries) rebasePublished(node, days); else zoomPublished(node, days);
    });
    return button;
  }));
  node._chips = row;
  const title = node.closest(".card") && node.closest(".card").querySelector("h2");
  if (title && row.parentElement !== title) {
    const tools = title.querySelector(".card-tools");
    if (tools) title.insertBefore(row, tools); else title.append(row);
  }
}
function zoomPublished(node, days) {
  const xs = node.data.flatMap((trace) => Array.from(trace.x || []));
  const time = (x) => typeof x === "number" ? (x < 1e11 ? x * 1000 : x) : Date.parse(x);
  const last = Math.max(...xs.map(time).filter(Number.isFinite));
  if (!Number.isFinite(last)) return;
  const update = { "yaxis.autorange": true };
  Object.keys(node.layout || {}).filter((name) => /^xaxis\d*$/.test(name)).forEach((axis) => {
    update[axis + ".autorange"] = !days;
    update[axis + ".range"] = days ? [new Date(last - days * 86400000).toISOString(), new Date(last).toISOString()] : null;
  });
  Plotly.relayout(node, update);
}

function decoratePublishedBars(traces, layout) {
  const good = token("--good-text") || token("--good"), bad = token("--bad-text") || token("--critical");
  let horizontal = false;
  const data = traces.map((source) => {
    if (source.type !== "bar") return source;
    const trace = Object.assign({}, source);
    horizontal = horizontal || trace.orientation === "h";
    const key = trace.orientation === "h" ? "x" : "y";
    const values = decodeNumbers(trace[key]);
    if (!values.length || !values.some(isNum)) return trace;
    const axis = layout[trace.orientation === "h" ? "xaxis" : "yaxis"] || {};
    const format = String(axis.tickformat || "");
    const label = (value) => {
      if (!isNum(value)) return "";
      if (format.includes("%")) return readablePct(value);
      if (axis.ticksuffix === "%") return readablePct(value / 100);
      if (format.includes("$") || axis.tickprefix === "$") return usd(value);
      return (axis.tickprefix || "") + number(value) + (axis.ticksuffix || "");
    };
    const old = Array.isArray(trace.text) ? trace.text : values.map(() => trace.text || "");
    const text = values.map((value, i) => {
      const numberLabel = label(value), prior = String(old[i] || "").trim();
      return prior && !prior.includes(numberLabel) ? `${prior} · ${numberLabel}` : numberLabel;
    });
    const colors = values.map((value) => !isNum(value) || value === 0 ? token("--ink-2") : value > 0 ? good : bad);
    return Object.assign(trace, { [key]: values, text, customdata: values.map(label),
      hovertemplate: "%{fullData.name}: %{customdata}<extra></extra>", textposition: "outside", cliponaxis: false,
      outsidetextfont: LABEL_FONT(12, colors) });
  });
  if (data.some((trace, i) => trace !== traces[i] && trace.type === "bar")) {
    layout.margin = Object.assign({}, layout.margin, horizontal
      ? { l: Math.max(56, Number(layout.margin && layout.margin.l) || 0), r: Math.max(44, Number(layout.margin && layout.margin.r) || 0) }
      : { t: Math.max(42, Number(layout.margin && layout.margin.t) || 0) });
  }
  return data;
}

function rebasePublished(node, days) {
  node._orig = node._orig || node.data.map((trace) => ({ x: Array.from(trace.x || []), y: Array.from(trace.y || []) }));
  const last = Math.max(...node._orig.filter((t) => t.x.length).map((t) => new Date(t.x[t.x.length - 1]).getTime()));
  const start = days ? last - days * 86400000 : null;
  const ys = node._orig.map((trace, i) => {
    if (!start || node.data[i].type !== "scatter") return trace.y;
    let first = -1;
    trace.x.forEach((x, k) => { if (new Date(x).getTime() <= start) first = k; });
    if (first < 0) first = 0;
    const base = 1 + trace.y[first] / 100;
    return trace.y.map((y) => Math.round(10000 * ((1 + y / 100) / base - 1)) / 100);
  });
  const notes = (node.layout.annotations || []).map((note) => Object.assign({}, note));
  let k = 0;
  node.data.forEach((trace, i) => {
    if (trace.type !== "scatter") return;
    let at = ys[i].length - 1;
    while (at >= 0 && !Number.isFinite(ys[i][at])) at--;
    if (at < 0) return;
    const note = notes[k++];
    if (!note) return;
    const y = ys[i][at];
    note.y = y;
    note.text = note.text.replace(/-?[\d.]+%$/, readablePct(y / 100));
  });
  Plotly.restyle(node, { y: ys });
  Plotly.relayout(node, { annotations: notes, "xaxis.range": start ? [new Date(start).toISOString(), new Date(last + 86400000).toISOString()] : null, "xaxis.autorange": !start, "yaxis.autorange": true });
}
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
  const names = publishedTitles(tiles);
  [...content.querySelectorAll(".feed .card[data-published]")].forEach((card) => {
    if (wanted.has(card.dataset.published)) return;
    delete CARDS[cardId(card)];
    card.closest(".slot").remove();
  });
  tiles.forEach((tile) => {
    let card = [...content.querySelectorAll(".feed .card[data-published]")].find((node) => node.dataset.published === tile.key);
    const home = Number(tile.relevance) >= 0.5 ? feed : later;
    const anchorId = Object.entries(tileAnchors()).find(([prefix]) => tile.key.startsWith(prefix));
    const anchorCard = anchorId && document.getElementById(anchorId[1]) && document.getElementById(anchorId[1]).closest(".card");
    if (!card) {
      const inner = tile.kind === "chart" ? el("div", { class: "chart", id: "pub-" + tile.key }) : el("div", { class: "table-wrap", id: "t-pub-" + tile.key });
      card = el("div", { class: "card published", "data-published": tile.key }, [
        el("h2", { title: tile.reason || "", text: names.get(tile.key) }), inner]);   // why it is here: on hover, not a caption
      if (anchorCard && anchorCard.parentElement) anchorCard.parentElement.after(el("div", { class: "slot" }, [card]));   // beside the tile it was added from
      else home.append(card);
      adoptCard(card);
      applyCardState(card);
    }
    if (tile.kind === "chart" && typeof Plotly !== "undefined") {
      const base = baseLayout({ hovermode: "closest", margin: { l: 56, r: 24, t: 30, b: 40 }, colorway: [1, 2, 3, 4, 5].map((i) => token("--series-" + i)) });
      const layout = Object.assign({}, base, Object.fromEntries(Object.entries(tile.layout || {}).filter(([key]) => !PUBLISHED_AXES.includes(key))));
      PUBLISHED_AXES.forEach((key) => { if (base[key] || (tile.layout || {})[key]) layout[key] = Object.assign({}, base[key] || {}, (tile.layout || {})[key] || {}); });
      Object.keys(layout).filter((key) => /^xaxis\d*$/.test(key)).forEach((key) => {
        layout[key] = Object.assign({}, layout[key], { tickformatstops: DATE_TICK_FORMATS });
      });
      layout.margin = Object.assign({}, layout.margin, {
        l: Math.min(Number(layout.margin.l) || 40, 52), r: Math.min(Number(layout.margin.r) || 32, 36),
        t: Math.min(Number(layout.margin.t) || 30, 42), b: Math.min(Number(layout.margin.b) || 32, 42),
      });
      const node = document.getElementById("pub-" + tile.key);
      const series = isReturnSeries(tile.data || []);
      const data = (series ? decoratePublishedTraces(tile.data || [], layout) : decoratePublishedBars(tile.data || [], layout)).map(colorHover);
      if (node) whenVisible(node, () => Plotly.react(node, data, layout, CONFIG).then(() => {
        markNumericNotes();
        if (isTimeSeriesChart(tile.data || [])) {
          spreadEndLabels(node.id);
          node._returnSeries = series;
          node._orig = null;
          addPeriodChips(node);
          let period = "ALL";
          try { period = JSON.parse(localStorage.getItem("desk-published-periods") || "{}")["published:" + node.id] || "ALL"; } catch (e) {}
          const days = ({ D: 1, W: 7, M: 30, ALL: 0 })[period] ?? 0;
          if (days) { if (series) rebasePublished(node, days); else zoomPublished(node, days); }
        }
      }));
    } else if (tile.kind === "table") {
      renderTable("pub-" + tile.key, publishedTable(tile));
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
const isAssetColumn = (column) => /^(symbol|coin|asset)$/i.test(column);
function assetButton(market, label, className) {
  const button = el("button", { type: "button", class: className, text: label,
    title: `Open ${marketName(market)} chart`, "aria-label": `Open ${label} chart` });
  button.addEventListener("click", (event) => { event.stopPropagation(); openAsset(market); });
  return button;
}
const CANDLE_INTERVALS = [["1m", 1], ["5m", 5], ["15m", 15], ["1h", 60], ["4h", 240], ["1d", 1440], ["1w", 10080]];
// A symbol in a highlight (or the strip) opens the asset as a tile at the top of the page: price and candles (HL markets),
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
// Percentages in a line: a minus is red; a positive is green on the "what stands out" list (good news), plain elsewhere.
let numberContext = { positive: false };
function coloredNumbers(text) {
  const parts = [], pattern = /(-?\d+(?:\.\d+)?%)/g;
  let last = 0, match;
  const source = String(text);
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > last) parts.push(source.slice(last, match.index));
    const negative = match[1].startsWith("-"), cls = negative ? "neg" : numberContext.positive ? "pos" : "";
    parts.push(cls ? el("span", { class: cls, text: match[1] }) : match[1]);
    last = match.index + match[1].length;
  }
  if (last < source.length) parts.push(source.slice(last));
  return parts;
}
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
    if (words) parts.push(el("span", { class: "kind-run " + (crypto ? "kind-crypto" : "kind-stocks") }, coloredNumbers(words)));
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


// Executable lines: a highlight that names held symbols offers to act on them: trim a third or close, each planned with the
// daemon (its notices on top) and confirmed in one dialog. Only held HL positions; the web page runs them.
function symbolActions(text) {
  const found = [...knownSymbols()].filter((s) => new RegExp("(^|[^A-Za-z0-9])" + s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?![A-Za-z0-9])", "i").test(text));
  const targets = found.map((symbol) => ({ symbol, rows: assetRows(symbol).filter((r) => isNum(r.Value) && r._table !== "wallets") })).filter(({ rows }) => rows.length);
  if (!targets.length) return [];
  const button = el("button", { type: "button", class: "hl-act", title: "Act", "aria-label": "Act on " + targets.map((t) => t.symbol).join(", "), text: "▶" });
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    document.querySelectorAll(".more-menu").forEach((node) => node.remove());
    const menu = el("div", { class: "more-menu" }, targets.flatMap(({ symbol, rows }) => {
      const hl = rows.find((r) => /^HL /.test(String(r.Account || "")));
      const target = hl ? { coin: hl.Symbol, wallet: String(hl.Account).slice(-4) } : null;
      return [["Trim ⅓", "reduce", 1 / 3, `trim ${symbol} by a third`], ["Raise ⅓", "add", 1 / 3, `raise ${symbol} by a third`], ["Close", "close", 1, `close ${symbol}`]].map(([label, change, fraction, sentence]) => {
        const side = symbolSide(symbol);
        const row = el("button", { type: "button", class: "sections-row tile-pick" }, [el("b", { class: "name-" + (side || symbolKind(symbol)), text: symbol }), el("span", { text: label })]);
        row.addEventListener("click", () => { menu.remove(); if (target && window.DESK_SOURCE) runAction(label, change, fraction, target); else copyOrder(row, sentence); });
        return row;
      });
    }));
    button.after(menu);
    setTimeout(() => document.addEventListener("click", function away(e) { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener("click", away); } }), 0);
  });
  return [button];
}

function symbolKind(symbol) {   // crypto or stocks, by the table the symbol is held in (or its venue)
  const tables = (latest && latest.data && latest.data.tables) || {}, name = marketName(symbol).toLowerCase();
  if (((tables.positions_stocks || {}).rows || []).some((row) => marketName(row.Symbol).toLowerCase() === name)) return "stocks";
  return /^(spy|s&p500|nasdaq100)$/.test(name) ? "stocks" : "crypto";
}

// A highlight line: the crypto and stocks parts each become one tinted run (the word itself is not shown, only the colour
// behind what follows, symbols and figures included, to the next break), figures coloured, symbols as buttons.
function lineNodes(text, tint = true) {
  const source = String(text), found = [];
  KIND_WORDS.lastIndex = 0;
  let match;
  while ((match = KIND_WORDS.exec(source)) !== null) found.push({ at: match.index, end: match.index + match[0].length, word: match[1] });
  KIND_WORDS.lastIndex = 0;
  const plain = (piece) => linkSymbols(piece).flatMap((part) => (typeof part === "string" ? coloredNumbers(part) : [part]));
  if (!found.length) return plain(source);
  const nodes = [];
  if (found[0].at > 0) nodes.push(...plain(source.slice(0, found[0].at)));
  found.forEach((item, i) => {
    const next = i + 1 < found.length ? found[i + 1].at : source.length;
    const stop = source.slice(item.end, next).search(/[;.](\s|$)/);
    const end = stop >= 0 ? item.end + stop : next;
    const body = source.slice(item.end, end).replace(/^[:\s]+/, "").replace(/,\s*$/, "");
    if (body) nodes.push(tint ? el("span", { class: "kind-run " + (/crypto/i.test(item.word) ? "kind-crypto" : "kind-stocks") }, plain(body)) : el("span", {}, plain(body)));
    const between = source.slice(end, next);
    if (between) nodes.push(...plain(between));
  });
  return nodes;
}

function symbolSide(symbol) {   // long or short by what is held (the sign of the position's value), else null
  const held = assetRows(symbol).filter((row) => isNum(row.Value) && row._table !== "wallets");
  const net = held.reduce((sum, row) => sum + row.Value, 0);
  return held.length ? (net < 0 ? "short" : "long") : null;
}
function linkSymbols(text) {   // the text with each known symbol as a button that opens its tile, coloured by its side
  const symbols = [...knownSymbols()].filter((s) => s.length >= 2).sort((a, b) => b.length - a.length);
  if (!symbols.length) return [text];
  const escaped = symbols.map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp("(^|[^A-Za-z0-9])(" + escaped.join("|") + ")(?![A-Za-z0-9])(?:\\s+(long|short)\\b)?", "gi");
  const parts = [];
  let last = 0, match;
  while ((match = pattern.exec(text)) !== null) {
    const start = match.index + match[1].length;
    if (start > last) parts.push(text.slice(last, start));
    const symbol = match[2], side = (match[3] || symbolSide(symbol) || "").toLowerCase();
    parts.push(assetButton(symbol, symbol, "symbol-chip name-" + (side || symbolKind(symbol))));
    last = match.index + match[0].length;
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
  if (!held.length) return null;
  const value = held.reduce((sum, row) => sum + row.Value, 0), pnl = held.reduce((sum, row) => sum + (row.uPnL || 0), 0);
  const costs = held.reduce((sum, row) => sum + Math.abs(row.Value - (row.uPnL || 0)), 0), share = costs ? pnl / costs : NaN;
  const side = value < 0 ? "short" : "long", name = marketName(held[0].Symbol);
  const line = `${side}, ${usd(Math.abs(value))}, uPnL ${isNum(share) ? pct(share) : "n/a"}`;
  const trim = ["Trim", `trim ${name} by a third`, "reduce", 1 / 3], close = [`Close`, `close ${name}`, "close", 1],
    add = ["Raise", `raise the ${name} ${side} by a third`, "add", 1 / 3];
  const actions = share >= 0.1 ? [trim, close, add] : share <= -0.1 ? [close, trim, add] : [trim, add, close];
  // On the web dashboard an HL position's actions run through the daemon (plan, then confirm); IB and the local page copy.
  const hl = held.find((row) => /^HL /.test(String(row.Account || "")));
  return { line, actions, lead: share >= 0.1 ? "Winner: take part of it" : share <= -0.1 ? "Loser: decide" : "",
    target: hl && window.DESK_SOURCE ? { coin: hl.Symbol, wallet: String(hl.Account).slice(-4) } : null };
}
function candleColors() {
  const up = token("--good-text"), down = token("--bad-text");
  return {
    upColor: withAlpha(up, 0.42), downColor: withAlpha(down, 0.42),
    borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
  };
}
async function openAsset(symbol) {
  const name = friendlyName(symbol), rows = assetRows(symbol);
  const market = String(symbol).includes(":") ? symbol : rows.find((row) => /^HL /.test(String(row.Account || "")))?.Symbol || symbol;
  const key = "asset-" + String(market).toUpperCase().replace(/[^A-Z0-9]/g, "_");
  feedback("asset", "open", name);
  let feed = document.getElementById("feed");
  if (!feed) { renderPublished([]); feed = document.getElementById("feed"); }
  if (!feed) return;
  feed.hidden = false;
  const existing = document.getElementById(key)?.closest(".asset-card");
  if (existing) {
    existing.classList.remove("card-off", "card-min");
    existing.refreshPosition();
    existing.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const body = el("div", { class: "asset-body", id: key });
  const card = el("div", { class: "card asset-card", "data-asset": key }, [el("h2", { text: name }), body]);
  feed.prepend(card);
  adoptCard(card);
  requestAnimationFrame(() => card.scrollIntoView({ behavior: "smooth", block: "center" }));
  const external = rows.some((row) => /^IB/.test(String(row.Account || ""))) ? stockLink(name) : hlLink(market);
  const chart = el("div", { class: "chart asset-chart", text: "Loading candles…" });
  const price = el("span", { class: "asset-price" });
  const scales = el("div", { class: "scale-chips asset-scales", role: "group", "aria-label": "Candle interval" });
  const copy = (sentence, button) => {
    const label = button.textContent;
    button.disabled = true;
    Promise.resolve().then(() => navigator.clipboard.writeText(sentence)).then(() => {
      button.textContent = "✓";
      feedback("asset", "action", name, sentence);
    }, () => { button.textContent = "!"; }).finally(() => {
      setTimeout(() => { button.textContent = label; button.disabled = false; }, 2500);
    });
  };
  const move = el("span", { class: "asset-change", title: "24h price change" });
  const position = el("span", { class: "asset-position" });
  const tradeActions = el("div", { class: "asset-trade-actions", role: "group", "aria-label": `Position actions for ${name}` });
  let hlName;
  card.refreshPosition = () => {
    const advice = assetAdvice(assetRows(symbol));
    position.textContent = advice?.line || "";
    if (!advice) {
      const open = el("button", { type: "button", class: "row-act chart-act lead", text: "▶", title: `Open ${name}`, "aria-label": `Open ${name}` });
      open.addEventListener("click", () => {
        if (assetAdvice(assetRows(symbol))) { card.refreshPosition(); return; }
        const title = `Open ${name}`;
        const { overlay, box } = actionDialog(title);
        const sides = ["Long", "Short"].map((label) => {
          const side = label.toLowerCase();
          const button = el("button", { type: "button", class: "asset-action", text: label });
          button.addEventListener("click", () => {
            if (window.DESK_SOURCE && hlName) {
              overlay.remove();
              runAction(label, side, 1, { coin: hlName, wallet: "07f2" });
            } else {
              copy(`${side} ${hlName || market}`, button);
            }
          });
          return button;
        });
        box.replaceChildren(el("h3", { text: title }), el("div", { class: "asset-actions" }, sides), closeButton(overlay));
      });
      tradeActions.replaceChildren(open);
      return;
    }
    tradeActions.replaceChildren(...advice.actions.map(([label, sentence, change, fraction], i) => {
      const button = el("button", { type: "button", class: "row-act chart-act" + (i === 0 && advice.lead ? " lead" : ""),
        title: advice.target ? `Plan it, then confirm: ${sentence}` : `Copy: ${sentence}`,
        "aria-label": `${label} ${name}`, text: { reduce: "⅓", add: "▲", close: "✕" }[change] });
      button.addEventListener("click", () => {
        const current = assetAdvice(assetRows(symbol));
        const action = current?.actions.find(([, , type]) => type === change);
        if (!action) { card.refreshPosition(); return; }
        if (current.target) runAction(label, change, fraction, current.target);
        else copy(action[1], button);
      });
      return button;
    }));
  };
  card.refreshPosition();
  const strip = el("button", { type: "button", class: "row-act chart-act", title: "Add to price strip", "aria-label": `Add ${name} to price strip`, text: "+" });
  strip.addEventListener("click", () => { const s = tickerState(); if (!s.assets.includes(name)) s.assets.push(name); s.on = true; saveTicker(s); refreshTicker(); drawModes(); strip.textContent = "✓"; });
  body.replaceChildren(
    scales,
    el("div", { class: "asset-plot" }, [
      chart,
      el("div", { class: "asset-line" }, [price, move, position, tradeActions]),
    ]),
    el("div", { class: "asset-links" }, [
      el("a", { class: "row-act chart-act symbol-link", href: external, target: "_blank", rel: "noopener noreferrer",
        title: "Open market page", "aria-label": `Open ${name} market page`, text: "↗" }),
      strip,
    ]));
  await loadUniverse();
  hlName = String(market).includes(":") ? market : universeMap[String(market).toLowerCase()];
  if (!hlName) {
    chart.textContent = "No Hyperliquid market found for this asset.";
    return;
  }
  tickerContexts([hlName]).then((contexts) => {
    const context = contexts[hlName];
    if (context) {
      const change = context.previous ? context.price / context.previous - 1 : NaN;
      if (isNum(context.price)) price.textContent = priceText(context.price);
      if (isNum(change)) {
        move.textContent = pct(change, 1, true);
        move.className = `asset-change ${signClass(change)}`;
      }
    }
  });

  let request;
  async function loadCandles(interval, minutes) {
    request?.abort();
    const controller = new AbortController();
    request = controller;
    for (const chip of scales.children) {
      const active = chip.textContent === interval;
      chip.classList.toggle("on", active);
      chip.setAttribute("aria-pressed", String(active));
    }
    chart._lightweightChart?.remove();
    chart._lightweightChart = null;
    chart._candleSeries = null;
    chart.textContent = "Loading candles…";
    try {
      const end = Date.now();
      const response = await fetch(HL_INFO, { method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ type: "candleSnapshot", req: { coin: hlName, interval, startTime: end - 168 * minutes * 60000, endTime: end } }) });
      if (!response.ok) throw new Error("Candle request failed");
      const candles = await response.json();
      if (controller.signal.aborted) return;
      if (!Array.isArray(candles) || !candles.length || !window.LightweightCharts) {
        chart.textContent = "Candles are unavailable for this asset.";
        return;
      }
      chart.replaceChildren();
      const entry = rows.map((row) => row["Avg price"]).find(isNum);
      const border = token("--grid"), ink = token("--ink-2");
      const instance = LightweightCharts.createChart(chart, {
        autoSize: true,
        layout: { background: { type: "solid", color: token("--surface") }, textColor: ink, attributionLogo: true },
        grid: { vertLines: { color: border }, horzLines: { color: border } },
        rightPriceScale: { borderColor: border, scaleMargins: { top: 0.35, bottom: 0.12 } },
        timeScale: { borderColor: border, timeVisible: minutes < 1440, secondsVisible: false, rightOffset: 2 },
        crosshair: { vertLine: { color: token("--muted"), labelBackgroundColor: token("--ink-2") },
          horzLine: { color: token("--muted"), labelBackgroundColor: token("--ink-2") } },
        handleScroll: true, handleScale: true,
      });
      chart._lightweightChart = instance;
      const series = instance.addSeries(LightweightCharts.CandlestickSeries, {
        ...candleColors(), priceLineVisible: false,
      });
      chart._candleSeries = series;
      series.setData(candles.map((c) => ({ time: Math.floor(Number(c.t) / 1000), open: Number(c.o), high: Number(c.h), low: Number(c.l), close: Number(c.c) }))
        .filter((c) => Number.isFinite(c.time) && Number.isFinite(c.open) && Number.isFinite(c.high) && Number.isFinite(c.low) && Number.isFinite(c.close)));
      if (isNum(entry)) series.createPriceLine({ price: entry, color: token("--muted"), lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true, title: "Entry" });
      instance.timeScale().fitContent();
    } catch (error) {
      if (!controller.signal.aborted) chart.textContent = "Candles could not be loaded.";
    }
  }
  for (const [interval, minutes] of CANDLE_INTERVALS) {
    const chip = el("button", { type: "button", class: "scale-chip", text: interval });
    chip.addEventListener("click", () => loadCandles(interval, minutes));
    scales.append(chip);
  }
  return loadCandles("1h", 60);
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

// A notice appears only when something went wrong (a successful order says nothing): bottom right, until it is dismissed.
function notify(lines, title) {
  let host = document.getElementById("notices");
  if (!host) { host = el("div", { id: "notices", role: "alert" }); document.body.append(host); }
  const close = el("button", { type: "button", class: "notice-close", "aria-label": "Dismiss", text: "×" });
  const note = el("div", { class: "notice-card" }, [close, el("b", { text: title }), ...lines.filter(Boolean).map((line) => el("div", { class: "notice-line", text: line }))]);
  close.addEventListener("click", () => note.remove());
  host.append(note);
}

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
    body: JSON.stringify(Object.assign({ change, coin, wallet: target.wallet, fraction, label: target.label }, body)) }).then((r) => r.json());
  let plan;
  try { plan = await post({ stage: "plan" }); } catch (e) { plan = { error: String(e) }; }
  if (plan.error || !plan.token) {
    const { notices, mine } = planLines(plan.plan || plan.error || "", coin);
    box.replaceChildren(el("h3", { text: title }), el("p", { class: "neg", text: "Nothing can be sent." }),
      ...notices.concat(mine).map((line) => el("p", { class: "action-line", text: line })), closeButton(overlay));
    return;
  }
  const { notices, mine } = planLines(plan.plan, coin);
  const confirm = el("button", { type: "button", class: "asset-action lead", text: "✓", title: `${label} ${marketName(coin)}`, "aria-label": `Confirm: ${label} ${marketName(coin)}` });
  const cancel = el("button", { type: "button", class: "asset-action", text: "✗", title: "Cancel", "aria-label": "Cancel" });
  cancel.addEventListener("click", () => { feedback("action", "cancel", marketName(coin), label); overlay.remove(); });
  confirm.addEventListener("click", async () => {
    overlay.remove();   // gone at once; if something goes wrong a notice appears
    feedback("action", "confirm", marketName(coin), label);
    let result;
    try { result = await post({ stage: "confirm", token: plan.token }); } catch (e) { result = { error: String(e) }; }
    if (result.code !== 0) {
      const lines = planLines(result.result || result.error || "", coin);
      notify(lines.notices.concat(lines.mine), `${title}: not completed`);
    }
    lastRendered = null; load();
  });
  box.replaceChildren(el("h3", { text: title }),
    ...notices.map((line) => el("p", { class: "action-line notice", text: line })),
    ...mine.map((line) => el("p", { class: "action-line", text: line })),
    el("div", { class: "asset-actions" }, [confirm, cancel]));
}

// Exposure to an asset class from the return chart: reduce or add a third of every held HL position in the class. Each
// position is planned with the daemon first; one dialog shows the plans and their notices, and Confirm sends them in turn.
async function runClassAction(kind, change, fraction) {
  const id = kind === "crypto" ? "positions" : "positions_stocks";
  const rows = (((latest && latest.data && latest.data.tables) || {})[id] || { rows: [] }).rows
    .filter((row) => /^HL /.test(String(row.Account || "")) && isNum(row.Value));
  const word = change === "reduce" ? "Reduce" : "Add to";
  const title = `${word} ${kind} by a third`;
  feedback("action", "plan class", kind, title);
  if (!window.DESK_SOURCE) {   // the local page copies the order for Claude
    if (navigator.clipboard) navigator.clipboard.writeText(`${change === "reduce" ? "trim" : "add a third to"} all ${kind} positions${change === "reduce" ? " by a third" : ""}`);
    return;
  }
  const { overlay, box } = actionDialog(title);
  box.replaceChildren(el("h3", { text: title }), el("p", { class: "muted", text: "Planning each position…" }));
  const post = (row, body) => fetch("/action", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
    body: JSON.stringify(Object.assign({ change, coin: row.Symbol, wallet: String(row.Account).slice(-4), fraction }, body)) }).then((r) => r.json());
  const plans = [];
  for (const row of rows) { try { plans.push([row, await post(row, { stage: "plan" })]); } catch (e) { plans.push([row, { error: String(e) }]); } }
  const ready = plans.filter(([, plan]) => plan.token);
  const lines = plans.flatMap(([row, plan]) => { const l = planLines(plan.plan || plan.error || "", row.Symbol); return [...l.notices.map((t) => ["notice", t]), ...l.mine.slice(-1).map((t) => ["", t])]; });
  const confirm = el("button", { type: "button", class: "asset-action lead", text: "✓", title: `${ready.length} position${ready.length === 1 ? "" : "s"}`, "aria-label": "Confirm" });
  const cancel = el("button", { type: "button", class: "asset-action", text: "✗", title: "Cancel", "aria-label": "Cancel" });
  cancel.addEventListener("click", () => { feedback("action", "cancel class", kind, title); overlay.remove(); });
  confirm.addEventListener("click", async () => {
    overlay.remove();   // gone at once; a notice only for the positions that did not go through
    feedback("action", "confirm class", kind, title);
    const failed = [];
    for (const [row, plan] of ready) { try { const r = await post(row, { stage: "confirm", token: plan.token }); if (r.code !== 0) failed.push(`${marketName(row.Symbol)} not completed`); } catch (e) { failed.push(`${marketName(row.Symbol)} failed`); } }
    if (failed.length) notify(failed, title);
    lastRendered = null; load();
  });
  box.replaceChildren(el("h3", { text: title }), ...lines.map(([cls, t]) => el("p", { class: "action-line " + cls, text: t })),
    el("div", { class: "asset-actions" }, [...(ready.length ? [confirm] : []), cancel]));
}


// Ladder actions from the market-making rows: stop one, or start a neutral ladder on a candidate (web page; the server asks the
// daemon). One small confirm, no plan; a notice only if it did not go through.
function runLadder(mode, coin, wallet) {
  const name = marketName(coin), title = mode === "stop" ? `Stop the ${name} ladder` : `Start a neutral ladder on ${name}`;
  feedback("action", "plan ladder", name, mode);
  const { overlay, box } = actionDialog(title);
  const go = el("button", { type: "button", class: "asset-action lead", text: "✓", title: mode === "stop" ? "Stop" : "Start", "aria-label": "Confirm" });
  const cancel = el("button", { type: "button", class: "asset-action", text: "✗", title: "Cancel", "aria-label": "Cancel" });
  cancel.addEventListener("click", () => overlay.remove());
  go.addEventListener("click", async () => {
    overlay.remove();
    feedback("action", "confirm ladder", name, mode);
    let result;
    try { result = await (await fetch("/ladder", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify({ mode, coin, wallet }) })).json(); } catch (e) { result = { error: String(e) }; }
    if (result.error || result.code !== 0) notify([String(result.result || result.error || "").split("\n").filter(Boolean).slice(-3).join(" ")], `${title}: not completed`);
    lastRendered = null; load();
  });
  box.replaceChildren(el("h3", { text: title }), el("div", { class: "asset-actions" }, [go, cancel]));
}
function copyOrder(button, sentence) {   // positions the dashboard cannot trade (IB): the order is copied for Claude
  feedback("action", "copy order", sentence);
  if (navigator.clipboard) navigator.clipboard.writeText(sentence);
  const old = button.textContent; button.textContent = "✓"; setTimeout(() => { button.textContent = old; }, 1200);
}

function closeButton(overlay) {
  const button = el("button", { type: "button", class: "asset-action", text: "Close" });
  button.addEventListener("click", () => overlay.remove());
  return el("div", { class: "asset-actions" }, [button]);
}


// "+" on a tile: the analytics that go deeper on it, related ones first (matched on the tile's name and its section
// keywords), all of them searchable; a pick is built and placed next to this tile.
function openMoreMenu(card, button) {
  document.querySelectorAll(".more-menu").forEach((node) => node.remove());
  const catalog = (latest && latest.data && latest.data.catalog) || [];
  const words = cardTitle(card).toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 3);
  const score = (entry) => words.filter((w) => (entry.title + " " + entry.keywords + " " + entry.description).toLowerCase().includes(w)).length;
  const menu = el("div", { class: "more-menu" });
  const field = el("input", { type: "search", class: "sections-search", placeholder: "search", "aria-label": "Search analytics" });
  const list = el("div", {});
  const fill = () => {
    const q = field.value.trim().toLowerCase();
    const rows = catalog.filter((entry) => !q || (entry.title + " " + entry.keywords).toLowerCase().includes(q))
      .map((entry) => ({ entry, s: score(entry) })).sort((a, b) => b.s - a.s);
    const selectedFor = ({ entry }) => {
      const prefix = entry.id.replace(":", "-");
      const matches = [...document.querySelectorAll(".feed .card[data-published]")].filter((tile) => tile.dataset.published.startsWith(prefix));
      return matches.some((tile) => !tile.classList.contains("card-off"));
    };
    const selected = rows.filter(selectedFor);
    const favorites = rows.filter((row) => row.s > 0 && !selectedFor(row));
    const moreRows = rows.filter((row) => row.s === 0 && !selectedFor(row));
    const makeRow = ({ entry, s }) => {
      const prefix = entry.id.replace(":", "-");
      const matches = [...document.querySelectorAll(".feed .card[data-published]")].filter((tile) => tile.dataset.published.startsWith(prefix));
      const selected = matches.some((tile) => !tile.classList.contains("card-off"));
      const row = el("div", { class: "sections-row" + (selected ? " is-picked" : ""), role: "checkbox", tabindex: "0",
        "aria-checked": String(selected), text: (s ? "★ " : "") + entry.title });
      const toggle = () => {
        if (!matches.length) { pullTile(entry, null, cardId(card)).then(fill); return; }
        const show = !selected;
        matches.forEach((tile) => tile.classList.toggle("card-off", !show));
        const state = layoutState(); state.hidden = [...document.querySelectorAll("#content .card-off")].map(cardId); saveLayout(state);
        document.querySelectorAll(".feed").forEach((feed) => { feed.hidden = !feed.querySelector(".card:not(.card-off)"); });
        feedback("section", show ? "show" : "hide", entry.title);
        if (show) matches.forEach((tile) => tile.querySelectorAll(".js-plotly-plot").forEach((plot) => { try { Plotly.Plots.resize(plot); } catch (e) {} }));
        fill();
      };
      row.addEventListener("click", toggle);
      row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } });
      return row;
    };
    const group = (label, items) => items.length ? [el("div", { class: "more-menu-group", text: label }), ...items.map(makeRow)] : [];
    list.replaceChildren(...group("Favorites", favorites), ...group("Selected", selected), ...group("More", moreRows));
  };
  field.addEventListener("input", fill);
  menu.append(field, list);
  fill();
  card.querySelector("h2").append(menu);
  field.focus();
  setTimeout(() => document.addEventListener("click", function away(event) {
    if (!menu.contains(event.target) && event.target !== button) { menu.remove(); document.removeEventListener("click", away); }
  }), 0);
  feedback("section", "more", cardTitle(card));
}

function saveOrder() {
  const state = layoutState(), content = document.getElementById("content"), cols = currentColumns(content);
  // Every arrangement is kept (the last 12), so the one before can be brought back from the menu.
  const last = (state.history || [])[0];
  if (!last || JSON.stringify(last.cols) !== JSON.stringify(cols)) state.history = [{ at: Date.now(), cols }].concat(state.history || []).slice(0, 12);
  state.cols = Object.assign({}, state.cols, { [cols.length]: cols });
  state.order = cols.flat();   // column after column: the fallback for a screen width not arranged yet
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
  const item = (text, selected, action, title = "") => {
    const row = el("div", { class: "sections-row" + (selected ? " is-picked" : ""), role: "checkbox", tabindex: "0",
      "aria-checked": String(selected), text, title });
    row.addEventListener("click", action);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); action(); }
    });
    return row;
  };
  // Ordered by how much each tile is used (clicks, and the time the pointer rests on it), most used first.
  const usage = usageState(), used = (card) => { const u = usage[cardId(card)] || {}; return (u.clicks || 0) + (u.seconds || 0) / 30; };
  const present = [...document.querySelectorAll("#content .slot")].map((slot) => slot.firstElementChild).filter(Boolean)
    .filter((card) => !card.classList.contains("published"))
    .sort((a, b) => used(b) - used(a));
  const rows = present.filter((card) => !card.classList.contains("card-off") && menuMatches(cardTitle(card)))
    .map((card) => item(cardTitle(card).trim(), true, () => setHidden(cardId(card), true)));
  // Available to add: key-number tiles that were hidden, and the notebook sections that can be pulled onto the dashboard.
  const addable = present.filter((card) => card.classList.contains("card-off") && menuMatches(cardTitle(card)))
    .map((card) => item(cardTitle(card).trim(), false, () => setHidden(cardId(card), false)));
  [...document.querySelectorAll("#tiles .tile")].filter((tile) => menuMatches(tile.dataset.label || "")).forEach((tile) => {
    const label = tile.dataset.label;
    rows.push(item(label, true, () => {
      const state = layoutState(); state.tileHidden = [...new Set([...(state.tileHidden || []), label])]; saveLayout(state);
      feedback("tile", "hide", label); if (latest && latest.data) renderTiles(latest.data.kpis || {}, latest.data.theses); drawSectionsMenu();
    }));
  });
  (layoutState().tileHidden || []).filter(menuMatches).forEach((label) => {
    addable.push(item(label, false, () => { const s = layoutState(); s.tileHidden = (s.tileHidden || []).filter((x) => x !== label); saveLayout(s);
      feedback("tile", "show", label); if (latest && latest.data) renderTiles(latest.data.kpis || {}, latest.data.theses); drawSectionsMenu(); }, "Key number"));
  });
  const attention = ((latest && latest.data && latest.data.highlights && latest.data.highlights.attention) || []).map((row) => row.join(" ")).join(" ").toLowerCase();
  const catalog = ((latest && latest.data && latest.data.catalog) || []).filter((entry) => menuMatches(entry.title + " " + entry.description + " " + entry.keywords))
    .map((entry) => ({ entry, score: entry.keywords.split(/\s+/).filter((word) => word && attention.includes(word)).length }))
    .sort((a, b) => b.score - a.score);
  catalog.forEach(({ entry, score }) => {
    const prefix = entry.id.replace(":", "-");
    const matches = [...document.querySelectorAll(".feed .card[data-published]")].filter((card) => card.dataset.published.startsWith(prefix));
    const selected = matches.some((card) => !card.classList.contains("card-off"));
    const row = item((score ? "★ " : "") + entry.title, selected, () => {
      if (!matches.length) { pullTile(entry, null); return; }
      const show = !selected;
      matches.forEach((card) => card.classList.toggle("card-off", !show));
      const state = layoutState(); state.hidden = [...document.querySelectorAll("#content .card-off")].map(cardId); saveLayout(state);
      document.querySelectorAll(".feed").forEach((feed) => { feed.hidden = !feed.querySelector(".card:not(.card-off)"); });
      feedback("section", show ? "show" : "hide", entry.title);
      drawSectionsMenu();
      if (show && latest && latest.data) renderCharts(latest.data.charts);
    });
    (selected ? rows : addable).push(row);
  });
  const button = document.getElementById("sections-button");
  if (button) button.classList.toggle("has-suggestion", rarelyUsed().length > 0);
  lists.replaceChildren(...rows, ...addable,
    ...(rows.length || addable.length ? [] : [el("div", { class: "sections-empty", text: "No match" })]), ...menuFooter());
}
async function pullTile(entry, button, anchor) {   // the section is built (the Mac for the local page, the server for the web) and published
  feedback("tile", "pull", entry.title);
  if (anchor) { const anchors = tileAnchors(); anchors[entry.id.replace(":", "-")] = anchor; saveTileAnchors(anchors); }
  if (button) { button.textContent = "…"; button.disabled = true; }
  const web = Boolean(window.DESK_SOURCE);
  const start = web ? () => fetch("/pull", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: entry.id }) })
    : () => fetch("/api/tiles/pull", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: entry.id }) });
  const status = web ? async () => (await (await fetch("/pull", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: entry.id, stage: "status" }) })).json()).status || ""
    : async () => (await (await fetch("/api/tiles/status")).json())[entry.id] || "";
  try {
    await start();
    for (let tries = 0; tries < 90; tries++) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const now = await status();
      if (now.startsWith("done")) { if (button) button.textContent = "✓"; lastRendered = null; await load(); return; }
      if (now.startsWith("failed") || now.startsWith("nothing")) { if (button) { button.textContent = "!"; button.title = now; button.disabled = false; } return; }
    }
  } catch (e) { if (button) { button.textContent = "!"; button.disabled = false; } }
}
// A pulled tile sits next to the tile it was added from (remembered per section).
function tileAnchors() { try { return JSON.parse(localStorage.getItem("desk-anchors")) || {}; } catch (e) { return {}; } }
function saveTileAnchors(map) { try { localStorage.setItem("desk-anchors", JSON.stringify(map)); } catch (e) {} }
function menuFooter() {
  const state = highlightState(), hidden = state.deleted.length + Object.keys(state.read).length;
  const out = [];
  if (hidden) {
    const restore = el("button", { type: "button", class: "sections-reset", text: `Restore ${hidden} hidden highlight${hidden === 1 ? "" : "s"}` });
    restore.addEventListener("click", () => { saveHighlightState({ read: {}, deleted: [] }); feedback("highlight", "restore", "all"); redrawHighlights(); });
    out.push(restore);
  }
  const history = layoutState().history || [];
  const undo = history.length > 1 ? el("button", { type: "button", class: "sections-reset", text: "Undo last arrangement" }) : null;
  if (undo) undo.addEventListener("click", () => {   // the arrangement before this one
    const state = layoutState(), previous = state.history[1];
    state.history = state.history.slice(1);
    state.cols = Object.assign({}, state.cols, { [previous.cols.length]: previous.cols });
    state.order = previous.cols.flat();
    saveLayout(state);
    feedback("section", "undo arrangement", "layout");
    buildColumns(document.getElementById("content"), previous.cols);
    if (latest && latest.data) renderCharts(latest.data.charts);
    drawSectionsMenu();
  });
  const reset = el("button", { type: "button", class: "sections-reset", text: "Reset layout" });
  reset.addEventListener("click", () => { try { localStorage.removeItem(LAYOUT_KEY); } catch (e) {} location.reload(); });
  if (undo) out.push(undo);
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
const priceText = (value) => (value >= 1000 ? Math.round(value).toLocaleString("en-US") : value >= 100 ? value.toFixed(1).replace(/\.0$/, "") : String(Number(value.toPrecision(5))));   // thousands: whole numbers
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
let universeRequest;
// The names people type (no venue prefix; a few markets by their common name) and the HL market each one means.
const MARKET_ALIASES = { SP500: "S&P500", XYZ100: "Nasdaq100" };
const universeMap = {};   // lower-case typed name -> HL market ("amd" -> "xyz:AMD", "s&p500" -> "xyz:SP500")
const friendlyName = (market) => { const bare = marketName(market); return MARKET_ALIASES[bare] || bare; };
async function loadUniverse() {
  if (!universeRequest) universeRequest = fetchUniverse();
  return universeRequest;
}
async function fetchUniverse() {
  const names = [];
  for (const dex of ["", "xyz"]) {
    try {
      const response = await fetch(HL_INFO, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ type: "meta" }, dex ? { dex } : {})) });
      (await response.json()).universe.forEach((asset) => { if (!asset.isDelisted) names.push(asset.name); });
    } catch (e) { universeRequest = null; }
  }
  let list = document.getElementById("tick-universe");
  if (!list) { list = el("datalist", { id: "tick-universe" }); document.body.append(list); }
  const friendly = [];
  names.forEach((market) => {   // the main DEX wins a name both carry
    const name = friendlyName(market), key = name.toLowerCase();
    if (!universeMap[key] || !market.includes(":")) { if (!universeMap[key]) friendly.push(name); universeMap[key] = market; }
    universeMap[marketName(market).toLowerCase()] = universeMap[marketName(market).toLowerCase()] || market;
  });
  list.replaceChildren(...friendly.sort().map((name) => el("option", { value: name })));
}
// Outliers among the open positions (a 24h move far from the rest, at least 4%) join the pulse bar while they are outliers and held.
function outlierAssets() {
  const tables = (latest && latest.data && latest.data.tables) || {};
  const rows = ["positions", "positions_stocks"].flatMap((id) => ((tables[id] || {}).rows || []))
    .filter((row) => /^HL /.test(String(row.Account || "")) && isNum(row["24h change (%)"]));
  if (rows.length < 4) return [];
  const moves = rows.map((row) => row["24h change (%)"]), mean = moves.reduce((a, b) => a + b, 0) / moves.length;
  const spread = Math.sqrt(moves.reduce((a, b) => a + (b - mean) ** 2, 0) / moves.length) || 1;
  return rows.filter((row) => Math.abs(row["24h change (%)"]) >= 0.04 && Math.abs(row["24h change (%)"] - mean) / spread >= 2).map((row) => row.Symbol);
}

// A chip on the pulse bar is actionable on hover: for a position held on HL, trim a third, raise a third (add to it: poker's
// "raise"; craps calls it a "press") or close; for an asset not held, short or long it (a conviction entry). The web page
// plans each with the daemon and asks once; the local page copies the order.
function chipActions(asset) {
  const rows = assetRows(asset).filter((row) => /^HL /.test(String(row.Account || "")) && isNum(row.Value)), name = friendlyName(asset);
  const buttons = [];
  const add = (text, label, run, sentence) => {
    const b = el("button", { type: "button", class: "tick-act", title: label, "aria-label": `${label} ${name}`, text });
    b.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); if (window.DESK_SOURCE) run(); else copyOrder(b, sentence); });
    buttons.push(b);
  };
  if (rows.length) {
    const target = { coin: rows[0].Symbol, wallet: String(rows[0].Account).slice(-4) };
    add("⅓", "Trim a third", () => runAction("Trim ⅓", "reduce", 1 / 3, target), `trim ${name} by a third`);
    add("▲", "Raise a third", () => runAction("Raise ⅓", "add", 1 / 3, target), `raise the ${name} by a third`);
    add("✕", "Close", () => runAction("Close", "close", 1, target), `close ${name}`);
  } else if (universeMap[asset.toLowerCase()] || asset) {
    const market = universeMap[asset.toLowerCase()] || asset;
    add("S", "Short it", () => runAction("Short", "short", 1, { coin: market, wallet: "07f2" }), `short ${name}`);
    add("L", "Long it", () => runAction("Long", "long", 1, { coin: market, wallet: "07f2" }), `long ${name}`);
  }
  return el("span", { class: "tick-pop" }, buttons);
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
  await loadUniverse();
  universeMap.spy = universeMap.spy || universeMap["s&p500"] || universeMap.sp500;   // SPY on the strip is HL's S&P 500 perp
  const marketOf = (asset) => universeMap[asset.toLowerCase()] || asset;
  const auto = outlierAssets().filter((asset) => !state.assets.some((mine) => marketOf(mine) === asset));
  const shownAssets = state.assets.concat(auto);
  const contexts = await tickerContexts(shownAssets.map(marketOf));
  const adding = strip.querySelector(".tick-input");
  if (adding && document.activeElement === adding) return;   // do not redraw under a field being typed in
  const addBox = el("span", { class: "tick-add" }, [el("input", { type: "text", class: "tick-input", placeholder: "+ asset", list: "tick-universe",
    autocomplete: "off", "aria-label": "Add an asset to the price strip", size: "8" })]);
  addBox.firstChild.addEventListener("focus", loadUniverse);
  const addAsset = () => {
    const name = addBox.firstChild.value.trim();
    if (!name) return;
    const s = tickerState();
    const known = universeMap[name.toLowerCase()];   // the typed name's market (AMD -> xyz:AMD, S&P500 -> xyz:SP500)
    const asset = known ? known : name.includes(":") ? name.replace(/^([A-Za-z0-9]+):(.*)$/, (m, dex, coin) => dex.toLowerCase() + ":" + coin.toUpperCase()) : name.toUpperCase();
    if (!s.assets.includes(asset)) s.assets.push(asset);
    addBox.firstChild.value = "";
    addBox.firstChild.blur();   // an empty focused field blocks refreshTicker from redrawing the newly added chip
    saveTicker(s); feedback("look", "price strip add", asset); refreshTicker();
  };
  addBox.firstChild.addEventListener("keydown", (event) => { if (event.key === "Enter") addAsset(); });
  addBox.firstChild.addEventListener("input", (event) => {   // a suggestion picked from the list (not a typed letter) adds at once
    if (event.inputType && event.inputType !== "insertReplacementText") return;
    if (universeMap[addBox.firstChild.value.trim().toLowerCase()]) addAsset();
  });
  addBox.firstChild.addEventListener("change", () => {   // and when the field is left with a known name in it
    const value = addBox.firstChild.value.trim();
    if (universeMap[value.toLowerCase()]) addAsset();
  });
  const dropper = (asset) => {
    const x = el("button", { type: "button", class: "tick-x", title: "Remove " + asset, "aria-label": "Remove " + asset, text: "×" });
    x.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); const s = tickerState(); s.assets = s.assets.filter((a) => a !== asset); saveTicker(s); refreshTicker(); });
    return x;
  };
  const reorderable = (chip, asset) => {   // drag a chip onto another to move it there
    chip.setAttribute("draggable", "true");
    chip.addEventListener("dragstart", (event) => { event.dataTransfer.setData("text/plain", "tick:" + asset); event.dataTransfer.effectAllowed = "move"; });
    chip.addEventListener("dragover", (event) => { event.preventDefault(); chip.classList.add("drop-target"); });
    chip.addEventListener("dragleave", () => chip.classList.remove("drop-target"));
    chip.addEventListener("drop", (event) => {
      event.preventDefault(); chip.classList.remove("drop-target");
      const moving = String(event.dataTransfer.getData("text/plain")).replace(/^tick:/, "");
      const s = tickerState(), from = s.assets.indexOf(moving), to = s.assets.indexOf(asset);
      if (from < 0 || to < 0 || from === to) return;
      s.assets.splice(to, 0, s.assets.splice(from, 1)[0]);
      saveTicker(s); feedback("look", "price strip move", moving); refreshTicker();
    });
    return chip;
  };
  const hideStrip = el("button", { type: "button", class: "tick-x tick-hide", "aria-label": "Hide the price strip", text: "×" });
  hideStrip.addEventListener("click", () => { const s = tickerState(); s.on = false; saveTicker(s); feedback("look", "price strip", "off"); drawModes(); refreshTicker(); });
  strip.replaceChildren(...shownAssets.map((asset) => reorderable((() => {
    const context = contexts[marketOf(asset)];
    if (!context || !isFinite(context.price)) return el("a", { class: "tick", "data-market": marketOf(asset), title: "Open " + friendlyName(asset) + " chart", href: hlLink(asset), target: "_blank", rel: "noopener noreferrer" },
      [el("b", { class: "name-" + symbolKind(asset), text: friendlyName(asset) }), el("span", { class: "muted", text: "n/a" }), dropper(asset)]);
    const change = context.previous ? context.price / context.previous - 1 : NaN;
    const direction = "";   // no flashing on a price tick
    lastPrices[asset] = context.price;
    return el("a", { class: "tick " + direction + (auto.includes(asset) ? " auto-asset" : ""), href: hlLink(asset), "data-market": marketOf(asset), target: "_blank", rel: "noopener noreferrer", title: "Open " + friendlyName(asset) + " chart" }, [
      el("b", { class: "name-" + (symbolSide(asset) || symbolKind(asset)), text: friendlyName(asset) }),
      el("span", { class: "change " + signClass(change), text: isNum(change) ? pct(change) : "" }),   // the move first, the price after
      el("span", { class: "price", text: priceText(context.price) }), ...(auto.includes(asset) ? [] : [dropper(asset)]), chipActions(asset)]);
  })(), asset)).concat([addBox, hideStrip]));
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


// Price-strip symbols open chart tiles. Explicit page links share a separate window.
document.addEventListener("click", (event) => {
  const link = event.target.closest && event.target.closest("a.symbol-link, a.tick, a.asset-action[href]");
  if (!link || event.metaKey || event.ctrlKey || event.shiftKey || !link.href || event.target.closest("button")) return;
  event.preventDefault();
  if (link.matches(".tick")) { openAsset(link.dataset.market); return; }
  const page = window.open(link.href, "desk-symbol-page", "popup=yes,width=1280,height=860,noopener=no");
  if (page) page.focus(); else window.location.assign(link.href);
});


// Screens: swipe the whole page left or right (touch), press the arrow keys, or tap a dot to move between alternative screens: Now
// (what is happening: highlights, positions, exposure, strategies, candidates, market making, the return chart) and Research (the
// value and return charts and the analytics pulled from the notebooks). The dots sit at the bottom; the choice is remembered.
const SCREENS = ["now", "research"];
// Research keeps the research items in: the candidates (short and market-making), the strategy result, the long charts, and whatever is pulled from the notebooks.
const SCREEN_OF = { now: ["hl-crypto-attention", "hl-stocks-attention", "hl-desk-attention", "t-positions", "t-positions_stocks", "t-exposure", "chart-twr"],
  research: ["t-candidates", "t-mm_candidates", "t-market_making", "t-strategies", "chart-value", "chart-twr"] };
function screensFor(card) {
  const id = cardId(card);
  if (card.dataset.published) return "research";
  if (card.dataset.asset) return "now research";   // an asset opened from either screen must be visible on that screen
  const on = SCREENS.filter((name) => SCREEN_OF[name].includes(id));
  return on.length ? on.join(" ") : "now research";   // a tile that belongs to no screen shows on both
}
function setScreen(name) {
  if (!SCREENS.includes(name)) return;
  document.documentElement.dataset.screen = name;
  try { localStorage.setItem("desk-screen", name); } catch (e) {}
  document.querySelectorAll("#screen-dots button").forEach((dot) => dot.setAttribute("aria-pressed", dot.dataset.screen === name ? "true" : "false"));
  feedback("look", "screen", name);
  // Columns with nothing to show on this screen give their width to the others (Research: a few big charts, not strips).
  document.querySelectorAll("#columns > .col").forEach((col) => {
    col.hidden = false;
    const visible = [...col.children].some((slot) => slot.firstElementChild && getComputedStyle(slot.firstElementChild).display !== "none");
    col.hidden = !visible;
  });
  matchChartHeights();
  if (latest && latest.data) renderCharts(latest.data.charts);   // charts that were hidden have no size until now
  requestAnimationFrame(() => document.querySelectorAll(".js-plotly-plot").forEach((node) => { try { Plotly.Plots.resize(node); } catch (e) {} }));
}
function stepScreen(direction) {
  const at = SCREENS.indexOf(document.documentElement.dataset.screen || "now");
  setScreen(SCREENS[Math.min(SCREENS.length - 1, Math.max(0, at + direction))]);
}
function wireScreens() {
  let saved = "now";
  try { saved = localStorage.getItem("desk-screen") || "now"; } catch (e) {}
  const dots = el("div", { id: "screen-dots", role: "group", "aria-label": "Screens" }, SCREENS.map((name) => {
    const dot = el("button", { type: "button", "data-screen": name, "aria-label": `Screen ${name}`, "aria-pressed": "false" });
    dot.addEventListener("click", () => setScreen(name));
    return dot;
  }));
  document.body.append(dots);
  setScreen(SCREENS.includes(saved) ? saved : "now");
  let startX = 0, startY = 0, tracking = false;
  document.addEventListener("touchstart", (event) => {
    tracking = event.touches.length === 1 && !event.target.closest(".js-plotly-plot, .table-wrap, input, textarea, select, .card-resize, .card-resize-x, #notices, .action-dialog");
    if (tracking) { startX = event.touches[0].clientX; startY = event.touches[0].clientY; }
  }, { passive: true });
  document.addEventListener("touchend", (event) => {
    if (!tracking) return;
    tracking = false;
    const dx = event.changedTouches[0].clientX - startX, dy = event.changedTouches[0].clientY - startY;
    if (Math.abs(dx) > 90 && Math.abs(dy) < 60) stepScreen(dx < 0 ? 1 : -1);   // swipe left: the next screen; right: the one before
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest && event.target.closest("input, textarea, select") || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "ArrowRight") stepScreen(1); else if (event.key === "ArrowLeft") stepScreen(-1);
  });
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

// Nothing in the arrangement may stop the data from loading: each setup step is isolated, then the poll starts.
for (const step of [watchUse, setupSections, wireScreens]) { try { step(); } catch (error) { console.error("dashboard setup:", error); } }
poll();
