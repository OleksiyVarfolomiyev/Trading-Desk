// Desk relay: answers the public desk page's account questions without revealing wallet addresses.
//
// Deploy as a Cloudflare Worker (free plan) and add one secret in the Worker's
// Settings > Variables and Secrets:
//   WALLETS  {"07f2": "0x7532...07f2", "c248": "0xf3fb...c248"}   (short id -> full address)
//
// Endpoints (GET only; answers are cached for CACHE_SECONDS):
//   /wallets             the wallets' short ids (their last four characters)
//   /benchmark/SPY       SPY's daily closes from Yahoo Finance (a browser page cannot call Yahoo)
//   /hl/<id>/history     the wallet's daily value and PnL saved so far (KV), so old history keeps its
//                        daily detail after HL thins it to weekly points
//   /ib                  the IB account as the desk's Mac last sent it (KV): balances, positions, risk,
//                        daily NAV; written with POST /ingest/ib and the INGEST_TOKEN secret
//
// Storage: a KV namespace bound as DESK (wrangler.jsonc). Without it, history and IB are skipped.
//   /hl/<id>/account     positions and margin on every HL DEX, spot balances, account mode, fee rates
//   /hl/<id>/portfolio   HL's value and PnL history (day, week, month, all time)
//   /wallet/<id>         token balances outside HL: Base and Ethereum (free public nodes, Blockscout)
//
// Every answer is scrubbed: transaction hashes are dropped (a hash leads back to the address)
// and any text equal to the address is removed, so the page never learns it.

const HL_INFO = "https://api.hyperliquid.xyz/info";
// Free public nodes, tried in turn: Cloudflare's shared addresses are often rate limited by one.
const BASE_RPCS = ["https://base-rpc.publicnode.com", "https://base.drpc.org", "https://1rpc.io/base",
  "https://base-mainnet.public.blastapi.io", "https://mainnet.base.org"];
