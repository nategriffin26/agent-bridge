// agent-bridge omp runtime: everything the extension does, hot-swapped into running sessions.
// The stable shim (agent-bridge.ts) re-imports this file whenever it changes and passes the same state object `s`,
// so an edit here reaches every running omp within one poll (3 s) with no /reload. Store logic and wording live in
// the CLI (~/bin/agent-bridge); every call is bounded by a timeout.
// @ts-nocheck
import { statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const RUNTIME_VERSION = "2.2.1";
const CLI = process.env.AB_CLI || join(homedir(), "bin", "agent-bridge");
const HOME = process.env.AB_HOME || join(homedir(), ".agent-bridge");
const POINTER_GAP_MS = 20_000; // at most one pointer per 20 s; mail arriving inside the gap waits for it

export function createRuntime(pi, s) {
  const version = s.messageEndHook ? RUNTIME_VERSION : `${RUNTIME_VERSION}-legacy-shim`;
  s.pendingInline ??= [];
  s.deliveryGeneration ??= 0;

  async function cli(args, timeout = 5_000) {
    try {
      const r = await pi.exec(CLI, args, { timeout });
      return { ok: r.code === 0 && !r.killed, out: (r.stdout || r.stderr || "").trim() };
    } catch (e) {
      return { ok: false, out: String(e?.message ?? e) };
    }
  }

  const json = (out) => {
    try {
      return JSON.parse(out);
    } catch {
      return undefined;
    }
  };

  function model(ctx) {
    const m = ctx?.models?.current?.() ?? ctx?.model;
    return { model: m?.id, provider: m?.provider };
  }

  async function register(ctx) {
    const sessionId = ctx?.sessionManager?.getSessionId?.();
    if (!sessionId) return;
    const meta = {
      session_id: sessionId,
      session_file: ctx.sessionManager.getSessionFile?.(),
      cwd: ctx.cwd,
      pid: process.pid,
      state: s.state,
      ext_version: version,
      ab_self: process.env.AB_SELF || "",
      tmux: process.env.TMUX || "",
      tmux_pane: process.env.TMUX_PANE || "",
      ...model(ctx),
    };
    const r = await cli(["register", "--json", JSON.stringify(meta)]);
    const got = r.ok ? json(r.out) : undefined;
    if (!got?.id) return;
    if (s.current && s.current.id !== got.id) await cli(["exit", s.current.id, "switch"], 2_000);
    s.current = { id: got.id, aliases: got.aliases ?? [], sessionId };
    s.lastStamp = 0; // look at the inbox once right away: mail may have arrived while no session watched
  }

  function ensureRegistered(ctx) {
    s.registering ??= register(ctx ?? s.ctx).finally(() => {
      s.registering = undefined;
    });
    return s.registering;
  }

  // The bridge's handling rules come from `agent-bridge policy`, not from a prompt baked in at launch: added to
  // context at start and again whenever their version changes. nextTurn never starts a turn on its own.
  async function syncPolicy(version) {
    if (version && version === s.policyVersion) return;
    const got = json((await cli(["policy"])).out);
    if (!got?.version || got.version === s.policyVersion) return;
    s.policyVersion = got.version;
    pi.sendMessage(
      { customType: "agent-bridge-policy", content: `[agent-bridge policy ${got.version}]\n${got.text}`, display: false },
      { deliverAs: "nextTurn" },
    );
  }

  async function heartbeat() {
    if (!s.current) return;
    const m = model(s.ctx);
    const r = await cli([
      "heartbeat", s.current.id, s.state,
      ...(m.model ? ["--model", m.model] : []),
      ...(m.provider ? ["--provider", m.provider] : []),
      "--ext", version,
    ]);
    const got = r.ok ? json(r.out) : undefined;
    if (got?.reregister) await ensureRegistered(s.ctx);
    else if (got?.pending > 0) void checkMail();
    if (got?.policy) void syncPolicy(got.policy);
  }

  function setState(next) {
    if (next === s.state) return;
    s.state = next;
    void heartbeat();
  }

  function poll() {
    if (!s.current) return;
    let stamp = 0;
    for (const addr of [s.current.id, ...s.current.aliases]) {
      try {
        stamp = Math.max(stamp, statSync(join(HOME, "v2", "notify", addr.replaceAll("/", "__"))).mtimeMs);
      } catch {}
    }
    if (stamp > s.lastStamp || s.lastStamp === 0) {
      s.lastStamp = Math.max(stamp, 1);
      s.due = true;
    }
    if (s.due) void checkMail();
  }

  async function checkMail() {
    if (!s.current || s.checking || !s.ctx) return;
    if (Date.now() - s.lastPointer < POINTER_GAP_MS) {
      s.due = true; // the poll loop comes back once the gap has passed
      return;
    }
    s.checking = true;
    s.due = false;
    try {
      const recipient = s.current.id;
      const generation = s.deliveryGeneration;
      const got = json((await cli(["pending", recipient])).out);
      if (generation !== s.deliveryGeneration || recipient !== s.current?.id) return;
      if (!got?.pointer) return;
      s.lastPointer = Date.now();
      // Idle: start a turn. Busy: "aside" lands at the next step boundary of the current run; "followUp" waited for
      // the whole run to end, which held mail for 30-50 minutes on long runs.
      const idle = s.ctx.isIdle?.() !== false;
      if (!idle && got.inline?.length) {
        s.pendingInline.push({ recipient, text: got.pointer, ids: got.inline });
      }
      pi.sendUserMessage(got.pointer, idle ? { attribution: "agent" } : { deliverAs: "aside", attribution: "agent" });
      if (idle && got.inline?.length) await cli(["injected", recipient, ...got.inline]);
    } finally {
      s.checking = false;
    }
  }

  function userMessageText(message) {
    if (message?.role !== "user") return;
    return typeof message.content === "string" ? message.content :
      message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n");
  }

  // A busy aside is only queued by sendUserMessage; this event confirms its user record entered context.
  async function messageEnd(event) {
    if (!s.current || !s.pendingInline.length) return;
    const text = userMessageText(event.message);
    const index = s.pendingInline.findIndex((delivery) => delivery.recipient === s.current.id && delivery.text === text);
    if (index < 0) return;
    const [delivery] = s.pendingInline.splice(index, 1);
    await cli(["injected", delivery.recipient, ...delivery.ids]);
  }

  return {
    version,
    async start(ctx) {
      s.deliveryGeneration += 1;
      s.pendingInline = [];
      s.ctx = ctx;
      s.state = ctx?.isIdle?.() === false ? "working" : "idle";
      await ensureRegistered(ctx);
      await syncPolicy();
      poll();
    },
    agentStart(ctx) {
      s.ctx = ctx;
      setState(s.blocked > 0 ? "blocked" : "working");
    },
    async agentEnd() {
      s.blocked = 0;
      setState("idle");
      // Old shims cannot forward message_end. A turn ending alone is not proof: abort can leave asides queued.
      if (!s.messageEndHook && s.current && s.pendingInline.length) {
        for (const entry of s.ctx.sessionManager.getBranch()) {
          if (!s.pendingInline.length) break;
          if (entry.type === "message") await messageEnd({ message: entry.message });
        }
      }
      if (s.due) void checkMail();
    },
    askStart() {
      s.blocked += 1;
      setState("blocked");
    },
    askEnd() {
      s.blocked = Math.max(0, s.blocked - 1);
      setState(s.blocked > 0 ? "blocked" : "working");
    },
    async shutdown(event) {
      s.deliveryGeneration += 1;
      s.pendingInline = [];
      if (!s.current) return;
      // omp 18.12 sends no reason. Mark exited anyway: a /reload or /resume rebinds and registers again at once,
      // and /new moves to a new id. Should a later omp name a non-quit reason, skip those.
      if (event?.reason && event.reason !== "quit") return;
      await cli(["exit", s.current.id, event?.reason ?? "shutdown"], 1_500);
      s.current = undefined;
    },
    heartbeat,
    poll,
    messageEnd,
    async call(name, params, ctx) {
      if (!s.current) await ensureRegistered(ctx);
      if (!s.current) throw new Error("agent-bridge: this session is not registered (is ~/bin/agent-bridge installed?)");
      const r = await cli(["call", name, "--me", s.current.id, "--args", JSON.stringify(params ?? {})], 10_000);
      if (!r.ok) throw new Error(r.out || `agent-bridge ${name} failed`);
      return { content: [{ type: "text", text: r.out }] };
    },
    // A swapped-in runtime announces itself once, so the registry shows which code each session runs.
    async adopted() {
      if (s.current) await heartbeat();
    },
  };
}
