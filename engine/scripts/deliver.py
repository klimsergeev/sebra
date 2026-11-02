"""Доставка сводки дня в диалог пользователя: хуки, отметка «сводку показали», отправка в основную сессию.

python3 "$CLAUDE_PROJECT_DIR/engine/scripts/deliver.py" hook-prompt   (хук UserPromptSubmit, вход — JSON хука в stdin)
python3 "$CLAUDE_PROJECT_DIR/engine/scripts/deliver.py" hook-stop     (хук Stop, вход — JSON хука в stdin)
python3 engine/scripts/deliver.py shown [--live] [--json]   — сводку показали: показанные напоминания убрать, журнал, коммит
python3 engine/scripts/deliver.py send [--json]             — рутина: сообщение в основную сессию (claude -p --cloud)
python3 engine/scripts/deliver.py status [--json]           — состояние сводки на сегодня

Хуки: код выхода всегда 0; сбой — строка «deliver.py: …» в stderr. hook-prompt собирает сводку дня и строку о версии
движка (STALE_TEXT) порознь: сбой одной части не мешает другой, в контекст идёт уцелевшая; сбой обеих или всего хука
(например, не прочитан JSON на входе) — в контекст ничего. hook-stop при сбое ничего не выводит.
Остальные команды: 0 — сделано, 1 — правила или отказ получателя, 2 — вызов, 3 — окружение.
"""

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time

import common
import remind
import views
from common import Fail

DELIVER_DIR = os.environ.get("SEBRA_DELIVER_DIR") or "/tmp/sebra/deliver"
CLAUDE_BIN = os.environ.get("SEBRA_CLAUDE_BIN") or "claude"
GRACE = _dt.timedelta(minutes=90)      # после summary.time рутина ещё может работать
MIDNIGHT = _dt.timedelta(minutes=60)   # сколько после предложения вчерашний снимок ещё ждёт отметки
FETCH_EVERY = 60                        # секунд между fetch в хуке
FETCH_TIMEOUT = 10
SEND_TIMEOUT = 60
LOG_DAYS = 60                           # сколько дней журнала искать основную сессию
MAX_CONTEXT = 9000                      # предел контекста хука — 10 000 знаков
ROUTINE_PREFIX = "Выполни рутину "      # так начинается текст каждой рутины в форме
DELIVERY_PREFIX = "Sebra: сводка дня за "  # так начинается сообщение, которое шлёт send
STALE_TEXT = "Sebra: сессия начата на версии движка %s, в базе стоит %s."
STALE_EVERY = _dt.timedelta(minutes=60)  # напоминание о старой версии — не чаще, если сообщений меньше STALE_PROMPTS
STALE_PROMPTS = 10

A_SESSION = "основная сессия"
A_SHOWN = "сводка показана"
A_SENT = "сводка отправлена в сессию"
A_NOT_SENT = "сводка не отправлена в сессию"
LINK = re.compile(r"\[([^\]]+)\]\([^()\s<>]+\)")
DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
NONE_TEXT = "Нет"                       # пустой раздел и пустая группа в показе (текст владельца 27.10)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


# ---------------------------------------------------------------- состояние дня


def ddmm(day):
    return day.strftime("%d.%m")


def marker(day):
    """Первая строка показа сводки; по ней хук Stop узнаёт, что сводку показали."""
    return "Сводка дня — %s" % ddmm(day)


def daily_rel(day):
    return "%s/%s.md" % (common.REPORTS_DAILY, day.isoformat())


def configured():
    return os.path.exists(common.path(common.CONFIG)) and os.path.exists(common.path(common.ENGINE_LOCK))


def log_matches(day, main=False):
    """Строки журнала дня, разобранные LOG_RE: рабочая копия, при main=True — и origin/main; файла нет — пусто."""
    rel = "%s/%s.md" % (common.LOG, day.isoformat())
    lines = []
    try:
        lines += common.read_text(rel).splitlines()
    except FileNotFoundError:
        pass
    if main:
        try:
            cp = common.git("show", "origin/main:%s" % rel, check=False)
            if cp.returncode == 0:
                lines += cp.stdout.splitlines()
        except Fail:
            pass
    return [m for m in (common.LOG_RE.match(x) for x in lines) if m]


