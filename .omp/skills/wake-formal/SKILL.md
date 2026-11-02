---
name: wake-formal
description: Model checks that every message stored for an omp session wakes it (formal/InboundWake.tla) and that shared-alias mail left on a dead, exited or pruned session reaches a live carrier of the alias (formal/InboundRouting.tla), both TLA+/TLC. Use before committing any change to inbound delivery - insert, resolve, status_of, visible_sql, pending_rows, release_alias_mail, op_import, op_send, touch_notify, op_inbox, op_pending, op_heartbeat, op_read, op_ack, op_register, op_exit, op_prune or the notifications table in src/agent_bridge.py, or poll/checkMail/heartbeat/register/shutdown in extension/bridge-runtime.ts and the timers in extension/agent-bridge.ts - or to check the models still match that code.
---

# Inbound wake and routing, model checked

Two models:

- **InboundWake.** Every message stored for an omp session is pointed out to that session at least once (a pointer naming it reaches the session through `pi.sendUserMessage`), unless the session read it first. Harm: a box bot's instruction or a coworker's request sits in the store while the session it was sent to never hears about it.
- **InboundRouting.** A surviving session eventually claims a pointer for unread alias mail left by a dead owner, regardless of another carrier's notification count. Exact-ID mail stays with its addressee; live owners keep bound mail; read and acked mail is not released. Harm: a shared-alias message remains invisible after its owner crashes, or another session takes mail it does not own.

## Run it

```sh
formal/check.sh                  # both models
formal/check.sh InboundRouting   # one model
```

Last full run: InboundWake 35 s / 1,016,155 distinct states; InboundRouting 9 min 48 s / 8,774,437 distinct states (18 workers, concurrent mutation checks). The first run downloads TLA+ tools 1.7.4 into `local/tla/` (gitignored, sha256 pinned in the script); it needs Java 11+ and curl.

- Pass: each model prints `Model checking completed. No error has been found.`; the script ends `formal/check.sh: all models pass: ...` and exits 0.
- Property broken: `Error: Temporal properties were violated.` (a liveness property: `Woken`, `Routed`) or `Error: Action property NoSteal is violated.`, followed by numbered states; a liveness trace ends in `Back to state N`. The script ends `formal/check.sh: failed: <model>` and exits 1.
- `Invariant TypeOK is violated`, a parse error, or a deadlock report is a broken model, not a bug in the code.
- A JVM crash (`SIGBUS`, `hs_err_pid*.log`) is the JVM; delete the log and rerun.

## What the models cover (sync commit 61b8f40)

Both models describe the files below as they were at commit `61b8f40`.

### InboundWake

One recipient session with a fixed bridge id; the message's `rcpt` is that id.

| Model | Code |
|---|---|
| `dstate` | `deliveries.state` |
| `ncount`, `old` | `notifications.notify_count` and `notifications.notified_at < now - RENOTIFY_AFTER` for (message, this session); `ncount = 0` is no row |
| `Store(m)` | `insert` inside the transaction of `op_import` (`deliver`, `import-token(s)`) or `op_send` |
| `Touch(m)` | `touch_notify(rcpt or to)` after that commit, in `op_import` and `op_send` |
| `Crash(m)` | the CLI dying between that commit and `touch_notify`; also a redelivered duplicate, which returns before `touch_notify` |
| `PendingClaims`, `HeartbeatCounts` | `pending_rows`, the one filter `op_pending` claims with and `op_heartbeat` counts with |
| `PendingTx` | `op_pending`'s transaction: `release_alias_mail` (no effect on this session's own mail), claims rows, upserts `notifications`, sets `deliveries.state='notified'` |
| `HeartbeatTx` | `op_heartbeat`'s transaction: `release_alias_mail`, then `len(pending_rows)` |
| `CheckMail(d)` | `checkMail` in `extension/bridge-runtime.ts` up to its `await` (guards `s.current`, `s.checking`, `POINTER_GAP_MS`) |
| `PendingReturn` | `checkMail` after the `await`: pointer, `s.lastPointer`, `finally` |
| `PendingFail` | `cli(["pending"])` failing before the transaction (exit 4 busy store, 5 s timeout) |
| `PendingLost` | the transaction committed but the pointer never arrives (timeout after commit, rebind mid-call) |
| `HeartbeatReturn` | `heartbeat` after the `await`: `got.pending > 0` calls `checkMail` |
| `Poll`, `unseen`, `lastZero` | `poll`: notify file mtime against `s.lastStamp`, `s.due` |
| `gap`, `GapEnds` | `Date.now() - s.lastPointer < POINTER_GAP_MS` |
| `Age(m)` | `RENOTIFY_AFTER` passing after this session's `notifications.notified_at` |
| `Read(m)` | `op_read` (`bridge_read`) advancing to `read` |
| `Unbind`, `Register` | `shutdown` clearing `s.current`; `start` -> `register` setting `s.current` and `s.lastStamp = 0` (the shared `s` in `extension/agent-bridge.ts` survives) |
| `Fairness` | `setInterval` timers in `extension/agent-bridge.ts` (`HEARTBEAT_MS`, `POLL_MS`), CLI calls finishing, time passing |

