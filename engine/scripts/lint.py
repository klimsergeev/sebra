"""Проверка базы: форматы файлов данных, настроек и engine.lock. Только читает.

python3 engine/scripts/lint.py [--json] [пути…]

Без путей проверяет всю базу; с путями — только эти файлы (все правила, кроме root, layout и engine).
Правила и их имена — план Sebra (dev/plans/plan-sebra-2026-10-06.md в sebra-dev), раздел «Скрипты», подраздел
«lint.py»; форматы — engine/docs/formats.md.
Коды: 0 — нарушений нет; 1 — есть нарушения; 2 — ошибка вызова; 3 — окружение.
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys

import common
from common import Fail, UniqueKeyLoader

import yaml

# ---------------------------------------------------------------- раскладка

ROOT_OWN = (common.CONFIG, common.RULES, common.ENGINE_LOCK)
INDEX = common.INDEX
DATA_SUBDIRS = ("records", "wiki", "tasks", "reminders", "reports", "log")
NESTED = {"wiki": ("topics", "people", "projects", "sources"), "reports": ("daily", "weekly")}
KIND_BY_DIR = {
    common.RECORDS: "record", "data/wiki/topics": "topic", "data/wiki/people": "person",
    "data/wiki/projects": "project", "data/wiki/sources": "source", common.TASKS: "task",
    common.REMINDERS: "reminder", common.REPORTS_DAILY: "daily", common.REPORTS_WEEKLY: "weekly",
    common.LOG: "log",
}
PAGE_KINDS = ("topic", "person", "project")
WIKI_KINDS = PAGE_KINDS + ("source",)
WITH_HEADER = ("record",) + WIKI_KINDS + ("task", "daily", "weekly")

DATE = r"\d{4}-\d{2}-\d{2}"
SLUG_N = common.SLUG + r"(?:-[1-9][0-9]*)?"
_DATED = re.compile(r"(?P<d>%s)-%s\.md" % (DATE, SLUG_N))
_DAY = re.compile(r"(?P<d>%s)\.md" % DATE)
_PLAIN = re.compile(r"%s\.md" % SLUG_N)
NAMES = {
    "record": re.compile(r"(?P<d>%s)-(?P<t>\d{4})-%s\.md" % (DATE, SLUG_N)),
    "topic": _PLAIN, "person": _PLAIN, "project": _PLAIN,
    "source": _DATED, "task": _DATED,
    "reminder": _DAY, "daily": _DAY, "weekly": _DAY, "log": _DAY,
}

# ---------------------------------------------------------------- разделы

SECTIONS = {
    "topic": ["Кратко", "Факты", "Связанное", "Противоречия"],
    "person": ["Кратко", "Факты", "Связанное", "Противоречия"],
    "project": ["Кратко", "Факты", "Решения", "Задачи", "Связанное", "Противоречия"],
    "source": ["О чём", "Ключевые цитаты", "Что затронуто"],
    "task": [("Описание", False), "История"],
    "daily": ["Напоминания", "Дела на день", "Что сделал садовник", "Большие документы"],
    "weekly": ["Что пришло", "Что выросло", "Задачи", "Что сделал садовник", "Движок"],
    "index": ["Темы", "Люди", "Проекты", "Источники (последние 30)"],
    "rules": ["Кто я", "Правила"],
}
DAILY_SUB = ["Просрочено", "На сегодня", "Зависшие", "В работе", "Ждёт ответа"]
UPDATE = re.compile(r"^Обновление ((?:[01]\d|2[0-3]):[0-5]\d)$")
EMPTY_DAY = "Дел нет, ничего не менялось, зависших задач нет"
REPORT_KINDS = ("daily", "weekly")

# ---------------------------------------------------------------- строки тела

FACT_HEAD = re.compile(r"^- \*\*(?P<d>%s)\*\* — (?P<text>\S.*)$" % DATE)
OBSOLETE = re.compile(r" \(устарело (?P<d>%s)\)$" % DATE)
SOURCE_LINE = re.compile(r"^Источник: \[[^\]]+\]\((?P<target>[^()\s<>]+)\)$")
QUOTE = re.compile(r"^> «.+»$")
TRANSLATION = re.compile(r"^> Перевод: «.+»$")
CURRENT = re.compile(r"^Текущее: \S.*? — (?:ответ пользователя (?P<d>%s)|по надёжности источника|по свежести)$" % DATE)
ITEM = re.compile(r"^- \[[^\]]+\]\((?P<target>[^()\s<>]+)\)(?: — \S.*)?$")
DAILY_TASK = re.compile(r"^- \[[^\]]+\]\((?P<target>[^()\s<>]+)\)")
HISTORY = re.compile(r"^- (?P<d>%s) — \S.*$" % DATE)
RULE = re.compile(r"^- (?P<d>%s) — (?:не )?делать \S.*$" % DATE)
INDEX_LINE = re.compile(r"^- \[[^\]]+\]\(wiki/(?P<folder>topics|people|projects|sources)/(?P<name>[^()\s<>/]+\.md)\)"
                        r" — (?P<area>work|personal) — \S.*$")
INDEX_TARGET = re.compile(r"\]\(wiki/(?P<folder>topics|people|projects|sources)/(?P<name>[^()\s<>/]+\.md)\)")
INDEX_FOLDERS = {"Темы": "topics", "Люди": "people", "Проекты": "projects", "Источники (последние 30)": "sources"}
SOURCES_IN_INDEX = 30
# Разделы из пунктов-ссылок и папка их целей.
LINK_ITEMS = {
    "topic": {"Связанное": common.WIKI}, "person": {"Связанное": common.WIKI},
    "project": {"Задачи": common.TASKS, "Связанное": common.WIKI}, "source": {"Что затронуто": common.WIKI},
}
FIRST_LINE = {"index": "# Оглавление", "rules": "# Мои правила"}
QUOTE_CHARS = {ord(c): None for c in "«»“”„\"'"}
GARDENER_ACTIONS = ("слито", "исправлено", "пересобрано из записи", "противоречие", "привязано", "устарело", "удалено")
UNREADABLE_BODY = re.compile(r"Содержимое не читается: \S[^\n]*\n?")
IMAGE_LINE = "Что на картинке: "

# ---------------------------------------------------------------- engine.lock

ENGINE_FIELDS = ("version", "source", "commit", "installed", "stashed_config", "files")
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
AGENT_FILE = re.compile(r"\.claude/agents/seb-(?P<name>[^/]+)\.md")


def not_engine_path(p):
    """Путь, которого не может быть в files: данные и файлы пользователя."""
    return p == "data" or p.startswith("data/") or p in ROOT_OWN


def real_date(text, fmt="%Y-%m-%d"):
    try:
        dt.datetime.strptime(text, fmt)
        return True
    except (ValueError, TypeError):
        return False


def day(text):
    return dt.datetime.strptime(text, "%Y-%m-%d").date()


def short(text, limit=60):
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def normalize_quote(text):
    """Приведение цитаты и тела записи: снять `>`, кавычки «»“”„"', пробелы и переводы строк — один пробел."""
    lines = [re.sub(r"^\s*>", "", line) for line in text.split("\n")]
    return re.sub(r"\s+", " ", " ".join(lines).translate(QUOTE_CHARS)).strip()


def needs_translation(quote):
    """Цитата не русская: в ней есть буквы и нет ни одной кириллической."""
    letters = [c for c in quote if c.isalpha()]
    return bool(letters) and not any("\u0400" <= c <= "\u04ff" for c in letters)