def shown_on(day, main=False):
    """Сводку дня отметили: «сводка показана · <сводка этого дня>» в журнале дня или следующего (полночь)."""
    target = daily_rel(day)
    return any(m.group("who") == "summary" and m.group("action") == A_SHOWN and m.group("path") == target
               for d in (day, day + _dt.timedelta(days=1)) for m in log_matches(d, main))


def registered_session():
    """id основной сессии — из последней строки «summary · основная сессия · <id>» журнала; нет — None."""
    folder = common.path(common.LOG)
    if not os.path.isdir(folder):
        return None
    names = sorted((n for n in os.listdir(folder) if DAY_FILE.match(n)), reverse=True)[:LOG_DAYS]
    for name in names:
        with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        for line in reversed(lines):
            m = common.LOG_RE.match(line)
            if m and m.group("who") == "summary" and m.group("action") == A_SESSION:
                return m.group("path")
    return None


def current_session():
    """id этой облачной сессии; вне облака — None."""
    if os.environ.get("CLAUDE_CODE_REMOTE") != "true":
        return None
    sid = (os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID") or "").strip()
    return sid if re.fullmatch(r"[A-Za-z0-9_-]+", sid) else None


def summary_moment(moment):
    """Время рутины сводки сегодня (summary.time) в поясе пользователя."""
    value = str(common.config().get("summary.time") or "09:07")
    hh, mm = (int(x) for x in value.split(":"))
    return moment.replace(hour=hh, minute=mm)


def live_reminders(day):
    """Напоминания из data/reminders/ с датой не позже day: [{file, line, date, text, overdue}]."""
    files, _ = remind.day_files(day)
    out = []
    for d, rel in files:
        for line in remind.read_lines(rel) or []:
            m = common.REMINDER_RE.match(line)
            if m:
                out.append({"file": rel, "line": line, "date": d.isoformat(), "text": m.group("text"),
                            "overdue": d < day})
    return out


def is_routine(prompt):
    return (prompt or "").lstrip().startswith(ROUTINE_PREFIX)


def is_delivery(prompt):
    return (prompt or "").lstrip().startswith(DELIVERY_PREFIX)


# ---------------------------------------------------------------- текст сводки


def reminder_lines(items):
    """Строки напоминаний: сегодняшние, затем оставшиеся с прошлых дней с хвостом common.REMINDER_LEFT."""
    lines = []
    for r in sorted(items, key=lambda r: r["overdue"]):
        tail = common.reminder_left(common.parse_day(r["date"])) if r["overdue"] else ""
        lines.append("- %s%s" % (r["text"], tail))
    return lines


def reminder_groups(text):
    """Группы «## Напоминаний» файла сводки по порядку файла: [(название, {ключи напоминаний под ним})].

    В разделе нет строк «### » — []. Одно название дважды — одна группа; ключ — common.reminder_key: без хвоста
    прошлого дня, неразрывный пробел — обычный.
    """
    groups = []
    current = None
    for _, line in common.section_lines(text, "Напоминания"):
        if line.startswith("### "):
            title = line[4:].strip()
            current = next((g for g in groups if g[0] == title), None)
            if current is None:
                current = (title, set())
                groups.append(current)
        elif current is not None and line.startswith("- "):
            current[1].add(common.reminder_key(line[2:]))
    return groups


def hidden_groups():
    """Названия групп напоминаний нынешнего вида с if_empty: hide; вид не читается — пусто: группы — как в файле."""
    try:
        view, _, _ = views.load()
    except Exception:  # noqa: BLE001 — показ сводки не зависит от того, прочитался ли вид
        return set()
    return {g["title"] for g in view["reminders"]["groups"] if g["if_empty"] == "hide"}


def render_reminders(items, groups=(), hidden=()):
    """Раздел «Напоминания» показа. Без групп — список; с группами файла сводки — напоминания вне групп списком без
    заголовка, затем группы файла по порядку, напоминание — в первой группе, где стоит его текст. Группа без текущих
    напоминаний — «Нет», а если она в hidden (в нынешнем виде у неё if_empty: hide) — её нет. Текущих напоминаний нет
    совсем — «Нет»."""
    if not items:
        return NONE_TEXT
    if not groups:
        return "\n".join(reminder_lines(items))
    placed = {title: [] for title, _ in groups}
    loose = []
    for r in items:
        key = common.reminder_key(r["text"])
        title = next((t for t, keys in groups if key in keys), None)
        (loose if title is None else placed[title]).append(r)
    parts = ["\n".join(reminder_lines(loose))] if loose else []
    for title, _ in groups:
        if placed[title]:
            parts.append("### %s\n%s" % (title, "\n".join(reminder_lines(placed[title]))))
        elif title not in hidden:
            parts.append("### %s\n%s" % (title, NONE_TEXT))
    return "\n\n".join(parts)


def render_daily(text):
    """Тело сводки без шапки и без раздела «Напоминания»; ссылки — их текстом."""
    _, body = common.split_front_matter(text)
    out, skip = [], False
    for line in body.split("\n"):
        if line.startswith("## "):
            skip = line.strip() == "## Напоминания"
        if not skip:
            out.append(LINK.sub(r"\1", line))
    return "\n".join(out).strip()


def build(day, moment):
    """(текст контекста, снимок) — что показать и какие строки напоминаний показаны."""
    rel = daily_rel(day)
    ready = os.path.exists(common.path(rel))
    items = live_reminders(day)
    head = ["Sebra: сводка дня за %s пользователю ещё не показана." % ddmm(day)]
    if not ready:
        head.append("Рутина сводку за %s не собрала: остальные разделы делает навык seb-today." % ddmm(day))
    head.append("Первая строка показа: «%s»." % marker(day))
    daily_text = common.read_text(rel) if ready else None
    groups = reminder_groups(daily_text) if ready else []
    hidden = hidden_groups() if groups else set()
    parts = ["\n".join(head), "## Напоминания\n\n" + render_reminders(items, groups, hidden)]
    if ready:
        parts.append(render_daily(daily_text))
    text = "\n\n".join(p for p in parts if p)
    if len(text) > MAX_CONTEXT:
        full = os.path.join(DELIVER_DIR, "summary-%s.md" % day.isoformat())
        write_file(full, text + "\n")
        text = text[:MAX_CONTEXT - 300].rstrip() + "\n…\nПолный текст сводки — в файле %s." % full
    snap = {"date": day.isoformat(), "marker": marker(day), "kind": "ready" if ready else "missing",
            "daily": rel if ready else "", "reminders": [{"file": r["file"], "line": r["line"]} for r in items],
            "offered": common.fmt_dt(moment), "session": current_session() or "", "state": "offered"}
    return text, snap


# ---------------------------------------------------------------- снимок в /tmp/sebra/deliver


def write_file(full, text):
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, full)


