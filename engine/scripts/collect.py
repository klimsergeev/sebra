"""Факты для сводки и обзора; текущее время; slug; догрузка истории.

python3 engine/scripts/collect.py now [--json]
python3 engine/scripts/collect.py history
python3 engine/scripts/collect.py slug "<название>" [--json]
python3 engine/scripts/collect.py daily      (всегда JSON)
python3 engine/scripts/collect.py weekly     (всегда JSON)

Поля daily: date, boundary, reminders, tasks, gardener, documents, closed, deleted, errors.
weekly — те же с границей «начало дня week_from», плюс week_from, week_to, records, new_pages, facts_added,
new_tasks, corrections, engine. Сбой поля — null и запись {"field", "message"} в errors, выход 0.
"""

import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import unicodedata

import common
from common import Fail

# ICAO Doc 9303, часть 3, раздел 6, таблица B
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "ie", "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
}

DATE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
DATED_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
GARDENER_ACTIONS = ("слито", "исправлено", "пересобрано из записи", "удалено", "устарело", "противоречие",
                    "привязано")
DIGESTER_DONE = ("разобрано", "не читается")
FACT_PAGES = ("data/wiki/topics", "data/wiki/people", "data/wiki/projects")


# ---------------------------------------------------------------- now, slug, history


def cmd_now(json_mode):
    moment = common.now()
    tz = common.config().get("owner.timezone", "UTC")
    weekday = common.WEEKDAYS[moment.weekday()]
    if json_mode:
        print(json.dumps({
            "datetime": common.fmt_dt(moment), "date": moment.strftime("%Y-%m-%d"),
            "time": moment.strftime("%H:%M"), "weekday": weekday, "timezone": tz,
        }, ensure_ascii=False))
    else:
        print("%s %s %s" % (common.fmt_dt(moment), tz, weekday))
    return 0


def make_slug(title):
    text = "".join(TRANSLIT.get(ch, ch) for ch in title.lower())
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    words = re.findall(r"[a-z0-9]+", text)
    return "-".join(words[:6])


def cmd_slug(title, json_mode):
    slug = make_slug(title)
    if not slug:
        raise Fail(1, "название без букв и цифр")
    if json_mode:
        print(json.dumps({"title": title, "slug": slug}, ensure_ascii=False))
    else:
        print(slug)
    return 0


def cmd_history():
    common.ensure_repo()
    print("история %s" % common.ensure_history())
    return 0


# ---------------------------------------------------------------- чтение базы


def parse_date(value, field):
    if not isinstance(value, str):
        raise ValueError("%s: не строка-дата" % field)
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def md_files(folder):
    return sorted(glob.glob(common.path(folder, "*.md")))


def rel(p):
    return os.path.relpath(p, common.ROOT).replace(os.sep, "/")


def read_header(p):
    with open(p, encoding="utf-8") as f:
        return common.front_matter(f.read())


def first_commit():
    roots = common.git_out("rev-list", "--max-parents=0", "HEAD").split()
    if not roots:
        raise ValueError("в репозитории нет коммитов")
    times = [common.to_user_time(common.git_out("log", "-1", "--format=%cI", r)) for r in roots]
    return min(times)


def git_quiet(*args):
    """git, который в поле даёт ValueError вместо выхода из скрипта."""
    try:
        return common.git(*args)
    except Fail as e:
        raise ValueError(e.message) from None