def split_header(text):
    """(шапка или None, тело, сколько строк файла до тела)."""
    head, body = common.split_front_matter(text)
    if head is None:
        return None, text, 0
    return head, body, (head.count("\n") + 1 if head else 0) + 2


def resolve(rel, target):
    """Цель ссылки из файла rel → путь от корня базы (якорь отрезан)."""
    path = target.split("#", 1)[0]
    return os.path.normpath(os.path.join(os.path.dirname(rel), path)).replace(os.sep, "/")


def in_folder(path, folder):
    return path.startswith(folder + "/") and path.endswith(".md")


# ================================================================ поля шапки


def v_text(nonempty=False):
    def check(node, value):
        if not isinstance(value, str) or node.style != '"':
            return "нужен текст в двойных кавычках"
        if node.start_mark.line != node.end_mark.line or "\n" in value:
            return "текст — одной строкой, без перевода строки"
        if '"' in value:
            return "внутри текста — кавычки-ёлочки «», а не \""
        if nonempty and not value.strip():
            return "нужен непустой текст"
        return None
    return check


def v_date(allow_empty=False, fmt="date"):
    pattern, strp, word = {
        "date": (DATE, "%Y-%m-%d", "дата ГГГГ-ММ-ДД"),
        "datetime": (DATE + r"T\d{2}:\d{2}", "%Y-%m-%dT%H:%M", "дата со временем ГГГГ-ММ-ДДTЧЧ:ММ"),
    }[fmt]

    def check(node, value):
        if node.style != '"' or not isinstance(value, str):
            return "нужна %s в двойных кавычках" % word
        if allow_empty and value == "":
            return None
        if not re.fullmatch(pattern, value) or not real_date(value, strp):
            return "нужна настоящая %s, а не «%s»" % (word, value)
        return None
    return check


def v_enum(values):
    def check(node, value):
        if node.style is not None:
            return "значение из списка — без кавычек: одно из %s" % ", ".join(values)
        if not isinstance(value, str) or value not in values:
            return "нужно одно из: %s" % ", ".join(values)
        return None
    return check


def v_due(node, value):
    if isinstance(value, str) and value == "none" and node.style == '"':
        return None
    if isinstance(value, str) and value == "none":
        return "«none» без кавычек: нужно \"none\""
    return v_date()(node, value)


def v_published(node, value):
    if not isinstance(value, str) or node.style != '"':
        return "нужна строка в двойных кавычках"
    if value == "":
        return None
    for pattern, fmt in ((r"\d{4}", "%Y"), (r"\d{4}-\d{2}", "%Y-%m"), (DATE, "%Y-%m-%d")):
        if re.fullmatch(pattern, value):
            return None if real_date(value, fmt) else "ненастоящая дата «%s»" % value
    return "нужно \"ГГГГ\", \"ГГГГ-ММ\", \"ГГГГ-ММ-ДД\" или \"\""


def v_size(node, value):
    err = v_text()(node, value)
    if err:
        return err
    if value and not re.fullmatch(r"\d+ [^\s\d].*", value):
        return "нужно «<целое> <единица>» (например «12 страниц») или \"\""
    return None


def path_problem(value, folder):
    """Форма пути в шапке: от корня, без ./ и ../, файл .md в папке folder. Текст ошибки или None."""
    if value.startswith("/") or any(part in (".", "..", "") for part in value.split("/")):
        return "путь «%s» не от корня базы: без ./, ../ и / в начале" % value
    if not in_folder(value, folder):
        return "путь «%s» — не файл .md в папке %s/" % (value, folder)
    return None


def v_path(folder, allow_empty):
    def check(node, value):
        if not isinstance(value, str) or node.style != '"':
            return "нужен путь в двойных кавычках"
        if value == "":
            return None if allow_empty else "нужен путь к файлу в папке %s/" % folder
        return path_problem(value, folder)
    return check


def v_seen(node, value):
    if not isinstance(node, yaml.SequenceNode) or not node.flow_style:
        return "нужен список дат в квадратных скобках: [] или [\"ГГГГ-ММ-ДД\", …]"
    dates = []
    for item in node.value:
        if not isinstance(item, yaml.ScalarNode) or item.style != '"' or not re.fullmatch(DATE, item.value) \
                or not real_date(item.value):
            return "в списке — только настоящие даты \"ГГГГ-ММ-ДД\" в двойных кавычках"
        dates.append(item.value)
    if any(b <= a for a, b in zip(dates, dates[1:])):
        return "даты — по возрастанию, без повторов"
    return None


TEXT, TEXT1 = v_text(), v_text(True)
AREA = v_enum(("work", "personal"))
RECORD_TYPES = ("text", "link", "message", "image", "document", "file", "audio", "video")
RECORD_STATUSES = ("done", "processing", "unreadable")
TASK_STATUSES = ("open", "doing", "waiting", "done", "dropped")
WIKI_COMMON = [("title", TEXT1), ("area", AREA), ("created", v_date()), ("updated", v_date())]

SCHEMA = {
    "record": [("type", v_enum(RECORD_TYPES)), ("captured", v_date(fmt="datetime")), ("title", TEXT1),
               ("pointer", TEXT), ("why", TEXT), ("status", v_enum(RECORD_STATUSES)), ("seen", v_seen),
               ("size", v_size), ("error", TEXT)],
    "topic": [("type", v_enum(("topic",)))] + WIKI_COMMON,
    "person": [("type", v_enum(("person",)))] + WIKI_COMMON + [("role", TEXT)],
    "project": [("type", v_enum(("project",)))] + WIKI_COMMON + [("status", v_enum(("active", "paused", "done")))],
    "source": [("type", v_enum(("source",)))] + WIKI_COMMON + [
        ("record", v_path(common.RECORDS, False)), ("author", TEXT), ("published", v_published)],
    "task": [("type", v_enum(("task",))), ("title", TEXT1), ("area", AREA), ("status", v_enum(TASK_STATUSES)),
             ("due", v_due), ("project", v_path("data/wiki/projects", True)), ("record", v_path(common.RECORDS, True)),
             ("created", v_date()), ("updated", v_date()), ("closed", v_date(allow_empty=True))],
    "daily": [("type", v_enum(("daily",))), ("date", v_date())],
    "weekly": [("type", v_enum(("weekly",))), ("week_from", v_date()), ("week_to", v_date())],
}
LIST_FIELDS = ("seen",)
PATH_FIELDS = ("record", "project")
NAME_DATE_FIELD = {"source": "created", "task": "created", "daily": "date", "weekly": "week_to"}
ATTACHMENT = re.compile(r"^вложение от (?P<d>%s): (?P<name>\S.*)$" % DATE)


def pointer_problem(type_, pointer):
    """pointer по type записи (формат «Запись»): текст ошибки или None."""
    if type_ == "text":
        return None if pointer == "" else "у type text — \"\""
    if type_ == "link":
        return None if re.fullmatch(r"https?://\S+", pointer) else "у type link — адрес http:// или https://"
    if type_ == "message" and pointer == "":
        return None
    m = ATTACHMENT.match(pointer)
    if not m or not real_date(m.group("d")):
        return "у type %s — \"вложение от ГГГГ-ММ-ДД: <имя файла>\"%s" % (
            type_, " или \"\"" if type_ == "message" else "")
    if m.group("name").startswith("/"):
        return "путь /root/… или /tmp/… — не указатель: нужно исходное имя файла"
    return None


# ================================================================ проверка


