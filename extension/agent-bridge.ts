// agent-bridge omp extension: a stable shim. Symlinked into ~/.omp/agent/extensions/; source in this repo.
// Every main omp session, however it was started, registers with the bridge, heartbeats, and is woken natively when
// mail arrives (a one-line pointer, never the body). The behavior lives in bridge-runtime.ts, which this shim
// re-imports whenever that file changes, so updates reach running sessions within one poll (3 s) and no /reload.
// Keep this file small and unchanging: hooks and tool registrations are fixed when omp loads it.
// @ts-nocheck
import { realpathSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const RUNTIME = process.env.AB_RUNTIME || join(dirname(realpathSync(fileURLToPath(import.meta.url))), "bridge-runtime.ts");
const HEARTBEAT_MS = 30_000;
const POLL_MS = 3_000;

// Shared across this process's runtimes (omp rebinds extensions on /reload, /new, /resume, /fork).
const s = { state: "idle", blocked: 0, lastStamp: 0, lastPointer: 0, due: false, checking: false };

export default function agentBridge(pi) {
  const z = pi.zod;
  let rt;
  let loadedAt = -1;
  let timers = [];
  const main = (ctx) => ctx?.agent?.kind !== "sub";

  async function runtime() {
    let mtime = -1;
    try {
      mtime = statSync(RUNTIME).mtimeMs;
    } catch {}
    if (mtime !== loadedAt || !rt) {
      // Dynamic on purpose: the query busts Bun's module cache so an edited runtime is loaded into this live session.
      const mod = await import(`${RUNTIME}?v=${mtime}`);
      const fresh = !!rt;
      rt = mod.createRuntime(pi, s);
      loadedAt = mtime;
      if (fresh) await rt.adopted();
    }
    return rt;
  }

  async function start(ctx) {
    if (!main(ctx)) return;
    for (const t of timers) ctx.clearTimer?.(t);
    timers = [
      ctx.setInterval(async () => (await runtime()).heartbeat(), HEARTBEAT_MS),
      ctx.setInterval(async () => (await runtime()).poll(), POLL_MS),
    ];
    await (await runtime()).start(ctx);
  }

  pi.on("session_start", async (_e, ctx) => start(ctx));
  pi.on("session_switch", async (_e, ctx) => start(ctx));
  pi.on("agent_start", async (_e, ctx) => main(ctx) && (await runtime()).agentStart(ctx));
  pi.on("agent_end", async (_e, ctx) => main(ctx) && (await runtime()).agentEnd());
  pi.on("tool_execution_start", async (e, ctx) => e?.toolName === "ask" && main(ctx) && (await runtime()).askStart());
  pi.on("tool_execution_end", async (e, ctx) => e?.toolName === "ask" && main(ctx) && (await runtime()).askEnd());
  pi.on("session_shutdown", async (e, ctx) => main(ctx) && (await runtime()).shutdown(e));

  // Tool names, parameters and descriptions are fixed at load, so they say only what never changes; how to treat a
  // message comes with the message itself (bridge_read) and from the bridge policy the runtime keeps in context.
  const tool = (name, label, description, parameters) =>
    pi.registerTool({
      name,
      label,
      description,
      parameters,
      async execute(_id, params, _signal, _onUpdate, ctx) {
        return (await runtime()).call(name, params, ctx);
      },
    });

  const id = z.string().describe("message id, e.g. 1792559452445-d4c5077d");
  tool(
    "bridge_send",
    "Bridge Send",
    "Send a message over agent-bridge to a box bot (see bridge_list_agents) or any omp worker " +
      "(omp/<name>). Point first, then context. Max 32 KB. You are sent as this session's bridge id.",
    z.object({
      to: z.string().describe("a box bot name or omp/<name>"),
      message: z.string(),
      reply_to: z.string().optional().describe("id of the message you answer"),
      thread: z.string().optional().describe("thread id to continue (defaults from reply_to)"),
    }),
  );
  tool(
    "bridge_inbox",
    "Bridge Inbox",
    "List bridge messages for this session (unread by default; unread_only=false also shows read, unacked ones).",
    z.object({ unread_only: z.boolean().optional(), limit: z.number().int().positive().optional() }),
  );
  tool(
    "bridge_read",
    "Bridge Read",
    "Read one bridge message and mark it read. Its header says who sent it and how to handle it; follow that.",
    z.object({ id }),
  );
  tool(
    "bridge_ack",
    "Bridge Ack",
    "Mark a bridge message handled. Unacked messages stay in the inbox (at-least-once delivery).",
    z.object({ id }),
  );
  tool(
    "bridge_reply",
    "Bridge Reply",
    "Reply to a bridge message (to its sender, same thread) and ack it.",
    z.object({ id, message: z.string() }),
  );
  tool(
    "bridge_list_agents",
    "Bridge Agents",
    "Directory of agents on the bridge: omp sessions on this Mac (live, stale, exited) and the box bots.",
    z.object({
      filter: z.enum(["live", "stale", "exited", "all"]).optional(),
      name: z.string().optional().describe("substring of id or alias"),
    }),
  );
  tool("bridge_whoami", "Bridge Whoami", "This session's bridge id, aliases and registration.", z.object({}));
  tool("bridge_status", "Bridge Status", "Bridge health: on/off, webhook queue, failures, live agents.", z.object({}));
}
