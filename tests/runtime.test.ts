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

for (const idle of [true, false]) {
  test(`inline mail becomes read after injection; oversized mail stays unread (${idle ? "idle" : "busy"})`, async () => {
    const id = `omp/runtime-${idle}`;
    command("register", "--json", JSON.stringify({ session_id: id, ab_self: id, pid: process.pid, cwd: home }));
    const inline = command("send", "omp/sender", id, "inline question").split(" ")[1];
    const large = command("send", "omp/sender", id, "x".repeat(16385)).split(" ")[1];
    const messages = (): { id: string; state: string; read_at: number }[] =>
      JSON.parse(command("messages", "--json", "--addr", id));
    let injected = false;
    let injectionTime = 0;
    const finished = Promise.withResolvers<void>();
    let checking = false;
    const state = {
      current: { id, aliases: [] }, ctx: { isIdle: () => idle },
      lastStamp: 0, lastPointer: 0, due: false,
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
        injected = true;
      },
    }, state);
    runtime.poll();
    await finished.promise;
    expect(injected).toBe(true);
    const rows = messages();
    expect(rows.find((m) => m.id === inline)?.state).toBe("read");
    expect(rows.find((m) => m.id === inline)?.read_at).toBeGreaterThanOrEqual(injectionTime);
    expect(rows.find((m) => m.id === large)?.state).toBe("notified");
  });
}
