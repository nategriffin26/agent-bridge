#!/usr/bin/env python3
"""agent-bridge v2: a private, signed mailbox between omp coding sessions on a Mac and bots on a remote box.

One stdlib-only file (Python 3.9+). Mac side only; the box keeps running v1, whose tokens and webhook payload are unchanged.

Store: SQLite in WAL mode at ~/.agent-bridge/v2/bridge.db. Tables: agents (live directory), agents_archive (pruned rows),
messages (signed bodies), deliveries (one per message: its state and timestamps), events (log), meta.
  inbound to an omp agent:   queued -> notified -> read -> acked
  outbound to a box bot:     pending -> woken (webhook 2xx) | gave_up -> synced (box confirmed) -> read -> acked
Every message is HMAC-signed with the v1 shared secret over the v1 canonical fields, so it can still travel as a v1 token.
Received text is never executed. Secrets are never printed. Every subprocess and network call has a timeout.
"""
import base64, hashlib, hmac, json, os, random, re, secrets, socket, sqlite3, subprocess, sys, threading, time
import urllib.error, urllib.request
from contextlib import contextmanager
from pathlib import Path

VERSION = "2.0.0"
SCHEMA = 1
MAX_BODY = 32 * 1024
BOX = set()                            # box agent addresses (e.g. Grok bots), loaded from the store; the box's roster sets it
EXITED_KEEP = 3600                     # an exited omp row is archived after this long ...
STALE_KEEP = 86400                     # ... and a stale one after this long (pump runs the prune every minute)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
ID_RE = re.compile(r"^[0-9]{13}-[0-9a-f]{8}$")
DELAYS = [5, 15, 45, 120, 300, 600]   # webhook retry backoff (seconds) after each failed attempt; then gave_up
STALE_AFTER = 120                      # seconds without a heartbeat before an agent counts as stale
RENOTIFY_AFTER = 900                   # an unread, notified message is pointed out again after this long ...
MAX_NOTIFY = 3                         # ... at most this many times in all
NUDGE_GAP = 30                         # at most one tmux nudge per pane per this many seconds
PREFIX = "[agent-bridge] From agent {frm}"
HOST = socket.gethostname()


def home() -> Path:
    return Path(os.environ.get("AB_HOME") or "~/.agent-bridge").expanduser()


H = home()


def owner():
    """Whose agents these are, for the wording agents read ("owner" in config.json)."""
    try:
        return json.loads((H / "config.json").read_text()).get("owner") or "the owner"
    except Exception:
        return "the owner"


OWNER = owner()
V2 = H / "v2"
DB = V2 / "bridge.db"
NOTIFY = V2 / "notify"
BUSY_S = float(os.environ.get("AB_BUSY_MS") or 1500) / 1000          # how long a writer waits on a locked store
HTTP_S = min(float(os.environ.get("AB_HTTP_TIMEOUT") or 15), 15.0)    # hard cap per webhook attempt
TMUX_S = min(float(os.environ.get("AB_TMUX_TIMEOUT") or 2), 2.0)      # hard cap per tmux call
TMUX = os.environ.get("AB_TMUX") or "tmux"


class Fail(Exception):
    """A refusal the caller should see (bad address, unknown id, store busy)."""
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def now():
    return time.time()


# ---- config, secret, signing (v1-compatible) -------------------------------------------------------------------
def cfg():
    p = H / "config.json"
    if not p.exists():
        raise Fail(f"not initialised ({p} missing); run: agent-bridge init --side mac")
    c = json.loads(p.read_text())
    if c.get("side") != "mac":
        raise Fail("agent-bridge v2 runs on the Mac side only; the box keeps v1")
    return c


def is_off():
    return (H / "OFF").exists()


def guard_on():
    if is_off():
        raise Fail("bridge is OFF (remove with: agent-bridge on)", 3)


def key() -> bytes:
    p = H / "secret"
    if not p.exists():
        raise Fail(f"shared secret missing at {p} (0600)")
    if p.stat().st_mode & 0o077:
        os.chmod(p, 0o600)
    k = p.read_text().strip()
    if len(k) < 32:
        raise Fail("shared secret too short")
    return k.encode()


def canon(m):
    return json.dumps({k: m.get(k) for k in ("v", "id", "from", "to", "ts", "reply_to", "body")},
                      sort_keys=True, separators=(",", ":")).encode()


def sign(m):
    m["sig"] = hmac.new(key(), canon(m), hashlib.sha256).hexdigest()
    return m


def verify(m):
    return isinstance(m, dict) and isinstance(m.get("sig"), str) and hmac.compare_digest(
        m["sig"], hmac.new(key(), canon(m), hashlib.sha256).hexdigest())


def to_token(m):
    return base64.urlsafe_b64encode(json.dumps(m, separators=(",", ":")).encode()).decode().rstrip("=")


def from_token(t):
    t = t.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", t) or len(t) > 64 * 1024:
        raise ValueError("not a token")
    return json.loads(base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)))


def new_id():
    return f"{int(now() * 1000)}-{secrets.token_hex(4)}"


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


def webhook():
    e = read_env(H / "webhook.env")
    return e.get("AB_WEBHOOK_URL", ""), e.get("AB_WEBHOOK_TOKEN", "")


# ---- addresses -------------------------------------------------------------------------------------------------
def is_omp(a):
    return isinstance(a, str) and a.startswith("omp/") and bool(NAME_RE.match(a[4:]))


def is_bot(a):
    """A box agent on the current roster (agents rows of kind 'box'); dross always counts."""
    return a == "dross" or a in BOX


def as_addr(a):
    """A box agent stays as it is; 'foo' and 'omp/foo' both mean omp/foo."""
    a = (a or "").strip()
    return a if is_bot(a) or a.startswith("omp/") else "omp/" + a


def clean_label(s):
    s = re.sub(r"[^a-z0-9._-]+", "-", (s or "").lower()).strip("-._")[:40].strip("-._")
    return s or "agent"


# ---- store -----------------------------------------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'omp', label TEXT, aliases TEXT NOT NULL DEFAULT '[]',
  cwd TEXT, repo TEXT, branch TEXT, model TEXT, provider TEXT, tmux_target TEXT, tmux_pane TEXT,
  pid INTEGER, host TEXT, session_id TEXT, session_file TEXT, started_at REAL, heartbeat_at REAL,
  state TEXT, exit_reason TEXT, ext_version TEXT);
CREATE TABLE IF NOT EXISTS agents_archive(id TEXT NOT NULL, archived_at REAL NOT NULL, row TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, from_addr TEXT NOT NULL, to_addr TEXT NOT NULL, rcpt TEXT, ts INTEGER NOT NULL,
  reply_to TEXT, thread TEXT, body TEXT NOT NULL, sig TEXT NOT NULL, origin TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_rcpt ON messages(rcpt);
CREATE INDEX IF NOT EXISTS messages_to ON messages(to_addr);
CREATE TABLE IF NOT EXISTS deliveries(
  msg_id TEXT PRIMARY KEY REFERENCES messages(id), direction TEXT NOT NULL, state TEXT NOT NULL,
  queued_at REAL, notified_at REAL, notify_count INTEGER NOT NULL DEFAULT 0, read_at REAL, acked_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0, next_at REAL, lease_until REAL, last_code TEXT, woken_at REAL, synced_at REAL);
CREATE INDEX IF NOT EXISTS deliveries_state ON deliveries(direction, state, next_at);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, t REAL NOT NULL, ev TEXT NOT NULL, msg_id TEXT, agent TEXT,
  detail TEXT);
