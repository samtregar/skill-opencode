---
name: opencode
description: Use when the user asks to run opencode (opencode run, opencode resume) or wants a review, second opinion, or analysis from another model through opencode or OpenRouter (e.g. "have Kimi review this diff", "ask Gemini via opencode", "get a second opinion from GPT")
---

# opencode Skill Guide

All commands below call the bundled helper by absolute path: `<skill-dir>` is the "Base directory for this skill" shown when this skill loaded. Run it from the user's project; do not `cd` into the skill directory.

## Choosing a Model
opencode can offer hundreds of models (an OpenRouter account alone adds several hundred), far too many to list or offer as options. Never list them all; ask instead. Skip this section when resuming a session, which keeps its model (see *Following Up*).

1. If the user already named a model in the request, go to step 3 with their words.
2. Run `python3 <skill-dir>/scripts/oc.py recent`. It prints up to 4 previously chosen models: this skill's own history first, then the opencode TUI's recents and favorites, each with its saved effort. Then ask with `AskUserQuestion`: one question, "Which model should do this? Pick a recent one, or choose Other and type any model name, optionally with an effort (e.g. `gpt astra high`)." Give one option per entry: label it with the name and effort (`Kimi K3.1 Pro @ max`, shortened if long) and use the `id:` line plus cost and context as the description.
   - If `recent` printed fewer than 2 entries, ask the same question in plain text instead, since `AskUserQuestion` needs two options.
   - Picking a listed entry settles the model: go to step 4, which keeps the entry's effort if it shows one.
3. For free text (typed under Other, or named in the request), run `python3 <skill-dir>/scripts/oc.py find <their words>`. It matches every word against model IDs and names, ignoring punctuation (`gpt5.5` finds `gpt-5.5`), and treats a trailing effort word (`... max`, `... high effort`) or `#effort` as the effort.
   - **One match**: use it, and name the resolved model in your next message.
   - **Several**: ask with `AskUserQuestion`, offering up to 4 in the script's order (exact matches, then newest), and say how many matched in total. Other lets the user refine.
   - **None**: show the closest partial matches the script printed and ask the user to retype.
   - Use only `id:` values the script printed. Never build a model ID yourself.
4. **Effort**: if the chosen entry shows `@ <effort>`, use it. Otherwise, if the model lists efforts, ask with `AskUserQuestion`, offering up to 4 of the listed ones with `high` first as (Recommended) when listed (otherwise the highest listed). Skip this question when the model has no effort settings. If the user wants the provider default, omit the effort.
5. The model argument is `provider/model#effort`, or `provider/model` without an effort.

## Running a Task
```bash
python3 <skill-dir>/scripts/oc.py run -m <provider/model#effort> "<prompt>"
python3 <skill-dir>/scripts/oc.py run -m <provider/model#effort> --prompt-file <file>
```
- **Review mode (default)** runs the `review` agent defined in `<skill-dir>/review-agent.json`. It has no edit tools, no subagents, and no MCP or Code Mode tools, and its file tools are limited to the project. Its shell rules allow only read-only `git` subcommands. The script tells the model about these limits in its first prompt. Disallowed calls are rejected automatically and the model carries on; the footer counts them.
  - **This is a guardrail against accidents, not a sandbox.** Glob rules on shell command text can't fully constrain a shell, and `webfetch` and `websearch` are allowed. Don't rely on review mode to contain a model you don't trust, or a run where the project or prompt contains untrusted text that could steer the model, such as someone else's branch.
