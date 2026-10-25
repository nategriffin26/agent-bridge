#!/usr/bin/env python3
"""Stand-in for tmux in tests: reports session e2e for any pane. Logs each argv as a JSON line to $FAKE_TMUX_LOG.
$FAKE_TMUX_HANG=1 makes every call hang."""
import json, os, sys, time

if os.environ.get("FAKE_TMUX_HANG"):
    time.sleep(60)
with open(os.environ["FAKE_TMUX_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["display-message"]:
    print("e2e\t0\t1")
