"""Хук PreToolUse для Write, Edit и Bash: кто куда пишет и какие команды можно.

python3 "$CLAUDE_PROJECT_DIR/engine/scripts/guard.py" (вход — JSON вызова в stdin).

Решение — только deny: JSON в stdout и код 0; нет решения — пустой stdout и код 0.
Внутренняя ошибка — строка «guard.py: внутренняя ошибка: …» в stderr и код 2: для хука это запрет.
Это защита от ошибки, а не от умысла: shell обходит любую эвристику.
"""

import json
import os
import re
import shlex
import sys

import common

TMP_DIRS = sorted({"/tmp/sebra", os.path.realpath("/tmp/sebra")})
AGENTS = ("seb-intake", "seb-answer", "seb-digester", "seb-gardener", "seb-reporter", "seb-rules",
          "seb-challenger", "seb-restorer")
RECORD_FIELDS = {"seb-intake": {"status", "seen", "error"}, "seb-digester": {"status", "error"}}

ZONES_TEXT = {
    "seb-intake": "создаёшь и меняешь data/wiki/, data/tasks/, data/index.md; в записях data/records/ меняешь "
                  "только поля шапки status, seen, error; записи создаёт python3 engine/scripts/record.py new",
    "seb-answer": "в базу не пишешь",
    "seb-digester": "создаёшь и меняешь data/wiki/, data/tasks/, data/index.md; в записях data/records/ меняешь "
                    "только поля шапки status, error",
    "seb-gardener": "создаёшь страницы в data/wiki/; меняешь data/wiki/, data/tasks/, data/index.md",
    "seb-reporter": "создаёшь сводки в data/reports/daily/ и data/reports/weekly/ и только дописываешь их",
    "seb-rules": "меняешь rules.local.md",
    "seb-challenger": "в базу не пишешь",
    "seb-restorer": "меняешь существующие файлы data/wiki/, data/tasks/, data/index.md — только для удаления "
                    "по просьбе пользователя",
}

R_MAIN = ("Основная сессия не пишет в {path}. Запись делает агент через навык: материал — seb-capture, поправка — "
          "seb-correct. Обходить запрет через Bash нельзя.")
R_AGENT = ("{who} не пишет в {path}. Твои зоны: {zones}. Нужна запись туда — не делай её, в отчёте ИТОГ «частично» "
           "и что не сделано.")
R_UNKNOWN = "Писать в базу могут только агенты seb-…"
R_OUTSIDE = "Запись вне базы запрещена: {path}. Рабочие файлы — только в /tmp/sebra/."
R_PROTECTED = ("{path} напрямую не правится: настройки — python3 engine/scripts/config.py, журнал — python3 "
               "engine/scripts/journal.py, напоминания — python3 engine/scripts/remind.py, движок — python3 "
               "engine/scripts/update_engine.py, git — python3 engine/scripts/commit.py.")
R_APPEND = ("{path} — только дописывание: прежний текст не меняется. Допиши новое после последней строки "
            "(Edit: old_string — последняя строка целиком, new_string — она же и новое после неё).")
R_RECORD_NEW = "{path}: записи создаёт только python3 engine/scripts/record.py new."
R_RECORD = ("{path}: записи не меняются — тело никогда, в шапке только поля {fields}. Изменено: {what}.")
R_NOT_FOUND = ("Edit: old_string не найден в {path} дословно. Перечитай файл и повтори правку с точной строкой "
               "из файла.")

B_UNPARSED = "команда не разобрана: разбей на простые команды"
B_WRITE = "Файлы базы пишутся только инструментами Write и Edit и скриптами движка python3 engine/scripts/…"
B_DELETE = ("{prog}: удалять, переименовывать и переносить файлы нельзя — удаление только python3 "
            "engine/scripts/delete.py и remind.py; рабочие файлы в /tmp/sebra/ удалять не нужно.")
