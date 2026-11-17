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
  // Derived fields start with _; an aligned table keeps every column, present in its data or not.
  const columns = view.columns.filter(([from]) => view.align || from in source || from.startsWith("_"))
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
  Plotly.newPlot(node, traces, base, CONFIG);
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
    return el("td", { class: classes, text: ZERO_TEXT.test(text) ? "" : text });
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

function renderHighlights(id, rows, hover = {}) {
  const host = document.getElementById(id);
  if (!rows || !rows.length) {
    host.replaceChildren(el("li", {}, el("span", { class: "empty", text: "Nothing to report." })));
    return;
  }
  const rank = { critical: 0, watch: 1, "": 2 };
  const ordered = rows.map(([area, text]) => [area, text, severity(area, text)])
    .sort((a, b) => rank[a[2]] - rank[b[2]]);  // stable: same-level rows keep their order
  host.replaceChildren(...ordered.map(([area, text, level]) => el("li", Object.assign(level ? { class: level } : {},
    hover[area] ? { title: hover[area] } : {}), [
    el("span", { class: "area", text: (level === "critical" ? "⚠ " : "") + area }), el("span", { class: "text", text }),
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

// The wallets card sits under the shorter of the two position tables, so the pair fills the screen evenly.
function balanceColumns() {
  const grid = document.getElementById("positions-grid");
  const wallets = document.getElementById("t-wallets");
  const card = wallets && wallets.closest(".card");
  if (!grid || !card) return;
  const [left, right] = grid.querySelectorAll(":scope > .stack");
  if (!left || !right) return;
  card.remove();
  (right.offsetHeight < left.offsetHeight ? right : left).appendChild(card);
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
  }[kind];
  paths.forEach((d) => { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); svg.append(p); });
  return svg;
}
function setLook(kind, value) {
  const root = document.documentElement;
  root.dataset[kind] = value;
  root.dataset.theme = lookTheme(root.dataset.mode || "dark", root.dataset.style || "indie");
  try { localStorage.setItem("desk-" + kind, value); } catch (e) {}
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
  const next = mode === "dark" ? "light" : "dark";   // the sun shows in light mode, the moon in dark; a click flips it
  const toggle = el("div", { class: "switch", role: "group", "aria-label": "Light or dark" }, [
    el("button", { type: "button", class: "icon-button", title: `Switch to ${next} mode`, "aria-label": `Switch to ${next} mode`,
      "aria-pressed": "false" }, [icon(mode === "light" ? "sun" : "moon")])]);
  toggle.firstChild.addEventListener("click", () => setLook("mode", next));
  const styles = el("div", { class: "switch", role: "group", "aria-label": "Style" });
  STYLE_CHOICES.forEach(([value, name, dot]) => {
    const button = el("button", { type: "button", class: "style-button", title: name, "aria-pressed": value === style ? "true" : "false",
      style: `--dot:${dot}` }, [el("i"), name]);
    button.addEventListener("click", () => setLook("style", value));
    styles.append(button);
  });
  host.replaceChildren(views, toggle, styles);
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
  if (kind !== "view") renderCharts(data.charts);
}
drawModes();

poll();
