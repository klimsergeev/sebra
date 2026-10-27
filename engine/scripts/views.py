"""Вид сводки и списка дел: проверка, текст views.yaml, загрузка, группы задач; разовый перенос правил о виде.

python3 engine/scripts/views.py show [--json]
python3 engine/scripts/views.py set --from <файл> [--json]
python3 engine/scripts/views.py reset [--section reminders|tasks] [--json]
python3 engine/scripts/views.py rules-moved [--json]

Вид по умолчанию — engine/views.default.yaml (файл движка), свой вид пользователя — views.yaml в корне базы: раздел
reminders или tasks из него заменяет такой же раздел вида по умолчанию целиком. Формат — engine/docs/formats.md, раздел
«Вид сводки и списка дел». views.yaml пишет только этот скрипт: файл для set — весь будущий свой вид, раздел, которого
в нём нет, — по умолчанию. Строки журнала о виде пишет агент seb-rules; сам скрипт пишет только отметку rules-moved.
Коды: 0 — сделано; 1 — правила (ошибки вида — {"errors": [...]}); 2 — вызов; 3 — окружение.
"""

import argparse
import copy
import datetime as _dt
import json
import os
import re
import sys

import common
from common import Fail

import yaml

SECTIONS = ("reminders", "tasks")
OPEN = ("open", "doing", "waiting")
WHEN = ("overdue", "today", "upcoming", "nodue")
EACH_TASK = ("every_group", "first_group")
AREAS = ("work", "personal")
IF_EMPTY = ("show", "hide")
TASK_KEYS = ("title", "when", "days", "status", "stuck", "area", "about", "split", "if_empty")
REMINDER_KEYS = ("title", "about", "if_empty")
OTHER = "Остальное"
DAYS_MAX = 365
USER_HEAD = "# Свой вид сводки и списка дел. Меняется только командой: python3 engine/scripts/views.py"
DEFAULT_HEAD = ("# Вид сводки и списка дел по умолчанию — файл движка. Свой вид пользователя — views.yaml в корне "
                "базы; его пишет только python3 engine/scripts/views.py")
