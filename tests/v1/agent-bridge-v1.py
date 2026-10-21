#!/usr/bin/env python3
"""agent-bridge: a tiny, private, signed inbox between Nate's OMP sessions (Mac) and his Grok Bots (box).

One stdlib-only file, no dependencies. Two sides:
  box  (Grok Bot box): inbox for bots dross/scout/researcher, stdio MCP for Grok Bot, imports signed tokens.
  mac  (MacBook Pro):  inbox for OMP sessions (omp/<name>), stdio MCP for omp, wakes Grok Bot via a webhook.

Rules baked in: messages are saved before any delivery attempt; every message is HMAC-signed with a shared
secret and verified on import; nothing received is ever executed (no shell, no injection by default); every
message shown to an agent carries a "From agent X" prefix; an OFF switch stops everything;
secrets are never printed.
"""
import base64, hashlib, hmac, json, os, random, re, secrets, shutil, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

VERSION = "0.1.3"
MAX_BODY = 32 * 1024
BOTS = ("dross", "scout", "researcher")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
ID_RE = re.compile(r"^[0-9]{13}-[0-9a-f]{8}$")
DELAYS = [5, 15, 45, 120, 300, 600]           # webhook retry backoff (seconds), plus jitter
PREFIX = "[agent-bridge] From agent {frm}"


def home() -> Path:
    if os.environ.get("AB_HOME"):
        return Path(os.environ["AB_HOME"]).expanduser()
    return Path("/workspace/agent-bridge/data") if Path("/workspace").is_dir() and sys.platform != "darwin" \
        else Path("~/.agent-bridge").expanduser()


H = home()


def die(msg, code=1):
    print(f"agent-bridge: {msg}", file=sys.stderr)
    sys.exit(code)


def cfg():
    p = H / "config.json"
    if not p.exists():
        die(f"not initialised ({p} missing); run: agent-bridge init --side box|mac")
    return json.loads(p.read_text())


def is_off():
    return (H / "OFF").exists()


def guard_on():
    if is_off():
        die("bridge is OFF (remove with: agent-bridge on)", 3)


def key() -> bytes:
    p = H / "secret"
    if not p.exists():
        die(f"shared secret missing at {p} (0600). See agent-bridge.md 'secrets'.")
    if p.stat().st_mode & 0o077:
        os.chmod(p, 0o600)
    k = p.read_text().strip()
    if len(k) < 32:
        die("shared secret too short")
    return k.encode()


def wjson(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def log(name, **kv):
    (H / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    kv = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **kv}
    with open(H / "logs" / name, "a") as f:
        f.write(json.dumps(kv) + "\n")


def canon(m):
    return json.dumps({k: m.get(k) for k in ("v", "id", "from", "to", "ts", "reply_to", "body")},
                      sort_keys=True, separators=(",", ":")).encode()


def sign(m):
    m["sig"] = hmac.new(key(), canon(m), hashlib.sha256).hexdigest()
    return m


def verify(m):
    return isinstance(m.get("sig"), str) and hmac.compare_digest(
        m["sig"], hmac.new(key(), canon(m), hashlib.sha256).hexdigest())


def to_token(m):
    return base64.urlsafe_b64encode(json.dumps(m, separators=(",", ":")).encode()).decode().rstrip("=")


def from_token(t):
    t = t.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", t) or len(t) > 64 * 1024:
        raise ValueError("not a token")
    return json.loads(base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)))


# ---- addresses -------------------------------------------------------------------------------------------------
def side():
    return cfg()["side"]


def local_ok(addr):
    """Addresses that live on this side."""
    if side() == "box":
        return addr in BOTS
    return addr.startswith("omp/") and bool(NAME_RE.match(addr[4:]))


def remote_ok(addr):
    if side() == "box":
        return addr.startswith("omp/") and bool(NAME_RE.match(addr[4:]))
    return addr in BOTS


# ---- storage ---------------------------------------------------------------------------------------------------
def inbox_dir(addr):
    return H / "inbox" / addr.replace("/", "__")


def all_inbox():
    out = []
    for d in sorted((H / "inbox").glob("*")):
        for p in sorted(d.glob("*.json")):
            out.append((p, json.loads(p.read_text())))
    return out


