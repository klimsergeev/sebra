"""Новая запись data/records/ — единственный способ её создать: тело переносит скрипт, а не модель. Коммит не делает.

python3 engine/scripts/record.py new --who seb-intake --type <type> --captured "<ГГГГ-ММ-ДДTЧЧ:ММ>" --slug <slug>
    --title "<…>" [--pointer "<…>"] [--why "<…>"] [--status <done | processing | unreadable>] [--size "<…>"]
    [--error "<…>"] [--body-file <файл в /tmp/sebra/> …] [--json]
python3 engine/scripts/record.py duplicate <путь записи в data/records/> [--json]

new: сначала ищет повтор (CLAUDE.md, правило 3) среди data/records/*.md — у ссылки по адресу, у остального по телу
без строк «Что на картинке» и без пробельных знаков. Нашёл — новой записи нет: сегодняшняя дата дописывается в конец
seen найденной записи (раз в день), lint.py по ней, строка журнала «повтор», ответ — путь найденной записи.
Иначе size: у link, message, file — число слов тела; у document — --size, как передан; у остальных и у unreadable —
"". status: --status, если передан; без него — done, а у text, link, message, file больше big_docs.text_words слов —
processing. Файл — шапка по формату записи formats.md, затем тело со следующей строки после закрывающей `---`.
После записи — lint.py по этому пути: нарушение — файл удаляется, выход 1. Иначе строка журнала «запись создана».

duplicate: тот же поиск повтора для записи, которая уже лежит в data/records/, — без правок и без строки журнала.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

import common
from common import Fail

TYPES = ("text", "link", "message", "image", "document", "file", "audio", "video")
STATUSES = ("done", "processing", "unreadable")
TMP_ROOTS = sorted({"/tmp/sebra", os.path.realpath("/tmp/sebra")})
IMAGE_LINE = "Что на картинке: "
WORD_TYPES = ("text", "link", "message", "file")   # measured in words (formats.md)
SIZE_IN_WORDS = ("link", "message", "file")        # text has no measure: size ""
HEAD_FIELD = re.compile(r'^(type|captured|pointer|status):[ \t]*"?([^"\n]*?)"?[ \t]*$')
SEEN_LINE = re.compile(r'^seen:[ \t]*\[(.*)\][ \t]*$')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


def under_tmp(p):
    real = os.path.realpath(p)
    return any(real.startswith(d + "/") for d in TMP_ROOTS)


def field_text(name, value):
    """Значение поля шапки: перевод строки — пробел; прямая кавычка — отказ."""
    value = re.sub(r"\r\n|\r|\n", " ", value or "")
    if '"' in value:
        raise Fail(1, "%s: прямая кавычка \" в значении — замени на ёлочки «»" % name)
    return value


def quoted(value):
    return '"%s"' % value.replace("\\", "\\\\")


def read_body(files):
    parts = []
    for p in files:
        with open(p, "rb") as f:
            data = f.read()
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Fail(1, "%s: не UTF-8" % p) from None
        parts.append(text)
    text = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def words(text):
    """Word count as in extract.py and fetch_page.py: chunks between whitespace, locale-independent."""
    return len(text.split())


def text_words_limit():
    """big_docs.text_words. No fallback: an unreadable config.yaml already stops record.py new at common.now()."""
    return common.config()["big_docs.text_words"]


def address_key(url):
    """Address for the duplicate check: no http(s)://, no #…, no utm_* parameters, no trailing /.

    Not a web address — None.
    """
    m = re.match(r"(?i)https?://", (url or "").strip())
    if not m:
        return None
    rest = url.strip()[m.end():].split("#", 1)[0]
    if "?" in rest:
        rest, query = rest.split("?", 1)
        kept = [p for p in query.split("&") if p and not p.startswith("utm_")]
        if kept:
            rest += "?" + "&".join(kept)
    return rest.rstrip("/")


def body_key(text):
    """Body for the duplicate check: without «Что на картинке: …» lines and without any whitespace."""
    kept = [line for line in text.split("\n") if not line.startswith(IMAGE_LINE)]
    return "".join("\n".join(kept).split())


def head_fields(head):
    """Fields type, captured, pointer, status of a record header text; quotes removed, \\\\ — \\."""
    fields = {}
    for line in (head or "").split("\n"):
        m = HEAD_FIELD.match(line.rstrip("\r"))
        if m:
            fields[m.group(1)] = m.group(2).replace("\\\\", "\\")
    return fields


def saved_records(skip=None):
    """(path, fields type/captured/pointer/status, body) of data/records/*.md by name; skip — a path to leave out."""
    folder = common.path(common.RECORDS)
    if not os.path.isdir(folder):
        return
    for name in sorted(os.listdir(folder)):
        rel = "%s/%s" % (common.RECORDS, name)
        full = os.path.join(folder, name)
        if name.startswith(".") or not name.endswith(".md") or rel == skip or not os.path.isfile(full):
            continue
        with open(full, encoding="utf-8", errors="replace", newline="") as f:
            head, body = common.split_front_matter(f.read())
        if head is not None:
            yield rel, head_fields(head), body


def find_duplicates(type_, pointer, body, skip=None):
    """[(path, fields)] of saved records that repeat the material (CLAUDE.md, rule 3), by file name.

    A link — by address_key of pointer; anything else with a body — by body_key; body None or an empty key — none.
    """
    if type_ == "link":
        key = address_key(pointer)
        if key is None:
            return []
        return [(rel, f) for rel, f, _ in saved_records(skip) if address_key(f.get("pointer")) == key]
    key = body_key(body) if body is not None else ""
    if not key:
        return []
    # the normalized body is never longer than the raw one: a cheap filter before normalizing
    return [(rel, f) for rel, f, b in saved_records(skip) if len(b) >= len(key) and body_key(b) == key]


def add_seen(rel, today):
    """Append today to the end of seen of record rel. Returns (added, old text). Fail(1) — no one-line seen: [ … ]."""
    with open(common.path(rel), encoding="utf-8", newline="") as f:
        old = f.read()
    lines = old.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r") == "---"), None)
    found = None
    if lines and lines[0].rstrip("\r") == "---" and end is not None:
        found = next((i for i in range(1, end) if SEEN_LINE.match(lines[i].rstrip("\r"))), None)
    if found is None:
        raise Fail(1, "%s: в шапке нет строки seen: [ … ] — дату повтора не дописать" % rel)
    dates = re.findall(r'"(\d{4}-\d{2}-\d{2})"', SEEN_LINE.match(lines[found].rstrip("\r")).group(1))
    if dates and dates[-1] >= today:
        return False, old
    tail = "\r" if lines[found].endswith("\r") else ""
    lines[found] = "seen: [%s]%s" % (", ".join('"%s"' % d for d in dates + [today]), tail)
    common.write_atomic(rel, "\n".join(lines))
    return True, old


def run_lint(rel):
    return common.run_lint([rel])


def create(name_base, text):
    """Файл data/records/<name_base>.md, занято — -2, -3, …; создание O_CREAT | O_EXCL."""
    folder = common.path(common.RECORDS)
    os.makedirs(folder, exist_ok=True)
    n = 1
    while True:
        name = "%s.md" % name_base if n == 1 else "%s-%d.md" % (name_base, n)
        full = os.path.join(folder, name)
        try:
            fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            n += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return "%s/%s" % (common.RECORDS, name)


def cmd_new(args):
    if args.who != "seb-intake":
        raise Fail(2, "--who: записи создаёт только seb-intake, а не «%s»" % args.who)
    if args.type not in TYPES:
        raise Fail(2, "--type: одно из %s" % ", ".join(TYPES))
    if args.status is not None and args.status not in STATUSES:
        raise Fail(2, "--status: одно из %s" % ", ".join(STATUSES))
    status = args.status or "done"
    if not re.fullmatch(common.SLUG, args.slug or ""):
        raise Fail(2, "--slug: от одного до шести слов [a-z0-9] через дефис, а не «%s»" % args.slug)
    files = args.body_file or []
    for p in files:
        if not under_tmp(p):
            raise Fail(2, "--body-file: только файлы в /tmp/sebra/, а не %s" % p)
        if not os.path.isfile(p):
            raise Fail(2, "--body-file: нет файла %s" % p)
    if status == "unreadable" and files:
        raise Fail(2, "--status unreadable: без --body-file, тело пишет скрипт")
    if status != "unreadable" and not files:
        raise Fail(2, "нужен --body-file с текстом записи (или --status unreadable)")

    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})", args.captured or "")
    try:
        captured = dt.datetime.strptime(args.captured, "%Y-%m-%dT%H:%M") if m else None
    except ValueError:
        captured = None
    if captured is None:
        raise Fail(1, "--captured: нужна настоящая дата со временем ГГГГ-ММ-ДДTЧЧ:ММ, а не «%s»" % args.captured)
    current = common.now()
    if captured > current.replace(tzinfo=None):
        raise Fail(1, "--captured %s позже, чем сейчас (%s)" % (args.captured, common.fmt_dt(current)))

    fields = {}
    for name in ("title", "pointer", "why", "size", "error"):
        fields[name] = field_text(name, getattr(args, name))
    if not fields["title"].strip():
        raise Fail(1, "--title: нужен непустой текст")
    if args.type in ("audio", "video") and status != "unreadable":
        raise Fail(1, "%s — только --status unreadable: аудио и видео не читаются" % args.type)
    if status == "unreadable":
        if not fields["error"].strip():
            raise Fail(1, "--status unreadable: нужен непустой --error с причиной")
        body = "Содержимое не читается: %s\n" % fields["error"].strip()
    else:
        body = read_body(files)
        if args.type == "image" and not any(line.startswith(IMAGE_LINE) for line in body.split("\n")):
            raise Fail(1, "image: в теле нет строки, начинающейся с «%s»" % IMAGE_LINE)

    found = find_duplicates(args.type, fields["pointer"], None if status == "unreadable" else body)
    if found:
        return answer_duplicate(args, *found[0])

    size = ""
    if status != "unreadable":
        count = words(body)
        if args.type in SIZE_IN_WORDS:
            size = "%d слов" % count
        elif args.type == "document":
            size = fields["size"]
        if status == "done" and args.type in WORD_TYPES and count > text_words_limit():
            status = "processing"

    head = [
        "---",
        "type: %s" % args.type,
        "captured: %s" % quoted(args.captured),
        "title: %s" % quoted(fields["title"]),
        "pointer: %s" % quoted(fields["pointer"]),
        "why: %s" % quoted(fields["why"]),
        "status: %s" % status,
        "seen: []",
        "size: %s" % quoted(size),
        "error: %s" % quoted(fields["error"]),
        "---",
    ]
    name_base = "%s-%s%s-%s" % (m.group(1), m.group(2), m.group(3), args.slug)
    rel = create(name_base, "\n".join(head) + "\n" + body)

    code, violations, problem = run_lint(rel)
    if code != 0:
        os.remove(common.path(rel))
        result = {"path": None, "duplicate": False, "line": "", "violations": violations or []}
        if code == 3:
            result["error"] = problem
            raise_with(args, result, 3, problem)
        result["error"] = "запись не прошла проверку lint.py; файл удалён"
        raise_with(args, result, 1, result["error"] + "".join(
            "\n%s — %s — %s" % (v.get("path"), v.get("rule"), v.get("message")) for v in violations))
    _, line = common.journal_add("seb-intake", "запись создана", rel)
    if args.json:
        print(json.dumps({"path": rel, "duplicate": False, "status": status, "size": size, "line": line,
                          "violations": []}, ensure_ascii=False))
    else:
        print(rel)
    return 0


def answer_duplicate(args, rel, fields):
    """The material repeats saved record rel: today's date into its seen, lint.py, the «повтор» journal line, answer."""
    try:
        added, old = add_seen(rel, common.now().strftime("%Y-%m-%d"))
    except Fail as e:
        # the record exists: the answer says so, otherwise the agent would repeat the same failing call
        raise_with(args, {"path": None, "duplicate": True, "line": "", "violations": [], "error": e.message},
                   e.code, e.message)
    if added:
        code, violations, problem = run_lint(rel)
        if code != 0:
            common.write_atomic(rel, old)
            error = problem if code == 3 else "дата повтора в %s не прошла проверку lint.py; запись не изменена" % rel
            result = {"path": None, "duplicate": True, "line": "", "violations": violations or [], "error": error}
            raise_with(args, result, code if code == 3 else 1, error + "".join(
                "\n%s — %s — %s" % (v.get("path"), v.get("rule"), v.get("message")) for v in violations or []))
    _, line = common.journal_add("seb-intake", "повтор", rel)
    if args.json:
        print(json.dumps({"path": rel, "duplicate": True, "captured": fields.get("captured", ""), "seen_added": added,
                          "line": line, "violations": []}, ensure_ascii=False))
    else:
        print("повтор: %s" % rel)
    return 0


def cmd_duplicate(args):
    """Saved records that repeat record args.path (as new does), without changes and without a journal line."""
    rel = common.rel_path(args.path)
    if rel is None or os.path.dirname(rel) != common.RECORDS or not rel.endswith(".md") \
            or not os.path.isfile(common.path(rel)):
        raise Fail(2, "duplicate: нужен путь к записи в data/records/, а не %s" % args.path)
    with open(common.path(rel), encoding="utf-8", errors="replace", newline="") as f:
        head, body = common.split_front_matter(f.read())
    fields = head_fields(head)
    body = None if head is None or fields.get("status") == "unreadable" else body
    same = [r for r, _ in find_duplicates(fields.get("type"), fields.get("pointer"), body, skip=rel)]
    if args.json:
        print(json.dumps({"path": rel, "duplicates": same}, ensure_ascii=False))
    else:
        print("\n".join(same) or "нет")
    return 0


def raise_with(args, result, code, text):
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(code)
    raise Fail(code, text)


def main():
    p = Parser(prog="record.py")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("new")
    s.add_argument("--who", required=True)
    s.add_argument("--type", required=True)
    s.add_argument("--captured", required=True)
    s.add_argument("--slug", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--pointer", default="")
    s.add_argument("--why", default="")
    s.add_argument("--status")
    s.add_argument("--size", default="")
    s.add_argument("--error", default="")
    s.add_argument("--body-file", action="append")
    s.add_argument("--json", action="store_true")
    s.error = p.error
    d = sub.add_parser("duplicate")
    d.add_argument("path")
    d.add_argument("--json", action="store_true")
    d.error = p.error
    args = p.parse_args()
    handlers = {"new": cmd_new, "duplicate": cmd_duplicate}
    if args.cmd not in handlers:
        raise Fail(2, "команда: new или duplicate")
    return handlers[args.cmd](args)


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