def snap_path(day):
    return os.path.join(DELIVER_DIR, "%s.json" % day.isoformat())


def load_snap(day):
    try:
        with open(snap_path(day), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def pending_snap(moment):
    """Снимок, предложенный и ещё не отмеченный: сегодняшний или вчерашний не старше MIDNIGHT (ход перешёл полночь).

    Вчерашний старше — забыт: иначе хук Stop утром до времени сводки просил бы показать вчерашнюю.
    """
    day = moment.date()
    for d in (day, day - _dt.timedelta(days=1)):
        s = load_snap(d)
        if not s or s.get("state") != "offered":
            continue
        if d == day:
            return s
        try:
            offered = _dt.datetime.fromisoformat(str(s.get("offered"))).replace(tzinfo=moment.tzinfo)
        except ValueError:
            return None
        return s if moment - offered <= MIDNIGHT else None
    return None


def save_snap(snap):
    write_file(snap_path(common.parse_day(snap["date"])), json.dumps(snap, ensure_ascii=False, indent=1) + "\n")


# ---------------------------------------------------------------- хук UserPromptSubmit


def register(moment):
    """Эта облачная сессия — основная: строка журнала и коммит в фоне. Та же сессия — ничего."""
    sid = current_session()
    if not sid or registered_session() == sid:
        return
    common.journal_add("summary", A_SESSION, sid)
    # фоном: хук не ждёт отправку; commit.py без путей уносит журнал, его блокировка ждёт чужое сохранение
    subprocess.Popen([sys.executable, os.path.join(common.SCRIPTS_DIR, "commit.py"), "--who", "summary",
                      "--message", A_SESSION, "--json"], cwd=common.ROOT, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def maybe_sync(force=False):
    """Догнать main не чаще раза в FETCH_EVERY секунд: сводку рутина отправляет в main уже после старта сессии."""
    stamp = os.path.join(DELIVER_DIR, "fetch.stamp")
    try:
        if not force and _dt.datetime.now().timestamp() - os.path.getmtime(stamp) < FETCH_EVERY:
            return
    except OSError:
        pass
    write_file(stamp, "")
    common.sync_main(timeout=FETCH_TIMEOUT)


def routine_session(inp):
    """Сессия рутины: её первый ход начинается с ROUTINE_PREFIX; id сессии хука запоминается в DELIVER_DIR."""
    sid = str(inp.get("session_id") or "")
    marks = os.path.join(DELIVER_DIR, "routine-sessions")
    try:
        with open(marks, encoding="utf-8") as f:
            known = set(f.read().split())
    except OSError:
        known = set()
    if is_routine(inp.get("prompt")):
        if sid and sid not in known:
            os.makedirs(DELIVER_DIR, exist_ok=True)
            with open(marks, "a", encoding="utf-8") as f:
                f.write(sid + "\n")
        return True
    return bool(sid) and sid in known


def hook_prompt(inp):
    if routine_session(inp) or not configured():
        return None
    try:
        # common.now() — внутри try: испорченный config.yaml (нет пояса пользователя) роняет сводку, но не напоминание
        summary = summary_context(inp, common.now())
    except Exception as e:  # noqa: BLE001 — сбой сводки не мешает напоминанию о версии
        print("deliver.py: %s" % (e.message if isinstance(e, Fail) else e), file=sys.stderr)
        summary = None
    try:
        # после summary_context: догон main мог принести engine.lock новой версии
        notice = stale_notice(inp.get("session_id"), stale_clock())
    except Exception as e:  # noqa: BLE001 — напоминание не мешает сводке
        print("deliver.py: версия сессии не сверена (%s)" % e, file=sys.stderr)
        notice = None
    text = "\n\n".join(p for p in (notice, summary) if p)
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def stale_clock():
    """Момент для частоты напоминания о версии — в UTC. Пояс пользователя нужен только чтобы прочесть SEBRA_NOW без
    смещения; config.yaml не читается — пояс UTC, как collect.py при сломанных настройках берёт пояс по умолчанию."""
    try:
        moment = common.now()
    except Exception:  # noqa: BLE001 — испорченный config.yaml, неизвестный пояс, нет базы поясов
        common.use_timezone(_dt.timezone.utc)
        moment = common.now()
    return moment.astimezone(_dt.timezone.utc)


def stale_notice(sid, moment):
    """Строка для агента, если сессия загрузила CLAUDE.md другой версии, чем в engine.lock сейчас.

    Первое сообщение после расхождения — строка; дальше — когда с прошлой прошло STALE_EVERY или STALE_PROMPTS
    сообщений. Файла сессии нет (сессия начата до этой доработки или хук начала не записал) — он заводится с
    текущей версией, без строки. moment — в UTC (stale_clock()); noticed в файле сессии — тоже UTC: поправка или
    порча config.yaml между сообщениями не сдвигает отсчёт часа на разницу поясов.
    """
    current = common.lock_version()
    if not current:
        return None
    rec = common.load_session(sid)
    if rec is None:
        common.save_session(sid, {"version": current, "notice_for": "", "noticed": "", "prompts": 0})
        return None
    loaded = str(rec.get("version") or "")
    if not loaded or loaded == current:
        return None
    prompts = rec.get("prompts") if isinstance(rec.get("prompts"), int) else 0
    try:
        noticed = _dt.datetime.fromisoformat(str(rec.get("noticed"))).replace(tzinfo=moment.tzinfo)
    except ValueError:
        noticed = None
    due = (rec.get("notice_for") != current or noticed is None or moment - noticed >= STALE_EVERY
           or prompts + 1 >= STALE_PROMPTS)
    if due:
        rec.update(notice_for=current, noticed=common.fmt_dt(moment), prompts=0)
    else:
        rec["prompts"] = prompts + 1
    common.save_session(sid, rec)
    return STALE_TEXT % (loaded, current) if due else None


def summary_context(inp, moment):
    """Текст сводки дня для контекста хода или None."""
    delivery = is_delivery(inp.get("prompt"))
    day = moment.date()
    due = moment >= summary_moment(moment) and not shown_on(day)
    if due:
        try:
            maybe_sync(force=delivery)
        except Exception as e:  # noqa: BLE001 — без сети показываем то, что есть в копии
            print("deliver.py: main не догнан (%s)" % e, file=sys.stderr)
    try:
        # после догона: строка журнала и фоновый коммит регистрации делают копию грязной, перемотка на main не пройдёт
        register(moment)
    except Exception as e:  # noqa: BLE001 — регистрация не мешает показу
        print("deliver.py: основная сессия не записана (%s)" % e, file=sys.stderr)
    # журнал и из origin/main: копия могла не перемотаться (грязная или впереди main)
    if not due or shown_on(day, main=True):
        return None
    waiting = moment < summary_moment(moment) + GRACE  # рутина могла ещё не отправить сводку в main
    if not delivery and waiting and not os.path.exists(common.path(daily_rel(day))):
        return None
    text, snap = build(day, moment)
    save_snap(snap)
    return text


# ---------------------------------------------------------------- хук Stop


def squash(text):
    return re.sub(r"\s+", " ", re.sub(r"[*_#>`]", "", text or "")).strip()


def hook_stop(inp):
    if not configured():
        return None
    snap = pending_snap(common.now())
    if snap is None or inp.get("stop_hook_active"):
        return None
    sday = common.parse_day(snap["date"])
    if shown_on(sday):  # отметила другая сессия или этот же ход
        snap["state"] = "shown"
        save_snap(snap)
        return None
    if squash(snap["marker"]) in squash(inp.get("last_assistant_message")):
        text = "Sebra: сводка дня за %s показана, но не отмечена." % ddmm(sday)
    else:
        text = "Sebra: сводка дня за %s в этом ходе не показана." % ddmm(sday)
    return {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}}


# ---------------------------------------------------------------- команды


def cmd_shown(args):
    common.ensure_repo()
    moment = common.now()
    day = moment.date()
    snap = pending_snap(moment)
    if snap:
        items, source = snap["reminders"], "снимок"
    elif args.live:
        items, source = [{"file": r["file"], "line": r["line"]} for r in live_reminders(day)], "текущие"
    elif shown_on(day):
        out = {"status": "already", "date": day.isoformat(), "reminders": 0, "files": [], "commit": None}
        return 0, out, "сводка за %s уже отмечена" % ddmm(day)
    else:
        raise Fail(1, "сводку за %s в этой сессии не предлагали; отметка по текущим напоминаниям — --live"
                   % ddmm(day))
    sday = common.parse_day(snap["date"]) if snap else day
    changed, deleted = remind.drop_lines(items, "summary")
    common.journal_add("summary", A_SHOWN, daily_rel(sday), source)
    paths = changed + deleted
    cp = subprocess.run([sys.executable, os.path.join(common.SCRIPTS_DIR, "commit.py"), "--who", "summary",
                         "--message", A_SHOWN, "--json"] + paths, cwd=common.ROOT, capture_output=True, text=True)
    try:
        result = json.loads(cp.stdout)
    except ValueError:
        result = {"status": "not_pushed", "error": (cp.stderr or cp.stdout).strip()}
    if snap:
        snap["state"] = "shown"
        save_snap(snap)
    out = {"status": "shown", "date": sday.isoformat(), "reminders": len(items), "files": paths, "commit": result}
    return cp.returncode, out, "сводка за %s отмечена; сохранение: %s" % (ddmm(sday), result.get("status"))


def cmd_send(args):
    day = common.now().date()
    sid = registered_session()
    out = {"status": "", "date": day.isoformat(), "session": sid or "", "error": ""}
    if shown_on(day):
        out["status"] = "skipped"
        return 0, out, "сводку за %s уже показали — не отправляю" % ddmm(day)
    if not sid:
        out.update(status="no_session", error="основная сессия не известна")
        common.journal_add("summary", A_NOT_SENT, daily_rel(day), out["error"])
        return 1, out, out["error"]
    binary = shutil.which(CLAUDE_BIN)
    if not binary:
        out.update(status="failed", error="нет команды %s" % CLAUDE_BIN)
        common.journal_add("summary", A_NOT_SENT, daily_rel(day), out["error"])
        return 3, out, out["error"]
    message = "%s%s готова — покажи её." % (DELIVERY_PREFIX, ddmm(day))
    try:
        cp = subprocess.run([binary, "-p", message, "--cloud", sid, "--output-format", "json"], cwd=common.ROOT,
                            capture_output=True, text=True, timeout=SEND_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        out.update(status="failed", error="нет ответа за %d секунд" % SEND_TIMEOUT)
        common.journal_add("summary", A_NOT_SENT, daily_rel(day), out["error"])
        return 3, out, out["error"]
    try:
        reply = json.loads(cp.stdout)
    except ValueError:
        reply = None
    if isinstance(reply, dict) and reply.get("ok") is True:
        out["status"] = "sent"
        common.journal_add("summary", A_SENT, daily_rel(day), sid)
        return 0, out, "отправлено в сессию %s" % sid
    if isinstance(reply, dict):
        out.update(status="failed", error=str(reply.get("error") or "ok: false"))
        code = 1
    else:
        lines = (cp.stderr or cp.stdout).strip().splitlines()
        out.update(status="failed", error=lines[0] if lines else "код выхода %d" % cp.returncode)
        code = 3
    common.journal_add("summary", A_NOT_SENT, daily_rel(day), "%s: %s" % (sid, out["error"]))
    return code, out, out["error"]


def state(moment=None):
    moment = moment or common.now()
    day = moment.date()
    snap = load_snap(day)
    return {"date": day.isoformat(), "time": common.fmt_dt(summary_moment(moment))[11:],
            "due": moment >= summary_moment(moment), "shown": shown_on(day),
            "daily": daily_rel(day) if os.path.exists(common.path(daily_rel(day))) else "",
            "reminders": len(live_reminders(day)), "session": registered_session() or "",
            "current_session": current_session() or "", "pending": bool(snap and snap.get("state") == "offered")}


def status_line():
    """Строка для хука начала сессии."""
    s = state()
    if s["shown"]:
        return "Сводка за сегодня: показана пользователю"
    if s["daily"]:
        return "Сводка за сегодня: собрана, пользователю не показана"
    return "Сводки за сегодня нет"


def cmd_status(args):
    s = state()
    return 0, s, "\n".join("%s: %s" % (k, v) for k, v in s.items())


# ---------------------------------------------------------------- вход


HOOKS = {"hook-prompt": hook_prompt, "hook-stop": hook_stop}


def run_hook(name):
    """Hook hook-prompt or hook-stop; exactly one trace event `hook` in the end and exit code 0.

    Hooks bypass common.run_main (it would add a `script` event): __main__ dispatches them before it.
    """
    start = time.time()
    inp = {}
    try:
        try:
            inp = json.loads(sys.stdin.read() or "{}")
            out = HOOKS[name](inp if isinstance(inp, dict) else {})
            if out:
                print(json.dumps(out, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 — хук не падает и не мешает ходу
            print("deliver.py: %s" % (e.message if isinstance(e, Fail) else e), file=sys.stderr)
    finally:
        sid, pid = common.hook_ids(inp)
        common.trace_mark("hook", "deliver.py " + name, start, sid=sid, pid=pid)
    sys.exit(0)


def main():
    p = Parser(prog="deliver.py")
    sub = p.add_subparsers(dest="cmd")
    # hook-prompt and hook-stop without extra arguments never get here (see __main__); with extra arguments the
    # parser refuses with code 2, as before
    sub.add_parser("hook-prompt")
    sub.add_parser("hook-stop")
    s = sub.add_parser("shown")
    s.add_argument("--live", action="store_true")
    s.add_argument("--json", action="store_true")
    for name in ("send", "status"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.error = p.error
    args = p.parse_args()
    handlers = {"shown": cmd_shown, "send": cmd_send, "status": cmd_status}
    if args.cmd not in handlers:
        raise Fail(2, "команда: hook-prompt, hook-stop, shown, send или status")
    code, out, text = handlers[args.cmd](args)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(text, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in HOOKS:
        run_hook(sys.argv[1])
    common.run_main(main, common.has_json_flag(sys.argv))
