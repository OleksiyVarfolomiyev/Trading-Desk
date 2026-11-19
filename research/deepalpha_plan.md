# DeepAlpha (app.deepalpha.pro) — recon and plan (2026-11-18)

## What is public
Free, no account or wallet. Browser app showing: order-book / liquidity heatmap with wall and spoof flags
("magnets"), liquidation map and cascade simulator (longs/shorts liquidated within ±10%, biggest cluster),
wallet tracker (daily PnL, edge per dollar, Sharpe, markout, net positions by coin), "Smart Money" followed traders.
Upstream data: Hyperliquid and Lighter public APIs plus 0xArchive. No refresh interval stated, no API docs, no terms page found.

## Restrictions
robots.txt: `Content-Signal: search=yes, ai-train=no, use=reference`; ~30 AI crawlers fully disallowed (ClaudeBot among them);
`/api/` disallowed for all crawlers. The page is a client-rendered app, so scraping it means hitting that `/api/` or
driving a headless browser. Both go against the stated wishes. **Decision: do not scrape the site. No logger built.**

## Plan: rebuild the signals from the same public upstream
Everything the app shows derives from Hyperliquid's public info API, which the desk already uses.
1. **Wall / spoof signal:** `l2Book` snapshots every 60 s for the ~40 liquid perps; log wall size vs depth, persistence,
   and cancel-before-touch rate (spoof proxy).
2. **Liquidation clusters:** approximate from open positions of known large wallets (`clearinghouseState`) and the
   leaderboard; log distance of price to the biggest cluster each hour.
3. **Smart-money net positioning:** top-PnL wallets' net long/short per coin, hourly (desk already tracks wallets).
4. **Log** to `research/data/deepalpha_like/*.parquet` (timestamp UTC, coin, signal values, mid price).
5. **Test (forward, no peeking):** signal -> next 1h/4h/24h return, net of taker costs; keep only if t>=2 in both halves
   (desk hypothesis rule). Failures get a memory note, as with pump/dump.
6. **Constraint:** the relay already hits HL 429s from the Hetzner IP, so snapshots must go through the daemon's shared,
   rate-limited client and be cached; fix the 429 handling before adding any polling.

## Open question for the user
Ask the creator (@bannedsleepingX) for permission or an API if the site's own numbers (not a rebuild) are wanted.