# Текст — одной строкой из знаков, которые PyYAML читает в двойных кавычках: без управляющих знаков и разделителей строк.
BAD_CHAR = re.compile("[^\x20-\x7e\xa0-\u2027\u202a-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
TITLE_BAN = "#[]/"

# Разовый перенос правил о виде из rules.local.md после обновления движка с версии ниже RULES_SINCE.
RULES_SINCE = (0, 2, 2)
RULES_TEXT = "Sebra: после обновления движка правила о виде сводки ещё не перенесены."
A_MOVED = "правила о виде перенесены"   # строка журнала seb-rules: разовый перенос сделан
A_UPDATE = "обновление движка"          # строка журнала update_engine.py, пояснение «<было> → <стало>»
UPDATE_NOTE = re.compile(r"^(\d+\.\d+\.\d+) → (\d+\.\d+\.\d+)$")
DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


# ---------------------------------------------------------------- проверка


def text_problem(value, title):
    """Ошибка текста title, about или категории; None — годится. title=True — ещё запреты названия."""
    if not isinstance(value, str) or not value.strip():
        return "нужен непустой текст в двойных кавычках"
    if value != value.strip():
        return "без пробелов в начале и в конце"
    if BAD_CHAR.search(value):
        return "текст — одной строкой, без управляющих знаков"
    if '"' in value:
        return "внутри текста — кавычки-ёлочки «», а не \""
    if title and any(c in value for c in TITLE_BAN):
        return "без знаков #, [, ], /"
    if title and value == OTHER:
        return "«%s» — категория для задач, которые не подошли ни одной" % OTHER
    return None


def words_problem(value, allowed):
    """Ошибка списка значений из allowed; None — годится."""
    if not isinstance(value, list) or not value:
        return "нужен непустой список в квадратных скобках из: %s" % ", ".join(allowed)
    if any(not isinstance(v, str) or v not in allowed for v in value):
        return "в списке только: %s" % ", ".join(allowed)
    if len(set(value)) != len(value):
        return "значения в списке не повторяются"
    return None


def split_problem(value):
    if not isinstance(value, list) or not value:
        return "нужен непустой список категорий в квадратных скобках"
    for item in value:
        err = text_problem(item, True)
        if err:
            return "категория «%s»: %s" % (item, err)
    if len(set(value)) != len(value):
        return "категории не повторяются"
    return None


def value_problem(key, value):
    if key == "title":
        return text_problem(value, True)
    if key == "about":
        return text_problem(value, False)
    if key == "when":
        return words_problem(value, WHEN)
    if key == "status":
        return words_problem(value, OPEN)
    if key == "days":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= DAYS_MAX:
            return "нужно целое от 1 до %d" % DAYS_MAX
    elif key == "stuck":
        if not isinstance(value, bool):
            return "нужно yes или no без кавычек"
    elif key == "area":
        if not isinstance(value, str) or value not in AREAS:
            return "нужно одно из: %s" % ", ".join(AREAS)
    elif key == "split":
        return split_problem(value)
    elif key == "if_empty":
        if not isinstance(value, str) or value not in IF_EMPTY:
            return "нужно одно из: %s" % ", ".join(IF_EMPTY)
    return None


def group_errors(group, keys, where):
    """Ошибки одной группы; where — «tasks.groups, группа 2»."""
    if not isinstance(group, dict):
        return ["%s: группа — набор ключей" % where]
    title = group.get("title")
    name = "%s «%s»" % (where, title) if isinstance(title, str) else where
    errors = ["%s: лишний ключ %s; ключи группы: %s" % (name, k, ", ".join(keys)) for k in group if k not in keys]
    need = ("title", "about") if keys == REMINDER_KEYS else ("title",)
    errors += ["%s: нет ключа %s" % (name, k) for k in need if k not in group]
    for key in keys:
        if key in group:
            err = value_problem(key, group[key])
            if err:
                errors.append("%s: %s: %s" % (name, key, err))
    if keys == TASK_KEYS:
        upcoming = isinstance(group.get("when"), list) and "upcoming" in group["when"]
        if upcoming and "days" not in group:
            errors.append("%s: в when есть upcoming — нужен days" % name)
        if "days" in group and not upcoming:
            errors.append("%s: days — только когда в when есть upcoming" % name)
    return errors


def groups_errors(groups, keys, where):
    errors, seen = [], set()
    for i, group in enumerate(groups, 1):
        errors += group_errors(group, keys, "%s, группа %d" % (where, i))
        title = group.get("title") if isinstance(group, dict) else None
        if isinstance(title, str):
            if title in seen:
                errors.append("%s: название «%s» повторяется" % (where, title))
            seen.add(title)
    return errors


def reminders_errors(part):
    if not isinstance(part, dict):
        return ["reminders: нужен ключ groups"]
    errors = ["reminders: лишний ключ %s; ключ раздела — groups" % k for k in part if k != "groups"]
    groups = part.get("groups")
    if not isinstance(groups, list):
        return errors + ["reminders.groups: нужен список групп; групп нет — []"]
    return errors + groups_errors(groups, REMINDER_KEYS, "reminders.groups")


def tasks_errors(part):
    if not isinstance(part, dict):
        return ["tasks: нужны ключи each_task и groups"]
    errors = ["tasks: лишний ключ %s; ключи раздела — each_task, groups" % k for k in part
              if k not in ("each_task", "groups")]
    each = part.get("each_task")
    if not isinstance(each, str) or each not in EACH_TASK:
        errors.append("tasks.each_task: нужно одно из: %s" % ", ".join(EACH_TASK))
    groups = part.get("groups")
    if not isinstance(groups, list) or not groups:
        return errors + ["tasks.groups: нужен список групп, хотя бы одна"]
    return errors + groups_errors(groups, TASK_KEYS, "tasks.groups")


def check(data, user):
    """Ошибки вида по таблице formats.md, раздел «Вид сводки и списка дел»; [] — вид годится.

    user=False — вид по умолчанию: оба раздела обязательны; user=True — свой вид: хотя бы один раздел.
    """
    if not isinstance(data, dict):
        return ["нужен раздел reminders или tasks" if user else "нужны разделы reminders и tasks"]
    errors = ["лишний раздел %s; разделы: %s" % (k, ", ".join(SECTIONS)) for k in data if k not in SECTIONS]
    if user and not any(s in data for s in SECTIONS):
        errors.append("нужен раздел reminders или tasks")
    if not user:
        errors += ["нет раздела %s" % s for s in SECTIONS if s not in data]
    if "reminders" in data:
        errors += reminders_errors(data["reminders"])
    if "tasks" in data:
        errors += tasks_errors(data["tasks"])
    return errors


# ---------------------------------------------------------------- текст файла


def normalize(data):
    """Копия проверенного вида: только разделы; у групп без if_empty — "show"."""
    out = {}
    for section in SECTIONS:
        if section in data:
            part = copy.deepcopy(data[section])
            for group in part["groups"]:
                group.setdefault("if_empty", "show")
            out[section] = part
    return out


def scalar(key, value):
    if key in ("title", "about"):
        return json.dumps(value, ensure_ascii=False)
    if key == "split":
        return "[%s]" % ", ".join(json.dumps(v, ensure_ascii=False) for v in value)
    if key in ("when", "status"):
        return "[%s]" % ", ".join(value)
    if key == "stuck":
        return "yes" if value else "no"
    return str(value)


def group_lines(groups, keys):
    if not groups:
        return ["  groups: []"]
    out = ["  groups:"]
    for group in groups:
        lead = "    - "
        for key in keys:
            if key not in group or (key == "if_empty" and group[key] == "show"):
                continue
            out.append("%s%s: %s" % (lead, key, scalar(key, group[key])))
            lead = "      "
    return out


def render(data, head=USER_HEAD):
    """Текст файла вида по пункту «Текст файла» formats.md; data — проверенный вид (разделы, какие есть)."""
    lines = [head]
    if "reminders" in data:
        lines += ["", "reminders:"] + group_lines(data["reminders"]["groups"], REMINDER_KEYS)
    if "tasks" in data:
        lines += ["", "tasks:", "  each_task: %s" % data["tasks"]["each_task"]]
        lines += group_lines(data["tasks"]["groups"], TASK_KEYS)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- загрузка


def parse(text, name):
    """(данные, ошибки) текста файла вида; повтор ключа — ошибка."""
    try:
        return common.load_yaml(text), []
    except yaml.YAMLError as e:
        return None, ["%s не разбирается: %s" % (name, common.yaml_error(e))]


def load_default():
    """Вид по умолчанию, проверенный и с if_empty у групп; файл движка испорчен — Fail(1)."""
    try:
        text = common.read_text(common.VIEWS_DEFAULT)
    except (OSError, UnicodeDecodeError) as e:
        raise Fail(1, "%s не читается: %s" % (common.VIEWS_DEFAULT, e)) from None
    data, errors = parse(text, common.VIEWS_DEFAULT)
    if not errors:
        errors = ["%s: %s" % (common.VIEWS_DEFAULT, e) for e in check(data, user=False)]
    if errors:
        raise Fail(1, "; ".join(errors))
    return normalize(data)


def read_user():
    """(разобранный views.yaml или None, ошибки). Файла нет — (None, []); не разбирается — (None, [ошибка])."""
    if not os.path.lexists(common.path(common.VIEWS)):
        return None, []
    try:
        text = common.read_text(common.VIEWS)
    except (OSError, UnicodeDecodeError) as e:
        return None, ["%s не читается: %s" % (common.VIEWS, e)]
    data, errors = parse(text, common.VIEWS)
    if not errors:
        errors = ["%s: %s" % (common.VIEWS, e) for e in check(data, user=True)]
    return data, errors


def load():
    """(вид, source, ошибки): вид по умолчанию, поверх — разделы views.yaml; source — "user" или "default".

    views.yaml не читается или не проходит check — вид по умолчанию и ошибки. У групп без if_empty — "show".
    """
    default = load_default()
    data, errors = read_user()
    if data is None or errors:
        return default, "default", errors
    view = dict(default)
    view.update(normalize(data))
    return view, "user", []


def custom(view, default):
    """Разделы вида, которые отличаются от вида по умолчанию: только их и пишет views.yaml."""
    return {s: view[s] for s in SECTIONS if s in view and view[s] != default[s]}


# ---------------------------------------------------------------- группы задач


def task_state(header, day, stuck_days):
    """Статус, срок, зависание и область открытой задачи; закрытая — None; due или updated не дата — ValueError."""
    status = header.get("status")
    if status not in OPEN:
        return None
    due = header.get("due")
    due_day = None if due == "none" else common.parse_day(due)
    updated = common.parse_day(header.get("updated"))
    area = header.get("area")
    return {"status": status, "due": due_day, "area": area if isinstance(area, str) else "",
            "stuck": status in ("open", "doing") and (day - updated).days > stuck_days}


def fits(group, state, day):
    """Задача подходит условиям группы: when, days, status, stuck, area — через «и», значения в списке — через «или»."""
    when = group.get("when")
    if when:
        due = state["due"]
        hit = (("overdue" in when and due is not None and due < day)
               or ("today" in when and due == day)
               or ("upcoming" in when and due is not None and day < due <= day + _dt.timedelta(days=group["days"]))
               or ("nodue" in when and due is None))
        if not hit:
            return False
    if "status" in group and state["status"] not in group["status"]:
        return False
    if "stuck" in group and state["stuck"] != group["stuck"]:
        return False
    if "area" in group and state["area"] != group["area"]:
        return False
    return True


def task_groups(tasks_view, header, day, stuck_days):
    """Названия групп раздела tasks, условиям которых подходит задача с шапкой header в день day, по порядку вида.

    about, split и if_empty — не условия. Закрытая задача — []; due или updated не дата — ValueError.
    """
    state = task_state(header, day, stuck_days)
    if state is None:
        return []
    return [g["title"] for g in tasks_view["groups"] if fits(g, state, day)]


# ---------------------------------------------------------------- разовый перенос правил о виде


def crosses(note):
    """Пояснение строки обновления «<было> → <стало>» пересекает RULES_SINCE снизу вверх."""
    m = UPDATE_NOTE.match(note or "")
    if not m:
        return False
    was, now = (common.version_tuple(v) for v in m.groups())
    return was < RULES_SINCE <= now


def rule_lines():
    """Строк «- …» в «## Правила» rules.local.md; файла нет или не читается — 0."""
    try:
        text = common.read_text(common.RULES)
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for _, line in common.section_lines(text, "Правила") if line.startswith("- "))