CREATE INDEX IF NOT EXISTS events_t ON events(t);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Conn(sqlite3.Connection):
    """Every statement retries a locked/busy store every 20 ms until BUSY_S, then fails with exit 4.
    SQLite's own busy handler is off: on this Mac it sleeps in whole-second steps (a 1.5 s timeout waited 2.7 s).
    Reads need the retry too: in WAL mode a reader can see SQLITE_BUSY for a moment while a writer checkpoints."""

    def execute(self, sql, params=()):
        deadline = time.monotonic() + BUSY_S
        while True:
            try:
                return super().execute(sql, params)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e):
                    raise
                if time.monotonic() >= deadline:
                    raise Fail(f"store busy ({e}); nothing was written, try again", 4)
                time.sleep(0.02)


def connect():
    V2.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh = not DB.exists()
    c = sqlite3.connect(str(DB), timeout=0, isolation_level=None, factory=Conn)
    c.row_factory = sqlite3.Row
    if fresh:
        os.chmod(DB, 0o600)
    if c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
        if c.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            c.execute("PRAGMA journal_mode=WAL")
        with tx(c):
            for stmt in DDL.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            c.execute("INSERT OR IGNORE INTO agents(id, kind, label, state) VALUES('dross', 'box', 'Dross', 'remote')")
            c.execute(f"PRAGMA user_version={SCHEMA}")
    c.execute("PRAGMA synchronous=FULL")
    BOX.clear()
    BOX.update(r[0] for r in c.execute("SELECT id FROM agents WHERE kind='box'"))
    return c


@contextmanager
def tx(c):
    """A short write transaction."""
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def event(c, ev, msg_id=None, agent=None, **detail):
    c.execute("INSERT INTO events(t, ev, msg_id, agent, detail) VALUES(?,?,?,?,?)",
              (now(), ev, msg_id, agent, json.dumps(detail) if detail else None))


# state machine: one forward order per direction; the timestamp column each state sets
RANK = {"in": ["queued", "notified", "read", "acked"],
        "out": ["pending", "gave_up", "woken", "synced", "read", "acked"]}
STAMP = {"notified": "notified_at", "read": "read_at", "acked": "acked_at", "woken": "woken_at", "synced": "synced_at"}


def advance(c, mid, new):
    """Move a delivery forward to `new`; never backwards. Returns True when it moved."""
    d = c.execute("SELECT direction, state FROM deliveries WHERE msg_id=?", (mid,)).fetchone()
    if not d or new not in RANK[d["direction"]]:
        return False
    order = RANK[d["direction"]]
    if order.index(new) <= order.index(d["state"]):
        return False
    col = STAMP.get(new)
    c.execute(f"UPDATE deliveries SET state=?{', ' + col + '=?' if col else ''} WHERE msg_id=?",
              (new, now(), mid) if col else (new, mid))
    event(c, new, mid)
    return True


def agent_row(c, aid):
    return c.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True


def status_of(r, t=None):
    if r["kind"] == "box":
        return "box"
    if r["state"] == "exited":
        return "exited"
    if (t or now()) - (r["heartbeat_at"] or 0) > STALE_AFTER:
        return "stale"
    if r["pid"] and r["host"] == HOST and not pid_alive(r["pid"]):
        return "stale"
    return "live"


def addrs_of(c, me):
    """The addresses a caller answers to: its id plus its aliases."""
    r = agent_row(c, me)
    return [me] + (json.loads(r["aliases"]) if r else [])


def resolve(c, addr):
    """An exact agent id resolves to itself; an alias resolves when exactly one live agent carries it; else None."""
    if agent_row(c, addr):
        return addr
    like = f'%"{addr}"%'
    live = [r["id"] for r in c.execute("SELECT * FROM agents WHERE kind='omp' AND aliases LIKE ?", (like,))
            if addr in json.loads(r["aliases"]) and status_of(r) == "live"]
    return live[0] if len(live) == 1 else None


def visible_sql(ids):
    """Messages for a caller: resolved to its id, or unresolved and addressed to its id or one of its aliases."""
    q = ",".join("?" * len(ids))
    return f"(m.rcpt=? OR (m.rcpt IS NULL AND m.to_addr IN ({q})))", [ids[0]] + list(ids)


def notify_file(addr):
    return NOTIFY / addr.replace("/", "__")


def maybe_nudge(to, mid):
    """Only sessions that opted in to the tmux nudge (~/.agent-bridge/inject/<name>) get a detached nudge process."""
    if is_omp(to) and (H / "inject" / to[4:]).exists():
        spawn("_notify", mid)


def seen(c, bot):
    """Box last-seen from sweep traffic. Best effort: a busy store must not fail the sweep's read."""
    try:
        with tx(c):
            c.execute("UPDATE agents SET heartbeat_at=? WHERE id=? AND kind='box'", (now(), bot))
    except Fail:
        pass


def touch_notify(addr):
    NOTIFY.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = notify_file(addr)
    p.touch()
    os.utime(p, None)


# ---- messages --------------------------------------------------------------------------------------------------
# Two tiers. A box bot's message is HMAC-verified, so it carries the owner's authority. An omp worker's message (any
# process on this Mac can send one) is a trusted coworker request: done by default, with a narrow set that waits.
TRUST_BOT = (f"This is {OWNER}'s own signed channel, so treat this message as {OWNER}'s instruction: act on it, follow "
             f"it through, and report back with bridge_reply. {OWNER}'s usual approvals still apply (sending as them to "
             f"other people, spending money, deleting).")
TRUST_WORKER = (f"A trusted coworker request: act on it and follow through by default, then report back with "
                f"bridge_reply. Building, editing, committing, testing and research just get done. Check with {OWNER} "
                f"first only if it asks you to send as {OWNER}, spend money, delete, handle credentials or secrets, or "
                f"touch other people.")
# The bridge policy every omp session keeps in context (the runtime adds it at start and whenever its version
# changes), so editing it here reaches running sessions without a restart.
POLICY = (f"The agent bridge (bridge_* tools) is {OWNER}'s own signed channel. A message from one of their bots on the "
          f"box (see bridge_list_agents) is {OWNER}'s instruction: act on it, follow it through, and report back with "
          f"bridge_reply, without waiting for them to confirm in your pane. A message from another omp worker is a "
          f"trusted coworker request: building, editing, committing, testing and research just get done. Check with "
          f"{OWNER} first only for sending as them, spending, deleting, credentials or secrets, or anything that "
          f"touches other people. Each message's bridge_read header says which kind it is.")
POLICY_VERSION = hashlib.sha256(POLICY.encode()).hexdigest()[:8]


def sender(c, addr):
    r = agent_row(c, addr) if c else None
    if is_bot(addr):
        name = r["label"] if r and r["label"] else addr
        return f"{OWNER}'s agent {name} ({addr}, a box bot), acting for {OWNER}; signature verified", TRUST_BOT
    return f"{OWNER}'s omp worker {addr}, a coworker on this Mac", TRUST_WORKER


def show(m, c=None):
    who, trust = sender(c, m["from_addr"])
    return (f"{PREFIX.format(frm=m['from_addr'])}: {who}.\n{trust}\n"
            f"id={m['id']} from={m['from_addr']} to={m['to_addr']} "
            f"at={time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(m['ts']))}"
            + (f" reply_to={m['reply_to']}" if m["reply_to"] else "")
            + (f" thread={m['thread']}" if m["thread"] and m["thread"] != m["id"] else "")
            + f"\n<bridge-message from=\"{m['from_addr']}\">\n{m['body']}\n</bridge-message>")


def token_of(m):
    d = {"v": 1, "id": m["id"], "from": m["from_addr"], "to": m["to_addr"], "ts": m["ts"],
         "reply_to": m["reply_to"], "body": m["body"], "sig": m["sig"]}
    if m["thread"]:
        d["thread"] = m["thread"]
    return to_token(d)


