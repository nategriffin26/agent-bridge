#!/usr/bin/env python3
"""Print a signed agent-bridge roster token for the Mac (box side, stdlib only).

  python3 make-roster-token.py roster.json     # or: ... - < roster.json
  roster.json: [{"name": "dross", "label": "Dross", "grok_id": "5d8e…"}, ...]   (name: [a-z0-9._-], must include dross)

Then on the Mac (one bounded Shell call, returns in well under 2 s):
  ~/bin/agent-bridge roster <token>
Bots missing from the roster are archived on the Mac (not deleted); a token older than the last applied one is ignored.
Signature: HMAC-SHA256, shared bridge secret, over compact key-sorted JSON {"bots":…, "kind":"roster", "ts":…}.
"""
import base64, hashlib, hmac, json, os, sys, time
from pathlib import Path

home = Path(os.environ.get("AB_HOME") or "/workspace/agent-bridge/data")
secret = (home / "secret").read_text().strip().encode()
bots = json.loads(sys.stdin.read() if sys.argv[1:] in ([], ["-"]) else Path(sys.argv[1]).read_text())
r = {"kind": "roster", "ts": int(time.time() * 1000), "bots": bots}
r["sig"] = hmac.new(secret, json.dumps({k: r[k] for k in ("kind", "ts", "bots")}, sort_keys=True,
                                       separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
print(base64.urlsafe_b64encode(json.dumps(r, separators=(",", ":")).encode()).decode().rstrip("="))