def rules_state():
    """{"needed", "pending", "rules"} разового переноса правил о виде.

    needed — последняя по времени из строк журнала «engine · обновление движка · engine.lock · X → Y» (X ниже
    RULES_SINCE, Y — не ниже) и «seb-rules · правила о виде перенесены» — строка обновления; файлы журнала — от нового к
    старому, в одну минуту отметка считается позже. rules — строк в «## Правила»; pending — needed и rules > 0.
    """
    folder = common.path(common.LOG)
    names = sorted((n for n in os.listdir(folder) if DAY_FILE.match(n)), reverse=True) if os.path.isdir(folder) else []
    needed = False
    for name in names:
        events = []
        with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines():
                m = common.LOG_RE.match(line)
                if not m:
                    continue
                if m.group("who") == "seb-rules" and m.group("action") == A_MOVED:
                    events.append((m.group("time"), 1))
                elif m.group("who") == "engine" and m.group("action") == A_UPDATE and crosses(m.group("note")):
                    events.append((m.group("time"), 0))
        if events:
            needed = max(events)[1] == 0
            break
    rules = rule_lines()
    return {"needed": needed, "pending": needed and rules > 0, "rules": rules}


# ---------------------------------------------------------------- запись


def head_has(rel):
    """Путь есть в HEAD; git не ответил — считать, что есть: путь отдаётся, commit.py сам скажет, если сохранять нечего."""
    try:
        return common.in_head(rel, timeout=30)
    except Fail:
        return True


