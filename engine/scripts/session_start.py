"""Хук SessionStart: догоняет main, если копия чиста и отстаёт, и печатает 4–5 строк в контекст сессии.

python3 "$CLAUDE_PROJECT_DIR/engine/scripts/session_start.py" (вход — JSON хука в stdin, поле source).

Код выхода всегда 0: от хука ничего не зависит, сбой пункта — строка «<пункт>: не удалось (<причина>)».
"""

import json
import os
import sys

import common
from common import Fail


def read_source():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data.get("source", "") if isinstance(data, dict) else ""
    except ValueError:
        return ""


def sync():
    """Догнать origin/main; строка о состоянии копии."""
    common.git("fetch", "origin", common.MAIN_REFSPEC, timeout=15)
    behind_ancestor = common.git("merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode == 0
    dirty = common.git_out("status", "--porcelain", "--untracked-files=no")
    if behind_ancestor and not dirty:
        common.git("merge", "--ff-only", "origin/main")
    # как в commit.py: git status облака видит ветку сессии вровень с main
    common.set_upstream_main()
    behind = int(common.git_out("rev-list", "--count", "HEAD..origin/main"))
    if behind == 0:
        return "Копия базы обновлена до main"
    n = behind % 100
    word = "коммит" if n % 10 == 1 and n != 11 else "коммита" if 2 <= n % 10 <= 4 and not 12 <= n <= 14 else "коммитов"
    return "Копия базы отстаёт от main на %d %s" % (behind, word)


def item(name, fn):
    try:
        return fn()
    except Fail as e:
        return "%s: не удалось (%s)" % (name, e.message)
    except Exception as e:  # хук не падает ни при какой ошибке
        return "%s: не удалось (%s)" % (name, e)


def engine_line():
    lock = common.engine_lock()
    if lock and lock.get("version"):
        return "Движок: version %s" % lock["version"]
    return "Движок: version %s" % common.read_text(common.ENGINE_DIR + "/VERSION").strip()


def now_line():
    moment = common.now()
    tz = common.config().get("owner.timezone", "UTC")
    return "Сейчас у пользователя: %s %s, %s" % (common.fmt_dt(moment), tz, common.WEEKDAYS[moment.weekday()])


def daily_line():
    today = common.now().strftime("%Y-%m-%d")
    rel = "%s/%s.md" % (common.REPORTS_DAILY, today)
    return "Сводка за сегодня: %s" % rel if os.path.exists(common.path(rel)) else "Сводки за сегодня нет"


def configured():
    """Настроена — есть и config.yaml, и engine.lock: прерванная настройка без engine.lock не считается."""
    return os.path.exists(common.path(common.CONFIG)) and os.path.exists(common.path(common.ENGINE_LOCK))


def main():
    source = read_source()
    last = None
    if source in ("startup", "resume"):
        last = item("Копия базы", sync)
    lines = [
        "Sebra: база настроена" if configured() else "База не настроена: следуй engine/SETUP.md",
        item("Движок", engine_line),
        item("Сейчас у пользователя", now_line),
        item("Сводка за сегодня", daily_line),
    ]
    if last:
        lines.append(last)
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("Sebra: хук начала сессии не удался (%s)" % e)
    sys.exit(0)