def find(mid):
    if not ID_RE.match(mid):
        die("bad id")
    for p in list((H / "inbox").glob(f"*/{mid}.json")) + list((H / "outbox").glob(f"{mid}.json")):
        return p, json.loads(p.read_text())
    die(f"no message {mid}")


def show(m):
    return (f"{PREFIX.format(frm=m['from'])}\n"
            f"id={m['id']} from={m['from']} to={m['to']} at={time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(m['ts']))}"
            + (f" reply_to={m['reply_to']}" if m.get("reply_to") else "")
            + f"\n<peer-message>\n{m['body']}\n</peer-message>")


# ---- core operations -------------------------------------------------------------------------------------------
def op_send(frm, to, body, reply_to=None):
    guard_on()
    if not local_ok(frm):
        die(f"'{frm}' is not an address on this side")
    if not remote_ok(to):
        die(f"'{to}' is not an allowed recipient from this side")
    if not body or len(body.encode()) > MAX_BODY:
        die(f"body must be 1..{MAX_BODY} bytes")
    if reply_to and not ID_RE.match(reply_to):
        die("bad reply_to")
    m = sign({"v": 1, "id": f"{int(time.time()*1000)}-{secrets.token_hex(4)}", "from": frm, "to": to,
              "ts": int(time.time()), "reply_to": reply_to, "body": body})
    m["state"] = "pending"                        # saved first, then delivery is attempted
    wjson(H / "outbox" / f"{m['id']}.json", m)
    log("events.log", ev="send", id=m["id"], frm=frm, to=to)
    if side() == "mac":
        url = read_env(H / "webhook.env").get("AB_WEBHOOK_URL", "")
        if not url:   # not configured yet: just queue quietly; the sweep carries it
            return m, f"saved {m['id']} for {to}; queued for the sweep (webhook not configured yet)"
        spawn_wake(m["id"])
        return m, (f"saved {m['id']} for {to}; waking Grok Bot via webhook in the background "
                   f"(retries with backoff; the sweep is the fallback)")
    return m, (f"saved {m['id']} for {to}. Deliver now with ONE Mac Shell call:\n"
               f"  ~/bin/agent-bridge deliver {to_token(m)}\n"
               f"then on the box: agent-bridge mark delivered {m['id']}  (the 15-min sweep does this if you don't)")


def op_import(tok, via):
    """Verify and store an incoming token. Never executes anything."""
    guard_on()
    try:
        m = from_token(tok)
    except Exception:
        log("events.log", ev="reject", why="undecodable", via=via)
        return False, "rejected: not a valid token"
    if not verify(m):
        log("events.log", ev="reject", why="bad-signature", via=via, id=str(m.get("id"))[:40])
        return False, "rejected: bad signature"
    if not (ID_RE.match(str(m.get("id"))) and local_ok(m.get("to", "")) and remote_ok(m.get("from", ""))):
        log("events.log", ev="reject", why="bad-address", via=via, id=str(m.get("id"))[:40])
        return False, "rejected: address not allowed"
    dest = inbox_dir(m["to"]) / f"{m['id']}.json"
    if dest.exists() or (H / "archive" / f"{m['id']}.json").exists():
        return True, f"duplicate {m['id']} (already stored)"
    m["state"] = "unread"
    m["received"] = int(time.time())
    wjson(dest, m)
    log("events.log", ev="import", id=m["id"], frm=m["from"], to=m["to"], via=via)
    return True, f"stored {m['id']} from {m['from']} for {m['to']}"


def op_inbox(addr=None, include_read=False):
    rows = []
    for p, m in all_inbox():
        if addr and m["to"] != addr:
            continue
        if m["state"] == "unread" or include_read:
            first = m["body"].strip().splitlines()[0][:100] if m["body"].strip() else ""
            rows.append(f"{m['id']}  {m['state']:<6} {m['from']} -> {m['to']}  {first}")
    return "\n".join(rows) or "No messages."


def op_read(mid, mark=True):
    p, m = find(mid)
    if mark and m.get("state") == "unread":
        m["state"] = "read"
        wjson(p, m)
    return show(m)


def op_ack(mid):
    p, m = find(mid)
    m["state"] = "done"
    wjson(H / "archive" / p.name, m)
    p.unlink()
    return f"archived {mid}"


