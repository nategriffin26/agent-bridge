----------------------------- MODULE InboundWake -----------------------------
(***************************************************************************)
(* Inbound wake: every message stored for an omp session is pointed out    *)
(* to that session at least once, unless the session read it first.        *)
(*                                                                         *)
(* One recipient session with a fixed bridge id (rcpt = its id). Python is *)
(* src/agent_bridge.py; TypeScript is extension/bridge-runtime.ts and the *)
(* shim extension/agent-bridge.ts. Each action below names the function it *)
(* stands for; one action is one transaction (Python) or one synchronous   *)
(* run of the event loop up to an await (TypeScript).                      *)
(* Coverage, bounds and assumptions: .omp/skills/wake-formal/SKILL.md.     *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Msgs,       \* message ids
          MaxNotify,  \* MAX_NOTIFY
          MaxFaults   \* faults in all: lost `pending` results, failed `pending` calls, session rebinds

VARIABLES
  dstate,     \* deliveries.state: "none" (not stored yet), "queued", "notified", "read"
  ncount,     \* deliveries.notify_count
  old,        \* deliveries.notified_at < now - RENOTIFY_AFTER
  toTouch,    \* stored messages whose CLI has committed but not run touch_notify yet
  unseen,     \* the notify file v2/notify/<id> has an mtime above s.lastStamp (stamp > s.lastStamp in poll())
  lastZero,   \* s.lastStamp = 0
  registered, \* s.current is set
  due,        \* s.due
  checking,   \* s.checking
  gap,        \* Date.now() - s.lastPointer < POINTER_GAP_MS
  pend,       \* the `agent-bridge pending` call from checkMail: "none", "running", "returned"
  claimed,    \* ids that call's op_pending claimed (its "new")
  hb,         \* the `agent-bridge heartbeat` call: "none", "returned"
  hbDue,      \* the "pending" count op_heartbeat returned is > 0
  pointed,    \* history: ids named by a pointer that reached the session (pi.sendUserMessage)
  faults      \* faults so far (bounded by MaxFaults)

store  == <<dstate, ncount, old>>
ext    == <<lastZero, registered, due, checking, gap>>
calls  == <<pend, claimed, hb, hbDue>>
vars   == <<store, toTouch, unseen, ext, calls, pointed, faults>>

TypeOK ==
  /\ dstate \in [Msgs -> {"none", "queued", "notified", "read"}]
  /\ ncount \in [Msgs -> 0..MaxNotify]
  /\ old \in [Msgs -> BOOLEAN]
  /\ toTouch \subseteq Msgs
  /\ unseen \in BOOLEAN /\ lastZero \in BOOLEAN
  /\ registered \in BOOLEAN /\ due \in BOOLEAN /\ checking \in BOOLEAN /\ gap \in BOOLEAN
  /\ pend \in {"none", "running", "returned"} /\ claimed \subseteq Msgs
  /\ hb \in {"none", "returned"} /\ hbDue \in BOOLEAN
  /\ pointed \subseteq Msgs
  /\ checking = (pend # "none")

Init ==
  /\ dstate = [m \in Msgs |-> "none"]
  /\ ncount = [m \in Msgs |-> 0]
  /\ old = [m \in Msgs |-> FALSE]
  /\ toTouch = {}
  /\ unseen = FALSE /\ lastZero = TRUE
  /\ registered = TRUE /\ due = FALSE /\ checking = FALSE /\ gap = FALSE
  /\ pend = "none" /\ claimed = {} /\ hb = "none" /\ hbDue = FALSE
  /\ pointed = {}
  /\ faults = 0

(* The two copies of the "due for a pointer" filter in the Python. *)
\* op_pending: d.state='queued' OR (d.state='notified' AND d.notified_at<? AND d.notify_count<?)
PendingClaims(m) == \/ dstate[m] = "queued"
                    \/ dstate[m] = "notified" /\ old[m] /\ ncount[m] < MaxNotify
\* op_heartbeat: the same filter, counted
HeartbeatCounts(m) == \/ dstate[m] = "queued"
                      \/ dstate[m] = "notified" /\ old[m] /\ ncount[m] < MaxNotify

(* checkMail() in bridge-runtime.ts up to its await; d is s.due on entry. Sets due', checking', pend'. *)
CheckMail(d) ==
  IF ~registered \/ checking
    THEN due' = d /\ UNCHANGED <<checking, pend>>
  ELSE IF gap
    THEN due' = TRUE /\ UNCHANGED <<checking, pend>>
  ELSE checking' = TRUE /\ due' = FALSE /\ pend' = "running"

(* ---- the CLI that stores a message: op_import (deliver) or op_send ---- *)

\* insert() inside op_import's / op_send's transaction: a new deliveries row, state 'queued'
Store(m) ==
  /\ dstate[m] = "none"
  /\ dstate' = [dstate EXCEPT ![m] = "queued"]
  /\ toTouch' = toTouch \cup {m}
  /\ UNCHANGED <<ncount, old, unseen, ext, calls, pointed, faults>>

\* touch_notify(rcpt or to) after the commit: the file's mtime moves past anything poll() has stored
Touch(m) ==
  /\ m \in toTouch
  /\ toTouch' = toTouch \ {m}
  /\ unseen' = TRUE
  /\ UNCHANGED <<store, ext, calls, pointed, faults>>

\* The CLI dies between the commit and touch_notify. A redelivered duplicate returns before touch_notify too.
Crash(m) ==
  /\ m \in toTouch
  /\ toTouch' = toTouch \ {m}
  /\ UNCHANGED <<store, unseen, ext, calls, pointed, faults>>

(* ---- the extension ---- *)

\* poll(), every POLL_MS: stamp > s.lastStamp || s.lastStamp === 0 sets s.lastStamp and s.due; then checkMail()
\* if s.due. s.lastStamp becomes max(stamp, 1), so afterwards neither test holds until the next touch or register.
Poll ==
  /\ registered
  /\ LET fresh == unseen \/ lastZero
         d == fresh \/ due
     IN /\ unseen' = (unseen /\ ~fresh)
        /\ lastZero' = (lastZero /\ ~fresh)
        /\ IF d THEN CheckMail(d) ELSE UNCHANGED <<due, checking, pend>>
  /\ UNCHANGED <<store, toTouch, registered, gap, claimed, hb, hbDue, pointed, faults>>

\* op_pending's transaction: claim every row PendingClaims picks, set state 'notified', notified_at, notify_count+1
PendingTx ==
  /\ pend = "running"
  /\ LET rows == {m \in Msgs : PendingClaims(m)}
     IN /\ dstate' = [m \in Msgs |-> IF m \in rows THEN "notified" ELSE dstate[m]]
        /\ ncount' = [m \in Msgs |-> IF m \in rows THEN ncount[m] + 1 ELSE ncount[m]]
        /\ old' = [m \in Msgs |-> IF m \in rows THEN FALSE ELSE old[m]]
        /\ claimed' = rows
  /\ pend' = "returned"
  /\ UNCHANGED <<toTouch, unseen, ext, hb, hbDue, pointed, faults>>

\* The `pending` call fails before its transaction (store busy past AB_BUSY_MS, or cli()'s 5 s timeout):
\* got is undefined, checkMail returns, finally clears s.checking. s.due stays false.
PendingFail ==
  /\ pend = "running" /\ faults < MaxFaults
  /\ pend' = "none" /\ checking' = FALSE /\ faults' = faults + 1
  /\ UNCHANGED <<store, toTouch, unseen, lastZero, registered, due, gap, claimed, hb, hbDue, pointed>>

\* checkMail after the await: a non-empty "new" makes a pointer; pi.sendUserMessage; s.lastPointer = now
PendingReturn ==
  /\ pend = "returned"
  /\ IF claimed # {}
       THEN pointed' = pointed \cup claimed /\ gap' = TRUE
       ELSE UNCHANGED <<pointed, gap>>
  /\ pend' = "none" /\ checking' = FALSE /\ claimed' = {}
  /\ UNCHANGED <<store, toTouch, unseen, lastZero, registered, due, hb, hbDue, faults>>

\* The transaction committed but the pointer never reaches the session: cli() killed by its 5 s timeout after
\* the commit, or the session rebinding mid-call. The rows stay 'notified'.
PendingLost ==
  /\ pend = "returned" /\ claimed # {} /\ faults < MaxFaults
  /\ pend' = "none" /\ checking' = FALSE /\ claimed' = {} /\ faults' = faults + 1
  /\ UNCHANGED <<store, toTouch, unseen, lastZero, registered, due, gap, hb, hbDue, pointed>>

\* heartbeat(), every HEARTBEAT_MS: op_heartbeat counts the rows HeartbeatCounts picks
HeartbeatTx ==
  /\ registered /\ hb = "none"
  /\ hb' = "returned"
  /\ hbDue' = (\E m \in Msgs : HeartbeatCounts(m))
  /\ UNCHANGED <<store, toTouch, unseen, ext, pend, claimed, pointed, faults>>

\* heartbeat() after the await: got.pending > 0 calls checkMail()
HeartbeatReturn ==
  /\ hb = "returned"
  /\ hb' = "none"
  /\ IF hbDue THEN CheckMail(due) ELSE UNCHANGED <<due, checking, pend>>
  /\ UNCHANGED <<store, toTouch, unseen, lastZero, registered, gap, claimed, hbDue, pointed, faults>>

\* Time passes POINTER_GAP_MS after the last pointer
GapEnds ==
  /\ gap /\ gap' = FALSE
  /\ UNCHANGED <<store, toTouch, unseen, lastZero, registered, due, checking, calls, pointed, faults>>

\* Time passes RENOTIFY_AFTER after a row's notified_at
Age(m) ==
  /\ dstate[m] = "notified" /\ ~old[m]
  /\ old' = [old EXCEPT ![m] = TRUE]
  /\ UNCHANGED <<dstate, ncount, toTouch, unseen, ext, calls, pointed, faults>>

\* bridge_read: op_read advances the row to 'read'
Read(m) ==
  /\ dstate[m] \in {"queued", "notified"}
  /\ dstate' = [dstate EXCEPT ![m] = "read"]
  /\ UNCHANGED <<ncount, old, toTouch, unseen, ext, calls, pointed, faults>>

\* session_shutdown: shutdown() runs `exit` and clears s.current. s itself (due, checking, lastPointer) survives.
Unbind ==
  /\ registered /\ faults < MaxFaults
  /\ registered' = FALSE /\ faults' = faults + 1
  /\ UNCHANGED <<store, toTouch, unseen, lastZero, due, checking, gap, calls, pointed>>

\* session_start: start() -> register() sets s.current to the same id and s.lastStamp = 0
Register ==
  /\ ~registered
  /\ registered' = TRUE /\ lastZero' = TRUE
  /\ UNCHANGED <<store, toTouch, unseen, due, checking, gap, calls, pointed, faults>>

Next ==
  \/ \E m \in Msgs : Store(m) \/ Touch(m) \/ Crash(m) \/ Age(m) \/ Read(m)
  \/ Poll \/ PendingTx \/ PendingFail \/ PendingReturn \/ PendingLost
  \/ HeartbeatTx \/ HeartbeatReturn \/ GapEnds \/ Unbind \/ Register

(* Timers keep firing, CLI calls finish, time passes, a stored message's CLI runs to its end.
   Nothing forces a message to be sent, a fault to happen, or the agent to read. *)
Fairness ==
  /\ WF_vars(Poll) /\ WF_vars(HeartbeatTx) /\ WF_vars(HeartbeatReturn)
  /\ WF_vars(PendingTx) /\ WF_vars(PendingReturn) /\ WF_vars(GapEnds) /\ WF_vars(Register)
  /\ \A m \in Msgs : WF_vars(Touch(m)) /\ WF_vars(Age(m))

Spec == Init /\ [][Next]_vars /\ Fairness

(* The property: a stored message is eventually pointed out to its session, or read first. *)
Woken == \A m \in Msgs : (dstate[m] # "none") ~> (m \in pointed \/ dstate[m] = "read")

=============================================================================
