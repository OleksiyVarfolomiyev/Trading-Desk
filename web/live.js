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

  // The relay is served with this page (web/server.mjs on the desk server), so its paths are relative. A page
  // hosted elsewhere sets window.DESK_CONFIG = {relay: "https://..."} before this script.
  const CFG = Object.assign({ relay: "", wallets: [], refreshSeconds: 60 }, window.DESK_CONFIG || {});
  const HL_INFO = "https://api.hyperliquid.xyz/info";
  const LLAMA = "https://coins.llama.fi";
  const DAY_MS = 86400000, HOUR_MS = 3600000;
  // Sizing rules shared with the executors (desk/risk.py).
  const STOP_ATR_MULTIPLE = 3.0, RESERVE_PCT = 0.20, RISK_PCT_OF_USABLE = 0.05, MAX_TOTAL_RISK_PCT = 0.33;
  const FUNDING_HORIZON_HOURS = 120, FUNDING_PERCENTILE = 0.95, FUNDING_LOOKBACK_DAYS = 20;
  const ATR_PERIOD = 14, CANDLE_DAYS = 200, RISK_DAYS = 90;
  const WINDOWS = { "7D": 7, "30D": 30, "90D": 90, "1Y": 365 };
  const MOVERS = 5, HIGHLIGHT_MOVERS = 3;
  const CONCENTRATION_PCT = 0.25, HIGH_LEVERAGE = 2.0, LOW_AVAILABLE_PCT = 0.10, NEAR_CAP_PCT = 0.80, DEEP_DRAWDOWN = -0.10;
  // desk/funding_watch.py: a position paying more than 0.1% of its value a day, and at least $0.25 a day.
  const WALLET_MIN_SHARE = 0.05;  // wallet tokens shown from this share of the desk up (desk/overview.py)
  const ADVERSE_FUNDING_APR = 0.365, ADVERSE_FUNDING_MIN_DAILY = 0.25, FUNDING_SUSTAINED_HOURS = 6;
  const CRYPTO_HIP3_DEXES = new Set(["hyna"]);
  const CRYPTO_HIP3_MARKETS = new Set(["cash:BTC", "cash:ETH", "flx:BTC", "flx:USDE", "flx:XMR", "io:PRL",
    "para:ANSEM", "para:BTCD", "para:HYPE2", "para:OTHERS", "para:TOTAL2"]);
  const USD_STABLES = ["USDC", "USDT", "USDE", "DAI", "USDH"];
  const COLORED = new Set(["susd", "vusd", "vpct", "spct", "sbps", "rusd", "rpct", "side"]);
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
  // A start date for reading, as desk/numbers.py ``since``: "May'25" once older than 60 days, else "Nov 3".
  function sinceLabel(when) {
    const t = typeof when === "number" ? when : Date.parse(when);
    if (!isFinite(t)) return "";
    const d = new Date(t), month = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
    return Date.now() - t > 60 * 86400000 ? `${month}'${String(d.getUTCFullYear()).slice(2)}` : `${month} ${d.getUTCDate()}`;
  }
  // Dollars rounded for reading, as desk/numbers.py: $0.42, $548, $2.11K, $12.3K, $2.53M.
  const money = (v) => {
    if (!fin(v)) return "n/a";
    const sign = v < 0 ? "-$" : "$", size = Math.abs(v), rounded = Number(size.toPrecision(3));
    for (const [scale, unit] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
      if (rounded >= scale) return sign + Number((rounded / scale).toPrecision(3)) + unit;
    }
    return sign + (size >= 10 ? Math.round(size).toLocaleString("en-US") : size.toFixed(2));
  };
  const signedMoney = money;
  const readablePct = (value) => {  // as desk/numbers.py pct: 100%, 57%, 5.3%, 0.42%; trailing zeros dropped
    const number = value * 100, size = Math.abs(number);
    let text = size.toFixed(size >= 9.95 ? 0 : size >= 0.995 ? 1 : 2);
    if (text.includes(".")) text = text.replace(/0+$/, "").replace(/\.$/, "");
    return text === "0" ? "0%" : (number < 0 ? "-" : "") + text + "%";
  };
  const percent = (v, signed, digits = 1) => (fin(v) ? readablePct(v) : "n/a");
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
      out[asset.name] = { coin: asset.name, dex: dexes[i], mark, oracle: num(ctx.oraclePx) || mark, prevDay: num(ctx.prevDayPx), funding: num(ctx.funding) };
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
      stops: data.stops || {},  // coin -> resting reduce-only stop prices
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
        if (value < DUST_USD && !holding.rewards && !holding.cooldown) continue;
        const own = `${holding.chain}:${holding.token}`;
        // Unstaking (Venice's cooldown, desk/venues/onchain.py): still the wallet's, valued as the token.
        const unstaking = holding.cooldown ? (holding.unlocks && holding.unlocks > Date.now()
          ? `unstaking, liquid ${eastern(holding.unlocks)}` : "unstaking, claimable now") : "";
        const symbol = STAKED_LABELS[own] ? `${holding.symbol} (${STAKED_LABELS[own]}, ${holding.chain})`
          : holding.rewards ? `${holding.symbol} (unclaimed staking rewards, ${holding.chain})`
          : unstaking ? `${holding.symbol} (${unstaking}, ${holding.chain})`
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
  // Scenario stress (desk/stress.py): what a correlated shock would cost the whole desk, no stop assumed to help.
  const STRESS_WARN = 0.25, STRESS_BLOCK = 0.35;
  const STRESS = { "alt rally": { alt: 0.25, major: 0.10 }, "equity sell-off": { equity: -0.10 } };
  const STRESS_MAJORS = new Set(["BTC", "ETH", "WBTC", "CBBTC", "WETH", "CBETH", "STETH", "WSTETH"]);
  const STRESS_STABLES = new Set(["USDC", "USDT", "USDE", "DAI", "USDH", "USDBC", "EURC", "GHO", "PYUSD", "FDUSD", "USDS", "SUSDE"]);
  const STRESS_COMMODITIES = new Set(["GOLD", "SILVER", "BRENTOIL", "WTIOIL", "OIL", "CL", "COPPER", "NATGAS", "PLATINUM", "PALLADIUM", "URANIUM", "PAXG", "XAUT"]);
  const stressKind = (assetClass, symbol) => {
    const base = String(symbol).split(":").pop().trim().split(/[ (]/)[0].toUpperCase();
    if (assetClass === "crypto perp" || assetClass === "crypto spot") return STRESS_STABLES.has(base) ? "stable" : STRESS_MAJORS.has(base) ? "major" : "alt";
    if (assetClass === "equity") return "equity";
    if (assetClass === "tradfi perp" || String(symbol).includes(":")) return STRESS_COMMODITIES.has(base) ? "other" : "equity";
    return "other";
  };
  // The desk's clock (Eastern time, shown without a zone name), as desk/numbers.py ``clock``: "10 am", "10:35 am"
  // today, "Nov 12, 9 am" this year, "May 21 '25, 10:05 am" before.
  const eastern = (stamp, utcText) => {
    const date = utcText ? new Date(String(stamp).slice(0, 19) + "Z") : new Date(stamp);
    if (Number.isNaN(date.getTime())) return "?";
    const parts = (d) => Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "2-digit",
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true }).formatToParts(d).map((p) => [p.type, p.value]));
    const at = parts(date), now = parts(new Date());
    const time = `${at.hour}${at.minute === "00" ? "" : ":" + at.minute} ${at.dayPeriod.toLowerCase()}`;
    if (at.year === now.year && at.month === now.month && at.day === now.day) return time;
    return `${at.month} ${at.day}${at.year === now.year ? "" : " '" + at.year}, ${time}`;
  };

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

  // A window's start may be up to WINDOW_EDGE_MS past its cut. HL's month history begins exactly 30 days
  // before its latest sample, so a young account's first point (c248, funded 10-21) sits on the 30D cut:
  // read a few seconds early, before the cached history aged past it, the 30D cell was blank until a refresh.
  const WINDOW_EDGE_MS = DAY_MS;
  function windowReturns(index, now) {
    const out = { since_start: index.length ? index[index.length - 1].v / index[0].v - 1 : NaN };
    for (const [label, days] of Object.entries(WINDOWS)) {
      const cut = now - days * DAY_MS;
      let prior = index.filter((p) => p.t <= cut);
      if (!prior.length && index.length && index[0].t <= cut + WINDOW_EDGE_MS) prior = [index[0]];
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
    const regime = await every("regime", 30 * 60000, () => relay("/regime")).catch(() => null);
    const daemonAlerts = await every("alerts", 60000, () => relay("/alerts")).catch(() => null);
    const syncRun = await every("sync", 30 * 60000, () => relay("/sync")).catch(() => null);
    const research = await every("research", 15 * 60000, () => relay("/research")).catch(() => null);
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
        // A hedge counts nothing toward the cap (HEDGED_POSITIONS in desk/risk.py): the 07f2 VVV short offsets the staked sVVV.
        if (String(account.id).slice(-4).toLowerCase() === "07f2" && p.coin === "VVV") continue;
        const side = p.quantity > 0 ? "LONG" : "SHORT";
        const atr = wilderAtr(candleOf[p.coin] || []);
        const context = contexts[p.coin] || { mark: p.mark_price, oracle: p.mark_price };
        let reserve = adverseFundingPerUnit(fundingOf[p.coin] || [], side, context.oracle);
        if (!fin(reserve)) { reserve = 0; riskNotes.push(`${p.coin}: not enough funding history for a reserve`); }
        if (fin(atr)) {
          // To the position's own resting stop when it has one (the nearest), else the executor's ATR stop.
          const own = (account.stops || {})[p.coin] || [];
          const stop = own.length ? (p.quantity < 0 ? Math.min(...own) : Math.max(...own)) : NaN;
          const priceRisk = fin(stop) ? Math.max(0, p.quantity < 0 ? stop - p.mark_price : p.mark_price - stop) : atr * STOP_ATR_MULTIPLE;
          risk += Math.abs(p.quantity) * (priceRisk + reserve + p.mark_price * (account.fees.maker + account.fees.taker));
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
        // What the position earns (positive) or pays (negative) in funding, annualised; HL perps only.
        "Funding (APR)": p.venue === "hl" && context && fin(context.funding) ? (p.quantity > 0 ? -1 : 1) * context.funding * 24 * 365 : NaN,
        "Weight": grossAll ? Math.abs(v) / grossAll : NaN, "Exposure / desk value": deskEquity ? Math.abs(v) / deskEquity : NaN,
      };
    }).sort((a, b) => a["Asset class"].localeCompare(b["Asset class"]) || Math.abs(b.Value) - Math.abs(a.Value));
    // One table per kind: crypto perps, then stocks and tradfi (IB and HL equity perps). Wallet tokens
    // (crypto spot) have their own table, so weights are re-based on the trading positions.
    const isCrypto = (r) => r["Asset class"] === "crypto perp";
    const tableRows = positionRows.filter((r) => r["Asset class"] !== "crypto spot");
    const tableGross = sum(tableRows.map((r) => Math.abs(r.Value)));
    tableRows.forEach((r) => { r.Weight = tableGross ? Math.abs(r.Value) / tableGross : NaN; });
    const positionTotal = (rows) => {
      const priced = rows.filter((r) => fin(r["uPnL (%)"]));
      const pricedCost = sum(priced.map((r) => Math.abs(r.Value - r["uPnL"])));
      return { "Asset class": "Total", "Value": sum(rows.map((r) => r.Value)),
        "uPnL": sum(rows.map((r) => r["uPnL"])), "Weight": sum(rows.map((r) => r.Weight)),
        "Exposure / desk value": sum(rows.map((r) => r["Exposure / desk value"])),
        "uPnL (%)": pricedCost ? sum(priced.map((r) => r["uPnL"])) / pricedCost : NaN,
        "24h change (%)": dayChangeTotal(rows), "Funding (APR)": fundingTotal(rows) };
    };
    const fundingTotal = (rows) => {  // annualised funding over the gross value of the positions that have a rate
      const known = rows.filter((r) => fin(r["Funding (APR)"]));
      const base = sum(known.map((r) => Math.abs(r.Value)));
      return base ? sum(known.map((r) => Math.abs(r.Value) * r["Funding (APR)"])) / base : NaN;
    };

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
    // Crypto and stocks are ranked apart (stocks: IB and HL equity perps).
    const marketKind = (p) => (String(p.asset_class || "").startsWith("crypto") ? "Crypto" : "Stocks");
    const KINDS = ["Crypto", "Stocks"];
    const moverRows = (kind) => {  // crypto and stocks are two tables: never ranked together
      const group = ranked.filter((p) => marketKind(p) === kind);
      return [...new Set(group.slice(-MOVERS).reverse().concat(group.slice(0, MOVERS)))].map((p) => ({
        "Account": p.account, "Symbol": p.symbol, "Side": p.quantity > 0 ? "LONG" : "SHORT",
        "Value": value(p), "uPnL": p.unrealized, "% of position": share(p) }));
    };

    // -- accounts in percent
    const accountRows = accounts.map((a) => ({
      "Account": a.label, "% of desk": deskEquity ? a.equity / deskEquity : NaN,
      "Cash / equity": a.equity > 0 ? a.cash / a.equity : NaN, "Margin used / equity": a.equity > 0 ? a.margin_used / a.equity : NaN,
      "Available / equity": a.equity > 0 ? a.available / a.equity : NaN,
      "uPnL / equity": a.equity > 0 ? a.unrealized / a.equity : NaN,
      "Gross / equity": a.equity > 0 ? sum(a.positions.map((p) => Math.abs(value(p)))) / a.equity : NaN,
      "Net / equity": a.equity > 0 ? sum(a.positions.map(value)) / a.equity : NaN, "Positions": a.positions.length,
    }));
    const accountTotal = { "Account": "Total", "% of desk": 1, "Positions": sum(accounts.map((a) => a.positions.length)),
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
        "Return since inception": r.since_start, "Since": own.length ? sinceLabel(own[0].t) : "" };
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
      row["Since"] = sinceLabel(points[0].t - DAY_MS);  // the first day's return starts at the day before's close
      return row;
    });
    walletPerformanceRows.forEach((row) => { row["Return 24h"] = dayReturn([row.Account]); });
    // A wallet worth less than WALLET_MIN_SHARE of the desk is dust: no row (the bold total still includes it).
    const walletLastValue = (name) => { const s = walletSeries.find((w) => w.name === name); return s ? num(s.points[s.points.length - 1].value) : NaN; };
    performanceRows.push(...walletPerformanceRows.filter((row) => walletLastValue(row.Account) / deskEquity >= WALLET_MIN_SHARE));
    performanceRows.push({ "Account": "Desk (HL + IB)", "Return 24h": dayReturn(traders.map((a) => a.label)), "Return 7D": returns["7D"], "Return 30D": returns["30D"], "Return 90D": returns["90D"],
      "Return 1Y": returns["1Y"], "Return since inception": returns.since_start, "Since": sinceLabel(start) });
    // The headline return (highlights and tiles) is the whole desk's, wallets included: the table's bold last
    // row. Without wallet history it falls back to the trading accounts (HL + IB).
    let headline = { returns, start, drawdown, peak };
    if (walletSeries.length) {
      const everything = twrIndex(histories.concat(walletSeries.map(({ points }) => points)));
      const all = windowReturns(everything, now);
      if (everything.length) {
        let top = everything[0];
        everything.forEach((p) => { if (p.v >= top.v) top = p; });
        headline = { returns: all, start: everything[0].t, drawdown: everything[everything.length - 1].v / top.v - 1, peak: top, series: everything };
      }
      performanceRows.push({ "Account": "Desk incl. wallets", "Return 24h": dayReturn(accounts.map((a) => a.label)), "Return 7D": all["7D"], "Return 30D": all["30D"], "Return 90D": all["90D"],
        "Return 1Y": all["1Y"], "Return since inception": all.since_start, "Since": everything.length ? sinceLabel(everything[0].t) : "" });
    }

    // -- wallet holdings outside HL
    const walletRows = wallets.flatMap((a) => a.positions.map((p) => {
      const [token, detail] = p.symbol.split(" (");
      const inner = (detail || "").replace(/\)$/, "");
      const chain = inner.split(", ").pop(), note = inner.includes(", ") ? inner.split(", ").slice(0, -1).join(", ") : "";
      return { "Wallet": a.label.replace(/\s+wallet$/, ""), "Chain": chain, "Symbol": token, "Value": value(p),
        "% of desk": value(p) / deskEquity, "Amount": p.quantity, "Price": p.mark_price, "Note": note };
    })).filter((r) => r["% of desk"] >= WALLET_MIN_SHARE)  // smaller holdings are dust for decisions (desk/overview.py)
      .sort((a, b) => b.Value - a.Value);

    // -- ex-ante risk: today's holdings moved by each of the last 90 days' prices
    const riskRow = exAnteRisk({ hlPositions, trading, wallets, candleOf, deskEquity, value, ibs, ibCloses: (ibData && ibData.closes) || {} });

    // -- charts
    const valueSeries = traders.map((a) => {
      const history = (historyOf[a.id] || []).filter((p) => p.t >= start);
      const days = dailyLast(history, "value");
      return { name: a.label, x: [...days.keys()], y: [...days.values()].map((v) => Math.round(v * 100) / 100) };
    }).filter((s) => s.x.length);
    // Each layer ends today at the account's live value (as the accounts table), so the chart's end labels are
    // current values rather than the last saved day (IB's NAV is yesterday's close).
    const today = dayKey(now);
    const lastDay = valueSeries.reduce((latest, s) => (s.x[s.x.length - 1] > latest ? s.x[s.x.length - 1] : latest), today);
    valueSeries.forEach((s) => {  // carry each layer to the last day, as the local dashboard does
      const known = new Map(s.x.map((d, i) => [d, s.y[i]]));
      const x = [], y = [];
      let carried = null;
      for (let t = Date.parse(s.x[0]); dayKey(t) <= lastDay; t += DAY_MS) {
        const d = dayKey(t);
        if (known.has(d)) carried = known.get(d);
        if (carried !== null) { x.push(d); y.push(carried); }
      }
      const live = traders.find((a) => a.label === s.name);
      if (live && fin(live.equity) && y.length) y[y.length - 1] = Math.round(live.equity * 100) / 100;
      s.x = x; s.y = y;
    });
    // The dashed "incl. wallets" line, as on the local dashboard: the stacked accounts plus the wallets' tokens
    // on other chains, only on days the wallet values are known (relay /walletperf), never a stacked layer
    // (a layer reads as $0 where it has no data). Today's point is the live desk value.
    const valueWithWallets = () => {
      const walletDays = new Map();
      walletSeries.forEach(({ points }) => {
        const days = dailyLast(points, "value");
        let carried = null;
        const first = [...days.keys()][0];
        if (!first) return;
        for (let t = Date.parse(first); dayKey(t) <= today; t += DAY_MS) {  // carry each wallet forward
          const d = dayKey(t);
          if (days.has(d) && fin(num(days.get(d)))) carried = num(days.get(d));
          if (carried !== null) walletDays.set(d, (walletDays.get(d) || 0) + carried);
        }
      });
      if (walletDays.size < 2) return null;
      const stackedOn = (d) => sum(valueSeries.map((s) => {
        let last = NaN;
        for (let i = 0; i < s.x.length && s.x[i] <= d; i += 1) last = s.y[i];
        return last;
      }));
      const x = [...walletDays.keys()].filter((d) => d >= valueSeries.reduce((a, s) => (s.x[0] < a ? s.x[0] : a), today)).sort();
      const y = x.map((d) => Math.round((stackedOn(d) + walletDays.get(d)) * 100) / 100);
      if (x.length && x[x.length - 1] === today && fin(deskEquity)) y[y.length - 1] = Math.round(deskEquity * 100) / 100;
      return x.length > 1 ? { name: "Desk incl. wallets", note: "", x, y } : null;
    };
    // The return chart is the tiles' series (``headline``: wallets included when they have history), as % since its
    // start, so the desk line ends at the "TW return" tile; the benchmarks start the same day.
    const chartIndex = headline.series || index, chartStart = headline.start;
    const indexDays = dailyLast(chartIndex, "v");
    const firstLevel = chartIndex.length ? chartIndex[0].v : 1;
    const twr = [{ name: headline.series ? "Desk incl. wallets" : "Desk (HL + IB)", x: [...indexDays.keys()],
      y: [...indexDays.values()].map((v) => Math.round(1e4 * (v / firstLevel - 1)) / 1e2) }];
    const benchmarkDays = Math.ceil((now - chartStart) / DAY_MS) + 2;
    const rebased = (name, rows) => {  // rows: [{t, c}] daily closes
      const inRange = rows.filter((row) => row.t >= Math.floor(chartStart / DAY_MS) * DAY_MS && row.c > 0);
      if (!inRange.length) return null;
      return { name, x: inRange.map((row) => dayKey(row.t)), y: inRange.map((row) => Math.round(1e4 * (row.c / inRange[0].c - 1)) / 1e2) };
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
    let fundingNow = [];  // positions paying adverse funding at the current rate, for the one "Funding" row
    // The desk return is in the TW return and Return 30D tiles and the performance table's bold row: no highlight.
    if (tradePositions.length) {
      // uPnL by venue, each over its own position value: gains under what stands out, losses under needs
      // attention; the desk total joins the side its sign falls on.
      const ratio = (group) => {
        const notional = sum(group.map((p) => Math.abs(value(p))));
        return notional ? sum(group.map((p) => p.unrealized)) / notional : NaN;
      };
      const venues = [["IB", tradePositions.filter((p) => p.venue === "ib")], ["HL", tradePositions.filter((p) => p.venue !== "ib")]]
        .filter(([, group]) => group.length).map(([name, group]) => [name, ratio(group)]).filter(([, r]) => fin(r));
      const total = gross ? unrealized / gross : NaN;
      [[well, (r) => r >= 0], [attention, (r) => r < 0]].forEach(([list, side]) => {
        const parts = venues.filter(([, r]) => side(r)).map(([name, r]) => `${name} ${percent(r, true)}`);
        if (fin(total) && side(total)) parts.unshift(`all positions ${percent(total, true)}`);  // not the desk value: open positions only
        if (parts.length) list.push(["uPnL", parts.join(", ") + "."]);
      });
      const mover = (p) => `${p.symbol} ${fin(share(p)) ? percent(share(p), true) : "n/a"}`;
      const winnerParts = [], loserParts = [];
      KINDS.forEach((kind) => {  // crypto and stocks ranked apart, shown in one row per side
        const group = ranked.filter((p) => marketKind(p) === kind);
        const winners = group.slice().reverse().filter((p) => p.unrealized > 0).slice(0, HIGHLIGHT_MOVERS);
        const losers = group.filter((p) => p.unrealized < 0).slice(0, HIGHLIGHT_MOVERS);
        if (winners.length) winnerParts.push(`${kind.toLowerCase()}: ${winners.map(mover).join(", ")}`);
        if (losers.length) loserParts.push(`${kind.toLowerCase()}: ${losers.map(mover).join(", ")}`);
      });
      if (winnerParts.length) well.push(["Largest winners", winnerParts.join("; ") + "."]);
      if (loserParts.length) attention.push(["Largest losers", loserParts.join("; ") + "."]);
      const largest = tradePositions.reduce((a, b) => (Math.abs(value(b)) > Math.abs(value(a)) ? b : a));
      const concentration = deskEquity ? Math.abs(value(largest)) / deskEquity : NaN;
      if (concentration > CONCENTRATION_PCT) {
        attention.push(["Concentration", `${largest.symbol} ${largest.quantity > 0 ? "long" : "short"} (${largest.account}) ${percent(concentration, false, 0)} of desk value.`]);
      }
      // Positions paying funding fast enough to matter (desk/funding_watch.py).
      const dailyCost = (r) => -r["Funding (APR)"] * Math.abs(r.Value) / 365;
      const payers = positionRows.filter((r) => fin(r["Funding (APR)"]) && r["Funding (APR)"] <= -ADVERSE_FUNDING_APR
        && dailyCost(r) >= ADVERSE_FUNDING_MIN_DAILY).sort((a, b) => dailyCost(b) - dailyCost(a));
      fundingNow = payers.map((r) => ({ coin: String(r.Symbol).split(":").pop(), side: r.Side.toLowerCase(),
        account: String(r.Account).slice(-4), apr: r["Funding (APR)"] }));
      if (payers.length) attention.push(["Funding", ""]);  // filled in below with the daemon's sustained payers
    }
    if (tradingEquity) {
      const leverage = gross / tradingEquity;
      const available = sum(traders.map((a) => a.available));
      if (available / tradingEquity < LOW_AVAILABLE_PCT) {
        attention.push(["Available margin low", `${percent(available / tradingEquity, false, 0)} of trading equity.`]);
      }
    }
    // Each HL wallet keeps 20% of its equity free; past that the executor refuses new trades there.
    trading.forEach((a) => {
      if (!(a.equity > 0)) return;
      const used = a.margin_used / a.equity;
      if (used > 0.80) attention.push(["Margin reserve breached", `${a.label} ${percent(used, false, 0)} of equity in margin (max 80%); new trades refused.`]);
    });
    // One Risk row: open risk against the cap, then the book's volatility, VaR and betas. It needs attention
    // once open risk nears the cap.
    const riskParts = [];
    if (book.length) riskParts.push(`open risk ${percent(riskUsed / riskCap, false, 0)} of the cap`);
    if (riskRow && fin(riskRow["Volatility (/yr)"])) {
      riskParts.push(`volatility ${percent(riskRow["Volatility (/yr)"], false, 0)}/yr`);
      if (deskEquity > 0 && fin(riskRow["VaR 95%"])) riskParts.push(`VaR 95% ${percent(riskRow["VaR 95%"] / deskEquity)}/day`);
      [["BTC", riskRow["Beta BTC"]], ["S&P 500", riskRow["Beta S&P 500"]]].filter(([, b]) => fin(b))
        .forEach(([name, b]) => riskParts.push(`beta ${name} ${b.toFixed(2)}`));
    }
    if (riskParts.length) {
      const text = riskParts.join(", ").replace(/^./, (c) => c.toUpperCase()) + ".";
      if (book.length && riskUsed > riskCap) attention.unshift(["Risk: over cap", text]);
      else if (book.length && riskUsed > riskCap * NEAR_CAP_PCT) attention.push(["Risk: near cap", text]);
      else well.push(["Risk", text]);
    }
    if (problems.length) attention.unshift(["Unreadable accounts", problems.join("; ") + " Totals leave them out."]);
    ((regime && regime.notices) || []).slice().reverse().forEach((row) => attention.unshift(row));  // desk/market_regime.py
    Object.entries(STRESS).forEach(([name, shocks]) => {  // scenario stress rows (the Python dashboard's, desk/stress.py)
      const loss = -sum(positionRows.map((r) => (fin(r.Value) ? r.Value * (shocks[stressKind(r["Asset class"], r.Symbol)] || 0) : 0)));
      const share = deskEquity > 0 ? loss / deskEquity : NaN;
      if (!(share >= STRESS_WARN)) return;
      const status = share >= STRESS_BLOCK ? "block" : "warn";
      const shock = name === "alt rally" ? "alts +25% and BTC/ETH +10%" : "equities -10%";
      attention.unshift([`Stress: ${name} (${status})`, `${shock}: ${percent(-share, false, 0)} of desk equity; ${status === "block" ? "adding entries blocked" : "entries shrunk past 35%"}.`]);
    });
    // Data flow: every feed the page relies on, with how stale it may get before it is a problem.
    const minutesSince = (stamp) => (fin(stamp) ? (Date.now() - stamp) / 60000 : Infinity);
    const flow = [
      ["trading daemon", daemonAlerts && Number(daemonAlerts.heartbeat) * 1000, 20, "guards and ladders stopped"],
      ["IB", ibData && Date.parse(ibData.as_of), 30, "Mac push; TWS logged in?"],
      ["market regime", regime && Date.parse(regime.as_of), 8 * 60, "hourly from the daemon"],
      ["wallet performance", walletPerf && Date.parse(walletPerf.as_of), 36 * 60, "daily job"],
      ["relay history capture", syncRun && Date.parse(syncRun.at), 7 * 60, "every 6 hours"],
    ];
    const staleFeeds = flow.filter(([, stamp, limit]) => minutesSince(stamp) > limit);
    if (staleFeeds.length) {
      const describe = ([name, stamp, , note]) => `${name} ${fin(stamp) ? `last ${eastern(stamp)}` : "never received"} (${note})`;
      attention.unshift(["Data flow down", staleFeeds.map(describe).join("; ") + "."]);
    }
    ((research && research.overdue) || []).forEach((item) => attention.push([`Review: ${item.coin} held ${Math.round(item.days)} days`,
      `...${item.wallet} ${item.coin} ${String(item.side).toLowerCase()}: close, or re-tag conviction or hedge.`]));
    if (daemonAlerts) {  // the trading daemon's problems (desk/alerts.py), first
      const age = (Date.now() / 1000 - Number(daemonAlerts.heartbeat || 0)) / 60;
      const rows = age > 15
        ? [["Trading daemon not reporting", `Silent ${age.toFixed(0)} min: guards, margin guard and ladders stopped.`]]
        : (daemonAlerts.alerts || []).filter((a) => !a.expires || a.expires > Date.now() / 1000)
          .map((a) => [String(a.title).charAt(0).toUpperCase() + String(a.title).slice(1),
            /^squeeze/i.test(a.title) ? a.text : `${a.text} (since ${eastern(a.since, true)})`]);  // a squeeze is about now: no time
      rows.reverse().forEach((row) => attention.unshift(row));
    }
    // One "Funding" row (desk/funding_watch.py consolidate): the daemon's sustained payers, worst first, with their
    // average, then the payers adverse only at the current rate; it stands where the first funding row stood.
    {
      const sustained = new Map();
      let hours = FUNDING_SUSTAINED_HOURS, at = -1;
      const kept = [];
      attention.forEach(([title, text]) => {
        const match = title === "Funding" && /^\.\.\.(\w+) (\S+) (long|short) (-?[\d.]+)% avg APR over (\d+) hrs/.exec(text);
        if (match) {
          const coin = match[2].split(":").pop(), account = match[1].slice(-4);
          sustained.set(`${coin} ${account}`, { name: `${coin} ${match[3]} (${account})`, apr: Number(match[4]) / 100 });
          hours = Number(match[5]);
        } else if (!(title === "Funding" && text === "")) {
          kept.push([title, text]);
          return;
        }
        if (at < 0) at = kept.length;
      });
      if (at >= 0) {
        const worstFirst = (items) => items.sort((a, b) => a.apr - b.apr).map((i) => `${i.name} ${percent(i.apr)}`).join(", ");
        const onlyNow = fundingNow.filter((i) => !sustained.has(`${i.coin} ${i.account}`))
          .map((i) => ({ name: `${i.coin} ${i.side} (${i.account})`, apr: i.apr }));
        const parts = [];
        if (sustained.size) parts.push(`${worstFirst([...sustained.values()])} avg APR over ${hours} hrs`);
        if (onlyNow.length) parts.push(`${worstFirst(onlyNow)} APR now`);
        kept.splice(at, 0, ["Funding", parts.join("; ") + "."]);
        attention.splice(0, attention.length, ...kept);
      }
    }

    const money2 = { "Equity": "usd", "Cash": "usd", "Margin used": "usd", "Available": "usd", "Gross": "usd", "Value": "usd",
      "Risk equity": "usd", "Open risk": "usd", "Long": "usd", "Short": "usd", "VaR 95%": "usd", "VaR 99%": "usd" };
    const signed = { "uPnL": "susd", "Net": "susd", "Value": "susd" };
    const tables = {
      performance: table(performanceRows, ["Account", "Return 24h", "Return 7D", "Return 30D", "Return 90D", "Return 1Y", "Return since inception", "Since"],
        { "Return 24h": "spct", "Return 7D": "spct", "Return 30D": "spct", "Return 90D": "spct", "Return 1Y": "spct", "Return since inception": "spct" },
        { totalLast: true, key: { "Return 30D": "abs" } }),
      accounts: table(accountRows, ["Account", "% of desk", "Cash / equity", "Margin used / equity", "Available / equity",
        "uPnL / equity", "Gross / equity", "Net / equity", "Positions"],
      { "% of desk": "pct", "Cash / equity": "pct", "Margin used / equity": "pct", "Available / equity": "pct",
        "uPnL / equity": "spct", "Gross / equity": "pct", "Net / equity": "spct", "Positions": "int" },
      { total: accountTotal, key: { "% of desk": "max" } }),
      exposure: table(exposureRows, ["Asset class", "Long", "Short", "Net", "Gross", "Gross / equity", "Net / equity"],
        Object.assign({}, money2, { "Net": "susd", "Gross / equity": "pct", "Net / equity": "spct" }), { totalLast: true, key: { "Net / equity": "abs" } }),
      movers: table(moverRows("Crypto"), ["Account", "Symbol", "Side", "Value", "uPnL", "% of position"],
        { "Side": "side", "Value": "susd", "uPnL": "susd", "% of position": "spct" }, { key: { "uPnL": "abs" } }),
      movers_stocks: table(moverRows("Stocks"), ["Account", "Symbol", "Side", "Value", "uPnL", "% of position"],
        { "Side": "side", "Value": "susd", "uPnL": "susd", "% of position": "spct" }, { key: { "uPnL": "abs" } }),
      positions: table(tableRows.filter(isCrypto), ["Asset class", "Account", "Symbol", "Side", "Quantity", "Avg price", "Mark", "Value",
        "uPnL", "uPnL (%)", "24h change (%)", "Funding (APR)", "Weight", "Exposure / desk value"],
      Object.assign({ "Side": "side", "Quantity": "num", "Avg price": "price", "Mark": "price" }, signed,
        { "uPnL (%)": "spct", "24h change (%)": "spct", "Funding (APR)": "spct", "Weight": "pct", "Exposure / desk value": "pct" }),
      { total: positionTotal(tableRows.filter(isCrypto)), key: { "Weight": "max" } }),
      positions_stocks: table(tableRows.filter((r) => !isCrypto(r)), ["Asset class", "Account", "Symbol", "Side", "Quantity", "Avg price", "Mark", "Value",
        "uPnL", "uPnL (%)", "24h change (%)", "Funding (APR)", "Weight", "Exposure / desk value"],
      Object.assign({ "Side": "side", "Quantity": "num", "Avg price": "price", "Mark": "price" }, signed,
        { "uPnL (%)": "spct", "24h change (%)": "spct", "Funding (APR)": "spct", "Weight": "pct", "Exposure / desk value": "pct" }),
      { total: positionTotal(tableRows.filter((r) => !isCrypto(r))), key: { "Weight": "max" } }),
      open_risk: table(book.map((b) => ({ "Venue": b.venue, "Account": b.label, "Positions": b.positions, "Risk equity": b.equity,
        "Open risk": b.risk, "% of desk equity": riskEquity ? b.risk / riskEquity : NaN }))
        .concat([{ "Venue": "Total", "Account": "", "Positions": sum(book.map((b) => b.positions)), "Risk equity": riskEquity,
          "Open risk": riskUsed, "% of desk equity": riskEquity ? riskUsed / riskEquity : NaN }]),
      ["Venue", "Account", "Positions", "Risk equity", "Open risk", "% of desk equity"],
      Object.assign({}, money2, { "Positions": "int", "Open risk": "rusd", "% of desk equity": "rpct" }),
      { totalLast: true, key: { "% of desk equity": "max" } }),
      market_making: (() => {  // the ladders, as the trading daemon last sent them (desk/market_making.py)
        const rows = (daemonAlerts && daemonAlerts.market_making) || [];
        if (!rows.length) return { columns: [], rows: [], total: null };
        const total = { "Account": "Total" };
        ["Fills", "Volume", "Spread captured", "Inventory drift", "Funding", "Ladder PnL", "Fees"].forEach((k) => { total[k] = sum(rows.map((r) => r[k])); });
        total["Captured per $1k"] = total.Volume ? (total["Spread captured"] + total.Fees) / total.Volume * 1000 : NaN;
        ["Markout 1m (bps)", "Markout 15m (bps)"].forEach((k) => {  // weighted by each ladder's fills
          const known = rows.filter((r) => Number.isFinite(r[k]));
          const fills = sum(known.map((r) => r.Fills));
          total[k] = fills ? sum(known.map((r) => r[k] * r.Fills)) / fills : NaN;
        });
        return table(rows, ["Account", "Coin", "Side", "Status", "Since", "Fills", "Volume", "Spread captured", "Inventory drift", "Fees", "Funding", "Ladder PnL", "Captured per $1k", "Markout 1m (bps)", "Markout 15m (bps)", "Position vs base", "Quotes", "Queued"],
          { "Fills": "int", "Volume": "usd", "Spread captured": "susd", "Inventory drift": "susd", "Fees": "susd", "Funding": "susd", "Ladder PnL": "susd", "Captured per $1k": "susd", "Markout 1m (bps)": "sbps", "Markout 15m (bps)": "sbps", "Position vs base": "spct", "Quotes": "int", "Queued": "int" },
          { total, key: { "Ladder PnL": "abs" } });
      })(),
      strategies: (() => {  // desk/journal.py, as the daemon last sent it (the last row is the total)
        const rows = (research && research.strategies) || [];
        if (rows.length < 2) return { columns: [], rows: [], total: null };
        // Each strategy's PnL as a share of desk value (desk/journal.py performance_view): Realized 30d is net of fees
        // and includes funding; activity (positions, volume, fills) stays in the data.
        const view = (r) => {
          const realized = (num(r["Realized PnL 30d"]) + num(r["Funding 30d"])) / deskEquity, unrealized = num(r["Unrealized PnL"]) / deskEquity;
          return { "Strategy": r.Strategy, "Realized 30d": realized, "Unrealized": unrealized, "Total": realized + unrealized };
        };
        return table(rows.slice(0, -1).map(view), ["Strategy", "Realized 30d", "Unrealized", "Total"],
          { "Realized 30d": "spct", "Unrealized": "spct", "Total": "spct" },
          { total: view(rows[rows.length - 1]), key: { "Total": "abs" } });
      })(),
      candidates: (() => {  // desk/screener.py
        const rows = (research && research.screener && research.screener.shorts) || [];
        return rows.length ? table(rows, ["Coin", "Score", "Basis", "vs BTC 30d", "vs BTC 7d", "From high 60d", "Funding 7d (APR)", "OI / volume", "Volume 24h", "Notes"],
          { "Score": "num2", "vs BTC 30d": "spct", "vs BTC 7d": "spct", "From high 60d": "spct", "Funding 7d (APR)": "spct", "OI / volume": "num2", "Volume 24h": "usd" },
          { key: { "Score": "min" } }) : { columns: [], rows: [], total: null };
      })(),
      mm_candidates: (() => {
        const rows = (research && research.screener && research.screener.market_making) || [];
        return rows.length ? table(rows, ["Coin", "Efficiency 30d", "Move 30d", "Range position 60d", "Daily range 30d", "Volume 24h", "Longs pay 7d (APR)", "Own ladder edge per $1k", "Notes"],
          { "Efficiency 30d": "num2", "Move 30d": "spct", "Range position 60d": "pct", "Daily range 30d": "pct", "Volume 24h": "usd", "Longs pay 7d (APR)": "spct", "Own ladder edge per $1k": "susd" },
          { key: { "Efficiency 30d": "min" } }) : { columns: [], rows: [], total: null };
      })(),
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
      notes.push(`${ibs[0].label} is read through TWS on the desk's Mac, which sends it to the relay every 10 minutes while the local dashboard runs; sent ${eastern(ibData.as_of)}.`);
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
        // The whole desk, wallets included (``headline``); the money-weighted PnL stays HL + IB, since the
        // wallets have no deposit record.
        returns: { since_start: headline.returns.since_start, "7D": headline.returns["7D"], "30D": headline.returns["30D"],
          "90D": headline.returns["90D"] },
        money_weighted: moneyWeighted(histories, start), since: iso(headline.start), drawdown: headline.drawdown,
        peak_date: iso(headline.peak.t),
      },
      highlights: { well, attention, hover: Object.fromEntries(((daemonAlerts && daemonAlerts.alerts) || [])
        .filter((a) => a.context).map((a) => [String(a.title).charAt(0).toUpperCase() + String(a.title).slice(1), a.context])) },
      tables,
      charts: { value: valueSeries, value_with_wallets: valueWithWallets(), twr, value_idle: [] },
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
      "Basis": "Current holdings (ex-ante)", "Periods": days.length, "From": sinceLabel(days[0]), "To": sinceLabel(days[days.length - 1]),
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
  refresh();
  setInterval(refresh, CFG.refreshSeconds * 1000);
})();