def op_mark(state, ids):
    out = []
    for mid in ids:
        p = H / "outbox" / f"{mid}.json"
        if not ID_RE.match(mid) or not p.exists():
            out.append(f"{mid}: not in outbox")
            continue
        m = json.loads(p.read_text())
        m["state"] = state
        if state in ("delivered", "synced"):
            wjson(H / "sent" / p.name, m)
            p.unlink()
        else:
            wjson(p, m)
        out.append(f"{mid}: {state}")
    return "\n".join(out)


def op_export():
    """Outbox messages not yet confirmed by the other side, as signed tokens (one per line)."""
    return "\n".join(to_token({k: v for k, v in json.loads(p.read_text()).items() if k != "state"})
                     for p in sorted((H / "outbox").glob("*.json")))


# ---- mac: webhook wake with retry/backoff ---------------------------------------------------------------------
def read_env(p):
    env = {}
    if p.exists():
        if p.stat().st_mode & 0o077:
            os.chmod(p, 0o600)
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def spawn_wake(mid):
    if os.environ.get("AB_NO_SPAWN"):
        return
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_wake", mid], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=os.environ)


def op_wake(mid, delays=None):
    if delays is None:
        delays = [int(x) for x in os.environ["AB_DELAYS"].split(",")] if os.environ.get("AB_DELAYS") else DELAYS
    p = H / "outbox" / f"{mid}.json"
    if not p.exists():
        return 0
    m = json.loads(p.read_text())
    env = read_env(H / "webhook.env")
    url, tok = env.get("AB_WEBHOOK_URL", ""), env.get("AB_WEBHOOK_TOKEN", "")
    if not url:
        return 0      # not configured: queued for the sweep, nothing to log
    payload = json.dumps({"agent_bridge": 1, "id": m["id"], "from": m["from"], "to": m["to"],
                          "token": to_token({k: v for k, v in m.items() if k != "state"})}).encode()
    for i, d in enumerate(delays):
        if is_off():
            log("webhook.log", id=mid, attempt=i + 1, code="OFF", note="bridge switched off; left in outbox")
            return 1
        req = urllib.request.Request(url, data=payload, method="POST", headers={"content-type": "application/json"})
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code, note = r.status, r.read(200).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code, note = e.code, e.read(200).decode("utf-8", "replace")
        except Exception as e:  # network error
            code, note = "net-error", type(e).__name__
        log("webhook.log", id=mid, attempt=i + 1, code=str(code), note=note[:200])
        if isinstance(code, int) and 200 <= code < 300:
            m["state"] = "woken"                 # stays in outbox until the box confirms (sweep marks synced)
            wjson(p, m)
            return 0
        if code in (401, 403, 404) and i >= 1:
            break
        time.sleep(d + random.uniform(0, 4))
    log("webhook.log", id=mid, attempt=0, code="GAVE_UP", note="left in outbox; the 15-min sweep will carry it")
    return 1


# ---- mac: deliver into an OMP session's inbox (no injection unless opted in) -----------------------------------
def tmux_has(name):
    t = shutil.which("tmux")
    return t and subprocess.run([t, "has-session", "-t", f"={name}"], capture_output=True).returncode == 0


def notify_omp(m):
    name = m["to"][4:]
    t = shutil.which("tmux")
    if not t or not tmux_has(name):
        return "stored (no tmux session of that name to notify)"
    msg = f"agent-bridge: message {m['id']} from {m['from']}. Read: agent-bridge inbox"
    tgt = f"={name}:"                           # exact session match; trailing ':' makes it a valid pane target
    r = subprocess.run([t, "display-message", "-d", "8000", "-t", tgt, msg], capture_output=True)
    if r.returncode != 0:
        r = subprocess.run([t, "display-message", "-t", tgt, msg], capture_output=True)   # older tmux: no -d
    shown = "tmux notified" if r.returncode == 0 else "tmux notify FAILED (message still stored)"
    if (H / "inject" / name).exists():         # opt-in per session: types a POINTER line, never the message body
        line = f"[agent-bridge] New message {m['id']} from agent {m['from']}: read it with bridge_read."
        subprocess.run([t, "send-keys", "-t", tgt, "-l", line], capture_output=True)
        subprocess.run([t, "send-keys", "-t", tgt, "Enter"], capture_output=True)
        return f"stored, {shown}, pointer typed (inject opt-in)"
    return f"stored, {shown}"