B_SHELL = "{prog}: вложенный shell и eval не разрешены — запускай простые команды по одной."
B_PYTHON = "Python — только скрипты движка: python3 engine/scripts/<имя>.py"
B_CONFIG = "Настройки меняет только основная сессия навыком seb-settings"
B_GIT = "git {sub}: только через скрипты движка"
B_GIT_OPTION = "git {opt}: глобальный параметр запрещён — можно только -C, --no-pager, -P, --no-optional-locks"
B_RESTORE = ("git restore: seb-restorer восстанавливает только файлы data/ вне data/log/, командой "
             "git restore --source=<коммит> -- <пути>.")


class Unparsed(Exception):
    pass


# ================================================================ пути


def under(p, d):
    return p == d or p.startswith(d.rstrip("/") + "/")


def in_tmp(real):
    return any(under(real, d) for d in TMP_DIRS)


def rel_of(real, root):
    return os.path.relpath(real, root).replace(os.sep, "/")


def is_data(rel):
    return rel.startswith(common.DATA_DIR + "/")


def protected(rel, engine):
    """Закрыто всем: файлы движка, .git/, config.yaml, engine.lock, data/log/, data/reminders/."""
    return (rel in engine or rel in (common.CONFIG, common.ENGINE_LOCK) or under(rel, ".git")
            or under(rel, common.LOG) or under(rel, common.REMINDERS))


def rights(who, rel):
    """(можно создать, как можно менять существующий: None | edit | append | record)."""
    def d(name):
        return rel.startswith(name + "/")

    knowledge = d(common.WIKI) or d(common.TASKS) or rel == common.INDEX
    if who in ("seb-intake", "seb-digester"):
        if knowledge:
            return True, "edit"
        if d(common.RECORDS):
            return False, "record"
        return False, None
    if who == "seb-gardener":
        if d(common.WIKI):
            return True, "edit"
        if knowledge:
            return False, "edit"
        return False, None
    if who == "seb-reporter":
        return (True, "append") if d(common.REPORTS_DAILY) or d(common.REPORTS_WEEKLY) else (False, None)
    if who == "seb-rules":
        return (False, "edit") if rel == common.RULES else (False, None)
    if who == "seb-restorer":
        return (False, "edit") if knowledge else (False, None)
    return False, None  # основная сессия, seb-answer, seb-challenger


# ================================================================ Write и Edit


def header_groups(head):
    """Строки шапки по полям верхнего уровня: {поле: текст строк}; None — шапка не разбирается."""
    try:
        data = common.load_yaml(head) if head.strip() else {}
    except common.yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    groups, key = {}, None
    for line in head.split("\n"):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if m:
            key = m.group(1)
            groups[key] = groups.get(key, "") + line + "\n"
        elif key is not None:
            groups[key] += line + "\n"
        elif line.strip():
            return None
    return groups


def record_change(old, new, allowed):
    """Что изменено в записи сверх разрешённого: текст или None."""
    oh, ob = common.split_front_matter(old)
    nh, nb = common.split_front_matter(new)
    if oh is None or nh is None:
        return "шапка"
    if ob != nb:
        return "тело"
    og, ng = header_groups(oh), header_groups(nh)
    if og is None or ng is None:
        return "шапка не разбирается"
    changed = sorted(k for k in set(og) | set(ng) if og.get(k) != ng.get(k))
    extra = [k for k in changed if k not in allowed]
    return ", ".join(extra) if extra else None


