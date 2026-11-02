"""Detailed step trace for the developer: trace hook, per-turn report, export, carry into the base data.

python3 "$CLAUDE_PROJECT_DIR/engine/scripts/trace_log.py" hook                 (trace hook; input — hook JSON on stdin;
                                                                                exit code always 0, prints nothing)
python3 engine/scripts/trace_log.py report [--session <id>] [--days N] [--json]   — per-turn report in /tmp/sebra/
python3 engine/scripts/trace_log.py export [--days N] [--json]                    — report and raw events in one file

Event format — formats.md, «Подробный лог». Work files — /tmp/sebra/trace/<session_id>.jsonl (SEBRA_TRACE_DIR for
tests); only commit.py moves them into data/log/trace/ (carry). At module level only the standard library is imported:
the hook runs on every Claude Code event and must not load common or YAML. A step label carries only words the engine
or the system defines (engine set from engine.lock, closed lists below, programs found on the system); anything else
is «?». Arguments, paths, texts of messages and agent prompts never reach the trace.
"""

import fcntl
import json
import os
import re
import shlex
import shutil
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
# engine/scripts/ -> base root, as common.ROOT
ROOT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))

TRACE_DIR = "/tmp/sebra/trace"
OUT_DIR = "/tmp/sebra"
TMP_DIRS = sorted({"/tmp/sebra", os.path.realpath("/tmp/sebra")})
UPLOADS = "/root/.claude/uploads"
NO_SESSION = "nosession"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
WORK_FILE = re.compile(r"^([A-Za-z0-9_-]{1,128})\.jsonl$")
DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([A-Za-z0-9_-]{1,128})\.jsonl$")
TRACE_REL = "data/log/trace"
KEEP_DAYS = 30
ROUTINE_PREFIX = "Выполни рутину "
MAX_LABELS = 8

# Closed set of engine scripts and the subcommands that may appear in a script label (plan of 0.2.3, formats.md).
# A new script or subcommand is added here; until then its label is «?» or the bare script name.
SCRIPTS = {
    "collect.py": ("now", "history", "slug", "daily", "weekly", "tasks"),
    "config.py": ("init", "get", "set", "migrate"),
    "delete.py": ("path", "tasks", "drafts", "draft"),
    "deliver.py": ("hook-prompt", "hook-stop", "shown", "send", "status"),
    "journal.py": ("add",),
    "record.py": ("new",),
    "remind.py": ("add", "cancel", "show"),
    "update_engine.py": ("init", "check", "update"),
    "views.py": ("show", "set", "reset", "rules-moved"),
    "trace_log.py": ("hook", "report", "export"),
    "commit.py": (),
    "extract.py": (),
    "fetch_page.py": (),
    "lint.py": (),
    "guard.py": (),
    "session_start.py": (),
    "cleanup_branches.py": (),
}

GIT_SUBS = frozenset((
    "add", "am", "apply", "archive", "bisect", "blame", "branch", "cat-file", "check-ignore", "checkout",
    "cherry-pick", "clean", "clone", "commit", "config", "describe", "diff", "fetch", "for-each-ref", "format-patch",
    "fsck", "gc", "grep", "init", "log", "ls-files", "ls-remote", "ls-tree", "merge", "merge-base", "mv", "notes",
    "pull", "push", "rebase", "reflog", "remote", "reset", "restore", "rev-list", "rev-parse", "revert", "rm",
    "shortlog", "show", "show-ref", "stash", "status", "submodule", "switch", "symbolic-ref", "tag", "update-index",
    "worktree",
))
GIT_WITH_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace")
SHELL_WORDS = frozenset((
    "cd", "echo", "printf", "test", "[", "[[", "export", "source", ".", "set", "unset", "read", "eval", "exec", "exit",
    "return", "true", "false", "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac",
    "time", "wait", "trap", "type", "command", "pwd", "shift", "local", "declare", "pushd", "popd", "umask", "ulimit",
))
DATA_FOLDERS = sorted((
    "data/records", "data/tasks", "data/reminders", "data/log/trace", "data/log", "data/wiki/topics",
    "data/wiki/people", "data/wiki/projects", "data/wiki/sources", "data/wiki", "data/reports/daily",
    "data/reports/weekly", "data/reports",
), key=len, reverse=True)
ENGINE_TOP = ("CLAUDE.md", "README.md", ".claude", "engine")
PATH_FIELDS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Grep": "path", "Glob": "path"}

HOOK_EVENTS = {
    "UserPromptSubmit": "prompt", "PreToolUse": "pre", "PostToolUse": "post", "PostToolUseFailure": "fail",
    "SubagentStart": "sub_start", "SubagentStop": "sub_stop", "Stop": "stop", "StopFailure": "stop_fail",
    "PreCompact": "compact", "PostCompact": "compact",
}
AGENT_NUMBERS = ("totalDurationMs", "totalToolUseCount", "totalTokens")
AGENT_WORDS = ("resolvedModel", "status")
SAFE_WORD = re.compile(r"[A-Za-z0-9._:@\[\]/-]{1,100}")
EXIT_LINE = re.compile(r"Exit code -?\d{1,10}")

