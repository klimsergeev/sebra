"""Напоминания на день: data/reminders/<ГГГГ-ММ-ДД>.md. Единственный, кто их пишет. Коммит не делает.

python3 engine/scripts/remind.py add --who <seb-intake | seb-digester> --date <ГГГГ-ММ-ДД> --text "<что напомнить>" --record <путь записи> [--json]
python3 engine/scripts/remind.py cancel --who <seb-intake | seb-restorer> --date <ГГГГ-ММ-ДД> --record <путь записи> [--text "<начало текста>"] [--json]
python3 engine/scripts/remind.py show --through <ГГГГ-ММ-ДД> [--json]

Строка файла — `- <текст> — [запись](../records/<имя записи>.md)`. Сегодня — по поясу пользователя.
"""

import argparse
import errno
import fcntl
import json
import os
import re
import sys
import time

import common
from common import Fail

LOCK_WAIT = 60
RECORD_PATH = re.compile(r"^data/records/([^()\s<>/]+\.md)$")
FILE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


def parse_day(text, what="--date"):
    try:
        return common.parse_day(text)
    except ValueError:
        raise Fail(2, "%s: нужна настоящая дата ГГГГ-ММ-ДД, а не «%s»" % (what, text)) from None


def day_file(day):
    return "%s/%s.md" % (common.REMINDERS, day.isoformat())


def record_name(record):
    """Имя записи из пути data/records/<имя>.md; путь не той формы — Fail(2)."""
    rel = common.rel_path(record) if record else None
    m = RECORD_PATH.match(rel or "")
    if not m:
        raise Fail(2, "--record: нужен путь к записи data/records/<имя>.md, а не «%s»" % record)
    return rel, m.group(1)


def lock():
    """flock на $(git rev-parse --git-path sebra-remind.lock): блокировка вне папки данных."""
    rel = common.git_out("rev-parse", "--git-path", "sebra-remind.lock")
    f = open(os.path.join(common.ROOT, rel), "a")
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if time.monotonic() >= deadline:
                f.close()
                raise Fail(3, "другая запись напоминаний ещё идёт")
            time.sleep(0.2)


