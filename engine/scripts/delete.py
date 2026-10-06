"""Удаление файлов данных: один файл по просьбе пользователя или дубль, старые закрытые задачи. Коммит не делает.

python3 engine/scripts/delete.py path <путь> --who <seb-restorer | seb-gardener> --reason "<причина>" [--dry-run] [--json]
python3 engine/scripts/delete.py tasks --older <N> [--json]

Удалённые пути исполнитель передаёт commit.py со своим --who. Удаление — os.remove, не git rm.
Ссылки на файл (referenced_by) — {"path", "line", "via"}: ссылки и поля record, project в data/wiki/, data/tasks/,
data/index.md и строки data/reminders/ с этой записью (via reminder). data/reports/ и data/log/ не смотрятся.
"""

import argparse
import glob
import json
import os
import sys

import common
from common import Fail

DELETERS = ("seb-restorer", "seb-gardener")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


def link_sources():
    """Файлы, ссылки из которых держат удаление: data/wiki/**, data/tasks/*, data/index.md."""
    out = sorted(glob.glob(common.path(common.WIKI, "**", "*.md"), recursive=True))
    out += sorted(glob.glob(common.path(common.TASKS, "*.md")))
    if os.path.isfile(common.path(common.INDEX)):
        out.append(common.path(common.INDEX))
    return [os.path.relpath(p, common.ROOT).replace(os.sep, "/") for p in out]


def read(rel):
    with open(common.path(rel), encoding="utf-8", errors="replace") as f:
        return f.read()


def referenced_by(targets):
    """{цель: [{"path", "line", "via"}]} для набора целей; ссылка файла на самого себя не считается."""
    targets = set(targets)
    refs = {t: [] for t in targets}
    for src in link_sources():
        for n, p, via in common.iter_links(src, read(src)):
            if p in targets and p != src:
                refs[p].append({"path": src, "line": n, "via": via})
    records = {os.path.basename(t): t for t in targets if t.startswith(common.RECORDS + "/")}
    if records:
        for full in sorted(glob.glob(common.path(common.REMINDERS, "*.md"))):
            src = os.path.relpath(full, common.ROOT).replace(os.sep, "/")
            for n, line in enumerate(read(src).split("\n"), 1):
                m = common.REMINDER_RE.match(line)
                if m and m.group("rec") in records:
                    refs[records[m.group("rec")]].append({"path": src, "line": n, "via": "reminder"})
    return refs


def cmd_path(args):
    if args.who not in DELETERS:
        raise Fail(2, "--who: только seb-restorer или seb-gardener, а не «%s»" % args.who)
    rel = common.rel_path(args.path)
    if rel is None or not rel.startswith(common.DATA_DIR + "/"):
        raise Fail(1, "удалять можно только файлы data/: %s" % args.path)
    if rel.startswith(common.LOG + "/"):
        raise Fail(1, "журнал не удаляется: %s" % rel)
    if rel.startswith(common.REMINDERS + "/"):
        raise Fail(1, "напоминания удаляет python3 engine/scripts/remind.py cancel: %s" % rel)
    if rel in common.engine_files():
        raise Fail(1, "файл движка не удаляется: %s" % rel)
    if args.who == "seb-gardener" and not rel.startswith(common.WIKI + "/"):
        raise Fail(1, "seb-gardener удаляет этой командой только страницы data/wiki/: %s" % rel)
    full = common.path(rel)
    if os.path.isdir(full):
        raise Fail(2, "это папка, а не файл: %s" % rel)
    if not os.path.isfile(full):
        raise Fail(2, "нет такого файла: %s" % rel)
    refs = referenced_by([rel])[rel]
    result = {"deleted": None, "line": "", "referenced_by": refs}
    if args.dry_run:
        emit(args, result, "пробный запуск: ничего не удалено")
        return 0
    if not (args.reason or "").strip():
        raise Fail(2, "--reason: нужна причина")
    if refs:
        result["error"] = "на файл ссылаются: %s; сначала убери ссылки" % ", ".join(
            sorted({r["path"] for r in refs}))
        emit(args, result, result["error"], stream=sys.stderr)
        return 1
    os.remove(full)
    _, line = common.journal_add(args.who, "удалено", rel, args.reason)
    result.update({"deleted": rel, "line": line})
    emit(args, result, "удалено: %s" % rel)
    return 0


def emit(args, result, text, stream=None):
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return
    print(text, file=stream or sys.stdout)
    for r in result["referenced_by"]:
        print("  ссылается %s, строка %d (%s)" % (r["path"], r["line"], r["via"]), file=stream or sys.stdout)


def cmd_tasks(args):
    try:
        older = int(args.older)
    except ValueError:
        raise Fail(2, "--older: нужно целое число дней, а не «%s»" % args.older) from None
    if older < 0:
        raise Fail(2, "--older: нужно целое не меньше 0")
    today = common.now().date()
    victims, skipped = [], []
    for full in sorted(glob.glob(common.path(common.TASKS, "*.md"))):
        rel = os.path.relpath(full, common.ROOT).replace(os.sep, "/")
        try:
            h = common.front_matter(read(rel))
        except ValueError as e:
            skipped.append({"path": rel, "reason": "шапка не читается: %s" % e})
            continue
        status = h.get("status")
        if status not in ("done", "dropped"):
            continue
        closed = h.get("closed")
        try:
            when = common.parse_day(closed)
        except ValueError:
            skipped.append({"path": rel, "reason": "закрытая задача без даты closed" if not closed
                            else "closed не дата: %s" % closed})
            continue
        if (today - when).days > older:
            victims.append((rel, closed))
    refs = referenced_by([rel for rel, _ in victims])
    gone = {rel for rel, _ in victims}
    deleted = []
    for rel, closed in victims:
        os.remove(common.path(rel))
        common.journal_add("seb-gardener", "удалено", rel, "закрыта %s, старше %d дней" % (closed, older))
        # ссылки из задач, удалённых в том же запуске, убирать не нужно
        deleted.append({"path": rel, "referenced_by": [r for r in refs[rel] if r["path"] not in gone]})
    if args.json:
        print(json.dumps({"deleted": deleted, "skipped": skipped}, ensure_ascii=False))
    else:
        for d in deleted:
            print("удалено: %s" % d["path"])
            for r in d["referenced_by"]:
                print("  ссылается %s, строка %d (%s)" % (r["path"], r["line"], r["via"]))
        for s in skipped:
            print("пропущено: %s — %s" % (s["path"], s["reason"]))
        if not deleted and not skipped:
            print("удалять нечего")
    return 0


def main():
    p = Parser(prog="delete.py")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("path")
    s.add_argument("path")
    s.add_argument("--who", required=True)
    s.add_argument("--reason")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("tasks")
    s.add_argument("--older", required=True)
    s.add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.error = p.error
    args = p.parse_args()
    if args.cmd == "path":
        return cmd_path(args)
    if args.cmd == "tasks":
        return cmd_tasks(args)
    raise Fail(2, "команда: path или tasks")


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