def save(keep):
    """Свой вид — разделы keep (без равных умолчанию); пустой — файла нет. (changed, files)."""
    full = common.path(common.VIEWS)
    if not keep:
        if not os.path.lexists(full):
            return False, []
        os.remove(full)
    else:
        text = render(keep)
        try:
            old = common.read_text(common.VIEWS)
        except (OSError, UnicodeDecodeError):
            old = None
        if text == old:
            return False, []
        common.write_atomic(common.VIEWS, text)
    files = [common.VIEWS] if os.path.lexists(full) or head_has(common.VIEWS) else []
    return True, files


# ---------------------------------------------------------------- команды


def emit(args, data, text):
    if args.json:
        print(json.dumps(data, ensure_ascii=False, default=str, skipkeys=True))
    else:
        print(text)


def refuse(args, errors):
    if args.json:
        print(json.dumps({"errors": errors}, ensure_ascii=False))
    else:
        print("\n".join(errors), file=sys.stderr)
    return 1


def answer(args, changed, files):
    view, source, _ = load()
    emit(args, {"files": files, "changed": changed, "view": view, "source": source},
         "вид %s; действует %s" % ("изменён" if changed else "уже такой",
                                  "свой вид" if source == "user" else "вид по умолчанию"))
    return 0


def cmd_show(args):
    default = load_default()
    user, errors = read_user()
    view, source, _ = load()
    head = "# Вид сейчас — %s" % ("свой вид пользователя" if source == "user" else "вид по умолчанию")
    text = render(view, head) + "".join("ошибка своего вида: %s\n" % e for e in errors)
    emit(args, {"view": view, "source": source, "user": user, "default": default, "errors": errors}, text.rstrip("\n"))
    return 0