PYTHON = re.compile(r"python(?:3(?:\.\d+)?)?")
SCRIPT_PATH = re.compile(r"(?:^|/)engine/scripts/[^/]+\.py$")
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
LIST_ITEM = re.compile(r"^[ \t]*-(?:[ \t]+(.*))?$")
PUNCT = "();<>|&\n"
SEPARATOR_CHARS = set(";|()\n")


# ---------------------------------------------------------------- work files


def trace_dir():
    """Folder of work files and of the flag `on`: SEBRA_TRACE_DIR for tests, otherwise /tmp/sebra/trace."""
    return os.environ.get("SEBRA_TRACE_DIR") or TRACE_DIR


def clean_sid(sid):
    """session_id usable as a file name, otherwise «nosession»."""
    return sid if isinstance(sid, str) and SESSION_ID.fullmatch(sid) else NO_SESSION


def event_line(event):
    """One JSON line (bytes, with LF); text that UTF-8 cannot encode falls back to \\u escapes."""
    try:
        text = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        data = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return data + b"\n"


def write_all(fd, data):
    while data:
        n = os.write(fd, data)
        data = data[n:]


def append(sid, event):
    """Append one event to the session work file under flock (background hooks of one event run in parallel)."""
    folder = trace_dir()
    os.makedirs(folder, exist_ok=True)
    data = event_line(event)
    fd = os.open(os.path.join(folder, clean_sid(sid) + ".jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        write_all(fd, data)
    finally:
        os.close(fd)  # closing the descriptor releases the lock


# ---------------------------------------------------------------- engine set


class Engine:
    """Engine set — the `files` list of engine.lock; read lazily, at most once per call, never cached between calls."""

    def __init__(self, root=ROOT):
        self.root = root
        self._files = None
        self._dirs = None

    def files(self):
        if self._files is None:
            self._files = lock_files(os.path.join(self.root, "engine.lock"))
        return self._files

    def dirs(self):
        """Folders holding at least one file of the set; the root itself is «.»."""
        if self._dirs is None:
            dirs = set()
            for f in self.files():
                parts = f.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    dirs.add("/".join(parts[:i]))
            if self.files():
                dirs.add(".")
            self._dirs = dirs
        return self._dirs


def lock_files(path):
    """Paths after the line `files:` without a YAML parser: `- <path>` as is or, in double quotes, as a JSON string.

    The first non-empty line of another shape ends the list; a path that cannot be read this way is skipped.
    No file or unreadable file — empty set.
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, ValueError):
        return frozenset()
    files, inside = set(), False
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not inside:
            inside = line.rstrip() == "files:"
            continue
        if not line.strip():
            continue
        m = LIST_ITEM.match(line)
        if not m:
            break
        value = (m.group(1) or "").strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                continue
            if not isinstance(value, str):
                continue
        if value:
            files.add(value)
    return frozenset(files)


# ---------------------------------------------------------------- step labels


def script_label(argv):
    """Script label: name from the closed table SCRIPTS and the subcommand, if the table lists it; unknown — «?»."""
    if not argv:
        return "?"
    name = os.path.basename(str(argv[0]))
    subs = SCRIPTS.get(name)
    if subs is None:
        return "?"
    sub = next((a for a in argv[1:] if not str(a).startswith("-")), None)
    return "%s %s" % (name, sub) if sub in subs else name


def python_label(args):
    """python3 [interpreter options] <…engine/scripts/<name>.py> … — script label; any other Python run — python3."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        if a.startswith("--"):
            i += 1
            continue
        letters = a[1:]
        if letters[0] in "XW":
            i += 1 if len(letters) > 1 else 2  # -X utf8 / -Xutf8
            continue
        if "c" in letters or "m" in letters:
            return "python3"
        i += 1
    if i < len(args) and SCRIPT_PATH.search(args[i]):
        return script_label(args[i:])
    return "python3"


def git_label(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a in GIT_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    if i < len(args) and args[i] in GIT_SUBS:
        return "git " + args[i]
    return "git"


def found_program(word):
    try:
        return bool(word) and "/" not in word and shutil.which(word) is not None
    except (OSError, ValueError):
        return False


def command_label(words):
    """Label of one simple command by its first word after NAME=value assignments; no word — None."""
    i = 0
    while i < len(words) and ASSIGN.match(words[i]):
        i += 1
    if i >= len(words):
        return None
    first, rest = words[i], words[i + 1:]
    if PYTHON.fullmatch(first):
        return python_label(rest)
    if SCRIPT_PATH.search(first):
        return script_label([first] + rest)
    if first == "git":
        return git_label(rest)
    if first in SHELL_WORDS or found_program(first):
        return first
    return "?"


def simple_commands(command):
    """Words of simple commands, lazily. Separators — ; && || | |& & ( ) and newline; a redirection skips its target.

    A simple command with << (here-document) is the last one: neither the document body nor anything after it is
    tokenized. An unbalanced quote raises ValueError from shlex.
    """
    lex = shlex.shlex(command.replace("\\\n", " "), posix=True, punctuation_chars=PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    words, skip = [], False
    while True:
        tok = lex.get_token()
        if tok is None:
            break
        if tok and set(tok) <= set(PUNCT):
            if "<<" in tok.replace("<<<", ""):
                if words:
                    yield words
                return
            if SEPARATOR_CHARS & set(tok) or set(tok) == {"&"}:
                if words:
                    yield words
                words, skip = [], False
                continue
            skip = True  # redirection operator: the next word is its target
            continue
        if skip:
            skip = False
            continue
        words.append(tok)
    if words:
        yield words


def bash_label(command):
    if not isinstance(command, str):
        return "?"
    labels = []
    try:
        for words in simple_commands(command):
            label = command_label(words)
            if label is not None:
                labels.append(label)
            if len(labels) > MAX_LABELS:
                break
    except ValueError:
        return "?"
    if not labels:
        return "?"
    if len(labels) > MAX_LABELS:
        return "; ".join(labels[:MAX_LABELS]) + "; …"
    return "; ".join(labels)


def under(p, d):
    return p == d or p.startswith(d.rstrip("/") + "/")


def path_label(value, engine):
    """Path class: tmp, вложение, an engine path from the set, a data folder from the list, «?» or другое."""
    if not isinstance(value, str) or not value or "\0" in value:
        return "?"
    full = os.path.normpath(value if os.path.isabs(value) else os.path.join(engine.root, value))
    if any(under(full, d) for d in TMP_DIRS):
        return "tmp"
    if under(full, UPLOADS):
        return "вложение"
    rel = relative(full, engine.root)
    if rel is None:
        return "другое"
    if rel in engine.files() or rel in engine.dirs():
        return rel
    if rel != "." and any(under(rel, top) for top in ENGINE_TOP):
        return "?"
    if under(rel, "data"):
        return next((d for d in DATA_FOLDERS if under(rel, d)), "data")
    return "другое"


def relative(full, root):
    """Path from the base root («.» — the root itself) or None, if outside; the root is tried as given and resolved."""
    roots = [os.path.normpath(root)]
    real = os.path.realpath(root)
    if real not in roots:
        roots.append(real)
    for candidate in (full, os.path.realpath(full)):
        for r in roots:
            if candidate == r:
                return "."
            if candidate.startswith(r.rstrip("/") + "/"):
                return candidate[len(r.rstrip("/")) + 1:]
    return None


def step_label(tool, ti, engine):
    if not isinstance(ti, dict):
        ti = {}
    if tool == "Bash":
        return bash_label(ti.get("command"))
    if tool == "Skill":
        name = ti.get("skill")
        ok = isinstance(name, str) and ".claude/skills/%s/SKILL.md" % name in engine.files()
        return name if ok else "?"
    if tool == "Agent":
        kind = ti.get("subagent_type")
        return kind if isinstance(kind, str) and ".claude/agents/%s.md" % kind in engine.files() else "?"
    if tool in PATH_FIELDS:
        return path_label(ti.get(PATH_FIELDS[tool]), engine)
    return tool if isinstance(tool, str) and tool else "?"


def agent_field(value, subagent_event, engine):
    """agent_type of the hook input: engine agent — its type, otherwise «?»; empty — main (sub_start, sub_stop — «»)."""
    if value is None or value == "":
        return "" if subagent_event else "main"
    if isinstance(value, str) and ".claude/agents/%s.md" % value in engine.files():
        return value
    return "?"


# ---------------------------------------------------------------- trace hook


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def agent_result(resp):
    """Run telemetry of a finished Agent call — only the numbers, the model and the status; the answer is not read."""
    if not isinstance(resp, dict):
        return {}
    x = {k: resp[k] for k in AGENT_NUMBERS if number(resp.get(k))}
    x.update({k: resp[k] for k in AGENT_WORDS if isinstance(resp.get(k), str) and SAFE_WORD.fullmatch(resp[k])})
    return x


def exit_line(error):
    """First line of a tool error, only if it is exactly `Exit code N`: the rest may carry paths and data."""
    if not isinstance(error, str):
        return None
    first = error.split("\n", 1)[0].rstrip("\r")
    return first if EXIT_LINE.fullmatch(first) else None


def hook_event(inp, now, engine):
    """(session_id, event) for a hook input; an event the trace does not record — (None, None)."""
    name = inp.get("hook_event_name")
    ev = HOOK_EVENTS.get(name) if isinstance(name, str) else None
    if ev is None:
        return None, None
    sid = clean_sid(inp.get("session_id"))
    event = {"t": round(now, 3), "ev": ev, "sid": sid}
    pid = inp.get("prompt_id")
    if isinstance(pid, str) and pid:
        event["pid"] = pid
    event["agent"] = agent_field(inp.get("agent_type"), ev in ("sub_start", "sub_stop"), engine)
    aid = inp.get("agent_id")
    if isinstance(aid, str) and aid:
        event["aid"] = aid
    x = {}
    if ev in ("pre", "post", "fail"):
        tid, tool = inp.get("tool_use_id"), inp.get("tool_name")
        if isinstance(tid, str) and tid:
            event["tid"] = tid
        if isinstance(tool, str) and tool:
            event["tool"] = tool
        if ev == "pre":
            event["what"] = step_label(tool, inp.get("tool_input"), engine)
        else:
            if number(inp.get("duration_ms")):
                event["ms"] = inp["duration_ms"]
            if ev == "post" and tool == "Agent":
                x = agent_result(inp.get("tool_response"))
            if ev == "fail":
                x["is_interrupt"] = inp.get("is_interrupt") is True
                line = exit_line(inp.get("error"))
                if line:
                    x["exit"] = line
    elif ev == "prompt":
        prompt = inp.get("prompt") if isinstance(inp.get("prompt"), str) else ""
        x = {"routine": prompt.lstrip().startswith(ROUTINE_PREFIX), "chars": len(prompt)}
    elif ev == "compact":
        x = {"phase": "pre" if name == "PreCompact" else "post"}
    if x:
        event["x"] = x
    return sid, event


def hook():
    """Trace hook: one event per Claude Code hook input; any error — silently nothing (the trace never gets in the
    way of the turn)."""
    now = time.time()
    try:
        inp = json.loads(sys.stdin.buffer.read().decode("utf-8") or "null")
        if not isinstance(inp, dict):
            return
        sid, event = hook_event(inp, now, Engine(ROOT))
        if event is not None:
            append(sid, event)
    except Exception:  # noqa: BLE001 — the trace hook never fails the turn
        pass


# ---------------------------------------------------------------- carry into the base


def append_bytes(path, data):
    """Append data to a file of the base under flock; a missing final LF of the old content is added first."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        write_all(fd, data)
    finally:
        os.close(fd)


def read_all(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def carry(root, day):
    """Move work files into data/log/trace/<day>-<session_id>.jsonl and drop day files older than KEEP_DAYS days.

    Each work file is handled under its own flock: its lines are appended to the day file, then the work file is
    truncated (not removed: a writer holding it open keeps appending to the same file). day — datetime.date of the
    user. Returns (moved, removed) — paths from the base root.
    """
    folder = trace_dir()
    target = os.path.join(root, TRACE_REL)
    moved = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for name in names:
        if not WORK_FILE.match(name):
            continue
        src = os.path.join(folder, name)
        if os.path.islink(src) or not os.path.isfile(src):
            continue
        fd = os.open(src, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = read_all(fd)
            if data:
                if not data.endswith(b"\n"):
                    data += b"\n"
                os.makedirs(target, exist_ok=True)
                rel = "%s/%s-%s" % (TRACE_REL, day.isoformat(), name)
                append_bytes(os.path.join(root, rel), data)
                os.ftruncate(fd, 0)
                moved.append(rel)
        finally:
            os.close(fd)
    return moved, drop_old(root, day)


def drop_old(root, day):
    import datetime as dt

    target = os.path.join(root, TRACE_REL)
    removed = []
    try:
        names = sorted(os.listdir(target))
    except OSError:
        return removed
    for name in names:
        m = DAY_FILE.match(name)
        if not m:
            continue
        try:
            d = dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        full = os.path.join(target, name)
        if (day - d).days > KEEP_DAYS and os.path.isfile(full) and not os.path.islink(full):
            os.remove(full)
            removed.append("%s/%s" % (TRACE_REL, name))
    return removed


# ---------------------------------------------------------------- reading the trace

KINDS = ("think", "launch", "handover", "tools", "scripts", "hooks", "compact")
AGENT_KINDS = ("think", "launch", "handover", "tools", "scripts")
KIND_NAMES = {"think": "раздумья", "launch": "запуск агента", "handover": "передача ответа агента",
              "tools": "прочие инструменты", "scripts": "скрипты движка", "hooks": "хуки движка",
              "compact": "сжатие контекста"}
ENGINE_AGENT = "движок"
INF = float("inf")


def read_lines(path, shared_lock=False):
    """Non-empty lines of a file (UTF-8, damaged bytes replaced); unreadable — []."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return []
    try:
        if shared_lock:
            fcntl.flock(fd, fcntl.LOCK_SH)
        data = read_all(fd)
    except OSError:
        return []
    finally:
        os.close(fd)
    return [line for line in data.decode("utf-8", errors="replace").split("\n") if line.strip()]


def collect_lines(root, today, days):
    """Raw lines: day files of data/log/trace/ for the last `days` days (by file name) and not yet carried work files."""
    import datetime as dt

    lines = []
    folder = os.path.join(root, TRACE_REL)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for name in names:
        m = DAY_FILE.match(name)
        if not m:
            continue
        try:
            d = dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if (today - d).days < days:
            lines += read_lines(os.path.join(folder, name))
    try:
        names = sorted(os.listdir(trace_dir()))
    except OSError:
        names = []
    for name in names:
        if WORK_FILE.match(name):
            lines += read_lines(os.path.join(trace_dir(), name), shared_lock=True)
    return lines


def parse_events(lines, session=None):
    """Events of the lines with numeric t and string ev and sid; seq — line order (ties of t keep it)."""
    out = []
    for seq, line in enumerate(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or not number(e.get("t")) or not isinstance(e.get("ev"), str) \
                or not isinstance(e.get("sid"), str):
            continue
        if session is not None and e["sid"] != session:
            continue
        e["_seq"] = seq
        out.append(e)
    return out


def ms_of(e):
    return e["ms"] / 1000.0 if number(e.get("ms")) and e["ms"] > 0 else 0.0


def seg_start(e):
    """Start of an event: hook and script — t minus duration (t is the end of their work), otherwise t."""
    return e["t"] - ms_of(e) if e["ev"] in ("hook", "script") else e["t"]


class Turn:
    def __init__(self, prompt):
        self.prompt = prompt
        self.sid = prompt["sid"]
        self.events = [prompt]
        self.start = prompt["t"]
        self.stop_t = None      # closed end: the first stop or stop_fail after the prompt
        self.next_t = INF       # open end: the next prompt of the session


def split_turns(events):
    """(turns, outside): turns of every session in time order; events of no turn — outside."""
    sessions = {}
    for e in events:
        sessions.setdefault(e["sid"], []).append(e)
    turns, outside = [], []
    for sid in sorted(sessions, key=lambda s: min(e["t"] for e in sessions[s])):
        evs = sorted(sessions[sid], key=lambda e: (e["t"], e["_seq"]))
        own = []
        for i, e in enumerate(evs):
            if e["ev"] != "prompt":
                continue
            turn = Turn(e)
            for f in evs[i + 1:]:
                if f["ev"] == "prompt":
                    turn.next_t = f["t"]
                    break
                if f["ev"] in ("stop", "stop_fail"):
                    turn.stop_t = f["t"]
                    break
            own.append(turn)
        by_pid = {}
        for turn in own:
            pid = turn.prompt.get("pid")
            if isinstance(pid, str) and pid:
                by_pid.setdefault(pid, []).append(turn)
        for e in evs:
            if e["ev"] == "prompt":
                continue
            turn = pick_turn(e, own, by_pid)
            if turn is None:
                outside.append(e)
            else:
                turn.events.append(e)
        turns += own
    return turns, outside


def in_span(turn, t):
    if t < turn.start:
        return False
    if turn.stop_t is not None:
        return t <= turn.stop_t
    return t < turn.next_t


def pick_turn(e, turns, by_pid):
    pid = e.get("pid")
    same = by_pid.get(pid) if isinstance(pid, str) and pid else None
    if same:
        inside = [t for t in same if in_span(t, e["t"])]
        if inside:
            return inside[0]
        before = [t for t in same if t.start <= e["t"]]
        return before[-1] if before else same[0]
    for turn in turns:
        if in_span(turn, e["t"]):
            return turn
    return None


class Seg:
    __slots__ = ("start", "end", "kind", "name", "agent", "part", "aid")

    def __init__(self, start, end, kind, name="", agent="", part="", aid=None):
        self.start, self.end, self.kind, self.name, self.agent, self.part, self.aid = \
            start, end, kind, name, agent, part, aid


class Acc:
    """Sums in seconds: by kind, by agent, and the breakdowns of the report."""

    def __init__(self):
        self.total = 0.0
        self.kinds = {k: 0.0 for k in KINDS}
        self.parts = {"prep": 0.0, "first_step": 0.0}
        self.agents = {}
        self.detail = {k: {} for k in KINDS}          # kind -> name -> seconds
        self.launch_by_agent = {}                     # agent -> {"prep", "first_step"}

    def add(self, kind, seconds, name=None, agent=None, part=None):
        self.kinds[kind] += seconds
        if name is not None:
            d = self.detail[kind]
            d[name] = d.get(name, 0.0) + seconds
        if agent is not None and kind in AGENT_KINDS:
            a = self.agents.setdefault(agent, {k: 0.0 for k in AGENT_KINDS})
            a[kind] += seconds
        if part is not None:
            self.parts[part] += seconds
            lp = self.launch_by_agent.setdefault(agent, {"prep": 0.0, "first_step": 0.0})
            lp[part] += seconds

    def merge(self, other):
        self.total += other.total
        for k in KINDS:
            self.kinds[k] += other.kinds[k]
            for name, v in other.detail[k].items():
                self.detail[k][name] = self.detail[k].get(name, 0.0) + v
        for p in self.parts:
            self.parts[p] += other.parts[p]
        for agent, vals in other.agents.items():
            a = self.agents.setdefault(agent, {k: 0.0 for k in AGENT_KINDS})
            for k in AGENT_KINDS:
                a[k] += vals[k]
        for agent, vals in other.launch_by_agent.items():
            lp = self.launch_by_agent.setdefault(agent, {"prep": 0.0, "first_step": 0.0})
            for p in lp:
                lp[p] += vals[p]


def analyze(turn):
    """(Acc, step lines, turn start) — every second of the turn goes to exactly one kind (formats.md, plan of 0.2.3)."""
    evs = sorted(turn.events, key=lambda e: (e["t"], e["_seq"]))
    start = min(seg_start(e) for e in evs)
    end = max(e["t"] for e in evs)

    calls, open_calls = [], {}
    hooks, scripts, compacts, subs = [], [], [], {}
    compact_open = None
    for e in evs:
        ev, t = e["ev"], e["t"]
        if ev == "pre":
            call = {"tid": e.get("tid"), "tool": e.get("tool") or "?", "what": e.get("what"),
                    "agent": e.get("agent") or "main", "aid": e.get("aid"), "start": t, "end": None, "ms": None}
            calls.append(call)
            if call["tid"] and call["tid"] not in open_calls:
                open_calls[call["tid"]] = call
        elif ev in ("post", "fail"):
            call = open_calls.pop(e.get("tid"), None) if e.get("tid") else None
            if call is not None:
                call["end"], call["ms"] = t, e.get("ms")
        elif ev == "hook":
            hooks.append(Seg(seg_start(e), t, "hooks", name=str(e.get("what") or "?"), agent=ENGINE_AGENT))
        elif ev == "script":
            scripts.append(Seg(seg_start(e), t, "scripts", name=str(e.get("what") or "?")))
        elif ev == "compact":
            phase = (e.get("x") or {}).get("phase") if isinstance(e.get("x"), dict) else None
            if phase == "pre" and compact_open is None:
                compact_open = t
            elif phase == "post" and compact_open is not None:
                compacts.append(Seg(compact_open, t, "compact"))
                compact_open = None
        elif ev == "sub_start":
            aid = e.get("aid")
            if e.get("agent") and isinstance(aid, str) and aid not in subs:
                subs[aid] = {"aid": aid, "agent": e["agent"], "start": t, "stop": None, "call": None,
                             "first": None, "end": None}
        elif ev == "sub_stop":
            sub = subs.get(e.get("aid"))
            if sub is not None and sub["stop"] is None:
                sub["stop"] = t
    if compact_open is not None:
        compacts.append(Seg(compact_open, end, "compact"))

    # calls of subagents without their sub_start in the turn give no segments
    calls = [c for c in calls if not c["aid"] or c["aid"] in subs]
    for c in calls:
        c["stop"] = c["end"] if c["end"] is not None else end
        if c["aid"]:
            sub = subs[c["aid"]]
            if sub["first"] is None or c["start"] < sub["first"]:
                sub["first"] = c["start"]
    agent_calls = [c for c in calls if c["tool"] == "Agent"]
    linked = set()
    for sub in sorted(subs.values(), key=lambda s: s["start"]):
        for i, c in enumerate(agent_calls):
            if i not in linked and c["start"] <= sub["start"]:
                linked.add(i)
                sub["call"] = c
                break
    for sub in subs.values():
        if sub["stop"] is not None:
            sub["end"] = sub["stop"]
        elif sub["call"] is not None:
            sub["end"] = sub["call"]["stop"]
        else:
            sub["end"] = end

    for s in scripts:
        bash = [c for c in calls if c["tool"] == "Bash" and c["start"] < max(s.end, s.start + 1e-9)
                and c["stop"] > s.start]
        s.agent = max(bash, key=lambda c: c["start"])["agent"] if bash else "main"

    tools = [Seg(c["start"], c["stop"], "tools", name=c["tool"], agent=c["agent"], aid=c["aid"])
             for c in calls if c["tool"] != "Agent"]
    launches, handovers = [], []
    for sub in subs.values():
        if sub["call"] is not None:
            launches.append(Seg(sub["call"]["start"], sub["start"], "launch", agent=sub["agent"], part="prep"))
        first_end = sub["first"] if sub["first"] is not None else sub["end"]
        launches.append(Seg(sub["start"], first_end, "launch", agent=sub["agent"], part="first_step"))
        if sub["stop"] is not None:
            if sub["call"] is not None and sub["call"]["end"] is not None:
                hand_end = sub["call"]["end"]
            else:
                hand_end = next((e["t"] for e in evs if e["t"] > sub["stop"] and e.get("agent") == "main"), end)
            handovers.append(Seg(sub["stop"], hand_end, "handover", agent=sub["agent"]))
    for i, c in enumerate(agent_calls):
        if i not in linked:
            launches.append(Seg(c["start"], c["stop"], "launch", agent=c["what"] or "?", part="prep"))
    thinkers = [sub for sub in subs.values() if sub["first"] is not None]
    sub_tools = {}
    for c in calls:
        if c["aid"]:
            sub_tools.setdefault(c["aid"], []).append(c)

    acc = Acc()
    acc.total = end - start
    order = (hooks, scripts, compacts, handovers, launches, tools)
    points = {start, end}
    for group in order:
        for s in group:
            points.add(min(max(s.start, start), end))
            points.add(min(max(s.end, start), end))
    for sub in thinkers:
        points.update((min(max(sub["first"], start), end), min(max(sub["end"], start), end)))
    points = sorted(points)
    for a, b in zip(points, points[1:]):
        d = b - a
        if d <= 0:
            continue
        m = (a + b) / 2
        for group in order:
            active = [s for s in group if s.start <= m < s.end]
            if active:
                share = d / len(active)
                for s in active:
                    if s.kind == "hooks":
                        acc.add("hooks", share, name=s.name)
                    elif s.kind == "scripts":
                        acc.add("scripts", share, name=s.name, agent=s.agent)
                    elif s.kind == "compact":
                        acc.add("compact", share)
                    elif s.kind == "launch":
                        acc.add("launch", share, name=s.agent, agent=s.agent, part=s.part)
                    else:
                        acc.add(s.kind, share, name=s.name if s.kind == "tools" else s.agent, agent=s.agent)
                break
        else:
            busy = [sub for sub in thinkers if sub["first"] <= m < sub["end"]
                    and not any(c["start"] <= m < c["stop"] for c in sub_tools.get(sub["aid"], ()))]
            if busy:
                for sub in busy:
                    acc.add("think", d / len(busy), name=sub["agent"], agent=sub["agent"])
            else:
                acc.add("think", d, name="main", agent="main")

    steps = []
    for c in calls:
        label = c["tool"] if not c["what"] or c["what"] == c["tool"] else "%s %s" % (c["tool"], c["what"])
        work = " (работа %s с)" % sec(c["ms"] / 1000.0) if number(c["ms"]) else ""
        steps.append((c["start"], c["agent"], label, "%s с%s" % (sec(c["stop"] - c["start"]), work)))
    for s in scripts:
        steps.append((s.start, s.agent, "скрипт " + s.name, "%s с" % sec(s.end - s.start)))
    for s in hooks:
        steps.append((s.start, ENGINE_AGENT, "хук " + s.name, "%s с" % sec(s.end - s.start)))
    for sub in subs.values():
        prep = (sub["start"] - sub["call"]["start"]) if sub["call"] is not None else 0.0
        first = (sub["first"] if sub["first"] is not None else sub["end"]) - sub["start"]
        begin = sub["call"]["start"] if sub["call"] is not None else sub["start"]
        steps.append((begin, sub["agent"], "запуск агента",
                      "%s с (подготовка %s с, первый шаг %s с)" % (sec(prep + first), sec(prep), sec(first))))
    for s in handovers:
        steps.append((s.start, s.agent, "передача ответа", "%s с" % sec(s.end - s.start)))
    steps.sort(key=lambda x: x[0])
    lines = ["+%s · %s · %s · %s" % (sec(t - start), agent, label, dur) for t, agent, label, dur in steps]
    return acc, lines, start


# ---------------------------------------------------------------- report and export


def sec(v):
    return "%.3f" % (round(v, 3) + 0.0)


def r3(v):
    return round(v, 3) + 0.0


def pct(part, total):
    return ("%.1f" % (100.0 * part / total)).replace(".", ",") if total > 0 else "0,0"


def summary_lines(acc):
    total = acc.total
    out = ["- всего: %s с" % sec(total)]
    for kind in KINDS:
        value = acc.kinds[kind]
        detail = sorted(acc.detail[kind].items(), key=lambda kv: -kv[1])
        if kind == "launch":
            parts = ", ".join("%s %s с (подготовка %s с, первый шаг %s с)"
                              % (name, sec(v), sec(acc.launch_by_agent.get(name, {}).get("prep", 0.0)),
                                 sec(acc.launch_by_agent.get(name, {}).get("first_step", 0.0)))
                              for name, v in detail)
        else:
            parts = ", ".join("%s %s с" % (name, sec(v)) for name, v in detail)
        out.append("- %s: %s с (%s %%)%s" % (KIND_NAMES[kind], sec(value), pct(value, total),
                                              " — " + parts if parts else ""))
    return out


def turn_title(n, turn, start, tz):
    import datetime as dt

    when = dt.datetime.fromtimestamp(start, tz).strftime("%Y-%m-%d %H:%M:%S")
    x = turn.prompt.get("x") if isinstance(turn.prompt.get("x"), dict) else {}
    kind = "рутина" if x.get("routine") is True else "сообщение"
    chars = x.get("chars")
    size = ", %d знаков" % chars if isinstance(chars, int) and not isinstance(chars, bool) else ""
    return "## Ход %d — %s, сессия %s (%s%s)" % (n, when, turn.sid, kind, size)


def outside_line(e, tz):
    import datetime as dt

    when = dt.datetime.fromtimestamp(e["t"], tz).strftime("%Y-%m-%d %H:%M:%S")
    parts = [when, e["ev"], e["sid"]]
    for key in ("agent", "tool", "what"):
        if isinstance(e.get(key), str) and e[key]:
            parts.append(e[key])
    if number(e.get("ms")):
        parts.append("%s с" % sec(e["ms"] / 1000.0))
    return "- " + " · ".join(parts)


def build_report(lines, session, days, tz, title):
    """(Markdown text, Acc, number of turns)."""
    events = parse_events(lines, session)
    turns, outside = split_turns(events)
    total = Acc()
    parts = ["# %s" % title, "",
             "Дней: %d%s. Ходов: %d. Строка шага — «+секунды от начала хода · агент · шаг · длительность»."
             % (days, ", сессия %s" % session if session else "", len(turns)),
             "Раздумья — промежуток между шагами одного агента: запрос к модели и накладные расходы Claude Code "
             "вместе.", ""]
    for n, turn in enumerate(turns, 1):
        acc, steps, start = analyze(turn)
        total.merge(acc)
        parts += [turn_title(n, turn, start, tz), ""] + (steps or ["Шагов нет."]) + ["", "Итог хода:"]
        parts += summary_lines(acc) + [""]
    if outside:
        parts += ["## Вне ходов", ""] + [outside_line(e, tz) for e in sorted(outside, key=lambda e: e["t"])] + [""]
    parts += ["## Итог по всем ходам", ""] + summary_lines(total)
    return "\n".join(parts) + "\n", total, len(turns)


def write_out(name, text):
    os.makedirs(OUT_DIR, exist_ok=True)
    full = os.path.join(OUT_DIR, name)
    tmp = full + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, full)
    return full


def report_json(full, turns, acc):
    return {"file": full, "turns": turns, "total": r3(acc.total),
            "kinds": {k: r3(acc.kinds[k]) for k in KINDS},
            "launch_parts": {k: r3(v) for k, v in acc.parts.items()},
            "agents": {a: {k: r3(v[k]) for k in AGENT_KINDS} for a, v in sorted(acc.agents.items())}}


def fence_for(lines):
    longest = max([len(m) for line in lines for m in re.findall(r"`+", line)] or [0])
    return "`" * max(3, longest + 1)


def cli(argv):
    import argparse
    import datetime as dt

    import common
    from common import Fail

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise Fail(2, "ошибка вызова: %s" % message)

    json_mode = "--json" in argv
    try:
        p = Parser(prog="trace_log.py")
        sub = p.add_subparsers(dest="cmd")
        r = sub.add_parser("report")
        r.add_argument("--session")
        r.add_argument("--days", default="1")
        r.add_argument("--json", action="store_true")
        x = sub.add_parser("export")
        x.add_argument("--days", default="7")
        x.add_argument("--json", action="store_true")
        for sp in sub.choices.values():
            sp.error = p.error
        args = p.parse_args(argv)
        if args.cmd not in ("report", "export"):
            raise Fail(2, "команда: hook, report или export")
        if not re.fullmatch(r"[0-9]{1,4}", args.days) or int(args.days) < 1:
            raise Fail(2, "--days: нужно целое число дней от 1 до 9999")
        days = int(args.days)
        try:
            moment = common.now()
        except Fail:
            common.use_timezone(dt.timezone.utc)  # config.yaml unreadable — the report is still useful in UTC
            moment = common.now()
        tz = moment.tzinfo
        lines = collect_lines(common.ROOT, moment.date(), days)
        stamp = moment.strftime("%Y%m%d-%H%M")
        if args.cmd == "report":
            text, acc, turns = build_report(lines, args.session, days, tz, "Подробный лог Sebra — отчёт по ходам")
            full = write_out("trace-report-%s.md" % stamp, text)
            out, human = report_json(full, turns, acc), "отчёт: %s" % full
        else:
            text, acc, turns = build_report(lines, None, days, tz, "Подробный лог Sebra")
            fence = fence_for(lines)
            text += "\n## Сырые события\n\n%sjsonl\n%s\n%s\n" % (fence, "\n".join(lines), fence) if lines else \
                "\n## Сырые события\n\nНет.\n"
            full = write_out("sebra-log-%s.md" % stamp, text)
            out = {"file": full, "turns": turns, "events": len(lines)}
            human = "лог: %s — ходов %d, событий %d" % (full, turns, len(lines))
    except Fail as e:
        if json_mode:
            print(json.dumps({"error": e.message}, ensure_ascii=False))
        else:
            print(e.message, file=sys.stderr)
        return e.code
    except OSError as e:
        message = "не удалось записать или прочитать лог: %s" % e
        print(json.dumps({"error": message}, ensure_ascii=False) if json_mode else message,
              file=sys.stdout if json_mode else sys.stderr)
        return 3
    print(json.dumps(out, ensure_ascii=False) if json_mode else human)
    return 0


def main():
    # trace_log.py writes no `script` events about itself: the hook would double every event, and export is already
    # visible as the Bash step «trace_log.py export».
    if sys.argv[1:2] == ["hook"]:
        hook()
        return 0
    return cli(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