def decide_file(tool, ti, who, root):
    fp = ti.get("file_path")
    if not isinstance(fp, str) or not fp:
        raise ValueError("нет tool_input.file_path")
    real = os.path.realpath(fp)
    if in_tmp(real):
        return None
    if not under(real, root) or real == root:
        return "deny", R_OUTSIDE.format(path=fp)
    rel = rel_of(real, root)
    if protected(rel, common.engine_files()):
        return "deny", R_PROTECTED.format(path=rel)
    if who is not None and who not in AGENTS:
        return "deny", R_UNKNOWN
    if os.path.isdir(real):
        return "deny", "%s — папка, а не файл." % rel
    exists = os.path.isfile(real)
    can_create, mode = rights(who, rel)
    if not exists and mode == "record":
        return "deny", R_RECORD_NEW.format(path=rel)
    if (not exists and not can_create) or (exists and mode is None):
        if who is None:
            return "deny", R_MAIN.format(path=rel)
        return "deny", R_AGENT.format(who=who, path=rel, zones=ZONES_TEXT[who])
    if not exists:
        return None
    with open(real, encoding="utf-8", errors="surrogateescape") as f:
        old = f.read()
    if tool == "Write":
        new = ti.get("content")
        if not isinstance(new, str):
            raise ValueError("нет tool_input.content")
    else:
        old_s, new_s = ti.get("old_string"), ti.get("new_string")
        if not isinstance(old_s, str) or not isinstance(new_s, str):
            raise ValueError("нет tool_input.old_string или new_string")
        if not old_s or old_s not in old:
            return "deny", R_NOT_FOUND.format(path=rel)
        new = old.replace(old_s, new_s) if ti.get("replace_all") else old.replace(old_s, new_s, 1)
    if mode == "append":
        return None if new.startswith(old) else ("deny", R_APPEND.format(path=rel))
    if mode == "record":
        allowed = RECORD_FIELDS[who]
        what = record_change(old, new, allowed)
        if what:
            return "deny", R_RECORD.format(path=rel, fields=", ".join(sorted(allowed)), what=what)
    return None


# ================================================================ разбор команды

OPS = ("&&", "||", ";;", "|&", "&>>", "&>", ">>", ">|", ">&", "<&", "<<<", "<<-", "<<", "<>",
       ";", "&", "|", "(", ")", "<", ">")
SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "\n"}
REDIR_WRITE = {">", ">>", "&>", "&>>", ">|", "<>"}
REDIRS = REDIR_WRITE | {"<", "<&", ">&", "<<<", "<<", "<<-"}
SUB = "__SEBRA_SUBST__"


def read_balanced(s, i):
    """Текст до парной `)` от позиции i (после `$(`); учитывает кавычки и вложенность."""
    depth, j, n = 1, i, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = s.find("'", j + 1)
            if k < 0:
                raise Unparsed()
            j = k + 1
            continue
        if c == '"':
            j = skip_dquote(s, j)
            continue
        if c == "`":
            k = find_backtick(s, j + 1)
            j = k + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[i:j], j + 1
        j += 1
    raise Unparsed()


def find_backtick(s, i):
    j = i
    while j < len(s):
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == "`":
            return j
        j += 1
    raise Unparsed()


def skip_dquote(s, i):
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        if c == "$" and s.startswith("$(", j):
            _, j = read_balanced(s, j + 2)
            continue
        if c == "`":
            j = find_backtick(s, j + 1) + 1
            continue
        j += 1
    raise Unparsed()


def read_dquote(s, i, subs):
    """Строка в двойных кавычках от s[i] == '"': текст с заменой подстановок и позиция после неё."""
    out, j = ['"'], i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            out.append(s[j:j + 2])
            j += 2
            continue
        if c == '"':
            out.append('"')
            return "".join(out), j + 1
        if c == "$" and s.startswith("$(", j):
            inner, j = read_balanced(s, j + 2)
            subs.append(inner)
            out.append(SUB)
            continue
        if c == "`":
            k = find_backtick(s, j + 1)
            subs.append(s[j + 1:k].replace("\\`", "`"))
            out.append(SUB)
            j = k + 1
            continue
        out.append(c)
        j += 1
    raise Unparsed()


def skip_heredocs(s, i, pending, subs):
    """Пропустить тела heredoc после перевода строки; возвращает позицию после последнего."""
    for delim, quoted, strip_tabs in pending:
        while True:
            if i >= len(s):
                return i
            k = s.find("\n", i)
            line = s[i:] if k < 0 else s[i:k]
            i = len(s) if k < 0 else k + 1
            check = line.lstrip("\t") if strip_tabs else line
            if check == delim:
                break
            if not quoted and ("$(" in line or "`" in line):
                raise Unparsed()
    return i


