#!/usr/bin/env python3
"""tmux registration stand-in. FAKE_TMUX_HANG=1 makes the call hang."""
import os, sys, time

if os.environ.get("FAKE_TMUX_HANG"):
    time.sleep(60)
args = sys.argv[1:]
if args[:1] == ["display-message"]:
    print("e2e\t0\t1")
