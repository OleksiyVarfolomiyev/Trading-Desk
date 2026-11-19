// The desk relay and web dashboard on our own server (Hetzner), in place of Cloudflare.
//
// It runs web/worker.js unchanged: the same endpoints, answers and scrubbing. This file only gives
// the Worker what Cloudflare used to:
//   * storage: each key Cloudflare kept in KV is a JSON file in DESK_STORE (default ~/.trading-desk/relay);
//   * the 6-hourly history capture (the Worker's cron), on a timer;
//   * the page: the files web/build_site.sh assembles into web/site;
//   * a login: a browser signs in on the desk's own sign-in page and keeps a session cookie for 30 days
//     (the browser's built-in basic-auth box is not used: Brave, for one, never shows it). Scripts use
//     the relay token (the Mac's pushes and reads) or basic auth. Requests from this machine itself (the
//     daemon, the daily job) need none of it.
//
// Config: DESK_RELAY_CONFIG (default ~/.trading-desk/relay.json, readable only by the desk user):
//   {"wallets": {"07f2": "0x..."}, "ingest_token": "...", "user": "desk", "password_scrypt": "salt:hash"}
//
//   node web/server.mjs                 serve on 127.0.0.1:8787 (PORT to change); Caddy proxies HTTPS to it
//   node web/server.mjs --set-password  ask for the dashboard password and store its scrypt hash

import http from "node:http";
import net from "node:net";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

import worker from "./worker.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HOME = os.homedir();
const CONFIG_FILE = process.env.DESK_RELAY_CONFIG || path.join(HOME, ".trading-desk", "relay.json");
const STORE = process.env.DESK_STORE || path.join(HOME, ".trading-desk", "relay");
const SITE = process.env.DESK_SITE || path.join(HERE, "site");
const PORT = Number(process.env.PORT || 8787);
const CAPTURE_EVERY_MS = 6 * 3600_000;  // the Worker's cron ran every 6 hours
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
  ".ico": "image/x-icon" };

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
  } catch (error) {
    return {};
  }
}

function writeConfig(config) {
  fs.mkdirSync(path.dirname(CONFIG_FILE), { recursive: true });
  const temporary = CONFIG_FILE + ".tmp";
  fs.writeFileSync(temporary, JSON.stringify(config, null, 1), { mode: 0o600 });
  fs.renameSync(temporary, CONFIG_FILE);
}

// ---- KV in files: one JSON file per key ----
const fileFor = (key) => path.join(STORE, encodeURIComponent(key) + ".json");
const kv = {
  async get(key, type) {
    try {
      const text = await fsp.readFile(fileFor(key), "utf8");
      return type === "json" ? JSON.parse(text) : text;
    } catch (error) {
      return null;
    }
  },
  async put(key, value) {
    await fsp.mkdir(STORE, { recursive: true });
    const target = fileFor(key), temporary = `${target}.${process.pid}.tmp`;
    await fsp.writeFile(temporary, typeof value === "string" ? value : JSON.stringify(value), { mode: 0o600 });
    await fsp.rename(temporary, target);  // whole files only: a reader never sees half a write
  },
};

function environment() {
  const config = readConfig();
  return { DESK: kv, WALLETS: JSON.stringify(config.wallets || {}), INGEST_TOKEN: config.ingest_token || "" };
}

const context = { waitUntil: (promise) => { Promise.resolve(promise).catch(() => {}); } };

