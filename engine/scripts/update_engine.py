"""Версия движка в базе: init (новая база), check (есть ли новее), update (обновление и откат).

python3 engine/scripts/update_engine.py init [--source <адрес>]
python3 engine/scripts/update_engine.py check [--json]
python3 engine/scripts/update_engine.py update [--to X.Y.Z] [--json]

Версии — теги X.Y.Z репозитория движка (адрес — поле source в engine.lock). Код из клона не исполняется: клон
только копируется; после копирования config.py, journal.py, commit.py и lint.py — уже файлы движка базы.
Параметры вызовов config.py migrate и commit.py — постоянный договор: меняются только со сменой старшего номера.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

import common
from common import Fail

ENGINE_SOURCE = "https://github.com/klimsergeev/sebra.git"
WORK = "/tmp/sebra"
TAG_RE = re.compile(r"^refs/tags/(\d+)\.(\d+)\.(\d+)(\^\{\})?$")
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
USER_FILES = (common.CONFIG, common.RULES, common.ENGINE_LOCK)
WORKFLOWS = ".github/workflows/"
LOCK_HEAD = "# Версия движка в этой базе. Пишет только engine/scripts/update_engine.py."


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


def git_any(*args, cwd=None, check=True, timeout=300):
    """git с GIT_TERMINAL_PROMPT=0 и LC_ALL=C в cwd (по умолчанию — корень базы)."""
    env = dict(os.environ)
    env.update(GIT_ENV)
    try:
        cp = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd or common.ROOT, env=env,
                            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise Fail(3, "git не запускается") from None
    except subprocess.TimeoutExpired:
        raise Fail(3, "git %s: нет ответа за %d секунд" % (args[0], timeout)) from None
    if check and cp.returncode != 0:
        raise Fail(3, "git %s: %s" % (args[0], common.git_error(cp)))
    return cp


def user_path(p):
    return p in USER_FILES or p == common.DATA_DIR or p.startswith(common.DATA_DIR + "/")


def remote_versions(source):
    """{(X, Y, Z): sha коммита версии} по git ls-remote --tags; у аннотированного тега — строка ^{}."""
    cp = git_any("ls-remote", "--tags", source, check=False, timeout=120)
    if cp.returncode != 0:
        raise Fail(3, "репозиторий движка недоступен: %s" % common.git_error(cp))
    plain, peeled = {}, {}
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        m = TAG_RE.match(parts[1])
        if not m:
            continue
        v = tuple(int(x) for x in m.group(1, 2, 3))
        if "%d.%d.%d" % v != "%s.%s.%s" % m.group(1, 2, 3):
            continue  # ведущие нули — не версия
        (peeled if m.group(4) else plain)[v] = parts[0]
    return {v: peeled.get(v, sha) for v, sha in plain.items()}


def vstr(v):
    return "%d.%d.%d" % v


def clone(source, version):
    target = os.path.join(WORK, "engine-%s" % vstr(version))
    if os.path.exists(target):
        shutil.rmtree(target)
    os.makedirs(WORK, exist_ok=True)
    cp = git_any("clone", "-q", "--depth", "1", "--branch", vstr(version), source, target, cwd=WORK, check=False)
    if cp.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise Fail(3, "версия %s не скачалась: %s" % (vstr(version), common.git_error(cp)))
    return target


def read_lock():
    lock = common.engine_lock()
    if lock is None:
        raise Fail(1, "база не настроена: нет engine.lock")
    for key in ("version", "source", "commit", "files"):
        if key not in lock:
            raise Fail(1, "engine.lock: нет поля %s" % key)
    if not isinstance(lock["files"], list):
        raise Fail(1, "engine.lock: files — не список")
    return lock


def lock_text(version, source, commit, installed, stashed, files):
    def scalar(p):
        return p if re.fullmatch(r"[A-Za-z0-9_./-]+", p) and not p.startswith("-") else json.dumps(p,
                                                                                                    ensure_ascii=False)
    lines = [LOCK_HEAD, 'version: "%s"' % version, "source: %s" % json.dumps(source, ensure_ascii=False),
             'commit: "%s"' % commit, 'installed: "%s"' % installed,
             "stashed_config: %s" % json.dumps(stashed or {}, ensure_ascii=False), "files:"]
    lines += ["  - %s" % scalar(f) for f in sorted(files)]
    return "\n".join(lines) + "\n"


def changelog_sections(text, low, high):
    """Разделы `## X.Y.Z — …` с low < версия ≤ high, по порядку файла."""
    out, keep = [], False
    for line in text.split("\n"):
        m = re.match(r"^## (\d+\.\d+\.\d+)\b", line)
        if m:
            v = common.version_tuple(m.group(1))
            keep = low < v <= high
        elif line.startswith("## ") or line.startswith("# "):
            keep = False
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def run_engine(name, *args):
    """Скрипт движка базы (уже новой версии после копирования): (код, JSON или None, текст)."""
    cp = subprocess.run([sys.executable, common.path(common.ENGINE_DIR, "scripts", name), *args], cwd=common.ROOT,
                        capture_output=True, encoding="utf-8", errors="replace", timeout=900)
    try:
        data = json.loads(cp.stdout)
    except ValueError:
        data = None
    return cp.returncode, data, ((cp.stderr or "") + (cp.stdout or "")).strip()


# ---------------------------------------------------------------- init


def cmd_init(args):
    if os.path.exists(common.path(common.ENGINE_LOCK)):
        raise Fail(1, "engine.lock уже есть: база уже настроена")
    if not os.path.isfile(common.path(common.ENGINE_DIR, "VERSION")):
        raise Fail(1, "нет engine/VERSION: это не копия движка")
    template = common.path(common.ENGINE_DIR, "templates", "rules.local.md")
    if not os.path.exists(common.path(common.RULES)) and not os.path.isfile(template):
        raise Fail(1, "нет engine/templates/rules.local.md")
    common.ensure_repo()
    if common.git_out("rev-parse", "--is-shallow-repository") == "true":
        git_any("fetch", "--unshallow", "origin")
    roots = common.git_out("rev-list", "--max-parents=0", "HEAD").split()
    if len(roots) != 1:
        raise Fail(1, "у базы %d первых коммитов, нужен один — копия шаблона" % len(roots))
    root = roots[0]
    files = [f for f in common.git_out("ls-tree", "-r", "--name-only", root).split("\n") if f]
    bad = [f for f in files if user_path(f)]
    if bad:
        raise Fail(1, "в первом коммите базы файлы пользователя или данных: %s" % ", ".join(bad))
    version = common.read_text(common.ENGINE_DIR + "/VERSION").strip()
    v = common.version_tuple(version)
    source = args.source or ENGINE_SOURCE
    versions = remote_versions(source)
    if v not in versions:
        raise Fail(1, "версии %s нет в %s" % (version, source))
    # временный клон, а не fetch --depth 1 в базу: такой fetch делает копию базы неглубокой
    tmp = clone(source, v)
    try:
        tree = git_any("rev-parse", "HEAD^{tree}", cwd=tmp).stdout.strip()
        head = git_any("rev-parse", "HEAD", cwd=tmp).stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if head != versions[v]:
        raise Fail(1, "клон версии %s — не коммит её тега" % version)
    if tree != common.git_out("rev-parse", "%s^{tree}" % root):
        raise Fail(1, "копия не совпадает с версией %s" % version)
    common.write_atomic(common.ENGINE_LOCK,
                        lock_text(version, source, versions[v], common.fmt_dt(common.now()), {}, files))
    created = []
    if not os.path.exists(common.path(common.RULES)):
        shutil.copyfile(template, common.path(common.RULES))
        created.append(common.RULES)
    print("engine.lock: версия %s%s" % (version, "; создан rules.local.md" if created else ""))
    return 0


# ---------------------------------------------------------------- check


def cmd_check(args):
    lock = read_lock()
    installed = common.version_tuple(lock["version"])
    versions = remote_versions(str(lock["source"]))
    latest = max(versions) if versions else None
    newer = latest is not None and latest > installed
    tag_moved = versions.get(installed) != lock["commit"]
    changelog = ""
    if newer:
        tmp = clone(str(lock["source"]), latest)
        try:
            p = os.path.join(tmp, common.ENGINE_DIR, "CHANGELOG.md")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    changelog = changelog_sections(f.read(), installed, latest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    result = {"installed": vstr(installed), "installed_commit": lock["commit"],
              "latest": vstr(latest) if latest else None, "newer": newer, "changelog": changelog,
              "tag_moved": tag_moved}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if tag_moved:
            print("Внимание: версия на сервере подменена — тег %s указывает не на коммит из engine.lock"
                  % vstr(installed))
        if newer:
            print("Есть новая версия %s (стоит %s)" % (vstr(latest), vstr(installed)))
            if changelog:
                print(changelog)
        else:
            print("Новой версии нет: стоит %s" % vstr(installed))
    return 0


# ---------------------------------------------------------------- update


def hash_object(p):
    return git_any("hash-object", "--", p).stdout.strip()


def remove_empty_dirs(paths):
    """Опустевшие папки движка после удаления файлов — снизу вверх."""
    dirs = set()
    for p in paths:
        d = os.path.dirname(p)
        while d:
            dirs.add(d)
            d = os.path.dirname(d)
    for d in sorted(dirs, key=lambda x: -x.count("/")):
        full = common.path(d)
        if os.path.isdir(full) and not os.listdir(full):
            os.rmdir(full)


class Rollback(Exception):
    pass


def choose_target(args, versions, installed):
    """Шаг 0: версия, которую ставить; None — новой версии нет."""
    if args.to:
        target = common.version_tuple(args.to)
        if target not in versions:
            raise Fail(1, "версии %s нет среди версий движка" % args.to)
        if target == installed:
            raise Fail(1, "версия %s уже стоит" % args.to)
        return target
    target = max(versions) if versions else installed
    return target if target > installed else None


def nothing_new(args, installed):
    say(args, {"from": vstr(installed), "to": vstr(installed), "replaced": [], "added": [], "deleted": [],
               "config": None, "commit": "", "pushed": False, "lint": None}, "новой версии нет")
    return 0


def cmd_update(args):
    lock = read_lock()
    installed = common.version_tuple(lock["version"])
    source = str(lock["source"])
    versions = remote_versions(source)
    target = choose_target(args, versions, installed)
    if target is None:
        return nothing_new(args, installed)

    # 1) рабочая копия чистая (неотслеживаемые вне .gitignore — тоже правки)
    dirty = common.git_out("status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise Fail(1, "в рабочей копии несохранённые правки: %s" % ", ".join(
            line[3:] for line in dirty.split("\n") if line.strip()))
    # 2) копия вровень с main
    common.git("fetch", "origin", common.MAIN_REFSPEC, timeout=180)
    ahead = int(common.git_out("rev-list", "--count", "origin/main..HEAD") or 0)
    if ahead:
        raise Fail(1, "в сессии есть неотправленные сохранения: сначала python3 engine/scripts/commit.py --who "
                      "seb-update-engine --message \"отправка перед обновлением\"")
    merged = common.git("merge", "--ff-only", "origin/main", check=False)
    if merged.returncode != 0:
        raise Fail(1, "копия не догнала main: %s" % common.git_error(merged))
    # перемотка могла принести engine.lock другой сессии, которая уже обновила движок: шаг 0 — заново
    lock = read_lock()
    installed = common.version_tuple(lock["version"])
    target = choose_target(args, versions, installed)
    if target is None:
        return nothing_new(args, installed)
    old_files = [str(f) for f in lock["files"]]
    # 3) клон версии и его проверка
    tmp = clone(source, target)
    try:
        return update_from_clone(args, lock, installed, target, versions, old_files, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def update_from_clone(args, lock, installed, target, versions, old_files, tmp):
    head = git_any("rev-parse", "HEAD", cwd=tmp).stdout.strip()
    if head != versions[target]:
        raise Fail(1, "клон версии %s — не коммит её тега" % vstr(target))
    with open(os.path.join(tmp, common.ENGINE_DIR, "VERSION"), encoding="utf-8") as f:
        if f.read().strip() != vstr(target):
            raise Fail(1, "engine/VERSION клона — не %s" % vstr(target))
    # 4) новый список
    new_files = [f for f in git_any("ls-files", cwd=tmp).stdout.split("\n") if f]
    # 5) проверки списков
    bad = sorted({f for f in old_files + new_files if user_path(f)})
    if bad:
        raise Fail(1, "в списке файлов движка — файлы пользователя или данных: %s" % ", ".join(bad))
    old_set, new_set = set(old_files), set(new_files)
    clash = sorted(f for f in new_set - old_set if os.path.lexists(common.path(f)))
    if clash:
        raise Fail(1, "новый файл движка уже есть в базе: %s" % ", ".join(clash))
    flows = sorted({f for f in old_set | new_set if f.startswith(WORKFLOWS)})
    for f in flows:
        if f not in old_set or f not in new_set or \
                hash_object(os.path.join(tmp, f)) != hash_object(common.path(f)):
            raise Fail(1, "эту версию нельзя поставить из облака: она меняет задачу GitHub (%s)" % f)

    # 6) замена файлов; время и пояс — до копирования: после него engine/config.default.yaml уже новой версии
    moment = common.now()
    today_log = "%s/%s.md" % (common.LOG, moment.strftime("%Y-%m-%d"))
    before = common.git_out("rev-parse", "HEAD")
    rollback = target < installed
    os.makedirs(WORK, exist_ok=True)
    migrations = common.path(common.ENGINE_DIR, "config.migrations.yaml")
    if rollback:
        saved = os.path.join(WORK, "config.migrations-%s.yaml" % vstr(installed))
        shutil.copyfile(migrations, saved)
        migrations = saved
    stash_file = os.path.join(WORK, "stash.json")
    stashed = lock.get("stashed_config") or {}
    with open(stash_file, "w", encoding="utf-8") as f:
        json.dump(stashed if isinstance(stashed, dict) else {}, f, ensure_ascii=False)
    added = sorted(new_set - old_set)
    deleted = sorted(old_set - new_set)
    replaced = sorted(f for f in new_set & old_set
                      if not os.path.isfile(common.path(f)) or hash_object(os.path.join(tmp, f)) != hash_object(
                          common.path(f)) or (os.stat(os.path.join(tmp, f)).st_mode & 0o111) != (
                          os.stat(common.path(f)).st_mode & 0o111))
    journal_file = None
    try:
        for f in sorted(new_set):
            dst = common.path(f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(tmp, f), dst)
        for f in deleted:
            if os.path.lexists(common.path(f)):
                os.remove(common.path(f))
        remove_empty_dirs(deleted)
        # 7) настройки и модели агентов — config.py новой версии
        code, cfg, text = run_engine("config.py", "migrate", "--from", vstr(installed), "--to", vstr(target),
                                     "--migrations", migrations, "--stash", stash_file, "--json")
        if code != 0:
            raise Rollback("перенос настроек не прошёл: %s" % text)
        # 8) engine.lock
        with open(stash_file, encoding="utf-8") as f:
            new_stash = json.load(f)
        common.write_atomic(common.ENGINE_LOCK, lock_text(vstr(target), str(lock["source"]), versions[target],
                                                          common.fmt_dt(moment), new_stash, new_files))
        # 9) журнал и сохранение
        code, data, text = run_engine("journal.py", "add", "--who", "engine", "--action",
                                      "откат движка" if rollback else "обновление движка", "--target",
                                      common.ENGINE_LOCK, "--note", "%s → %s" % (vstr(installed), vstr(target)),
                                      "--json")
        if code != 0 or not data:
            raise Rollback("строка журнала не записана: %s" % text)
        journal_file = data["file"]
        paths = sorted(set(old_files) | set(new_files) | {common.CONFIG, common.ENGINE_LOCK})
        code, saved, text = run_engine("commit.py", "--who", "engine", "--message", "version %s" % vstr(target),
                                       "--json", *paths)
        if code != 0:
            if landed(before):
                # commit.py сообщил о сбое (например, таймаут push), а коммит уже в main: откатывать нельзя
                saved = {"commit": common.git_out("rev-parse", "HEAD"), "status": "pushed"}
            else:
                raise Rollback("сохранение не прошло: %s" % ((saved or {}).get("error") or text))
    except BaseException as e:  # noqa: BLE001 — любой сбой после шага 6: база как до запуска
        reason = e.message if isinstance(e, Fail) else (str(e) or type(e).__name__)
        problem = restore(before, old_files, new_files, journal_file or today_log)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        if problem:
            raise Fail(1, "обновление не прошло: %s; откат не завершён: %s" % (reason, problem)) from None
        raise Fail(1, "обновление не прошло, база как до запуска: %s" % reason) from None

    # 10) проверка базы
    code, lint, text = run_engine("lint.py", "--json")
    result = {"from": vstr(installed), "to": vstr(target), "replaced": replaced, "added": added,
              "deleted": deleted, "config": cfg, "commit": saved.get("commit", ""),
              "pushed": saved.get("status") == "pushed", "lint": lint if lint is not None else {"error": text}}
    say(args, result, "Движок: %s → %s; сохранено в main: %s" % (vstr(installed), vstr(target),
                                                                 saved.get("commit", "")[:7]))
    if not args.json:
        print("Начните новую сессию: новая версия загружается при старте сессии")
    return 0


def landed(before):
    """Коммит обновления создан и уже в main (отправка дошла, хотя commit.py сообщил о сбое)."""
    head = common.git_out("rev-parse", "HEAD")
    if head == before:
        return False
    if common.git("fetch", "origin", common.MAIN_REFSPEC, check=False, timeout=180).returncode != 0:
        return False
    return common.git("merge-base", "--is-ancestor", head, "origin/main", check=False).returncode == 0


def restore(before, old_files, new_files, journal_file):
    """Сбой после шага 6: база как до запуска. Возвращает "" или что осталось не так."""
    try:
        _restore(before, old_files, new_files, journal_file)
    except Exception as e:  # noqa: BLE001 — откат доводится до проверки ниже
        return getattr(e, "message", None) or str(e)
    head = common.git_out("rev-parse", "HEAD")
    if head != before:
        return "HEAD %s вместо %s" % (head[:7], before[:7])
    dirty = common.git_out("status", "--porcelain", "--untracked-files=all")
    return "в рабочей копии остались правки: %s" % dirty.replace("\n", "; ") if dirty else ""


def _restore(before, old_files, new_files, journal_file):
    common.git("rebase", "--abort", check=False)  # commit.py мог быть прерван посреди rebase
    if common.git_out("rev-parse", "HEAD") != before:
        # коммит есть, отправка не прошла
        common.git("reset", "--keep", before)
        return
    # журнал дня — всегда (journal_file — путь из journal.py или файл сегодняшнего дня): строку мог дописать
    # journal.py, который потом упал
    paths = sorted(set(old_files) | set(new_files) | {common.CONFIG, common.ENGINE_LOCK, journal_file})
    in_head = [p for p in paths if common.git("cat-file", "-e", "HEAD:%s" % p, check=False).returncode == 0]
    fresh = [p for p in paths if p not in in_head]
    if in_head:
        common.git("restore", "--source=HEAD", "--staged", "--worktree", "--", *in_head, check=False)
    for p in fresh:
        common.git("rm", "-q", "--cached", "--ignore-unmatch", "--", p, check=False)
        if os.path.lexists(common.path(p)):
            os.remove(common.path(p))
    remove_empty_dirs(fresh)


def say(args, result, text):
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(text)


def main():
    p = Parser(prog="update_engine.py")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("init")
    s.add_argument("--source")
    s = sub.add_parser("check")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("update")
    s.add_argument("--to")
    s.add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.error = p.error
    args = p.parse_args()
    handlers = {"init": cmd_init, "check": cmd_check, "update": cmd_update}
    if args.cmd not in handlers:
        raise Fail(2, "команда: init, check или update")
    return handlers[args.cmd](args)


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
