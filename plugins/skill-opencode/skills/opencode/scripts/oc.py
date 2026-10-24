#!/usr/bin/env python3
"""Helper for the opencode skill: pick a model, run opencode, and read results back.

  recent    models chosen before: this skill's history, then opencode's own recents
  find      match free text against every model opencode offers
  run       send a prompt through `opencode run` and print the final response
  show      reprint a session's response from opencode's saved transcript
  sessions  list sessions this skill started

Nothing here prints raw model records: opencode includes provider settings,
API keys among them, in its model listings.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_AGENT_CONFIG = os.path.join(SKILL_DIR, "review-agent.json")
XDG_STATE = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
STATE_DIR = os.path.join(XDG_STATE, "claude-opencode-skill")
HISTORY_FILE = os.path.join(STATE_DIR, "models.json")
RUNS_FILE = os.path.join(STATE_DIR, "runs.jsonl")
OPENCODE_MODEL_STATE = os.path.join(XDG_STATE, "opencode", "model.json")

EFFORT_WORDS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "thinking"}

# Prepended to the first prompt of a review session, so the reviewer doesn't
# spend turns discovering the limits of review-agent.json by trial and error.
REVIEW_PREAMBLE = (
    "[Note from the harness running you: this is a read-only review session. You have no tools "
    "that edit files, and file access is limited to this project directory. The shell runs only "
    "read-only git commands (git diff, log, show, status, blame, grep, ls-files, ls-tree, "
    "cat-file, rev-parse, rev-list, merge-base, describe, shortlog) on paths inside the project; "
    "anything else is rejected automatically, including pipes into other programs, output "
    "redirection (even 2>&1), absolute paths and ../ paths. Use the read, grep and glob tools "
    "for files. Nobody can answer questions during the run, so state any assumptions in your "
    "answer.]"
)


def die(message, code=2):
    print(f"oc.py: {message}", file=sys.stderr)
    sys.exit(code)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def opencode_json(args, timeout=120):
    # Capture through a file, not a pipe: opencode 2.1 exits before a large write
    # to a pipe drains, which cut `api model.list` off at exactly 256 KiB.
    try:
        with tempfile.TemporaryFile(mode="w+") as out:
            subprocess.run(["opencode", *args], stdout=out, stderr=subprocess.DEVNULL, timeout=timeout)
            out.seek(0)
            return json.load(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ---- models ----------------------------------------------------------------

def load_models():
    """Return {"provider/model": record} for every model opencode offers, or None."""
    listing = opencode_json(["api", "model.list"], timeout=60)
    if isinstance(listing, dict) and isinstance(listing.get("data"), list):
        return {f"{m['providerID']}/{m['id']}": m for m in listing["data"]}
    # Older or unreachable API: IDs only, so efforts are unknown (no "variants" key).
    try:
        proc = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    models = {}
    for line in proc.stdout.split():
        provider, _, model = line.partition("/")
        if model:
            models[line] = {"providerID": provider, "id": model, "name": model}
    return models or None


def require_models():
    models = load_models()
    if models is None:
        die("could not list opencode's models (is `opencode` installed and working?)", 1)
    return models


def split_spec(spec):
    """'provider/model#effort' -> ('provider/model', 'effort' or None)."""
    model, _, variant = spec.partition("#")
    return model, (variant or None)


def efforts(m):
    """The model's effort variants, [] if it has none, None if unknown."""
    variants = m.get("variants")
    return None if variants is None else [v["id"] for v in variants]


def fmt_count(n):
    if n >= 1_000_000:
        return f"{n // 1_048_576}M" if n % 1_048_576 == 0 else f"{n / 1_000_000:.3g}M"
    return f"{round(n / 1000)}K" if n >= 1000 else str(n)


