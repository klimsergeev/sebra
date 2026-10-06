"""Новая запись data/records/ — единственный способ её создать: тело переносит скрипт, а не модель. Коммит не делает.

python3 engine/scripts/record.py new --who seb-intake --type <type> --captured "<ГГГГ-ММ-ДДTЧЧ:ММ>" --slug <slug>
    --title "<…>" [--pointer "<…>"] [--why "<…>"] --status <done | processing | unreadable> [--size "<…>"]
    [--error "<…>"] [--body-file <файл в /tmp/sebra/> …] [--json]

Файл — шапка по формату «Запись», затем тело со следующей строки после закрывающей `---`. После записи —
lint.py по этому пути: нарушение — файл удаляется, выход 1. Иначе строка журнала «запись создана».
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

import common
from common import Fail

TYPES = ("text", "link", "message", "image", "document", "file", "audio", "video")
STATUSES = ("done", "processing", "unreadable")
TMP_ROOTS = sorted({"/tmp/sebra", os.path.realpath("/tmp/sebra")})
IMAGE_LINE = "Что на картинке: "


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


def run_lint(rel):
    script = common.path(common.ENGINE_DIR, "scripts", "lint.py")
    try:
        cp = subprocess.run([sys.executable, script, rel, "--json"], cwd=common.ROOT, capture_output=True,
                            encoding="utf-8", errors="replace", timeout=300)
        data = json.loads(cp.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return 3, None, "проверка lint.py не запустилась: %s" % e
    if cp.returncode not in (0, 1) or not isinstance(data, dict):
        return 3, None, "проверка lint.py не прошла: код %d, %s" % (cp.returncode, data)
    return cp.returncode, data.get("violations") or [], ""


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
    if args.status not in STATUSES:
        raise Fail(2, "--status: одно из %s" % ", ".join(STATUSES))
    if not re.fullmatch(common.SLUG, args.slug or ""):
        raise Fail(2, "--slug: от одного до шести слов [a-z0-9] через дефис, а не «%s»" % args.slug)
    files = args.body_file or []
    for p in files:
        if not under_tmp(p):
            raise Fail(2, "--body-file: только файлы в /tmp/sebra/, а не %s" % p)
        if not os.path.isfile(p):
            raise Fail(2, "--body-file: нет файла %s" % p)
    if args.status == "unreadable" and files:
        raise Fail(2, "--status unreadable: без --body-file, тело пишет скрипт")
    if args.status != "unreadable" and not files:
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
    if args.type in ("audio", "video") and args.status != "unreadable":
        raise Fail(1, "%s — только --status unreadable: аудио и видео не читаются" % args.type)
    if args.status == "unreadable":
        if not fields["error"].strip():
            raise Fail(1, "--status unreadable: нужен непустой --error с причиной")
        body = "Содержимое не читается: %s\n" % fields["error"].strip()
    else:
        body = read_body(files)
        if args.type == "image" and not any(line.startswith(IMAGE_LINE) for line in body.split("\n")):
            raise Fail(1, "image: в теле нет строки, начинающейся с «%s»" % IMAGE_LINE)

    head = [
        "---",
        "type: %s" % args.type,
        "captured: %s" % quoted(args.captured),
        "title: %s" % quoted(fields["title"]),
        "pointer: %s" % quoted(fields["pointer"]),
        "why: %s" % quoted(fields["why"]),
        "status: %s" % args.status,
        "seen: []",
        "size: %s" % quoted(fields["size"]),
        "error: %s" % quoted(fields["error"]),
        "---",
    ]
    name_base = "%s-%s%s-%s" % (m.group(1), m.group(2), m.group(3), args.slug)
    rel = create(name_base, "\n".join(head) + "\n" + body)

    code, violations, problem = run_lint(rel)
    if code != 0:
        os.remove(common.path(rel))
        result = {"path": None, "line": "", "violations": violations or []}
        if code == 3:
            result["error"] = problem
            raise_with(args, result, 3, problem)
        result["error"] = "запись не прошла проверку lint.py; файл удалён"
        raise_with(args, result, 1, result["error"] + "".join(
            "\n%s — %s — %s" % (v.get("path"), v.get("rule"), v.get("message")) for v in violations))
    _, line = common.journal_add("seb-intake", "запись создана", rel)
    if args.json:
        print(json.dumps({"path": rel, "line": line, "violations": []}, ensure_ascii=False))
    else:
        print(rel)
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
    s.add_argument("--status", required=True)
    s.add_argument("--size", default="")
    s.add_argument("--error", default="")
    s.add_argument("--body-file", action="append")
    s.add_argument("--json", action="store_true")
    s.error = p.error
    args = p.parse_args()
    if args.cmd != "new":
        raise Fail(2, "команда: new")
    return cmd_new(args)


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