def run_script(name, *args):
    script = common.path(common.ENGINE_DIR, "scripts", name)
    if not os.path.exists(script):
        raise ValueError("нет engine/scripts/%s" % name)
    try:
        cp = subprocess.run([sys.executable, script, *args], cwd=common.ROOT, capture_output=True,
                            encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        raise ValueError("%s: нет ответа за 300 секунд" % name) from None
    try:
        return json.loads(cp.stdout)
    except ValueError:
        raise ValueError("%s: вывод не JSON, код %d: %s" % (name, cp.returncode,
                                                             (cp.stderr or cp.stdout).strip()[:300])) from None


class Boundary:
    """Граница: момент (aware datetime). inclusive — строки журнала той же минуты входят (начало дня)."""

    def __init__(self, moment, inclusive=False):
        self.moment = moment
        self.inclusive = inclusive

    def after(self, day, hhmm):
        key, b = (day, hhmm), (self.moment.date(), self.moment.strftime("%H:%M"))
        return key >= b if self.inclusive else key > b


def log_lines(day_from, day_to):
    """Строки журнала с датами файлов от day_from до day_to: [(дата, разобранная строка)]."""
    out = []
    for p in md_files(common.LOG):
        m = DATE_NAME.match(os.path.basename(p))
        if not m:
            continue
        try:
            d = parse_date(m.group(1), "log")
        except ValueError:
            continue
        if not day_from <= d <= day_to:
            continue
        with open(p, encoding="utf-8") as f:
            for line in f.read().splitlines():
                lm = common.LOG_RE.match(line)
                if lm:
                    out.append((d, lm))
    # после merge=union строки идут в порядке отправки, а не по времени
    out.sort(key=lambda x: (x[0], x[1].group("time")))
    return out


def lines_after(boundary, today):
    if boundary is None:
        raise ValueError("нет границы прошлой сводки")
    return [(d, lm) for d, lm in log_lines(boundary.moment.date(), today) if boundary.after(d, lm.group("time"))]


# ---------------------------------------------------------------- поля


def field_boundary(today):
    """Время коммита, в котором появился последний файл сводки с датой раньше сегодня."""
    days = []
    for p in md_files(common.REPORTS_DAILY):
        m = DATE_NAME.match(os.path.basename(p))
        if m:
            try:
                d = parse_date(m.group(1), "daily")
            except ValueError:
                continue
            if d < today:
                days.append(d)
    if not days:
        return Boundary(first_commit())
    last = max(days)
    out = git_quiet("log", "--diff-filter=A", "-1", "--format=%cI", "--",
                    "%s/%s.md" % (common.REPORTS_DAILY, last)).stdout.strip()
    if out:
        return Boundary(common.to_user_time(out))
    # файла прошлой сводки ещё нет в истории — граница: начало следующего за ней дня
    return Boundary(dt.datetime.combine(last + dt.timedelta(days=1), dt.time(0, 0), tzinfo=common.timezone()),
                    inclusive=True)


def field_reminders(today, errors):
    data = run_script("remind.py", "show", "--through", today.isoformat(), "--json")
    if "reminders" not in data:
        raise ValueError("remind.py show: %s" % data.get("error", "нет поля reminders"))
    if data.get("errors"):
        errors.append({"field": "reminders", "message": "; ".join(str(e) for e in data["errors"])})
    return data["reminders"]


def field_tasks(today, cfg):
    groups = {"overdue": [], "today": [], "stuck": [], "doing": [], "waiting": []}
    stuck_days = cfg["tasks.stuck_after_days"]
    for p in md_files(common.TASKS):
        try:
            h = read_header(p)
            status = h.get("status")
            if status not in ("open", "doing", "waiting", "done", "dropped"):
                raise ValueError("status: %r" % status)
            if status not in ("open", "doing", "waiting"):
                continue
            due = h.get("due")
            due_date = None if due == "none" else parse_date(due, "due")
            updated = parse_date(h.get("updated"), "updated")
            title = h.get("title")
            if not isinstance(title, str):
                raise ValueError("title: не строка")
        except (ValueError, OSError, UnicodeDecodeError) as e:
            raise ValueError("%s: %s" % (rel(p), e)) from None
        item = {"path": rel(p), "title": title, "due": due, "status": status, "updated": h.get("updated")}
        if due_date is not None and due_date < today:
            groups["overdue"].append(item)
        elif due_date is not None and due_date == today:
            groups["today"].append(item)
        if status in ("open", "doing") and (today - updated).days > stuck_days:
            groups["stuck"].append(item)
        if status == "doing":
            groups["doing"].append(item)
        if status == "waiting":
            groups["waiting"].append(item)
    return groups


def field_gardener(boundary, today):
    groups = {name: [] for name in GARDENER_ACTIONS + ("прочее",)}
    total = 0
    for d, lm in lines_after(boundary, today):
        if lm.group("who") != "seb-gardener" or lm.group("action") is None:
            continue
        action = lm.group("action")
        groups[action if action in GARDENER_ACTIONS else "прочее"].append(
            {"date": d.isoformat(), "time": lm.group("time"), "action": action, "path": lm.group("path"),
             "note": lm.group("note") or ""})
        total += 1
    return {"groups": groups, "total": total}


def record_header(p):
    try:
        return read_header(p)
    except (ValueError, OSError, UnicodeDecodeError) as e:
        raise ValueError("%s: %s" % (rel(p), e)) from None


def field_documents(boundary, today):
    processing = []
    for p in md_files(common.RECORDS):
        h = record_header(p)
        if h.get("status") == "processing":
            processing.append({"path": rel(p), "title": h.get("title"), "captured": h.get("captured")})
    finished, seen = [], set()
    for d, lm in lines_after(boundary, today):
        if lm.group("who") != "seb-digester" or lm.group("action") not in DIGESTER_DONE:
            continue
        path = lm.group("path")
        full = common.path(path)
        if path in seen or not path.startswith(common.RECORDS + "/") or not os.path.isfile(full):
            continue
        h = record_header(full)
        if h.get("status") in ("done", "unreadable"):
            seen.add(path)
            finished.append({"path": path, "title": h.get("title"), "status": h.get("status"),
                             "action": lm.group("action"), "date": d.isoformat(), "time": lm.group("time")})
    return {"processing": processing, "finished": finished}


def field_closed(boundary):
    if boundary is None:
        raise ValueError("нет границы прошлой сводки")
    out = git_quiet("log", "--since=%s" % boundary.moment.isoformat(), "--name-only", "--format=", "--",
                    common.TASKS + "/").stdout
    changed = sorted({f for f in out.split("\n") if f})
    since = boundary.moment.date()
    closed = []
    for path in changed:
        full = common.path(path)
        if not os.path.isfile(full):
            continue
        try:
            h = read_header(full)
            status = h.get("status")
            if status not in ("done", "dropped"):
                continue
            when = parse_date(h.get("closed"), "closed")
        except (ValueError, OSError, UnicodeDecodeError) as e:
            raise ValueError("%s: %s" % (path, e)) from None
        if when >= since:
            closed.append({"path": path, "title": h.get("title"), "status": status, "closed": h.get("closed")})
    return closed


def field_deleted(boundary, today):
    return [{"path": lm.group("path"), "who": lm.group("who"), "date": d.isoformat(), "time": lm.group("time"),
             "note": lm.group("note") or ""}
            for d, lm in lines_after(boundary, today)
            if lm.group("action") == "удалено" and (lm.group("path") or "").startswith(common.TASKS + "/")]


# ---------------------------------------------------------------- поля weekly


def dated_files(folder, week_from, today):
    out = []
    for p in md_files(folder):
        m = DATED_NAME.match(os.path.basename(p))
        if not m:
            continue
        try:
            d = parse_date(m.group(1), folder)
        except ValueError:
            continue
        if week_from <= d <= today:
            out.append(p)
    return out


def field_records(week_from, today):
    counts = {}
    for p in dated_files(common.RECORDS, week_from, today):
        kind = record_header(p).get("type")
        if not isinstance(kind, str):
            raise ValueError("%s: type: %r" % (rel(p), kind))
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def field_new_pages(since):
    out = git_quiet("log", "--diff-filter=A", "--since=%s" % since, "--name-only", "--format=", "--",
                    common.WIKI + "/").stdout
    pages = []
    for f in out.split("\n"):
        if f and f not in pages:
            pages.append(f)
    return sorted(pages)


def field_facts_added(week_from, today):
    """Новые факты по текущим файлам: пункты «Факты» и «Решения» с датой от week_from до сегодня."""
    counts = {}
    for folder in FACT_PAGES:
        for p in md_files(folder):
            with open(p, encoding="utf-8") as f:
                text = f.read()
            n, section = 0, None
            for _, line, code in common.lines_outside_code(text):
                if code:
                    continue  # факт в блоке кода — не факт
                if line.startswith("## "):
                    section = line[3:].strip()
                    continue
                m = common.FACT_LINE.match(line) if section in ("Факты", "Решения") else None
                if not m:
                    continue
                try:
                    d = parse_date(m.group(1), "факт")
                except ValueError:
                    continue
                if week_from <= d <= today:
                    n += 1
            if n:
                counts[rel(p)] = n
    return counts


def field_new_tasks(week_from, today):
    out = []
    for p in dated_files(common.TASKS, week_from, today):
        h = record_header(p)
        out.append({"path": rel(p), "title": h.get("title"), "status": h.get("status")})
    return out


def field_corrections(week_from, today):
    return [{"date": d.isoformat(), "time": lm.group("time"), "who": lm.group("who"), "text": lm.group("text")}
            for d, lm in log_lines(week_from, today) if lm.group("text") is not None]


def field_engine():
    try:
        return run_script("update_engine.py", "check", "--json")
    except ValueError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------- сборка


def collect(weekly):
    common.ensure_repo()
    errors = []
    try:
        cfg = common.config()
        common.timezone()
    except Fail as e:
        if e.code == 3:
            raise
        # сломанный config.yaml — сводка всё равно собирается, со значениями по умолчанию
        errors.append({"field": "config", "message": e.message})
        cfg = common.defaults()
        common.use_timezone(common.timezone(cfg["owner.timezone"]))
    moment = common.now()
    today = moment.date()
    result = {"date": today.isoformat()}

    def field(name, fn, *args):
        try:
            value = fn(*args)
        except (ValueError, OSError, UnicodeDecodeError, Fail, KeyError, TypeError) as e:
            errors.append({"field": name, "message": getattr(e, "message", None) or str(e)})
            value = None
        return value

    # неглубокая копия: --diff-filter=A считает добавленными все файлы граничного коммита
    try:
        common.ensure_history()
    except Fail as e:
        if e.code != 3:
            raise
        errors.append({"field": "history", "message": e.message})

    if weekly:
        week_from = today - dt.timedelta(days=6)
        boundary = Boundary(dt.datetime.combine(week_from, dt.time(0, 0), tzinfo=moment.tzinfo), inclusive=True)
    else:
        try:
            boundary = field_boundary(today)
        except (ValueError, Fail, OSError) as e:
            errors.append({"field": "boundary", "message": getattr(e, "message", None) or str(e)})
            boundary = None
    result["boundary"] = boundary.moment.isoformat() if boundary else None
    result["reminders"] = field("reminders", field_reminders, today, errors)
    result["tasks"] = field("tasks", field_tasks, today, cfg)
    result["gardener"] = field("gardener", field_gardener, boundary, today)
    result["documents"] = field("documents", field_documents, boundary, today)
    result["closed"] = field("closed", field_closed, boundary)
    result["deleted"] = field("deleted", field_deleted, boundary, today)
    if weekly:
        result["week_from"] = week_from.isoformat()
        result["week_to"] = today.isoformat()
        result["records"] = field("records", field_records, week_from, today)
        result["new_pages"] = field("new_pages", field_new_pages, boundary.moment.isoformat())
        result["facts_added"] = field("facts_added", field_facts_added, week_from, today)
        result["new_tasks"] = field("new_tasks", field_new_tasks, week_from, today)
        result["corrections"] = field("corrections", field_corrections, week_from, today)
        result["engine"] = field_engine()
    result["errors"] = errors
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def main():
    args = sys.argv[1:]
    json_mode = "--json" in args
    args = [a for a in args if a != "--json"]
    if not args:
        raise Fail(2, "команда: now, history, slug, daily или weekly")
    cmd, rest = args[0], args[1:]
    if cmd == "now" and not rest:
        return cmd_now(json_mode)
    if cmd == "history" and not rest:
        return cmd_history()
    if cmd == "slug" and len(rest) == 1:
        return cmd_slug(rest[0], json_mode)
    if cmd in ("daily", "weekly") and not rest:
        return collect(cmd == "weekly")
    raise Fail(2, "ошибка вызова: collect.py %s" % " ".join(sys.argv[1:]))


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv) or sys.argv[1:2] in (["daily"], ["weekly"]))