def model_summary(m):
    parts = []
    cost = (m.get("cost") or [None])[0]
    if cost:
        if cost.get("input") or cost.get("output"):
            parts.append(f"${cost.get('input', 0):g} in / ${cost.get('output', 0):g} out per 1M tokens")
        else:
            parts.append("free")
    context = (m.get("limit") or {}).get("context")
    if context:
        parts.append(f"{fmt_count(context)} context")
    e = efforts(m)
    if e is not None:
        parts.append("efforts: " + ", ".join(e) if e else "no effort settings")
    released = (m.get("time") or {}).get("released")
    if released:
        parts.append("released " + time.strftime("%Y-%m-%d", time.gmtime(released / 1000)))
    return " · ".join(parts)


def print_entry(i, key, m, variant=None, note=None):
    print(f"{i}. {m.get('name') or key}" + (f" @ {variant}" if variant else ""))
    print(f"   id: {key}" + (f"#{variant}" if variant else ""))
    summary = model_summary(m)
    if summary:
        print(f"   {summary}")
    if note:
        print(f"   {note}")


# ---- history ---------------------------------------------------------------

def remember(model, variant):
    history = load_json(HISTORY_FILE, {}).get("models", [])
    now = time.time()
    for h in history:
        if h["model"] == model and h.get("variant") == variant:
            h["last_used"] = now
            h["count"] = h.get("count", 0) + 1
            break
    else:
        history.append({"model": model, "variant": variant, "last_used": now, "count": 1})
    history.sort(key=lambda h: h["last_used"], reverse=True)
    write_json(HISTORY_FILE, {"models": history[:50]})


def cmd_recent(args):
    models = load_models()
    entries, seen = [], set()

    def add(model, variant, note):
        if (model, variant) not in seen:
            seen.add((model, variant))
            entries.append((model, variant, note))

    for h in load_json(HISTORY_FILE, {}).get("models", []):
        runs = h.get("count", 1)
        when = time.strftime("%Y-%m-%d", time.localtime(h["last_used"]))
        add(h["model"], h.get("variant"), f"last used {when} with this skill ({plural(runs, 'run')})")
    tui = load_json(OPENCODE_MODEL_STATE, {})
    tui_variants = tui.get("variant") or {}
    for source, note in (("recent", "recent in the opencode TUI"), ("favorite", "opencode TUI favorite")):
        for r in tui.get(source) or []:
            if isinstance(r, dict) and r.get("providerID") and r.get("modelID"):
                key = f"{r['providerID']}/{r['modelID']}"
                add(key, tui_variants.get(key), note)

    shown, dropped = [], []
    for model, variant, note in entries:
        if models is not None and model not in models:
            dropped.append(model)
            continue
        m = (models or {}).get(model, {"name": model})
        e = efforts(m)
        if variant and e is not None and variant not in e:
            note += f"; saved effort '{variant}' is no longer offered"
            variant = None
        shown.append((model, variant, note, m))
    if len(shown) < 2 and models is not None:
        default = opencode_json(["api", "model.default"], timeout=60)
        data = default.get("data") if isinstance(default, dict) else None
        if isinstance(data, dict) and data.get("providerID") and data.get("id"):
            key = f"{data['providerID']}/{data['id']}"
            if key in models and all(s[0] != key for s in shown):
                shown.append((key, None, "opencode's configured default model", models[key]))

    if not shown:
        print("No previously chosen models.")
    for i, (model, variant, note, m) in enumerate(shown[: args.limit], 1):
        print_entry(i, model, m, variant, note)
    if dropped:
        print(f"Skipped, no longer offered by opencode: {', '.join(sorted(set(dropped)))}")
    if models is None:
        print("(Could not list opencode's models, so these were not checked against it.)")


# ---- find ------------------------------------------------------------------

