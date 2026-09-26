# jev-gate

A Claude Code plugin that makes Claude build what you asked, all of it, with [Jev](https://typesafe.ai) as the referee.

Every time you ask Claude Code for something:

1. Claude replies with **What you asked** and **What I'm building**.
2. Jev, TypeSafe's judgment model, checks that plan against your request, clause by clause. Only a plan that covers everything gets built; file edits stay blocked until then.
3. Claude builds it and ends with an item-by-item summary.
4. Jev reviews that summary. Anything you asked for that isn't shown as delivered, anything Claude promised and didn't deliver, and any "I'll do that later" sends the task back to Claude with the missing items quoted. This repeats until Jev passes it (5 rounds at most, fewer if Claude's reply stops changing; then you decide).

## Install

You need Python 3 on your `PATH` and a TypeSafe API key from [console.typesafe.ai](https://console.typesafe.ai).

```bash
claude plugin marketplace add Neoo-Blue/jev-gate
claude plugin install jev-gate@jev-gate
export TYPESAFE_API_KEY=...   # in the shell or profile that launches Claude Code
```

Or inside Claude Code: `/plugin marketplace add Neoo-Blue/jev-gate`, then `/plugin install jev-gate@jev-gate`. New sessions pick it up.

## How it works

Jev doesn't write text. It answers typed yes/no questions with calibrated probabilities. So the plugin cuts your message, Claude's plan and Claude's summary into items, asks Jev one narrow question per item in a single request (about 0.2 s for 50 questions, a tiny fraction of a cent), and quotes back the items that fail.

| Hook | What it does |
| --- | --- |
| `UserPromptSubmit` | Records your request and reminds Claude of the protocol. |
| `PreToolUse` (Edit, Write, NotebookEdit) | Blocks file edits until Jev has approved this request's plan. Claude's own notes under `~/.claude` and temp files are exempt. |
| `Stop` | A "What you asked / What I'm building" message gets a plan check. Any other final message gets the review. A failed check blocks the stop and hands Claude the gaps. |

The review asks Jev, for every item:

- each "What you asked" and "What I'm building" bullet: does the final message show all of it done?
- each clause of your own message: is it a request you still want, and is it shown done? This catches a plan that quietly left part of your message out of its "What you asked" list.
- each line of the summary: does it commit Claude to later work, or admit that something you asked for is unfinished? Notes about extras you never asked for ("skipped: X, add when Y") pass.
- the whole reply: is it waiting on something only you can give (a decision, a credential)? Then the gate pauses instead of looping, and the open request is carried into your next message.

The skill in `skills/jev-gate/SKILL.md` spells out the protocol for Claude.

## Settings

| Env var | Default | Effect |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | none | Required for Jev calls. |
| `JEV_GATE` | unset | `off` disables the gate. `on` forces it in `claude -p` and Agent SDK runs, which are skipped by default so scripts and automations aren't gated. |
| `JEV_GATE_MAX_ROUNDS` | `5` | Fix rounds per request before the gate lets the stop through and shows you what is still open. It also lets the stop through as soon as Claude's final message comes back unchanged. |

Each decision is logged, with Jev's probabilities, to `~/.claude/jev-gate/log.jsonl`.

## Limits

- Jev reads the summary, not the code. The gate keeps Claude honest about scope, not about correctness; keep your tests and code review.
- The edit guard covers Edit, Write and NotebookEdit. Changes made through Bash aren't blocked, though the review still covers what they were for.
- Jev is trained mainly on English. Other languages work, with lower accuracy.
- A request whose own instructions rule out a full summary (a one-line reply, say) can't pass. The gate notices when Claude's reply stops changing and hands the open items to you.
- It fails open: a missing key, network trouble or an API error never blocks you; you get a warning instead.
- Your prompts, Claude's plan and its final summary are sent to TypeSafe's API (`api.typesafe.ai`) to be judged.

## Test

```bash
python3 scripts/test_jev_gate.py   # offline; a fake Jev drives the whole loop
```

## License

MIT