def insert(c, m, origin, thread=None):
    """Store one signed message and its delivery row. Returns False when the id is already stored (idempotent)."""
    direction = "out" if is_bot(m["to"]) else "in"
    rcpt = resolve(c, m["to"]) if direction == "in" else None
    cur = c.execute("INSERT OR IGNORE INTO messages(id, from_addr, to_addr, rcpt, ts, reply_to, thread, body, sig, origin,"
                    " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (m["id"], m["from"], m["to"], rcpt, int(m["ts"]), m.get("reply_to"), thread or m.get("thread"),
                     m["body"], m["sig"], origin, now()))
    if cur.rowcount == 0:
        return False
    c.execute("INSERT INTO deliveries(msg_id, direction, state, queued_at, next_at) VALUES(?,?,?,?,?)",
              (m["id"], direction, "pending" if direction == "out" else "queued", now(),
               now() if direction == "out" else None))
    return True


def op_send(c, frm, to, body, reply_to=None, thread=None):
    guard_on()
    raw_to = (to or "").strip()
    frm, to = as_addr(frm), as_addr(to)
    if not is_omp(frm):
        raise Fail(f"'{frm}' is not an address on this side (omp/<name>)")
    if ("/" not in raw_to and not is_bot(raw_to)) or not (is_omp(to) or is_bot(to)):
        raise Fail(f"'{raw_to}' is not a valid recipient: omp/<name>, or a current box bot "
                   f"({', '.join(sorted(BOX | {'dross'}))})")
    if not body or len(body.encode()) > MAX_BODY:
        raise Fail(f"body must be 1..{MAX_BODY} bytes")
    if reply_to and not ID_RE.match(reply_to):
        raise Fail("bad reply_to")
    m = sign({"v": 1, "id": new_id(), "from": frm, "to": to, "ts": int(now()), "reply_to": reply_to or None,
              "body": body})
    with tx(c):
        if not thread and reply_to:
            p = c.execute("SELECT thread FROM messages WHERE id=?", (reply_to,)).fetchone()
            thread = (p and p["thread"]) or reply_to
        insert(c, m, "local", thread or m["id"])
        event(c, "send", m["id"], frm, to=to)
        rcpt = c.execute("SELECT rcpt FROM messages WHERE id=?", (m["id"],)).fetchone()["rcpt"]
    if is_bot(to):
        url, _ = webhook()
        if not url:
            return m, f"saved {m['id']} for {to}; queued for the sweep (webhook not configured)"
        spawn("_pump")
        return m, (f"saved {m['id']} for {to}; waking {to} via webhook in the background "
                   f"(retries with backoff; the sweep is the fallback)")
    touch_notify(rcpt or to)
    maybe_nudge(to, m["id"])
    return m, f"saved {m['id']} for {to}" + ("" if rcpt else " (no live agent with that address yet; held until one reads it)")


def op_import(c, tok, via):
    """Verify and store a token from the box. Never executes anything. Returns (ok, stored, text)."""
    guard_on()
    try:
        m = from_token(tok)
    except Exception:
        with tx(c):
            # Length and a short hash (never the token) show a cut-off paste: compare with what the sender made.
            event(c, "reject", why="undecodable", via=via, length=len(tok),
                  sha=hashlib.sha256(tok.strip().encode()).hexdigest()[:12])
        return False, False, "rejected: not a valid token"
    if not verify(m):
        with tx(c):
            event(c, "reject", str(m.get("id"))[:40] if isinstance(m, dict) else None, why="bad-signature", via=via)
        return False, False, "rejected: bad signature"
    if not (ID_RE.match(str(m.get("id"))) and is_omp(m.get("to")) and is_bot(m.get("from"))
            and isinstance(m.get("body"), str) and isinstance(m.get("ts"), int)):
        with tx(c):
            event(c, "reject", str(m.get("id"))[:40], why="bad-address", via=via)
        return False, False, "rejected: address not allowed"
    with tx(c):
        c.execute("UPDATE agents SET heartbeat_at=? WHERE id=? AND kind='box'", (now(), m["from"]))
        thread = m.get("thread") if isinstance(m.get("thread"), str) else None
        if not insert(c, m, "box", thread or m.get("reply_to") or m["id"]):
            return True, False, f"duplicate {m['id']} (already stored)"
        event(c, "import", m["id"], m["to"], frm=m["from"], via=via)
        rcpt = c.execute("SELECT rcpt FROM messages WHERE id=?", (m["id"],)).fetchone()["rcpt"]
    touch_notify(rcpt or m["to"])
    return True, True, f"stored {m['id']} from {m['from']} for {m['to']}" + (f" (agent {rcpt})" if rcpt and rcpt != m["to"] else "")


def get_msg(c, mid):
    if not ID_RE.match(mid or ""):
        raise Fail("bad id")
    m = c.execute("SELECT m.*, d.state, d.direction FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE m.id=?",
                  (mid,)).fetchone()
    if not m:
        raise Fail(f"no message {mid}")
    return m


def mine(c, me, m):
    return m["rcpt"] == me or (m["rcpt"] is None and m["to_addr"] in addrs_of(c, me))


def op_inbox(c, me=None, unread_only=True, limit=50):
    """me=None lists every address (operator view)."""
    where, args = ("1=1", [])
    if me:
        where, args = visible_sql(addrs_of(c, me))
    states = ("queued", "notified") if unread_only else ("queued", "notified", "read")
    rows = c.execute(f"SELECT m.*, d.state FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE d.direction='in' "
                     f"AND {where} AND d.state IN ({','.join('?' * len(states))}) ORDER BY m.created_at LIMIT ?",
                     args + list(states) + [int(limit)]).fetchall()
    out = []
    for m in rows:
        first = m["body"].strip().splitlines()[0][:100] if m["body"].strip() else ""
        out.append(f"{m['id']}  {m['state']:<8} {m['from_addr']} -> {m['to_addr']}  {first}")
    return "\n".join(out) or "No messages."


def op_read(c, mid, me=None):
    with tx(c):
        m = get_msg(c, mid)
        if me and not (mine(c, me, m) or m["from_addr"] in addrs_of(c, me)):
            raise Fail(f"message {mid} is not addressed to {me}")
        if m["direction"] == "in" and (me is None or mine(c, me, m)):
            advance(c, mid, "read")
    return show(m, c)


def op_ack(c, mid, me=None):
    with tx(c):
        m = get_msg(c, mid)
        if me and not mine(c, me, m):
            raise Fail(f"message {mid} is not addressed to {me}")
        if m["direction"] != "in":
            raise Fail(f"{mid} is outbound to {m['to_addr']}; the box confirms it with 'mark', and an ack here would "
                       f"drop it from export before it arrives")
        if m["rcpt"] is None and me:
            c.execute("UPDATE messages SET rcpt=? WHERE id=?", (me, mid))
        advance(c, mid, "acked")
    return f"acked {mid}"


def op_reply(c, me, mid, body):
    m = get_msg(c, mid)
    if not mine(c, me, m):
        raise Fail(f"message {mid} is not addressed to {me}")
    _, text = op_send(c, me, m["from_addr"], body, reply_to=mid, thread=m["thread"])
    with tx(c):
        advance(c, mid, "read")   # answering it means it was read
    op_ack(c, mid, me)
    return text + f"; acked {mid}"