def compact(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def match(tokens, models):
    """Keys whose ID or display name contains every token (ignoring punctuation)."""
    hits = []
    for key, m in models.items():
        hay = f"{key} {m.get('name') or ''}".lower()
        hay_compact = compact(hay)
        if all(t in hay or (compact(t) and compact(t) in hay_compact) for t in tokens):
            hits.append(key)
    return hits


def released(m):
    return (m.get("time") or {}).get("released") or 0


def cmd_find(args):
    models = require_models()
    query = " ".join(args.query).strip()
    variant = None
    explicit = re.match(r"^(.*?)\s*[#@]\s*([A-Za-z0-9_-]+)$", query)
    if explicit:
        query, variant = explicit.group(1), explicit.group(2).lower()
    q = query.lower()

    hits = [k for k in models if q in (k.lower(), str(models[k].get("id", "")).lower())]
    tokens = [t for t in re.split(r"[\s,]+", q) if t]
    if not hits:
        hits = match(tokens, models)
    if not hits and variant is None:
        # A trailing effort word: "kimi k3.1 pro max", "gemini argon high effort".
        words = tokens[:]
        while len(words) > 1 and words[-1] in ("effort", "reasoning"):
            words.pop()
        known = EFFORT_WORDS | {e for m in models.values() for e in (efforts(m) or [])}
        if len(words) > 1 and words[-1] in known:
            hits = match(words[:-1], models)
            if hits:
                variant = words[-1]
                tokens = words[:-1]

    if not hits:
        scored = []
        for key, m in models.items():
            hay = f"{key} {m.get('name') or ''}".lower()
            n = sum(1 for t in tokens if t in hay or (compact(t) and compact(t) in compact(hay)))
            if n:
                scored.append((-n, -released(m), key))
        print(f'No model matches all of "{query}".')
        if scored:
            print("Closest partial matches:")
            for i, (_, _, key) in enumerate(sorted(scored)[:5], 1):
                print_entry(i, key, models[key])
        return 1

    def rank(key):
        m = models[key]
        exact = q in (key.lower(), str(m.get("id", "")).lower(), str(m.get("name") or "").lower())
        return (not exact, -released(m), key)

    hits.sort(key=rank)
    more = f", showing the first {args.limit}" if len(hits) > args.limit else ""
    effort = f" at effort '{variant}'" if variant else ""
    count = "1 model matches" if len(hits) == 1 else f"{len(hits)} models match"
    print(f'{count} "{query}"{effort}{more}:')
    for i, key in enumerate(hits[: args.limit], 1):
        m = models[key]
        e = efforts(m)
        if variant and e is not None and variant not in e:
            print_entry(i, key, m, None, f"effort '{variant}' is not offered for this model")
        else:
            print_entry(i, key, m, variant)
    return 0


# ---- transcripts -----------------------------------------------------------

def export_session(session_id):
    data = opencode_json(["session", "export", session_id])
    return data if isinstance(data, dict) and "messages" in data else None


def split_turns(export):
    """Each user message with the messages that follow it, up to the next one."""
    turns = []
    for msg in export.get("messages", []):
        if msg.get("type") == "user":
            turns.append({"prompt": msg.get("text") or "", "created": (msg.get("time") or {}).get("created"), "messages": []})
        elif turns:
            turns[-1]["messages"].append(msg)
    return turns


def message_text(msg):
    return "\n\n".join(
        c["text"].strip() for c in msg.get("content", []) if c.get("type") == "text" and (c.get("text") or "").strip()
    )


def model_label(model):
    if not model:
        return "unknown model"
    label = f"{model.get('providerID')}/{model.get('id')}"
    variant = model.get("variant")
    return label + (f"#{variant}" if variant and variant != "default" else "")


def summarize_turn(turn):
    assistants = [m for m in turn["messages"] if m.get("type") == "assistant"]
    texts = [t for t in (message_text(m) for m in assistants) if t]
    idle = [m for m in turn["messages"] if m.get("type") == "idle"]
    tools = [c for m in assistants for c in m.get("content", []) if c.get("type") == "tool"]
    rejected = 0
    for c in tools:
        error = (c.get("state") or {}).get("error")
        kind = error.get("type", "") if isinstance(error, dict) else str(error or "")
        rejected += "permission" in kind.lower()
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cached": 0}
    for m in assistants:
        t = m.get("tokens") or {}
        tokens["input"] += t.get("input") or 0
        tokens["output"] += t.get("output") or 0
        tokens["reasoning"] += t.get("reasoning") or 0
        tokens["cached"] += (t.get("cache") or {}).get("read") or 0
    return {
        "final": texts[-1] if texts else "",
        "all_text": "\n\n".join(texts),
        "narration": texts[:-1],
        "outcome": idle[-1].get("outcome", "unknown") if idle else "incomplete",
        "error": idle[-1].get("error") if idle else None,
        "steps": len(assistants),
        "tools": len(tools),
        "rejected": rejected,
        "tokens": tokens,
        "cost": sum(m.get("cost") or 0 for m in assistants),
        "model": assistants[-1].get("model") if assistants else None,
        "agent": assistants[-1].get("agent") if assistants else None,
    }


def plural(n, noun):
    return f"{n} {noun}" + ("" if n == 1 else "s")


def print_footer(session_id, info, turn, show_hint=True):
    say = lambda line: print(f"[opencode] {line}", file=sys.stderr)
    agent = turn["agent"] or info.get("agent") or "?"
    say(f"session {session_id} · {model_label(turn['model'] or info.get('model'))} · agent {agent} · {turn['outcome']}")
    t = turn["tokens"]
    say(
        f"this turn: {plural(turn['steps'], 'step')}, {plural(turn['tools'], 'tool call')} ({turn['rejected']} rejected) · "
        f"{t['input']:,} input + {t['cached']:,} cached / {t['output']:,} output / {t['reasoning']:,} reasoning tokens · "
        f"${turn['cost']:.4f} (session total ${info.get('cost') or 0:.4f})"
    )
    if turn["narration"] and show_hint:
        chars = sum(len(n) for n in turn["narration"])
        say(f"{plural(len(turn['narration']), 'earlier message')} in this turn ({chars:,} chars) not shown; `show {session_id} --all` prints them")
    if turn["error"]:
        error = turn["error"]
        say("error: " + (f"{error.get('type')}: {error.get('message')}" if isinstance(error, dict) else str(error)))


# ---- runs ------------------------------------------------------------------

def load_runs():
    """Sessions this skill started, oldest first, one merged record per session."""
    merged = {}
    try:
        with open(RUNS_FILE) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                merged.setdefault(r["session"], {}).update(r)
    except OSError:
        pass
    return sorted(merged.values(), key=lambda r: r.get("updated", 0))


def log_run(record):
    os.makedirs(STATE_DIR, exist_ok=True)
    # Records merge per session, so a field this record doesn't know stays as it was.
    record = {k: v for k, v in record.items() if v is not None}
    with open(RUNS_FILE, "a") as f:
        f.write(json.dumps({**record, "updated": time.time()}) + "\n")


def review_config(existing):
    with open(REVIEW_AGENT_CONFIG) as f:
        config = json.load(f)
    if existing:
        try:
            base = json.loads(existing)
        except ValueError:
            return json.dumps(config)
        base.setdefault("agent", {}).update(config["agent"])
        config = base
    return json.dumps(config)


def cmd_run(args):
    cwd = os.path.abspath(args.cd or os.getcwd())
    runs = load_runs()
    session = args.session
    previous = None
    if args.continue_:
        previous = next((r for r in reversed(runs) if r.get("cwd") == cwd), None)
        if not previous:
            die(f"no earlier session from this skill in {cwd}; start one with --model")
        session = previous["session"]
    elif session:
        previous = next((r for r in runs if r["session"] == session), None)
    mode = args.mode or (previous or {}).get("mode") or "review"

    model, variant = split_spec(args.model) if args.model else (None, None)
    if not session and not model:
        die("a new session needs --model provider/model[#effort]")
    if model:
        models = load_models()
        if models is not None:
            if model not in models:
                die(f"opencode does not offer {model}; look it up with `oc.py find`")
            offered = efforts(models[model])
            if variant and offered is not None and variant not in offered:
                die(f"{model} has no effort '{variant}'; offered: {', '.join(offered) or 'none'}")

    if args.prompt_file:
        with open(args.prompt_file) as f:
            prompt = f.read()
    elif args.prompt == "-":
        prompt = sys.stdin.read()
    elif args.prompt:
        prompt = args.prompt
    else:
        die("give the prompt as an argument, with --prompt-file, or as - to read stdin")
    if not prompt.strip():
        die("the prompt is empty")
    title = args.title or "Claude Code: " + prompt.strip().splitlines()[0][:60]
    if mode == "review" and not session:
        prompt = REVIEW_PREAMBLE + "\n\n" + prompt

    env = os.environ.copy()
    # opencode takes its project directory from $PWD, not the process's cwd.
    env["PWD"] = cwd
    if mode == "review":
        env["OPENCODE_CONFIG_CONTENT"] = review_config(env.get("OPENCODE_CONFIG_CONTENT"))
    # --standalone: the background service never sees OPENCODE_CONFIG_CONTENT,
    # so the review agent only exists on a private server.
    cmd = ["opencode", "run", "--standalone", "--format", "json", "--agent", "review" if mode == "review" else "build"]
    if model:
        cmd += ["-m", f"{model}#{variant}" if variant else model]
    if session:
        cmd += ["-s", session]
    else:
        cmd += ["--title", title]
    for path in args.file or []:
        cmd += ["-f", path]

    # A resumed session already has turns; the run has to add one before its output counts.
    before = export_session(session) if session else None
    previous_turns = len(split_turns(before)) if before else 0

    stderr = tempfile.TemporaryFile(mode="w+")
    # Make a Bash-tool timeout (SIGTERM) or a closed terminal (SIGHUP) stop opencode too,
    # rather than orphaning a paid run. Installed before opencode starts, so there is no gap.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, _: sys.exit(128 + signum))
    proc = None
    session_id, errors, texts = session, [], {}
    logged = False
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)

        def feed():
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threading.Thread(target=feed, daemon=True).start()
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not session_id and event.get("sessionID"):
                session_id = event["sessionID"]
            if session_id and not logged:
                log_run({"session": session_id, "cwd": cwd, "mode": mode, "title": title if not session else None,
                         "model": model and (f"{model}#{variant}" if variant else model), "outcome": "running"})
                logged = True
            if event.get("type") == "error":
                errors.append(event.get("error"))
            elif event.get("type") == "text":
                part = event.get("part") or {}
                texts.setdefault(part.get("messageID"), []).append(part.get("text") or "")
        proc.wait()
    except BaseException:
        if logged:
            log_run({"session": session_id, "outcome": "interrupted"})
        raise
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    stderr.seek(0)
    stderr_text = re.sub(r"\x1b\[[0-9;]*m", "", stderr.read())

    def fail(message):
        sys.stderr.write(stderr_text)
        for error in errors:
            print(f"[opencode] error: {error}", file=sys.stderr)
        die(message, 1)

    if not session_id:
        fail(f"opencode exited with status {proc.returncode} before starting a session")

    export = export_session(session_id)
    if export is None:
        # Fall back to what streamed past: the last message that had text.
        final = "\n".join(list(texts.values())[-1]).strip() if texts else ""
        print(final)
        print(f"[opencode] session {session_id} · exit status {proc.returncode} · transcript export failed, stats unavailable", file=sys.stderr)
        for error in errors:
            print(f"[opencode] error: {error}", file=sys.stderr)
        log_run({"session": session_id, "outcome": "unknown (transcript export failed)"})
        return 0 if proc.returncode == 0 and final else 1

    turns = split_turns(export)
    if len(turns) <= previous_turns:
        # Without a new turn, the last one in the transcript is an earlier answer.
        if logged:
            log_run({"session": session_id, "outcome": "failed"})
        fail(f"opencode exited with status {proc.returncode} without adding a turn to session {session_id}")

    info = export.get("info") or {}
    turn = summarize_turn(turns[-1])
    print(turn["final"])
    sys.stdout.flush()
    print_footer(session_id, info, turn)
    if not turn["final"]:
        print("[opencode] warning: the model produced no text in this turn", file=sys.stderr)
    if proc.returncode:
        print(f"[opencode] warning: opencode exited with status {proc.returncode}", file=sys.stderr)
    log_run({"session": session_id, "cwd": cwd, "mode": mode, "model": model_label(turn["model"] or info.get("model")),
             "outcome": turn["outcome"]})
    if turn["outcome"] == "succeeded" and turn["model"]:
        used = turn["model"]
        variant = used.get("variant")
        remember(f"{used['providerID']}/{used['id']}", variant if variant and variant != "default" else None)
    return 0 if turn["outcome"] == "succeeded" and turn["final"] and proc.returncode == 0 else 1