### InboundRouting

Every session carries one alias (`omp/work` in the replay below). The extension is reduced to the calls it makes; whether a claimed pointer then reaches the agent is InboundWake's property.

| Model | Code |
|---|---|
| `life` | the agents row as `status_of` sees it: `none` (no row yet), `live`, `stale` (dead pid on this host, or `heartbeat_at` older than `STALE_AFTER`), `exited` (`state='exited'`), `gone` (archived by `op_prune`) |
| `dstate`, `rcpt` | `deliveries.state`, `messages.rcpt` (`Null` is SQL NULL) |
| `ncount[m][s]`, `old[m][s]` | `notifications.notify_count` and `notified_at < now - RENOTIFY_AFTER` for (`msg_id`, `agent`); `0` is no row |
| `Visible(r, m, s)` | `visible_sql(addrs_of(s))` |
| `Due(r, m, s)` | `pending_rows(c, s, ids, t)` |
| `Released(l)` | `release_alias_mail`: unread alias mail (`rcpt != to_addr`) whose `rcpt` row is missing or not live goes to `rcpt=NULL`; `l` is the agents rows after the transaction's own writes |
| `Resolve(m)` | `resolve` in `insert`: the exact id when its row exists; the alias when exactly one live session carries it |
| `Store(m)` | `insert` inside `op_send` / `op_import` |
| `Read(m)` | `op_read` (`bridge_read`, or the operator's `read`) |
| `Ack(m, s)` | `op_ack` by session `s` (`bridge_ack`, `bridge_reply`): `rcpt` NULL becomes `s`, state `acked` |
| `Register(s)` | `op_register`'s transaction: the row, then `release_alias_mail(aliases)` |
| `Die(s)` | a kill, a crash, or `shutdown`'s 1.5 s `cli exit` failing: no write; `status_of` turns `stale` |
| `Exit(s)` | `op_exit`'s transaction: `state='exited'`, then `release_alias_mail(aliases)` |
| `Prune` | `op_prune`'s transaction (`_pump` from launchd every minute, or `prune`): `archive_agent` for any subset of exited/stale rows past their keep time, then `release_alias_mail(c)` over every alias |
| `Inbox(s)` | `op_inbox`'s transaction (`bridge_inbox`): `release_alias_mail(aliases)`, then the select |
| `HeartbeatTx(s)` | `op_heartbeat`'s transaction: `heartbeat_at`, `release_alias_mail(aliases)`, `len(pending_rows)`; `hb = "due"` is `pending > 0` |
| `HeartbeatReturn(s)` | `heartbeat` after the `await`: `got.pending > 0` calls `checkMail`, which starts `pending` unless `s.checking` |
| `CheckMail(s)` | `checkMail` started from `poll` or `agentEnd` (a notify-file touch, `s.due` after the gap) |
| `PendingTx(s)` | `op_pending`'s transaction: `release_alias_mail(aliases)`, claim `pending_rows`, upsert `notifications`, `deliveries.state='notified'`; the CLI may commit after its session died |
| `PendingFail(s)` | `cli(["pending"])` failing before the transaction |
| `Age(m, s)` | `RENOTIFY_AFTER` passing after `notifications.notified_at` for (`m`, `s`) |
| `Fairness` | the `HEARTBEAT_MS` timer of a live session, CLI calls finishing |

A change touches the verified code when it edits any function or constant in these tables (`MAX_NOTIFY`, `RENOTIFY_AFTER`, `STALE_AFTER`, `EXITED_KEEP`, `STALE_KEEP`, `POINTER_GAP_MS`, `POLL_MS`, `HEARTBEAT_MS`, the `cli` timeout), changes which rows `pending_rows`, `visible_sql` or `release_alias_mail` select, moves a `release_alias_mail` call into or out of a transaction, adds a writer of `messages.rcpt`, `notifications` or `deliveries.state` for `direction='in'`, or adds or removes a path that wakes a session.

## Bounds and assumptions

InboundWake:
- Two messages (`Msgs = {m1, m2}`), `MaxNotify = 3` as in `MAX_NOTIFY`, at most `MaxFaults = 2` faults in all (lost pointers, failed `pending` calls, rebinds).
- Fewer than `MAX_NOTIFY` pointers to one message are lost. With `MAX_NOTIFY` losses the design stops re-pointing on purpose, so `MaxFaults` must stay below `MaxNotify`.
- The heartbeat and poll timers keep firing, CLI calls finish, and every notify-file touch moves its mtime forward.
- A pointer handed to `pi.sendUserMessage` reaches the session; whether the agent then acts on it is outside the model.

InboundRouting:
- Two sessions `{a, b}`, both carrying the alias; one alias message `m1`; one message `m2` to `a`'s exact id; `MaxNotify = 3`; at most `MaxFaults = 1` failed `pending` call.
- A session registers once and its death, exit or pruning is permanent. A hung session that heartbeats again after going stale, and a pruned session re-registering under the same id, are not modeled.
- Fairness: a live session's heartbeat timer keeps firing and its CLI calls finish. Nothing forces a session to start, die, exit or read, a message to be sent, the pump to prune, or `RENOTIFY_AFTER` to pass. The launchd pump's release is therefore a second path the liveness result does not rely on: `Routed` holds through the surviving session's own heartbeat.
- `Routed` asks for the first claim for each session (`ncount > 0`). Re-pointing after a lost pointer, within that session's own `MAX_NOTIFY` budget, is InboundWake's property.

## Not covered

The tmux nudge, outbound mail (the webhook pump's sends, `export`, `mark`), the box side, signing, the `events`-to-`notifications` migration in `connect`, three or more sessions sharing an alias, several aliases per session, and the timings themselves (only their order). A pass covers the models under these bounds, not the system.

## A counterexample: real bug or modeling error

Replay each step of the trace against the code. Python steps replay with the real CLI under a throwaway home; never point `AB_HOME` at the live store.

```sh
export AB_HOME="$(mktemp -d)" AB_NO_SPAWN=1; unset AB_ME AB_SELF
printf '{"side":"mac","owner":"Ada"}' > "$AB_HOME/config.json"
(umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > "$AB_HOME/secret")
ab() { python3 src/agent_bridge.py "$@"; }
sql() { python3 -c "import sqlite3,os,sys; c=sqlite3.connect(os.environ['AB_HOME']+'/v2/bridge.db'); r=c.execute(sys.argv[1]).fetchall(); c.commit(); print(*r, sep='\n')" "$1"; }
reg() { ab register --json "{\"session_id\":\"$1\",\"cwd\":\"/tmp/work\",\"pid\":$2}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }
ab init --side mac >/dev/null
```

InboundWake steps (one session):

```sh
ME=$(reg s1 $$)
ab send omp/peer "$ME" hello                     # Store + Touch
ab pending "$ME"                                 # PendingTx (ignore the output for PendingLost)
sql "UPDATE notifications SET notified_at=notified_at-1000"   # Age
ab heartbeat "$ME"                               # HeartbeatTx: {"pending": N, ...}
ab call bridge_read --me "$ME" --args '{"id":"<id>"}'   # Read
ab exit "$ME" quit                               # Unbind (then register again: Register)
```

InboundRouting steps (sessions `a` and `b` share alias `omp/work`):

```sh
sleep 600 & PA=$!
A=$(reg sa $PA)                                  # Register(a)
ab send omp/peer omp/work hello                  # Store(m1): rcpt = A, the only live carrier
ab send omp/peer "$A" exact                      # Store(m2)
B=$(reg sb $$)                                   # Register(b): A is live, m1 stays with A
kill $PA                                         # Die(a) by dead pid
# For a hang instead of a kill: sql "UPDATE agents SET heartbeat_at=heartbeat_at-200 WHERE id='$A'"
# For graceful exit instead of a kill: ab exit "$A" quit
# To include pruning after the kill: ab prune --days 0
ab heartbeat "$B"                                # HeartbeatTx(b): {"pending": N}
ab pending "$B"                                  # PendingTx(b): "new" lists the claimed ids
ab inbox "$B"                                    # Inbox(b)
ab call bridge_ack --me "$B" --args '{"id":"<id>"}'   # Ack(m, b)
sql "UPDATE notifications SET notified_at=notified_at-1000 WHERE agent='$A'"   # Age(m, a)
sql "SELECT id, to_addr, rcpt FROM messages"; sql "SELECT * FROM notifications"   # read rcpt and ncount
```

TypeScript steps (`Poll`, `CheckMail`, `PendingReturn`, `HeartbeatReturn`) replay by reading `extension/bridge-runtime.ts` line by line against the trace's variables; `python3 tests/e2e.py` runs a real omp session end to end when a trace needs the live extension.

If every step can happen in the code, the code breaks the property: report the trace as steps in the code's words, with the replay as evidence, and leave product code alone unless the change you are making is the fix. If a step cannot happen in the code, the model is wrong: fix the model from the code and say what was wrong.

## Show it can fail

Each break below goes into a scratch copy, never into `formal/` or the checkout: the model in a temp directory, the code in a detached worktree. Model break:

```sh
d=$(mktemp -d); cp formal/InboundRouting.tla formal/InboundRouting.cfg "$d"/
# edit $d/InboundRouting.tla as below, then:
java -XX:+UseParallelGC -cp local/tla/tla2tools-1.7.4.jar tlc2.TLC -workers auto -lncheck final -cleanup \
  -metadir "$d/states" -config "$d/InboundRouting.cfg" "$d/InboundRouting.tla"
```

Code break: `w=$(mktemp -d)/mut; git worktree add --detach "$w" HEAD`, edit `$w/src/agent_bridge.py`, run `python3 tests/test_bridge.py -k <test>` from `$w`, then `git worktree remove --force "$w"`.

1. Release only at register and exit (the code before the fix). Model: in `Inbox` replace `rcpt' = Released(life)` with `rcpt' = rcpt`; in `HeartbeatTx` and `PendingTx` replace `LET r == Released(life)` with `LET r == rcpt`; in `Prune` replace `rcpt' = Released(l)` with `rcpt' = rcpt`. Expect `Temporal properties were violated`: `Register(a)`, `Store(m1)` with `rcpt = a`, `Register(b)`, `Die(a)`, then `b`'s heartbeat loops with `hb = "clear"`. Code: delete the `release_alias_mail` calls in `op_inbox`, `op_pending`, `op_heartbeat` and `op_prune`; `test_existing_alias_carrier_recovers_crashed_stale_and_pruned_mail` fails.
2. First pointer incorrectly gated by global state. Model: in `Due` replace `ncount[m][s] = 0` with `dstate[m] = "queued"`. Expect `Temporal properties were violated`: both sessions register, `Store(m1)` with `rcpt = Null`, `a` claims `m1`, and `b` is never due. Code: in `pending_rows` replace `n.msg_id IS NULL` with `d.state='queued'`; `test_notification_budget_belongs_to_each_alias_carrier` fails.
3. Release from a live owner. Model: in `Released` delete the line `/\ l[rcpt[m]] # "live"`. Expect `Action property NoSteal is violated`, for example at `Prune` while `a` is live. Code: in `release_alias_mail` replace `if not r or status_of(r) != "live":` with `if True:`; `test_existing_alias_carrier_recovers_crashed_stale_and_pruned_mail` fails at its first `assertNotIn`.

Then run `formal/check.sh` on the unchanged tree and confirm both models pass.

At sync commit `61b8f40`, both models passed and TLC rejected all three model breaks above. Before the code fix, the CLI repros failed in all 11 crash/stale/prune and notification-budget cases. An isolated code mutation removing the exact-ID release guards also failed all three prune cases in the ownership regression. The fixed code passed all 31 CLI tests and the real omp end-to-end run.

## When the code changes on purpose

1. Edit the models from the new code, not from the result: one action per transaction or per synchronous run up to an `await`; keep the names in the tables above.
2. Keep the bounds. A smaller bound goes in your report.
3. Run `formal/check.sh`. If it passes, break the new code the way each property guards against (the breaks above, or for InboundWake drop the renotify clause from `pending_rows`), mirror it in the model, confirm the check fails, and drop both.
4. Update the tables, the bounds and the sync commit above. Move the sync commit only after confirming the models describe the code at that commit.
