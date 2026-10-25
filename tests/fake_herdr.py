#!/usr/bin/env python3
"""Stand-in for herdr in tests. Logs each argv as a JSON line to $FAKE_HERDR_LOG.
$FAKE_HERDR_LABEL is the workspace label it reports (default "vault"); $FAKE_HERDR_FAIL=1 makes `agent prompt` fail;
$FAKE_HERDR_HANG=1 makes every call hang."""
import json, os, sys, time

if os.environ.get("FAKE_HERDR_HANG"):
    time.sleep(60)
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] == ["workspace", "get"]:
    print(json.dumps({"id": "cli:workspace:get", "result": {"type": "workspace_info", "workspace": {
        "label": os.environ.get("FAKE_HERDR_LABEL", "vault"), "workspace_id": args[2]}}}))
elif args[:2] == ["agent", "prompt"]:
    if os.environ.get("FAKE_HERDR_FAIL"):
        sys.exit(1)
    print("{}")