def op_pending(c, me):
    """Claim the messages `me` has not been pointed at yet (or that sat unread past RENOTIFY_AFTER)."""
    where, args = visible_sql(addrs_of(c, me))
    t = now()
    with tx(c):
        rows = c.execute(f"SELECT m.id, m.from_addr, d.state, d.notified_at, d.notify_count FROM messages m "
                         f"JOIN deliveries d ON d.msg_id=m.id WHERE d.direction='in' AND {where} "
                         f"AND (d.state='queued' OR (d.state='notified' AND d.notified_at<? AND d.notify_count<?))",
                         args + [t - RENOTIFY_AFTER, MAX_NOTIFY]).fetchall()
        for r in rows:
            c.execute("UPDATE deliveries SET state='notified', notified_at=?, notify_count=notify_count+1 WHERE msg_id=?",
                      (t, r["id"]))
            event(c, "notified", r["id"], me)
        unread = c.execute(f"SELECT count(*) FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE d.direction='in' "
                           f"AND {where} AND d.state IN ('queued','notified')", args).fetchone()[0]
    new = [{"id": r["id"], "from": r["from_addr"]} for r in rows]
    froms = ", ".join(sorted({r["from_addr"] for r in rows}))
    act = (f"act on it as {OWNER}'s instruction" if all(is_bot(r["from_addr"]) for r in rows) else
           f"act on it (a box bot's message is {OWNER}'s instruction; a worker's is a coworker request to do by default)")
    pointer = (f"[agent-bridge] You have {len(new)} new bridge message{'' if len(new) == 1 else 's'} from {froms}"
               + (f" ({unread} unread in all)" if unread > len(new) else "") +
               f". Read each with bridge_read and {act}, then report back with bridge_reply." if new else "")
    return {"new": new, "unread": unread, "pointer": pointer}


# ---- registry --------------------------------------------------------------------------------------------------
def run(cmd, timeout, cwd=None):
    """A bounded subprocess. Returns stdout, or None on any failure or timeout."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def tmux_info(pane):
    out = run([TMUX, "display-message", "-p", "-t", pane, "#{session_name}\t#{window_index}\t#{pane_index}"], TMUX_S)
    if not out or out.count("\t") != 2:
        return None, None
    s, w, p = out.strip().split("\t")
    return s, f"{s}:{w}.{p}"


def op_register(c, meta):
    """Register (or refresh) an omp session. Identity is computed here, in one place."""
    sid = str(meta.get("session_id") or "")
    if not sid:
        raise Fail("session_id required")
    cwd = str(meta.get("cwd") or os.getcwd())
    pane = meta.get("tmux_pane") or None
    tmux_session, target = tmux_info(pane) if pane and meta.get("tmux") else (None, None)
    ab_self = (meta.get("ab_self") or "").strip()
    if ab_self:
        aid = as_addr(ab_self)
        if not is_omp(aid):
            raise Fail(f"AB_SELF '{ab_self}' is not a valid omp address")
        label = aid[4:]
    else:
        label = clean_label(tmux_session or os.path.basename(cwd.rstrip("/")))
        aid = f"omp/{label}-{hashlib.sha256(sid.encode()).hexdigest()[:4]}"
    aliases = []
    for a in ([f"omp/{clean_label(tmux_session)}"] if tmux_session else []) + [f"omp/{label}"]:
        if a != aid and is_omp(a) and a not in aliases:
            aliases.append(a)
    top = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], 1.5)
    branch = run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], 1.5) if top else None
    t = now()
    with tx(c):
        old = agent_row(c, aid)
        same = old and old["session_id"] == sid and old["pid"] == meta.get("pid") and old["state"] != "exited"
        c.execute("INSERT OR REPLACE INTO agents(id, kind, label, aliases, cwd, repo, branch, model, provider, tmux_target,"
                  " tmux_pane, pid, host, session_id, session_file, started_at, heartbeat_at, state, exit_reason,"
                  " ext_version) VALUES(?, 'omp', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, NULL, ?)",
                  (aid, label, json.dumps(aliases), cwd, top.strip() if top else None,
                   branch.strip() if branch else None, meta.get("model"), meta.get("provider"), target, pane,
                   meta.get("pid"), HOST, sid, meta.get("session_file"), old["started_at"] if same else t, t,
                   meta.get("state") or "idle", meta.get("ext_version")))
        event(c, "register", agent=aid, pid=meta.get("pid"), refresh=bool(same))
        release_alias_mail(c, aliases)   # a crashed predecessor never exited cleanly; its alias mail comes here
    return {"id": aid, "aliases": aliases, "tmux_target": target}


def op_heartbeat(c, aid, state=None, model=None, provider=None, ext=None):
    with tx(c):
        r = agent_row(c, aid)
        if not r or r["state"] == "exited":
            return {"reregister": True}
        c.execute("UPDATE agents SET heartbeat_at=?, state=COALESCE(?, state), model=COALESCE(?, model),"
                  " provider=COALESCE(?, provider), ext_version=COALESCE(?, ext_version) WHERE id=?",
                  (now(), state, model, provider, ext, aid))
    where, args = visible_sql(addrs_of(c, aid))
    n = c.execute(f"SELECT count(*) FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE d.direction='in' AND {where}"
                  f" AND (d.state='queued' OR (d.state='notified' AND d.notified_at<? AND d.notify_count<?))",
                  args + [now() - RENOTIFY_AFTER, MAX_NOTIFY]).fetchone()[0]
    return {"pending": n, "policy": POLICY_VERSION}


def release_alias_mail(c, aliases):
    """Unread mail sent to an alias, bound to a session that is no longer live, goes back to the alias so whichever
    session carries it next can see it. Mail sent to an exact id stays with that id. Runs inside a transaction."""
    for alias in aliases:
        for m in c.execute("SELECT m.id, m.rcpt FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE "
                           "d.direction='in' AND d.state IN ('queued','notified') AND m.to_addr=? AND m.rcpt IS NOT NULL "
                           "AND m.rcpt != m.to_addr", (alias,)).fetchall():
            r = agent_row(c, m["rcpt"])
            if not r or status_of(r) != "live":
                c.execute("UPDATE messages SET rcpt=NULL WHERE id=?", (m["id"],))
                event(c, "released", m["id"], m["rcpt"], alias=alias)


def op_exit(c, aid, reason):
    with tx(c):
        c.execute("UPDATE agents SET state='exited', exit_reason=?, heartbeat_at=? WHERE id=? AND kind='omp'",
                  (reason, now(), aid))
        event(c, "exit", agent=aid, reason=reason)
        r = agent_row(c, aid)
        if r:
            release_alias_mail(c, json.loads(r["aliases"]))
    return {"ok": True}


def agent_json(c, r):
    where, args = visible_sql([r["id"]] + json.loads(r["aliases"]))
    unread = c.execute(f"SELECT count(*) FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE d.direction='in' "
                       f"AND {where} AND d.state IN ('queued','notified')", args).fetchone()[0]
    d = {k: r[k] for k in r.keys()}
    d["aliases"] = json.loads(r["aliases"])
    d["status"] = status_of(r)
    d["unread"] = unread
    return d


def op_agents(c, which="live", name=None):
    rows = [agent_json(c, r) for r in c.execute("SELECT * FROM agents ORDER BY kind DESC, heartbeat_at DESC")]
    keep = {"live": ("live", "box"), "stale": ("stale",), "exited": ("exited",)}.get(which)
    return [a for a in rows if (not keep or a["status"] in keep)
            and (not name or name.lower() in (a["id"] + " " + " ".join(a["aliases"])).lower())]


def fmt_agents(rows):
    if not rows:
        return "No agents."
    out = []
    for a in rows:
        age = int(now() - a["heartbeat_at"]) if a["heartbeat_at"] else None
        seen = f"{age}s ago" if age is not None and age < 120 else (f"{age // 60}m ago" if age is not None else "never")
        bits = [a["id"], a["status"] + (f"/{a['state']}" if a["status"] == "live" else ""), f"seen {seen}"]
        if a["kind"] == "box" and a["label"] and a["label"] != a["id"]:
            bits.append(a["label"] + (f" (grok {a['session_id']})" if a["session_id"] else ""))
        if a["aliases"]:
            bits.append("aka " + ",".join(a["aliases"]))
        if a["model"]:
            bits.append(f"{a['provider']}/{a['model']}" if a["provider"] else a["model"])
        if a["tmux_target"]:
            bits.append(f"tmux {a['tmux_target']}")
        if a["cwd"]:
            bits.append(a["cwd"].replace(str(Path.home()), "~"))
        if a["unread"]:
            bits.append(f"{a['unread']} unread")
        out.append("  ".join(bits))
    return "\n".join(out)


def archive_agent(c, r, why):
    """Move one agent row to agents_archive (inside the caller's transaction). Its messages stay."""
    c.execute("INSERT INTO agents_archive(id, archived_at, row) VALUES(?,?,?)",
              (r["id"], now(), json.dumps({**{k: r[k] for k in r.keys()}, "archived_why": why})))
    c.execute("DELETE FROM agents WHERE id=?", (r["id"],))


def op_prune(c, exited_after=EXITED_KEEP, stale_after=STALE_KEEP):
    """Archive omp rows that exited more than `exited_after` s ago or went stale more than `stale_after` s ago.
    Runs from the pump every minute. A pruned agent that comes back re-registers on its next heartbeat."""
    t, moved = now(), []
    with tx(c):
        for r in c.execute("SELECT * FROM agents WHERE kind='omp'").fetchall():
            s, age = status_of(r, t), t - (r["heartbeat_at"] or 0)
            if (s == "exited" and age > exited_after) or (s == "stale" and age > stale_after):
                archive_agent(c, r, s)
                moved.append(r["id"])
        if moved:
            event(c, "prune", moved=moved)
    return moved


def roster_canon(r):
    return json.dumps({"kind": "roster", "ts": r.get("ts"), "bots": r.get("bots")},
                      sort_keys=True, separators=(",", ":")).encode()


def op_roster(c, tok):
    """Apply the box's signed bot roster: upsert every bot it lists, archive box agents it no longer lists.
    Token: base64url(JSON {kind:"roster", ts, bots:[{name, label, grok_id}], sig}); sig = HMAC-SHA256 over the
    compact, key-sorted JSON of {kind, ts, bots} with the shared secret. An older or equal ts is ignored (replay)."""
    guard_on()
    try:
        r = from_token(tok)
    except Exception:
        raise Fail("rejected: not a roster token", 2)
    if not (isinstance(r, dict) and r.get("kind") == "roster" and isinstance(r.get("sig"), str)
            and hmac.compare_digest(r["sig"], hmac.new(key(), roster_canon(r), hashlib.sha256).hexdigest())):
        with tx(c):
            event(c, "reject", why="bad-roster-signature")
        raise Fail("rejected: bad roster signature", 2)
    bots = r.get("bots")
    if not (isinstance(r.get("ts"), int) and isinstance(bots, list) and bots and all(
            isinstance(b, dict) and isinstance(b.get("name"), str) and NAME_RE.match(b["name"]) for b in bots)):
        raise Fail("rejected: roster needs an integer ts and bots [{name: [a-z0-9._-], label?, grok_id?}]", 2)
    names = [b["name"] for b in bots]
    if "dross" not in names or len(set(names)) != len(names):
        raise Fail("rejected: roster must list dross, and each name once", 2)
    with tx(c):
        last = c.execute("SELECT v FROM meta WHERE k='roster_ts'").fetchone()
        if last and int(last["v"]) >= r["ts"]:
            return f"roster ts {r['ts']} is not newer than the applied one ({last['v']}); nothing changed"
        for b in bots:
            label, gid = str(b.get("label") or b["name"])[:80], str(b.get("grok_id") or "")[:80] or None
            if agent_row(c, b["name"]):
                c.execute("UPDATE agents SET label=?, session_id=? WHERE id=? AND kind='box'", (label, gid, b["name"]))
            else:
                c.execute("INSERT INTO agents(id, kind, label, session_id, state) VALUES(?, 'box', ?, ?, 'remote')",
                          (b["name"], label, gid))
        retired = [x for x in c.execute("SELECT * FROM agents WHERE kind='box'").fetchall() if x["id"] not in names]
        for x in retired:
            archive_agent(c, x, "retired")
        c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('roster_ts', ?)", (str(r["ts"]),))
        event(c, "roster", bots=names, retired=[x["id"] for x in retired])
        c.execute("UPDATE agents SET heartbeat_at=? WHERE id='dross'", (now(),))
    BOX.clear()
    BOX.update(names)
    return f"roster applied: {', '.join(names)}" + (f"; retired {', '.join(x['id'] for x in retired)}" if retired else "")


def ancestry_self(c):
    """The registered omp agent among this process's ancestors (omp -> bash tool -> shell -> this CLI, or omp -> MCP)."""
    out = run(["ps", "-axo", "pid=,ppid="], 1.0)
    if not out:
        return None
    parent = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            parent[int(p[0])] = int(p[1])
    pid, chain = os.getppid(), []
    while pid > 1 and len(chain) < 12:
        chain.append(pid)
        pid = parent.get(pid, 0)
    if not chain:
        return None
    q = ",".join("?" * len(chain))
    rows = c.execute(f"SELECT id, pid FROM agents WHERE kind='omp' AND host=? AND state!='exited' AND pid IN ({q})"
                     f" ORDER BY heartbeat_at DESC", [HOST] + chain).fetchall()
    return min(rows, key=lambda r: chain.index(r["pid"]))["id"] if rows else None


def caller(c, me=None):
    """Who is calling. Order: explicit `me`, AB_ME (set by the omp extension), AB_SELF, then process ancestry."""
    me = me or os.environ.get("AB_ME") or os.environ.get("AB_SELF") or ancestry_self(c)
    if not me:
        raise Fail("can't tell who you are: pass 'me' (your omp/<name> id; see bridge_whoami in an omp session)")
    me = as_addr(me)
    if not is_omp(me):
        raise Fail(f"'{me}' can't act from the Mac side (only omp/<name>; box agents arrive as signed tokens)")
    return resolve(c, me) or me


# ---- webhook pump (Mac -> box) ---------------------------------------------------------------------------------
def spawn(*args):
    """Detached, fire-and-forget child of this CLI. Never waited on."""
    if os.environ.get("AB_NO_SPAWN"):
        return
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + list(args), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=os.environ)


