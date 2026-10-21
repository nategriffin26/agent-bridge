#!/usr/bin/env python3
"""Stand-in for tmux in tests. Logs each argv as a JSON line to $FAKE_TMUX_LOG.
$FAKE_TMUX_MODE is the pane_in_mode it reports (default 0); $FAKE_TMUX_HANG=1 makes every call hang."""
import json, os, sys, time

if os.environ.get("FAKE_TMUX_HANG"):
    time.sleep(60)
with open(os.environ["FAKE_TMUX_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:1] == ["list-panes"]:
    print("0\t%8\n1\t%9")
elif args[:1] == ["display-message"] and "#{pane_in_mode}" in args:
    print(os.environ.get("FAKE_TMUX_MODE", "0"))
elif args[:1] == ["display-message"]:
    print("e2e\t0\t1")
