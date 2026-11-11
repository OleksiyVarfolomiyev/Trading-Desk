# Desk dashboard on the web

A public, always-on copy of the desk dashboard. It shows the HL wallets and their tokens on Base
and Ethereum, live: each visitor's browser reads the data itself, so nothing on the desk's Mac has
to run. IB is not included, because it can only be read through TWS on the desk's own machine.

## How it works

One Cloudflare Worker, `desk-relay`, serves both the dashboard and the account data. It is built
from this repository: every push to `main` redeploys it (Cloudflare Workers Builds, configured by
`wrangler.jsonc` at the repository root). Code changes go live without any copying or pasting.

- **The page:** `web/build_site.sh` assembles it into `web/site` before each deploy. It takes
  `index.html` and `live.js` from `web/`, and `app.js` and `app.css` from `desk/dashboard_static`,
  so the web copy always looks like the local dashboard. `live.js` builds the same data the local
  dashboard server serves, and the dashboard's renderer draws it. It refreshes every minute.
- **Market data** (prices, 24-hour moves, daily candles, funding) comes straight from HL. Token
  prices come from DefiLlama, and Plotly from its CDN. None of these needs a wallet address.
- **Account data** comes from the Worker (`web/worker.js`), which holds the wallet addresses in
  its `WALLETS` secret. It lists the wallets by their last four characters (`/wallets`). For each
  wallet it answers three questions:
  - positions and margin on every HL DEX;
  - HL's value and PnL history;
  - token balances on Base and Ethereum, read through free public nodes in one Multicall3 call
    per chain.

  It removes the address and every transaction hash from its answers.

Exact position sizes and entry prices are still shown. Someone determined could match them
against HL's public trade records to find the wallets. Hide those columns if that matters.

## One-time setup

1. **The secret.** In Cloudflare, open the `desk-relay` Worker → Settings → Variables and
   Secrets → Add. Choose Type Secret, Name `WALLETS`, and paste its value straight from the
   clipboard after running:

   ```bash
   /Users/admin/anaconda3/envs/py314/bin/python web/build.py --wallets-secret | pbcopy
   ```

   Then click Deploy.
2. **Connect the repository.** In the `desk-relay` Worker, go to Settings → Builds → Connect,
   choose GitHub, and allow Cloudflare to read this repository. Use these settings:
   - branch `main`;
   - build command empty (`wrangler.jsonc` runs `web/build_site.sh`);
   - deploy command `npx wrangler deploy`;
   - root directory `/`.

   The name in `wrangler.jsonc` must stay `desk-relay`, the Worker's name.

The dashboard is at `https://desk-relay.<your-subdomain>.workers.dev/`. The secret is kept
across deploys.

## Saved history and IB

The relay keeps data in a KV namespace bound as `DESK` (`wrangler.jsonc`):

- **HL daily history.** A Cron Trigger runs every 6 hours and saves each wallet's last point of
  every day, so the page keeps daily detail beyond HL's one-month window. `/sync` shows the last run.
- **IB.** Only the desk's Mac can read IB, through TWS. It sends balances, positions, open risk and
  daily NAV to `POST /ingest/ib`, using the `INGEST_TOKEN` secret, which is also kept in the Keychain.

Setup, once:

```bash
python -m desk.web_sync --store-token | pbcopy   # paste as the Worker secret INGEST_TOKEN (type Secret)
python -m desk.web_sync --seed-history           # send the saved local HL history
/Users/admin/anaconda3/envs/py314/bin/python -m desk.web_sync --install-daily
```

The daily job (launchd) runs at 08:00 and 23:30. If the Mac is asleep then, it runs at the next
wake. Each run saves the HL history and IB Flex NAV locally and sends IB. When TWS is closed it
sends the new NAV and keeps the last positions. The log is `~/.trading-desk/daily_sync.log`.
While the local dashboard runs, it also sends IB every 10 minutes. Remove the job with
`--uninstall-daily`.

## Trying it locally

```bash
bash web/build_site.sh
.venv/bin/python web/dev_relay.py      # page and relay on http://127.0.0.1:8787, reading accounts.json
```

Free-plan limits are far above one desk's use. Cloudflare Workers allow 100,000 requests a day,
an open page uses about 4 a minute, and serving the page's own files does not count.
