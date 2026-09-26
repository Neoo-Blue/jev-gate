#!/usr/bin/env python3
"""jev-gate: Jev, TypeSafe's judgment model, holds Claude Code to what you asked.

  prompt  (UserPromptSubmit)  records the request and tells Claude the protocol
  guard   (PreToolUse)        no file edits until Jev has approved this turn's plan
  stop    (Stop)              a "What you asked / What I'm building" message gets a Jev plan
                              check; any other final message gets a Jev review, and every gap
                              sends Claude back to work, up to JEV_GATE_MAX_ROUNDS times

Jev returns yes/no probabilities, never text, so code cuts the request, the plan and the
summary into items and Jev judges each one; gaps are reported by quoting those items.

Env: TYPESAFE_API_KEY. JEV_GATE=off disables the gate; JEV_GATE=on forces it in `claude -p`
and SDK runs, where it is skipped by default. JEV_GATE_MAX_ROUNDS (default 5).
Fails open: without a key, or on any network or API error, it warns and never blocks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
YES = 0.5   # ponytail: one threshold for every question; split it per question if the log shows misses
WAIT = 0.7  # a reply that is blocked on the user pauses the gate instead of looping
MAX_ROUNDS = int(os.environ.get("JEV_GATE_MAX_ROUNDS") or 5)
# Seconds a Jev call may take, kept under each hook's timeout in hooks.json (UserPromptSubmit 30,
# Stop 120) so a hang ends in this script's fail-open path instead of Claude Code killing the hook.
PROMPT_BUDGET, STOP_BUDGET = 15.0, 90.0
HOME = Path.home() / ".claude" / "jev-gate"
SCRIPT = Path(__file__).resolve()
RAW = ""  # the hook input, kept so jev() can run the hook again under `sesame run`

HEAD = re.compile(
    r"^[\s#>*_]*(what you asked(?: for)?|what i['’]?m building|what i am building|what i['’]?ll build"
    r"|what i will build|你要的|你的要求|我要做的)[\s*_]*(?:[:：][\s*_]*(.*?))?[\s*_]*$", re.I)

PROTOCOL = """jev-gate is on. Jev (TypeSafe's judgment model) checks your plan before you build and your final summary before you stop.
1. If this request needs anything built, changed or run: explore as much as you need, then send a message with two bullet lists under the exact headings "What you asked:" (every part of the request, in the user's terms, nothing dropped) and "What I'm building:" (each deliverable), and end your turn there. When you stop, Jev checks the lists: approved, you are told to build; not approved, you get the gaps to fix. Change nothing (files, git, deploys) before the plan is approved; file edits are blocked until then. Pure questions and chat skip this step.
2. Do all of it, then end with a summary that shows each asked and promised item done, with evidence (files, commands and results, URLs), and no promises of later work. Jev reviews that summary, and anything it can't see delivered sends you back to finish it."""

GO = ("jev-gate: Jev approved your plan. Build all of it now. When done, end with a summary showing each "
      "asked and promised item delivered, with evidence (files, commands and results, URLs). Jev reviews "
      "that summary before you can stop.")
PLAN_FAIL = ("Jev does not see these parts of the request covered by \"What I'm building\". Add them to the "
             "plan (or fix \"What you asked\" if you misread the request), then send both lists again and "
             "end your message; the plan is re-checked when you stop.")
REVIEW_FAIL = ("Jev does not see these delivered in your final message. Finish each one now; if one is "
               "already done, show the evidence. Then end with a complete item-by-item summary; the review "
               "runs again when you stop. If an item truly needs the user (a decision or credential only "
               "they have), ask for it plainly and stop.")

# Each question: (instruction, what yes means, what no means). Backticked names point into the state.
REQUEST = ("Read `fragment` as part of `user_messages` (oldest first). Is it asking for something to be "
           "done, built, changed or answered that the user still wants?",
           "A request, requirement, instruction or question the user wants handled, including conditions "
           "on how it should be done.",
           "Background, a reason, a greeting or filler, or something a later message cancelled or replaced.")
COVERED = ("Does `plan.building` deliver `requirement` completely?",
           "Some items in plan.building clearly produce all of it.",
           "Nothing in plan.building delivers it, or only part of it.")
COVERED_FRAG = ("Does `plan.building` deliver what `fragment` asks for?",) + COVERED[1:]
DONE = ("Does `final_reply` show that all of `item` has been done or answered?",
        "The reply reports every part of it finished, with concrete detail such as files, commands and results, "
        "or links.",
        "Any part is missing from the reply, only restated as a plan, or called partial, pending, skipped or next.")
DONE_FRAG = ("Does `final_reply` show that all of what `fragment` asks for has been done or answered?",) + DONE[1:]
# One question per summary line: does it commit to later work? A second one, "does it admit requested
# work unfinished?", misread lines describing tests built to fail ("a plan missing the publish step
# failed the check": 0.90). DONE reads the whole reply and catches real admissions instead.
LATER = ("Does `sentence`, a line of `final_reply`, commit the assistant to doing something after this reply?",
         "The assistant says it will do, check or finish something later (\"I will\", \"I'll keep an eye\", "
         "\"next I'll\", \"TODO\").",
         "No commitment by the assistant: it reports, explains, suggests, offers or asks (\"want me to...?\", "
         "\"let me know\"), or tells the user what they could do or add later.")
CONTINUES = ("Is `new_message` the user's reply to `assistant_stopped_on`, or otherwise a continuation of the "
             "request in `earlier_messages`?",
             "It answers the assistant's question (even a bare name, number, choice, yes or no counts), confirms, "
             "corrects or adds to the same request.",
             "It ignores the question and starts something unrelated, asks about another topic, or drops the old "
             "request.")
WAITING = ("Is `final_reply` stopping to wait for the user, asking for something only they can give (a "
           "decision, credentials, access, a physical action) that the remaining requested work cannot go "
           "on without?",
           "Yes, the requested work is blocked on the user's answer.",
           "No question, or only an optional one; the work could have continued without the user.")


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def active() -> bool:
    flag = os.environ.get("JEV_GATE", "").lower()
    if flag in ("0", "off", "false", "no"):
        return False
    if flag in ("1", "on", "true", "yes"):
        return True
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    return not (entry.startswith("sdk") or entry in ("mcp", "claude-code-github-action"))


def spath(sid: str) -> Path:
    return HOME / ((re.sub(r"[^\w-]", "", sid)[:100] or "nosession") + ".json")


def load(sid: str) -> dict:
    try:
        return json.loads(spath(sid).read_text())
    except (OSError, ValueError):
        return {}


def save(sid: str, s: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    p = spath(sid)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, p)


def log(sid: str, event: str, p: dict, gaps: list) -> None:
    try:
        f = HOME / "log.jsonl"
        if f.exists() and f.stat().st_size > 5_000_000:
            f.replace(HOME / "log.1.jsonl")
        with f.open("a") as fh:
            fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "sid": sid[:8], "event": event,
                                 "gaps": gaps, "p": {k: round(v, 2) for k, v in p.items()}}) + "\n")
    except OSError:
        pass