// ---- login ----
function sameText(a, b) {
  const x = Buffer.from(String(a)), y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

function passwordMatches(config, given) {
  const [salt, hash] = String(config.password_scrypt || "").split(":");
  if (!salt || !hash) return false;
  return sameText(crypto.scryptSync(given, Buffer.from(salt, "hex"), 32).toString("hex"), hash);
}

// A session is "expiry.signature", signed with a key derived from the stored password hash: changing the
// password signs everyone out.
const SESSION_COOKIE = "desk_session";
const SESSION_DAYS = 30;
const failures = new Map();   // address -> [times of failed sign-ins], to slow guessing

function sessionKey(config) {
  return crypto.createHash("sha256").update("desk-session:" + String(config.password_scrypt || "")).digest();
}

function makeSession(config) {
  const expiry = String(Date.now() + SESSION_DAYS * 86400_000);
  return `${expiry}.${crypto.createHmac("sha256", sessionKey(config)).update(expiry).digest("hex")}`;
}

function sessionValid(request, config) {
  if (!config.password_scrypt) return false;
  const cookies = Object.fromEntries(String(request.headers.cookie || "").split(";").map((part) => {
    const at = part.indexOf("=");
    return at > 0 ? [part.slice(0, at).trim(), part.slice(at + 1).trim()] : ["", ""];
  }));
  const [expiry, signature] = String(cookies[SESSION_COOKIE] || "").split(".");
  if (!expiry || !signature || Number(expiry) < Date.now()) return false;
  return sameText(signature, crypto.createHmac("sha256", sessionKey(config)).update(expiry).digest("hex"));
}

function clientAddress(request) {
  return String(request.headers["x-forwarded-for"] || request.socket.remoteAddress || "").split(",")[0].trim();
}

function tooManyFailures(address) {
  const recent = (failures.get(address) || []).filter((time) => Date.now() - time < 15 * 60_000);
  failures.set(address, recent);
  return recent.length >= 10;
}

function loginPage(message = "") {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Trading Desk</title>
<style>
:root { --bg: #f6f7f9; --card: #fff; --text: #16181d; --muted: #6b7280; --line: #d9dde3; --accent: #2563eb; --bad: #c62828; }
@media (prefers-color-scheme: dark) { :root { --bg: #0f1115; --card: #171a21; --text: #e8eaee; --muted: #9aa1ad; --line: #2a2f3a; --accent: #5b8def; --bad: #ef5350; } }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--text);
  font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; padding: 16px; }
form { width: 100%; max-width: 320px; background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 24px; }
h1 { font-size: 18px; margin: 0 0 16px; }
label { display: block; font-size: 13px; color: var(--muted); margin: 12px 0 4px; }
input { width: 100%; font: inherit; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--text); }
button { width: 100%; margin-top: 20px; padding: 10px; font: inherit; font-weight: 600; border: 0; border-radius: 8px; background: var(--accent); color: #fff; cursor: pointer; }
.message { color: var(--bad); font-size: 13px; margin-top: 12px; min-height: 1em; }
</style></head><body>
<form method="post" action="/login">
<h1>Trading Desk</h1>
<label for="user">User</label><input id="user" name="user" autocomplete="username" value="desk" required>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button>
<div class="message">${message}</div>
</form></body></html>`;
}

async function signIn(request, response) {
  const config = readConfig(), address = clientAddress(request);
  if (tooManyFailures(address)) {
    response.writeHead(429, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
    response.end(loginPage("Too many attempts. Try again in 15 minutes."));
    return;
  }
  const form = new URLSearchParams((await readBody(request)).toString("utf8"));
  if (sameText(form.get("user") || "", config.user || "desk") && passwordMatches(config, form.get("password") || "")) {
    failures.delete(address);
    response.writeHead(303, { Location: "/", "Cache-Control": "no-store",
      "Set-Cookie": `${SESSION_COOKIE}=${makeSession(config)}; Path=/; Max-Age=${SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax` });
    response.end();
    return;
  }
  failures.get(address).push(Date.now());
  await new Promise((resolve) => setTimeout(resolve, 1000));   // a failed attempt costs a second
  response.writeHead(401, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
  response.end(loginPage("Wrong user or password."));
}

function allowed(request) {
  const header = request.headers.authorization || "";
  // This machine itself (the daemon, the daily job): Caddy's proxied requests always carry X-Forwarded-For.
  const address = request.socket.remoteAddress || "";
  if (!request.headers["x-forwarded-for"] && (address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1")) {
    return true;
  }
  const config = readConfig();
  if (/^Bearer\s+/i.test(header)) {
    return Boolean(config.ingest_token) && sameText(header.replace(/^Bearer\s+/i, ""), config.ingest_token);
  }
  if (/^Basic\s+/i.test(header)) {
    const decoded = Buffer.from(header.replace(/^Basic\s+/i, ""), "base64").toString("utf8");
    const split = decoded.indexOf(":");
    return split > 0 && sameText(decoded.slice(0, split), config.user || "desk") && passwordMatches(config, decoded.slice(split + 1));
  }
  return sessionValid(request, config);
}

// ---- the page ----
async function staticFile(pathname) {
  const relative = pathname === "/" ? "index.html" : decodeURIComponent(pathname).replace(/^\/+/, "");
  const full = path.resolve(SITE, relative);
  if (!full.startsWith(path.resolve(SITE) + path.sep)) return null;  // nothing outside web/site
  try {
    const stat = await fsp.stat(full);
    if (!stat.isFile()) return null;
    return { body: await fsp.readFile(full), type: TYPES[path.extname(full)] || "application/octet-stream" };
  } catch (error) {
    return null;
  }
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks);
}

// What the reader does with the dashboard (marking a highlight read or not important, hiding or moving a section, the
// look): one JSON line per event in DESK_FEEDBACK (default ~/.trading-desk/feedback.jsonl), read back by
// research/feedback_summary.py. The browser sends a small batch; anything malformed is dropped.
const FEEDBACK_FILE = process.env.DESK_FEEDBACK || path.join(HOME, ".trading-desk", "feedback.jsonl");
const HIGHLIGHT_STATE_FILE = process.env.DESK_HIGHLIGHT_STATE || path.join(HOME, ".trading-desk", "highlight_state.json");
async function highlightState(request, response) {
  if (request.method === "GET") {
    let text = '{"read":{},"deleted":[]}';
    try { text = await fsp.readFile(HIGHLIGHT_STATE_FILE, "utf8"); } catch (error) { /* no answers yet */ }
    response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    response.end(text);
    return;
  }
  if (request.method !== "POST") return response.writeHead(405).end();
  let state = {};
  try { const raw = await readBody(request); state = raw.length < 128_000 ? JSON.parse(raw.toString("utf8")) : {}; } catch (error) { state = {}; }
  if (!state || typeof state.read !== "object" || !Array.isArray(state.deleted)) {
    response.writeHead(400, { "Content-Type": "application/json" }); response.end('{"error":"bad state"}'); return;
  }
  await fsp.mkdir(path.dirname(HIGHLIGHT_STATE_FILE), { recursive: true });
  await fsp.writeFile(HIGHLIGHT_STATE_FILE, JSON.stringify({ read: state.read, deleted: state.deleted.filter((x) => typeof x === "string").slice(0, 500) }));
  response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" }); response.end('{"ok":true}');
}
async function saveFeedback(request, response) {
  const raw = await readBody(request);
  let events = [];
  try { events = raw.length < 64_000 ? (JSON.parse(raw.toString("utf8")).events || []) : []; } catch (error) { events = []; }
  const clean = events.slice(0, 200).filter((event) => event && typeof event.kind === "string" && typeof event.action === "string")
    .map((event) => JSON.stringify({ received: new Date().toISOString(), source: "web", ...event }));
  if (clean.length) await fsp.appendFile(FEEDBACK_FILE, clean.join("\n") + "\n");
  response.writeHead(200, { "Content-Type": "application/json" });
  response.end(JSON.stringify({ ok: true, saved: clean.length }));
}

// ---- Dashboard actions on an open HL position (close, reduce or add a share of it), through the trading daemon.
// Two steps, each a click on the page: "plan" runs the daemon's dry run and returns its plan with a one-time token;
// "confirm" (within two minutes, same request) sends it live, the click standing in for the typed confirmations. Every
// confirm is written to ~/.trading-desk/web_actions.log. The daemon's own caps, kill switch and guards apply as always.
const DAEMON_SOCKET = path.join(HOME, ".trading-desk", "hl_daemon.sock");
const ACTIONS_LOG = path.join(HOME, ".trading-desk", "web_actions.log");
const pendingActions = new Map();
let actionRunning = false;

function daemonCall(message, answer) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ path: DAEMON_SOCKET });
    let buffer = "", text = "", code = 2, data;
    const finish = () => { socket.destroy(); resolve({ code, text, data }); };
    socket.setEncoding("utf8");
    socket.setTimeout(message.batch ? 360_000 : 240_000, finish);
    socket.on("connect", () => socket.write(JSON.stringify(message) + "\n"));
    socket.on("data", (chunk) => {
      buffer += chunk;
      let at;
      while ((at = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, at); buffer = buffer.slice(at + 1);
        let item; try { item = JSON.parse(line); } catch (error) { continue; }
        if (item.t === "out") text += item.text;
        else if (item.t === "data") data = item.data;
        else if (item.t === "ask") { text += item.text + "\n"; socket.write(JSON.stringify({ t: "answer", text: answer ? answer(item.text) : "" }) + "\n"); }
        else if (item.t === "end") { code = Number(item.code); finish(); }
      }
    });
    socket.on("error", (error) => { text += `The trading daemon could not be reached (${error.code || error.message}).`; finish(); });
    socket.on("end", finish);
  });
}

async function dashboardAction(request, response) {
  const send = (status, body) => { response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); response.end(JSON.stringify(body)); };
  const origin = request.headers.origin;
  if (origin && origin.replace(/^https?:\/\//, "") !== request.headers.host) return send(403, { error: "cross-site request refused" });
  let body = {};
  try { body = JSON.parse((await readBody(request)).toString("utf8") || "{}"); } catch (error) { return send(400, { error: "not JSON" }); }
  if (!body || typeof body !== "object") return send(400, { error: "bad request" });
  const { stage, change, coin, wallet, token, signals } = body;
  const batch = change === "batch";
  const entry = change === "short" || change === "long";   // a new entry from the trade bar: no fraction, an optional journal label
  const fraction = entry && body.fraction === undefined ? 1 : Number(body.fraction);
  const label = ["directional", "conviction", "hedge", "fade"].includes(body.label) ? body.label : "";
  const market = /^[A-Za-z0-9_]{1,24}(?::[A-Za-z0-9_]{1,24})?$/;
  if (!/^[0-9a-fA-F]{4}$/.test(String(wallet || ""))) return send(400, { error: "invalid HL wallet" });
  if (batch ? (!Array.isArray(signals) || signals.length < 2 || signals.length > 10 ||
      signals.some((row) => !row || typeof row.coin !== "string" || !market.test(row.coin) ||
        !["LONG", "SHORT"].includes(row.side) || typeof row.dex !== "string" ||
        (row.dex !== "" && !/^[A-Za-z0-9_]{1,24}$/.test(row.dex))))
    : (!["close", "reduce", "add", "short", "long"].includes(change) ||
      !/^[A-Za-z0-9:]{1,24}$/.test(String(coin || "")) || !(fraction > 0 && fraction <= 1))) {
    return send(400, { error: batch ? "Enter 2–10 valid market and side rows." : "bad request" });
  }
  const request_ = batch ? { cmd: "trade", wallet, batch: signals.map(({coin, side, dex}) => ({coin, side, dex})), fast: true }
    : { cmd: "resize", wallet, coin, change, fraction, label };
  const key = JSON.stringify(request_);
  const outputLimit = batch ? 12000 : 4000;
  if (actionRunning) return send(409, { error: "another dashboard action is running" });
  actionRunning = true;
  try {
    if (stage === "plan") {
      const { code, text, data } = await daemonCall({ ...request_, live: false });
      const id = code === 0 && (!batch || data?.limits) ? crypto.randomBytes(12).toString("hex") : null;
      const now = Date.now();
      for (const [expiredId, pending] of pendingActions) {
        if (pending.expires <= now) pendingActions.delete(expiredId);
      }
      if (id) pendingActions.set(id, { key, expires: now + 120_000, limits: data?.limits });
      return send(200, { code, plan: text.slice(-outputLimit), token: id });
    }
    if (stage === "confirm") {
      const pending = pendingActions.get(String(token || ""));
      pendingActions.delete(String(token || ""));
      if (!pending || pending.key !== key || pending.expires < Date.now()) return send(400, { error: "the plan expired or changed: plan it again" });
      const { code, text } = await daemonCall({ ...request_, live: true, limits: pending.limits }, (prompt) =>
        prompt.includes("SUBMIT ORDERS") ? "SUBMIT ORDERS" : prompt.includes("SUBMIT POSITION ACTIONS") ? "SUBMIT POSITION ACTIONS" : prompt.includes("LIVE TRADING") ? "LIVE TRADING" : "");
      await fsp.appendFile(ACTIONS_LOG, JSON.stringify({ at: new Date().toISOString(), change, coin, wallet, fraction,
        signals: request_.batch, code,
        result: text.slice(-600) }) + "\n").catch(() => {});
      return send(200, { code, result: text.slice(-outputLimit) });
    }
    return send(400, { error: "stage must be plan or confirm" });
  } finally {
    actionRunning = false;
  }
}

async function handle(request, response) {
  const url = new URL(request.url, "http://localhost");
  if (url.pathname === "/login" && request.method === "POST") return signIn(request, response);
  if (url.pathname === "/logout") {
    response.writeHead(303, { Location: "/", "Set-Cookie": `${SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax` });
    response.end();
    return;
  }
  if (!allowed(request)) {
    // A page request gets the sign-in page; anything else (the page's data calls, scripts) a plain 401, with no
    // basic-auth challenge, so no browser pops up (or silently withholds) its own login box.
    const page = request.method === "GET" && String(request.headers.accept || "").includes("text/html");
    response.writeHead(page ? 200 : 401, { "Content-Type": page ? "text/html; charset=utf-8" : "text/plain", "Cache-Control": "no-store" });
    response.end(page ? loginPage() : "Sign in to see the desk.");
    return;
  }
  if (url.pathname === "/feedback" && request.method === "POST") return saveFeedback(request, response);
  if (url.pathname === "/highlight_state") return highlightState(request, response);
  if (url.pathname === "/action" && request.method === "POST") return dashboardAction(request, response);
  if (url.pathname === "/ladder" && request.method === "POST") {   // stop a market-making ladder, or start a neutral one (the confirmations are the click)
    let body = {};
    try { body = JSON.parse((await readBody(request)).toString("utf8") || "{}"); } catch (error) { body = {}; }
    const wallet = String(body.wallet || ""), coin = String(body.coin || "");
    const send = (status, payload) => { response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); response.end(JSON.stringify(payload)); };
    if (!/^[0-9a-fA-F]{4}$/.test(wallet) || !/^[A-Za-z0-9:]{1,24}$/.test(coin) || !["stop", "neutral"].includes(body.mode)) return send(400, { error: "bad request" });
    if (actionRunning) return send(409, { error: "another dashboard action is running" });
    actionRunning = true;
    try {
      const message = { cmd: "ladder", wallet, coin, ...(body.mode === "stop" ? { stop: true } : { neutral: true }) };
      const { code, text } = await daemonCall(message, (prompt) => (prompt.includes("START LADDER") ? "START LADDER" : ""));
      await fsp.appendFile(ACTIONS_LOG, JSON.stringify({ at: new Date().toISOString(), ladder: body.mode, coin, wallet, code, result: text.slice(-400) }) + "\n").catch(() => {});
      return send(200, { code, result: text.slice(-1500) });
    } finally { actionRunning = false; }
  }
  if (url.pathname === "/pull" && request.method === "POST") {   // a notebook section onto the dashboard, built by the daemon
    let body = {};
    try { body = JSON.parse((await readBody(request)).toString("utf8") || "{}"); } catch (error) { body = {}; }
    const id = String(body.id || "");
    const ok = /^[a-z_]+:[a-z0-9-]+$/.test(id);
    const { text } = ok ? await daemonCall(body.stage === "status" ? { cmd: "pull_status" } : { cmd: "pull", id }) : { text: "{}" };
    let parsed = {};
    try { parsed = JSON.parse(text.trim().split("\n").pop() || "{}"); } catch (error) { parsed = {}; }
    const status = body.stage === "status" ? parsed[id] || "" : parsed.status || "";
    response.writeHead(ok ? 200 : 400, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    response.end(JSON.stringify({ status }));
    return;
  }
  if (url.pathname === "/usage_model" && request.method === "GET") {   // fitted hourly by the daemon from the feedback log (desk/usage_model.py)
    let text = '{"areas":{},"tiles":{},"quiet":[]}';
    try { text = await fsp.readFile(path.join(HOME, ".trading-desk", "usage_model.json"), "utf8"); } catch (error) { /* none yet */ }
    response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-cache" });
    response.end(text);
    return;
  }
  if (request.method === "GET") {
    const file = await staticFile(url.pathname);
    if (file) {
      response.writeHead(200, { "Content-Type": file.type, "Cache-Control": "no-cache" });
      response.end(file.body);
      return;
    }
  }
  const body = request.method === "POST" ? await readBody(request) : undefined;
  const headers = new Headers();
  for (const [name, value] of Object.entries(request.headers)) {
    if (["host", "connection", "content-length", "transfer-encoding"].includes(name)) continue;  // set by Node itself
    if (value !== undefined) headers.set(name, Array.isArray(value) ? value.join(", ") : value);
  }
  const answer = await worker.fetch(new Request(`http://${request.headers.host || "localhost"}${request.url}`,
    { method: request.method, headers, body }), environment(), context);
  const out = {};
  answer.headers.forEach((value, name) => { out[name] = value; });
  response.writeHead(answer.status, out);
  response.end(Buffer.from(await answer.arrayBuffer()));
}

function serve() {
  const server = http.createServer((request, response) => {
    handle(request, response).catch((error) => {
      console.error(new Date().toISOString(), "request failed:", error && error.stack || error);
      if (!response.headersSent) response.writeHead(500, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: "the relay failed to answer" }));
    });
  });
  server.listen(PORT, "127.0.0.1", () => console.log(`${new Date().toISOString()} desk relay on 127.0.0.1:${PORT}, store ${STORE}`));
  const capture = () => worker.scheduled(null, environment(), context);
  setTimeout(capture, 60_000);              // one capture a minute after start...
  setInterval(capture, CAPTURE_EVERY_MS);   // ...then every 6 hours, as the cron did
}

async function setPassword() {
  const ask = (question) => new Promise((resolve) => {
    const prompt = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    prompt._writeToOutput = (text) => { if (text.includes(question)) process.stdout.write(text); };  // no echo
    prompt.question(question, (answer) => { prompt.close(); process.stdout.write("\n"); resolve(answer); });
  });
  const first = await ask("New dashboard password: ");
  const again = await ask("Again: ");
  if (!first || first !== again) {
    console.error("The passwords were empty or did not match; nothing changed.");
    process.exit(1);
  }
  const salt = crypto.randomBytes(16);
  const config = readConfig();
  config.user = config.user || "desk";
  config.password_scrypt = `${salt.toString("hex")}:${crypto.scryptSync(first, salt, 32).toString("hex")}`;
  writeConfig(config);
  console.log(`Saved. Sign in as "${config.user}" with this password.`);
}

if (process.argv.includes("--set-password")) setPassword();
else serve();