# ---- show / sessions ---------------------------------------------------------

def cmd_show(args):
    session_id = args.session
    if not session_id:
        cwd = os.path.abspath(args.cd or os.getcwd())
        last = next((r for r in reversed(load_runs()) if r.get("cwd") == cwd), None)
        if not last:
            die(f"no session from this skill in {cwd}; pass a session ID (see `sessions` or `opencode session list`)")
        session_id = last["session"]
    export = export_session(session_id)
    if export is None:
        die(f"could not export session {session_id}", 1)
    turns = split_turns(export)
    if not turns:
        die(f"session {session_id} has no prompts", 1)
    if args.all:
        for i, t in enumerate(turns, 1):
            s = summarize_turn(t)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created"] / 1000)) if t["created"] else "?"
            prompt = t["prompt"].replace(REVIEW_PREAMBLE, "").strip()
            print(f"=== Turn {i} of {len(turns)} · {when} · {model_label(s['model'])} · {s['outcome']} ===")
            print("Prompt: " + (prompt[:300] + " [...]" if len(prompt) > 300 else prompt))
            print()
            print(s["all_text"] or "(no text)")
            print()
    else:
        print(summarize_turn(turns[-1])["final"])
    sys.stdout.flush()
    print_footer(session_id, export.get("info") or {}, summarize_turn(turns[-1]), show_hint=not args.all)
    return 0


