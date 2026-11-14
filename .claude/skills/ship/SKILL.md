---
name: ship
description: Commit and push the pending desk changes, wait for the Hetzner server to pull them, restart desk-daemon and desk-relay, and check they came back. The user's shorthand for "commit and push, restart".
---

# Ship

The user's standing request, "commit and push, restart", in one step.

1. **Test first.** Run `/Users/admin/anaconda3/envs/py314/bin/python tests/run_affected.py`. If any `web/*.js` or `desk/dashboard_static/*.js` file changed, syntax-check it on the server, which has node: `cat FILE | ssh desk "cat > /tmp/c.js && node --check /tmp/c.js"`. If anything fails, stop and report. Never ship red.
2. **Commit only the work of this session.** Stage the specific files; never `research/data/*` (those are local data refreshes). Write a message that says what the reader sees change, and end it with the attribution trailer from the system reminder. Push to `main`.
3. **Deploy.** The server pulls `main` every 2 minutes. Use Monitor to wait until `ssh desk 'cd trading-desk && git rev-parse --short HEAD'` equals the pushed commit. Then run `ssh desk 'sudo -n /usr/bin/systemctl restart desk-daemon && sudo -n /usr/bin/systemctl restart desk-relay'`.
4. **Verify.** Run `systemctl is-active desk-daemon desk-relay`. Where the change is visible on the relay (alerts, regime, research), read it back once the daemon has pushed.
5. **Report.** Give the commit hash, what is now live, and anything that failed, in a few lines.