def post(url, tok, payload):
    """One webhook attempt with an absolute HTTP_S deadline. urlopen's timeout is per socket read, so a server that
    trickles bytes could stretch it; the request runs in a daemon thread that this short-lived process abandons."""
    req = urllib.request.Request(url, data=payload, method="POST", headers={"content-type": "application/json"})
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    out = ["timeout", "no answer within the deadline"]

    def attempt():
        try:
            with urllib.request.urlopen(req, timeout=HTTP_S) as r:
                out[:] = [r.status, r.read(200).decode("utf-8", "replace")]
        except urllib.error.HTTPError as e:
            out[:] = [e.code, e.read(200).decode("utf-8", "replace")]
        except Exception as e:
            out[:] = ["net-error", type(e).__name__]

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(HTTP_S)
    return tuple(out) if not t.is_alive() else ("timeout", "no answer within the deadline")


def op_pump(c, budget=45.0):
    """Send every due outbound row once. Bounded: one attempt is <= HTTP_S, the run stops claiming after `budget`."""
    if is_off():
        return 0
    url, tok = webhook()
    if not url:
        return 0
    start, sent = now(), 0
    while now() - start < budget:
        t = now()
        with tx(c):
            d = c.execute("SELECT msg_id, attempts FROM deliveries WHERE direction='out' AND state='pending' AND next_at<=? "
                          "AND (lease_until IS NULL OR lease_until<?) ORDER BY next_at LIMIT 1", (t, t)).fetchone()
            if not d:
                break
            c.execute("UPDATE deliveries SET lease_until=? WHERE msg_id=?", (t + HTTP_S + 15, d["msg_id"]))
        m = c.execute("SELECT * FROM messages WHERE id=?", (d["msg_id"],)).fetchone()
        payload = json.dumps({"agent_bridge": 1, "id": m["id"], "from": m["from_addr"], "to": m["to_addr"],
                              "token": token_of(m)}).encode()
        code, note = post(url, tok, payload)
        ok = isinstance(code, int) and 200 <= code < 300
        n = d["attempts"] + 1
        with tx(c):
            c.execute("UPDATE deliveries SET attempts=?, last_code=?, lease_until=NULL WHERE msg_id=?", (n, str(code), m["id"]))
            event(c, "webhook", m["id"], m["to_addr"], attempt=n, code=str(code), note=note[:200])
            if ok:
                advance(c, m["id"], "woken")
                c.execute("UPDATE agents SET heartbeat_at=? WHERE id=? AND kind='box'", (now(), m["to_addr"]))
                sent += 1
            elif n >= len(DELAYS) or (code in (401, 403, 404) and n >= 2):
                advance(c, m["id"], "gave_up")
            else:
                c.execute("UPDATE deliveries SET next_at=? WHERE msg_id=?",
                          (now() + DELAYS[n - 1] + random.uniform(0, 4), m["id"]))
    return sent