def units(text: str) -> int:
    """Rough word count: Latin words, plus one per CJK character."""
    return len(re.findall(r"[A-Za-z0-9_'\u2019]+|[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]", text))


def clauses(text: str, least: int = 6, longest: int = 25) -> list:
    """Cut text into clause-sized items: sentences and lines, long ones again at commas.
    Fragments under `least` words ride along with the next one. Code blocks are dropped."""
    out, buf = [], ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    for sent in re.split(r"(?<=[.!?;])\s+|(?<=[。！？；])|\n+", text):
        for part in (re.split(r"[,，]\s*", sent) if units(sent) > longest else [sent]):
            part = part.strip(" \t-*•>#|")
            if not part:
                continue
            buf = f"{buf}{' ' if buf[-1:] in '.!?;:。！？；：' else ', '}{part}" if buf else part
            if units(buf) >= least:
                out.append(buf)
                buf = ""
    if buf:
        if out:
            out[-1] += ", " + buf
        else:
            out.append(buf)
    return out


def contract(text: str) -> tuple:
    """The "What you asked" / "What I'm building" bullet lists in a message, or ([], [])."""
    got, cur = {}, None
    fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
            continue
        if fence:  # a code block belongs to the item above it; bullets inside it are content, not items
            if cur and got[cur]:
                got[cur][-1] += " " + line.strip()
            continue
        m = HEAD.match(line)
        if m:
            cur = "asked" if re.search(r"asked|你要|你的", m.group(1), re.I) else "building"
            got[cur] = [m.group(2).strip()] if (m.group(2) or "").strip() else []
            continue
        if cur is None:
            continue
        item = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)", line)
        if item:
            got[cur].append(item.group(1))
        elif line.strip() and not line[:1].isspace():
            cur = None  # prose after a list ends it
    return got.get("asked", []), got.get("building", [])


