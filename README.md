# opencode Integration for Claude Code

A Claude Code skill that sends reviews and other prompts to any model [opencode](https://opencode.ai) can reach, then shows you that model's response verbatim alongside Claude's own assessment.

Ask Claude for "a review via opencode", "have Kimi review the last commit", or "a second opinion from Gemini". You can also type `/skill-opencode:opencode` (plugin install) or `/opencode` (standalone install).

## Why

opencode connected to an OpenRouter account offers hundreds of models, far too many for Claude to list or offer as options. This skill:

- **Offers models you used before.** It lists up to 4 of them, each with its saved reasoning effort, price and context size. They come from this skill's own history first, then opencode's recent and favorite models.
- **Takes free text for anything else.** `gpt5.5` finds `gpt-5.5`, and a trailing effort works too (`kimi k3.1 pro max`, `gemini argon high effort`). One match is used directly; several are offered newest first; none shows the closest partial matches.
- **Asks for an effort** when needed, offering only the efforts the chosen model supports.
- **Checks the model and effort** against opencode before anything is sent to the provider.

## Prerequisites

- [opencode](https://opencode.ai) installed and on `PATH`, with your providers configured. Developed and tested with opencode 2.1.4.
- Python 3, standard library only, for the bundled helper script.

## Installation

### Option 1: Plugin (recommended)

```
/plugin marketplace add samtregar/skill-opencode
/plugin install skill-opencode@skill-opencode
```

### Option 2: Standalone skill

```
git clone --depth 1 https://github.com/samtregar/skill-opencode.git /tmp/skill-opencode && \
mkdir -p ~/.claude/skills && \
cp -r /tmp/skill-opencode/plugins/skill-opencode/skills/opencode ~/.claude/skills/opencode && \
rm -rf /tmp/skill-opencode
```

## Usage

**You:** `Have opencode review the last commit.`

Claude will:

1. Ask which model to use, offering your recent ones plus free text, and then which effort if the model has effort settings.
2. Write a prompt that points the reviewer at the change, for example `git show HEAD`, and run it through opencode. Long runs go in the background.
3. Show the model's final response verbatim, followed by a footer line (model, effort, cost, session ID) and then Claude's own take. Claude checks the reviewer's claims against the code rather than relaying them as fact.

Follow-ups resume the same opencode session, which keeps its model and effort: "opencode resume", or "ask it about finding 3".

### Review mode and build mode

- **Review mode (default)** runs a custom opencode agent (`review-agent.json`) that has no edit tools, no subagents and no MCP tools, keeps file tools inside the project, and limits the shell to read-only `git` commands. **It is a guardrail against accidents, not a sandbox.** Glob rules on shell commands can't fully constrain a shell, and web fetch and search stay available. Don't rely on it to contain a model you don't trust, or for runs where the project or prompt contains untrusted text that could steer the model.
- **Build mode** uses opencode's standard `build` agent, which can edit files and run commands. Claude asks before using it unless you've already said to.

Each run starts a private opencode server (`opencode run --standalone`), so the review agent never touches your background opencode service. Startup adds about 2 seconds.

### The helper script

Claude drives everything through `scripts/oc.py`, which you can also run yourself:

```
oc.py recent                       # previously chosen models
oc.py find kimi k3.1 pro max       # match free text against every model
oc.py run -m provider/model#effort "prompt"   # or --prompt-file FILE, or - for stdin
oc.py run --continue "follow-up"   # resume this skill's latest session here
oc.py sessions [--all-dirs]        # sessions this skill started
oc.py show [SESSION] [--all]       # reprint a response from opencode's transcript
```

`run` prints only the model's final message on stdout, and a footer on stderr: session, model, outcome, steps, tool calls, tokens and cost. It exits with 0 on success, 1 if the run failed or produced nothing, and 2 for bad arguments. If the script is stopped (e.g. a Claude Code timeout), it stops opencode too.

Model history and run records are kept in `~/.local/state/claude-opencode-skill/` (or under `$XDG_STATE_HOME`). Delete them to start fresh.

## Notes

- The helper never prints opencode's raw model records, which include provider settings such as API keys.
- opencode 2.1.4 can cut off large output written to a pipe at exactly 256 KiB, so the helper reads opencode's JSON output through temporary files.

## License

MIT