# ---- status / switches -----------------------------------------------------------------------------------------
def op_status():
    c = cfg()
    lines = [f"agent-bridge {VERSION} side={c['side']} home={H} {'OFF' if is_off() else 'ON'}",
             f"secret: {'present' if (H/'secret').exists() else 'MISSING'}"]
    if c["side"] == "mac":
        e = read_env(H / "webhook.env")
        u = e.get('AB_WEBHOOK_URL', '')
        lines.append(f"webhook: {'configured' if u else 'NOT configured (sends queue for the sweep)'}"
                     f", token {'set' if e.get('AB_WEBHOOK_TOKEN') else 'not set'}")
    ob = list((H / "outbox").glob("*.json"))
    oldest = min((json.loads(p.read_text())["ts"] for p in ob), default=None)
    lines.append(f"outbox: {len(ob)} awaiting confirmation"
                 + (f" (oldest {int((time.time()-oldest)//60)} min)" if oldest else "")
                 + f"; inbox unread: {sum(1 for _, m in all_inbox() if m['state']=='unread')}")
    wl = H / "logs" / "webhook.log"
    if wl.exists():
        bad = [json.loads(l) for l in wl.read_text().splitlines()
               if l and json.loads(l).get("code") not in ("200", "201", "202", "204")]
        lines.append(f"webhook failures logged: {len(bad)}" + (f"; last: {bad[-1]['code']} at {bad[-1]['t']}" if bad else ""))
    rej = H / "logs" / "events.log"
    if rej.exists():
        n = sum(1 for l in rej.read_text().splitlines() if '"ev": "reject"' in l)
        lines.append(f"rejected imports: {n}")
    return "\n".join(lines)


def op_init(sd):
    if sd not in ("box", "mac"):
        die("--side box|mac")
    for d in ("inbox", "outbox", "sent", "archive", "logs", "incoming", "inject"):
        (H / d).mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(H, 0o700)
    if not (H / "config.json").exists():
        wjson(H / "config.json", {"side": sd, "version": VERSION})
    if sd == "box" and not (H / "secret").exists():
        fd = os.open(H / "secret", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, secrets.token_hex(32).encode())
        os.close(fd)
        return f"initialised {H} (side=box); generated a new shared secret (not shown)"
    return f"initialised {H} (side={sd})"


# ---- stdio MCP server ------------------------------------------------------------------------------------------
def mcp_tools():
    s = side()
    me = "bot name: dross, scout or researcher" if s == "box" else "your OMP session name, as omp/<name>"
    peer = "omp/<session>" if s == "box" else "dross, scout or researcher"
    T = lambda name, desc, props, req: {"name": name, "description": desc,
                                        "inputSchema": {"type": "object", "properties": props, "required": req}}
    S = {"type": "string"}
    return [
        T("bridge_inbox", "List unread peer messages (and with all=true, read ones too).",
          {"me": {**S, "description": me}, "all": {"type": "boolean"}}, []),
        T("bridge_read", "Read one message by id.",
          {"id": S}, ["id"]),
        T("bridge_ack", "Archive a message you have handled.", {"id": S}, ["id"]),
        T("bridge_send", f"Send a short message to {peer}. Point first, then context.",
          {"me": {**S, "description": me}, "to": {**S, "description": peer}, "message": S,
           "reply_to": {**S, "description": "id of the message you answer"}}, ["to", "message"]),
        T("bridge_status", "Bridge health: on/off, pending, failures.", {}, []),
    ]


def mcp_call(name, a):
    me = a.get("me") or os.environ.get("AB_SELF") or ""
    if side() == "mac" and me and not me.startswith("omp/"):
        me = "omp/" + me
    if name == "bridge_inbox":
        return op_inbox(me or None, bool(a.get("all")))
    if name == "bridge_read":
        return op_read(a["id"])
    if name == "bridge_ack":
        return op_ack(a["id"])
    if name == "bridge_status":
        return op_status()
    if name == "bridge_send":
        if not me:
            raise ValueError("say who you are: pass 'me' (or set AB_SELF)")
        return op_send(me, a["to"], a["message"], a.get("reply_to"))[1]
    raise ValueError(f"unknown tool {name}")


