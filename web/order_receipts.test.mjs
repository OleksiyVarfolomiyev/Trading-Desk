import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { EventEmitter, once } from "node:events";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { test } from "node:test";

test("confirmed orders survive relay restart without a second live submission", { timeout: 15000 }, async () => {
  const home = await mkdtemp(path.join(tmpdir(), "desk-receipt-"));
  const runtime = path.join(home, ".trading-desk");
  await mkdir(runtime);
  let submitCount = 0;
  let liveSocket;
  const events = new EventEmitter();
  const daemon = net.createServer((socket) => {
    const lines = createInterface({ input: socket });
    lines.once("line", (line) => {
      if (JSON.parse(line).live) {
        submitCount++;
        liveSocket = socket;
        events.emit("submitted");
      } else {
        socket.end('{"t":"out","text":"Mock plan"}\n{"t":"end","code":0}\n');
      }
    });
  });
  daemon.listen(path.join(runtime, "hl_daemon.sock"));
  await once(daemon, "listening");
  const relays = [];
  async function startRelay() {
    const relay = spawn(process.execPath, ["web/server.mjs"], {
      env: { ...process.env, HOME: home, PORT: "0", DESK_STORE: path.join(runtime, "store"),
        DESK_RELAY_CONFIG: path.join(runtime, "relay.json") },
    });
    relays.push(relay);
    for await (const line of createInterface({ input: relay.stdout })) {
      const match = /desk relay on 127\.0\.0\.1:(\d+)/.exec(line);
      if (match) return { relay, base: "http://127.0.0.1:" + match[1] };
    }
    throw new Error("relay failed to start");
  }
  const payload = { change: "long", wallet: "a123", coin: "BTC", label: "directional" };
  const post = (base, stage, token) => fetch(base + "/action", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, stage, token }),
  }).then((response) => response.json());
  const status = (base, token) => fetch(base + "/action/status?id=" + token).then((response) => response.json());
  try {
    const { relay, base } = await startRelay();
    const { token } = await post(base, "plan");
    assert.match(token, /^[a-f\d]{24}$/);
    const submitted = once(events, "submitted");
    const reply = post(base, "confirm", token);
    await submitted;
    assert.equal((await status(base, token)).state, "processing");
    const exited = once(relay, "exit");
    relay.kill("SIGTERM");
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal(relay.exitCode, null);
    liveSocket.end('{"t":"out","text":"Mock fill"}\n{"t":"end","code":0}\n');
    assert.deepEqual(await reply, { id: token, state: "complete", code: 0, result: "Mock fill" });
    await exited;
    const restarted = await startRelay();
    assert.deepEqual(await status(restarted.base, token), await post(restarted.base, "confirm", token));
    assert.equal(submitCount, 1);

    const interrupted = await post(restarted.base, "plan");
    const secondSubmitted = once(events, "submitted");
    const disconnected = assert.rejects(post(restarted.base, "confirm", interrupted.token));
    await secondSubmitted;
    const crashed = once(restarted.relay, "exit");
    restarted.relay.kill("SIGKILL");
    await crashed;
    await disconnected;
    liveSocket.destroy();

    const recovered = await startRelay();
    assert.equal((await status(recovered.base, interrupted.token)).state, "unknown");
    assert.equal((await post(recovered.base, "confirm", interrupted.token)).state, "unknown");
    assert.equal(submitCount, 2);
  } finally {
    liveSocket?.destroy();
    for (const relay of relays) relay.kill("SIGKILL");
    await new Promise((resolve) => daemon.close(resolve));
    await rm(home, { force: true, recursive: true });
  }
});
