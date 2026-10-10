"""Удаление файлов данных: один файл по просьбе пользователя или дубль, старые закрытые задачи, черновики сессии.
Коммит не делает.

python3 engine/scripts/delete.py path <путь> --who <seb-restorer | seb-gardener> --reason "<причина>" [--dry-run] [--json]
python3 engine/scripts/delete.py tasks --older <N> [--json]
python3 engine/scripts/delete.py drafts [--json]
python3 engine/scripts/delete.py draft <путь> --who seb-intake --reason "<причина>" [--json]

Удалённые пути исполнитель передаёт commit.py со своим --who. Удаление — os.remove, не git rm.
Ссылки на файл (referenced_by) — {"path", "line", "via"}: ссылки и поля record, project в data/wiki/, data/tasks/,
data/index.md и строки data/reminders/ с этой записью (via reminder). data/reports/ и data/log/ не смотрятся.
Черновик — несохранённая правка файла data/ вне журнала: new — файла нет в HEAD, changed — файл есть в HEAD и на
диске и отличается, deleted — файл есть в HEAD, а на диске нет. draft берёт new и changed вне напоминаний: new
убирает с диска и из индекса, changed возвращает на диске и в индексе к HEAD; нынешняя версия остаётся в
/tmp/sebra/drafts/ до конца сессии; строку журнала «черновик убран» уносит следующий commit.py без путей.
"""

import argparse
import glob
import json
import os
import shutil
import sys

import common
from common import Fail

DELETERS = ("seb-restorer", "seb-gardener")
DRAFTERS = ("seb-intake",)
DRAFT_COPIES = os.environ.get("SEBRA_DRAFTS_DIR") or "/tmp/sebra/drafts"  # переменная — для тестов
A_DRAFT = "черновик убран"


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


def in_head(rel):
    return common.git("cat-file", "-e", "HEAD:%s" % rel, check=False).returncode == 0


def draft_state(rel):
    """Состояние несохранённой правки по HEAD и диску: new | changed | deleted."""
    if not in_head(rel):
        return "new"
    return "changed" if os.path.lexists(common.path(rel)) else "deleted"


def data_changes():
    """Несохранённые правки data/ вне журнала: [(состояние, путь)] по порядку путей, каждый путь один раз."""
    out = common.git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--",
                     common.DATA_DIR + "/").stdout
    paths, parts, i = set(), out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        if "R" in entry[:2] or "C" in entry[:2]:
            i += 1  # у переименования следом идёт старое имя
        if not entry[3:].startswith(common.LOG + "/"):
            paths.add(entry[3:])
    return [(draft_state(rel), rel) for rel in sorted(paths)]


def saved_blobs():
    """{хеш содержимого: [пути]} файлов data/ в HEAD."""
    out = common.git("ls-tree", "-r", "-z", "HEAD", "--", common.DATA_DIR + "/").stdout
    blobs = {}
    for entry in out.split("\0"):
        if "\t" not in entry:
            continue
        meta, rel = entry.split("\t", 1)
        blobs.setdefault(meta.split()[2], []).append(rel)
    return blobs


def cmd_drafts(args):
    common.ensure_repo()
    blobs, out = None, []
    for state, rel in data_changes():
        same = ""
        if state == "new" and os.path.isfile(common.path(rel)):
            if blobs is None:
                blobs = saved_blobs()
            sha = common.git_out("hash-object", "--", rel)
            same = sorted(blobs.get(sha, [""]))[0]
        out.append({"path": rel, "state": state, "same_as": same})
    if args.json:
        print(json.dumps({"drafts": out}, ensure_ascii=False))
    else:
        for d in out:
            print("%s — %s%s" % (d["path"], d["state"], (", как " + d["same_as"]) if d["same_as"] else ""))
        if not out:
            print("несохранённых правок данных нет")
    return 0


def copy_target(rel):
    """Путь копии в папке копий; файл с тем же путём за ту же минуту не затирается."""
    base = os.path.join(DRAFT_COPIES, common.now().strftime("%Y%m%d-%H%M"), rel)
    copy, n = base, 1
    while os.path.lexists(copy):
        n += 1
        copy = "%s.%d" % (base, n)
    return copy


def cmd_draft(args):
    common.ensure_repo()
    if args.who not in DRAFTERS:
        raise Fail(2, "--who: черновики убирает только seb-intake, а не «%s»" % args.who)
    rel = common.rel_path(args.path)
    if rel is None or not rel.startswith(common.DATA_DIR + "/"):
        raise Fail(1, "черновик — только файл data/: %s" % args.path)
    if rel.startswith(common.LOG + "/"):
        raise Fail(1, "журнал не удаляется: %s" % rel)
    if rel.startswith(common.REMINDERS + "/"):
        raise Fail(1, "напоминания убирает python3 engine/scripts/remind.py cancel: %s" % rel)
    full = common.path(rel)
    if os.path.isdir(full) and not os.path.islink(full):
        raise Fail(2, "это папка, а не файл: %s" % rel)
    state = dict((p, s) for s, p in data_changes()).get(rel)
    if state is None:
        if os.path.lexists(full):
            raise Fail(1, "в %s нет несохранённых правок — убирать нечего" % rel)
        raise Fail(2, "нет такого файла: %s" % rel)
    if state == "deleted":
        raise Fail(1, "%s сохранён в базе и удалён с диска — это не черновик; вернуть его — seb-restorer" % rel)
    if not (args.reason or "").strip():
        raise Fail(2, "--reason: нужна причина")
    copy = copy_target(rel)
    os.makedirs(os.path.dirname(copy), exist_ok=True)
    shutil.copy2(full, copy, follow_symlinks=False)
    if state == "changed":
        back = common.git("restore", "--source=HEAD", "--staged", "--worktree", "--", rel, check=False)
    else:
        # -f: файл в индексе, потом ещё правленный, без него git rm отказывает; с --cached снимает только запись индекса
        back = common.git("rm", "-q", "-f", "--cached", "--ignore-unmatch", "--", rel, check=False)
        if back.returncode == 0:
            os.remove(full)
    if back.returncode != 0:
        raise Fail(3, "черновик не убран, копия — %s: %s" % (copy, common.git_error(back)))
    _, line = common.journal_add(args.who, A_DRAFT, rel, args.reason)
    result = {"path": rel, "state": state, "copy": copy, "line": line}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif state == "changed":
        print("черновик убран: %s — как в базе; копия правки — %s" % (rel, copy))
    else:
        print("черновик убран: %s; копия — %s" % (rel, copy))
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
    s = sub.add_parser("drafts")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("draft")
    s.add_argument("path")
    s.add_argument("--who", required=True)
    s.add_argument("--reason")
    s.add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.error = p.error
    args = p.parse_args()
    if args.cmd == "path":
        return cmd_path(args)
    if args.cmd == "tasks":
        return cmd_tasks(args)
    if args.cmd == "drafts":
        return cmd_drafts(args)
    if args.cmd == "draft":
        return cmd_draft(args)
    raise Fail(2, "команда: path, tasks, drafts или draft")


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
