"""Stands in for the `claude` CLI in tests: prints stream-json events like the real one.

Records its argv to FAKE_CLAUDE_LOG (one JSON line per call) so tests can check the flags.
"""
import json
import os
import sys
import time

args = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd(), "stdin_tty": sys.stdin.isatty()}) + "\n")

prompt = args[args.index("-p") + 1] if "-p" in args else ""
resume = args[args.index("--resume") + 1] if "--resume" in args else ""
session = resume or "sess-" + str(abs(hash(prompt)) % 10000)
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")

if mode == "crash":
    print("fatal: something broke", file=sys.stderr)
    sys.exit(3)

print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "model": "fake"}))
sys.stdout.flush()
if mode == "slow":
    time.sleep(30)
print(json.dumps({"type": "assistant", "session_id": session, "message": {"content": [
    {"type": "tool_use", "name": "Read", "input": {"file_path": "/proj/src/main.py"}}]}}))
print(json.dumps({"type": "system", "subtype": "permission_denied", "session_id": session, "tool_name": "Bash"}))
print(json.dumps({"type": "assistant", "session_id": session, "message": {"content": [
    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/proj/src/main.py"}},
    {"type": "text", "text": "Working on it"}]}}))
print("not json noise line")
sys.stdout.flush()
time.sleep(0.2)
text = f"Resumed: {prompt}" if resume else f"Done: {prompt}\nChanged src/main.py"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                  "session_id": session, "total_cost_usd": 0.0421, "duration_ms": 1234, "num_turns": 3}))
