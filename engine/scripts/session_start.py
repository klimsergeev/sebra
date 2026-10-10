"""Хук SessionStart: запоминает версию движка сессии, догоняет main, если копия чиста и отстаёт, и печатает 4–5 строк
в контекст сессии.

python3 "$CLAUDE_PROJECT_DIR/engine/scripts/session_start.py" (вход — JSON хука в stdin, поля source и session_id).

Версия сессии — версия engine.lock до догона main: с ней Claude Code загрузил CLAUDE.md (при старте, возобновлении
и после сжатия контекста); хук на сообщение сравнивает её с engine.lock.

Код выхода всегда 0: от хука ничего не зависит, сбой пункта — строка «<пункт>: не удалось (<причина>)».
"""

import json
import os
import sys

import common
import deliver
from common import Fail


def read_input():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def remember_version(sid):
    """Версия движка, с которой сессия загрузила CLAUDE.md; сбой — строка в stderr, в контекст ничего."""
    try:
        version = common.lock_version()
        if version:
            common.save_session(sid, {"version": version, "notice_for": "", "noticed": "", "prompts": 0})
    except Exception as e:  # noqa: BLE001 — хук не падает
        print("session_start.py: версия сессии не записана (%s)" % e, file=sys.stderr)


def sync():
    """Догнать origin/main; строка о состоянии копии."""
    behind = common.sync_main()
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
    return deliver.status_line()


def configured():
    """Настроена — есть и config.yaml, и engine.lock: прерванная настройка без engine.lock не считается."""
    return os.path.exists(common.path(common.CONFIG)) and os.path.exists(common.path(common.ENGINE_LOCK))


def main():
    inp = read_input()
    source = inp.get("source", "")
    remember_version(inp.get("session_id"))
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
