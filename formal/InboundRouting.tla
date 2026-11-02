---------------------------- MODULE InboundRouting ----------------------------
(***************************************************************************)
(* Inbound routing across sessions that share an alias: unread alias mail *)
(* bound to a session that died, exited or was pruned reaches a session   *)
(* that still carries the alias, and the per-session pointer budget lets  *)
(* that session be pointed at it; mail is never taken from a live owner,  *)
(* from its exact id, or once read or acked.                               *)
(*                                                                         *)
(* Every session carries the one alias (omp/work). Python is             *)
(* src/agent_bridge.py; each Python action is one `with tx(c)` block.     *)
(* The extension (extension/bridge-runtime.ts) is reduced to the calls it *)
(* makes: the heartbeat timer, and checkMail's `pending` call. Pointer     *)
(* delivery to the agent (poll, s.due, POINTER_GAP_MS, lost pointers) is  *)
(* InboundWake's model.                                                    *)
(* Coverage, bounds and assumptions: .omp/skills/wake-formal/SKILL.md.     *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Sessions,   \* omp session ids, each carrying the alias
          AliasMsgs,  \* messages whose to_addr is the alias
          ExactMsgs,  \* messages whose to_addr is the exact id ExactTo
          ExactTo,    \* the session the exact messages are addressed to
          Null,       \* messages.rcpt IS NULL
          MaxNotify,  \* MAX_NOTIFY
          MaxFaults   \* failed `pending` calls in all

Msgs == AliasMsgs \cup ExactMsgs

VARIABLES
  life,    \* the agents row as status_of sees it: "none" (never registered), "live", "stale" (dead pid or
           \* heartbeat_at older than STALE_AFTER), "exited" (state='exited'), "gone" (archived by op_prune)
  dstate,  \* deliveries.state: "none" (not stored yet), "queued", "notified", "read", "acked"
  rcpt,    \* messages.rcpt
  ncount,  \* notifications.notify_count for (msg_id, agent); 0 = no row
  old,     \* notifications.notified_at < now - RENOTIFY_AFTER for (msg_id, agent)
  hb,      \* the session's `agent-bridge heartbeat` call: "none", "due" (returned pending > 0), "clear" (pending = 0)
  pend,    \* the session's `agent-bridge pending` call from checkMail: "none", "running"
  faults   \* failed `pending` calls so far (bounded by MaxFaults)

vars == <<life, dstate, rcpt, ncount, old, hb, pend, faults>>

TypeOK ==
  /\ life \in [Sessions -> {"none", "live", "stale", "exited", "gone"}]
  /\ dstate \in [Msgs -> {"none", "queued", "notified", "read", "acked"}]
  /\ rcpt \in [Msgs -> Sessions \cup {Null}]
  /\ ncount \in [Msgs -> [Sessions -> 0..MaxNotify]]
  /\ old \in [Msgs -> [Sessions -> BOOLEAN]]
  /\ hb \in [Sessions -> {"none", "due", "clear"}]
  /\ pend \in [Sessions -> {"none", "running"}]
  /\ faults \in 0..MaxFaults

Init ==
  /\ life = [s \in Sessions |-> "none"]
  /\ dstate = [m \in Msgs |-> "none"]
  /\ rcpt = [m \in Msgs |-> Null]
  /\ ncount = [m \in Msgs |-> [s \in Sessions |-> 0]]
  /\ old = [m \in Msgs |-> [s \in Sessions |-> FALSE]]
  /\ hb = [s \in Sessions |-> "none"]
  /\ pend = [s \in Sessions |-> "none"]
  /\ faults = 0

Live(s) == life[s] = "live"
HasRow(s) == life[s] \in {"live", "stale", "exited"}
Unread(m) == dstate[m] \in {"queued", "notified"}

\* visible_sql(addrs_of(s)) against messages.rcpt = r: rcpt is s, or rcpt is NULL and to_addr is s or its alias
Visible(r, m, s) == \/ r[m] = s
                    \/ r[m] = Null /\ (m \in AliasMsgs \/ ExactTo = s)

\* pending_rows(s): the one filter op_pending claims with and op_heartbeat counts with
Due(r, m, s) == /\ Visible(r, m, s) /\ Unread(m)
                /\ \/ ncount[m][s] = 0
                   \/ old[m][s] /\ ncount[m][s] < MaxNotify

\* release_alias_mail: unread alias mail whose rcpt row is missing or not live (in agents rows l, after the
\* transaction's own writes) goes back to rcpt NULL. Exact-id, read and acked mail never moves.
Released(l) == [m \in Msgs |-> IF /\ m \in AliasMsgs /\ Unread(m) /\ rcpt[m] # Null
                                  /\ l[rcpt[m]] # "live"
                               THEN Null ELSE rcpt[m]]

\* resolve(to_addr) in insert: an existing agents row for the exact id; an alias with exactly one live carrier
Resolve(m) ==
  IF m \in ExactMsgs
    THEN IF HasRow(ExactTo) THEN ExactTo ELSE Null
  ELSE IF Cardinality({s \in Sessions : Live(s)}) = 1
    THEN CHOOSE s \in Sessions : Live(s)
  ELSE Null

(* ---- writers of messages and deliveries ---- *)

\* insert() inside op_send's or op_import's transaction
Store(m) ==
  /\ dstate[m] = "none"
  /\ dstate' = [dstate EXCEPT ![m] = "queued"]
  /\ rcpt' = [rcpt EXCEPT ![m] = Resolve(m)]
  /\ UNCHANGED <<life, ncount, old, hb, pend, faults>>

\* op_read (bridge_read by the recipient, or the operator's `read`): advance to 'read'
Read(m) ==
  /\ Unread(m)
  /\ dstate' = [dstate EXCEPT ![m] = "read"]
  /\ UNCHANGED <<life, rcpt, ncount, old, hb, pend, faults>>

\* op_ack by session s (bridge_ack, bridge_reply): mine(); rcpt NULL becomes s; advance to 'acked'
Ack(m, s) ==
  /\ Live(s) /\ Visible(rcpt, m, s) /\ dstate[m] \in {"queued", "notified", "read"}
  /\ dstate' = [dstate EXCEPT ![m] = "acked"]
  /\ rcpt' = IF rcpt[m] = Null THEN [rcpt EXCEPT ![m] = s] ELSE rcpt
  /\ UNCHANGED <<life, ncount, old, hb, pend, faults>>

(* ---- the agents registry ---- *)

\* op_register's transaction: INSERT OR REPLACE the row, then release_alias_mail(aliases)
Register(s) ==
  /\ life[s] = "none"
  /\ LET l == [life EXCEPT ![s] = "live"]
     IN life' = l /\ rcpt' = Released(l)
  /\ UNCHANGED <<dstate, ncount, old, hb, pend, faults>>

\* The session dies without op_exit (kill, crash, shutdown's `exit` timing out): status_of turns 'stale'
\* from the dead pid or an old heartbeat_at. No write. Permanent in this model.
Die(s) ==
  /\ Live(s)
  /\ life' = [life EXCEPT ![s] = "stale"]
  /\ UNCHANGED <<dstate, rcpt, ncount, old, hb, pend, faults>>

\* op_exit's transaction: state='exited', then release_alias_mail(its aliases)
Exit(s) ==
  /\ Live(s)
  /\ LET l == [life EXCEPT ![s] = "exited"]
     IN life' = l /\ rcpt' = Released(l)
  /\ UNCHANGED <<dstate, ncount, old, hb, pend, faults>>

\* op_prune's transaction (the pump, every minute): archive_agent for the exited or stale rows past their keep
\* time (any subset, empty included), then release_alias_mail(c) over every alias
Prune ==
  /\ \E P \in SUBSET {s \in Sessions : life[s] \in {"stale", "exited"}} :
       LET l == [s \in Sessions |-> IF s \in P THEN "gone" ELSE life[s]]
       IN life' = l /\ rcpt' = Released(l)
  /\ UNCHANGED <<dstate, ncount, old, hb, pend, faults>>

(* ---- one session's calls ---- *)

\* op_inbox's transaction (bridge_inbox): release_alias_mail(its aliases), then the visible rows
Inbox(s) ==
  /\ Live(s)
  /\ rcpt' = Released(life)
  /\ UNCHANGED <<life, dstate, ncount, old, hb, pend, faults>>

\* heartbeat(), every HEARTBEAT_MS -> op_heartbeat's transaction: heartbeat_at, release_alias_mail(its aliases),
\* then len(pending_rows)
HeartbeatTx(s) ==
  /\ Live(s) /\ hb[s] = "none"
  /\ LET r == Released(life)
     IN /\ rcpt' = r
        /\ hb' = [hb EXCEPT ![s] = IF \E m \in Msgs : Due(r, m, s) THEN "due" ELSE "clear"]
  /\ UNCHANGED <<life, dstate, ncount, old, pend, faults>>

\* heartbeat() after the await: got.pending > 0 calls checkMail(), which starts `pending` unless s.checking
HeartbeatReturn(s) ==
  /\ hb[s] # "none"
  /\ hb' = [hb EXCEPT ![s] = "none"]
  /\ pend' = IF hb[s] = "due" /\ Live(s) THEN [pend EXCEPT ![s] = "running"] ELSE pend
  /\ UNCHANGED <<life, dstate, rcpt, ncount, old, faults>>

\* checkMail() from poll() or agentEnd (a notify-file touch, s.due after POINTER_GAP_MS): starts `pending`
CheckMail(s) ==
  /\ Live(s) /\ pend[s] = "none"
  /\ pend' = [pend EXCEPT ![s] = "running"]
  /\ UNCHANGED <<life, dstate, rcpt, ncount, old, hb, faults>>

\* op_pending's transaction: release_alias_mail(its aliases); claim pending_rows: upsert notifications(m, s)
\* (notified_at, notify_count+1) and set deliveries.state 'notified'. The CLI may finish after its session died.
PendingTx(s) ==
  /\ pend[s] = "running"
  /\ LET r == Released(life)
         rows == {m \in Msgs : Due(r, m, s)}
     IN /\ rcpt' = r
        /\ dstate' = [m \in Msgs |-> IF m \in rows THEN "notified" ELSE dstate[m]]
        /\ ncount' = [m \in Msgs |-> [x \in Sessions |->
                        IF x = s /\ m \in rows THEN ncount[m][x] + 1 ELSE ncount[m][x]]]
        /\ old' = [m \in Msgs |-> [x \in Sessions |-> IF x = s /\ m \in rows THEN FALSE ELSE old[m][x]]]
  /\ pend' = [pend EXCEPT ![s] = "none"]
  /\ UNCHANGED <<life, hb, faults>>

\* The `pending` call fails before its transaction (store busy past BUSY_S, cli()'s 5 s timeout)
PendingFail(s) ==
  /\ pend[s] = "running" /\ faults < MaxFaults
  /\ pend' = [pend EXCEPT ![s] = "none"] /\ faults' = faults + 1
  /\ UNCHANGED <<life, dstate, rcpt, ncount, old, hb>>

\* Time passes RENOTIFY_AFTER after notifications.notified_at for (m, s)
Age(m, s) ==
  /\ ncount[m][s] > 0 /\ ~old[m][s]
  /\ old' = [old EXCEPT ![m][s] = TRUE]
  /\ UNCHANGED <<life, dstate, rcpt, ncount, hb, pend, faults>>

Next ==
  \/ \E m \in Msgs : Store(m) \/ Read(m)
  \/ \E m \in Msgs, s \in Sessions : Ack(m, s) \/ Age(m, s)
  \/ Prune
  \/ \E s \in Sessions :
       \/ Register(s) \/ Die(s) \/ Exit(s) \/ Inbox(s)
       \/ HeartbeatTx(s) \/ HeartbeatReturn(s) \/ CheckMail(s) \/ PendingTx(s) \/ PendingFail(s)

(* The heartbeat timer of a live session keeps firing and CLI calls finish. Nothing forces a session to start,
   die, exit or read, a message to be sent, the pump to prune, or time to pass RENOTIFY_AFTER. *)
Fairness ==
  \A s \in Sessions : WF_vars(HeartbeatTx(s)) /\ WF_vars(HeartbeatReturn(s)) /\ WF_vars(PendingTx(s))

Spec == Init /\ [][Next]_vars /\ Fairness

(* m no longer needs a pointer to s: read or acked, already claimed for s by op_pending, bound to another
   session that is live, or addressed to another session's exact id. *)
Served(m, s) ==
  \/ dstate[m] \in {"read", "acked"}
  \/ ncount[m][s] > 0
  \/ rcpt[m] \notin {Null, s} /\ Live(rcpt[m])
  \/ m \in ExactMsgs /\ ExactTo # s

(* The liveness property: a session that stays live is, in the end, pointed at every stored message it is the
   one left to answer, unless someone read it. A claim (ncount > 0) means pending_rows saw the message as
   visible and due for that session. *)
Routed == \A m \in Msgs, s \in Sessions :
            <>[]Live(s) => <>[](dstate[m] # "none" => Served(m, s))

(* The safety property: messages.rcpt changes only when insert sets it, when op_ack takes unresolved mail, or
   when unread alias mail leaves an owner that is not live after the transaction. *)
RcptMove(m) ==
  \/ dstate[m] = "none"
  \/ rcpt[m] = Null /\ dstate'[m] = "acked"
  \/ /\ rcpt'[m] = Null /\ m \in AliasMsgs /\ Unread(m)
     /\ life'[rcpt[m]] # "live"
NoSteal == [][\A m \in Msgs : rcpt'[m] # rcpt[m] => RcptMove(m)]_vars

=============================================================================
