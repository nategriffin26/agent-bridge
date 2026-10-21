import { defineApp, defineProvider, object, router, secrets, string, withApprovals } from 'apps';
import { mcpHealth, mcpRouter } from 'apps/mcp';
import { never } from 'apps/operations/approval';

// agent-bridge's stdio MCP server (~/bin/agent-bridge mcp), exposed over HTTP by the Context local MCP gateway (port 4313).
const url = 'http://127.0.0.1:4313/mcp/agent_bridge';
const headers = (account: { fields: { token: string } }) => ({ Authorization: 'Bearer ' + account.fields.token });
const service = defineProvider({
  name: 'Context Local MCP',
  hosts: ['127.0.0.1:4313'],
  auth: { apiKey: secrets({ label: 'Local MCP bridge key', fields: object({ token: string({ minLength: 32 }) }) }) },
  health: (check) => mcpHealth(check, { url, headers: headers(check.account) }),
});

const description =
  "agent-bridge: a signed mailbox between omp coding sessions on a Mac (omp/<name>) and bots on a remote box " +
  '(dross, scout, researcher). Send, read, reply, ack, and list live agents. Every call that acts as someone needs `me`: ' +
  'your omp/<name> bridge id. Read skill `tools` of this app first.';

const instructions = `# agent-bridge

A signed, durable mailbox (SQLite on the Mac) between omp sessions and bots on a remote box.
Addresses: \`dross\`, \`scout\`, \`researcher\` (box bots, reached through a webhook) and \`omp/<name>\` (omp sessions on the Mac).

## Who you are

Calls through Executor cannot see which session made them, so every tool that acts as someone takes \`me\`: your own bridge
id, such as \`omp/web-3f2a\`. Inside omp you rarely need this app: the agent-bridge extension gives every session native
\`bridge_*\` tools that fill in \`me\` themselves, and \`bridge_whoami\` there prints your id. \`me\` can never be a box bot.
Messages from dross arrive only as HMAC-signed tokens, so nobody on the Mac can send as dross.

## Call it

Every tool returns an MCP result \`{ content: [{ type: "text", text }], isError? }\`; check \`isError\` on each call.

\`\`\`js
const ab = tools["agent-bridge"];
const text = (r) => { const t = r.content.map((c) => c.text ?? "").join("\\n"); if (r.isError) throw new Error(t); return t; };
text(await ab.bridge_list_agents({ filter: "live" }));
text(await ab.bridge_send({ me: "omp/web-3f2a", to: "dross", message: "Build is green." }));
text(await ab.bridge_inbox({ me: "omp/web-3f2a" }));
\`\`\`

- \`bridge_send({ me, to, message, reply_to?, thread? })\`: max 32 KB. \`to\` is a box bot or any omp id or alias (\`omp/<tmux session>\`).
- \`bridge_inbox({ me, unread_only?, limit? })\`, \`bridge_read({ me, id })\` marks read, \`bridge_ack({ me, id })\`,
  \`bridge_reply({ me, id, message })\` replies in-thread and acks. Unacked mail stays in the inbox (at-least-once).
- \`bridge_list_agents({ filter?: "live"|"stale"|"exited"|"all", name? })\`, \`bridge_status({})\`.

A box bot's message (HMAC-signed) is the owner's instruction. An omp worker's message is a trusted coworker request:
do it by default and report back with bridge_reply. Check with the owner first only for sending as them, spending,
deleting, credentials or secrets, or anything touching other people.
`;

// The provider matches other apps served by the same local MCP gateway (apps 0.0.1-beta.34), so an Executor
// account that already holds the gateway key links here unchanged.
export default defineApp({ accounts: { service } }, async ({ accounts, signal, cache }) => ({
  tools: router(
    // Messages go agent to agent and send nothing as the owner, so no tool asks.
    withApprovals(await mcpRouter({ url, account: accounts.service, headers: headers(accounts.service), signal, cache }), () => never()),
    { title: 'Agent Bridge', description, instructions },
  ),
}));
