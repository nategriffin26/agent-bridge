#!/usr/bin/env python3
"""End-to-end: a fresh omp worker that Dross did not start (no AB_SELF), in a throwaway tmux session `ab2-e2e`.

  python3 tests/e2e.py [--model anthropic/claude-sonnet-5-5] [--installed]

It must auto-register; a box-signed token from dross (made by the v1 box code) is delivered with the Mac CLI; the
extension must wake the worker (no tmux typing: there is no inject opt-in); the worker reads and replies; the reply
must land addressed to dross and the webhook (a local mock, never the real one) must be attempted.
Everything runs against a throwaway AB_HOME. --installed uses ~/bin/agent-bridge and the extension omp discovers
on its own; the default passes the repo's CLI and extension explicitly. Prints timings as JSON at the end.
"""
import http.server, json, os, shutil, subprocess, sys, threading, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = Path("/tmp/ab2-e2e")
SESSION = "ab2-e2e"
MODEL = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "anthropic/claude-sonnet-5-5"
INSTALLED = "--installed" in sys.argv
CLI = str(Path.home() / "bin" / "agent-bridge") if INSTALLED else str(REPO / "src" / "agent_bridge.py")
hooks = []


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        hooks.append((time.time(), json.loads(self.rfile.read(int(self.headers["content-length"])))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


def sh(*cmd, timeout=10, env=None, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and r.returncode != 0:
        raise SystemExit(f"{cmd} failed: {r.stderr or r.stdout}")
    return r.stdout


def wait(what, fn, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    raise SystemExit(f"timed out after {timeout}s waiting for: {what}\n--- pane ---\n{pane()}")


def pane():
    return sh("tmux", "capture-pane", "-p", "-t", f"={SESSION}:", "-S", "-60", check=False, timeout=2)


def main():
    if subprocess.run(["tmux", "has-session", "-t", f"={SESSION}"], capture_output=True, timeout=2).returncode == 0:
        raise SystemExit(f"tmux session {SESSION} already exists; stop it first")
    if ROOT.exists():
        ROOT.rename(ROOT.with_name(f"ab2-e2e.prev-{int(time.time())}"))
    mac, box, work = ROOT / "home", ROOT / "box", ROOT / SESSION
    for d in (mac, box, work):
        d.mkdir(parents=True)
    secret = os.urandom(32).hex()
    for home, side in ((mac, "mac"), (box, "box")):
        (home / "config.json").write_text(json.dumps({"side": side}))
        (home / "secret").write_text(secret)
        os.chmod(home / "secret", 0o600)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (mac / "webhook.env").write_text(f"AB_WEBHOOK_URL=http://127.0.0.1:{srv.server_address[1]}/hook\n")

    env = {k: v for k, v in os.environ.items() if not k.startswith(("AB_", "TMUX"))}
    menv = {**env, "AB_HOME": str(mac)}
    ab = lambda *a, **k: sh(CLI, *a, env=menv, **k)
    ab("init", "--side", "mac")
    t = {}

    launch = f"cd {work} && exec env -u AB_SELF AB_HOME={mac} AB_CLI={CLI} {shutil.which('omp')} --model {MODEL}"
    if not INSTALLED:
        launch += f" -e {REPO / 'extension' / 'agent-bridge.ts'}"
    t0 = time.time()
    sh("tmux", "new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "-c", str(work), launch)
    me = wait("auto-registration", lambda: next((a for a in json.loads(ab("agents", "--json"))
                                                 if f"omp/{SESSION}" in a["aliases"]), None), 90)
    t["register_s"] = round(time.time() - t0, 2)
    assert me["id"].startswith(f"omp/{SESSION}-") and len(me["id"]) == len(f"omp/{SESSION}-") + 4, me["id"]
    assert me["tmux_target"] and me["model"], me
    print(f"registered {me['id']} aka {me['aliases']} tmux {me['tmux_target']} {me['provider']}/{me['model']}")
    wait("idle", lambda: next(a for a in json.loads(ab("agents", "--json")) if a["id"] == me["id"])["state"] == "idle", 60)

    out = sh(sys.executable, str(REPO / "tests" / "v1" / "agent-bridge-v1.py"), "send", "dross", f"omp/{SESSION}",
             "E2E check from dross: please answer this message with bridge_reply, text 'pong', and nothing else.",
             env={**env, "AB_HOME": str(box), "AB_NO_SPAWN": "1"})
    token = out.split("deliver ")[1].split()[0]
    t0 = time.time()
    out = ab("deliver", token)
    t["deliver_s"] = round(time.time() - t0, 3)
    mid = out.split()[1]
    print(out.strip())
    assert t["deliver_s"] < 2, t

    db = lambda: {m["id"]: m for m in json.loads(ab("messages", "--json", "--limit", "20"))}
    wait("extension pointer (notified)", lambda: db()[mid]["notified_at"], 30)
    t["notified_s"] = round(db()[mid]["notified_at"] - t0, 2)
    reply = wait("reply to dross", lambda: next((m for m in db().values() if m["reply_to"] == mid), None), 180)
    t["reply_s"] = round(reply["created_at"] - t0, 2)
    t["read_s"] = round(db()[mid]["read_at"] - t0, 2)   # bridge_read, or the reply itself (answering implies reading)
    assert (reply["from"], reply["to"], reply["thread"]) == (me["id"], "dross", mid), reply
    assert db()[mid]["state"] == "acked", db()[mid]
    wait("webhook attempted", lambda: any(h[1]["id"] == reply["id"] for h in hooks), 30)
    hook = next(h for h in hooks if h[1]["id"] == reply["id"])
    t["webhook_s"] = round(hook[0] - t0, 2)
    assert {k: hook[1][k] for k in ("agent_bridge", "from", "to")} == {"agent_bridge": 1, "from": me["id"], "to": "dross"}
    imported = sh(sys.executable, str(REPO / "tests" / "v1" / "agent-bridge-v1.py"), "import-token", hook[1]["token"],
                  env={**env, "AB_HOME": str(box)})
    assert imported.startswith("stored"), imported
    wait("woken", lambda: db()[reply["id"]]["state"] == "woken", 10)
    print(f"reply {reply['id']}: {reply['body']!r}; box v1 import: {imported.strip()}")
    assert not any((mac / "inject").iterdir()), "no inject opt-in: the wake must come from the extension"

    # quit the worker (this session was created here): it must mark itself exited
    sh("tmux", "send-keys", "-t", f"={SESSION}:", "-l", "/quit", timeout=2)
    sh("tmux", "send-keys", "-t", f"={SESSION}:", "Enter", timeout=2)
    wait("exited on quit", lambda: next(a for a in json.loads(ab("agents", "--all", "--json"))
                                        if a["id"] == me["id"])["status"] == "exited", 30)
    subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True, timeout=2)
    srv.shutdown()
    print(json.dumps({"ok": True, "agent": me["id"], "model": MODEL, "timings": t}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True, timeout=2)
        raise
