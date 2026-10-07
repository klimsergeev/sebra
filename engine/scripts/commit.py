"""Коммит и отправка в main — единственный путь в main для всех исполнителей базы.

python3 engine/scripts/commit.py --who <исполнитель> --message "<что>" [--json] [<пути…>]

Никогда не отправляет с --force и никогда не отправляет ветку сессии: только `git push origin HEAD:main`.
"""

import argparse
import errno
import fcntl
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time

import common
from common import Fail

LOCK_WAIT = 60
DIRTY_WAIT = 30
MAX_PUSHES = 4
CONFLICT_DIR = "/tmp/sebra"
REJECTED = ("(fetch first)", "non-fast-forward", "[rejected]")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


class Result:
    def __init__(self):
        self.status = "not_pushed"
        self.commit = ""
        self.files = []
        self.conflicts = []
        self.attempts = 0
        self.error = ""

    def as_dict(self):
        return {"status": self.status, "commit": self.commit, "files": self.files,
                "conflicts": self.conflicts, "attempts": self.attempts, "error": self.error}


class Stop(Exception):
    """Остановка протокола с кодом и текстом; Result уже заполнен."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def lock():
    lock_path = common.git_out("rev-parse", "--git-path", "sebra-commit.lock")
    lock_path = os.path.join(common.ROOT, lock_path)
    f = open(lock_path, "a")
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
                raise Fail(3, "другое сохранение ещё идёт")
            time.sleep(0.2)


def porcelain(*pathspec, codes=False):
    """Изменённые и новые файлы под путями: список путей от корня (codes=True — пары (код, путь))."""
    # --no-renames: переименование в индексе — удаление старого пути и новый путь; иначе удаление прошло бы мимо
    # проверки прав шага 2
    out = common.git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--",
                     *pathspec).stdout
    items, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, name = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            i += 1  # у переименования следом идёт старое имя
        items.append((code, name) if codes else name)
    return items


def normalize(p):
    full = os.path.normpath(os.path.join(common.ROOT, p))
    root = os.path.normpath(common.ROOT)
    if full != root and not full.startswith(root + os.sep):
        raise Fail(2, "путь вне базы: %s" % p)
    return os.path.relpath(full, root).replace(os.sep, "/")


def exists_in_head(p):
    return common.git("cat-file", "-e", "HEAD:%s" % p, check=False).returncode == 0


def ahead_of_main():
    """В HEAD есть коммиты, которых нет в origin/main. Сначала явный fetch main; сети нет — считать, что есть."""
    fetched = common.git("fetch", "origin", common.MAIN_REFSPEC, check=False, timeout=180)
    if fetched.returncode != 0:
        return True  # сети нет — сразу к отправке: её отказ и скажет, что не так
    cp = common.git("rev-list", "--count", "origin/main..HEAD", check=False)
    if cp.returncode != 0:
        # нет origin/main (пустой удалённый репозиторий) — отправлять есть что, если есть HEAD
        return common.git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
    return int(cp.stdout.strip() or 0) > 0


def author_env(extra=None):
    """Автор из config.yaml (оба поля заданы) — для commit и для всех rebase.

    Коммиттер — по умолчанию копии: в облаке это Claude <noreply@anthropic.com> с подписью, и GitHub показывает
    коммит как Verified; коммиттер из настроек при той же подписи — Unverified, и стоп-хук облака требует переписать
    коммит. Только если git сам коммиттера не знает (git var GIT_COMMITTER_IDENT — отказ), коммиттер — тоже из настроек.
    """
    env = dict(extra or {})
    try:
        cfg = common.config()
    except Fail as e:
        print("commit.py: config.yaml не читается (%s); автор — по умолчанию" % e.message, file=sys.stderr)
        return env
    name, email = cfg.get("git.author_name") or "", cfg.get("git.author_email") or ""
    if isinstance(name, str) and isinstance(email, str) and name and email:
        env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email})
        if common.git("var", "GIT_COMMITTER_IDENT", check=False).returncode != 0:
            env.update({"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return env


def message_text(who, what):
    first = "%s: %s" % (who, " ".join(what.split()))
    lines = [first, "", "Co-Authored-By: Claude <noreply@anthropic.com>"]
    session = os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID")
    if session:
        if session.startswith("cse_"):
            session = "session_" + session[len("cse_"):]
        lines.append("Claude-Session: https://claude.ai/code/%s" % session)
    return "\n".join(lines) + "\n"


def rebase_in_progress():
    for name in ("rebase-merge", "rebase-apply"):
        p = os.path.join(common.ROOT, common.git_out("rev-parse", "--git-path", name))
        if os.path.isdir(p):
            return True
    return False


def unmerged():
    out = common.git("diff", "--name-only", "-z", "--diff-filter=U").stdout
    return [p for p in out.split("\0") if p]


def abort_rebase():
    common.git("rebase", "--abort", check=False)


def resolve_conflicts(who, res):
    """На каждой остановке rebase: версия сессии — в /tmp/sebra, в файл — версия main, строка в журнал."""
    env = author_env({"GIT_EDITOR": "true"})
    while rebase_in_progress():
        files = unmerged()
        if files:
            sha = common.git_out("rev-parse", "--short=7", "REBASE_HEAD")
            for p in files:
                copy = os.path.join(CONFLICT_DIR, "conflict-%s" % sha, p)
                shown = subprocess.run(["git", "show", "REBASE_HEAD:%s" % p], cwd=common.ROOT,
                                       capture_output=True)
                if shown.returncode == 0:
                    os.makedirs(os.path.dirname(copy), exist_ok=True)
                    with open(copy, "wb") as f:
                        f.write(shown.stdout)
                else:
                    copy = ""  # правка сессии — удаление файла, копии нет
                if common.git("cat-file", "-e", "HEAD:%s" % p, check=False).returncode == 0:
                    restored = common.git("restore", "--source=HEAD", "--staged", "--worktree", "--", p, check=False)
                else:
                    # в main файл удалён, а сессия его меняла: принять удаление, версия сессии — в копии
                    restored = common.git("rm", "-q", "-f", "--", p, check=False)
                if restored.returncode != 0:
                    abort_rebase()
                    res.error = common.git_error(restored)
                    raise Stop(1, "не удалось поставить версию main для %s: %s; коммит остаётся в сессии"
                               % (p, res.error))
                log_rel, _ = common.journal_add(who, "конфликт: правка не вошла", p,
                                                "файл одновременно изменён в другой сессии")
                common.git("add", "--", log_rel)
                if p not in [c["path"] for c in res.conflicts]:
                    res.conflicts.append({"path": p, "copy": copy})
        cont = common.git("-c", "core.editor=true", "rebase", "--continue", check=False, env=env)
        if cont.returncode == 0:
            continue
        if rebase_in_progress() and not unmerged():
            if common.git("diff", "--cached", "--quiet", check=False).returncode != 0:
                # остановка не из-за пустого коммита (например, перенос затёр бы неотслеживаемый файл):
                # --skip выбросил бы непустой коммит сессии
                abort_rebase()
                res.error = common.git_error(cont)
                raise Stop(1, "rebase не завершился: %s; коммит остаётся в сессии" % res.error)
            # коммит стал пустым — пропустить
            skip = common.git("rebase", "--skip", check=False, env=env)
            if skip.returncode == 0 or not rebase_in_progress():
                continue
            abort_rebase()
            res.error = common.git_error(skip)
            raise Stop(1, "rebase не завершился: %s; коммит остаётся в сессии" % res.error)
        if not rebase_in_progress():
            abort_rebase()
            res.error = common.git_error(cont)
            raise Stop(1, "rebase не завершился: %s; коммит остаётся в сессии" % res.error)


def wait_clean():
    """Отслеживаемые файлы вне коммита не изменены; ждать до 30 секунд."""
    deadline = time.monotonic() + DIRTY_WAIT
    while True:
        dirty = common.git("status", "--porcelain", "--untracked-files=no").stdout
        if not dirty.strip():
            return []
        if time.monotonic() >= deadline:
            return [line[3:] for line in dirty.split("\n") if line.strip()]
        time.sleep(1)


def push(who, res):
    """Отправка с повторами; при любом сбое посреди rebase rebase отменяется."""
    try:
        _push(who, res)
    except BaseException:
        if rebase_in_progress():
            abort_rebase()
        raise


def _push(who, res):
    while True:
        res.attempts += 1
        cp = common.git("push", "origin", "HEAD:main", check=False, timeout=180)
        if cp.returncode == 0:
            return
        text = common.git_error(cp)
        if not any(mark in (cp.stderr or "") for mark in REJECTED):
            res.error = text
            raise Stop(3, "отправка не прошла: %s; коммит остаётся в сессии" % text)
        if res.attempts >= MAX_PUSHES:
            res.error = text
            raise Stop(1, "отправка не прошла после %d попыток; коммит остаётся в сессии и уйдёт со следующим "
                          "сохранением" % res.attempts)
        time.sleep(random.uniform(1, 5))
        dirty = wait_clean()
        if dirty:
            res.error = "в рабочей копии несохранённые правки: %s" % ", ".join(dirty)
            raise Stop(1, "%s; коммит сохранён в сессии и уйдёт со следующим сохранением" % res.error)
        # явная ссылка: origin/main обновится при любом refspec облачной копии
        fetched = common.git("fetch", "origin", common.MAIN_REFSPEC, check=False, timeout=180)
        if fetched.returncode != 0:
            res.error = common.git_error(fetched)
            raise Stop(3, "не удалось забрать main: %s; коммит остаётся в сессии" % res.error)
        rb = common.git("rebase", "--no-autostash", "origin/main", check=False, env=author_env({"GIT_EDITOR": "true"}))
        if rb.returncode == 0:
            continue
        if rebase_in_progress() and unmerged():
            if who == "engine":
                abort_rebase()
                res.error = common.git_error(rb)
                raise Stop(1, "конфликт при обновлении движка: rebase отменён; %s" % res.error)
            resolve_conflicts(who, res)
            continue
        abort_rebase()
        res.error = common.git_error(rb)
        raise Stop(1, "rebase не прошёл: %s; коммит остаётся в сессии" % res.error)


DELETERS = ("seb-restorer", "seb-gardener", "summary")


def agent_model_only(rel, cfg):
    """Файл агента отличается от HEAD только значением строки model: в шапке, и оно равно agents.<имя>."""
    m = re.fullmatch(r"\.claude/agents/seb-([a-z0-9]+(?:-[a-z0-9]+)*)\.md", rel)
    if not m or not os.path.isfile(common.path(rel)):
        return False
    head = subprocess.run(["git", "show", "HEAD:%s" % rel], cwd=common.ROOT, capture_output=True)
    if head.returncode != 0:
        return False
    with open(common.path(rel), "rb") as f:
        disk = f.read()
    want = cfg.get("agents.%s" % m.group(1))
    if not isinstance(want, str) or common.agent_model(disk) != want:
        return False
    old, new = head.stdout.split(b"\n"), disk.split(b"\n")
    if len(old) != len(new):
        return False
    diff = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    if not diff:
        return True
    if len(diff) != 1 or common.agent_model(head.stdout) is None:
        return False
    i = diff[0]
    pattern = re.compile(rb"^model:[ \t]*")
    return bool(pattern.match(old[i]) and pattern.match(new[i])) and \
        common.agent_model(head.stdout) != common.agent_model(disk)


def check_rights(who, paths, entries):
    """Шаг 2: кто что может сохранять. Нарушение — Fail(1)."""
    if not os.path.exists(common.path(common.ENGINE_LOCK)) and who != "setup":
        raise Fail(1, "база не настроена: нет engine.lock; сохранять может только настройка (--who setup)")
    named = set(paths) | {p for _, p in entries}
    engine = common.engine_files()
    if who != "engine":
        touched = sorted(p for p in named if p in engine)
        if touched:
            cfg = None
            if who == "seb-settings":
                try:
                    cfg = common.config()
                except Fail:
                    cfg = {}
            bad = [p for p in touched if cfg is None or not agent_model_only(p, cfg)]
            if bad:
                if who == "seb-settings":
                    raise Fail(1, "файл агента меняется только сменой модели через config.py set: %s" % ", ".join(bad))
                raise Fail(1, "файлы движка сохраняет только обновление движка (--who engine): %s" % ", ".join(bad))
    if who not in ("engine", "setup") and common.ENGINE_LOCK in named:
        raise Fail(1, "engine.lock сохраняют только настройка и обновление движка")
    deleted = {p for code, p in entries if "D" in code}
    deleted |= {p for p in paths if not os.path.lexists(common.path(p)) and exists_in_head(p)}
    for p in sorted(deleted):
        if not p.startswith(common.DATA_DIR + "/"):
            continue
        if who in DELETERS or (who == "seb-intake" and p.startswith(common.REMINDERS + "/")):
            continue
        raise Fail(1, "удалять файлы данных может только seb-restorer, seb-gardener, summary "
                      "(и seb-intake — напоминания): %s" % p)


def run(args, res):
    common.ensure_repo()
    common.check_who(args.who)
    if rebase_in_progress():
        # прошлый вызов прервали посреди rebase (процесс убит, машина усыплена) — вернуть ветку как была
        abort_rebase()
    if not args.message or not args.message.strip():
        raise Fail(2, "--message: нужен текст")
    paths = [normalize(p) for p in args.paths]
    for p in paths:
        if not os.path.lexists(common.path(p)) and not exists_in_head(p):
            raise Fail(2, "нет такого пути ни на диске, ни в HEAD: %s" % p)
    entries = porcelain(*paths, codes=True) if paths else []
    check_rights(args.who, paths, entries)
    logs = porcelain(common.LOG + "/")
    for p in logs:
        if p not in paths:
            paths.append(p)

    if paths:
        committed = False
        try:
            # удаление, уже внесённое в индекс (git rm), add не находит — такие пути он пропускает
            to_add = [p for p in paths if os.path.lexists(common.path(p)) or
                      common.git("ls-files", "--error-unmatch", "--", p, check=False).returncode == 0]
            added = common.git("add", "-A", "--", *to_add, check=False) if to_add else None
            if added is None:
                pass
            elif added.returncode != 0:
                res.error = common.git_error(added)
                raise Stop(2 if "ignored by one of your .gitignore" in (added.stderr or "") else 3,
                           "git add: %s" % res.error)
            staged = common.git("diff", "--cached", "--quiet", "--", *paths, check=False)
            if staged.returncode == 1:
                fd, msg = tempfile.mkstemp(prefix="sebra-commit-", suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(message_text(args.who, args.message))
                try:
                    cp = common.git("commit", "--quiet", "-F", msg, "--", *paths, check=False, env=author_env())
                finally:
                    os.unlink(msg)
                if cp.returncode != 0:
                    res.error = common.git_error(cp)
                    raise Stop(3, "коммит не создан: %s" % res.error)
            committed = True
        finally:
            if not committed:
                # индекс — как до вызова: иначе застрявшие пути остановят следующий rebase
                common.git("reset", "-q", "--", *paths, check=False)
        if staged.returncode == 1:
            res.files = [f for f in common.git_out("diff-tree", "--no-commit-id", "--name-only", "-r",
                                                   "--no-renames", "HEAD").split("\n") if f]
    if not res.files and not ahead_of_main():
        common.forget_session_ref()
        res.status = "nothing"
        return 0, "нечего сохранять"

    subject = common.git_out("log", "-1", "--format=%s", "HEAD") if res.files else None
    push(args.who, res)
    res.commit = common.git_out("rev-parse", "HEAD")
    if subject is not None:
        # после rebase в коммите нет файлов из конфликта; коммит мог и пропасть, если стал пустым
        same = common.git_out("log", "-1", "--format=%s", "HEAD") == subject
        res.files = [f for f in common.git_out("diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames",
                                               "HEAD").split("\n") if f] if same else []
    # явный refspec: у облачной копии обычный fetch может не создавать origin/main, а upstream нужна эта ссылка
    common.git("fetch", "origin", common.MAIN_REFSPEC, check=False, timeout=180)
    common.set_upstream_main()
    # стоп-хук облака сравнивает ветку с origin/<ветка>, если она есть: без неё — с origin/HEAD = main
    common.forget_session_ref()
    if res.conflicts:
        res.status = "partial"
        lines = ["сохранено всё, кроме: %s — их одновременно изменила другая сессия;" %
                 ", ".join(c["path"] for c in res.conflicts)]
        for c in res.conflicts:
            lines.append("ваша версия %s — %s;" % (c["path"], c["copy"]))
        lines.append("перенесите правку на свежую версию файла и сохраните снова")
        return 1, " ".join(lines)
    res.status = "pushed"
    return 0, "сохранено в main: %s%s" % (res.commit[:7], (" — " + ", ".join(res.files)) if res.files else "")


def main():
    p = Parser(prog="commit.py")
    p.add_argument("--who", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("paths", nargs="*")
    json_mode = common.has_json_flag(sys.argv)
    res = Result()
    handle = None
    try:
        args = p.parse_args()
        handle = lock()
        code, text = run(args, res)
    except Stop as e:
        code, text = e.code, e.message
    except Fail as e:
        code, text = e.code, e.message
        res.error = res.error or e.message
    finally:
        if handle:
            handle.close()
    if res.status == "not_pushed" and not res.commit and code != 2:
        try:
            head = common.git("rev-parse", "HEAD", check=False)
            res.commit = head.stdout.strip() if head.returncode == 0 else ""
        except Fail:
            res.commit = ""
    if json_mode:
        print(json.dumps(res.as_dict(), ensure_ascii=False))
    else:
        print(text, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
