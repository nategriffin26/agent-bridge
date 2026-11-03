import { afterAll, expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const home = mkdtempSync(join(tmpdir(), "bridge-runtime-"));
const cli = resolve(import.meta.dir, "../src/agent_bridge.py");
process.env.AB_HOME = home;
process.env.AB_CLI = cli;
process.env.AB_NO_SPAWN = "1";
writeFileSync(join(home, "config.json"), JSON.stringify({ side: "mac" }));
writeFileSync(join(home, "secret"), "test-secret-" + "x".repeat(40), { mode: 0o600 });
// The runtime reads AB_HOME/AB_CLI at import time; load after isolating the store, optionally selecting old code.
const { createRuntime } = await import(process.env.AB_TEST_RUNTIME || "../extension/bridge-runtime.ts");

function command(...args: string[]) {
  const result = Bun.spawnSync([cli, ...args], { env: process.env });
  if (result.exitCode !== 0) throw new Error(result.stderr.toString());
  return result.stdout.toString().trim();
}

command("init", "--side", "mac");
afterAll(() => rmSync(home, { recursive: true, force: true }));

type UserMessage = { role: "user"; content: { type: "text"; text: string }[] };

async function queueDelivery(name: string, idle: boolean, messageEndHook = true) {
  const id = `omp/${name}`;
  command("register", "--json", JSON.stringify({ session_id: id, ab_self: id, pid: process.pid, cwd: home }));
  const inline = command("send", "omp/sender", id, "inline question").split(" ")[1];
  const large = command("send", "omp/sender", id, "x".repeat(16385)).split(" ")[1];
  const messages = (): { id: string; state: string; read_at: number }[] =>
    JSON.parse(command("messages", "--json", "--addr", id));
  const states = () => {
    const rows = messages();
    return [rows.find((m) => m.id === inline)?.state, rows.find((m) => m.id === large)?.state];
  };
  const branch: { type: "message"; message: UserMessage }[] = [];
  const ctx = { cwd: home, isIdle: () => idle, sessionManager: { getSessionId: () => id, getBranch: () => branch } };
  let queuedText = "";
  let injectionTime = 0;
  const finished = Promise.withResolvers<void>();
  let checking = false;
  const state = {
    current: { id, aliases: [], sessionId: id }, ctx, state: idle ? "idle" : "working",
    policyVersion: JSON.parse(command("policy")).version,
    messageEndHook, lastStamp: 0, lastPointer: 0, due: false,
    get checking() { return checking; },
    set checking(value: boolean) { checking = value; if (!value) finished.resolve(); },
  };
  const runtime = createRuntime({
    async exec(file: string, args: string[]) {
      const result = Bun.spawnSync([file, ...args], { env: process.env });
      return { code: result.exitCode, stdout: result.stdout.toString(), stderr: result.stderr.toString() };
    },
    sendUserMessage(text: string, options: object) {
      expect(messages().map((m) => m.state)).toEqual(["notified", "notified"]);
      expect(text).toContain("inline question");
      expect(text).toContain(large);
      expect(options).toEqual(idle ? { attribution: "agent" } : { deliverAs: "aside", attribution: "agent" });
      injectionTime = Date.now() / 1000;
      queuedText = text;
    },
  }, state);
  runtime.poll();
  await finished.promise;
  expect(queuedText).not.toBe("");
  const event: { message: UserMessage } = { message: { role: "user", content: [{ type: "text", text: queuedText }] } };
  return { runtime, state, messages, states, inline, large, event, branch, ctx, injectionTime };
}

test("idle inline delivery marks read; the large-body pointer stays unread", async () => {
  const delivery = await queueDelivery("idle", true);
  const rows = delivery.messages();
  expect(rows.find((m) => m.id === delivery.inline)?.state).toBe("read");
  expect(rows.find((m) => m.id === delivery.inline)?.read_at).toBeGreaterThanOrEqual(delivery.injectionTime);
  expect(rows.find((m) => m.id === delivery.large)?.state).toBe("notified");
});

test("a queued busy aside stays unread until its user message enters context", async () => {
  const delivery = await queueDelivery("busy", false);
  expect(delivery.states()).toEqual(["notified", "notified"]);
  await delivery.runtime.agentEnd();
  await delivery.runtime.messageEnd({ message: { role: "assistant", content: delivery.event.message.content } });
  await delivery.runtime.messageEnd({ message: { role: "user", content: "unrelated message" } });
  expect(delivery.states()).toEqual(["notified", "notified"]);
  await delivery.runtime.messageEnd(delivery.event);
  expect(delivery.states()).toEqual(["read", "notified"]);
  await delivery.runtime.messageEnd(delivery.event);
  expect(JSON.parse(command("pending", delivery.state.current.id)).new).toEqual([]);
});

test("an old shim marks only asides present in the transcript at agentEnd", async () => {
  const delivery = await queueDelivery("old-shim", false, false);
  expect(delivery.states()).toEqual(["notified", "notified"]);
  await delivery.runtime.agentEnd(); // An aborted turn can end without draining the aside queue.
  expect(delivery.states()).toEqual(["notified", "notified"]);
  delivery.branch.push({ type: "message", message: delivery.event.message });
  await delivery.runtime.agentEnd();
  expect(delivery.states()).toEqual(["read", "notified"]);
});

for (const messageEndHook of [true, false]) {
  test(`shutdown discards queued read confirmations (${messageEndHook ? "new" : "old"} shim)`, async () => {
    const delivery = await queueDelivery(`shutdown-${messageEndHook}`, false, messageEndHook);
    expect(delivery.states()).toEqual(["notified", "notified"]);
    await delivery.runtime.shutdown({});
    delivery.branch.push({ type: "message", message: delivery.event.message });
    await delivery.runtime.agentEnd();
    await delivery.runtime.messageEnd(delivery.event);
    expect(delivery.states()).toEqual(["notified", "notified"]);
  });
}

test("session switching discards confirmations from the outgoing context", async () => {
  const delivery = await queueDelivery("switch", false);
  expect(delivery.states()).toEqual(["notified", "notified"]);
  await delivery.runtime.start({ ...delivery.ctx, sessionManager: { getSessionId: () => "next-session", getBranch: () => [] } });
  await delivery.runtime.messageEnd(delivery.event);
  expect(delivery.states()).toEqual(["notified", "notified"]);
});
