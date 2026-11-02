---
name: wake-formal
description: Model check that every message stored for an omp session wakes it (formal/InboundWake.tla, TLA+/TLC). Use before committing any change to the inbound wake path - op_import, op_send, insert, touch_notify, op_pending, op_heartbeat, op_read in src/agent_bridge.py, or poll/checkMail/heartbeat/register/shutdown in extension/bridge-runtime.ts and the timers in extension/agent-bridge.ts - or to check the model still matches that code.
---

# Inbound wake, model checked

**Property.** Every message stored for an omp session is pointed out to that session at least once (a pointer naming it reaches the session through `pi.sendUserMessage`), unless the session read it first.

**Harm it guards.** A box bot's instruction or a coworker's request sits in the store while the session it was sent to never hears about it, and nobody notices.

## Run it

```sh
formal/check.sh
```

About 30 s (1.0M distinct states on 18 cores). The first run downloads TLA+ tools 1.7.4 into `local/tla/` (gitignored, sha256 pinned in the script); it needs Java 11+ and curl. A rerun is a local process and costs no model tokens.

- Pass: `Model checking completed. No error has been found.`, exit 0.
- Property broken: `Error: Temporal properties were violated.`, then `The following behavior constitutes a counter-example:` and numbered states ending in `Back to state N` (a loop the session never leaves). Exit non-zero.
- `Invariant TypeOK is violated`, a parse error, or a deadlock report is a broken model, not a bug in the code.
- A JVM crash (`SIGBUS`, `hs_err_pid*.log`) is the JVM; delete the log and rerun.

## What the model covers (sync commit 0212541)

The model describes the files below as they were at commit `0212541`. One recipient session with a fixed bridge id; the message's `rcpt` is that id.

| Model | Code |
|---|---|
| `dstate`, `ncount`, `old` | `deliveries.state`, `notify_count`, and `notified_at < now - RENOTIFY_AFTER` |
| `Store(m)` | `insert` inside the transaction of `op_import` (`deliver`, `import-token(s)`) or `op_send` |
| `Touch(m)` | `touch_notify(rcpt or to)` after that commit, in `op_import` and `op_send` |
| `Crash(m)` | the CLI dying between that commit and `touch_notify`; also a redelivered duplicate, which returns before `touch_notify` |
| `PendingClaims` | the row filter in `op_pending` |
| `PendingTx` | `op_pending`'s transaction: claims rows, sets `notified`, `notified_at`, `notify_count+1` |
| `HeartbeatCounts`, `HeartbeatTx` | the count query in `op_heartbeat` |
| `CheckMail(d)` | `checkMail` in `extension/bridge-runtime.ts` up to its `await` (guards `s.current`, `s.checking`, `POINTER_GAP_MS`) |
| `PendingReturn` | `checkMail` after the `await`: pointer, `s.lastPointer`, `finally` |
| `PendingFail` | `cli(["pending"])` failing before the transaction (exit 4 busy store, 5 s timeout) |
| `PendingLost` | the transaction committed but the pointer never arrives (timeout after commit, rebind mid-call) |
| `HeartbeatReturn` | `heartbeat` after the `await`: `got.pending > 0` calls `checkMail` |
| `Poll`, `unseen`, `lastZero` | `poll`: notify file mtime against `s.lastStamp`, `s.due` |
| `gap`, `GapEnds` | `Date.now() - s.lastPointer < POINTER_GAP_MS` |
| `Age(m)` | `RENOTIFY_AFTER` passing |
| `Read(m)` | `op_read` (`bridge_read`) advancing to `read` |
| `Unbind`, `Register` | `shutdown` clearing `s.current`; `start` -> `register` setting `s.current` and `s.lastStamp = 0` (the shared `s` in `extension/agent-bridge.ts` survives) |
| `Fairness` | `setInterval` timers in `extension/agent-bridge.ts` (`HEARTBEAT_MS`, `POLL_MS`), CLI calls finishing, time passing |

A change touches the verified code when it edits any function or constant in that table (`MAX_NOTIFY`, `RENOTIFY_AFTER`, `POINTER_GAP_MS`, `POLL_MS`, `HEARTBEAT_MS`, the `cli` timeout), changes which rows `op_pending` or `op_heartbeat` select, adds a new writer of `deliveries.state` for `direction='in'`, or adds or removes a path that wakes the session.

## Bounds and assumptions

- Two messages (`Msgs = {m1, m2}`), `MaxNotify = 3` as in `MAX_NOTIFY`, at most `MaxFaults = 2` faults in all (lost pointers, failed `pending` calls, rebinds).
- Fewer than `MAX_NOTIFY` pointers to one message are lost. With `MAX_NOTIFY` losses the design stops re-pointing on purpose, so `MaxFaults` must stay below `MaxNotify`.
- The heartbeat and poll timers keep firing, CLI calls finish, and every notify-file touch moves its mtime forward.
- A pointer handed to `pi.sendUserMessage` reaches the session; whether the agent then acts on it is outside the model.

## Not covered

Several sessions sharing an alias, unresolved mail (`rcpt` NULL), messages bound to a session that later exits for good, pruning, the tmux nudge, outbound mail (the webhook pump, `export`, `mark`), the box side, signing, and the 20 s / 900 s timings themselves (only their order). A pass covers the model under these bounds, not the system.

## A counterexample: real bug or modeling error

Replay each step of the trace against the code. Python steps replay with the real CLI under a throwaway home; never point `AB_HOME` at the live store.

```sh
export AB_HOME="$(mktemp -d)" AB_NO_SPAWN=1; unset AB_ME AB_SELF
printf '{"side":"mac","owner":"Ada"}' > "$AB_HOME/config.json"
(umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > "$AB_HOME/secret")
ab() { python3 src/agent_bridge.py "$@"; }
ab init --side mac >/dev/null
ME=$(ab register --json "{\"session_id\":\"s1\",\"cwd\":\"/tmp/wake\",\"pid\":$$}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ab send omp/peer "$ME" hello                     # Store + Touch
ab pending "$ME"                                 # PendingTx (ignore the output for PendingLost)
python3 -c "import sqlite3,os; c=sqlite3.connect(os.environ['AB_HOME']+'/v2/bridge.db'); c.execute('UPDATE deliveries SET notified_at=notified_at-1000'); c.commit()"   # Age
ab heartbeat "$ME"                               # HeartbeatTx: {"pending": N, ...}
ab call bridge_read --me "$ME" --args '{"id":"<id>"}'   # Read
ab exit "$ME" quit                               # Unbind (then register again: Register)
```

TypeScript steps (`Poll`, `CheckMail`, `PendingReturn`, `HeartbeatReturn`) replay by reading `extension/bridge-runtime.ts` line by line against the trace's variables; `python3 tests/e2e.py` runs a real omp session end to end when a trace needs the live extension.

If every step can happen in the code, the code breaks the property: report the trace as steps in the code's words, with the replay as evidence, and leave product code alone unless the change you are making is the fix. If a step cannot happen in the code, the model is wrong: fix the model from the code and say what was wrong.

## When the code changes on purpose

1. Edit the model from the new code, not from the result: one action per transaction or per synchronous run up to an `await`; keep the names in the table above.
2. Keep the bounds. A smaller bound goes in your report.
3. Run `formal/check.sh`. If it passes, break the new code the way the property guards against (for example drop the renotify clause from `op_heartbeat`'s count), mirror it in the model, confirm the check fails, and drop both.
4. Update the table, the bounds and the sync commit above. Move the sync commit only after confirming the model describes the code at that commit.