- **`--mode build`** uses opencode's `build` agent, which can edit files and run any shell command. Use it only when the user wants opencode to make changes, and confirm with `AskUserQuestion` first unless they already said so.
- `-C <DIR>` runs in another directory; `-f <FILE>` attaches a file (repeatable); `--title` names the session (default: `Claude Code: ` plus the prompt's first line).
- When opencode can list its models, the script checks the model and effort before starting. A bad one exits with status 2 and a message, before anything reaches the provider. If the listing fails, the check is skipped and opencode reports any problem itself.
- **stdout is the model's final message**, trimmed of leading and trailing whitespace. Lines starting with `[opencode]` on stderr are the footer: session ID, model, outcome, steps, tool calls and rejections, tokens, cost, and a note when narration between tool calls was left out.
- Exit status: 0 succeeded with text; 1 the run failed, stopped early, added no turn to the session, or produced no text; 2 bad arguments. When no turn was added (e.g. a resume that failed before sending the prompt), nothing goes to stdout: an earlier answer is never printed as the new one.

### Writing the prompt
- The model sees the repository (opencode also loads its AGENTS.md or CLAUDE.md) but nothing from this conversation. State the goal, what to look at, what to check, and the answer format you want (e.g. "findings with file:line and severity; no rewrites").
- Point it at the change rather than pasting it: uncommitted work (`git diff`, `git diff --staged`), a commit (`git show <sha>`), a branch (`git diff master...HEAD`), or named files. Paste a diff only when it is small or the model handles tools poorly.
- If the prompt contains quotes, backticks or `$`, or runs long, write it with the Write tool to a file in the scratchpad and pass `--prompt-file`. A single command-line argument can't exceed 128 KB.

### Timing
Runs take minutes: a commit review on a fast model at `high` took about 3. Run in the foreground with a Bash `timeout` of 600000 by default. For slow or large models, `xhigh`/`max` efforts, or big reviews, use `run_in_background` and wait for the completion notice; launching is not finishing. If the script is killed, it stops opencode too. The transcript keeps whatever was produced, and `show` recovers it.

### Presenting the result
- **Show the model's output to the user verbatim** after every run, resumes included. Tool output is collapsed in the user's view, so this is how they see what the model said. Under a `### <Model name>'s response` heading, reproduce stdout exactly as written: no summarizing, trimming, reordering, or rewording. Follow it with one italic line from the footer: model, effort, cost and session ID. Then, under a separate `### Claude's take` heading, give your own assessment (see *Critical Evaluation*). Skip the verbatim copy only if the user asked for just a summary.
- If the captured output was truncated (Claude Code marks long tool output as truncated) or a background run's output file is empty, recover it with `python3 <skill-dir>/scripts/oc.py show <session> > <scratchpad>/opencode-<session>.md`. Then read that file in parts with the Read tool, so the recovery can't be cut off too. Never present a truncated capture as complete.
- If the response is too long to reproduce in one reply, tell the user its length and where the full text is saved, and ask whether to paste it in full or in parts.
- If the run failed or produced partial output, show what it produced verbatim, say plainly that it is incomplete, then follow *Error Handling*.
- Afterwards tell the user: "You can continue this opencode session by saying 'opencode resume' or asking me to follow up."

## Following Up
- `python3 <skill-dir>/scripts/oc.py run --continue "<prompt>"` resumes this skill's latest session in the current directory, in the same mode. `--session <id>` resumes a specific one (IDs come from footers or `oc.py sessions`). A resumed session keeps its model and effort; pass `-m` only when the user asks to switch.
- After a run, use `AskUserQuestion` to confirm next steps, collect clarifications, or decide whether to resume. Restate the model, effort and mode when proposing follow-ups.
- If the first turn of a session failed (e.g. a provider error), start a new session instead of resuming it. The failed prompt stays in the transcript and would be sent again with the next one.

## Viewing Past Output
```bash
python3 <skill-dir>/scripts/oc.py sessions [--all-dirs]   # sessions this skill started, newest first
python3 <skill-dir>/scripts/oc.py show [<session>]        # final response of the latest turn (default: this skill's latest session here)
python3 <skill-dir>/scripts/oc.py show [<session>] --all  # every turn: prompt, then all the model's text, narration included
```
`opencode session list` shows every opencode session in the project, including ones from the TUI; `show` accepts those IDs too. Show recovered output verbatim, as in *Presenting the result*.

## Critical Evaluation
These models range from frontier to small and fast, with their own knowledge cutoffs and blind spots. Treat the reviewer as a **colleague, not an authority**.
- Check its claims against the code before relaying them as fact; file:line references and quoted code can be wrong.
- Trust your own knowledge when confident, and research disagreements (docs, WebSearch) before accepting a claim.
- To discuss a disagreement, resume the session and **identify yourself as Claude**, using your actual model name: `oc.py run --continue "This is Claude (<your model name>) following up. I disagree with [X] because [evidence]. What's your take?"`. Frame it as a discussion; either side could be wrong.
- Let the user decide when the disagreement is genuinely ambiguous.

## Error Handling
- **Exit 2**: fix the arguments. For a model or effort problem, rerun `find` and pick again with the user.
- **Exit 1 with `[opencode] error:`** (e.g. `provider.no-route`, exhausted credits, rate limits): show the error and ask the user how to proceed, such as another model or effort, or a retry. Don't switch models on your own.
- Rejected tool calls are normal in review mode. Mention them only if they likely hurt the result, e.g. the reviewer couldn't see the change; then inline the diff in a follow-up.
- If `opencode` is missing or failing outright, report it and stop.

## Maintenance Notes
- Model history lives in `~/.local/state/claude-opencode-skill/models.json` and run records in `runs.jsonl` next to it; edit or delete them to forget models. `recent` drops models opencode no longer offers.
- The review agent only exists on a private server: `run` passes `review-agent.json` through `OPENCODE_CONFIG_CONTENT` together with `--standalone`, because the background service never reads that variable. Permission rules are globs on each command of a shell line (opencode splits `;`, `&&`, pipes and `$(...)`), and the last matching rule wins.
- opencode takes its project directory from `$PWD`, not the process's working directory, so `run` sets `PWD` for `-C`.
- The review agent's rules were tested on opencode 2.1.4 by having a model try to get around them. Not every attempt was blocked, which is why review mode is documented as a guardrail. Re-test after an opencode upgrade.
