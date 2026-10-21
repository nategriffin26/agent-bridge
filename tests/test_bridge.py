"""agent-bridge v2: store, signing, webhook pump, tmux nudge, and the timeouts that keep everything from hanging.

Run: python3 -m unittest discover -s tests -v
Each test runs the real CLI against throwaway homes; the box side is the pinned v1 file (tests/v1), which is what the
box runs, so tokens cross between the two exactly as they do in production.
"""
import http.server, json, os, sqlite3, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "src" / "agent_bridge.py"
V1 = REPO / "tests" / "v1" / "agent-bridge-v1.py"
FAKE_TMUX = REPO / "tests" / "fake_tmux.py"
SECRET = "test-secret-" + "x" * 40


class Hook(http.server.BaseHTTPRequestHandler):
    """Mock Grok webhook. Class attributes steer it per test."""
    mode, bodies, auth = "ok", [], []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        Hook.bodies.append(json.loads(body))
        Hook.auth.append(self.headers.get("authorization"))
        if Hook.mode == "slow":
            time.sleep(20)
        code = 200 if Hook.mode in ("ok", "slow", "trickle") else 500
        self.send_response(code)
        self.end_headers()
        if Hook.mode == "trickle":          # each byte inside any per-read timeout, the whole answer far past it
            for _ in range(40):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.3)
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class Bridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/hook"
        # A live pid that is not an ancestor of the CLI, so ancestry identity never matches by accident.
        cls.sleeper = subprocess.Popen(["sleep", "600"])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.sleeper.kill()
        cls.sleeper.wait()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mac, self.box = root / "mac", root / "box"
        for home, side in ((self.mac, "mac"), (self.box, "box")):
            home.mkdir()
            (home / "config.json").write_text(json.dumps({"side": side, "owner": "Ada"}))
            (home / "secret").write_text(SECRET)
            os.chmod(home / "secret", 0o600)
        self.tlog = root / "tmux.log"
        Hook.mode, Hook.bodies, Hook.auth = "ok", [], []
        self.ab("init", "--side", "mac")

    def tearDown(self):
        if getattr(self, "_db", None):
            self._db.close()
        self.tmp.cleanup()

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("AB_", "TMUX"))}
        e.update(AB_HOME=str(self.mac), AB_NO_SPAWN="1", AB_TMUX=str(FAKE_TMUX), FAKE_TMUX_LOG=str(self.tlog))
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def ab(self, *args, input=None, check=True, timeout=20, **env):
        r = subprocess.run([sys.executable, str(CLI), *args], input=input, capture_output=True, text=True,
                           env=self.env(**env), timeout=timeout)
        if check and r.returncode != 0:
            self.fail(f"agent-bridge {args} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def v1(self, *args):
        """The box side: the real v1 code with side=box."""
        e = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
        e.update(AB_HOME=str(self.box), AB_NO_SPAWN="1")
        r = subprocess.run([sys.executable, str(V1), *args], capture_output=True, text=True, env=e, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def box_token(self, to, body="hello from the box", reply_to=None):
        out = self.v1("send", "dross", to, *(["--reply-to", reply_to] if reply_to else []), body)
        return out.split("deliver ")[1].split()[0]

    def register(self, session_id="sess-1", cwd="/tmp/proj", pid=None, **meta):
        meta = {"session_id": session_id, "cwd": cwd, "pid": pid or self.sleeper.pid, "ext_version": "test", **meta}
        return json.loads(self.ab("register", input=json.dumps(meta)).stdout)

    def db(self):
        if not getattr(self, "_db", None):
            self._db = sqlite3.connect(str(self.mac / "v2" / "bridge.db"))
            self._db.row_factory = sqlite3.Row
        return self._db

    def state(self, mid):
        return self.db().execute("SELECT state FROM deliveries WHERE msg_id=?", (mid,)).fetchone()["state"]

    def call(self, tool, me, **args):
        r = self.ab("call", tool, "--me", me, input=json.dumps(args), check=False)
        return r.returncode, r.stdout.strip()

    # ---- identity and registry ----------------------------------------------------------------------------------
    def test_identity_is_stable_and_derived_from_label_and_session(self):
        a = self.register("abc", cwd="/Users/x/My Project")
        self.assertRegex(a["id"], r"^omp/my-project-[0-9a-f]{4}$")
        self.assertEqual(self.register("abc", cwd="/Users/x/My Project")["id"], a["id"])     # /reload, /resume
        self.assertNotEqual(self.register("other", cwd="/Users/x/My Project")["id"], a["id"])  # /new
        self.assertEqual(self.register("abc", ab_self="omp/bridge2")["id"], "omp/bridge2")    # AB_SELF verbatim
        t = self.register("abc", tmux="/tmp/sock", tmux_pane="%5")
        self.assertRegex(t["id"], r"^omp/e2e-[0-9a-f]{4}$")
        self.assertEqual((t["tmux_target"], t["aliases"]), ("e2e:0.1", ["omp/e2e"]))

    def test_stale_when_heartbeat_old_or_pid_dead_and_exited_on_quit(self):
        live = self.register("s1")["id"]
        dead = self.register("s2", cwd="/tmp/dead", pid=99999999)["id"]
        old = self.register("s3", cwd="/tmp/old")["id"]
        self.db().execute("UPDATE agents SET heartbeat_at=heartbeat_at-200 WHERE id=?", (old,)).connection.commit()
        rows = {a["id"]: a["status"] for a in json.loads(self.ab("agents", "--all", "--json").stdout)}
        self.assertEqual((rows[live], rows[dead], rows[old], rows["dross"]), ("live", "stale", "stale", "box"))
        self.ab("exit", live, "quit")
        self.assertEqual(json.loads(self.ab("heartbeat", live, "idle").stdout), {"reregister": True})
        self.assertNotIn(live, self.ab("agents").stdout)
        self.ab("prune", "--days", "0")
        self.assertEqual(self.db().execute("SELECT count(*) FROM agents_archive").fetchone()[0], 3)

    def test_pump_prunes_exited_after_an_hour_and_stale_after_a_day(self):
        gone = self.register("s1", cwd="/tmp/gone")["id"]
        fresh = self.register("s2", cwd="/tmp/fresh")["id"]
        stale = self.register("s3", cwd="/tmp/stale")["id"]
        self.ab("exit", gone, "quit")
        self.ab("exit", fresh, "quit")
        db = self.db()
        db.execute("UPDATE agents SET heartbeat_at=heartbeat_at-3700 WHERE id=?", (gone,))
        db.execute("UPDATE agents SET heartbeat_at=heartbeat_at-3700 WHERE id=?", (stale,))   # stale 1 h: kept
        db.commit()
        self.ab("_pump")
        ids = {a["id"] for a in json.loads(self.ab("agents", "--all", "--json").stdout)}
        self.assertEqual((gone in ids, fresh in ids, stale in ids), (False, True, True))
        db.execute("UPDATE agents SET heartbeat_at=heartbeat_at-90000 WHERE id=?", (stale,)).connection.commit()
        self.ab("_pump")
        self.assertNotIn(stale, self.ab("agents", "--all").stdout)
        self.register("s1", cwd="/tmp/gone")                                       # a pruned agent can come back
        self.assertIn(gone, self.ab("agents").stdout)

    def roster(self, bots, ts=None):
        """A roster token made by the box-side script, from the box's own secret."""
        e = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
        r = subprocess.run([sys.executable, str(REPO / "box" / "make-roster-token.py"), "-"], input=json.dumps(bots),
                           capture_output=True, text=True, env={**e, "AB_HOME": str(self.box)}, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_box_roster_sets_the_bots_and_archives_retired_ones(self):
        old = self.roster([{"name": "dross"}, {"name": "scout"}])
        self.assertIn("roster applied: dross, scout", self.ab("roster", old).stdout)
        tok = self.roster([{"name": "dross", "label": "Dross", "grok_id": "5d8e"}, {"name": "ops", "label": "Ops"},
                           {"name": "dr-eggbot", "label": "dr eggbot"}])
        out = self.ab("roster", input=tok).stdout
        self.assertIn("retired scout", out)
        box = {a["id"]: a for a in json.loads(self.ab("agents", "--all", "--json").stdout) if a["kind"] == "box"}
        self.assertEqual(sorted(box), ["dr-eggbot", "dross", "ops"])
        self.assertEqual((box["dross"]["label"], box["dross"]["session_id"]), ("Dross", "5d8e"))
        archived = [json.loads(r[0]) for r in self.db().execute("SELECT row FROM agents_archive")]
        self.assertEqual([(r["id"], r["archived_why"]) for r in archived], [("scout", "retired")])
        self.assertIn("not newer", self.ab("roster", old).stdout)                   # replaying the old roster
        self.assertNotIn("scout", self.ab("agents", "--all").stdout)
        me = self.register("s1")["id"]
        self.assertEqual(self.call("bridge_send", me, to="ops", message="hi")[0], 0)  # a new bot is a recipient
        code, text = self.call("bridge_send", me, to="scout", message="hi")
        self.assertEqual(code, 1)                                                    # a retired one is not, and
        self.assertIn("not a valid recipient", text)                                 # never becomes omp/scout
        import base64
        r = json.loads(base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)))
        r["bots"].append({"name": "evil"})
        bad = base64.urlsafe_b64encode(json.dumps(r).encode()).decode().rstrip("=")
        self.assertEqual(self.ab("roster", bad, check=False).returncode, 2)
        self.assertEqual(self.ab("roster", self.roster([{"name": "ops"}]), check=False).returncode, 2)  # no dross

    def test_reads_ride_out_a_brief_lock(self):
        """status/messages/agents crashed with 'database is locked' when a read met a writer's checkpoint."""
        hold = sqlite3.connect(str(self.mac / "v2" / "bridge.db"), isolation_level=None, check_same_thread=False)
        hold.execute("PRAGMA locking_mode=EXCLUSIVE")
        hold.execute("BEGIN EXCLUSIVE")
        hold.execute("INSERT INTO meta(k, v) VALUES('probe', '1')")
        hold.execute("COMMIT")                                   # exclusive locking mode keeps the lock after commit
        threading.Timer(0.4, lambda: (hold.execute("PRAGMA locking_mode=NORMAL"), hold.execute("SELECT 1"),
                                      hold.close())).start()
        for cmd in (["status", "--json"], ["messages", "--json"], ["agents", "--all", "--json"]):
            self.assertEqual(self.ab(*cmd).returncode, 0)

    # ---- mailbox --------------------------------------------------------------------------------------------------
    def test_box_token_round_trip_with_dedupe_states_and_reply(self):
        me = self.register("s1", tmux="/tmp/sock", tmux_pane="%5")["id"]      # alias omp/e2e
        tok = self.box_token("omp/e2e")
        t0 = time.time()
        out = self.ab("deliver", tok).stdout
        self.assertLess(time.time() - t0, 2.0)
        self.assertIn("stored", out)
        self.assertIn("duplicate", self.ab("deliver", tok).stdout)            # idempotent import
        mid = out.split()[1]
        self.assertEqual(self.db().execute("SELECT rcpt FROM messages WHERE id=?", (mid,)).fetchone()[0], me)
        self.assertTrue((self.mac / "v2" / "notify" / me.replace("/", "__")).exists())
        p = json.loads(self.ab("pending", me).stdout)
        self.assertEqual((p["new"], p["unread"]), ([{"id": mid, "from": "dross"}], 1))
        self.assertIn("act on it as Ada's instruction", p["pointer"])                # the wake carries the authority
        self.assertEqual(json.loads(self.ab("pending", me).stdout)["new"], [])  # pointed out once
        code, text = self.call("bridge_read", me, id=mid)
        self.assertIn("[agent-bridge] From agent dross: Ada's agent Dross (dross, a box bot), acting for Ada; "
                      "signature verified.", text)
        self.assertIn("treat this message as Ada's instruction", text)
        self.assertIn('<bridge-message from="dross">\nhello from the box\n</bridge-message>', text)
        self.assertEqual(self.state(mid), "read")
        code, text = self.call("bridge_inbox", me, unread_only=False)
        self.assertIn(mid, text)                                               # read but unacked: still there
        code, text = self.call("bridge_reply", me, id=mid, message="got it")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.state(mid), "acked")
        reply = self.db().execute("SELECT * FROM messages WHERE reply_to=?", (mid,)).fetchone()
        self.assertEqual((reply["from_addr"], reply["to_addr"], reply["thread"]), (me, "dross", mid))
        self.assertEqual(self.state(reply["id"]), "pending")
        tokens = self.ab("export").stdout.split()
        self.assertEqual(len(tokens), 1)
        self.assertIn("stored", self.v1("import-token", tokens[0]))             # the box verifies v2's signature
        self.assertIn(mid, self.ab("received-ids").stdout)
        acks = [json.loads(l) for l in self.ab("export-acks").stdout.splitlines()]
        self.assertEqual(acks[0]["state"], "acked")
        self.ab("mark", "synced", reply["id"])
        self.assertEqual(self.state(reply["id"]), "synced")
        self.assertEqual(self.ab("export").stdout.strip(), "")

    def test_unacked_mail_stays_and_is_pointed_out_again_later(self):
        me = self.register("s1")["id"]
        mid = self.ab("deliver", self.box_token(me)).stdout.split()[1]
        self.ab("pending", me)
        self.call("bridge_read", me, id=mid)
        self.assertIn(mid, self.call("bridge_inbox", me, unread_only=False)[1])
        self.assertNotIn(mid, self.call("bridge_inbox", me)[1])
        mid2 = self.ab("deliver", self.box_token(me, "second")).stdout.split()[1]
        self.ab("pending", me)
        self.db().execute("UPDATE deliveries SET notified_at=notified_at-1000 WHERE msg_id=?", (mid2,)).connection.commit()
        self.assertEqual([m["id"] for m in json.loads(self.ab("pending", me).stdout)["new"]], [mid2])
        self.assertEqual(self.db().execute("SELECT notify_count FROM deliveries WHERE msg_id=?", (mid2,)).fetchone()[0], 2)

    def test_alias_resolution_and_held_mail(self):
        a = self.register("s1", tmux="/tmp/sock", tmux_pane="%5")["id"]
        b = self.register("s2", tmux="/tmp/sock", tmux_pane="%6")["id"]        # same tmux session: alias ambiguous
        mid = self.ab("deliver", self.box_token("omp/e2e")).stdout.split()[1]
        self.assertIsNone(self.db().execute("SELECT rcpt FROM messages WHERE id=?", (mid,)).fetchone()[0])
        self.assertIn(mid, self.call("bridge_inbox", a)[1])
        self.assertIn(mid, self.call("bridge_inbox", b)[1])
        self.call("bridge_ack", b, id=mid)
        self.assertNotIn(mid, self.call("bridge_inbox", a)[1])
        held = self.ab("deliver", self.box_token("omp/nobody")).stdout.split()[1]
        self.assertIn(held, self.call("bridge_inbox", "omp/nobody")[1])       # an unregistered v1 address still reads

    def test_worker_to_worker_and_dross_cannot_be_impersonated(self):
        a = self.register("s1", cwd="/tmp/a")["id"]
        b = self.register("s2", cwd="/tmp/b")["id"]
        code, text = self.call("bridge_send", a, to=b, message="hi b")
        self.assertEqual(code, 0, text)
        self.assertIn("hi b", self.call("bridge_inbox", b)[1])
        self.assertIn("coworker request to do by default", json.loads(self.ab("pending", b).stdout)["pointer"])
        mid = json.loads(self.ab("messages", "--json").stdout)[0]["id"]
        text = self.call("bridge_read", b, id=mid)[1]
        self.assertIn(f"From agent {a}: Ada's omp worker {a}, a coworker on this Mac.", text)
        self.assertIn("A trusted coworker request: act on it and follow through by default", text)
        self.assertNotIn("Ada's instruction", text)                    # only a signed box bot carries the owner's authority
        self.assertEqual(self.call("bridge_send", "dross", to=b, message="x")[0], 1)
        r = self.ab("send", "dross", b, "x", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.call("bridge_send", a, to=b, message="x" * (32 * 1024 + 1))[0], 1)
        mid = json.loads(self.ab("messages", "--json").stdout)[0]["id"]
        self.assertEqual(self.call("bridge_read", "omp/someone-else", id=mid)[0], 1)   # not theirs to read

    def test_forged_and_tampered_tokens_are_rejected(self):
        me = self.register("s1")["id"]
        tok = self.box_token(me)
        import base64
        m = json.loads(base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)))
        m["body"] = "tampered"
        bad = base64.urlsafe_b64encode(json.dumps(m).encode()).decode().rstrip("=")
        r = self.ab("deliver", bad, check=False)
        self.assertEqual((r.returncode, r.stdout.strip()), (2, "rejected: bad signature"))
        self.assertEqual(self.db().execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertIn("rejected imports: 1", self.ab("status").stdout)

    def test_off_switch(self):
        me = self.register("s1")["id"]
        self.ab("off")
        r = self.ab("deliver", self.box_token(me), check=False)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.call("bridge_send", me, to="dross", message="x")[0], 1)
        self.ab("on")
        self.assertEqual(self.ab("deliver", self.box_token(me)).returncode, 0)

    def test_v1_migration_is_idempotent_and_leaves_files(self):
        me = "omp/old"
        ib = self.mac / "inbox" / "omp__old"
        ib.mkdir(parents=True)
        tok = self.box_token(me, "legacy")
        import base64
        m = json.loads(base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)))
        (ib / f"{m['id']}.json").write_text(json.dumps({**m, "state": "unread", "received": m["ts"]}))
        out = {**m, "id": "1700000000000-deadbeef", "from": me, "to": "dross", "state": "woken"}
        (self.mac / "outbox").mkdir(exist_ok=True)
        (self.mac / "outbox" / f"{out['id']}.json").write_text(json.dumps(out))
        self.assertEqual(json.loads(self.ab("migrate-v1").stdout), {"queued": 1, "woken": 1})
        self.assertEqual(json.loads(self.ab("migrate-v1").stdout), {})
        self.assertTrue((ib / f"{m['id']}.json").exists())
        self.assertIn("legacy", self.call("bridge_inbox", me)[1])

    # ---- Mac -> box webhook ---------------------------------------------------------------------------------------
    def hook(self, url):
        (self.mac / "webhook.env").write_text(f"AB_WEBHOOK_URL={url}\nAB_WEBHOOK_TOKEN=tok123\n")

    def test_webhook_wakes_box_with_v1_payload(self):
        self.hook(self.url)
        me = self.register("s1")["id"]
        mid = self.call("bridge_send", me, to="dross", message="ping")[1].split()[1]
        self.ab("pump")
        self.assertEqual(self.state(mid), "woken")
        body = Hook.bodies[-1]
        self.assertEqual({k: body[k] for k in ("agent_bridge", "id", "from", "to")},
                         {"agent_bridge": 1, "id": mid, "from": me, "to": "dross"})
        self.assertEqual(Hook.auth[-1], "Bearer tok123")
        self.assertIn("stored", self.v1("import-token", body["token"]))

    def test_v1_session_send_after_cutover_still_wakes_the_box(self):
        """A v1 MCP server left running in an old session writes outbox JSON and spawns `_wake <id>` on the new CLI."""
        self.hook(self.url)
        v1home = dict(AB_HOME=str(self.mac), AB_NO_SPAWN="1")
        e = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
        out = subprocess.run([sys.executable, str(V1), "send", "omp/old", "dross", "from an old session"],
                             capture_output=True, text=True, env={**e, **v1home}, timeout=20).stdout
        mid = out.split()[1]
        self.ab("_wake", mid)
        self.assertEqual(self.state(mid), "woken")
        self.assertEqual(Hook.bodies[-1]["id"], mid)

    def test_webhook_down_retries_with_backoff_then_leaves_it_for_the_sweep(self):
        self.hook("http://127.0.0.1:9/down")
        me = self.register("s1")["id"]
        mid = self.call("bridge_send", me, to="dross", message="ping")[1].split()[1]
        t0 = time.time()
        self.ab("pump")
        self.assertLess(time.time() - t0, 3)
        d = self.db().execute("SELECT * FROM deliveries WHERE msg_id=?", (mid,)).fetchone()
        self.assertEqual((d["state"], d["attempts"], d["last_code"]), ("pending", 1, "net-error"))
        self.assertGreater(d["next_at"], time.time() + 3)
        self.ab("pump")
        self.assertEqual(self.db().execute("SELECT attempts FROM deliveries WHERE msg_id=?", (mid,)).fetchone()[0], 1)
        self.db().execute("UPDATE deliveries SET attempts=5, next_at=0 WHERE msg_id=?", (mid,)).connection.commit()
        self.ab("pump")
        self.assertEqual(self.state(mid), "gave_up")
        self.assertEqual(len(self.ab("export").stdout.split()), 1)             # the sweep still carries it

    def test_trickling_webhook_meets_the_absolute_deadline(self):
        Hook.mode = "trickle"
        self.hook(self.url)
        me = self.register("s1")["id"]
        mid = self.call("bridge_send", me, to="dross", message="ping")[1].split()[1]
        t0 = time.time()
        self.ab("pump", AB_HTTP_TIMEOUT=1)
        self.assertLess(time.time() - t0, 3)
        d = self.db().execute("SELECT state, last_code FROM deliveries WHERE msg_id=?", (mid,)).fetchone()
        self.assertEqual((d["state"], d["last_code"]), ("pending", "timeout"))

    def test_slow_webhook_attempt_is_cut_off(self):
        Hook.mode = "slow"
        self.hook(self.url)
        me = self.register("s1")["id"]
        mid = self.call("bridge_send", me, to="dross", message="ping")[1].split()[1]
        t0 = time.time()
        self.ab("pump", AB_HTTP_TIMEOUT=2)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(self.state(mid), "pending")
        code = subprocess.run([sys.executable, "-c", f"import os;os.environ['AB_HTTP_TIMEOUT']='999';"
                               f"import importlib.util as u;s=u.spec_from_file_location('ab','{CLI}');m=u.module_from_spec(s);"
                               f"s.loader.exec_module(m);print(m.HTTP_S, m.TMUX_S)"], capture_output=True, text=True)
        self.assertEqual(code.stdout.split(), ["15.0", "2.0"])                    # hard caps hold whatever env says

    # ---- the tmux last-resort nudge ---------------------------------------------------------------------------
    def test_nudge_leaves_copy_mode_targets_pane_and_types_only_a_pointer(self):
        (self.mac / "inject" / "legacy").touch()
        mid = self.ab("deliver", self.box_token("omp/legacy", "SECRET BODY")).stdout.split()[1]
        r = self.ab("_notify", mid, AB_DEBUG=1, FAKE_TMUX_MODE=1)
        self.assertEqual(r.stdout.strip(), "nudged")
        calls = [json.loads(l) for l in self.tlog.read_text().splitlines()]
        self.assertEqual(calls[0][:3], ["list-panes", "-t", "=legacy:"])
        self.assertEqual(calls[2], ["send-keys", "-t", "%9", "-X", "cancel"])
        self.assertEqual(calls[3][:4], ["send-keys", "-t", "%9", "-l"])
        self.assertNotIn("SECRET BODY", calls[3][4])
        self.assertEqual(calls[4], ["send-keys", "-t", "%9", "Enter"])
        self.assertEqual(self.ab("_notify", mid, AB_DEBUG=1).stdout.strip(), "nudged recently")

    def test_nudge_skips_extension_agents_and_sessions_without_opt_in(self):
        me = self.register("s1")["id"]
        (self.mac / "inject" / me[4:]).touch()
        mid = self.ab("deliver", self.box_token(me)).stdout.split()[1]
        self.assertEqual(self.ab("_notify", mid, AB_DEBUG=1).stdout.strip(), "extension wakes it")
        mid = self.ab("deliver", self.box_token("omp/plain")).stdout.split()[1]
        self.assertEqual(self.ab("_notify", mid, AB_DEBUG=1).stdout.strip(), "no inject opt-in")
        self.assertFalse(self.tlog.exists())

    def test_hung_tmux_is_cut_off(self):
        (self.mac / "inject" / "legacy").touch()
        mid = self.ab("deliver", self.box_token("omp/legacy")).stdout.split()[1]
        t0 = time.time()
        r = self.ab("_notify", mid, AB_DEBUG=1, FAKE_TMUX_HANG=1)
        self.assertLess(time.time() - t0, 3.5)
        self.assertEqual(r.stdout.strip(), "no tmux session legacy")
        t0 = time.time()
        self.register("s9", tmux="/tmp/sock", tmux_pane="%5", **{})               # registration survives a hung tmux too
        self.assertLess(time.time() - t0, 3.5)

    # ---- a locked store ---------------------------------------------------------------------------------------
    def test_locked_store_fails_fast(self):
        me = self.register("s1")["id"]
        tok = self.box_token(me)
        hold = sqlite3.connect(str(self.mac / "v2" / "bridge.db"), isolation_level=None)
        hold.execute("BEGIN EXCLUSIVE")
        try:
            t0 = time.time()
            r = self.ab("deliver", tok, check=False)
            self.assertLess(time.time() - t0, 2.5)
            self.assertEqual(r.returncode, 4)
            self.assertIn("store busy", r.stderr)
            t0 = time.time()
            self.assertEqual(self.ab("export").returncode, 0)                    # reads still work under a writer
            self.assertLess(time.time() - t0, 2.5)
        finally:
            hold.execute("ROLLBACK")
        self.assertIn("stored", self.ab("deliver", tok).stdout)                # retry after the lock lifts


    # ---- the stdio MCP server, as omp and the Executor gateway run it -----------------------------------------
    def rpc(self, *reqs, **env):
        lines = "".join(json.dumps({"jsonrpc": "2.0", "id": i + 1, **r}) + "\n" for i, r in enumerate(reqs))
        out = self.ab("mcp", input=lines, **env).stdout.splitlines()
        return [json.loads(l)["result"] for l in out]

    def test_tools_and_identity_rules(self):
        me = self.register("s1")["id"]
        init, tools, who, send = self.rpc(
            {"method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"method": "tools/list"},
            {"method": "tools/call", "params": {"name": "bridge_whoami", "arguments": {}}},
            {"method": "tools/call", "params": {"name": "bridge_send", "arguments": {"me": me, "to": "dross", "message": "x"}}})
        self.assertEqual({t["name"] for t in tools["tools"]},
                         {"bridge_send", "bridge_inbox", "bridge_read", "bridge_ack", "bridge_reply",
                          "bridge_list_agents", "bridge_whoami", "bridge_status"})
        self.assertTrue(who["isError"])                                          # no me, no AB_SELF, no omp ancestor
        self.assertFalse(send["isError"], send)
        (who,) = self.rpc({"method": "tools/call", "params": {"name": "bridge_whoami", "arguments": {}}}, AB_SELF=me)
        self.assertIn(me, who["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
