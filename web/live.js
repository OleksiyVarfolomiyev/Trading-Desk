/* Live data for the web copy of the desk dashboard.

   Builds, in the visitor's browser, the same document the local dashboard server returns from
   /api/state (desk/dashboard.py), so the dashboard's own app.js renders it unchanged:
   - market data (prices, 24-hour moves, daily candles, funding) straight from HL;
   - each wallet's account data through the desk relay (web/worker.js), which holds the wallet
     addresses and never returns them;
   - token prices and 24-hour changes from DefiLlama.
   IB is not included: it is reachable only through TWS on the desk's own machine. */
(function () {
  "use strict";

  // The relay is served with this page (one Cloudflare Worker), so its paths are relative. A page
  // hosted elsewhere sets window.DESK_CONFIG = {relay: "https://..."} before this script.
  const CFG = Object.assign({ relay: "", wallets: [], refreshSeconds: 60 }, window.DESK_CONFIG || {});
  const HL_INFO = "https://api.hyperliquid.xyz/info";
  const LLAMA = "https://coins.llama.fi";
  const DAY_MS = 86400000, HOUR_MS = 3600000;
  // Sizing rules shared with the executors (desk/risk.py).
  const STOP_ATR_MULTIPLE = 1.25, RESERVE_PCT = 0.20, RISK_PCT_OF_USABLE = 0.05, MAX_TOTAL_RISK_PCT = 0.33;
  const FUNDING_HORIZON_HOURS = 120, FUNDING_PERCENTILE = 0.95, FUNDING_LOOKBACK_DAYS = 20;
  const ATR_PERIOD = 14, CANDLE_DAYS = 200, RISK_DAYS = 90;
  const WINDOWS = { "7D": 7, "30D": 30, "90D": 90, "1Y": 365 };
  const MOVERS = 5, HIGHLIGHT_MOVERS = 3;
  const CONCENTRATION_PCT = 0.25, HIGH_LEVERAGE = 2.0, LOW_AVAILABLE_PCT = 0.10, NEAR_CAP_PCT = 0.80, DEEP_DRAWDOWN = -0.10;
  const CRYPTO_HIP3_DEXES = new Set(["hyna"]);
  const CRYPTO_HIP3_MARKETS = new Set(["cash:BTC", "cash:ETH", "flx:BTC", "flx:USDE", "flx:XMR", "io:PRL",
    "para:ANSEM", "para:BTCD", "para:HYPE2", "para:OTHERS", "para:TOTAL2"]);
  const USD_STABLES = ["USDC", "USDT", "USDE", "DAI", "USDH"];
  const COLORED = new Set(["susd", "vusd", "vpct", "spct", "rusd", "rpct", "side"]);
  // Wallet tokens: DefiLlama price keys; a staking receipt is priced as the token it represents.
  const NATIVE = "0x0000000000000000000000000000000000000000";
  const NATIVE_PRICE = { base: "coingecko:ethereum", ethereum: "coingecko:ethereum" };
  const PRICED_AS = { "base:0x321b7ff75154472b18edb199033ff4d116f340ff": "base:0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf" };
  const STAKED_LABELS = { "base:0x321b7ff75154472b18edb199033ff4d116f340ff": "VVV staked on Venice" };
  const MIN_PRICE_CONFIDENCE = 0.8, DUST_USD = 1.0, MAX_PLAUSIBLE_HOLDING_USD = 1e7;
  const BTC = "BTC", SP500 = "xyz:SP500";
  const STABLE_SYMBOLS = new Set(["USDC", "USDT", "DAI", "USDbC", "USDE", "USDH", "EURC"]);
  // The HL market whose price moves a wallet token: sVVV follows VVV, WETH and cbETH follow ETH.
  const riskCoin = (symbol) => symbol.split(" (")[0].replace(/^s(?=[A-Z]{2})/, "").replace(/^(W|cb)(?=ETH|BTC)/, "");

  // ------------------------------------------------------------------ requests
  async function getJSON(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = "";
      try { detail = (await response.json()).error || ""; } catch (error) { /* not JSON */ }
      throw new Error(`${new URL(url).host} answered HTTP ${response.status}${detail ? ": " + detail : ""}`);
    }
    return response.json();
  }
  const info = (body) => getJSON(HL_INFO, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const relay = (path) => getJSON(CFG.relay.replace(/\/$/, "") + path, { cache: "no-store" });
  // The wallets, by their last four characters, as the relay lists them.
  async function listWallets() {
    if (!CFG.wallets.length) {
      const listed = await relay("/wallets");
      CFG.wallets = (listed.wallets || []).map((id) => ({ id, label: `HL ${id}` }));
      if (!CFG.wallets.length) throw new Error("the relay lists no wallets (is its WALLETS secret set?)");
    }
    return CFG.wallets;
  }

  // Slow-changing data (daily candles, hourly funding) is kept in the browser for an hour.
  function remember(key, ttlMs, load) {
    try {
      const hit = JSON.parse(localStorage.getItem(key) || "null");
      if (hit && Date.now() - hit.t < ttlMs) return Promise.resolve(hit.v);
    } catch (error) { /* storage blocked: read again */ }
    return load().then((value) => {
      try { localStorage.setItem(key, JSON.stringify({ t: Date.now(), v: value })); } catch (error) { /* full or blocked */ }
      return value;
    });
  }
  const memory = {};
  function every(key, ttlMs, load) {
    const hit = memory[key];
    if (hit && Date.now() - hit.t < ttlMs) return hit.p;
    const promise = load().catch((error) => { delete memory[key]; throw error; });
    memory[key] = { t: Date.now(), p: promise };
    return promise;
  }

  // ------------------------------------------------------------------ small helpers
  const sum = (values) => values.reduce((total, value) => total + (Number.isFinite(value) ? value : 0), 0);
  const num = (value) => (value === null || value === undefined || value === "" ? NaN : Number(value));
  const fin = (value) => Number.isFinite(value);
  const iso = (ms) => new Date(ms).toISOString();
  const dayKey = (ms) => new Date(ms).toISOString().slice(0, 10);
  const money = (v) => (fin(v) ? (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "n/a");
  const signedMoney = (v) => (fin(v) ? (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "n/a");
  const percent = (v, signed, digits = 1) => (fin(v) ? (v * 100).toFixed(digits) + "%" : "n/a");
  function quantile(values, q) {
    const sorted = values.filter(fin).slice().sort((a, b) => a - b);
    if (!sorted.length) return NaN;
    const position = (sorted.length - 1) * q, low = Math.floor(position), high = Math.ceil(position);
    return sorted[low] + (sorted[high] - sorted[low]) * (position - low);
  }
  function mean(values) { const v = values.filter(fin); return v.length ? sum(v) / v.length : NaN; }
  function std(values) {
    const v = values.filter(fin); if (v.length < 2) return NaN;
    const m = mean(v); return Math.sqrt(sum(v.map((x) => (x - m) ** 2)) / (v.length - 1));
  }
  function beta(y, x) {
    const pairs = y.map((value, i) => [value, x[i]]).filter(([a, b]) => fin(a) && fin(b));
    if (pairs.length < 8) return NaN;
    const mx = mean(pairs.map((p) => p[1])), my = mean(pairs.map((p) => p[0]));
    const cov = sum(pairs.map(([a, b]) => (a - my) * (b - mx))), varx = sum(pairs.map(([, b]) => (b - mx) ** 2));
    return varx > 0 ? cov / varx : NaN;
  }
  function plain(value) {  // NaN and Infinity become null, as the server's json_safe does
    if (Array.isArray(value)) return value.map(plain);
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, plain(v)]));
    return typeof value === "number" && !Number.isFinite(value) ? null : value;
  }
  const assetClass = (coin) => {
    const dex = coin.includes(":") ? coin.split(":")[0] : "";
    return !dex || CRYPTO_HIP3_DEXES.has(dex) || CRYPTO_HIP3_MARKETS.has(coin) ? "crypto perp" : "tradfi perp";
  };

  // ------------------------------------------------------------------ market data (HL, no address)
  async function marketContexts(dexes) {
    const responses = await Promise.all(dexes.map((dex) => info(dex ? { type: "metaAndAssetCtxs", dex } : { type: "metaAndAssetCtxs" })));
    const out = {};
    responses.forEach(([meta, contexts], i) => (meta.universe || []).forEach((asset, j) => {
      const ctx = contexts[j] || {}, mark = num(ctx.markPx);
      if (!(mark > 0)) return;
      out[asset.name] = { coin: asset.name, dex: dexes[i], mark, oracle: num(ctx.oraclePx) || mark, prevDay: num(ctx.prevDayPx) };
    }));
    return out;
  }

  function spotUsdPrices() {
    return every("spot-prices", 30000, async () => {
      const [meta, mids] = await Promise.all([info({ type: "spotMeta" }), info({ type: "allMids" })]);
      const names = new Map((meta.tokens || []).map((token) => [token.index, token.name]));
      const rates = {};
      for (const market of meta.universe || []) {
        if (!market.tokens || market.tokens.length !== 2) continue;
        const mid = num(mids["@" + market.index]);
        if (!(mid > 0)) continue;
        const base = names.get(market.tokens[0]), quote = names.get(market.tokens[1]);
        if (!base || !quote) continue;
        (rates[base] = rates[base] || []).push([quote, mid]);
        (rates[quote] = rates[quote] || []).push([base, 1 / mid]);
      }
      const usd = Object.fromEntries(USD_STABLES.map((coin) => [coin, 1]));
      for (let changed = true; changed;) {
        changed = false;
        for (const [coin, edges] of Object.entries(rates)) {
          if (coin in usd) continue;
          for (const [quote, rate] of edges) {
            if (quote in usd) { usd[coin] = rate * usd[quote]; changed = true; break; }
          }
        }
      }
      return usd;
    });
  }

  function dailyCandles(coin, days) {
    return remember(`desk:candles:${coin}:${days}`, HOUR_MS, async () => {
      const end = Date.now();
      const rows = await info({ type: "candleSnapshot", req: { coin, interval: "1d", startTime: end - days * DAY_MS, endTime: end } });
      const today = Math.floor(end / DAY_MS) * DAY_MS;  // the forming candle is left out
      return (rows || []).filter((row) => row.t < today).map((row) => ({ t: row.t, h: num(row.h), l: num(row.l), c: num(row.c) }));
    });
  }

  function fundingHistory(coin) {
    return remember(`desk:funding:${coin}`, HOUR_MS, async () => {
      const rows = [];
      let start = Date.now() - FUNDING_LOOKBACK_DAYS * DAY_MS;
      for (let page = 0; page < 5; page++) {
        const batch = await info({ type: "fundingHistory", coin, startTime: start });
        if (!batch || !batch.length) break;
        rows.push(...batch.map((row) => ({ time: row.time, rate: num(row.fundingRate) })));
        if (batch.length < 500) break;
        start = batch[batch.length - 1].time + 1;
      }
      return rows;
    });
  }

  // Wilder's ATR over completed daily candles, as desk/hl/trade_analytics.py.
  function wilderAtr(rows, period = ATR_PERIOD) {
    const ranges = [];
    for (let i = 1; i < rows.length; i++) {
      const previous = rows[i - 1].c, row = rows[i];
      ranges.push(Math.max(row.h - row.l, Math.abs(row.h - previous), Math.abs(row.l - previous)));
    }
    if (!ranges.length) return NaN;
    const n = Math.min(period, ranges.length);
    let atr = sum(ranges.slice(0, n)) / n;
    for (let i = n; i < ranges.length; i++) atr = (atr * (n - 1) + ranges[i]) / n;
    return atr > 0 ? atr : NaN;
  }

  // The 95th-percentile adverse funding over contiguous 5-day windows, per unit, floored at zero.
  function adverseFundingPerUnit(rows, side, oracle) {
    const ordered = rows.filter((row) => fin(row.rate)).sort((a, b) => a.time - b.time);
    const horizon = FUNDING_HORIZON_HOURS, direction = side === "LONG" ? 1 : -1, sums = [];
    let running = 0, contiguous = 0;
    ordered.forEach((row, i) => {
      running += row.rate;
      if (i >= horizon) running -= ordered[i - horizon].rate;
      contiguous = i > 0 && Math.abs(row.time - ordered[i - 1].time - HOUR_MS) <= 2000 ? contiguous + 1 : 0;
      if (i >= horizon - 1 && contiguous >= horizon - 1) sums.push(direction * running);
    });
    if (!sums.length) return NaN;
    return Math.max(0, quantile(sums, FUNDING_PERCENTILE)) * oracle;
  }

  // ------------------------------------------------------------------ accounts
  async function readHL(wallet) {
    const [data, spotUsd] = await Promise.all([relay(`/hl/${wallet.id}/account`), spotUsdPrices()]);
    const unified = data.abstraction === "unifiedAccount";
    let perpEquity = 0, marginUsed = 0;
    const positions = [], dexes = new Set([""]);
    for (const [dex, state] of data.perp || []) {
      perpEquity += num(state.marginSummary.accountValue);
      marginUsed += num(state.marginSummary.totalMarginUsed);
      for (const item of state.assetPositions || []) {
        const p = item.position || {}, quantity = num(p.szi);
        if (!quantity) continue;
        dexes.add(dex);
        positions.push({
          venue: "hl", account: wallet.label, symbol: p.coin, coin: p.coin, dex, asset_class: assetClass(p.coin),
          quantity, avg_price: num(p.entryPx), mark_price: Math.abs(num(p.positionValue)) / Math.abs(quantity),
          unrealized: num(p.unrealizedPnl),
        });
      }
    }
    let spotTotal = 0, usdc = 0;
    const spot = [];
    for (const balance of data.spot || []) {
      const amount = num(balance.total);
      if (!amount) continue;
      if (balance.coin === "USDC") usdc = amount;
      const price = spotUsd[balance.coin];
      if (!(price > 0)) continue;
      spotTotal += amount * price;
      if (!USD_STABLES.includes(balance.coin)) spot.push({ coin: balance.coin, amount, price, value: amount * price });
    }
    const equity = unified ? spotTotal : spotTotal + perpEquity;
    const unrealized = sum(positions.map((p) => p.unrealized));
    return {
      venue: "hl", id: wallet.id, label: wallet.label, equity, margin_used: marginUsed,
      cash: unified ? spotTotal : spotTotal + perpEquity - unrealized,
      available: Math.max(0, equity - marginUsed), unrealized, positions, spot, dexes: [...dexes],
      // Executor risk equity: USDC collateral on a unified account, else perp account value.
      collateral: unified ? usdc : perpEquity,
      fees: data.fees || { maker: 0, taker: 0 },
    };
  }

  const priceKey = (holding) => {
    if (holding.token === NATIVE) return NATIVE_PRICE[holding.chain];
    const own = `${holding.chain}:${holding.token}`;
    return (PRICED_AS[own] || own).toLowerCase();
  };

  async function readWallets(wallets) {
    const replies = await Promise.all(wallets.map((wallet) => relay(`/wallet/${wallet.id}`)
      .then((reply) => ({ wallet, reply }), (error) => ({ wallet, error }))));
    const keys = new Set();
    replies.forEach(({ reply }) => (reply && reply.holdings || []).forEach((holding) => keys.add(priceKey(holding))));
    let prices = {}, changes = {};
    if (keys.size) {
      const list = [...keys].join(",");
      const [priced, moved] = await Promise.all([
        getJSON(`${LLAMA}/prices/current/${list}`),
        getJSON(`${LLAMA}/percentage/${list}?period=24h`).catch(() => ({ coins: {} })),
      ]);
      for (const [key, coin] of Object.entries(priced.coins || {})) {
        if (num(coin.confidence) >= MIN_PRICE_CONFIDENCE && num(coin.price) > 0) prices[key.toLowerCase()] = num(coin.price);
      }
      for (const [key, change] of Object.entries(moved.coins || {})) changes[key.toLowerCase()] = num(change) / 100;
    }
    return replies.map(({ wallet, reply, error }) => {
      const label = `${wallet.label.replace(/^HL /, "")} wallet`;  // its tokens are not HL trading
      if (error) return { venue: "onchain", id: wallet.id, label, error: String(error.message || error) };
      const positions = [], notes = (reply.problems || []).map((problem) => `${label}: ${problem}`);
      let unpriced = 0;
      for (const holding of reply.holdings || []) {
        const key = priceKey(holding), price = prices[key];
        if (!(price > 0)) { unpriced += 1; continue; }
        const value = holding.amount * price;
        if (value > MAX_PLAUSIBLE_HOLDING_USD) continue;
        if (value < DUST_USD && !holding.rewards) continue;
        const own = `${holding.chain}:${holding.token}`;
        const symbol = STAKED_LABELS[own] ? `${holding.symbol} (${STAKED_LABELS[own]}, ${holding.chain})`
          : holding.rewards ? `${holding.symbol} (unclaimed staking rewards, ${holding.chain})`
          : `${holding.symbol} (${holding.chain})`;
        positions.push({ venue: "onchain", account: label, symbol, asset_class: "crypto spot", quantity: holding.amount,
          avg_price: NaN, mark_price: price, unrealized: NaN, key, change24h: changes[key], rewards: !!holding.rewards });
      }
      if (unpriced) notes.push(`${label}: ${unpriced} unpriced token balance(s) ignored (mostly spam airdrops)`);
      const equity = sum(positions.map((p) => p.quantity * p.mark_price));
      return { venue: "onchain", id: wallet.id, label, equity, cash: equity, margin_used: 0, available: equity,
        unrealized: NaN, positions, notes };
    });
  }

  // ------------------------------------------------------------------ history (HL portfolio)
  // PnL at time t, interpolated between a series' points (its first or last point outside them).
  function pnlAt(series, t) {
    const before = series.filter((p) => p.t <= t), after = series.filter((p) => p.t > t);
    if (before.length && after.length) {
      const a = before[before.length - 1], b = after[0];
      return a.pnl + (b.pnl - a.pnl) * (t - a.t) / (b.t - a.t);
    }
    return (before.length ? before[before.length - 1] : after[0]).pnl;
  }

  // One series per wallet on HL's all-time PnL scale: weekly points, then the month window's
  // daily points, then the week window's, then the day window's, each shifted to continue the last.
  function mergedHistory(portfolio) {
    const windows = Object.fromEntries(portfolio || []);
    const points = (name) => {
      const window = windows[name];
      if (!window) return [];
      const pnl = new Map((window.pnlHistory || []).map(([t, v]) => [t, num(v)]));
      return (window.accountValueHistory || []).filter(([t]) => pnl.has(t))
        .map(([t, v]) => ({ t, value: num(v), pnl: pnl.get(t) })).sort((a, b) => a.t - b.t);
    };
    let series = points("allTime");
    for (const name of ["month", "week", "day"]) {
      const finer = points(name);
      if (!finer.length) continue;
      if (!series.length) { series = finer; continue; }
      const start = finer[0].t;
      // A finer window that starts before the coarse series (a young wallet) is aligned where the
      // coarse series starts; otherwise where the finer window starts.
      const offset = series[0].t >= start ? series[0].pnl - pnlAt(finer, series[0].t) : pnlAt(series, start) - finer[0].pnl;
      series = series.filter((p) => p.t < start).concat(finer.map((p) => ({ t: p.t, value: p.value, pnl: p.pnl + offset })));
    }
    // PnL is measured from the wallet's first point, so a wallet joining later enters as a deposit.
    return series.map((p) => ({ t: p.t, value: p.value, pnl: p.pnl - series[0].pnl }));
  }

  // HL keeps only weekly points before its last month. The relay saves each day's last point (KV),
  // so the days before the month window come from there, shifted onto the live series' PnL scale.
  function withSaved(live, portfolio, saved) {
    const month = (Object.fromEntries(portfolio || []).month || {}).pnlHistory || [];
    const points = (saved || []).map(([t, value, pnl]) => ({ t, value: num(value), pnl: num(pnl) }))
      .filter((p) => fin(p.value) && fin(p.pnl)).sort((a, b) => a.t - b.t);
    if (!live.length || !month.length) return live;
    const cutoff = month[0][0];
    const older = points.filter((p) => p.t < cutoff);
    if (!older.length) return live;
    const anchor = older[older.length - 1];
    const offset = pnlAt(live, anchor.t) - anchor.pnl;
    const series = live.filter((p) => p.t < older[0].t)
      .concat(older.map((p) => ({ t: p.t, value: p.value, pnl: p.pnl + offset })), live.filter((p) => p.t >= cutoff));
    return series.map((p) => ({ t: p.t, value: p.value, pnl: p.pnl - series[0].pnl }));
  }

  // ------------------------------------------------------------------ IB (sent by the desk's Mac)
  const IB_STALE_MS = 26 * HOUR_MS;

  function ibAccount(data) {
    const label = data.label, a = data.account || {};
    const positions = (data.positions || []).map((p) => ({
      venue: "ib", account: label, symbol: p.symbol, coin: null, asset_class: p.asset_class,
      quantity: num(p.quantity), avg_price: num(p.avg_price), mark_price: num(p.mark_price),
      multiplier: num(p.multiplier) || 1, unrealized: num(p.unrealized), change24h: num(p.change24h),
    }));
    return { venue: "ib", id: data.id, label, equity: num(a.equity), cash: num(a.cash), margin_used: num(a.margin_used),
      available: num(a.available), unrealized: num(a.unrealized), positions, spot: [], dexes: [], asOf: Date.parse(data.as_of) };
  }

  // Daily NAV (IB's close, about 21:00 UTC) as value and PnL net of deposits, then today's equity.
  function ibHistory(data, account) {
    let pnl = 0, previous = null;
    const series = (data.nav || []).map(([day, nav, flows]) => {
      nav = num(nav);
      if (previous !== null) pnl += nav - previous - num(flows);
      previous = nav;
      return { t: Date.parse(day + "T21:00:00Z"), value: nav, pnl };
    }).filter((p) => fin(p.value) && fin(p.t));
    const last = series[series.length - 1];
    if (last && fin(account.equity) && account.asOf > last.t) {
      series.push({ t: account.asOf, value: account.equity, pnl: last.pnl + account.equity - last.value });
    }
    return series;
  }

  // Growth-of-1 index net of deposits and transfers, as time_weighted_index in trade_analytics.py.
  function twrIndex(histories) {
    const times = [...new Set(histories.flatMap((h) => h.map((p) => p.t)))].sort((a, b) => a - b);
    if (!times.length) return [];
    const cursors = histories.map(() => ({ i: -1 }));
    const combined = times.map((t) => {
      let value = 0, pnl = 0;
      histories.forEach((history, k) => {
        const cursor = cursors[k];
        while (cursor.i + 1 < history.length && history[cursor.i + 1].t <= t) cursor.i += 1;
        if (cursor.i >= 0) { value += history[cursor.i].value; pnl += history[cursor.i].pnl; }
      });
      return { t, value, pnl };
    });
    let level = 1, firstFunded = -1;
    const index = combined.map((point, i) => {
      if (i > 0) {
        const previous = combined[i - 1];
        const pnlChange = point.pnl - previous.pnl;
        const flow = point.value - previous.value - pnlChange;
        const base = previous.value + Math.max(flow, 0);
        if (base > 0) {
          if (firstFunded < 0) firstFunded = i;
          level *= 1 + Math.max(-1, pnlChange / base);
        }
      }
      return { t: point.t, v: level, value: point.value, pnl: point.pnl };
    });
    return firstFunded < 0 ? [] : index.slice(Math.max(firstFunded - 1, 0));
  }

  function windowReturns(index, now) {
    const out = { since_start: index.length ? index[index.length - 1].v / index[0].v - 1 : NaN };
    for (const [label, days] of Object.entries(WINDOWS)) {
      const prior = index.filter((p) => p.t <= now - days * DAY_MS);
      out[label] = prior.length ? index[index.length - 1].v / prior[prior.length - 1].v - 1 : NaN;
    }
    return out;
  }

  function dailyLast(points, field) {
    const days = new Map();
    for (const p of points) days.set(dayKey(p.t), p[field]);
    return days;
  }

  // Modified Dietz since the start, as DeskAccounts.money_weighted.
  function moneyWeighted(histories, start) {
    const series = histories.map((history) => {
      const after = history.filter((p) => p.t >= start);
      if (!after.length) return null;
      const first = after[0].pnl;
      return { value: dailyLast(after, "value"), pnl: new Map([...dailyLast(after, "pnl")].map(([d, v]) => [d, v - first])) };
    }).filter(Boolean);
    if (!series.length) return { pnl: NaN, return: NaN, flows: NaN };
    const days = [...new Set(series.flatMap((s) => [...s.value.keys()]))].sort();
    const last = series.map(() => ({ value: 0, pnl: 0 }));
    const value = [], pnl = [];
    for (const day of days) {
      series.forEach((s, k) => {
        if (s.value.has(day)) last[k].value = s.value.get(day);
        if (s.pnl.has(day)) last[k].pnl = s.pnl.get(day);
      });
      value.push(sum(last.map((x) => x.value)));
      pnl.push(sum(last.map((x) => x.pnl)));
    }
    const span = Math.max(1, (Date.parse(days[days.length - 1]) - Date.parse(days[0])) / DAY_MS);
    let capital = value[0], flows = 0;
    for (let i = 1; i < days.length; i++) {
      const flow = (value[i] - value[i - 1]) - (pnl[i] - pnl[i - 1]);
      flows += flow;
      capital += flow * (Date.parse(days[days.length - 1]) - Date.parse(days[i])) / DAY_MS / span;
    }
    const total = pnl[pnl.length - 1];
    return { pnl: total, return: capital > 0 ? total / capital : NaN, flows };
  }

  // ------------------------------------------------------------------ tables (as desk/dashboard.py)
  function table(rows, columns, formats, options = {}) {
    if (!rows.length) return { columns: [], rows: [], total: null };
    const keys = options.key || {};
    const fmt = (column) => formats[column] || "text";
    const standsOut = (column) => column in keys || COLORED.has(fmt(column));
    const lead = [];
    for (const column of columns) {
      if (standsOut(column) || fmt(column) !== "text") break;
      lead.push(column);
    }
    const moved = columns.filter((column) => standsOut(column) && !lead.includes(column));
    const ordered = lead.concat(moved, columns.filter((column) => !lead.includes(column) && !moved.includes(column)));
    let body = rows, total = options.total || null;
    if (options.totalLast) { total = rows[rows.length - 1]; body = rows.slice(0, -1); }
    return {
      columns: ordered.map((column) => Object.assign({ key: column, fmt: fmt(column) },
        column in keys ? { emphasis: keys[column] || "none" } : {})),
      rows: body, total,
    };
  }

  // ------------------------------------------------------------------ the document
  async function build() {
    const now = Date.now();
    const problems = [], notes = [];
    await listWallets();
    const hlReads = await Promise.all(CFG.wallets.map((wallet) => readHL(wallet).catch((error) => ({ wallet, error }))));
    const trading = [], status = [];
    hlReads.forEach((read, i) => {
      const label = CFG.wallets[i].label;
      if (read.error) {
        problems.push(`${label}: ${read.error.message || read.error}`);
        status.push({ name: label, ok: false, problem: String(read.error.message || read.error) });
      } else {
        trading.push(read);
        status.push({ name: label, ok: true });
      }
    });
    if (!trading.length) throw new Error(problems.join("; ") || "no account could be read");
    const walletReads = await readWallets(CFG.wallets);
    const wallets = walletReads.filter((w) => !w.error);
    walletReads.forEach((w) => {
      status.push(w.error ? { name: w.label, ok: false, problem: w.error } : { name: w.label, ok: true });
      if (w.error) problems.push(`${w.label}: ${w.error}`);
      (w.notes || []).forEach((note) => notes.push(note));
    });
    const ibData = await every("ib", 5 * 60000, () => relay("/ib")).catch(() => null);
    const walletPerf = await every("walletperf", 30 * 60000, () => relay("/walletperf")).catch(() => null);
    const ibs = ibData && ibData.account ? [ibAccount(ibData)] : [];
    ibs.forEach((account) => {
      const age = Date.now() - account.asOf;
      if (!(age < IB_STALE_MS)) {
        const problem = `last sent ${fin(account.asOf) ? dayKey(account.asOf) : "at an unknown time"}; start the local dashboard to refresh it`;
        status.push({ name: account.label, ok: false, problem });
        notes.push(`${account.label}: ${problem}.`);
      } else status.push({ name: account.label, ok: true });
    });
    const traders = trading.concat(ibs);  // trading accounts on every venue (HL and IB)
    const accounts = traders.concat(wallets);

    // Market data for every DEX with a position, plus BTC and the S&P 500 perp for benchmarks.
    const dexes = [...new Set(trading.flatMap((a) => a.dexes).concat(["", "xyz"]))];
    const contexts = await marketContexts(dexes);
    const hlPositions = trading.flatMap((a) => a.positions);
    const others = trading.flatMap((a) => a.spot.map((s) => s.coin))
      .concat(wallets.flatMap((a) => a.positions.map((p) => riskCoin(p.symbol))));
    const coins = [...new Set(hlPositions.map((p) => p.coin).concat(others.filter((coin) => contexts[coin])))];
    const [candles, funding, portfolios, savedPoints] = await Promise.all([
      Promise.all(coins.concat([BTC, SP500]).map((coin) => dailyCandles(coin, CANDLE_DAYS).then((rows) => [coin, rows], () => [coin, []]))),
      Promise.all([...new Set(hlPositions.map((p) => p.coin))].map((coin) => fundingHistory(coin).then((rows) => [coin, rows], () => [coin, []]))),
      Promise.all(trading.map((a) => every(`portfolio:${a.id}`, 5 * 60000, () => relay(`/hl/${a.id}/portfolio`))
        .then((p) => [a.id, p], (error) => { problems.push(`${a.label} history: ${error.message}`); return [a.id, null]; }))),
      Promise.all(trading.map((a) => every(`saved:${a.id}`, HOUR_MS, () => relay(`/hl/${a.id}/history`))
        .then((reply) => [a.id, reply.points || []], () => [a.id, []]))),
    ]);
    const candleOf = Object.fromEntries(candles), fundingOf = Object.fromEntries(funding), portfolioOf = Object.fromEntries(portfolios);
    const savedOf = Object.fromEntries(savedPoints);
    const historyOf = Object.fromEntries(trading.map((a) => [a.id,
      portfolioOf[a.id] ? withSaved(mergedHistory(portfolioOf[a.id]), portfolioOf[a.id], savedOf[a.id]) : []]));
    ibs.forEach((a) => { historyOf[a.id] = ibHistory(ibData, a); });

    // -- totals
    const deskEquity = sum(accounts.map((a) => a.equity));
    const tradingEquity = sum(traders.map((a) => a.equity));
    const walletValue = sum(wallets.map((a) => a.equity));
    const value = (p) => p.quantity * p.mark_price * (p.multiplier || 1);
    const tradePositions = traders.flatMap((a) => a.positions);
    const gross = sum(tradePositions.map((p) => Math.abs(value(p))));
    const net = sum(tradePositions.map(value));
    const unrealized = sum(traders.map((a) => a.unrealized));

    // -- open risk per HL account (executor-consistent)
    const riskNotes = [];
    const book = trading.map((account) => {
      let risk = 0;
      for (const p of account.positions) {
        const side = p.quantity > 0 ? "LONG" : "SHORT";
        const atr = wilderAtr(candleOf[p.coin] || []);
        const context = contexts[p.coin] || { mark: p.mark_price, oracle: p.mark_price };
        let reserve = adverseFundingPerUnit(fundingOf[p.coin] || [], side, context.oracle);
        if (!fin(reserve)) { reserve = 0; riskNotes.push(`${p.coin}: not enough funding history for a reserve`); }
        if (fin(atr)) {
          risk += Math.abs(p.quantity) * (atr * STOP_ATR_MULTIPLE + reserve + p.mark_price * (account.fees.maker + account.fees.taker));
        } else {
          risk += Math.abs(value(p)) * 0.10;
          riskNotes.push(`${p.coin}: no price history, assumed a 10% move`);
        }
      }
      return { venue: "hl", label: account.label, equity: account.collateral, risk, positions: account.positions.length };
    });
    ibs.forEach((account) => {  // IB's open risk as the desk's Mac computed it (desk/risk.py)
      const r = ibData.risk;
      if (r) book.push({ venue: "ib", label: account.label, equity: num(r.equity), risk: num(r.risk), positions: num(r.positions) });
    });
    const riskEquity = sum(book.map((b) => b.equity)), riskUsed = sum(book.map((b) => b.risk));
    const riskCap = riskEquity * MAX_TOTAL_RISK_PCT;

    // -- history, returns and charts
    const histories = traders.map((a) => historyOf[a.id] || []).filter((h) => h.length);
    const index = twrIndex(histories);
    const returns = windowReturns(index, now);
    const start = index.length ? index[0].t : now;
    let peak = index[0] || { v: 1, t: now };
    index.forEach((p) => { if (p.v >= peak.v) peak = p; });
    const drawdown = index.length ? index[index.length - 1].v / peak.v - 1 : NaN;

    // -- 24-hour change, net of flows
    const dayParts = {}, missing = [];
    let dayChange = 0, dayValue = 0;
    trading.forEach((account) => {
      const day = (Object.fromEntries(portfolioOf[account.id] || [])).day;
      const pnl = day && day.pnlHistory || [];
      if (pnl.length >= 2) {
        const change = num(pnl[pnl.length - 1][1]) - num(pnl[0][1]);
        dayParts[account.label] = Math.round(change * 100) / 100;
        dayChange += change; dayValue += account.equity;
      } else missing.push(account.label);
    });
    ibs.concat(wallets).forEach((account) => {
      let change = 0, ok = true;
      for (const p of account.positions) {
        const r = p.change24h;
        if (!fin(r) || r <= -1) { ok = false; break; }
        change += value(p) * r / (1 + r);
      }
      if (ok) { dayParts[account.label] = Math.round(change * 100) / 100; dayChange += change; dayValue += account.equity; }
      else missing.push(account.label);
    });
    const dayBase = dayValue - dayChange;

    // -- positions (HL and wallet tokens)
    const allPositions = accounts.flatMap((a) => a.positions.map((p) => Object.assign({ accountObject: a }, p)));
    const grossAll = sum(allPositions.map((p) => Math.abs(value(p))));
    const positionRows = allPositions.map((p) => {
      const v = value(p), cost = Math.abs(v - p.unrealized);
      const context = contexts[p.coin];
      const change = p.venue === "hl" ? (context && context.prevDay > 0 ? context.mark / context.prevDay - 1 : NaN) : p.change24h;
      return {
        "Asset class": p.asset_class, "Account": p.account, "Symbol": p.symbol, "Side": p.quantity > 0 ? "LONG" : "SHORT",
        "Quantity": p.quantity, "Avg price": p.avg_price, "Mark": p.mark_price, "Value": v, "uPnL": p.unrealized,
        "uPnL (%)": fin(p.avg_price) && cost > 0 ? p.unrealized / cost : NaN, "24h change (%)": change,
        "Weight": grossAll ? Math.abs(v) / grossAll : NaN, "Exposure / desk value": deskEquity ? Math.abs(v) / deskEquity : NaN,
      };
    }).sort((a, b) => a["Asset class"].localeCompare(b["Asset class"]) || Math.abs(b.Value) - Math.abs(a.Value));
    const priced = positionRows.filter((r) => fin(r["uPnL (%)"]));
    const pricedCost = sum(priced.map((r) => Math.abs(r.Value - r["uPnL"])));
    const positionTotal = { "Asset class": "Total", "Value": sum(positionRows.map((r) => r.Value)),
      "uPnL": sum(positionRows.map((r) => r["uPnL"])), "Weight": sum(positionRows.map((r) => r.Weight)),
      "Exposure / desk value": sum(positionRows.map((r) => r["Exposure / desk value"])),
      "uPnL (%)": pricedCost ? sum(priced.map((r) => r["uPnL"])) / pricedCost : NaN,
      "24h change (%)": dayChangeTotal(positionRows) };

    // -- exposure by asset class
    const classes = {};
    positionRows.forEach((r) => {
      const c = classes[r["Asset class"]] = classes[r["Asset class"]] || { Long: 0, Short: 0 };
      if (r.Value > 0) c.Long += r.Value; else c.Short += r.Value;
    });
    const exposureRows = Object.entries(classes).sort(([a], [b]) => a.localeCompare(b)).map(([name, c]) => ({ "Asset class": name, ...c }));
    exposureRows.push({ "Asset class": "Total", Long: sum(exposureRows.map((r) => r.Long)), Short: sum(exposureRows.map((r) => r.Short)) });
    exposureRows.forEach((r) => {
      r.Net = r.Long + r.Short; r.Gross = r.Long - r.Short;
      r["Gross / equity"] = r.Gross / deskEquity; r["Net / equity"] = r.Net / deskEquity;
    });

    // -- movers (trading positions)
    const share = (p) => { const entry = Math.abs(p.quantity * p.avg_price * (p.multiplier || 1)); return entry ? p.unrealized / entry : NaN; };
    const ranked = tradePositions.filter((p) => fin(p.unrealized)).sort((a, b) => a.unrealized - b.unrealized);
    const moverList = [...new Set(ranked.slice(-MOVERS).reverse().concat(ranked.slice(0, MOVERS)))];
    const moverRows = moverList.map((p) => ({ "Account": p.account, "Symbol": p.symbol, "Side": p.quantity > 0 ? "LONG" : "SHORT",
      "Value": value(p), "uPnL": p.unrealized, "% of position": share(p) }));

    // -- accounts in percent
    const accountRows = accounts.map((a) => ({
      "Venue": a.venue, "Account": a.label, "% of desk": deskEquity ? a.equity / deskEquity : NaN,
      "Cash / equity": a.equity > 0 ? a.cash / a.equity : NaN, "Margin used / equity": a.equity > 0 ? a.margin_used / a.equity : NaN,
      "Available / equity": a.equity > 0 ? a.available / a.equity : NaN,
      "uPnL / equity": a.equity > 0 ? a.unrealized / a.equity : NaN,
      "Gross / equity": a.equity > 0 ? sum(a.positions.map((p) => Math.abs(value(p)))) / a.equity : NaN,
      "Net / equity": a.equity > 0 ? sum(a.positions.map(value)) / a.equity : NaN, "Positions": a.positions.length,
    }));
    const accountTotal = { "Venue": "Total", "Account": "", "% of desk": 1, "Positions": sum(accounts.map((a) => a.positions.length)),
      "Cash / equity": sum(accounts.map((a) => a.cash)) / deskEquity, "Margin used / equity": sum(accounts.map((a) => a.margin_used)) / deskEquity,
      "Available / equity": sum(accounts.map((a) => a.available)) / deskEquity,
      "uPnL / equity": sum(accounts.map((a) => a.unrealized)) / deskEquity,
      "Gross / equity": sum(allPositions.map((p) => Math.abs(value(p)))) / deskEquity, "Net / equity": sum(allPositions.map(value)) / deskEquity };

    // -- performance by account
    // 24h return: each account's 24-hour change (the desk-value tile's own figures) over its value a day ago.
    const equityOf = Object.fromEntries(accounts.map((a) => [a.label, a.equity]));
    const dayReturn = (names) => {
      const known = names.filter((n) => n in dayParts && n in equityOf);
      const change = sum(known.map((n) => dayParts[n])), value = sum(known.map((n) => equityOf[n]));
      return known.length && value - change > 0 ? change / (value - change) : NaN;
    };
    const performanceRows = traders.map((a) => {
      const own = (historyOf[a.id] || []).length ? twrIndex([historyOf[a.id]]) : [];
      const r = windowReturns(own, now);
      return { "Account": a.label, "Return 24h": dayReturn([a.label]), "Return 7D": r["7D"], "Return 30D": r["30D"], "Return 90D": r["90D"], "Return 1Y": r["1Y"],
        "Return since inception": r.since_start, "Since": own.length ? dayKey(own[0].t) : "" };
    });
    const performanceCount = performanceRows.length;  // the trading accounts: what "best account" ranks
    // Wallet tokens outside HL: the daily price move of what was held (the Mac computes it; deposits earn nothing).
    const walletSeries = Object.entries((walletPerf && walletPerf.series) || {}).filter(([, points]) => points.length > 1)
      .map(([name, points]) => ({ name, points: points.map(([t, value, pnl, base]) => ({ t, value, pnl, base })) }));
    const walletPerformanceRows = walletSeries.map(({ name, points }) => {
      let level = 1;
      const levels = points.map((p, i) => {
        const gain = p.pnl - (i ? points[i - 1].pnl : 0);
        if (p.base > 0) level *= 1 + Math.max(-1, gain / p.base);
        return { t: p.t, v: level, pnl: p.pnl };
      });
      const row = { "Account": name, "Return 24h": NaN };
      for (const [label, days] of Object.entries(WINDOWS)) {
        const prior = levels.filter((l) => l.t <= now - days * DAY_MS);
        row[`Return ${label}`] = prior.length ? level / prior[prior.length - 1].v - 1 : NaN;
      }
      row["Return since inception"] = level - 1;
      row["Since"] = dayKey(points[0].t - DAY_MS);  // the first day's return starts at the day before's close
      return row;
    });
    walletPerformanceRows.forEach((row) => { row["Return 24h"] = dayReturn([row.Account]); });
    performanceRows.push(...walletPerformanceRows);
    performanceRows.push({ "Account": "Desk (HL + IB)", "Return 24h": dayReturn(traders.map((a) => a.label)), "Return 7D": returns["7D"], "Return 30D": returns["30D"], "Return 90D": returns["90D"],
      "Return 1Y": returns["1Y"], "Return since inception": returns.since_start, "Since": dayKey(start) });
    if (walletSeries.length) {
      const everything = twrIndex(histories.concat(walletSeries.map(({ points }) => points)));
      const all = windowReturns(everything, now);
      performanceRows.push({ "Account": "Desk incl. wallets", "Return 24h": dayReturn(accounts.map((a) => a.label)), "Return 7D": all["7D"], "Return 30D": all["30D"], "Return 90D": all["90D"],
        "Return 1Y": all["1Y"], "Return since inception": all.since_start, "Since": everything.length ? dayKey(everything[0].t) : "" });
    }

    // -- wallet holdings outside HL
    const walletRows = wallets.flatMap((a) => a.positions.map((p) => {
      const [token, detail] = p.symbol.split(" (");
      const inner = (detail || "").replace(/\)$/, "");
      const chain = inner.split(", ").pop(), note = inner.includes(", ") ? inner.split(", ").slice(0, -1).join(", ") : "";
      return { "Wallet": a.label.replace(/\s+wallet$/, ""), "Chain": chain, "Symbol": token, "Value": value(p),
        "% of desk": value(p) / deskEquity, "Amount": p.quantity, "Price": p.mark_price, "Note": note };
    })).sort((a, b) => b.Value - a.Value);

    // -- ex-ante risk: today's holdings moved by each of the last 90 days' prices
    const riskRow = exAnteRisk({ hlPositions, trading, wallets, candleOf, deskEquity, value, ibs, ibCloses: (ibData && ibData.closes) || {} });

    // -- charts
    const valueSeries = traders.map((a) => {
      const history = (historyOf[a.id] || []).filter((p) => p.t >= start);
      const days = dailyLast(history, "value");
      return { name: a.label, x: [...days.keys()], y: [...days.values()].map((v) => Math.round(v * 100) / 100) };
    }).filter((s) => s.x.length);
    const lastDay = valueSeries.reduce((latest, s) => (s.x[s.x.length - 1] > latest ? s.x[s.x.length - 1] : latest), "");
    valueSeries.forEach((s) => {  // carry each layer to the last day, as the local dashboard does
      const known = new Map(s.x.map((d, i) => [d, s.y[i]]));
      const x = [], y = [];
      let carried = null;
      for (let t = Date.parse(s.x[0]); dayKey(t) <= lastDay; t += DAY_MS) {
        const d = dayKey(t);
        if (known.has(d)) carried = known.get(d);
        if (carried !== null) { x.push(d); y.push(carried); }
      }
      s.x = x; s.y = y;
    });
    const indexDays = dailyLast(index, "v");
    const firstLevel = index.length ? index[0].v : 1;
    const twr = [{ name: "Desk (net of deposits)", x: [...indexDays.keys()], y: [...indexDays.values()].map((v) => Math.round(1e5 * v / firstLevel) / 1e3) }];
    const benchmarkDays = Math.ceil((now - start) / DAY_MS) + 2;
    const rebased = (name, rows) => {  // rows: [{t, c}] daily closes
      const inRange = rows.filter((row) => row.t >= Math.floor(start / DAY_MS) * DAY_MS && row.c > 0);
      if (!inRange.length) return null;
      return { name, x: inRange.map((row) => dayKey(row.t)), y: inRange.map((row) => Math.round(1e5 * row.c / inRange[0].c) / 1e3) };
    };
    // SPY's daily closes come from Yahoo Finance through the relay (a browser cannot call Yahoo), so
    // the line covers the whole chart; HL's S&P 500 perp, which starts at its listing, is the fallback.
    const spy = relay("/benchmark/SPY").then((reply) => rebased("SPY", (reply.closes || []).map(([day, close]) =>
      ({ t: Date.parse(day + "T00:00:00Z"), c: close }))), () => null)
      .then((series) => series || dailyCandles(SP500, benchmarkDays).then((rows) => rebased("S&P 500 (HL perp)", rows), () => null));
    const benchmarks = await Promise.all([dailyCandles(BTC, benchmarkDays).then((rows) => rebased("BTC", rows), () => null), spy]);
    benchmarks.filter(Boolean).forEach((series) => twr.push(series));

    // -- highlights (desk/overview.py and desk/desk_accounts.py rules)
    const well = [], attention = [];
    well.push(["Desk value", `${money(deskEquity)}: trading accounts ${money(tradingEquity)}, wallet holdings ${money(walletValue)}; ${tradePositions.length} open positions.`]);
    const returnText = `${percent(returns.since_start, true)} since ${dayKey(start)}; ` +
      Object.keys(WINDOWS).map((label) => `${label} ${percent(returns[label], true)}`).join(", ") + ".";
    ((returns["30D"] || 0) >= 0 ? well : attention).unshift(["Desk return (net of deposits)", returnText]);
    const recent = performanceRows.slice(0, performanceCount).filter((r) => fin(r["Return 30D"]));
    if (recent.length > 1) {
      const best = recent.reduce((a, b) => (b["Return 30D"] > a["Return 30D"] ? b : a));
      const worst = recent.reduce((a, b) => (b["Return 30D"] < a["Return 30D"] ? b : a));
      well.splice(1, 0, ["Best account, 30D", `${best.Account} ${percent(best["Return 30D"], true)}.`]);
      if (worst["Return 30D"] < 0) attention.splice(1, 0, ["Worst account, 30D", `${worst.Account} ${percent(worst["Return 30D"], true)}.`]);
    }
    if (tradePositions.length) {
      (unrealized >= 0 ? well : attention).push(["uPnL", `${signedMoney(unrealized)} on ${money(gross)} of positions (${gross ? percent(unrealized / gross, true) : "n/a"}).`]);
      const mover = (p) => `${p.symbol} ${signedMoney(p.unrealized)}${fin(share(p)) ? ` (${percent(share(p), true)})` : ""}`;
      const winners = ranked.slice().reverse().filter((p) => p.unrealized > 0).slice(0, HIGHLIGHT_MOVERS);
      const losers = ranked.filter((p) => p.unrealized < 0).slice(0, HIGHLIGHT_MOVERS);
      if (winners.length) well.push(["Largest winners", winners.map(mover).join(", ") + "."]);
      if (losers.length) attention.push(["Largest losers", losers.map(mover).join(", ") + "."]);
      const largest = tradePositions.reduce((a, b) => (Math.abs(value(b)) > Math.abs(value(a)) ? b : a));
      const concentration = deskEquity ? Math.abs(value(largest)) / deskEquity : NaN;
      if (concentration > CONCENTRATION_PCT) {
        attention.push(["Concentration", `${largest.symbol} ${largest.quantity > 0 ? "long" : "short"} is ${percent(concentration, false, 0)} of desk value (${money(Math.abs(value(largest)))}, ${largest.account}).`]);
      }
    }
    if (tradingEquity) {
      const leverage = gross / tradingEquity;
      (leverage > HIGH_LEVERAGE ? attention : well).push(["Leverage", `Gross ${leverage.toFixed(2)}x and net ${(net / tradingEquity).toFixed(2)}x trading equity.`]);
      const available = sum(traders.map((a) => a.available));
      if (available / tradingEquity < LOW_AVAILABLE_PCT) {
        attention.push(["Available margin low", `${money(available)} available, ${percent(available / tradingEquity, false, 0)} of trading equity.`]);
      }
    }
    // Each HL wallet keeps 20% of its equity free; past that the executor refuses new trades there.
    trading.forEach((a) => {
      if (!(a.equity > 0)) return;
      const used = a.margin_used / a.equity;
      if (used > 0.80) attention.push(["Margin reserve breached", `${a.label} uses ${percent(used, false, 0)} of its equity as margin (the 20% reserve allows 80%); new trades there are refused.`]);
    });
    if (book.length) {
      const text = `${money(riskUsed)} of ${money(riskCap)} (${percent(riskUsed / riskCap, false, 0)} of the cap); next position budget ${money(riskEquity * (1 - RESERVE_PCT) * RISK_PCT_OF_USABLE)}.`;
      if (riskUsed > riskCap) attention.unshift(["Open risk over cap", text]);
      else if (riskUsed > riskCap * NEAR_CAP_PCT) attention.push(["Open risk near cap", text]);
      else well.push(["Open risk within cap", text]);
    }
    (drawdown <= DEEP_DRAWDOWN ? attention : well).push(["Drawdown now",
      drawdown < 0 ? `${percent(drawdown)} below the peak of ${dayKey(peak.t)}.` : "At its peak."]);
    if (riskRow && fin(riskRow["Volatility (/yr)"])) {
      const betas = [["BTC", riskRow["Beta BTC"]], ["S&P 500", riskRow["Beta S&P 500"]]]
        .filter(([, b]) => fin(b)).map(([name, b]) => `; beta ${name} ${b.toFixed(2)}`).join("");
      well.push(["Risk-adjusted", `Current book: volatility ${percent(riskRow["Volatility (/yr)"], false, 0)}/yr, 1-day VaR 95% ${signedMoney(-riskRow["VaR 95%"])}${betas}.`]);
    }
    if (problems.length) attention.unshift(["Unreadable accounts", problems.join("; ") + " Totals leave them out."]);

    const money2 = { "Equity": "usd", "Cash": "usd", "Margin used": "usd", "Available": "usd", "Gross": "usd", "Value": "usd",
      "Risk equity": "usd", "Open risk": "usd", "Long": "usd", "Short": "usd", "VaR 95%": "usd", "VaR 99%": "usd" };
    const signed = { "uPnL": "susd", "Net": "susd", "Value": "susd" };
    const tables = {
      performance: table(performanceRows, ["Account", "Return 24h", "Return 7D", "Return 30D", "Return 90D", "Return 1Y", "Return since inception", "Since"],
        { "Return 24h": "spct", "Return 7D": "spct", "Return 30D": "spct", "Return 90D": "spct", "Return 1Y": "spct", "Return since inception": "spct" },
        { totalLast: true, key: { "Return 30D": "abs" } }),
      accounts: table(accountRows, ["Venue", "Account", "% of desk", "Cash / equity", "Margin used / equity", "Available / equity",
        "uPnL / equity", "Gross / equity", "Net / equity", "Positions"],
      { "% of desk": "pct", "Cash / equity": "pct", "Margin used / equity": "pct", "Available / equity": "pct",
        "uPnL / equity": "spct", "Gross / equity": "pct", "Net / equity": "spct", "Positions": "int" },
      { total: accountTotal, key: { "% of desk": "max" } }),
      exposure: table(exposureRows, ["Asset class", "Long", "Short", "Net", "Gross", "Gross / equity", "Net / equity"],
        Object.assign({}, money2, { "Net": "susd", "Gross / equity": "pct", "Net / equity": "spct" }), { totalLast: true, key: { "Net / equity": "abs" } }),
      movers: table(moverRows, ["Account", "Symbol", "Side", "Value", "uPnL", "% of position"],
        { "Side": "side", "Value": "susd", "uPnL": "susd", "% of position": "spct" }, { key: { "uPnL": "abs" } }),
      positions: table(positionRows, ["Asset class", "Account", "Symbol", "Side", "Quantity", "Avg price", "Mark", "Value",
        "uPnL", "uPnL (%)", "24h change (%)", "Weight", "Exposure / desk value"],
      Object.assign({ "Side": "side", "Quantity": "num", "Avg price": "price", "Mark": "price" }, signed,
        { "uPnL (%)": "spct", "24h change (%)": "spct", "Weight": "pct", "Exposure / desk value": "pct" }),
      { total: positionTotal, key: { "Weight": "max" } }),
      open_risk: table(book.map((b) => ({ "Venue": b.venue, "Account": b.label, "Positions": b.positions, "Risk equity": b.equity,
        "Open risk": b.risk, "% of desk equity": riskEquity ? b.risk / riskEquity : NaN }))
        .concat([{ "Venue": "Total", "Account": "", "Positions": sum(book.map((b) => b.positions)), "Risk equity": riskEquity,
          "Open risk": riskUsed, "% of desk equity": riskEquity ? riskUsed / riskEquity : NaN }]),
      ["Venue", "Account", "Positions", "Risk equity", "Open risk", "% of desk equity"],
      Object.assign({}, money2, { "Positions": "int", "Open risk": "rusd", "% of desk equity": "rpct" }),
      { totalLast: true, key: { "% of desk equity": "max" } }),
      wallets: table(walletRows, ["Wallet", "Chain", "Symbol", "Value", "% of desk", "Amount", "Price", "Note"],
        { "Amount": "num", "Price": "price", "Value": "vusd", "% of desk": "vpct" },
        { total: { "Wallet": "Total", "Value": sum(walletRows.map((r) => r.Value)), "% of desk": sum(walletRows.map((r) => r["% of desk"])) },
          key: { "Value": "max" } }),
      risk: riskRow ? table([riskRow], Object.keys(riskRow),
        { "Periods": "int", "Coverage": "pct", "Volatility (/yr)": "pct", "Max drawdown": "spct", "VaR 95%": "usd",
          "VaR 99%": "usd", "Beta BTC": "num2", "Beta S&P 500": "num2" }, { key: { "VaR 95%": null, "Beta BTC": null } })
        : { columns: [], rows: [], total: null },
    };

    if (ibs.length) {
      notes.push(`${ibs[0].label} is read through TWS on the desk's Mac, which sends it to the relay every 10 minutes while the local dashboard runs; sent ${ibData.as_of.slice(0, 16).replace("T", " ")} UTC.`);
      notes.push(`${ibs[0].label} stocks are in the current-holdings risk table, priced from Yahoo Finance's daily closes that the desk's Mac sends.`);
    } else {
      notes.push("IB is not included yet: the desk's Mac has not sent it (python -m desk.web_sync --push-ib).");
    }
    notes.push("Wallet tokens on Base (main tokens and Venice staking) and Ethereum are included; Arbitrum and Polygon need an API key and are left out.");
    notes.push("Returns and the value chart use HL's own history for the last month and the relay's saved daily points before it (HL's weekly points where none are saved); IB uses its daily NAV.");
    notes.push("SPY is Yahoo Finance's daily close, read through the relay; S&P 500 beta in the risk table uses HL's S&P 500 perp, which trades every day like the crypto book.");
    notes.push("Current-holdings risk is a historical simulation: today's positions moved by each of the last 90 days' prices.");
    [...new Set(riskNotes)].forEach((note) => notes.push(`Open risk: ${note}`));

    return plain({
      kpis: {
        desk_value: deskEquity,
        day_change: Object.keys(dayParts).length ? { change: dayChange, pct: dayBase > 0 ? dayChange / dayBase : NaN, parts: dayParts, missing } : null,
        trading_equity: tradingEquity, wallet_value: walletValue, unrealized, unrealized_share: gross ? unrealized / gross : NaN,
        gross, net, gross_leverage: tradingEquity ? gross / tradingEquity : NaN, net_leverage: tradingEquity ? net / tradingEquity : NaN,
        positions: tradePositions.length,
        returns: { since_start: returns.since_start, "7D": returns["7D"], "30D": returns["30D"], "90D": returns["90D"] },
        money_weighted: moneyWeighted(histories, start), since: iso(start), drawdown, peak_date: iso(peak.t),
      },
      highlights: { well, attention },
      tables,
      charts: { value: valueSeries, value_with_wallets: null, twr, value_idle: [] },
      problems, notes,
      accounts_status: status,
    });
  }

  // The book's 24-hour move: price PnL over the last day over the gross value a day ago, as
  // day_change_total in desk/overview.py. Positions without a 24-hour change are left out.
  function dayChangeTotal(rows) {
    const known = rows.filter((r) => fin(r.Value) && fin(r["24h change (%)"]) && r["24h change (%)"] > -1);
    const before = known.map((r) => r.Value / (1 + r["24h change (%)"]));
    const base = sum(before.map(Math.abs));
    return base ? sum(known.map((r, i) => r.Value - before[i])) / base : NaN;
  }

  function exAnteRisk({ hlPositions, trading, wallets, candleOf, deskEquity, value, ibs, ibCloses }) {
    // Daily close-to-close returns for each holding; wallet tokens and HL spot use HL's perp of the same coin.
    const holdings = hlPositions.map((p) => ({ coin: p.coin, value: value(p) }));
    trading.forEach((a) => a.spot.forEach((s) => holdings.push({ coin: s.coin, value: s.value })));
    wallets.forEach((a) => a.positions.forEach((p) => {
      const coin = riskCoin(p.symbol);
      holdings.push({ coin, value: value(p), stable: STABLE_SYMBOLS.has(coin) });
    }));
    // IB stocks: the Mac sends their daily closes; a closed day carries a zero return, as in desk/live_risk.py.
    const ibHoldings = ibs.flatMap((a) => a.positions.map((p) => ({ symbol: p.symbol, value: value(p) })));
    const days = [];
    const last = Date.now() - DAY_MS;
    for (let t = Math.floor((last - RISK_DAYS * DAY_MS) / DAY_MS) * DAY_MS; t < Math.floor(last / DAY_MS) * DAY_MS; t += DAY_MS) days.push(t);
    const returnsOf = (coin) => {
      const rows = candleOf[coin];
      if (!rows || rows.length < 2) return null;
      const close = new Map(rows.map((row) => [Math.floor(row.t / DAY_MS) * DAY_MS, row.c]));
      return days.map((t) => (close.has(t) && close.has(t - DAY_MS) ? close.get(t) / close.get(t - DAY_MS) - 1 : NaN));
    };
    const ibReturns = (symbol) => {
      const rows = ibCloses[symbol];
      if (!rows || rows.length < 2) return null;
      const known = rows.map(([day, close]) => [Date.parse(day + "T00:00:00Z"), close]).sort((a, b) => a[0] - b[0]);
      const closeOn = (t) => {  // the last close on or before the day
        let found = NaN;
        for (const [at, close] of known) { if (at <= t) found = close; else break; }
        return found;
      };
      return days.map((t) => { const x = closeOn(t) / closeOn(t - DAY_MS) - 1; return fin(x) ? x : NaN; });
    };
    let covered = 0, total = 0;
    const pnl = days.map(() => 0);
    for (const holding of ibHoldings) {
      total += Math.abs(holding.value);
      const r = ibReturns(holding.symbol);
      if (!r) continue;
      covered += Math.abs(holding.value);
      r.forEach((x, i) => { pnl[i] += fin(x) ? holding.value * x : 0; });
    }
    for (const holding of holdings) {
      total += Math.abs(holding.value);
      if (holding.stable) { covered += Math.abs(holding.value); continue; }
      const r = holding.coin ? returnsOf(holding.coin) : null;
      if (!r) continue;
      covered += Math.abs(holding.value);
      r.forEach((x, i) => { pnl[i] += fin(x) ? holding.value * x : 0; });
    }
    if (!total || !deskEquity) return null;
    const book = pnl.map((x) => x / deskEquity);
    let level = 1, top = 1, worst = 0;
    book.forEach((r) => { level *= 1 + r; top = Math.max(top, level); worst = Math.min(worst, level / top - 1); });
    const btc = returnsOf(BTC), sp = returnsOf(SP500);
    return {
      "Basis": "Current holdings (ex-ante)", "Periods": days.length, "From": dayKey(days[0]), "To": dayKey(days[days.length - 1]),
      "Coverage": covered / total, "Volatility (/yr)": std(book) * Math.sqrt(365),
      "VaR 95%": -quantile(pnl, 0.05), "VaR 99%": -quantile(pnl, 0.01), "Max drawdown": worst,
      "Beta BTC": btc ? beta(book, btc) : NaN, "Beta S&P 500": sp ? beta(book, sp) : NaN,
    };
  }

  // ------------------------------------------------------------------ refresh loop
  let doc = { status: { refreshing: true, interval_s: CFG.refreshSeconds }, data: null };
  let running = null;
  function refresh() {
    if (running) return running;
    const attempt = new Date().toISOString();
    doc = Object.assign({}, doc, { status: Object.assign({}, doc.status, { refreshing: true, last_attempt: attempt }) });
    running = build().then((data) => {
      doc = { data, status: { refreshing: false, last_ok: new Date().toISOString(), last_attempt: attempt, error: null,
        interval_s: CFG.refreshSeconds } };
    }, (error) => {
      console.error(error);
      doc = Object.assign({}, doc, { status: Object.assign({}, doc.status, { refreshing: false, error: String(error.message || error) }) });
    }).finally(() => { running = null; });
    return running;
  }
  window.DESK_SOURCE = async () => Object.assign({}, doc, { server_time: new Date().toISOString() });
  window.DESK_REFRESH = refresh;
  refresh();
  setInterval(refresh, CFG.refreshSeconds * 1000);
})();