def noul(q: tuple, **data) -> dict:
    return {"type": "noul", "instructions": {**data, "question": q[0]}, "criteria": {"true": q[1], "false": q[2]}}


def jev(state: dict, questions: dict, budget: float = STOP_BUDGET) -> dict:
    """Ask Jev every question in one request; {id: probability of yes}. Raises on any failure, and
    gives up within `budget` seconds."""
    deadline = time.monotonic() + budget
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and "JEV_GATE_INPUT" not in os.environ and shutil.which("sesame"):
        # Key kept in the sesame keychain: run this hook again with it injected and pass its answer on.
        # A child process, not exec, so a sesame failure still lands in the caller's fail-open path.
        r = subprocess.run(["sesame", "run", "typesafe", "--", sys.executable, str(SCRIPT)] + sys.argv[1:],
                           env={**os.environ, "JEV_GATE_INPUT": RAW}, capture_output=True, text=True, timeout=budget)
        if r.returncode != 0:
            raise RuntimeError(f"sesame run typesafe failed: {(r.stderr or r.stdout).strip()[:200]}")
        sys.stdout.write(r.stdout)
        sys.exit(0)
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY is not set")
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    req = urllib.request.Request(URL, body, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=max(1.0, min(25.0, deadline - time.monotonic()))) as r:
                answers = json.load(r)["answers"]
            return {k: float(answers[k]["noul"]) for k in questions}
        except urllib.error.HTTPError as e:
            wait = min(10.0, float(e.headers.get("retry-after") or 2 ** attempt))
            if attempt == 2 or (e.code != 429 and e.code < 500) or time.monotonic() + wait > deadline:
                raise RuntimeError(f"Jev HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from None
            time.sleep(wait)
    raise RuntimeError("unreachable")


def listed(gaps: list) -> str:
    return "\n".join(f"- {g}" for g in gaps)


def retry(sid: str, s: dict, what: str, gaps: list, how: str, reply: str) -> None:
    seen = hashlib.sha1(reply.encode()).hexdigest()
    stuck = seen == s.get("last_reply")  # same reply as the last failed round: going around in circles
    s.update(last_reply=seen, rounds=s.get("rounds", 0) + 1)
    if stuck or s["rounds"] > MAX_ROUNDS:
        s["open"] = False  # handed to the user, so the next message starts fresh instead of dragging this along
        save(sid, s)
        why = "Claude's reply stopped changing" if stuck else f"{MAX_ROUNDS} rounds"
        emit({"systemMessage": f"jev-gate: the {what} still fails after {why}, stopping so you can decide. "
                               f"Open:\n{listed(gaps)}"})
        return
    save(sid, s)
    emit({"decision": "block",
          "reason": f"jev-gate {what}, round {s['rounds']} of {MAX_ROUNDS}: FAIL. {how}\n{listed(gaps)}"})


def on_prompt(inp: dict) -> None:
    sid = inp.get("session_id") or ""
    s = load(sid)
    prompt = (inp.get("prompt") or "")[:8000]
    carry = False
    if s.get("open") and s.get("asks"):  # an unfinished request (paused or interrupted): does this continue it?
        try:
            state = {"earlier_messages": s["asks"], "assistant_stopped_on": s.get("paused_on", ""), "new_message": prompt}
            carry = jev(state, {"c": noul(CONTINUES)}, budget=PROMPT_BUDGET)["c"] >= YES
        except Exception:  # noqa: BLE001 - when unsure, start fresh rather than judge against the old request
            carry = False
    # Answering the question an approved plan stopped on keeps the approval; anything else plans anew.
    keep = carry and s.get("paused") and s.get("plan_turn") == s.get("turn")
    if not carry:
        s = {"turn": s.get("turn", 0)}
    s["asks"] = (s.get("asks", []) + [prompt])[-6:]
    s.update(turn=s["turn"] + 1, rounds=0, open=True, paused=False, paused_on="", last_reply=None)
    if keep:
        s["plan_turn"] = s["turn"]
    save(sid, s)
    emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": PROTOCOL}})


def on_guard(inp: dict) -> None:
    s = load(inp.get("session_id") or "")
    if not s.get("open") or s.get("plan_turn") == s.get("turn"):
        return
    tool = inp.get("tool_input") or {}
    path = tool.get("file_path") or tool.get("notebook_path") or ""
    free = [Path.home() / ".claude", tempfile.gettempdir(), "/tmp", inp.get("scratchpad_dir")]
    real = os.path.realpath(os.path.expanduser(path)) if path else ""
    if real and any(d and real.startswith(os.path.realpath(d) + os.sep) for d in free):
        return  # Claude's own notes and scratch files need no plan
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "jev-gate: no approved plan for this request yet. Send the \"What you "
        "asked:\" and \"What I'm building:\" lists and end your message; Jev checks them when you stop, "
        "then you can build."}})