def tokenize(s, subs):
    tokens, i, n = [], 0, len(s)
    pending, want_delim = [], None
    while i < n:
        c = s[i]
        if c in " \t":
            i += 1
            continue
        if c == "\\" and i + 1 < n and s[i + 1] == "\n":
            i += 2
            continue
        if c == "\n":
            tokens.append(("op", "\n"))
            i += 1
            if pending:
                i = skip_heredocs(s, i, pending, subs)
                pending = []
            continue
        if c == "#":
            k = s.find("\n", i)
            i = n if k < 0 else k
            continue
        op = next((o for o in OPS if s.startswith(o, i)), None)
        if op:
            if op in ("<", ">") and s.startswith("(", i + 1):
                inner, i = read_balanced(s, i + 2)
                subs.append(inner)
                tokens.append(("word", SUB))
                continue
            tokens.append(("op", op))
            i += len(op)
            if op in ("<<", "<<-"):
                want_delim = op
            continue
        raw = []
        while i < n:
            c = s[i]
            if c in " \t\n" or c in ";&|()<>":
                break
            if c == "\\":
                raw.append(s[i:i + 2])
                i += 2
                continue
            if c == "'":
                k = s.find("'", i + 1)
                if k < 0:
                    raise Unparsed()
                raw.append(s[i:k + 1])
                i = k + 1
                continue
            if c == '"':
                text, i = read_dquote(s, i, subs)
                raw.append(text)
                continue
            if c == "`":
                k = find_backtick(s, i + 1)
                subs.append(s[i + 1:k].replace("\\`", "`"))
                raw.append(SUB)
                i = k + 1
                continue
            if c == "$" and s.startswith("$(", i):
                inner, i = read_balanced(s, i + 2)
                subs.append(inner)
                raw.append(SUB)
                continue
            if c == "$" and s.startswith("${", i):
                k = s.find("}", i)
                if k < 0 or "$(" in s[i + 2:k] or "`" in s[i + 2:k]:
                    raise Unparsed()
                raw.append(s[i:k + 1])
                i = k + 1
                continue
            raw.append(c)
            i += 1
        word = "".join(raw)
        if word.isdigit() and i < n and s[i] in "<>":
            continue  # номер дескриптора перед перенаправлением: 2>/dev/null
        try:
            parts = shlex.split(word, posix=True)
        except ValueError:
            raise Unparsed() from None
        if len(parts) != 1:
            raise Unparsed()
        if want_delim:
            pending.append((parts[0], any(q in word for q in "'\"\\"), want_delim == "<<-"))
            want_delim = None
        tokens.append(("word", parts[0]))
    return tokens


def parse(s, depth=0):
    """Команда → список простых команд {"words", "redirs"}, подстановки — отдельными командами."""
    if depth > 10:
        raise Unparsed()
    subs = []
    tokens = tokenize(s, subs)
    commands, cur, i = [], {"words": [], "redirs": []}, 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "op" and value in SEPARATORS:
            if cur["words"] or cur["redirs"]:
                commands.append(cur)
            cur = {"words": [], "redirs": []}
        elif kind == "op" and value in REDIRS:
            if i + 1 >= len(tokens) or tokens[i + 1][0] != "word":
                raise Unparsed()
            cur["redirs"].append((value, tokens[i + 1][1]))
            i += 1
        elif kind == "op":
            raise Unparsed()
        else:
            cur["words"].append(value)
        i += 1
    if cur["words"] or cur["redirs"]:
        commands.append(cur)
    for sub in subs:
        commands.extend(parse(sub, depth + 1))
    return commands


# ================================================================ обёртки