def cmd_set(args):
    try:
        with open(args.src, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise Fail(2, "нет файла %s" % args.src) from None
    except (OSError, UnicodeDecodeError) as e:
        raise Fail(2, "%s не читается: %s" % (args.src, e)) from None
    data, errors = parse(text, args.src)
    if not errors:
        errors = check(data, user=True)
    if errors:
        return refuse(args, errors)
    default = load_default()
    changed, files = save(custom(normalize(data), default))
    return answer(args, changed, files)


def cmd_reset(args):
    default = load_default()
    keep = {}
    if args.section:
        data, errors = read_user()
        if errors:
            return refuse(args, errors + ["вернуть вид по умолчанию целиком — reset без --section"])
        if data is not None:
            keep = {s: v for s, v in custom(normalize(data), default).items() if s != args.section}
    changed, files = save(keep)
    return answer(args, changed, files)


def cmd_rules_moved(args):
    """Разовый перенос правил о виде сделан: строка журнала от имени seb-rules, если перенос ещё нужен."""
    if not rules_state()["needed"]:
        emit(args, {"files": []}, "перенос правил о виде уже отмечен")
        return 0
    rel, _ = common.journal_add("seb-rules", A_MOVED, common.RULES)
    emit(args, {"files": [rel]}, "перенос правил о виде отмечен")
    return 0


def main():
    p = Parser(prog="views.py")
    sub = p.add_subparsers(dest="cmd")
    for name in ("show", "rules-moved"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("--from", dest="src", required=True)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("reset")
    s.add_argument("--section", choices=SECTIONS)
    s.add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.error = p.error
    args = p.parse_args()
    handlers = {"show": cmd_show, "set": cmd_set, "reset": cmd_reset, "rules-moved": cmd_rules_moved}
    if args.cmd not in handlers:
        raise Fail(2, "команда: show, set, reset или rules-moved")
    return handlers[args.cmd](args)


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