const ETH_RPCS = ["https://ethereum-rpc.publicnode.com", "https://1rpc.io/eth", "https://eth-mainnet.public.blastapi.io"];
const ETH_BLOCKSCOUT = "https://eth.blockscout.com/api/v2/addresses/";
const CACHE_SECONDS = 20;
// Token balances change only when tokens are moved, so they are read at most every 10 minutes.
const WALLET_SECONDS = 600;
// Base tokens read through the public node: address -> [symbol, decimals].
const BASE_TOKENS = {
  "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ["USDC", 6],
  "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ["USDbC", 6],
  "0x4200000000000000000000000000000000000006": ["WETH", 18],
  "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": ["cbBTC", 8],
  "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": ["cbETH", 18],
  "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": ["DAI", 18],
  "0x940181a94a35a4569e4529a3cdfb74e38fd98631": ["AERO", 18],
  "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42": ["EURC", 6],
  "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": ["wstETH", 18],
  "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": ["USDT", 6],
  "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf": ["VVV", 18],
  "0x321b7ff75154472b18edb199033ff4d116f340ff": ["sVVV", 18],
};
// Ethereum tokens read through a public node when Blockscout (which lists every token) is busy:
// the priced tokens these wallets hold, and the majors.
const ETH_TOKENS = {
  "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ["USDC", 6],
  "0xdac17f958d2ee523a2206206994597c13d831ec7": ["USDT", 6],
  "0x6b175474e89094c44da98b954eedeac495271d0f": ["DAI", 18],
  "0x0000000000085d4780b73119b644ae5ecd22b376": ["TUSD", 18],
  "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ["WETH", 18],
  "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": ["WBTC", 8],
  "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": ["stETH", 18],
  "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0": ["wstETH", 18],
  "0x45804880de22913dafe09f4980848ece6ecbaf78": ["PAXG", 18],
  "0x1494ca1f11d487c2bbe4543e90080aeba4ba3c2b": ["DPI", 18],
  "0xaa6e8127831c9de45ae56bb1b0d4d4da6e5665bd": ["ETH2X-FLI", 18],
  "0x58d97b57bb95320f9a05dc918aef65434969c2b2": ["MORPHO", 18],
  "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": ["UNI", 18],
  "0x514910771af9ca656af840dff83e8264ecf986ca": ["LINK", 18],
  "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": ["AAVE", 18],
  "0x5a98fcbea516cf06857215779fd812ca3bef1b32": ["LDO", 18],
  "0xc00e94cb662c3520282e6f5717214004a7f26888": ["COMP", 18],
  "0xba100000625a3754423978a60c9317c58a424e3d": ["BAL", 18],
  "0x6b3595068778dd592e39a122f4f5a5cf09c90fe2": ["SUSHI", 18],
  "0x111111111117dc0aa78b770fa6a738034120c302": ["1INCH", 18],
};
// Unclaimed staking rewards: staking contract -> [view selector taking the wallet, reward token, symbol].
const STAKING = {
  "0x321b7ff75154472b18edb199033ff4d116f340ff": ["0x31d7a262", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf", "VVV"],
};
const NATIVE = "0x0000000000000000000000000000000000000000";

let dexCache = { at: 0, dexes: null };
// Answers kept in this Worker instance (the Cache API does nothing on workers.dev addresses).
const memo = new Map();
const lastGood = new Map();  // wallet -> {chain: [holdings]} from the last successful read

async function postJSON(url, body) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) throw new Error(`${new URL(url).host} answered HTTP ${response.status}`);
  return response.json();
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${new URL(url).host} answered HTTP ${response.status}`);
  return response.json();
}

const hl = (body) => postJSON(HL_INFO, body);

async function perpDexes() {
  if (!dexCache.dexes || Date.now() - dexCache.at > 3600_000) {
    const listed = await hl({ type: "perpDexs" });
    dexCache = { at: Date.now(), dexes: [""].concat(listed.filter((item) => item && item.name).map((item) => item.name)) };
  }
  return dexCache.dexes;
}

async function hlAccount(user) {
  const dexes = await perpDexes();
  const [abstraction, spot, fees, ...states] = await Promise.all([
    hl({ type: "userAbstraction", user }),
    hl({ type: "spotClearinghouseState", user }),
    hl({ type: "userFees", user }),
    ...dexes.map((dex) => hl(dex ? { type: "clearinghouseState", user, dex } : { type: "clearinghouseState", user })),
  ]);
  return {
    abstraction,
    spot: (spot && spot.balances) || [],
    // Only the two rates: the full fee answer can name linked accounts.
    fees: { maker: Number(fees.userAddRate), taker: Number(fees.userCrossRate) },
    perp: dexes.map((dex, index) => [dex, { marginSummary: states[index].marginSummary, assetPositions: states[index].assetPositions }]),
  };
}

// Every balance on a chain in one call, through Multicall3 (the same address on every chain).
// One ordinary eth_call is accepted by free nodes that refuse batches, and is throttled far less.
const MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11";
const word = (value) => BigInt(value).toString(16).padStart(64, "0");

function aggregate3(calls) {  // calldata for aggregate3((address target, bool allowFailure, bytes callData)[])
  const items = calls.map(({ target, data }) => {
    const length = data.length / 2;
    return word(target) + word(1) + word(0x60) + word(length) + data.padEnd(Math.ceil(length / 32) * 64, "0");
  });
  const offsets = [];
  let position = 32 * items.length;
  for (const item of items) { offsets.push(word(position)); position += item.length / 2; }
  return "0x82ad56cb" + word(0x20) + word(items.length) + offsets.join("") + items.join("");
}

function decodeAggregate3(result) {  // each call's uint256 answer, or null where it failed
  const raw = result.slice(2);
  const at = (byte) => BigInt("0x" + raw.slice(byte * 2, byte * 2 + 64));
  const base = Number(at(0)), count = Number(at(base)), start = base + 32, values = [];
  for (let k = 0; k < count; k++) {
    const item = start + Number(at(start + 32 * k));
    const dataAt = item + Number(at(item + 32)), length = Number(at(dataAt));
    values.push(at(item) !== 0n && length >= 32 ? at(dataAt + 32) : null);
  }
  return values;
}

async function multicall(urls, calls) {
  const data = aggregate3(calls), failures = [];
  const first = Math.floor(Math.random() * urls.length);  // spread the load over the nodes
  for (let i = 0; i < urls.length; i++) {
    const url = urls[(first + i) % urls.length];
    try {
      const reply = await postJSON(url, { jsonrpc: "2.0", id: 1, method: "eth_call", params: [{ to: MULTICALL, data }, "latest"] });
      if (!reply || typeof reply.result !== "string" || reply.result.length < 130) {
        throw new Error(reply && reply.error ? String(reply.error.message || reply.error) : "no result");
      }
      const values = decodeAggregate3(reply.result);
      if (values.length !== calls.length) throw new Error("answer has the wrong length");
      return values;
    } catch (error) {
      failures.push(`${new URL(url).host}: ${error.message}`);
    }
  }
  throw new Error(failures.join("; "));
}

const toUnits = (raw, decimals) => {
  if (raw === null || raw === undefined) return 0;
  const scale = 10n ** BigInt(decimals);
  return Number(raw / scale) + Number(raw % scale) / Number(scale);
};

// Native balance (Multicall3's getEthBalance) and each token's balanceOf, read in one call.
async function tokenHoldings(chain, urls, address, tokens, extra = []) {
  const padded = address.toLowerCase().slice(2).padStart(64, "0");
  const list = Object.keys(tokens);
  const calls = [{ target: MULTICALL, data: "4d2301cc" + padded }]
    .concat(list.map((token) => ({ target: token, data: "70a08231" + padded })))
    .concat(extra.map(({ target, selector }) => ({ target, data: selector.replace(/^0x/, "") + padded })));
  const values = await multicall(urls, calls);
  const holdings = [];
  const native = toUnits(values[0], 18);
  if (native) holdings.push({ chain, token: NATIVE, symbol: "ETH", amount: native });
  list.forEach((token, index) => {
    const [symbol, decimals] = tokens[token];
    const amount = toUnits(values[1 + index], decimals);
    if (amount) holdings.push({ chain, token, symbol, amount });
  });
  extra.forEach((item, index) => {
    const amount = toUnits(values[1 + list.length + index], 18);
    if (amount) holdings.push({ chain, token: item.token, symbol: item.symbol, amount, rewards: true });
  });
  return holdings;
}

function baseHoldings(address) {
  const staking = Object.entries(STAKING).map(([target, [selector, token, symbol]]) => ({ target, selector, token, symbol }));
  return tokenHoldings("base", BASE_RPCS, address, BASE_TOKENS, staking);
}

async function ethereumHoldings(address) {
  try {
    return await blockscoutHoldings(address);
  } catch (error) {
    // Blockscout often refuses Cloudflare's shared addresses: read the known tokens instead.
    return tokenHoldings("ethereum", ETH_RPCS, address, ETH_TOKENS);
  }
}

async function blockscoutHoldings(address) {
  const [info, balances] = await Promise.all([
    getJSON(ETH_BLOCKSCOUT + address),
    getJSON(ETH_BLOCKSCOUT + address + "/token-balances"),
  ]);
  const holdings = [];
  const native = Number(info.coin_balance || 0) / 1e18;
  if (native) holdings.push({ chain: "ethereum", token: NATIVE, symbol: "ETH", amount: native });
  for (const item of balances || []) {
    const token = item.token || {};
    if (token.type !== "ERC-20" || token.decimals === null || token.decimals === undefined || token.decimals === "") continue;
    const amount = Number(BigInt(item.value || "0")) / 10 ** Number(token.decimals);
    if (amount) holdings.push({ chain: "ethereum", token: String(token.address_hash || token.address || "").toLowerCase(),
      symbol: token.symbol || "?", amount });
  }
  return holdings;
}

async function walletHoldings(address) {
  const chains = ["base", "ethereum"];
  const results = await Promise.allSettled([baseHoldings(address), ethereumHoldings(address)]);
  const saved = lastGood.get(address) || {};
  const holdings = [], problems = [];
  results.forEach((result, index) => {
    const chain = chains[index];
    if (result.status === "fulfilled") {
      saved[chain] = { at: Date.now(), holdings: result.value };
      holdings.push(...result.value);
    } else if (saved[chain]) {  // a busy node: keep the last balances read
      holdings.push(...saved[chain].holdings);
      problems.push(`${chain}: last balances read ${Math.round((Date.now() - saved[chain].at) / 60000)} min ago (node busy)`);
    } else {
      problems.push(`${chain}: ${result.reason && result.reason.message || result.reason}`);
    }
  });
  lastGood.set(address, saved);
  return { holdings, problems };
}

// SPY daily closes since 2015, kept for an hour (they change once a day).
let spyMemo = { at: 0, body: null };
async function spyCloses() {
  if (spyMemo.body && Date.now() - spyMemo.at < 3600_000) return spyMemo.body;
  const url = "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=1d&period1=1420070400&period2="
    + Math.floor(Date.now() / 1000);
  const response = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (desk relay)", Accept: "application/json" } });
  if (!response.ok) throw new Error(`Yahoo Finance answered HTTP ${response.status}`);
  const result = (await response.json()).chart.result[0];
  const closes = result.indicators.quote[0].close;
  const body = { closes: result.timestamp.map((t, i) => [new Date(t * 1000).toISOString().slice(0, 10), closes[i]])
    .filter(([, close]) => close > 0) };
  spyMemo = { at: Date.now(), body };
  return body;
}

// Drop transaction hashes and anything naming the address; the address itself never leaves.
function scrub(value, address) {
  const hidden = new RegExp(address.slice(2), "gi");
  const clean = (item) => {
    if (Array.isArray(item)) return item.map(clean);
    if (item && typeof item === "object") {
      const out = {};
      for (const [key, inner] of Object.entries(item)) {
        if (key === "hash" || key === "user" || key === "users") continue;
        out[key] = clean(inner);
      }
      return out;
    }
    return typeof item === "string" ? item.replace(hidden, "") : item;
  };
  return clean(value);
}

function reply(body, status = 200, maxAge = 0) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": maxAge ? `public, max-age=${maxAge}` : "no-store",
    },
  });
}

// ---- saved history and IB (KV) ----
// The wallet's points on HL's all-time PnL scale: weekly before the last month, then finer, as the page merges them.
function mergedHistory(portfolio) {
  const windows = Object.fromEntries(portfolio || []);
  const points = (name) => {
    const window = windows[name];
    if (!window) return [];
    const pnl = new Map((window.pnlHistory || []).map(([t, v]) => [t, Number(v)]));
    return (window.accountValueHistory || []).filter(([t]) => pnl.has(t))
      .map(([t, v]) => ({ t, value: Number(v), pnl: pnl.get(t) })).sort((a, b) => a.t - b.t);
  };
  const pnlAt = (series, t) => {
    const before = series.filter((p) => p.t <= t), after = series.filter((p) => p.t > t);
    if (before.length && after.length) {
      const a = before[before.length - 1], b = after[0];
      return a.pnl + (b.pnl - a.pnl) * (t - a.t) / (b.t - a.t);
    }
    return (before.length ? before[before.length - 1] : after[0]).pnl;
  };
  let series = points("allTime");
  for (const name of ["month", "week", "day"]) {
    const finer = points(name);
    if (!finer.length) continue;
    if (!series.length) { series = finer; continue; }
    const start = finer[0].t;
    const offset = series[0].t >= start ? series[0].pnl - pnlAt(finer, series[0].t) : pnlAt(series, start) - finer[0].pnl;
    series = series.filter((p) => p.t < start).concat(finer.map((p) => ({ t: p.t, value: p.value, pnl: p.pnl + offset })));
  }
  return series;
}

const dayOf = (t) => new Date(t).toISOString().slice(0, 10);

// Merge points ([t, value, pnl]) into the wallet's saved daily series, keeping each day's last point.
async function saveDaily(env, id, points) {
  const key = `history:${id}`;
  const saved = (await env.DESK.get(key, "json")) || {};
  let changed = false;
  for (const [t, value, pnl] of points) {
    const day = dayOf(t);
    if (!saved[day] || saved[day][0] <= t) {
      if (!saved[day] || saved[day][1] !== value || saved[day][2] !== pnl) changed = true;
      saved[day] = [t, value, pnl];
    }
  }
  if (changed) await env.DESK.put(key, JSON.stringify(saved));
  return saved;
}

const lastCapture = new Map();
async function captureHistory(env, id, portfolio) {
  if (Date.now() - (lastCapture.get(id) || 0) < 3600_000) return;  // at most hourly: KV's free plan allows 1,000 writes a day
  lastCapture.set(id, Date.now());
  await saveDaily(env, id, mergedHistory(portfolio).map((p) => [p.t, p.value, p.pnl]));
}

function authorized(request, env) {
  const given = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  const expected = env.INGEST_TOKEN || "";
  if (!expected || given.length !== expected.length) return false;
  let difference = 0;
  for (let i = 0; i < given.length; i++) difference |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return difference === 0;
}

// POST /ingest/ib (the IB account from the desk's Mac) and /ingest/history/<id> (seeding saved history).
async function ingest(request, env, kind, id, wallets) {
  if (!env.DESK) return reply({ error: "no KV namespace is bound as DESK" }, 501);
  if (!authorized(request, env)) return reply({ error: "not authorized" }, 401);
  let body;
  try {
    body = await request.json();
  } catch (error) {
    return reply({ error: "the body is not JSON" }, 400);
  }
  if (kind === "ib") {
    const text = JSON.stringify(body);
    if (/(?:DU|U)\d{5,}/.test(text) || Object.values(wallets).some((a) => text.toLowerCase().includes(a.slice(2).toLowerCase()))) {
      return reply({ error: "refused: the body holds a full account identifier" }, 400);
    }
    await env.DESK.put("ib", text);
    return reply({ ok: true, bytes: text.length });
  }
  if (kind === "history" && wallets[id] && Array.isArray(body.points)) {
    const saved = await saveDaily(env, id, body.points);
    return reply({ ok: true, days: Object.keys(saved).length });
  }
  return reply({ error: "unknown ingest" }, 404);
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: { "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET", "Access-Control-Max-Age": "86400" } });
    }
    if (request.method === "POST") {
      const [first, kind, id] = new URL(request.url).pathname.split("/").filter(Boolean);
      let walletsForIngest = {};
      try { walletsForIngest = JSON.parse(env.WALLETS || "{}"); } catch (error) { /* checked below */ }
      if (first === "ingest") return ingest(request, env, kind, id, walletsForIngest);
      return reply({ error: "unknown request" }, 404);
    }
    if (request.method !== "GET") return reply({ error: "only GET and POST are supported" }, 405);
    let wallets;
    try {
      wallets = JSON.parse(env.WALLETS || "{}");
    } catch (error) {
      return reply({ error: "the WALLETS secret is not valid JSON" }, 500);
    }
    const url = new URL(request.url);
    const [kind, id, what] = url.pathname.split("/").filter(Boolean);
    if (kind === "wallets" && !id) return reply({ wallets: Object.keys(wallets) }, 200, CACHE_SECONDS);
    if (kind === "benchmark" && id === "SPY" && !what) {
      try {
        return reply(await spyCloses(), 200, 3600);
      } catch (error) {
        return reply({ error: String(error && error.message || error) }, 502);
      }
    }
    if (kind === "ib" && !id) {
      const saved = env.DESK ? await env.DESK.get("ib", "json") : null;
      return saved ? reply(saved, 200, CACHE_SECONDS) : reply({ error: "no IB data has been sent yet" }, 404);
    }
    const address = wallets[id];
    if (!address || !/^0x[0-9a-fA-F]{40}$/.test(address)) return reply({ error: "unknown wallet id" }, 404);
    if (kind === "hl" && what === "history") {
      const saved = env.DESK ? (await env.DESK.get(`history:${id}`, "json")) || {} : {};
      return reply({ points: Object.values(saved).sort((a, b) => a[0] - b[0]) }, 200, CACHE_SECONDS);
    }
    const maxAge = kind === "wallet" ? WALLET_SECONDS : CACHE_SECONDS;
    const remembered = memo.get(url.pathname);
    if (remembered && Date.now() - remembered.at < maxAge * 1000) return reply(remembered.body, 200, CACHE_SECONDS);
    const cache = typeof caches !== "undefined" && caches.default ? caches.default : null;
    const key = new Request(url.origin + url.pathname, { method: "GET" });
    if (cache) {
      const hit = await cache.match(key);
      if (hit) return hit;
    }
    let body;
    try {
      if (kind === "hl" && what === "account") body = await hlAccount(address);
      else if (kind === "hl" && what === "portfolio") body = await hl({ type: "portfolio", user: address });
      else if (kind === "wallet" && !what) body = await walletHoldings(address);
      else return reply({ error: "unknown request" }, 404);
    } catch (error) {
      return reply({ error: scrub(String(error && error.message || error), address) }, 502);
    }
    const clean = scrub(body, address);
    memo.set(url.pathname, { at: Date.now(), body: clean });
    if (kind === "hl" && what === "portfolio" && env.DESK && ctx && ctx.waitUntil) {
      ctx.waitUntil(captureHistory(env, id, body).catch(() => {}));  // keep today's daily points
    }
    const response = reply(clean, 200, CACHE_SECONDS);
    if (cache && ctx && ctx.waitUntil) ctx.waitUntil(cache.put(key, response.clone()));
    return response;
  },
};