def read_lines(rel):
    try:
        with open(common.path(rel), encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return None


def clean_text(text):
    text = re.sub(r"\r\n|\r|\n", " ", text or "")
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text.replace(" — [", " — (")


def check_who(who, allowed):
    if who not in allowed:
        raise Fail(2, "--who: только %s, а не «%s»" % (" или ".join(allowed), who))


def cmd_add(args):
    check_who(args.who, ("seb-intake", "seb-digester"))
    day = parse_day(args.date)
    rec_rel, name = record_name(args.record)
    if not os.path.isfile(common.path(rec_rel)):
        raise Fail(2, "--record: нет записи %s" % rec_rel)
    text = clean_text(args.text)
    if not text:
        raise Fail(2, "--text: нужен текст напоминания")
    if day < common.now().date():
        raise Fail(1, "дата %s уже прошла: напоминание ставится на сегодня или позже" % day.isoformat())
    line = "- %s — [запись](../records/%s)" % (text, name)
    if not common.REMINDER_RE.match(line):
        raise Fail(2, "строка напоминания не по формату: %s" % line)
    rel = day_file(day)
    handle = lock()
    try:
        lines = read_lines(rel) or []
        if line not in lines:
            common.write_atomic(rel, "\n".join(lines + [line]) + "\n")
            common.journal_add(args.who, "напоминание", rel, text)
    finally:
        handle.close()
    if args.json:
        print(json.dumps({"file": rel, "line": line}, ensure_ascii=False))
    else:
        print("%s: %s" % (rel, line))
    return 0


def cmd_cancel(args):
    check_who(args.who, ("seb-intake", "seb-restorer"))
    day = parse_day(args.date)
    _, name = record_name(args.record)
    prefix = None
    if args.text is not None:
        prefix = "- " + clean_text(args.text)
    rel = day_file(day)
    handle = lock()
    try:
        lines = read_lines(rel)
        removed, kept = [], []
        for line in lines or []:
            m = common.REMINDER_RE.match(line)
            if m and m.group("rec") == name and (prefix is None or line.startswith(prefix)):
                removed.append(line)
            else:
                kept.append(line)
        if not removed:
            raise Fail(1, "в %s нет напоминания со ссылкой на %s%s" % (
                rel, name, "" if prefix is None else " и текстом «%s»" % prefix[2:]))
        deleted = not [x for x in kept if x.strip()]
        if deleted:
            os.remove(common.path(rel))
        else:
            common.write_atomic(rel, "\n".join(kept) + "\n")
        common.journal_add(args.who, "напоминание отменено", rel)
    finally:
        handle.close()
    if args.json:
        print(json.dumps({"file": rel, "removed": removed, "deleted": deleted}, ensure_ascii=False))
    else:
        print("%s: убрано %d, файл %s" % (rel, len(removed), "удалён" if deleted else "остался"))
    return 0


def day_files(through):
    """[(дата, путь)] файлов data/reminders/ с датой не позже through; ошибки имён — в список."""
    out, errors = [], []
    folder = common.path(common.REMINDERS)
    if not os.path.isdir(folder):
        return out, errors
    for name in sorted(os.listdir(folder)):
        rel = "%s/%s" % (common.REMINDERS, name)
        m = FILE_NAME.match(name)
        try:
            day = common.parse_day(m.group(1)) if m else None
        except ValueError:
            day = None
        if day is None:
            errors.append("%s: имя не по формату <ГГГГ-ММ-ДД>.md" % rel)
            continue
        if day <= through:
            out.append((day, rel))
    return out, errors


def drop_lines(items, who):
    """Убирает ровно показанные строки: items — [{"file": data/reminders/<дата>.md, "line": строка}].

    Строки, которых уже нет (убрала другая сессия, отменил пользователь), пропускаются; опустевший файл удаляется;
    на каждый изменённый файл — строка журнала «напоминания показаны». Возвращает (изменённые, удалённые).
    """
    wanted = {}
    for it in items:
        rel, line = it.get("file"), it.get("line")
        name = os.path.basename(rel or "")
        if rel != "%s/%s" % (common.REMINDERS, name) or not FILE_NAME.match(name) or not isinstance(line, str):
            raise Fail(2, "строка напоминания не из data/reminders/: %r" % (it,))
        wanted.setdefault(rel, set()).add(line)
    changed, deleted = [], []
    handle = lock()
    try:
        for rel in sorted(wanted):
            lines = read_lines(rel)
            if lines is None:
                continue
            kept = [x for x in lines if x not in wanted[rel]]
            if len(kept) == len(lines):
                continue
            if not [x for x in kept if x.strip()]:
                os.remove(common.path(rel))
                deleted.append(rel)
            else:
                common.write_atomic(rel, "\n".join(kept) + "\n")
                changed.append(rel)
            common.journal_add(who, "напоминания показаны", rel)
    finally:
        handle.close()
    return changed, deleted


def cmd_show(args):
    through = parse_day(args.through, "--through")
    files, errors = day_files(through)
    reminders = []
    for day, rel in files:
        try:
            lines = read_lines(rel) or []
        except (OSError, UnicodeDecodeError) as e:
            errors.append("%s: не читается: %s" % (rel, e))
            continue
        for n, line in enumerate(lines, 1):
            m = common.REMINDER_RE.match(line)
            if not m:
                errors.append("%s: строка %d не по формату: %s" % (rel, n, line))
                continue
            reminders.append({"date": day.isoformat(), "text": m.group("text"),
                              "record": "%s/%s" % (common.RECORDS, m.group("rec")), "overdue": day < through})
    if args.json:
        print(json.dumps({"reminders": reminders, "errors": errors}, ensure_ascii=False))
    else:
        for r in reminders:
            print("%s%s — %s" % (r["date"], " (за прошлый день)" if r["overdue"] else "", r["text"]))
        for e in errors:
            print("ошибка: %s" % e)
        if not reminders and not errors:
            print("напоминаний нет")
    return 0


def main():
    p = Parser(prog="remind.py")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("add")
    s.add_argument("--who", required=True)
    s.add_argument("--date", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--record", required=True)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("cancel")
    s.add_argument("--who", required=True)
    s.add_argument("--date", required=True)
    s.add_argument("--record", required=True)
    s.add_argument("--text")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("--through", required=True)
    s.add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.error = p.error
    args = p.parse_args()
    handlers = {"add": cmd_add, "cancel": cmd_cancel, "show": cmd_show}
    if args.cmd not in handlers:
        raise Fail(2, "команда: add, cancel или show")
    return handlers[args.cmd](args)


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