def cmd_sessions(args):
    cwd = os.path.abspath(args.cd or os.getcwd())
    runs = [r for r in reversed(load_runs()) if args.all_dirs or r.get("cwd") == cwd][: args.limit]
    if not runs:
        print("No sessions from this skill" + ("" if args.all_dirs else f" in {cwd}") + ".")
        return 0
    for r in runs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("updated", 0)))
        line = f"{when}  {r['session']}  {r.get('model') or '?'}  {r.get('mode', '?')}  {r.get('outcome', '?')}  {r.get('title') or ''}"
        print(line + (f"  [{r.get('cwd')}]" if args.all_dirs else ""))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("recent", help="previously chosen models")
    p.add_argument("-n", "--limit", type=int, default=4)
    p.set_defaults(func=cmd_recent)

    p = sub.add_parser("find", help="match free text against opencode's models")
    p.add_argument("query", nargs="+", help='e.g. "kimi k3.1 pro", "gemini argon high", or provider/model#effort')
    p.add_argument("-n", "--limit", type=int, default=10)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("run", help="run opencode and print its final response")
    p.add_argument("prompt", nargs="?", help="the prompt, or - to read it from stdin")
    p.add_argument("--prompt-file")
    p.add_argument("-m", "--model", help="provider/model[#effort] (required for a new session)")
    p.add_argument("-s", "--session", help="resume this session")
    p.add_argument("-c", "--continue", dest="continue_", action="store_true",
                   help="resume this skill's latest session in the directory")
    p.add_argument("--mode", choices=["review", "build"], help="review (read-only, default) or build (can edit)")
    p.add_argument("-C", "--cd", help="directory to run in (default: current)")
    p.add_argument("--title")
    p.add_argument("-f", "--file", action="append", help="attach a file (repeatable)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("show", help="reprint a session's response")
    p.add_argument("session", nargs="?", help="session ID (default: this skill's latest in the directory)")
    p.add_argument("--all", action="store_true", help="every turn, including narration between tool calls")
    p.add_argument("-C", "--cd")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("sessions", help="sessions this skill started")
    p.add_argument("-n", "--limit", type=int, default=10)
    p.add_argument("--all-dirs", action="store_true")
    p.add_argument("-C", "--cd")
    p.set_defaults(func=cmd_sessions)

    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
