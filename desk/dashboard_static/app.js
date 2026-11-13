"use strict";

const POLL_MS = 5000;
const TABLES = ["performance", "accounts", "risk", "positions", "positions_stocks", "exposure", "open_risk", "wallets", "market_making", "strategies", "candidates", "mm_candidates"];
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

// ---------------------------------------------------------------- tables
// ---------------------------------------------------------------- what the reader sees
// The builders (desk/dashboard.py, web/live.js) send complete tables; each view picks the columns a reader acts on,
// names them for the reader and formats them: [source column, shown name, format]. ``derive`` adds computed
// fields to a row first, ``keep`` drops rows, ``hover`` gives a row its tooltip.
const RISK_CAP = 0.33;  // desk/risk.py MAX_TOTAL_RISK_PCT
const ASSET_NAMES = { "crypto perp": "Crypto", "crypto spot": "Wallet tokens", "tradfi perp": "Stocks & commodities (HL)",
  "equity": "Stocks (IB)", "stock": "Stocks (IB)", "etf": "ETFs (IB)" };
const marketName = (symbol) => String(symbol || "").replace(/^[a-z0-9]+:/, "");  // "xyz:AMD" -> "AMD"
const POSITION_VIEW = {
  derive: (r) => ({ Symbol: marketName(r.Symbol) }),
  columns: [["Account"], ["Symbol"], ["Side"], ["Value"], ["uPnL (%)", "uPnL"], ["24h change (%)", "24h"],
    ["Funding (APR)"], ["Weight"]],
};
const VIEWS = {
  performance: { columns: [["Account"], ["Return 24h", "24h"], ["Return 7D", "7d"], ["Return 30D", "30d"],
    ["Return 90D", "90d"], ["Return 1Y", "1y"], ["Return since inception", "Since start"], ["Since", "Start"]] },
  accounts: {
    keep: (r) => !/wallet$/i.test(String(r.Account || "")),  // margin and leverage mean nothing for a wallet
    columns: [["Account"], ["% of desk"], ["Margin used / equity", "Margin used", "pct"], ["Gross / equity", "Gross", "lev"],
      ["Net / equity", "Net", "slev"], ["uPnL / equity", "uPnL", "spct"]],
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
  open_risk: {
    derive: (r) => ({ _cap: isNum(r["% of desk equity"]) ? r["% of desk equity"] / RISK_CAP : NaN }),
    columns: [["Account"], ["% of desk equity", "Open risk", "rpct"], ["_cap", "Of the 33% cap", "rpct"]],
  },
  risk: {
    derive: (r, context) => ({
      _book: /^current holdings/i.test(String(r.Basis)) ? "Now" : /^current regime/i.test(String(r.Basis)) ? "This regime" : r.Basis,
      _var: isNum(r["VaR 95%"]) && context.deskValue ? r["VaR 95%"] / context.deskValue : NaN,
    }),
    hover: (r) => [r.Basis, isNum(r.Periods) ? `${r.Periods} days, ${r.From || ""} to ${r.To || ""}` : "",
      isNum(r.Coverage) ? `coverage ${pct(r.Coverage)}` : "", isNum(r["VaR 99%"]) ? `VaR 99% ${usd(r["VaR 99%"])}` : "",
      r["Beta BTC 90% range"] ? `beta BTC range ${r["Beta BTC 90% range"]}` : "", r["Not priced"] ? `not priced: ${r["Not priced"]}` : "",
      r.Note || ""].filter(Boolean).join("\n"),
    columns: [["_book", "Book"], ["Volatility (/yr)", "Volatility /yr", "pct"], ["_var", "VaR 95% /day", "rpct"],
      ["Max drawdown", "Max drawdown", "spct"], ["Beta BTC", "Beta BTC", "num2"], ["Sharpe", "Sharpe", "num2"]],
  },
  candidates: { columns: [["Coin"], ["Basis"], ["vs BTC 30d"], ["vs BTC 7d"], ["From high 60d"], ["Funding 7d (APR)"],
    ["OI / volume"], ["Volume 24h"], ["Notes"]] },
  mm_candidates: { columns: [["Coin"], ["Daily range 30d"], ["Longs pay 7d (APR)"], ["Own ladder edge per $1k"], ["Volume 24h"], ["Notes"]] },
  market_making: { derive: (r) => ({ Coin: marketName(r.Coin) }),
    columns: [["Account"], ["Coin"], ["Side"], ["Status"], ["Since"], ["Ladder PnL"], ["Position vs base"], ["Fills"]] },
};

function applyView(id, spec, context) {
  const view = VIEWS[id];
  if (!view || !spec || !spec.columns || !spec.columns.length) return spec;
  const source = Object.fromEntries(spec.columns.map((column) => [column.key, column]));
  const shape = (row) => {
    if (!row) return row;
    const full = Object.assign({}, row, view.derive ? view.derive(row, context) : {});
    const out = {};
    view.columns.forEach(([from, name]) => { out[name || from] = full[from]; });
    if (view.hover) out.__hover = view.hover(row);
    return out;
  };
  const columns = view.columns.filter(([from]) => from in source || from.startsWith("_"))  // derived fields start with _
    .map(([from, name, fmt]) => ({ key: name || from, fmt: fmt || (source[from] || {}).fmt, emphasis: (source[from] || {}).emphasis }));
  const rows = spec.rows.filter((row) => !view.keep || view.keep(row)).map(shape);
  let total = spec.total ? shape(spec.total) : null;
  if (total && columns.length && !total[columns[0].key]) total[columns[0].key] = "Total";
  return Object.assign({}, spec, { columns, rows, total });
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
  // A text column with the same value in every row is said once, above the table; an empty one is left out.
  const shared = [];
  if (spec.rows.length > 1) {
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
    return el("td", { class: classes, text: ZERO_TEXT.test(text) ? "" : text });
  };
  const body = el("tbody", {}, rows.map((row, index) => el("tr", row.__hover ? { title: row.__hover } : {},
    spec.columns.map((column) => cell(row, column, index)))));
  const parts = [el("thead", {}, head), body];
  if (spec.total) {
    parts.push(el("tfoot", {}, el("tr", { class: "total" }, spec.columns.map((column) => cell(spec.total, column)))));
  }
  if (shared.length) host.append(el("p", { class: "table-note", text: shared.join(" · ") }));
  host.append(el("table", {}, parts));
}

// ---------------------------------------------------------------- tiles
function tile(label, value, sub, extra) {  // extra.title: the definition, on hover only
  return el("div", { class: "tile", title: (extra && extra.title) || "" }, [
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
    tile("Desk value", wholeUsd(k.desk_value), "", { line: dayChangeLine(k.day_change) }),
    // The two returns since start, with the dollars under them: money-weighted (what the money made, a loss on a
    // small balance and a gain on a large one net out) and time-weighted (the strategy, deposits aside).
    tile("MW return", pct(mw.return, 1, true) || "n/a",
      [isNum(mw.pnl) ? `PnL ${usd(mw.pnl)}` : "", isNum(k.unrealized) ? `uPnL ${usd(k.unrealized)}` : ""].filter(Boolean).join(" · "),
      { valueClass: signClass(mw.return), title: "Money-weighted return since start, HL + IB; PnL net of deposits and withdrawals." }),
    tile("TW return", pct(r.since_start, 1, true), `since ${sinceLabel(k.since)}`,
      { valueClass: signClass(r.since_start), title: "Time-weighted return since start, wallets included." }),
    tile("Return 30D", pct(r["30D"], 1, true),
      `7D ${pct(r["7D"], 1, true) || "n/a"} · 90D ${pct(r["90D"], 1, true) || "n/a"}`,
      { valueClass: signClass(r["30D"]) }),
    tile("Drawdown now", pct(k.drawdown, 1), k.drawdown < 0 ? `peak ${sinceLabel(k.peak_date)}` : "at peak",
      { valueClass: k.drawdown <= -0.10 ? "neg" : "" }),
    tile("Gross leverage", isNum(k.gross_leverage) ? k.gross_leverage.toFixed(2) + "×" : "n/a",
      isNum(k.net_leverage) ? `net ${k.net_leverage.toFixed(2)}×` : "", { title: "Of trading equity." }),
  ];
  document.getElementById("tiles").replaceChildren(...tiles);
}

function renderAccountStatus(rows, now) {
  const host = document.getElementById("accounts-status");
  host.replaceChildren(...(rows || []).map((row) => el("span", { class: "chip", title: row.problem || "" }, [
    el("span", { class: "icon " + (row.ok ? "good" : "bad"), "aria-hidden": "true", text: row.ok ? "✓" : "!" }),
    el("span", { text: `${row.name}: ${row.ok ? "read" : "unreadable" + (row.last_seen ? ", last read " + ago(row.last_seen, now) : "")}` }),
  ])));
}

function renderHighlights(id, rows) {
  const host = document.getElementById(id);
  if (!rows || !rows.length) {
    host.replaceChildren(el("li", {}, el("span", { class: "empty", text: "Nothing to report." })));
    return;
  }
  host.replaceChildren(...rows.map(([area, text]) => el("li", {}, [
    el("span", { class: "area", text: area }), el("span", { class: "text", text }),
  ])));
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
    font: { family: "system-ui, -apple-system, Segoe UI, sans-serif", size: 12, color: ink },
    margin: { l: 56, r: 72, t: 8, b: 36 },
    hovermode: "x unified",
    hoverlabel: { bgcolor: token("--surface"), bordercolor: axis, font: { color: token("--ink") } },
    legend: { orientation: "h", x: 0, y: 1.08, font: { color: ink } },
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
    hovertemplate: "%{fullData.name}: $%{y:,.2f}<extra></extra>",
  }));
  // Not stacked: it covers only the days wallet values are known (recorded, or rebuilt before
  // recording began), never implying zero outside them.
  const withWallets = charts.value_with_wallets;
  if (withWallets && withWallets.x.length) {
    const note = withWallets.note ? `<br><span style="font-size:11px">${withWallets.note}</span>` : "";
    value.push({ type: "scatter", mode: "lines", name: withWallets.name, x: withWallets.x, y: withWallets.y,
      line: { width: 2, dash: "dot", color: token("--ink-2") },
      hovertemplate: `%{fullData.name}: $%{y:,.2f}${note}<extra></extra>` });
  }
  // Collapse stretches with no trading on any account, as the HL account charts do, and say so.
  const idle = charts.value_idle || [];
  const month = (d) => new Date(d + "T00:00:00Z").toLocaleString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
  const valueLayout = baseLayout({ yaxis: Object.assign(baseLayout().yaxis, { tickformat: "$,.1f" }) });
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
  const money = (v) => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let stackTop = 0;
  const valueEnds = value.filter((trace) => trace.y && trace.y.length).map((trace) => {
    const own = trace.y[trace.y.length - 1];
    const y = trace.stackgroup ? (stackTop += own) : own;
    return { x: trace.x[trace.x.length - 1], y, xanchor: "left", xshift: 6, showarrow: false, text: money(own),
             font: { color: trace.line.color, size: 11 } };
  });
  valueLayout.annotations = (valueLayout.annotations || []).concat(valueEnds);
  valueLayout.margin = Object.assign({}, valueLayout.margin, { r: 88 });
  Plotly.react("chart-value", value, valueLayout, CONFIG).then(() => spreadEndLabels("chart-value"));

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
  Plotly.react("chart-twr", twr, twrLayout, CONFIG).then(() => spreadEndLabels("chart-twr"));
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
  renderHighlights("hl-well", data.highlights && data.highlights.well);
  renderHighlights("hl-attention", data.highlights && data.highlights.attention);
  const context = { deskValue: (data.kpis || {}).desk_value };
  for (const id of TABLES) renderTable(id, applyView(id, data.tables && data.tables[id], context));
  renderCharts(data.charts);
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

poll();