# ---- tmux last-resort nudge (opt-in via ~/.agent-bridge/inject/<name>) -----------------------------------------
def tmux(*args):
    return run([TMUX] + list(args), TMUX_S)


def nudge(c, mid):
    """For sessions without the extension only. Never types the body; every tmux call is bounded by TMUX_S."""
    m = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not m:
        return "no such message"
    r = agent_row(c, m["rcpt"]) if m["rcpt"] else None
    if r and r["ext_version"] and status_of(r) == "live":
        return "extension wakes it"
    name = m["to_addr"][4:]
    if not (H / "inject" / name).exists():
        return "no inject opt-in"
    pane = r["tmux_pane"] if r and r["tmux_pane"] else None
    if not pane:
        out = tmux("list-panes", "-t", f"={name}:", "-F", "#{pane_active}\t#{pane_id}")
        if out is None:
            return f"no tmux session {name}"
        pane = next((l.split("\t")[1] for l in out.splitlines() if l.startswith("1\t")), None)
        if not pane:
            return "no active pane"
    last = c.execute("SELECT max(t) FROM events WHERE ev='nudge' AND agent=?", (pane,)).fetchone()[0]
    if last and now() - last < NUDGE_GAP:
        return "nudged recently"
    mode = tmux("display-message", "-p", "-t", pane, "#{pane_in_mode}")
    if mode is None:
        return "tmux unavailable"
    if mode.strip() == "1" and tmux("send-keys", "-t", pane, "-X", "cancel") is None:
        return "could not leave copy mode"
    line = f"[agent-bridge] You have a new bridge message from {m['from_addr']}. Run bridge_inbox."
    if tmux("send-keys", "-t", pane, "-l", line) is None:
        return "typing failed"
    tmux("send-keys", "-t", pane, "Enter")
    with tx(c):
        event(c, "nudge", mid, pane)
    return "nudged"


# ---- v1 migration ----------------------------------------------------------------------------------------------
def op_migrate_v1(c):
    """Import v1 JSON messages once, idempotently. The JSON files stay where they are."""
    counts = {}
    plan = [(p, "in") for p in sorted((H / "inbox").glob("*/*.json"))] + \
           [(p, "box") for box in ("outbox", "sent", "archive") for p in sorted((H / box).glob("*.json"))]
    with tx(c):
        for p, kind in plan:
            try:
                m = json.loads(p.read_text())
                if not (ID_RE.match(str(m.get("id"))) and isinstance(m.get("body"), str) and m.get("sig")):
                    continue
            except Exception:
                continue
            v1 = m.get("state")
            if not insert(c, m, "v1", m.get("reply_to") or m["id"]):
                continue
            box = p.parent.name if kind == "box" else "inbox"
            if is_bot(m["to"]):
                # A fresh pending v1 send (an old session's MCP server after the cutover) still gets its webhook wake.
                fresh = v1 == "pending" and now() - m["ts"] < 3600
                state = {"woken": "woken", "delivered": "synced", "synced": "synced"}.get(v1, "pending" if fresh else "gave_up")
                if box == "sent":
                    state = "synced"
            else:
                state = {"read": "read", "done": "acked"}.get(v1, "queued")
                if box == "archive":
                    state = "acked"
            col = STAMP.get(state)
            c.execute(f"UPDATE deliveries SET state=?, next_at=?{', ' + col + '=?' if col else ''} WHERE msg_id=?",
                      (state, now() if state == "pending" else None, m.get("received") or m["ts"], m["id"]) if col
                      else (state, now() if state == "pending" else None, m["id"]))
            c.execute("UPDATE messages SET created_at=? WHERE id=?", (m.get("received") or m["ts"], m["id"]))
            counts[state] = counts.get(state, 0) + 1
        c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('v1_migrated_at', ?)", (str(int(now())),))
        event(c, "migrate-v1", **counts)
    return counts


# ---- status ----------------------------------------------------------------------------------------------------
def held_count(c):
    """Unread mail that no live agent answers to: its recipient left (often v1-era addresses). Kept, never dropped."""
    live = [[r["id"]] + json.loads(r["aliases"]) for r in c.execute("SELECT * FROM agents WHERE kind='omp'")
            if status_of(r) == "live"]
    rows = c.execute("SELECT m.to_addr, m.rcpt FROM messages m JOIN deliveries d ON d.msg_id=m.id "
                     "WHERE d.direction='in' AND d.state IN ('queued','notified')").fetchall()
    return sum(1 for m in rows if not any(m["rcpt"] == a[0] or (m["rcpt"] is None and m["to_addr"] in a) for a in live))


def status_json(c):
    url, tok = webhook()
    q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    oldest = q("SELECT min(m.ts) FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE d.direction='out' "
               "AND d.state IN ('pending','woken','gave_up')")
    out = {
        "version": VERSION, "home": str(H), "on": not is_off(), "secret": (H / "secret").exists(),
        "webhook": bool(url), "webhook_token": bool(tok),
        "outbox_waiting": q("SELECT count(*) FROM deliveries WHERE direction='out' AND state IN ('pending','woken','gave_up')"),
        "outbox_pending": q("SELECT count(*) FROM deliveries WHERE direction='out' AND state='pending'"),
        "outbox_gave_up": q("SELECT count(*) FROM deliveries WHERE direction='out' AND state='gave_up'"),
        "outbox_oldest_ts": oldest,
        "inbox_unread": q("SELECT count(*) FROM deliveries WHERE direction='in' AND state IN ('queued','notified')"),
        "inbox_held": held_count(c),
        "webhook_failures": q("SELECT count(*) FROM events WHERE ev='webhook' AND json_extract(detail,'$.code') "
                              "NOT IN ('200','201','202','204')"),
        "rejected": q("SELECT count(*) FROM events WHERE ev='reject'"),
        "agents_live": sum(1 for r in c.execute("SELECT * FROM agents WHERE kind='omp'") if status_of(r) == "live"),
    }
    last = c.execute("SELECT t, detail FROM events WHERE ev='webhook' AND json_extract(detail,'$.code') "
                     "NOT IN ('200','201','202','204') ORDER BY t DESC LIMIT 1").fetchone()
    out["webhook_last_failure"] = ({"code": json.loads(last["detail"])["code"],
                                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(last["t"]))} if last else None)
    return out


