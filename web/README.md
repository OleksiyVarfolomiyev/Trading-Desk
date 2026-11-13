# Desk dashboard on the web

An always-on copy of the desk dashboard, behind a password, on the desk server
(https://5-223-72-14.sslip.io, user `desk`). It shows the HL wallets, their tokens on Base and
Ethereum, and IB as the Mac last sent it. Setting up the server and running it is in
[deploy/README.md](../deploy/README.md).

## How it works

`web/server.mjs` (the `desk-relay` service) serves both the page and the account data, behind
Caddy's HTTPS. The server pulls `main` every two minutes and restarts the relay, so code changes go
live without copying anything.

- **The page:** `web/build_site.sh` assembles it into `web/site` on every pull. It takes
  `index.html` and `live.js` from `web/`, and `app.js` and `app.css` from `desk/dashboard_static`,
  so the web copy always looks like the local dashboard. `live.js` builds the same data the local
  dashboard server serves, and the dashboard's renderer draws it. It refreshes every minute.
- **Market data** (prices, 24-hour moves, daily candles, funding) comes straight from HL. Token
  prices come from DefiLlama, and Plotly from its CDN. None of these needs a wallet address.
- **Account data** comes from the relay (`web/worker.js`, run by `web/server.mjs`), which holds the
  wallet addresses in `~/.trading-desk/relay.json` on the server. It lists the wallets by their last
  four characters (`/wallets`). For each wallet it answers three questions:
  - positions and margin on every HL DEX;
  - HL's value and PnL history;
  - token balances on Base and Ethereum, read through free public nodes in one Multicall3 call
    per chain.

  It removes the address and every transaction hash from its answers.

## Saved history and IB

The relay keeps its data as JSON files in `~/.trading-desk/relay`:

- **HL daily history.** Every 6 hours the relay saves each wallet's last point of every day, so the
  page keeps daily detail beyond HL's one-month window. `/sync` shows the last run.
- **IB.** Only the desk's Mac can read IB, through TWS. Its launchd job
  (`python -m desk.web_sync --install-ib-push`) sends balances, positions, open risk and daily NAV
  to `POST /ingest/ib` every ten minutes, with the relay token kept in the Keychain.

## Trying it locally

```bash
bash web/build_site.sh
.venv/bin/python web/dev_relay.py      # page and relay on http://127.0.0.1:8787, reading accounts.json
```