def mcp_serve():
    cfg()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        mid, meth = req.get("id"), req.get("method")
        if mid is None:
            continue  # notification
        if meth == "initialize":
            res = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-bridge", "version": VERSION},
                   "instructions": "Private inbox between Nate's OMP sessions and his Grok Bots."}
        elif meth == "tools/list":
            res = {"tools": mcp_tools()}
        elif meth == "tools/call":
            p = req.get("params", {})
            try:
                if is_off() and p.get("name") != "bridge_status":
                    raise ValueError("bridge is OFF")
                txt, err = mcp_call(p.get("name"), p.get("arguments") or {}), False
            except SystemExit as e:
                txt, err = f"error (exit {e.code})", True
            except Exception as e:
                txt, err = f"error: {e}", True
            res = {"content": [{"type": "text", "text": txt}], "isError": err}
        elif meth == "ping":
            res = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not found"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)


# ---- CLI -------------------------------------------------------------------------------------------------------
USAGE = """agent-bridge {v}
  init --side box|mac          create ~/.agent-bridge (mac) or /workspace/agent-bridge/data (box)
  send FROM TO [--reply-to ID] MESSAGE...   (or message on stdin)
  inbox [ADDR] [--all]         list messages      read ID / ack ID
  import-token TOKEN|@FILE     (box) store a signed token from the webhook body
  import-tokens FILE|-         store many tokens, one per line
  deliver TOKEN...             (mac) store tokens for OMP sessions and notify tmux (never executes them)
  export                       print outbox tokens awaiting confirmation (for the sweep)
  received-ids [--days N]     ids stored here recently (for the other side's 'mark')
  mark delivered|synced ID...  confirm outbox messages
  status | off | on | mcp | version"""


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.format(v=VERSION)); return 0
    c, a = argv[0], argv[1:]
    if c == "version":
        print(VERSION); return 0
    if c == "init":
        sd = a[a.index("--side") + 1] if "--side" in a else ""
        print(op_init(sd)); return 0
    if c == "status":
        print(op_status()); return 0
    if c == "off":
        (H / "OFF").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z\n")); print("agent-bridge is OFF"); return 0
    if c == "on":
        (H / "OFF").unlink(missing_ok=True); print("agent-bridge is ON"); return 0
    if c == "mcp":
        mcp_serve(); return 0
    if c == "_wake":
        return op_wake(a[0])
    if c == "send":
        rt = None
        if "--reply-to" in a:
            i = a.index("--reply-to"); rt = a[i + 1]; a = a[:i] + a[i + 2:]
        if len(a) < 2:
            die("send FROM TO MESSAGE")
        body = " ".join(a[2:]) if len(a) > 2 else sys.stdin.read()
        print(op_send(a[0], a[1], body.strip(), rt)[1]); return 0
    if c == "inbox":
        print(op_inbox(next((x for x in a if not x.startswith("--")), None), "--all" in a)); return 0
    if c == "read":
        print(op_read(a[0])); return 0
    if c == "ack":
        print(op_ack(a[0])); return 0
    if c == "received-ids":   # ids this side has stored in the last N days (the other side marks them confirmed)
        days = int(a[a.index("--days") + 1]) if "--days" in a else 3
        cut = time.time() - days * 86400
        ids = sorted({json.loads(p.read_text())["id"] for p in list((H / "inbox").glob("*/*.json")) +
                      list((H / "archive").glob("*.json")) if p.stat().st_mtime >= cut})
        if ids: print(" ".join(ids))
        return 0
    if c == "export":
        out = op_export()
        if out: print(out)
        return 0
    if c == "mark":
        if not a or a[0] not in ("delivered", "synced"):
            die("mark delivered|synced ID...")
        print(op_mark(a[0], a[1:])); return 0
    if c in ("import-token", "import-tokens", "deliver"):
        if c == "import-tokens":
            src = sys.stdin.read() if a == ["-"] else Path(a[0]).read_text()
            toks = [t for t in src.split() if t]
        elif c == "import-token" and a and a[0].startswith("@"):
            raw = Path(a[0][1:]).read_text().strip()
            try:  # accept the raw webhook JSON body too
                raw = json.loads(raw)["token"]
            except Exception:
                pass
            toks = [raw]
        else:
            toks = a
        ok_all = True
        for t in toks:
            ok, msg = op_import(t, c)
            ok_all &= ok
            if ok and c == "deliver" and msg.startswith("stored"):
                m = from_token(t)
                msg += "; " + notify_omp(m)
            print(msg)
        return 0 if ok_all else 2
    die(f"unknown command {c}; see agent-bridge help")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