def op_status(c):
    s = status_json(c)
    lines = [f"agent-bridge {VERSION} side=mac home={H} {'ON' if s['on'] else 'OFF'}",
             f"secret: {'present' if s['secret'] else 'MISSING'}",
             f"webhook: {'configured' if s['webhook'] else 'NOT configured (sends queue for the sweep)'}"
             f", token {'set' if s['webhook_token'] else 'not set'}",
             f"outbox: {s['outbox_waiting']} awaiting confirmation"
             + (f" (oldest {int((now() - s['outbox_oldest_ts']) // 60)} min)" if s["outbox_oldest_ts"] else "")
             + f"; {s['outbox_pending']} still retrying the webhook, {s['outbox_gave_up']} left for the sweep"
             + f"; inbox unread: {s['inbox_unread']}"
             + (f" ({s['inbox_held']} held for addresses with no live agent)" if s["inbox_held"] else ""),
             f"webhook failures logged: {s['webhook_failures']}"
             + (f"; last: {s['webhook_last_failure']['code']} at {s['webhook_last_failure']['at']}"
                if s["webhook_last_failure"] else ""),
             f"rejected imports: {s['rejected']}",
             f"agents live: {s['agents_live']}"]
    return "\n".join(lines)


def message_json(m):
    keys = m.keys()
    return {"id": m["id"], "from": m["from_addr"], "to": m["to_addr"], "rcpt": m["rcpt"], "ts": m["ts"],
            "reply_to": m["reply_to"], "thread": m["thread"], "body": m["body"], "origin": m["origin"],
            "direction": m["direction"], "state": m["state"], "created_at": m["created_at"],
            **{k: m[k] for k in ("queued_at", "notified_at", "read_at", "acked_at", "woken_at", "synced_at",
                                 "attempts", "last_code") if k in keys}}


def op_messages(c, addr=None, limit=100):
    where, args = ("1=1", [])
    if addr:
        ids = addrs_of(c, addr)
        q = ",".join("?" * len(ids))
        where, args = f"(m.from_addr IN ({q}) OR m.to_addr IN ({q}) OR m.rcpt IN ({q}))", ids * 3
    rows = c.execute(f"SELECT m.*, d.* FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE {where} "
                     f"ORDER BY m.created_at DESC, m.id DESC LIMIT ?", args + [int(limit)]).fetchall()
    return [message_json(m) for m in rows]


# ---- MCP tools (stdio server, the Executor gateway, and the omp extension's native tools) ---------------------
S = {"type": "string"}
ME = {"type": "string", "description": "Your bridge id (omp/<name>). Filled in automatically inside omp; required when "
                                       "calling through Executor. Box agents cannot be named here."}


def tool(name, desc, props, req=()):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": {**props, "me": ME}, "required": list(req)}}


TOOLS = [
    tool("bridge_send", "Send a message to another agent: a box bot, or any omp worker (omp/<name>); see "
                        "bridge_list_agents. Point first, then context. Max 32 KB.",
         {"to": S, "message": S, "reply_to": {**S, "description": "id of the message you answer"},
          "thread": {**S, "description": "thread id to continue (defaults from reply_to)"}}, ("to", "message")),
    tool("bridge_inbox", "List your unread bridge messages (unread_only=false also shows read, unacked ones).",
         {"unread_only": {"type": "boolean"}, "limit": {"type": "integer"}}),
    tool("bridge_read", "Read one message by id and mark it read. Its header says who sent it and how to handle it; "
                        "follow that.",
         {"id": S}, ("id",)),
    tool("bridge_ack", "Mark a message handled. Unacked messages stay in your inbox (at-least-once delivery).",
         {"id": S}, ("id",)),
    tool("bridge_reply", "Reply to a message (to its sender, same thread) and ack it.", {"id": S, "message": S},
         ("id", "message")),
    tool("bridge_list_agents", "Directory of agents: omp sessions on the Mac (live, stale or exited) and the box bots.",
         {"filter": {"type": "string", "enum": ["live", "stale", "exited", "all"]},
          "name": {**S, "description": "substring of id or alias"}}),
    tool("bridge_whoami", "Your bridge id, aliases and registration.", {}),
    tool("bridge_status", "Bridge health: on/off, webhook queue, failures, live agents.", {}),
]


def call_tool(c, name, a):
    a = a or {}
    if is_off() and name != "bridge_status":
        raise Fail("bridge is OFF", 3)
    body = a.get("message") if a.get("message") is not None else a.get("body")
    if name == "bridge_status":
        return op_status(c)
    if name == "bridge_list_agents":
        return fmt_agents(op_agents(c, a.get("filter") or "live", a.get("name")))
    me = caller(c, a.get("me"))
    if name == "bridge_whoami":
        r = agent_row(c, me)
        if not r:
            return f"{me} (not registered: no omp extension session with this id)"
        return fmt_agents([agent_json(c, r)])
    if name == "bridge_send":
        if not a.get("to"):
            raise Fail("'to' is required")
        return op_send(c, me, a["to"], (body or "").strip(), a.get("reply_to"), a.get("thread"))[1]
    if name == "bridge_inbox":
        unread_only = a.get("unread_only", not a.get("all", False))
        return op_inbox(c, me, bool(unread_only), int(a.get("limit") or 20))
    if name == "bridge_read":
        return op_read(c, str(a.get("id", "")), me)
    if name == "bridge_ack":
        return op_ack(c, str(a.get("id", "")), me)
    if name == "bridge_reply":
        return op_reply(c, me, str(a.get("id", "")), (body or "").strip())
    raise Fail(f"unknown tool {name}")


def mcp_serve():
    cfg()
    c = connect()
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
            continue
        if meth == "initialize":
            res = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-bridge", "version": VERSION},
                   "instructions": f"Signed mailbox between {OWNER}'s omp sessions and their bots on the box. "
                                   f"Each message's bridge_read header says who sent it and how to handle it."}
        elif meth == "tools/list":
            res = {"tools": TOOLS}
        elif meth == "tools/call":
            p = req.get("params", {})
            try:
                txt, err = call_tool(c, p.get("name"), p.get("arguments") or {}), False
            except Fail as e:
                txt, err = f"error: {e}", True
            except Exception as e:
                txt, err = f"error: {type(e).__name__}: {e}", True
            res = {"content": [{"type": "text", "text": txt}], "isError": err}
        elif meth == "ping":
            res = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not found"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)


# ---- CLI -------------------------------------------------------------------------------------------------------
USAGE = """agent-bridge {v} (Mac side; store {db})
  send FROM TO [--reply-to ID] MESSAGE...   (or message on stdin)
  deliver TOKEN...             store box tokens and wake the recipient (returns in ~2s; never executes them)
  import-token TOKEN|@FILE     import-tokens FILE|-      (same as deliver, no wake)
  inbox [ADDR] [--all]         read ID          ack ID
  export                       outbound tokens the box has not confirmed (for the sweep)
  received-ids [--days N]      ids received from the box recently (for the box's 'mark')
  export-acks [--days N]       JSON lines: state of messages received from the box (queued/notified/read/acked)
  policy                       JSON {version, text}: the bridge policy omp sessions keep in context
  roster TOKEN|-               apply the box's signed bot roster (box/make-roster-token.py); retired bots are archived
  mark delivered|synced|read|acked ID...   box confirms outbound messages
  agents [--all|--stale|--exited] [--json] [NAME]    whoami     messages [--json] [--addr A] [--limit N]
  status [--json] | off | on | mcp | version | init --side mac | migrate-v1 | prune [--days N] | pump
  (used by the omp extension) register --json META | heartbeat ID [STATE] [--model M] [--provider P] | exit ID REASON
                               pending ID | call TOOL [--me ID] [--args JSON]   (JSON may also come on stdin)"""


def opt(a, name, default=None):
    if name in a:
        i = a.index(name)
        v = a[i + 1] if i + 1 < len(a) else default
        del a[i:i + 2]
        return v
    return default