RESERVED = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac"}
SKIP_COMMANDS = {"for", "select", "case", "function"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")


def skip_options(words, i, with_arg, long_with_arg=()):
    """Пропустить параметры с позиции i; вернуть позицию первого не-параметра."""
    while i < len(words):
        w = words[i]
        if w == "--":
            return i + 1
        if not w.startswith("-") or w == "-":
            return i
        if w.startswith("--"):
            name = w.split("=", 1)[0]
            i += 2 if (name in long_with_arg and "=" not in w) else 1
            continue
        letters = w[1:]
        for k, ch in enumerate(letters):
            if ch in with_arg:
                if k == len(letters) - 1:
                    i += 1  # значение — следующее слово; иначе оно приклеено: -n5
                break
        i += 1
    return i


def strip_wrappers(words):
    words = list(words)
    while words:
        w = words[0]
        if ASSIGN.match(w) or w in RESERVED:
            words.pop(0)
            continue
        prog = os.path.basename(w)
        if prog in ("nohup", "builtin"):
            words = words[1:]
        elif prog == "exec":
            words = words[skip_options(words, 1, "a"):]
        elif prog == "command":
            words = words[skip_options(words, 1, ""):]
        elif prog == "time":
            words = words[skip_options(words, 1, "of", ("--output", "--format")):]
        elif prog == "nice":
            i = 1
            if i < len(words) and re.fullmatch(r"-\d+", words[i]):
                i += 1
            words = words[skip_options(words, i, "n", ("--adjustment",)):]
        elif prog == "timeout":
            i = skip_options(words, 1, "sk", ("--signal", "--kill-after"))
            words = words[i + 1:]  # длительность
        elif prog == "stdbuf":
            words = words[skip_options(words, 1, "ioe", ("--input", "--output", "--error")):]
        elif prog == "env":
            i = 1
            while i < len(words):
                a = words[i]
                if a in ("-S", "--split-string") or a.startswith("-S") or a.startswith("--split-string="):
                    raise Unparsed()
                if a == "--":
                    i += 1
                    continue
                if a in ("-u", "--unset", "-C", "--chdir"):
                    i += 2
                    continue
                if a.startswith("-") or ASSIGN.match(a):
                    i += 1
                    continue
                break
            words = words[i:]
        elif prog == "xargs":
            i = skip_options(words, 1, "adEILnPs", ("--arg-file", "--delimiter", "--eof", "--replace",
                                                     "--max-lines", "--max-args", "--max-procs", "--max-chars",
                                                     "--process-slot-var"))
            words = words[i:] or ["echo"]
        elif prog == "sudo":
            i = 1
            while i < len(words) and words[i].startswith("-") and words[i] != "--":
                if words[i] in ("-s", "-i", "--shell", "--login"):
                    return ["sh", "-c"]
                i += 2 if words[i] in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T") else 1
            if i < len(words) and words[i] == "--":
                i += 1
            words = words[i:]
        else:
            break
    return words


# ================================================================ проверка команд


class Ctx:
    def __init__(self, root, cwd, who):
        self.root = root
        self.cwd = cwd
        self.who = who


def resolve(ctx, target, base=None):
    """realpath цели относительно cwd; None — не определить (cwd неизвестен)."""
    base = ctx.cwd if base is None else base
    p = os.path.expanduser(target)
    if not os.path.isabs(p):
        if base is None:
            return None
        p = os.path.join(base, p)
    return os.path.realpath(p)


def in_base(ctx, target, base=None):
    if target in ("-", "") or target.startswith("/dev/"):
        return False
    real = resolve(ctx, target, base)
    if real is None:
        return True  # cwd неизвестен — считать, что внутри базы
    return under(real, ctx.root)


def operands(words, with_arg="", long_with_arg=()):
    """Аргументы команды без параметров (и их значений)."""
    out, i, after = [], 0, False
    while i < len(words):
        w = words[i]
        if after or not w.startswith("-") or w == "-":
            out.append(w)
            i += 1
            continue
        if w == "--":
            after = True
            i += 1
            continue
        i = skip_options(words, i, with_arg, long_with_arg)
        if i < len(words) and words[i - 1] == "--":
            after = True
    return out


def option_value(words, shorts, longs):
    """Значения параметров вида -t X, -tX, --target X, --target=X."""
    vals, i = [], 0
    while i < len(words):
        w = words[i]
        if w == "--":
            break
        for lg in longs:
            if w == lg and i + 1 < len(words):
                vals.append(words[i + 1])
            elif w.startswith(lg + "="):
                vals.append(w[len(lg) + 1:])
        if w.startswith("-") and not w.startswith("--"):
            letters = w[1:]
            for k, ch in enumerate(letters):
                if ch in shorts:
                    rest = letters[k + 1:]
                    vals.append(rest if rest else (words[i + 1] if i + 1 < len(words) else ""))
                    break
        i += 1
    return vals




PDFTOTEXT_VALUE = {"-f", "-l", "-r", "-x", "-y", "-W", "-H", "-enc", "-eol", "-opw", "-upw", "-fixed"}
UNZIP_NO_WRITE = set("lptvzZ")


def pdftotext_target(args):
    ops, i = [], 0
    while i < len(args):
        a = args[i]
        if a in PDFTOTEXT_VALUE:
            i += 2
            continue
        if a.startswith("-") and a != "-":
            i += 1
            continue
        ops.append(a)
        i += 1
    if len(ops) >= 2:
        return [] if ops[1] == "-" else [ops[1]]
    if len(ops) == 1 and ops[0] != "-":
        # без второго аргумента pdftotext пишет <файл>.txt рядом с PDF
        return [re.sub(r"\.pdf$", "", ops[0], flags=re.I) + ".txt"]
    return []


def unzip_targets(args):
    for a in args:
        if a.startswith("-") and not a.startswith("--") and set(a[1:]) & UNZIP_NO_WRITE:
            return []  # список, проверка, вывод в stdout — не распаковка
    return option_value(args, "d", ()) or ["."]


def tar_targets(args):
    first = args[0] if args else ""
    extract = any(a in ("--extract", "--get") or (a.startswith("-") and not a.startswith("--") and "x" in a[1:])
                  for a in args)
    if first and not first.startswith("-") and re.fullmatch(r"[A-Za-z]+", first) and "x" in first:
        extract = True  # старая запись без дефиса: tar xzf a.tgz
    if not extract:
        return []
    return option_value(args, "C", ("--directory",)) or ["."]


def write_targets(prog, args, ctx):
    """Куда команда пишет файлы: список путей (или "." — текущая папка)."""
    if prog == "tee":
        return operands(args, "", ("--output-error",))
    if prog in ("cp", "install", "ln"):
        arg_opts = {"cp": "tS", "install": "tSmog", "ln": "tS"}[prog]
        longs = ("--target-directory", "--suffix", "--mode", "--owner", "--group")
        t = option_value(args, "t", ("--target-directory",))
        if t:
            return t
        ops = operands(args, arg_opts, longs)
        if prog == "install" and any(a == "-d" or a == "--directory" for a in args):
            return ops
        if len(ops) >= 2:
            return [ops[-1]]
        return ["."] if prog == "ln" and ops else []
    if prog == "touch":
        return operands(args, "dtr", ("--date", "--reference"))
    if prog == "truncate":
        return operands(args, "sr", ("--size", "--reference"))
    if prog == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if prog == "sed":
        inplace = any(a == "--in-place" or a.startswith("--in-place=") or
                      (a.startswith("-") and not a.startswith("--") and "i" in a[1:].split("e")[0].split("f")[0])
                      for a in args)
        if not inplace:
            return []
        has_script = bool(option_value(args, "ef", ("--expression", "--file")))
        ops = operands(args, "efl", ("--expression", "--file", "--line-length"))
        return ops if has_script else ops[1:]
    if prog == "curl":
        out = option_value(args, "o", ("--output",))
        outdir = option_value(args, "", ("--output-dir",))
        if any(a in ("-O", "--remote-name", "--remote-name-all") or
               (a.startswith("-") and not a.startswith("--") and "O" in a) for a in args):
            out += outdir or ["."]
        return out + outdir
    if prog == "wget":
        out = [v for v in option_value(args, "O", ("--output-document",)) if v != "-"]
        out += option_value(args, "oa", ("--output-file", "--append-output"))
        prefix = option_value(args, "P", ("--directory-prefix",))
        if not option_value(args, "O", ("--output-document",)):
            out += prefix or ["."]
        return out
    if prog == "pdftotext":
        return pdftotext_target(args)
    if prog == "unzip":
        return unzip_targets(args)
    if prog == "tar":
        return tar_targets(args)
    return []


GIT_GLOBAL_OK = ("--no-pager", "-P", "--no-optional-locks")
GIT_READ = {"status", "log", "diff", "show", "ls-files", "ls-tree", "ls-remote", "rev-parse", "rev-list",
            "merge-base", "cat-file", "blame", "grep", "shortlog", "describe"}
MAIN_FETCH = ("+refs/heads/main:refs/remotes/origin/main", "refs/heads/main:refs/remotes/origin/main")
FETCH_DENY = ("--update-head-ok", "-u", "--refmap", "--upload-pack")


def check_git(args, ctx):
    i, gcwd = 0, ctx.cwd
    while i < len(args):
        a = args[i]
        if a == "-C":
            if i + 1 >= len(args):
                return B_UNPARSED
            gcwd = resolve(ctx, args[i + 1], gcwd)
            i += 2
            continue
        if a in GIT_GLOBAL_OK:
            i += 1
            continue
        if a.startswith("-"):
            # -c core.pager=…, core.fsmonitor, alias.*=!… запускают произвольную команду
            return B_GIT_OPTION.format(opt=a.split("=", 1)[0])
        break
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    if sub in GIT_READ:
        for a in rest:
            name = a.split("=", 1)[0]
            if name in ("--ext-diff", "--textconv"):
                return "git %s %s: внешние программы сравнения не разрешены" % (sub, name)
            if sub == "grep" and (name == "--open-files-in-pager" or re.fullmatch(r"-O.*", a)):
                return "git grep -O: запуск программы не разрешён"
        for target in option_value(rest, "", ("--output",)):
            if in_base(ctx, target, gcwd):
                return B_WRITE
        return None
    if sub == "fetch":
        for a in rest:
            name = a.split("=", 1)[0]
            if name in FETCH_DENY:
                return "git fetch %s: не разрешено" % name
            if not a.startswith("-") and ":" in a and a not in MAIN_FETCH and not re.match(r"^[a-z]+://", a) \
                    and not re.match(r"^[^/:]+@[^:]+:", a):
                return "git fetch %s: refspec с «:» — только +refs/heads/main:refs/remotes/origin/main" % a
        return None
    if sub == "branch":
        allowed = {"--show-current", "--list", "-l", "--all", "--remotes", "--verbose"}
        opts = [a for a in rest if a.startswith("-")]
        positional = [a for a in rest if not a.startswith("-")]
        if all(a in allowed or re.fullmatch(r"-[arvl]+", a) for a in opts) and \
                (not positional or "--list" in opts or any(re.fullmatch(r"-[arv]*l[arv]*", a) for a in opts)):
            return None
        return B_GIT.format(sub="branch " + " ".join(opts)).rstrip()
    if sub == "remote":
        if rest in ([], ["-v"], ["--verbose"]) or (rest and rest[0] in ("get-url", "show")):
            return None
        return B_GIT.format(sub="remote" + (" " + rest[0] if rest else ""))
    if sub == "restore" and ctx.who == "seb-restorer":
        return check_restore(rest, ctx, gcwd)
    return B_GIT.format(sub=sub)


def check_restore(rest, ctx, gcwd):
    source, paths, after, i = None, [], False, 0
    while i < len(rest):
        a = rest[i]
        if after:
            paths.append(a)
        elif a == "--":
            after = True
        elif a.startswith("--source="):
            source = a.split("=", 1)[1]
        elif a in ("--source", "-s"):
            source = rest[i + 1] if i + 1 < len(rest) else None
            i += 1
        elif a.startswith("-s") and len(a) > 2:
            source = a[2:]
        elif a in ("--worktree", "-W"):
            pass
        elif a.startswith("-"):
            return B_RESTORE
        else:
            paths.append(a)
        i += 1
    if not source or not paths:
        return B_RESTORE
    engine = common.engine_files()
    for p in paths:
        if any(ch in p for ch in "*?[") or p.startswith(":"):
            return B_RESTORE
        real = resolve(ctx, p, gcwd)
        if real is None or not under(real, ctx.root):
            return B_RESTORE
        rel = rel_of(real, ctx.root)
        if rel in engine or not is_data(rel) or under(rel, common.LOG):
            return B_RESTORE
    return None


def check_python(words, args, ctx):
    if words[0] == "python3" and args == ["--version"]:
        return None
    if words[0] == "python3" and args and re.fullmatch(r"(?:\./)?engine/scripts/[A-Za-z0-9_]+\.py", args[0]):
        real = resolve(ctx, args[0])
        script = os.path.join(ctx.root, common.ENGINE_DIR, "scripts", os.path.basename(args[0]))
        if real is not None and real == os.path.realpath(script) and os.path.isfile(script):
            if os.path.basename(script) == "config.py" and ctx.who:
                sub = next((a for a in args[1:] if not a.startswith("-")), None)
                if sub in ("init", "set", "migrate"):
                    return B_CONFIG
            return None
    return B_PYTHON


def check_command(cmd, ctx):
    for op, target in cmd["redirs"]:
        if op in REDIR_WRITE or (op == ">&" and not target.isdigit() and target != "-"):
            if in_base(ctx, target):
                return B_WRITE
    words = strip_wrappers(cmd["words"])
    if not words:
        return None
    prog, args = os.path.basename(words[0]), words[1:]
    if words[0] in SKIP_COMMANDS:
        return None
    if prog in ("cd", "pushd"):
        ops = operands(args)
        if not ops or ops[0] == "-" or "$" in ops[0] or SUB in ops[0]:
            ctx.cwd = None
        else:
            ctx.cwd = resolve(ctx, ops[0])
        return None
    if prog == "popd":
        ctx.cwd = None
        return None
    if prog in ("rm", "rmdir", "unlink", "mv"):
        return B_DELETE.format(prog=prog)
    if prog == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args):
        return B_DELETE.format(prog="find")
    if prog in ("sh", "bash", "zsh", "dash", "ksh", "eval", "source", "."):
        return B_SHELL.format(prog=prog)
    if re.fullmatch(r"python[0-9.]*", prog):
        return check_python(words, args, ctx)
    if prog == "git":
        return check_git(args, ctx)
    for target in write_targets(prog, args, ctx):
        if in_base(ctx, target):
            return B_WRITE
    return None


