"""Удаляет на GitHub ветки claude/…, вся работа которых уже есть в main. Запускает только задача GitHub Actions.

python3 engine/scripts/cleanup_branches.py      (из .github/workflows/cleanup-branches.yml)

Вне GitHub Actions (GITHUB_ACTIONS не true) — выход 2, ничего не делает. Только стандартная библиотека: common.py
не импортирует и config.yaml не читает. Итог — в $GITHUB_STEP_SUMMARY. Выход 0 — сбоев нет, иначе 1.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
PREFIX = "refs/remotes/origin/"


def git(*args, check=True):
    env = dict(os.environ)
    env.update(ENV)
    cp = subprocess.run(["git", *args], cwd=ROOT, env=env, capture_output=True, encoding="utf-8",
                        errors="replace", timeout=300)
    if check and cp.returncode != 0:
        raise RuntimeError("git %s: %s" % (args[0], (cp.stderr or cp.stdout).strip()))
    return cp


def patch_ids(rng):
    """{коммит: patch-id} для коммитов диапазона; дифф с -U0 — без строк контекста.

    git cherry сравнивает patch-id диффа с тремя строками контекста: у коммита сессии, дописавшего строку в журнал
    дня, после rebase поверх строки другой сессии меняется контекст — и patch-id тоже, хотя правка та же.
    """
    log = git("log", "-p", "-U0", "--no-merges", "--format=commit %H", rng).stdout
    env = dict(os.environ)
    env.update(ENV)
    cp = subprocess.run(["git", "patch-id", "--stable"], cwd=ROOT, env=env, input=log, capture_output=True,
                        encoding="utf-8", errors="replace", timeout=300)
    if cp.returncode != 0:
        raise RuntimeError("git patch-id: %s" % cp.stderr.strip())
    out = {}
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


def merged(ref):
    """Работа ветки уже в main: ветка — предок main, или все её коммиты попали в main под другим sha."""
    if git("merge-base", "--is-ancestor", ref, "origin/main", check=False).returncode == 0:
        return True, 0
    mine = patch_ids("origin/main.." + ref)
    theirs = set(patch_ids(ref + "..origin/main").values())
    left = [c for c, pid in mine.items() if pid not in theirs]
    return not left, len(left)


def summary(lines):
    text = "\n".join(lines) + "\n"
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write(text)
    sys.stdout.write(text)


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print("cleanup_branches.py запускает только задача GitHub Actions cleanup-branches", file=sys.stderr)
        return 2
    removed, kept, failed = [], [], []
    try:
        git("fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*")
        refs = git("for-each-ref", "--format=%(refname)", PREFIX + "claude/").stdout.split()
    except RuntimeError as e:
        summary(["## Чистка веток claude/…", "", "Сбой: %s" % e])
        return 1
    for ref in refs:
        name = ref[len(PREFIX):]
        try:
            done, left = merged(ref)
        except RuntimeError as e:
            failed.append("%s — %s" % (name, e))
            continue
        if not done:
            kept.append("%s — коммитов вне main: %d" % (name, left))
            continue
        sha = git("rev-parse", ref).stdout.strip()
        # удаление с проверкой: если живая сессия успела отправить в ветку новый коммит, GitHub откажет
        cp = git("push", "--force-with-lease=refs/heads/%s:%s" % (name, sha), "origin", ":refs/heads/%s" % name,
                 check=False)
        if cp.returncode == 0:
            removed.append(name)
        else:
            failed.append("%s — %s" % (name, (cp.stderr or cp.stdout).strip().replace("\n", " | ")))
    lines = ["## Чистка веток claude/…", "", "**Удалены:** %d" % len(removed)]
    lines += ["- %s" % x for x in removed]
    lines += ["", "**Оставлены** (в них есть то, чего нет в main): %d" % len(kept)]
    lines += ["- %s" % x for x in kept]
    lines += ["", "**Сбои:** %d" % len(failed)]
    lines += ["- %s" % x for x in failed]
    summary(lines)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