def on_stop(inp: dict) -> None:
    if inp.get("background_tasks") or any(not c.get("recurring") for c in inp.get("session_crons") or []):
        return  # paused on background work or a wakeup, not finished
    sid = inp.get("session_id") or ""
    s = load(sid)
    if not s.get("open") or not s.get("asks"):
        return
    if "last_assistant_message" not in inp:
        s["open"] = False
        save(sid, s)
        emit({"systemMessage": "jev-gate: this Claude Code version doesn't pass last_assistant_message to Stop "
                               "hooks, so there is nothing to review. Update Claude Code."})
        return
    reply = inp["last_assistant_message"] or ""
    if s.get("plan_turn") != s.get("turn"):
        asked, building = contract(reply)
        if asked and building:
            return check_plan(sid, s, asked, building, reply)
        if asked or building:
            return retry(sid, s, "plan check", ["the plan needs both lists"],
                         "Send \"What you asked:\" and \"What I'm building:\" together, then end your message.", reply)
    review(sid, s, reply)


def check_plan(sid: str, s: dict, asked: list, building: list, reply: str) -> None:
    asked, building = asked[:30], building[:30]
    frags = clauses("\n".join(s["asks"]))[:40]
    state = {"user_messages": s["asks"], "plan": {"asked": asked, "building": building}}
    qs = {f"a{i}": noul(COVERED, requirement=a) for i, a in enumerate(asked)}
    for j, f in enumerate(frags):
        qs[f"r{j}"], qs[f"c{j}"] = noul(REQUEST, fragment=f), noul(COVERED_FRAG, fragment=f)
    s["plan"] = {"asked": asked, "building": building}
    try:
        p = jev(state, qs)
    except Exception as e:  # noqa: BLE001 - fail open
        s["plan_turn"] = s["turn"]
        save(sid, s)
        emit({"systemMessage": f"jev-gate: plan not checked ({e}); letting Claude build.",
              "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": GO}})
        return
    gaps = [f"[asked] {a} (Jev {p[f'a{i}']:.2f})" for i, a in enumerate(asked) if p[f"a{i}"] < YES]
    gaps += [f'[your words] "{f}" (Jev {p[f"c{j}"]:.2f})' for j, f in enumerate(frags)
             if p[f"r{j}"] >= YES and p[f"c{j}"] < YES]
    log(sid, "plan", p, gaps)
    if gaps:
        return retry(sid, s, "plan check", gaps, PLAN_FAIL, reply)
    s["plan_turn"] = s["turn"]
    save(sid, s)
    emit({"systemMessage": f"jev-gate: Jev approved the plan ({len(asked)} asked, {len(building)} to build).",
          "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": GO}})