def flag(a, name):
    if name in a:
        a.remove(name)
        return True
    return False


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.format(v=VERSION, db=DB))
        return 0
    cmd, a = argv[0], list(argv[1:])
    if cmd == "version":
        print(VERSION)
        return 0
    if cmd == "init":
        if opt(a, "--side") != "mac":
            raise Fail("v2 is Mac only: init --side mac")
        for d in ("inbox", "outbox", "sent", "archive", "logs", "incoming", "inject"):
            (H / d).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(H, 0o700)
        if not (H / "config.json").exists():
            (H / "config.json").write_text(json.dumps({"side": "mac", "version": VERSION}))
        connect()
        print(f"initialised {H} (side=mac), store {DB}")
        return 0
    if cmd == "off":
        (H / "OFF").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z\n"))
        print("agent-bridge is OFF")
        return 0
    if cmd == "on":
        (H / "OFF").unlink(missing_ok=True)
        print("agent-bridge is ON")
        return 0
    if cmd == "mcp":
        mcp_serve()
        return 0
    cfg()
    c = connect()
    if cmd == "_wake":
        # Sent by v1 MCP servers still running in sessions started before the cutover: they wrote a v1 outbox file.
        op_migrate_v1(c)
        op_pump(c)
        return 0
    if cmd in ("_pump", "pump"):
        op_prune(c)
        n = op_pump(c)
        if cmd == "pump":
            print(f"woke {n}")
        return 0
    if cmd == "_notify":
        r = nudge(c, a[0])
        if os.environ.get("AB_DEBUG"):
            print(r)
        return 0
    if cmd == "status":
        print(json.dumps(status_json(c)) if flag(a, "--json") else op_status(c))
        return 0
    if cmd == "send":
        rt = opt(a, "--reply-to")
        if len(a) < 2:
            raise Fail("send FROM TO MESSAGE")
        body = " ".join(a[2:]) if len(a) > 2 else sys.stdin.read()
        print(op_send(c, a[0], a[1], body.strip(), rt)[1])
        return 0
    if cmd == "inbox":
        allm = flag(a, "--all")
        addr = next((x for x in a if not x.startswith("--")), None)
        print(op_inbox(c, as_addr(addr) if addr else None, not allm, 500))
        return 0
    if cmd == "read":
        print(op_read(c, a[0] if a else ""))
        return 0
    if cmd == "ack":
        print(op_ack(c, a[0] if a else ""))
        return 0
    if cmd in ("import-token", "import-tokens", "deliver"):
        if cmd == "import-tokens":
            src = sys.stdin.read() if a == ["-"] else Path(a[0]).read_text()
            toks = src.split()
        elif cmd == "import-token" and a and a[0].startswith("@"):
            raw = Path(a[0][1:]).read_text().strip()
            try:
                raw = json.loads(raw)["token"]
            except Exception:
                pass
            toks = [raw]
        elif cmd == "deliver" and not a and not sys.stdin.isatty():
            toks = sys.stdin.read().split()
        else:
            toks = a
        ok_all = True
        for t in toks:
            ok, stored, msg = op_import(c, t, cmd)
            ok_all &= ok
            if stored and cmd == "deliver":
                maybe_nudge(from_token(t)["to"], from_token(t)["id"])
                msg += "; recipient notified"
            print(msg)
        return 0 if ok_all else 2
    if cmd == "export":
        rows = c.execute("SELECT m.* FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE d.direction='out' "
                         "AND d.state IN ('pending','woken','gave_up') ORDER BY m.id").fetchall()
        seen(c, "dross")
        if rows:
            print("\n".join(token_of(m) for m in rows))
        return 0
    if cmd in ("received-ids", "export-acks"):
        cut = now() - int(opt(a, "--days", "3")) * 86400
        rows = c.execute("SELECT m.id, m.to_addr, d.* FROM messages m JOIN deliveries d ON d.msg_id=m.id WHERE "
                         "d.direction='in' AND m.from_addr NOT LIKE 'omp/%' AND m.created_at>=? "
                         "ORDER BY m.id", (cut,)).fetchall()
        seen(c, "dross")
        if cmd == "received-ids":
            if rows:
                print(" ".join(r["id"] for r in rows))
        else:
            for r in rows:
                print(json.dumps({"id": r["id"], "to": r["to_addr"], "state": r["state"], "notified_at": r["notified_at"],
                                  "read_at": r["read_at"], "acked_at": r["acked_at"]}))
        return 0
    if cmd == "mark":
        if not a or a[0] not in ("delivered", "synced", "read", "acked"):
            raise Fail("mark delivered|synced|read|acked ID...")
        state = "synced" if a[0] == "delivered" else a[0]
        out = []
        with tx(c):
            c.execute("UPDATE agents SET heartbeat_at=? WHERE id='dross'", (now(),))
            for mid in a[1:]:
                d = c.execute("SELECT direction, state FROM deliveries WHERE msg_id=?", (mid,)).fetchone()
                if not ID_RE.match(mid) or not d or d["direction"] != "out":
                    out.append(f"{mid}: not in outbox")
                    continue
                if state in ("read", "acked"):
                    advance(c, mid, "synced")
                advance(c, mid, state)
                out.append(f"{mid}: {a[0]}")
        print("\n".join(out))
        return 0
    if cmd == "agents":
        which = "all" if flag(a, "--all") else "stale" if flag(a, "--stale") else "exited" if flag(a, "--exited") else "live"
        js = flag(a, "--json")
        rows = op_agents(c, which, a[0] if a else None)
        print(json.dumps(rows) if js else fmt_agents(rows))
        return 0
    if cmd == "messages":
        js = flag(a, "--json")
        addr, limit = opt(a, "--addr"), int(opt(a, "--limit", "100"))
        rows = op_messages(c, as_addr(addr) if addr else None, limit)
        print(json.dumps(rows) if js else "\n".join(f"{m['id']} {m['state']:<8} {m['from']} -> {m['to']}" for m in rows))
        return 0
    if cmd == "whoami":
        print(call_tool(c, "bridge_whoami", {}))
        return 0
    if cmd == "policy":
        print(json.dumps({"version": POLICY_VERSION, "text": POLICY}))
        return 0
    if cmd == "roster":
        print(op_roster(c, (sys.stdin.read() if not a or a == ["-"] else a[0]).strip()))
        return 0
    if cmd == "migrate-v1":
        print(json.dumps(op_migrate_v1(c)))
        return 0
    if cmd == "prune":
        days = opt(a, "--days")
        moved = op_prune(c, *([float(days) * 86400] * 2 if days else []))
        print(f"archived {len(moved)} agent rows" + (": " + " ".join(moved) if moved else ""))
        return 0
    if cmd == "register":
        print(json.dumps(op_register(c, json.loads(opt(a, "--json") or sys.stdin.read() or "{}"))))
        return 0
    if cmd == "heartbeat":
        model, provider, ext = opt(a, "--model"), opt(a, "--provider"), opt(a, "--ext")
        print(json.dumps(op_heartbeat(c, a[0], a[1] if len(a) > 1 else None, model, provider, ext)))
        return 0
    if cmd == "exit":
        print(json.dumps(op_exit(c, a[0], a[1] if len(a) > 1 else "quit")))
        return 0
    if cmd == "pending":
        print(json.dumps(op_pending(c, a[0])))
        return 0
    if cmd == "call":
        me = opt(a, "--me")
        args = json.loads(opt(a, "--args") or sys.stdin.read() or "{}")
        if me:
            args["me"] = me
        try:
            print(call_tool(c, a[0], args))
            return 0
        except Fail as e:
            print(f"error: {e}")
            return 1
    raise Fail(f"unknown command {cmd}; see agent-bridge help")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print(f"agent-bridge: {e}", file=sys.stderr)
        sys.exit(e.code)
