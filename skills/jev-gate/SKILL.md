---
name: jev-gate
description: Requirement gate that runs on every Claude Code request. Jev, TypeSafe's judgment model, checks that your plan covers everything the user asked before you build, and that your final summary shows all of it delivered before you can stop; any gap sends you back to finish. Hooks run it automatically; load this for the exact message formats and how to handle a failed check.
---

# jev-gate

jev-gate holds every request to what the user actually asked. Hooks drive it; this is the protocol they expect.

## The loop

1. **Request arrives.** The prompt hook records it and reminds you of this protocol.
2. **Plan.** If anything will be built, changed or run, explore as much as you need, then send one message with both lists and end your turn there:

   ```
   **What you asked:**
   - every part of the request, in the user's terms: features, constraints, conditions, follow-up steps such as "publish it"
   **What I'm building:**
   - each concrete deliverable: files, commands, repos, settings
   ```

   Use exactly these two headings. One requirement or deliverable per bullet; don't fold two into one.
3. **Plan check.** When you stop, Jev checks each "What you asked" item, and each clause of the user's own message, against "What I'm building". Approved: you are told to build. Not approved: the uncovered items come back quoted; fix the lists and stop again. Edit, Write and NotebookEdit are blocked until approval (except under `~/.claude` and temp dirs). Don't route around that with Bash.
4. **Build all of it.**
5. **Summary.** End with an item-by-item summary: each asked and promised item, what was done, and the evidence (paths, commands and their results, URLs). Report failures as failures.
6. **Review.** Jev checks that the final message shows every asked item, every promised item and every clause of the user's message done, and that no line promises later work or admits requested work unfinished. Any gap blocks the stop and hands you the list. Finish those items, then write the whole summary again: the review reads only your final message.
7. After 5 failed rounds per request (`JEV_GATE_MAX_ROUNDS`) the stop goes through and the user sees what is still open.

## Skip the plan when

The request is a question, an explanation or chat. Just answer; the review still checks that you answered what was asked.

## Blocked on the user

If an item truly needs something only the user has (a decision, credentials, access, a physical step), ask for it plainly and stop. Jev recognizes a reply that is waiting on the user and pauses instead of looping; the unfinished request carries over into the user's next message and is judged together with it.

## Passing honestly

- Jev judges the text of your final message, not the code. A pass is only as true as the summary, so never claim what you didn't do.
- If Jev flags something that is done, the summary didn't show it: add the concrete evidence.
- Notes like "skipped: X, add when Y" about extras the user never asked for are fine, and so are offers ("want me to...?"). Promises are not: do the thing now, or don't promise it.

## Switches

- `JEV_GATE=off` disables the gate; `JEV_GATE=on` forces it in `claude -p` and SDK runs, where it is skipped by default.
- `JEV_GATE_MAX_ROUNDS` caps the fix rounds per request (default 5).
- Needs `TYPESAFE_API_KEY`. Without it, or when Jev is unreachable, the gate shows a warning and steps aside.
- Every decision is logged with Jev's probabilities to `~/.claude/jev-gate/log.jsonl`.
