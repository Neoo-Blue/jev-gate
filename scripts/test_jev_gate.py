#!/usr/bin/env python3
"""Offline self-check for jev_gate.py: a fake Jev drives the whole loop. Run: python3 scripts/test_jev_gate.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jev_gate as g  # noqa: E402

out = []
g.emit = out.append
g.HOME = Path(tempfile.mkdtemp())
SID = "test-session"
ASK = ("i want you to make a skill with jev, that auto starts everysessions when i asked for something on "
       "claude code, you give out what i asked and what you are building, and have it evaluate if it meets "
       "my requirement, if it does, do it, and then once finish, do a jev review on the summary, once done, "
       "open a public repo and publish it")
PLAN = """Here is the plan.

**What you asked:**
- A Jev skill that starts in every session
2. Publish it in a public repo

## What I'm building
1. A Claude Code plugin with hooks
- what you asked about is covered by a bullet, not a heading

Starting once Jev approves."""


def fake(**probs):
    """Jev stand-in: probability by question id, then by id prefix; judgments default to yes (0.9),
    the open-work and waiting flags (l, u, wait) to no (0.1)."""
    def ask(state, qs):
        return {k: probs.get(k, probs.get(k[0], 0.1 if k[0] in "lu" or k == "wait" else 0.9)) for k in qs}
    return ask


def last():
    return out[-1] if out else None


# Cutting the ask into clauses keeps each requirement quotable.
frags = g.clauses(ASK)
assert "once done, open a public repo and publish it" in frags, frags
assert all(g.units(f) >= 6 for f in frags), frags
assert g.clauses("做一个技能。然后发布到公开仓库，要能自动启动") == ["做一个技能。 然后发布到公开仓库，要能自动启动"]

asked, building = g.contract(PLAN)
assert asked == ["A Jev skill that starts in every session", "Publish it in a public repo"], asked
assert building == ["A Claude Code plugin with hooks", "what you asked about is covered by a bullet, not a heading"]
assert g.contract("**What you asked:** build X\n**What I'm building:** X.py") == (["build X"], ["X.py"])
assert g.contract("你要的：\n- 技能\n我要做的：\n- 插件") == (["技能"], ["插件"])
assert g.contract("Done. Everything works.") == ([], [])

# A new request opens a task and tells Claude the protocol.
g.on_prompt({"session_id": SID, "prompt": ASK})
assert last()["hookSpecificOutput"]["additionalContext"] == g.PROTOCOL
s = g.load(SID)
assert s["open"] and s["turn"] == 1 and s["asks"] == [ASK]

# No plan yet: project edits are denied, scratch and ~/.claude notes are not.
out.clear()
g.on_guard({"session_id": SID, "tool_input": {"file_path": "/Users/x/repo/app.py"}})
assert last()["hookSpecificOutput"]["permissionDecision"] == "deny"
out.clear()
g.on_guard({"session_id": SID, "tool_input": {"file_path": str(Path.home() / ".claude/projects/m/memory/a.md")}})
g.on_guard({"session_id": SID, "tool_input": {"file_path": os.path.join(tempfile.gettempdir(), "scratch.py")}})
assert out == []

# The plan message is checked; an uncovered requirement fails with the requirement quoted.
g.jev = fake(a1=0.2)
g.on_stop({"session_id": SID, "last_assistant_message": PLAN})
assert last()["decision"] == "block" and "Publish it in a public repo" in last()["reason"], last()
g.jev = fake()
g.on_stop({"session_id": SID, "last_assistant_message": PLAN})
assert last()["hookSpecificOutput"]["additionalContext"] == g.GO
out.clear()
g.on_guard({"session_id": SID, "tool_input": {"file_path": "/Users/x/repo/app.py"}})
assert out == []

# Background work in flight: not finished, no review.
out.clear()
g.on_stop({"session_id": SID, "last_assistant_message": "waiting", "background_tasks": [{"id": "t1"}]})
assert out == []

# The review fails on an undelivered promise and on a line that leaves work open.
g.jev = fake(i2=0.1, l0=0.8)
g.on_stop({"session_id": SID, "last_assistant_message": "Built the plugin.\nI will publish it tomorrow."})
r = last()
assert r["decision"] == "block" and "[promised] A Claude Code plugin with hooks" in r["reason"], r
assert "[left open]" in r["reason"] and "round 2 of" in r["reason"], r  # the failed plan check used round 1

# Blocked on the user: pause instead of looping, and the ask carries into the next prompt.
g.jev = fake(i0=0.1, wait=0.9)
g.on_stop({"session_id": SID, "last_assistant_message": "Which GitHub org should own the repo?"})
assert "paused" in last()["systemMessage"]
g.on_prompt({"session_id": SID, "prompt": "use my personal account"})
assert g.load(SID)["asks"] == [ASK, "use my personal account"] and g.load(SID)["rounds"] == 0

# The new turn needs its own plan; then everything delivered passes and later stops are left alone.
g.jev = fake()
g.on_stop({"session_id": SID, "last_assistant_message": PLAN})
g.jev = fake()
g.on_stop({"session_id": SID, "last_assistant_message": "All done: plugin at ./jev-gate, repo public."})
assert last()["systemMessage"].startswith("jev-gate: PASS"), last()
out.clear()
g.on_stop({"session_id": SID, "last_assistant_message": "fixed a lint nit"})
assert out == []

# After a pass the next prompt starts a fresh task.
g.on_prompt({"session_id": SID, "prompt": "what does YES mean?"})
assert g.load(SID)["asks"] == ["what does YES mean?"] and "plan" not in g.load(SID)

# A question needs no plan; failing past the round cap stops with the open items for the user.
g.jev = fake(d=0.1)
for _ in range(g.MAX_ROUNDS + 1):
    g.on_stop({"session_id": SID, "last_assistant_message": "hmm"})
assert "still fails after" in last()["systemMessage"] and "what does YES mean?" in last()["systemMessage"]


# Jev unreachable: fail open with a warning, never block.
def down(state, qs):
    raise RuntimeError("Jev HTTP 503")


g.jev = down
g.on_prompt({"session_id": SID, "prompt": "ship it"})
g.on_stop({"session_id": SID, "last_assistant_message": "shipped"})
assert last() == {"systemMessage": "jev-gate: review skipped (Jev HTTP 503)."} and not g.load(SID)["open"]

# Headless and SDK runs are skipped unless forced.
os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
assert not g.active()
os.environ["JEV_GATE"] = "on"
assert g.active()
os.environ["JEV_GATE"] = "off"
os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
assert not g.active()

print("jev-gate self-check: ok")