def decide_bash(ti, inp, root):
    command = ti.get("command")
    if not isinstance(command, str):
        raise ValueError("нет tool_input.command")
    cwd = inp.get("cwd") or root
    ctx = Ctx(root, os.path.realpath(cwd), inp.get("agent_type"))
    try:
        commands = parse(command)
    except (Unparsed, RecursionError):
        return "deny", B_UNPARSED
    for cmd in commands:
        try:
            reason = check_command(cmd, ctx)
        except Unparsed:
            return "deny", B_UNPARSED
        if reason:
            return "deny", reason
    return None


# ================================================================ вход и выход


def decide(inp):
    if not isinstance(inp, dict):
        raise ValueError("вход не объект JSON")
    root = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or common.ROOT)
    common.ROOT = root
    tool, ti = inp.get("tool_name"), inp.get("tool_input")
    if tool not in ("Write", "Edit", "Bash"):
        return None
    if not isinstance(ti, dict):
        raise ValueError("нет tool_input")
    if tool == "Bash":
        return decide_bash(ti, inp, root)
    return decide_file(tool, ti, inp.get("agent_type"), root)


def main():
    try:
        inp = json.loads(sys.stdin.read())
        decision = decide(inp)
    except Exception as e:  # noqa: BLE001 — любая ошибка хука — запрет
        message = getattr(e, "message", None) or str(e) or type(e).__name__
        print("guard.py: внутренняя ошибка: %s" % message, file=sys.stderr)
        sys.exit(2)
    if decision:
        kind, reason = decision
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                                 "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