class Lint:
    def __init__(self):
        self.root = os.path.realpath(common.ROOT)
        self.violations = []
        self.unchecked = []
        self.engine = set()
        self.engine_ok = True
        self._texts = {}
        self._files = None
        self._translate = None
        self._pending_records = []
        self._in_head = set()

    # ---------------------------------------------------------- общее

    def add(self, path, rule, message):
        self.violations.append({"path": path, "rule": rule, "message": message})

    def abs(self, rel):
        return os.path.join(self.root, rel)

    def text_of(self, rel):
        """Текст файла базы или None, если его нет или он не UTF-8."""
        if rel not in self._texts:
            try:
                with open(self.abs(rel), "rb") as f:
                    self._texts[rel] = f.read().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                self._texts[rel] = None
        return self._texts[rel]

    def git(self, *args, **kw):
        return common.git(*args, **kw)

    def list_files(self):
        """Файлы базы: git ls-files (отслеживаемые и новые вне .gitignore), кроме отсутствующих на диске."""
        if self._files is None:
            out = self.git("ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
            self._files = sorted({p for p in out.split("\0") if p and os.path.lexists(self.abs(p))})
        return self._files

    def translate_quotes(self):
        """language.translate_quotes из config.yaml; не читается — true, как в умолчаниях."""
        if self._translate is None:
            self._translate = True
            text = self.text_of(common.CONFIG)
            try:
                data = common.load_yaml(text) if text is not None else None
            except yaml.YAMLError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("language"), dict):
                value = data["language"].get("translate_quotes")
                if isinstance(value, bool):
                    self._translate = value
        return self._translate

    @staticmethod
    def kind(rel):
        """Тип файла базы по папке и имени; None — файл не из форматов (его ловят root, layout или name)."""
        if rel == INDEX:
            return "index"
        if rel == common.RULES:
            return "rules"
        if rel == common.CONFIG:
            return "config"
        if rel == common.ENGINE_LOCK:
            return "engine"
        folder, name = os.path.split(rel)
        if name.startswith(".") or not name.endswith(".md"):
            return None
        return KIND_BY_DIR.get(folder)

    # ---------------------------------------------------------- engine, root, layout

    def check_engine(self, report=True):
        """engine.lock: поля и значения; список файлов движка — в self.engine."""
        rel = common.ENGINE_LOCK
        add = self.add if report else (lambda *a: None)
        text = self.text_of(rel)
        if text is None:
            add(rel, "engine", "не читается как UTF-8")
            self.engine_ok = False
            return
        self.check_engine_text(text, add=add)

    def check_engine_text(self, text, add=None, check_disk=True):
        """Правило engine по тексту engine.lock; check_disk=False — без проверки файлов на диске."""
        rel = common.ENGINE_LOCK
        add = add or self.add
        try:
            data = common.load_yaml(text)
        except yaml.YAMLError as e:
            add(rel, "engine", "не разбирается: %s" % common.yaml_error(e))
            self.engine_ok = False
            return
        if not isinstance(data, dict):
            add(rel, "engine", "не разбирается: нужен набор полей")
            self.engine_ok = False
            return
        for key in ENGINE_FIELDS:
            if key not in data:
                add(rel, "engine", "нет поля %s" % key)
        for key in data:
            if key not in ENGINE_FIELDS:
                add(rel, "engine", "лишнее поле %s" % key)
        v = data.get("version")
        if "version" in data and not (isinstance(v, str) and VERSION.fullmatch(v)):
            add(rel, "engine", "version: нужен номер X.Y.Z без ведущих нулей, а не «%s»" % v)
        v = data.get("source")
        if "source" in data and not (isinstance(v, str) and v.strip()):
            add(rel, "engine", "source: нужен непустой адрес репозитория движка")
        v = data.get("commit")
        if "commit" in data and not (isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v)):
            add(rel, "engine", "commit: нужно 40 знаков 0-9a-f")
        v = data.get("installed")
        if "installed" in data and not (isinstance(v, str) and re.fullmatch(DATE + r"T\d{2}:\d{2}", v)
                                        and real_date(v, "%Y-%m-%dT%H:%M")):
            add(rel, "engine", "installed: нужна дата со временем ГГГГ-ММ-ДДTЧЧ:ММ")
        if "stashed_config" in data and not isinstance(data.get("stashed_config"), dict):
            add(rel, "engine", "stashed_config: нужен словарь (пустой — {})")
        files = data.get("files")
        if "files" not in data:
            self.engine_ok = False
            return
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
            add(rel, "engine", "files: нужен непустой список путей")
            self.engine_ok = False
            return
        engine = set()
        for f in files:
            if f.startswith("/") or any(part in (".", "..", "") for part in f.split("/")):
                add(rel, "engine", "files: путь не от корня базы: %s" % f)
            elif not_engine_path(f):
                add(rel, "engine", "files: путь данных или пользователя — не файл движка: %s" % f)
            else:
                engine.add(f)
                if check_disk and not os.path.lexists(self.abs(f)):
                    add(rel, "engine", "файла движка нет на диске: %s" % f)
        self.engine = engine

    def check_root_layout(self, files):
        tops = {f.split("/", 1)[0] for f in self.engine}
        engine_dirs = {f.split("/", 1)[0] for f in self.engine if "/" in f}
        reported = set()

        def once(path, rule, message):
            if path not in reported:
                reported.add(path)
                self.add(path, rule, message)

        for rel in files:
            parts = rel.split("/")
            top = parts[0]
            if len(parts) == 1:
                if top not in tops and top not in ROOT_OWN:
                    self.add(rel, "root", "файл вне структуры базы")
                continue
            if top != "data" and top not in tops:
                once(top + "/", "root", "папка вне структуры базы")
                continue
            if top in engine_dirs and top != "data":
                if rel not in self.engine:
                    self.add(rel, "layout", "в папке движка только файлы движка (список files в engine.lock)")
                continue
            self.check_data_layout(rel, parts, once)

    def check_data_layout(self, rel, parts, once):
        if len(parts) == 2:
            if parts[1] != "index.md":
                self.add(rel, "layout", "в data/ только index.md и папки %s" % ", ".join(d + "/" for d in DATA_SUBDIRS))
            return
        if parts[1] not in DATA_SUBDIRS:
            once("data/%s/" % parts[1], "layout", "в data/ только index.md и папки %s"
                 % ", ".join(d + "/" for d in DATA_SUBDIRS))
            return
        depth = 3
        if parts[1] in NESTED:
            allowed = NESTED[parts[1]]
            if len(parts) == 3:
                self.add(rel, "layout", "в data/%s/ только папки %s" % (parts[1], ", ".join(d + "/" for d in allowed)))
                return
            if parts[2] not in allowed:
                once("data/%s/%s/" % (parts[1], parts[2]), "layout", "в data/%s/ только папки %s"
                     % (parts[1], ", ".join(d + "/" for d in allowed)))
                return
            depth = 4
        if len(parts) > depth:
            once("/".join(parts[:depth]) + "/", "layout",
                 "вложенная папка: в %s/ только файлы .md" % "/".join(parts[:depth - 1]))
            return
        if parts[-1].startswith("."):
            self.add(rel, "layout", "скрытый файл в папке данных")
        elif not parts[-1].endswith(".md"):
            self.add(rel, "layout", "в папке данных только файлы .md")

    # ---------------------------------------------------------- один файл

    def check_file(self, rel):
        kind = self.kind(rel)
        if kind is None:
            return
        name_match = self.check_name(rel, kind) if kind in NAMES else None
        if not self.check_encoding(rel):
            return
        text = self.text_of(rel)
        if kind == "engine":
            return  # engine.lock — правило engine, только без путей
        if kind == "config":
            self.check_config(text)
            return
        if kind == "reminder":
            self.check_reminder(rel, text)
            return
        if kind == "log":
            self.check_log(rel, text)
            return
        fields, body, offset = None, text, 0
        if kind in WITH_HEADER:
            fields = self.check_header(rel, kind, text, name_match)
            if fields is None:
                return
            _, body, offset = split_header(text)
        if kind == "record":
            self._pending_records.append((rel, text, fields))
            return
        if kind in WIKI_KINDS or kind in ("task", "index"):
            self.check_links(rel, body, offset)
        elif kind in REPORT_KINDS:
            self.check_links(rel, body, offset, full=False)
        self.check_sections(rel, kind, body, offset)
        self.check_items(rel, kind, body, offset, fields)
        if kind in PAGE_KINDS:
            self.check_facts(rel, kind, body, offset)
        if kind == "index":
            self.check_index(rel, body)
        if kind == "rules":
            self.check_rules_length(rel, text)

    def check_name(self, rel, kind):
        name = os.path.basename(rel)
        m = NAMES[kind].fullmatch(name)
        if not m:
            self.add(rel, "name", "имя не по формату папки %s/" % os.path.dirname(rel))
            return None
        d, t = m.groupdict().get("d"), m.groupdict().get("t")
        if d is not None and not real_date(d):
            self.add(rel, "name", "в имени ненастоящая дата %s" % d)
            return None
        if t is not None and not real_date(t, "%H%M"):
            self.add(rel, "name", "в имени ненастоящее время %s" % t)
            return None
        return m

    def check_encoding(self, rel):
        try:
            with open(self.abs(rel), "rb") as f:
                data = f.read()
        except OSError as e:
            self.add(rel, "encoding", "не читается: %s" % e)
            return False
        if data.startswith(b"\xef\xbb\xbf"):
            self.add(rel, "encoding", "метка порядка байтов в начале: нужен UTF-8 без неё")
            return False
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            self.add(rel, "encoding", "не UTF-8")
            return False
        if b"\r" in data:
            self.add(rel, "encoding", "переводы строк CRLF или CR: нужен LF")
            return False
        return True

    # ---------------------------------------------------------- шапка

    def check_header(self, rel, kind, text, name_match):
        """Шапка по формату типа; словарь прошедших проверку значений или None, если шапки нет или она не разбирается."""
        if text.split("\n", 1)[0] != "---":
            self.add(rel, "header", "нет шапки: первая строка должна быть ровно ---")
            return None
        head, _, _ = split_header(text)
        if head is None:
            self.add(rel, "header", "шапка не закрыта строкой ---")
            return None
        try:
            node = yaml.compose(head, Loader=UniqueKeyLoader)
            data = common.load_yaml(head)
        except yaml.YAMLError as e:
            problem = getattr(e, "problem", None) or str(e)
            self.add(rel, "header", "шапка не разбирается: %s" % str(problem).replace("\n", " "))
            return None
        if not isinstance(node, yaml.MappingNode) or not isinstance(data, dict):
            self.add(rel, "header", "шапка не словарь полей")
            return None
        nodes = {k.value: v for k, v in node.value}
        schema = SCHEMA[kind]
        names = [n for n, _ in schema]
        ok = {}
        for key in data:
            if key not in names:
                self.add(rel, "header", "лишнее поле %s" % key)
        for field, check in schema:
            if field not in data:
                self.add(rel, "header", "нет поля %s" % field)
                continue
            value, vnode = data[field], nodes.get(field)
            if value is None:
                self.add(rel, "header", "%s: пустое значение — только \"\"" % field)
                continue
            if field not in LIST_FIELDS and not isinstance(vnode, yaml.ScalarNode):
                self.add(rel, "header", "%s: нужно одно значение, а не список или словарь" % field)
                continue
            if isinstance(vnode, yaml.ScalarNode) and vnode.style == '"':
                inner = head[vnode.start_mark.index + 1:vnode.end_mark.index - 1]
                if "\\" in inner.replace("\\\\", ""):
                    self.add(rel, "header", "%s: обратная косая черта не удвоена (или кавычка \\\" внутри)" % field)
                    continue
            err = check(vnode, value)
            if err:
                if field == "type" and kind != "record":
                    err = "в папке %s нужно %s" % (os.path.dirname(rel) + "/", kind)
                self.add(rel, "header", "%s: %s" % (field, err))
                continue
            ok[field] = value
        self.header_relations(rel, kind, ok, name_match)
        for field in PATH_FIELDS:
            value = ok.get(field)
            if isinstance(value, str) and value and not os.path.isfile(self.abs(value)):
                self.add(rel, "link", "%s: нет файла %s" % (field, value))
        return ok

    def header_relations(self, rel, kind, ok, name_match):
        bad = lambda message: self.add(rel, "header", message)  # noqa: E731
        if kind == "record":
            if "type" in ok and "pointer" in ok:
                problem = pointer_problem(ok["type"], ok["pointer"])
                if problem:
                    bad("pointer: %s" % problem)
            if ok.get("status") == "unreadable" and "error" in ok and not ok["error"].strip():
                bad("error: у status unreadable — непустая причина")
            if ok.get("type") in ("audio", "video") and "status" in ok and ok["status"] != "unreadable":
                bad("status: у %s — всегда unreadable" % ok["type"])
            if "seen" in ok and "captured" in ok and ok["seen"] and ok["seen"][0] < ok["captured"][:10]:
                bad("seen: дата раньше captured")
            if name_match is not None and "captured" in ok:
                want = "%sT%s:%s" % (name_match.group("d"), name_match.group("t")[:2], name_match.group("t")[2:])
                if ok["captured"] != want:
                    bad("дата и время в имени не совпадают с captured")
            return
        if "created" in ok and "updated" in ok and day(ok["updated"]) < day(ok["created"]):
            bad("updated раньше created")
        if kind == "task" and "status" in ok and "closed" in ok:
            closed = ok["closed"]
            if ok["status"] in ("done", "dropped"):
                if closed == "":
                    bad("closed: у status %s — дата закрытия" % ok["status"])
                elif "created" in ok and day(closed) < day(ok["created"]):
                    bad("closed раньше created")
                elif "updated" in ok and day(closed) > day(ok["updated"]):
                    bad("closed позже updated")
            elif closed != "":
                bad("closed: у status %s — \"\"" % ok["status"])
        if kind == "weekly" and "week_from" in ok and "week_to" in ok and \
                day(ok["week_to"]) - day(ok["week_from"]) != dt.timedelta(days=6):
            bad("week_from: должна быть на 6 дней раньше week_to")
        field = NAME_DATE_FIELD.get(kind)
        if name_match is not None and field and field in ok and ok[field] != name_match.group("d"):
            bad("дата в имени не совпадает с %s" % field)

    # ---------------------------------------------------------- ссылки

    def check_links(self, rel, body, offset, full=True):
        """Ссылки тела: форма; full — ещё цель есть и это файл (у сводок и обзоров — только форма)."""
        for n, line, code in common.lines_outside_code(body):
            if code or line.lstrip().startswith(">"):
                continue
            n += offset
            if common.LINK_DEF.match(line):
                self.add(rel, "link", "строка %d: определение ссылки [метка]: — нужна ссылка вида [текст](путь.md)" % n)
                continue
            unclosed = []
            for target in common.link_targets(line, unclosed):
                self.check_target(rel, n, target, full)
            if unclosed:
                self.add(rel, "link", "строка %d: ссылка без закрывающей скобки: «%s»" % (n, short(unclosed[0])))

    def check_target(self, rel, n, target, full):
        if not target:
            self.add(rel, "link", "строка %d: пустой путь ссылки" % n)
            return
        if any(c in target for c in " \t(<>"):
            self.add(rel, "link", "строка %d: путь ссылки не по форме: «%s»" % (n, short(target)))
            return
        path = target.split("#", 1)[0]
        if not path:
            return
        if path.startswith("/"):
            self.add(rel, "link", "строка %d: путь от корня диска, нужен относительный: %s" % (n, path))
            return
        full_path = resolve(rel, path)
        if not full_path.startswith("data/"):
            self.add(rel, "link", "строка %d: ссылка не внутрь data/: %s" % (n, path))
            return
        if not full_path.endswith(".md"):
            self.add(rel, "link", "строка %d: ссылка не на файл .md: %s" % (n, path))
            return
        if not full:
            return
        if os.path.isdir(self.abs(full_path)):
            self.add(rel, "link", "строка %d: ссылка на папку: %s" % (n, path))
        elif not os.path.isfile(self.abs(full_path)):
            self.add(rel, "link", "строка %d: нет файла %s" % (n, path))

    # ---------------------------------------------------------- разделы

    @staticmethod
    def blocks(body, offset, level="## "):
        """Заголовки уровня level вне блоков кода: [(номер строки в файле, название, [(номер, строка, в коде?)])];
        первый элемент — (None, None, строки до первого заголовка)."""
        out = [(None, None, [])]
        for n, line, code in common.lines_outside_code(body):
            if not code and line.startswith(level):
                out.append((n + offset, line[len(level):].strip(), []))
                continue
            out[-1][2].append((n + offset, line, code))
        return out

    def check_order(self, rel, heads, expected, mark="##"):
        """Заголовки — все обязательные, по порядку, по разу, других нет."""
        spec = [e if isinstance(e, tuple) else (e, True) for e in expected]
        order = {title: i for i, (title, _) in enumerate(spec)}
        seen, positions = set(), []
        for n, title in heads:
            if title not in order:
                self.add(rel, "sections", "строка %d: лишний раздел «%s %s»" % (n, mark, title))
                continue
            if title in seen:
                self.add(rel, "sections", "строка %d: раздел «%s %s» повторяется" % (n, mark, title))
                continue
            seen.add(title)
            positions.append((n, title, order[title]))
        for (_, t1, i1), (n2, t2, i2) in zip(positions, positions[1:]):
            if i2 < i1:
                self.add(rel, "sections", "строка %d: раздел «%s %s» стоит после «%s %s» — не по порядку"
                         % (n2, mark, t2, mark, t1))
                break
        for title, required in spec:
            if required and title not in seen:
                self.add(rel, "sections", "нет раздела «%s %s»" % (mark, title))

    def check_sections(self, rel, kind, body, offset):
        blocks = self.blocks(body, offset)
        if kind not in REPORT_KINDS:
            self.check_order(rel, [(n, t) for n, t, _ in blocks[1:]], SECTIONS[kind])
            return
        regular, updates = [], []
        for n, title, lines in blocks[1:]:
            m = UPDATE.match(title)
            if m:
                updates.append((n, m.group(1), lines))
                continue
            if updates:
                self.add(rel, "sections", "строка %d: «## %s» после «Обновления» — в конце только «Обновления»"
                         % (n, title))
                if title in SECTIONS[kind]:
                    regular.append((n, title, lines))  # раздел есть, он только не на месте
                continue
            regular.append((n, title, lines))
        for (_, t1, _), (n2, t2, _) in zip(updates, updates[1:]):
            if t2 < t1:
                self.add(rel, "sections", "строка %d: время «Обновления %s» раньше прежнего %s" % (n2, t2, t1))
        for n, _, lines in updates:
            self.check_update(rel, n, lines)
        head_lines = [line for _, line, _ in blocks[0][2] if line.strip()]
        if kind == "daily" and not regular:
            if head_lines != [EMPTY_DAY]:
                self.add(rel, "sections", "нет разделов: нужны разделы сводки или одна строка «%s»" % EMPTY_DAY)
            return
        self.check_order(rel, [(n, t) for n, t, _ in regular], SECTIONS[kind])
        if kind == "daily":
            for n, line, code in common.lines_outside_code(body):
                if not code and line.strip() == EMPTY_DAY:
                    self.add(rel, "sections", "строка %d: строка пустого дня рядом с разделами" % (n + offset))
        for n, title, lines in regular:
            if kind == "daily" and title == "Дела на день":
                self.check_daily_tasks_sections(rel, n, lines)
            elif not any(line.strip() for _, line, _ in lines):
                self.add(rel, "sections", "строка %d: пустой раздел «## %s» — нужна строка «нет»" % (n, title))

    def check_update(self, rel, n, lines):
        items = 0
        for m, line, code in lines:
            if not code and line.startswith("#"):
                self.add(rel, "sections", "строка %d: заголовок внутри «Обновления»" % m)
                return
            if not line.strip() or code:
                continue
            if line.startswith("- "):
                items += 1
            elif not line.startswith("  "):
                self.add(rel, "sections", "строка %d: «Обновление» — списком «- …»" % m)
                return
        if not items:
            self.add(rel, "sections", "строка %d: пустое «Обновление»" % n)

    def check_daily_tasks_sections(self, rel, n, lines):
        subs, cur = [], None
        for m, line, code in lines:
            if not code and line.startswith("### "):
                cur = (m, line[4:].strip(), [])
                subs.append(cur)
            elif cur is not None:
                cur[2].append(line)
        self.check_order(rel, [(m, t) for m, t, _ in subs], DAILY_SUB, mark="###")
        for m, title, sub in subs:
            if not any(x.strip() for x in sub):
                self.add(rel, "sections", "строка %d: пустой подраздел «### %s» — нужна строка «нет»" % (m, title))

    # ---------------------------------------------------------- пункты

    def check_items(self, rel, kind, body, offset, fields):
        if kind in FIRST_LINE and body.split("\n", 1)[0] != FIRST_LINE[kind]:
            self.add(rel, "items", "первая строка — не «%s»" % FIRST_LINE[kind])
        sections = {t: lines for _, t, lines in self.blocks(body, offset)[1:]}
        for title, folder in LINK_ITEMS.get(kind, {}).items():
            if title in sections:
                self.check_link_items(rel, title, sections[title], folder)
        if kind == "source" and "Ключевые цитаты" in sections:
            quotes = self.check_quote_lines(rel, "items", sections["Ключевые цитаты"], others_allowed=False)
            record = (fields or {}).get("record")
            if record and os.path.isfile(self.abs(record)):
                self.check_quotes(rel, record, quotes, "«Ключевые цитаты»")
        if kind == "task" and "История" in sections:
            lines = [(n, line) for n, line, _ in sections["История"] if line.strip()]
            if not lines:
                self.add(rel, "items", "«История» пустая: нужна хотя бы одна строка «- ГГГГ-ММ-ДД — …»")
            for n, line in lines:
                m = HISTORY.match(line)
                if not m or not real_date(m.group("d")):
                    self.add(rel, "items", "строка %d: «История» — строки «- ГГГГ-ММ-ДД — что изменилось»" % n)
        if kind == "rules" and "Правила" in sections:
            for n, line, _ in sections["Правила"]:
                if not line.strip():
                    continue
                m = RULE.match(line)
                if not m or not real_date(m.group("d")):
                    self.add(rel, "items", "строка %d: правило — «- ГГГГ-ММ-ДД — делать …» или «… — не делать …»" % n)
        if kind == "daily" and "Дела на день" in sections:
            for n, line, code in sections["Дела на день"]:
                if code or not line.strip() or line.startswith("### ") or line.strip() == "нет":
                    continue
                m = DAILY_TASK.match(line)
                if not m or not in_folder(resolve(rel, m.group("target")), common.TASKS):
                    self.add(rel, "items", "строка %d: в «Делах на день» задача — ссылкой на файл data/tasks/" % n)

    def check_link_items(self, rel, title, lines, folder):
        for n, line, _ in lines:
            if not line.strip():
                continue
            m = ITEM.match(line)
            if not m:
                self.add(rel, "items", "строка %d: пункт «%s» — «- [название](путь.md)» и, если надо, « — пояснение»"
                         % (n, title))
                continue
            if not in_folder(resolve(rel, m.group("target")), folder):
                self.add(rel, "items", "строка %d: в «%s» цель — файл в %s/" % (n, title, folder))

    def check_quote_lines(self, rel, rule, lines, others_allowed):
        """Строки цитат `> «…»` и переводов сразу под ними; возвращает [(номер, цитата)] для правила quote.

        lines — [(номер, строка, …)]; others_allowed — другие непустые строки допустимы (пункт факта), иначе нарушение.
        """
        quotes, prev = [], None
        entries = [(x[0], x[1]) for x in lines]
        for i, (n, line) in enumerate(entries):
            if not line.startswith(">"):
                if line.strip() and not others_allowed:
                    self.add(rel, rule, "строка %d: здесь только цитаты «> «…»» и переводы «> Перевод: «…»»" % n)
                prev = None
                continue
            if TRANSLATION.match(line):
                if prev != "quote":
                    self.add(rel, rule, "строка %d: перевод не сразу под цитатой" % n)
                prev = "translation"
                continue
            if line.startswith("> Перевод:"):
                self.add(rel, rule, "строка %d: перевод — «> Перевод: «…»»" % n)
                prev = "translation"
                continue
            if not QUOTE.match(line):
                self.add(rel, rule, "строка %d: цитата — одной строкой «> «…»»" % n)
            quotes.append((n, line))
            prev = "quote"
            nxt = entries[i + 1][1] if i + 1 < len(entries) else ""
            if self.translate_quotes() and needs_translation(line[2:]) and not TRANSLATION.match(nxt):
                self.add(rel, rule, "строка %d: цитата не на русском — сразу под ней «> Перевод: «…»»" % n)
        return quotes

    # ---------------------------------------------------------- факты, противоречия, цитаты

    def check_facts(self, rel, kind, body, offset):
        sections = {t: lines for _, t, lines in self.blocks(body, offset)[1:]}
        for title in ("Факты", "Решения") if kind == "project" else ("Факты",):
            if title in sections:
                for item in self.items_of(rel, sections[title]):
                    self.check_fact_item(rel, item)
        if "Противоречия" in sections:
            for item in self.items_of(rel, sections["Противоречия"]):
                self.check_contradiction(rel, item)

    def items_of(self, rel, lines):
        """Пункты раздела: строка «- …» без отступа и строки с отступом в два пробела под ней."""
        items, cur = [], None
        for n, line, _ in lines:
            if not line.strip():
                if cur is not None:
                    cur["lines"].append((n, ""))
                continue
            if line.startswith("- "):
                cur = {"n": n, "head": line, "lines": []}
                items.append(cur)
            elif line.startswith("  ") and cur is not None:
                cur["lines"].append((n, line[2:]))
            elif line.startswith("  "):
                self.add(rel, "fact", "строка %d: строка с отступом до первого пункта" % n)
            else:
                self.add(rel, "fact", "строка %d: строка без отступа вне пункта: «%s»" % (n, short(line)))
        return items

    def check_head(self, rel, item, what):
        m = FACT_HEAD.match(item["head"])
        if not m:
            self.add(rel, "fact", "строка %d: %s — «- **ГГГГ-ММ-ДД** — текст»" % (item["n"], what))
            return False
        if not real_date(m.group("d")):
            self.add(rel, "fact", "строка %d: ненастоящая дата %s" % (item["n"], m.group("d")))
            return False
        return True

    def check_fact_item(self, rel, item):
        n = item["n"]
        if self.check_head(rel, item, "пункт факта"):
            head = item["head"]
            if "(устарело" in head:
                m = OBSOLETE.search(head)
                if not m or not real_date(m.group("d")):
                    self.add(rel, "fact", "строка %d: пометка — « (устарело ГГГГ-ММ-ДД)» в конце строки пункта" % n)
        sources = [(m, x) for m, x in item["lines"] if x.startswith("Источник:")]
        if len(sources) != 1:
            self.add(rel, "fact", "факт в строке %d: строк «Источник:» %d, нужна одна" % (n, len(sources)))
        quotes = self.check_quote_lines(rel, "fact", item["lines"], others_allowed=True)
        if not quotes:
            self.add(rel, "fact", "факт в строке %d: нет строки цитаты «> «…»»" % n)
        if len(sources) != 1:
            return
        m = SOURCE_LINE.match(sources[0][1])
        target = resolve(rel, m.group("target")) if m else None
        if target is None or not in_folder(target, common.RECORDS):
            self.add(rel, "fact", "факт в строке %d: «Источник: [название](путь)» — ссылка на запись в data/records/" % n)
            return
        if quotes and os.path.isfile(self.abs(target)):
            self.check_quotes(rel, target, quotes, "факт в строке %d" % n)

    def check_contradiction(self, rel, item):
        n = item["n"]
        if self.check_head(rel, item, "пункт противоречия"):
            records = [t for t in common.link_targets(item["head"]) if in_folder(resolve(rel, t), common.RECORDS)]
            if len(records) < 2:
                self.add(rel, "fact", "противоречие в строке %d: ссылок на записи %d, нужно не меньше двух"
                         % (n, len(records)))
        lines = [(m, x) for m, x in item["lines"] if x.strip()]
        if len(lines) > 1:
            self.add(rel, "fact", "противоречие в строке %d: под пунктом только одна строка «Текущее: …»" % n)
            return
        for m, x in lines:
            c = CURRENT.match(x)
            if not c or (c.group("d") and not real_date(c.group("d"))):
                self.add(rel, "fact", "строка %d: «Текущее: <версия> — <причина>», причина — «ответ пользователя "
                                      "ГГГГ-ММ-ДД», «по надёжности источника» или «по свежести»" % m)

    def check_quotes(self, rel, record, quotes, where):
        text = self.text_of(record)
        if text is None:
            return
        _, body = common.split_front_matter(text)
        haystack = normalize_quote(body)
        for n, q in quotes:
            needle = normalize_quote(q)
            if needle and needle not in haystack:
                self.add(rel, "quote", "%s, строка %d: цитаты нет в теле %s: «%s»" % (where, n, record, short(needle)))

    # ---------------------------------------------------------- оглавление

    def pages(self):
        """Страницы вики в базе по папкам: {"topics": [путь, …], …}."""
        out = {f: [] for f in INDEX_FOLDERS.values()}
        for rel in self.list_files():
            parts = rel.split("/")
            if len(parts) == 4 and parts[:2] == ["data", "wiki"] and parts[2] in out and self.kind(rel):
                out[parts[2]].append(rel)
        return out

    def page_area(self, rel):
        text = self.text_of(rel)
        try:
            data = common.front_matter(text) if text is not None else None
        except ValueError:
            return None
        return data.get("area") if isinstance(data, dict) else None

    def check_index(self, rel, body):
        pages = self.pages()
        listed = {}
        for _, title, lines in self.blocks(body, 0)[1:]:
            folder = INDEX_FOLDERS.get(title)
            if folder is None:
                continue
            for n, line, _ in lines:
                if not line.strip():
                    continue
                m = INDEX_LINE.match(line)
                loose = m or INDEX_TARGET.search(line)
                page = "data/wiki/%s/%s" % (loose.group("folder"), loose.group("name")) if loose else None
                if page in listed:
                    self.add(rel, "index", "строка %d: %s уже есть в строке %d" % (n, page, listed[page]))
                    continue
                if page:
                    listed[page] = n
                if not m:
                    self.add(rel, "index", "строка %d: «- [название](wiki/<папка>/<файл>.md) — <area> — <фраза>»" % n)
                    continue
                if m.group("folder") != folder:
                    self.add(rel, "index", "строка %d: в «%s» — страницы из wiki/%s/" % (n, title, folder))
                    continue
                if os.path.isfile(self.abs(page)):
                    area = self.page_area(page)
                    if area in ("work", "personal") and area != m.group("area"):
                        self.add(rel, "index", "строка %d: area «%s», а в шапке %s — «%s»"
                                 % (n, m.group("area"), page, area))
        for folder in ("topics", "people", "projects"):
            for page in pages[folder]:
                if page not in listed:
                    self.add(rel, "index", "нет строки страницы %s" % page)
        newest = sorted(pages["sources"], key=os.path.basename, reverse=True)[:SOURCES_IN_INDEX]
        shown = [p for p in listed if p.startswith("data/wiki/sources/")]
        for page in shown:
            if page not in newest and os.path.isfile(self.abs(page)):
                self.add(rel, "index", "строка %d: %s — не из %d конспектов с наибольшими именами"
                         % (listed[page], page, SOURCES_IN_INDEX))
        for page in newest:
            if page not in listed:
                self.add(rel, "index", "в «Источниках» нет %s — он из %d конспектов с наибольшими именами"
                         % (page, SOURCES_IN_INDEX))

    def check_index_present(self):
        pages = self.pages()
        if not os.path.isfile(self.abs(INDEX)) and any(pages.values()):
            self.add(INDEX, "index", "нет оглавления, а страниц вики %d" % sum(len(v) for v in pages.values()))

    # ---------------------------------------------------------- напоминания, журнал

    def check_reminder(self, rel, text):
        if text == "":
            self.add(rel, "reminder", "пустой файл: опустевший файл напоминаний удаляется")
            return
        lines = text.split("\n")
        if text.endswith("\n"):
            lines = lines[:-1]
        seen = {}
        for n, line in enumerate(lines, 1):
            if not line.strip():
                self.add(rel, "reminder", "строка %d: пустая строка" % n)
                continue
            m = common.REMINDER_RE.match(line)
            if not m:
                self.add(rel, "reminder", "строка %d: не «- <что напомнить> — [запись](../records/<имя>.md)»" % n)
                continue
            if line in seen:
                self.add(rel, "reminder", "строка %d: повтор строки %d" % (n, seen[line]))
                continue
            seen[line] = n
            if not os.path.isfile(self.abs("%s/%s" % (common.RECORDS, m.group("rec")))):
                self.add(rel, "reminder", "строка %d: нет записи %s/%s" % (n, common.RECORDS, m.group("rec")))

    def check_log(self, rel, text):
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                self.add(rel, "log", "строка %d: пустая строка" % n)
                continue
            m = common.LOG_RE.match(line)
            if not m:
                self.add(rel, "log", "строка %d не по формату журнала: «%s»" % (n, short(line)))
            elif m.group("who") == "seb-gardener" and m.group("action") and m.group("action") not in GARDENER_ACTIONS:
                self.add(rel, "log", "строка %d: у seb-gardener действие — одно из: %s" % (n, ", ".join(GARDENER_ACTIONS)))

    # ---------------------------------------------------------- записи и история

    def flush_records(self):
        pending, self._pending_records = self._pending_records, []
        if not pending:
            return
        problem = self.prepare_history([r for r, _, _ in pending])
        added = self.added_versions()
        boundary = self.shallow_boundary() if problem else set()
        head = self.git("rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).stdout.strip()
        wanted = []
        for rel, _, _ in pending:
            if rel in added and added[rel] not in boundary:
                wanted.append((added[rel], rel))
                if head and rel in self._in_head:
                    wanted.append((head, rel))
        texts = self.committed_texts(wanted)
        for rel, text, fields in pending:
            if rel in added and added[rel] in boundary:
                self.unchecked.append({"path": rel, "rule": "record", "message": "история неполная: %s" % problem})
                continue
            sha = added.get(rel)
            self.check_record(rel, text, fields, texts.get((sha, rel)), sha, texts.get((head, rel)))

    def prepare_history(self, rels):
        """Неглубокая копия и записи из HEAD — догрузить историю. Текст сбоя или None."""
        cp = self.git("ls-tree", "-r", "--name-only", "HEAD", "--", common.RECORDS + "/", check=False)
        in_head = self._in_head = set(cp.stdout.split("\n")) if cp.returncode == 0 else set()
        if not in_head.intersection(rels):
            return None
        if self.git("rev-parse", "--is-shallow-repository", check=False).stdout.strip() != "true":
            return None
        try:
            common.ensure_history()
        except Fail as e:
            return e.message
        return None

    def shallow_boundary(self):
        name = self.git("rev-parse", "--git-path", "shallow", check=False).stdout.strip()
        try:
            with open(name if os.path.isabs(name) else self.abs(name), encoding="utf-8") as f:
                return {line.strip() for line in f if line.strip()}
        except OSError:
            return set()

    def added_versions(self):
        """Путь в data/records/ → sha коммита, в котором файл появился (последнее появление)."""
        added = {}
        cp = self.git("log", "--diff-filter=A", "--no-renames", "--format=%H", "--name-only", "--",
                      common.RECORDS + "/", check=False)
        if cp.returncode != 0:
            return added
        sha = None
        for line in cp.stdout.split("\n"):
            if re.fullmatch(r"[0-9a-f]{40}", line):
                sha = line
            elif line and sha and line not in added:
                added[line] = sha
        return added

    def committed_texts(self, wanted):
        """[(sha, путь)] → {(sha, путь): текст файла в этом коммите} — один вызов git cat-file --batch."""
        if not wanted:
            return {}
        env = dict(os.environ)
        env.update(common.GIT_ENV)
        query = "".join("%s:%s\n" % (sha, r) for sha, r in wanted).encode("utf-8")
        try:
            cp = subprocess.run(["git", "cat-file", "--batch"], cwd=self.root, env=env, input=query,
                                capture_output=True, timeout=300)
        except FileNotFoundError:
            raise Fail(3, "git не запускается") from None
        except subprocess.TimeoutExpired:
            raise Fail(3, "git cat-file: нет ответа за 300 секунд") from None
        out, pos, texts = cp.stdout, 0, {}
        for key in wanted:
            nl = out.find(b"\n", pos)
            if nl < 0:
                break
            header = out[pos:nl].split()
            pos = nl + 1
            if len(header) == 3 and header[1] == b"blob":
                size = int(header[2])
                texts[key] = out[pos:pos + size].decode("utf-8", errors="replace")
                pos += size + 1
        return texts

    @staticmethod
    def parse_head(text):
        """Поля шапки записи из истории или None."""
        head, _ = common.split_front_matter(text)
        try:
            data = common.load_yaml(head) if head is not None else None
        except yaml.YAMLError:
            return None
        return data if isinstance(data, dict) else None

    def check_record(self, rel, text, fields, old, sha, last):
        """Правило record: old — версия коммита появления (sha), last — версия в HEAD; нет в истории — None."""
        bad = lambda message: self.add(rel, "record", message)  # noqa: E731
        _, body = common.split_front_matter(text)
        created_status = fields.get("status")
        old_fields = self.parse_head(old) if old is not None else None
        if old is not None:
            where = "версии коммита %s, где запись появилась" % sha[:7]
            if common.split_front_matter(old)[1] != body:
                bad("тело изменено после %s" % where)
            if old_fields is not None:
                created_status = old_fields.get("status")
                for key in sorted(fields):
                    if key not in ("status", "seen", "error") and old_fields.get(key) != fields[key]:
                        bad("поле %s изменено после %s: меняются только status, seen, error" % (key, where))
        versions = [v for v in (old_fields, self.parse_head(last) if last is not None else None) if v is not None]
        for prev in versions:
            was, now = prev.get("status"), fields.get("status")
            if now is not None and was != now and not (was == "processing" and now in ("done", "unreadable")):
                bad("status: с %s на %s — меняется только с processing на done или unreadable" % (was, now))
                break
        for prev in versions:
            was = prev.get("seen")
            if "seen" in fields and isinstance(was, list) and fields["seen"][:len(was)] != was:
                bad("seen: прежние даты %s — начало списка, новые дописываются в конец" % ", ".join(map(str, was)))
                break
        if created_status == "unreadable":
            if not UNREADABLE_BODY.fullmatch(body):
                bad("тело записи, созданной со status unreadable, — ровно строка «Содержимое не читается: <причина>»")
        elif fields.get("type") == "image" and not any(x.startswith(IMAGE_LINE) for x in body.split("\n")):
            bad("у image в теле нет строки, начинающейся с «%s»" % IMAGE_LINE)

    # ---------------------------------------------------------- настройки, правила

    def check_config(self, text):
        rel = common.CONFIG
        try:
            values = common.load_flat(rel)
        except Fail as e:
            if e.code != 1:
                raise
            self.add(rel, "config", e.message)
            return
        try:
            defaults = common.defaults()
        except Fail as e:
            self.add(rel, "config", "engine/config.default.yaml не читается: %s" % e.message)
            return
        for err in common.check_config(values, defaults):
            self.add(rel, "config", err)
        if set(values) == set(defaults):
            try:
                expected = common.render_config(values)
            except Fail:
                expected = None
            if expected is not None and expected != text:
                got, want = text.split("\n"), expected.split("\n")
                n = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
                self.add(rel, "config", "строка %d не как в engine/config.default.yaml (отличаться может только значение): "
                                        "«%s»" % (n + 1, short(got[n] if n < len(got) else "")))
        self.check_agent_models(values)

    def check_agent_models(self, values):
        rel = common.CONFIG
        keys = {k[len("agents."):]: v for k, v in values.items() if k.startswith("agents.")}
        for name, value in sorted(keys.items()):
            agent = "%s/seb-%s.md" % (common.AGENTS_DIR, name)
            try:
                with open(self.abs(agent), "rb") as f:
                    data = f.read()
            except OSError:
                self.add(rel, "config", "agents.%s: нет файла агента %s" % (name, agent))
                continue
            model = common.agent_model(data)
            if model is None:
                self.add(rel, "config", "agents.%s: в шапке %s не ровно одна строка model:" % (name, agent))
            elif value in common.AGENT_MODELS and model != value:
                self.add(rel, "config", "agents.%s: «%s», а в строке model: %s — «%s»" % (name, value, agent, model))
        for f in sorted(self.engine):
            m = AGENT_FILE.fullmatch(f)
            if m and m.group("name") not in keys:
                self.add(rel, "config", "файл агента %s есть, а ключа agents.%s нет" % (f, m.group("name")))

    def check_rules_length(self, rel, text):
        total = len(text.splitlines())
        claude = self.abs("CLAUDE.md")
        if os.path.isfile(claude):
            with open(claude, encoding="utf-8", errors="replace") as f:
                total += len(f.read().splitlines())
        if total > 200:
            self.add(rel, "rules-length", "CLAUDE.md и rules.local.md вместе %d строк, можно не больше 200" % total)

    # ---------------------------------------------------------- запуск

    def ensure_repo(self):
        cp = self.git("rev-parse", "--show-toplevel", check=False)
        top = cp.stdout.strip()
        if cp.returncode != 0 or not top or os.path.realpath(top) != self.root:
            raise Fail(3, "корень базы не корень репозитория git: %s" % self.root)

    def resolve_args(self, args):
        """Пути вызова → [путь от корня]; удалённые (есть в HEAD, нет на диске) пропускаются."""
        out = []
        for a in args:
            full = os.path.realpath(a if os.path.isabs(a) else os.path.join(self.root, a))
            if full == self.root or not full.startswith(self.root + os.sep):
                raise Fail(2, "путь вне базы: %s" % a)
            rel = os.path.relpath(full, self.root).replace(os.sep, "/")
            if os.path.isdir(full):
                raise Fail(2, "не файл, а папка: %s" % a)
            if not os.path.lexists(full):
                if self.git("cat-file", "-e", "HEAD:%s" % rel, check=False).returncode == 0:
                    continue  # удалён delete.py или remind.py — пропуск
                raise Fail(2, "нет такого пути ни на диске, ни в HEAD: %s" % a)
            if rel not in out:
                out.append(rel)
        return out

    def run(self, args):
        self.ensure_repo()
        targets = self.resolve_args(args) if args else None
        if not os.path.exists(self.abs(common.ENGINE_LOCK)):
            self.add(common.ENGINE_LOCK, "root", "база не настроена: нет engine.lock")
            return 0
        self.check_engine(report=targets is None)
        if targets is None:
            files = self.list_files()
            if self.engine_ok:
                self.check_root_layout(files)
            checked = [f for f in files if f not in self.engine and (f.startswith("data/") or f in ROOT_OWN)]
            for rel in files:
                if rel not in self.engine:
                    self.check_file(rel)
            if not os.path.exists(self.abs(common.CONFIG)):
                self.add(common.CONFIG, "config", "нет config.yaml: база не настроена (config.py init)")
            self.check_index_present()
        else:
            checked = [t for t in targets if t not in self.engine]
            for rel in checked:
                self.check_file(rel)
        self.flush_records()
        return len(checked)


def main():
    argv = sys.argv[1:]
    json_mode = "--json" in argv
    args = [a for a in argv if a != "--json"]
    for a in args:
        if a.startswith("-"):
            raise Fail(2, "неизвестный параметр: %s; вызов: lint.py [--json] [пути…]" % a)
    lint = Lint()
    checked = lint.run(args)
    violations = sorted(lint.violations, key=lambda v: v["path"])
    unchecked = sorted(lint.unchecked, key=lambda v: v["path"])
    if json_mode:
        print(json.dumps({"checked": checked, "violations": violations, "unchecked": unchecked}, ensure_ascii=False))
    else:
        for v in violations:
            print("%s — %s — %s" % (v["path"], v["rule"], v["message"]))
        for v in unchecked:
            print("%s — %s — не проверено: %s" % (v["path"], v["rule"], v["message"]))
        total = "проверено файлов: %d, нарушений: %d" % (checked, len(violations))
        if unchecked:
            total += ", не проверено: %d" % len(unchecked)
        print(total)
    return 1 if violations else 0


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