def review(sid: str, s: dict, reply: str) -> None:
    plan = s.get("plan") or {}
    items = [("asked", x) for x in plan.get("asked", [])] + [("promised", x) for x in plan.get("building", [])]
    frags = clauses("\n".join(s["asks"]))[:40]
    lines = clauses(reply, least=4, longest=10 ** 6)[-30:]
    state = {"user_messages": s["asks"], "plan": plan or "none stated",
             "final_reply": reply if len(reply) <= 16000 else "[start cut] " + reply[-16000:]}
    qs = {"wait": noul(WAITING)}
    for i, (_, x) in enumerate(items):
        qs[f"i{i}"] = noul(DONE, item=x)
    for j, f in enumerate(frags):
        qs[f"r{j}"], qs[f"d{j}"] = noul(REQUEST, fragment=f), noul(DONE_FRAG, fragment=f)
    for k, line in enumerate(lines):
        qs[f"l{k}"] = noul(LATER, sentence=line)
    try:
        p = jev(state, qs)
    except Exception as e:  # noqa: BLE001 - fail open
        s["open"] = False
        save(sid, s)
        emit({"systemMessage": f"jev-gate: review skipped ({e})."})
        return
    gaps = [f"[{kind}] {x} (Jev {p[f'i{i}']:.2f})" for i, (kind, x) in enumerate(items) if p[f"i{i}"] < YES]
    gaps += [f'[your words] "{f}" (Jev {p[f"d{j}"]:.2f})' for j, f in enumerate(frags)
             if p[f"r{j}"] >= YES and p[f"d{j}"] < YES]
    gaps += [f'[promised later] "{line}" (Jev {p[f"l{k}"]:.2f})' for k, line in enumerate(lines) if p[f"l{k}"] >= YES]
    log(sid, "review", p, gaps)
    if not gaps:
        s["open"] = False
        save(sid, s)
        after = f" after {s['rounds']} fix round(s)" if s.get("rounds") else ""
        emit({"systemMessage": f"jev-gate: PASS, Jev sees every asked and promised item delivered{after}."})
        return
    if p["wait"] >= WAIT:
        s.update(paused=True, paused_on=reply[-2000:])
        save(sid, s)
        emit({"systemMessage": f"jev-gate: paused for your answer. Still open:\n{listed(gaps)}"})
        return
    retry(sid, s, "review", gaps, REVIEW_FAIL, reply)


def main() -> None:
    global RAW
    if not active():
        return
    RAW = os.environ.get("JEV_GATE_INPUT")
    if RAW is None:
        RAW = sys.stdin.read()
    inp = json.loads(RAW or "{}")
    handler = {"prompt": on_prompt, "guard": on_guard, "stop": on_stop}.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if handler is None:
        sys.exit("usage: jev_gate.py prompt|guard|stop  (hook input JSON on stdin)")
    handler(inp)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - a gate bug must never break the session
        emit({"systemMessage": f"jev-gate error: {e!r}"})
