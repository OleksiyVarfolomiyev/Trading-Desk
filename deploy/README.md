# The desk server

The desk runs on one small Hetzner server: the HL trading daemon (guards, ladders, held
take-profits, alerts, research), the relay and its stored data, the web dashboard, and the daily
sync. Only one thing stays on the Mac: reading IB through TWS, which a launchd job sends to the
server every ten minutes (`python -m desk.web_sync --install-ib-push`). The notebooks stay on the
Mac too.

| Piece | Where | How it runs |
| --- | --- | --- |
| Trading daemon | server | `desk-daemon.service`; IB comes from the Mac's last push (`DESK_IB_SOURCE=relay`) |
| Relay and web dashboard | server | `desk-relay.service`: `web/server.mjs` runs `web/worker.js` unchanged; data in `~/.trading-desk/relay`; HTTPS by Caddy on a sslip.io name; password login |
| HL history capture (every 6 hours) | server | inside the relay, as the Worker's cron did |
| Daily sync (08:00 and 23:30 ET) | server | `desk-daily.timer` |
| Code updates | server | `desk-deploy.timer` pulls `main` every two minutes and restarts the relay; the daemon restarts only by hand |
| IB through TWS | Mac | `com.trading-desk.ib-push` (every ten minutes) |

## Steps

1. **Server** (you): a Hetzner Cloud server in Singapore, Ubuntu 24.04, the smallest shared plan,
   with your SSH key. Note its IPv4 address (`SERVER` below).
2. **Setup** (as root, once): `ssh root@SERVER 'bash -s' < deploy/setup_server.sh`. It prints the
   dashboard address and a deploy key.
3. **Deploy key** (you): GitHub, the repository, Settings, Deploy keys, Add: paste the key, leave
   "Allow write access" off.
4. **Code and services**: `ssh root@SERVER 'bash -s' < deploy/install_desk.sh`. The relay and the
   timers start; the daemon does not.
5. **Relay data**: `python deploy/migrate.py --host SERVER --prepare` (safe to repeat).
6. **Dashboard password** (you): `ssh -t desk@SERVER 'node trading-desk/web/server.mjs --set-password'`,
   then sign in at the address from step 2 as `desk`.
7. **HL key** (you): `ssh -t desk@SERVER 'cd trading-desk && .venv/bin/python execution/hl_trade.py --wallet 07f2 --store-key'`.
   It is kept in a file only the desk user can read.
8. **Cutover**:
   1. Stop the daemon on the Mac: `printf 'STOP DAEMON\n' | .venv/bin/python execution/hl_daemon.py stop`.
   2. `python deploy/migrate.py --host SERVER --state`.
   3. Start it on the server: `ssh desk@SERVER 'sudo systemctl start desk-daemon'`, then check
      `ssh desk@SERVER '.venv/bin/python execution/hl_daemon.py status'`: every guard, ladder and
      held take-profit resumed.
   4. On the Mac: point the Mac's pushes at the server, install the IB push:
      `python -m desk.web_sync --install-ib-push` (with the new relay address as the default in
      `desk/web_sync.py`).
9. **Retire Cloudflare** once the server has run cleanly for a few days: delete the `desk-relay`
   Worker and its KV namespace, and `wrangler.jsonc`.

## Every day

- Daemon commands: `ssh desk@SERVER`, then the usual `.venv/bin/python execution/hl_daemon.py status`
  (or `ladder`, `reattach`, ...). The desk settings load with the login.
- Restart the daemon after a trading-code change: `ssh desk@SERVER 'sudo systemctl restart desk-daemon'`.
- Logs: `journalctl -u desk-daemon -f`, `journalctl -u desk-relay`, and `~/.trading-desk/hl_daemon.log`.
