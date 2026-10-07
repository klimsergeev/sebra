"""Общее для скриптов движка: корень базы, YAML, настройки, модели агентов, время, git, журнал, разбор Markdown.

Скрипты запускаются как `python3 engine/scripts/<имя>.py` из корня базы; корень — папка над `engine/`.
"""

import datetime as _dt
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
# PyYAML лежит в движке: в облаке его нет, а установка в сессии её не переживает.
sys.path.insert(0, os.path.join(SCRIPTS_DIR, "_vendor"))
import yaml  # noqa: E402

# engine/scripts/ → корень базы
ROOT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))

ENGINE_DIR = "engine"
DATA_DIR = "data"
CONFIG = "config.yaml"
CONFIG_DEFAULT = "engine/config.default.yaml"
ENGINE_LOCK = "engine.lock"
RULES = "rules.local.md"
AGENTS_DIR = ".claude/agents"

RECORDS = "data/records"
WIKI = "data/wiki"
TASKS = "data/tasks"
REMINDERS = "data/reminders"
REPORTS_DAILY = "data/reports/daily"
REPORTS_WEEKLY = "data/reports/weekly"
LOG = "data/log"
INDEX = "data/index.md"
DATA_DIRS = (RECORDS, "data/wiki/topics", "data/wiki/people", "data/wiki/projects", "data/wiki/sources", TASKS,
             REMINDERS, REPORTS_DAILY, REPORTS_WEEKLY, LOG)

WHO = r"seb-[a-z0-9]+(?:-[a-z0-9]+)*|gardener|summary|digester|setup|engine"
WHO_RE = re.compile(r"^(?:%s)$" % WHO)
# Строка журнала — выражение раздела «Форматы», «Журнал»; группы с именами нужны collect.py.
LOG_RE = re.compile(
    r"^- (?P<time>(?:[01]\d|2[0-3]):[0-5]\d) · (?P<who>%s)"
    r" · (?:поправка · (?P<text>[^·\n]+)|(?P<action>[^·\n]+?) · (?P<path>[^\s·]+)(?: · (?P<note>[^·\n]+))?)$" % WHO
)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
AGENT_MODELS = ("opus", "sonnet", "haiku", "fable")
SLUG = r"[a-z0-9]+(?:-[a-z0-9]+){0,5}"


class Fail(Exception):
    """Отказ с кодом выхода: 1 — правила, 2 — вызов, 3 — окружение."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def run_main(main, json_mode=False):
    """Запускает main(); Fail превращает в текст (или {"error"} при --json) и код выхода."""
    try:
        code = main()
    except Fail as e:
        if json_mode:
            print(json.dumps({"error": e.message}, ensure_ascii=False))
        else:
            print(e.message, file=sys.stderr)
        sys.exit(e.code)
    sys.exit(code or 0)


def path(*parts):
    return os.path.join(ROOT, *parts)


def read_text(rel):
    with open(path(rel), encoding="utf-8") as f:
        return f.read()


def file_mode(target):
    """Права для записи файла: как у существующего, иначе обычные (0o666 без umask), а не 0o600 от mkstemp."""
    try:
        return os.stat(target).st_mode & 0o7777
    except FileNotFoundError:
        mask = os.umask(0)
        os.umask(mask)
        return 0o666 & ~mask


def write_atomic(rel, text):
    """Запись через временный файл в той же папке и os.replace; права файла сохраняются."""
    target = path(rel)
    d = os.path.dirname(target)
    os.makedirs(d, exist_ok=True)
    mode = file_mode(target)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".sebra")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_bytes_atomic(target, data, mode=None):
    """Байты через временный файл и os.replace; mode — права файла (иначе как у временного)."""
    d = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".sebra")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def has_json_flag(argv):
    return "--json" in argv


def rel_path(p):
    """Путь от корня базы (абсолютный или относительный к корню) → нормализованный относительный; вне базы — None."""
    full = os.path.normpath(p if os.path.isabs(p) else os.path.join(ROOT, p))
    root = os.path.normpath(ROOT)
    if full == root or not full.startswith(root + os.sep):
        return None
    return os.path.relpath(full, root).replace(os.sep, "/")


# ---------------------------------------------------------------- YAML


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader, который отказывает на повторном ключе: safe_load молча берёт последнее значение."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError:
                continue
            if key in seen:
                raise yaml.constructor.ConstructorError(None, None, "повтор ключа «%s»" % key, key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml(text):
    """YAML загрузчиком с отказом на повторном ключе."""
    return yaml.load(text, Loader=UniqueKeyLoader)


def yaml_error(e):
    return str(e).replace("\n", " ")


def split_front_matter(text):
    """(шапка или None, тело). Шапка — между первой строкой `---` и следующей строкой `---`."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def front_matter(text):
    """Шапка как словарь; ValueError, если её нет или она не разбирается."""
    head, _ = split_front_matter(text)
    if head is None:
        raise ValueError("нет шапки")
    try:
        data = load_yaml(head)
    except yaml.YAMLError as e:
        raise ValueError("шапка не разбирается: %s" % yaml_error(e)) from None
    if not isinstance(data, dict):
        raise ValueError("шапка не словарь")
    return data


# ---------------------------------------------------------------- настройки


def _flatten(data, source):
    if not isinstance(data, dict):
        raise Fail(1, "%s: ожидаются разделы и ключи" % source)
    flat = {}
    for section, keys in data.items():
        if not isinstance(keys, dict):
            raise Fail(1, "%s: раздел %s — не набор ключей" % (source, section))
        for key, value in keys.items():
            flat["%s.%s" % (section, key)] = value
    return flat


def load_flat(rel):
    """Файл настроек как {"раздел.ключ": значение}; Fail(1), если не читается."""
    try:
        text = read_text(rel)
    except FileNotFoundError:
        raise Fail(1, "нет файла %s" % rel) from None
    try:
        data = load_yaml(text)
    except yaml.YAMLError as e:
        raise Fail(1, "%s не разбирается: %s" % (rel, yaml_error(e))) from None
    return _flatten(data, rel)


def defaults():
    return load_flat(CONFIG_DEFAULT)


def config():
    """Значения из config.yaml; если его нет — из engine/config.default.yaml. Ключи, которых нет, — из умолчаний."""
    values = dict(defaults())
    if os.path.exists(path(CONFIG)):
        values.update(load_flat(CONFIG))
    return values


# Строки шаблона engine/config.default.yaml: раздел и ключ со значением и комментарием.
_SECTION_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(#.*)?$")
_KEY_RE = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):(\s*)(.*)$")


def _split_value(rest):
    """Значение и хвост (пробелы и комментарий) строки ключа."""
    if rest.startswith('"'):
        _, end = json.JSONDecoder().raw_decode(rest)
        return rest[:end], rest[end:]
    m = re.match(r"([^\s#]*)(.*)$", rest)
    return m.group(1), m.group(2)


def template(rel=CONFIG_DEFAULT):
    """Строки шаблона и места значений: [(номер строки, "раздел.ключ", перед значением, после значения)]."""
    lines = read_text(rel).split("\n")
    slots = []
    section = None
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1)
            continue
        m = _KEY_RE.match(line)
        if m and section:
            try:
                _, tail = _split_value(m.group(4))
            except ValueError:
                raise Fail(1, "%s: строка %d не по формату" % (rel, i + 1)) from None
            before = m.group(1) + m.group(2) + ":" + m.group(3)
            slots.append((i, "%s.%s" % (section, m.group(2)), before, tail))
            continue
        raise Fail(1, "%s: строка %d не по формату" % (rel, i + 1))
    return lines, slots


def format_value(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def render_config(values, rel=CONFIG_DEFAULT):
    """Текст config.yaml: строки шаблона, в строке ключа заменено только значение."""
    lines, slots = template(rel)
    out = list(lines)
    for i, key, before, tail in slots:
        out[i] = before + format_value(values[key]) + tail
    return "\n".join(out)


def tz_database_missing():
    import zoneinfo

    try:
        return not zoneinfo.available_timezones()
    except Exception:
        return True


TIME_KEYS = ("gardener.time", "summary.time")

# Булево в настройках: файл пишется yes/no; командная строка принимает и true/false — в файл всё равно ляжет yes/no.
BOOL_WORDS = {"yes": True, "no": False, "true": True, "false": False}


def check_value(key, value, default):
    """Проверка одного значения по таблице раздела «Форматы», «Настройки»; текст ошибки или None."""
    want = type(default)
    if type(value) is not want:
        names = {str: "строка", int: "целое число", bool: "yes или no"}
        return "%s: нужно %s" % (key, names.get(want, want.__name__))
    if key == "owner.timezone":
        import zoneinfo

        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            if tz_database_missing():
                raise Fail(3, "нет базы часовых поясов") from None
            return "%s: нет часового пояса «%s»; нужно имя вида Europe/Moscow" % (key, value)
    elif key in TIME_KEYS:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            return "%s: нужно время ЧЧ:ММ от 00:00 до 23:59" % key
    elif key == "weekly.day":
        if value not in WEEKDAYS:
            return "%s: нужно одно из: %s" % (key, ", ".join(WEEKDAYS))
    elif key.startswith("tasks.") or key.startswith("big_docs."):
        if not 1 <= value <= 100000:
            return "%s: нужно целое от 1 до 100000" % key
    elif key == "language.main":
        if value != "ru":
            return "%s: пока допустимо только ru" % key
    elif key.startswith("agents."):
        if value not in AGENT_MODELS:
            return "%s: нужно одно из: %s" % (key, ", ".join(AGENT_MODELS))
    elif key == "git.author_email":
        if value and "@" not in value:
            return "%s: почта должна содержать @" % key
    return None


def check_config(values, defaults_):
    """Проверка всего набора: ключи, типы, значения, пара автора. Список ошибок."""
    errors = []
    missing = [k for k in defaults_ if k not in values]
    extra = [k for k in values if k not in defaults_]
    if missing:
        errors.append("нет ключей: %s" % ", ".join(missing))
    if extra:
        errors.append("лишние ключи: %s" % ", ".join(extra))
    for key, default in defaults_.items():
        if key in values:
            err = check_value(key, values[key], default)
            if err:
                errors.append(err)
    name, email = values.get("git.author_name"), values.get("git.author_email")
    if isinstance(name, str) and isinstance(email, str) and bool(name) != bool(email):
        errors.append("git.author_name и git.author_email: оба пустые или оба заполнены")
    return errors


def parse_cli_value(key, raw, default):
    """Значение из командной строки по типу умолчания; (значение, ошибка)."""
    if isinstance(default, bool):
        word = raw.strip().lower()
        if word not in BOOL_WORDS:
            return None, "%s: нужно yes или no" % key
        value = BOOL_WORDS[word]
    elif isinstance(default, int):
        if not re.fullmatch(r"[0-9]+", raw):
            return None, "%s: нужно целое число" % key
        value = int(raw)
    else:
        value = raw
        if key in TIME_KEYS:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
            if m:
                value = "%02d:%s" % (int(m.group(1)), m.group(2))
        elif key.startswith("agents."):
            value = raw.strip().lower()
    return value, check_value(key, value, default)


# ---------------------------------------------------------------- модели агентов

_MODEL_LINE = re.compile(rb"^(model:[ \t]*)(.*?)([ \t]*\r?)$")


def _model_lines(data):
    """Номера строк `model:` в шапке файла агента (между первой и второй строкой `---`)."""
    lines = data.split(b"\n")
    if not lines or lines[0].rstrip(b"\r") != b"---":
        return lines, []
    found = []
    for i in range(1, len(lines)):
        if lines[i].rstrip(b"\r") == b"---":
            return lines, found
        if _MODEL_LINE.match(lines[i]):
            found.append(i)
    return lines, []  # шапка не закрыта — строк model: в ней нет


def agent_model(data):
    """Значение строки `model:` шапки (bytes файла) или None, если строк не ровно одна."""
    lines, found = _model_lines(data)
    if len(found) != 1:
        return None
    return _MODEL_LINE.match(lines[found[0]]).group(2).decode("utf-8", errors="replace")


def plan_agent_models(values):
    """Проверка файлов агентов и новые байты для тех, где модель расходится с values.

    Возвращает [(путь от корня, новые байты, права файла)]; Fail(1), если хоть один файл не годится:
    файла нет, в шапке не ровно одна строка model:, файл .claude/agents/seb-*.md без ключа agents.<имя>.
    """
    keys = {k[len("agents."):]: v for k, v in values.items() if k.startswith("agents.")}
    errors, plan = [], []
    folder = path(AGENTS_DIR)
    present = []
    if os.path.isdir(folder):
        present = sorted(f[len("seb-"):-len(".md")] for f in os.listdir(folder)
                         if f.startswith("seb-") and f.endswith(".md"))
    for name in present:
        if name not in keys:
            errors.append("%s/seb-%s.md: нет ключа agents.%s в настройках" % (AGENTS_DIR, name, name))
    for name, value in sorted(keys.items()):
        rel = "%s/seb-%s.md" % (AGENTS_DIR, name)
        full = path(rel)
        try:
            with open(full, "rb") as f:
                data = f.read()
            mode = os.stat(full).st_mode & 0o7777
        except FileNotFoundError:
            errors.append("%s: нет файла агента для ключа agents.%s" % (rel, name))
            continue
        lines, found = _model_lines(data)
        if len(found) != 1:
            errors.append("%s: в шапке строк model: %d, нужна ровно одна" % (rel, len(found)))
            continue
        m = _MODEL_LINE.match(lines[found[0]])
        if m.group(2).decode("utf-8", errors="replace") == value:
            continue
        lines[found[0]] = m.group(1) + value.encode("utf-8") + m.group(3)
        plan.append((rel, b"\n".join(lines), mode))
    if errors:
        raise Fail(1, "\n".join(errors))
    return plan


def write_agent_models(plan):
    """Запись по плану plan_agent_models: только строка model:, права файла те же, атомарно."""
    for rel, data, mode in plan:
        write_bytes_atomic(path(rel), data, mode)
    return [rel for rel, _, _ in plan]


def apply_agent_models(values):
    """Строка model: в шапке .claude/agents/seb-<имя>.md = agents.<имя>; список изменённых файлов."""
    return write_agent_models(plan_agent_models(values))


# ---------------------------------------------------------------- время


_TZ = []


def timezone(name=None):
    """Пояс пользователя из настроек; найденный запоминается на время запуска."""
    import zoneinfo

    if name is None and _TZ:
        return _TZ[0]
    remember = name is None
    if name is None:
        name = config().get("owner.timezone", "UTC")
    try:
        tz = zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        if tz_database_missing():
            raise Fail(3, "нет базы часовых поясов") from None
        raise Fail(1, "owner.timezone: нет часового пояса «%s»" % name) from None
    if remember:
        _TZ[:] = [tz]
    return tz


def use_timezone(tz):
    """Задать пояс на время запуска: collect.py при сломанном config.yaml берёт пояс по умолчанию."""
    _TZ[:] = [tz]


def now():
    """Текущее время пользователя (aware datetime, без секунд).

    SEBRA_NOW="ГГГГ-ММ-ДДTЧЧ:ММ" подменяет его для тестов; со смещением (…+00:00) — момент,
    который переводится в пояс пользователя.
    """
    tz = timezone()
    fake = os.environ.get("SEBRA_NOW")
    if fake:
        try:
            moment = _dt.datetime.fromisoformat(fake)
        except ValueError:
            raise Fail(2, "SEBRA_NOW: нужно ГГГГ-ММ-ДДTЧЧ:ММ") from None
        moment = moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)
    else:
        moment = _dt.datetime.now(tz)
    return moment.replace(second=0, microsecond=0)


def to_user_time(iso):
    """Дата из git (%cI) — в пояс пользователя."""
    return _dt.datetime.fromisoformat(iso).astimezone(timezone())


def fmt_dt(moment):
    return moment.strftime("%Y-%m-%dT%H:%M")


def parse_day(text):
    """Дата ГГГГ-ММ-ДД или ValueError."""
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("не дата ГГГГ-ММ-ДД: %r" % (text,))
    return _dt.datetime.strptime(text, "%Y-%m-%d").date()


# ---------------------------------------------------------------- git

# Пути — буквально, не шаблоны: иначе удалённый файл «w/[ab].md» зацепит w/a.md
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_LITERAL_PATHSPECS": "1"}
MAIN_REFSPEC = "+refs/heads/main:refs/remotes/origin/main"


def git(*args, check=True, env=None, timeout=120, input=None):
    """git в корне базы; возвращает CompletedProcess с текстом. Нет git — Fail(3)."""
    full_env = dict(os.environ)
    full_env.update(GIT_ENV)
    if env:
        full_env.update(env)
    try:
        cp = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args], cwd=ROOT, env=full_env, capture_output=True, encoding="utf-8",
            errors="replace", timeout=timeout, input=input,
        )
    except FileNotFoundError:
        raise Fail(3, "git не запускается") from None
    except subprocess.TimeoutExpired:
        raise Fail(3, "git %s: нет ответа за %d секунд" % (args[0], timeout)) from None
    if check and cp.returncode != 0:
        raise Fail(3, "git %s: %s" % (args[0], git_error(cp)))
    return cp


def git_error(cp):
    text = (cp.stderr or "").strip() or (cp.stdout or "").strip()
    return text.replace("\n", " | ")


def git_out(*args, **kw):
    return git(*args, **kw).stdout.strip()


def ensure_repo():
    cp = git("rev-parse", "--is-inside-work-tree", check=False)
    if cp.returncode != 0 or cp.stdout.strip() != "true":
        raise Fail(3, "корень базы не репозиторий git: %s" % git_error(cp))


def set_upstream_main():
    """Ветка сессии следит за origin/main: git status облака видит её вровень с main.

    Если remote.origin.fetch копии сужен до своей ветки claude/…, git отказывает («starting point 'origin/main' is
    not a branch»): тогда в remote.origin.fetch дописывается MAIN_REFSPEC. При push.default upstream или tracking голый
    git push ушёл бы в main мимо commit.py — тогда push.default ставится simple (он отказывает: имя ветки не совпадает
    с upstream). HEAD — не ветка — ничего не делает. Возвращает True, если upstream установлен.
    """
    if git("symbolic-ref", "-q", "HEAD", check=False).returncode != 0:
        return False
    if git("config", "push.default", check=False).stdout.strip() in ("upstream", "tracking"):
        git("config", "push.default", "simple", check=False)
    if git("branch", "--set-upstream-to=origin/main", check=False).returncode == 0:
        return True
    specs = git("config", "--get-all", "remote.origin.fetch", check=False).stdout.split()
    if MAIN_REFSPEC not in specs:
        git("config", "--add", "remote.origin.fetch", MAIN_REFSPEC, check=False)
    return git("branch", "--set-upstream-to=origin/main", check=False).returncode == 0


def sync_main(timeout=15):
    """Догнать origin/main, если копия чиста и отстаёт; вернуть, на сколько коммитов копия позади main."""
    # до fetch: и без сети стоп-хук облака не должен сравнивать ветку с локальной ссылкой origin/claude/…
    forget_session_ref()
    git("fetch", "origin", MAIN_REFSPEC, timeout=timeout)
    behind_ancestor = git("merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode == 0
    dirty = git_out("status", "--porcelain", "--untracked-files=no")
    if behind_ancestor and not dirty:
        git("merge", "--ff-only", "origin/main")
    # как в commit.py: git status облака видит ветку сессии вровень с main
    set_upstream_main()
    return int(git_out("rev-list", "--count", "HEAD..origin/main"))


def forget_session_ref():
    """Стоп-хук облака сравнивает ветку сессии с origin/<ветка>, а без неё — с origin/HEAD.

    Облако при старте заводит локальную ссылку refs/remotes/origin/claude/…, а commit.py отправляет в main: ссылка
    остаётся позади, и хук просит отправить ветку. Функция удаляет эту ссылку (только локальную: на GitHub ничего не
    меняется; текущая ветка — не main) и ставит origin/HEAD на origin/main, если origin/main есть. Ошибки git не
    поднимает.
    """
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    if branch and branch != "main":
        ref = "refs/remotes/origin/%s" % branch
        if git("show-ref", "--verify", "-q", ref, check=False).returncode == 0:
            git("update-ref", "-d", ref, check=False)
    if git("show-ref", "--verify", "-q", "refs/remotes/origin/main", check=False).returncode == 0:
        if git("symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).stdout.strip() != "refs/remotes/origin/main":
            git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", check=False)


def ensure_history():
    """Неглубокая копия — догрузить историю. Возвращает "полная" или "догружена"."""
    if git_out("rev-parse", "--is-shallow-repository") != "true":
        return "полная"
    git("fetch", "--unshallow", "origin", timeout=300)
    return "догружена"


# ---------------------------------------------------------------- движок


def engine_lock():
    """engine.lock как словарь или None, если файла нет; Fail(1), если не читается."""
    try:
        text = read_text(ENGINE_LOCK)
    except FileNotFoundError:
        return None
    try:
        data = load_yaml(text)
    except yaml.YAMLError as e:
        raise Fail(1, "engine.lock не разбирается: %s" % yaml_error(e)) from None
    if not isinstance(data, dict):
        raise Fail(1, "engine.lock не разбирается: не словарь")
    return data


def engine_files():
    lock = engine_lock()
    if lock is None:
        return set()
    files = lock.get("files")
    if not isinstance(files, list):
        raise Fail(1, "engine.lock: поле files — не список")
    return set(str(f) for f in files)


def version_tuple(v):
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(v))
    if not m:
        raise Fail(2, "версия не вида X.Y.Z: %s" % v)
    return tuple(int(x) for x in m.groups())


# ---------------------------------------------------------------- журнал


def check_who(who):
    if not who or not WHO_RE.match(who):
        raise Fail(2, "--who: нужно имя seb-…, gardener, summary, digester, setup или engine, а не «%s»" % who)


def clean_field(text):
    """Перевод строки — пробел, разделитель « · » — запятая: поле не ломает строку журнала."""
    text = re.sub(r"\r\n|\r|\n", " ", text or "")
    text = text.replace(" · ", ", ").replace("·", ",")
    return re.sub(r"[ \t]+", " ", text).strip()


def journal_add(who, action=None, target=None, note=None, correction=False):
    """Дописывает строку в data/log/<дата>.md под flock; возвращает (файл, строка)."""
    check_who(who)
    moment = now()
    if correction:
        if action or target:
            raise Fail(2, "у поправки нет --action и --target")
        text = clean_field(note)
        if not text:
            raise Fail(2, "у поправки нужен --note с текстом")
        line = "- %s · %s · поправка · %s" % (moment.strftime("%H:%M"), who, text)
    else:
        act = clean_field(action)
        if not act:
            raise Fail(2, "нужен --action")
        if not target or re.search(r"[\s·]", target):
            raise Fail(2, "--target: путь от корня базы без пробелов")
        line = "- %s · %s · %s · %s" % (moment.strftime("%H:%M"), who, act, target)
        extra = clean_field(note)
        if extra:
            line += " · " + extra
    if not LOG_RE.match(line):
        raise Fail(2, "строка журнала не по формату: %s" % line)
    rel = "%s/%s.md" % (LOG, moment.strftime("%Y-%m-%d"))
    os.makedirs(path(LOG), exist_ok=True)
    with open(path(rel), "a+b") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0, os.SEEK_END)
            data = b""
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    # иначе после слияния merge=union две строки склеятся
                    data += b"\n"
            data += (line + "\n").encode("utf-8")
            f.seek(0, os.SEEK_END)
            f.write(data)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return rel, line


# ---------------------------------------------------------------- разбор Markdown и ссылок
# Одна реализация для lint.py, delete.py, collect.py.

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+).+?\1")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
LINK_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:")
REMINDER_RE = re.compile(r"^- (?P<text>\S.*?) — \[запись\]\(\.\./records/(?P<rec>[^()\s<>/]+\.md)\)$")
FACT_LINE = re.compile(r"^- \*\*(\d{4}-\d{2}-\d{2})\*\* — ")
PATH_FIELDS = ("record", "project")
_HEADER_PATH = re.compile(r'^(record|project):[ \t]*"([^"]*)"[ \t]*$')


def lines_outside_code(text):
    """Тройки (номер строки с 1, строка, внутри блока кода?) — ограждения ``` и ~~~."""
    fence = None
    for n, line in enumerate(text.split("\n"), 1):
        m = FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            yield n, line, True
            continue
        if fence is not None:
            s = line.strip()
            if s and set(s) == {fence[0]} and len(s) >= len(fence):
                fence = None
            yield n, line, True
            continue
        yield n, line, False


def headings(text):
    return [(n, line[3:].strip()) for n, line, code in lines_outside_code(text) if not code and line.startswith("## ")]


def section_lines(text, title):
    """Строки раздела «## title» до следующего заголовка «## » (вне блоков кода): [(номер, строка)]."""
    out, inside = [], False
    for n, line, code in lines_outside_code(text):
        if not code and line.startswith("## "):
            inside = line[3:].strip() == title
            continue
        if inside:
            out.append((n, line))
    return out


def _header_end(lines):
    """Номер (с 0) закрывающей строки шапки или None, если шапки нет."""
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            return i
    return None


def link_targets(line, unclosed=None):
    """Цели ссылок `](…)` строки тела: [(сырая цель)] без внешних адресов и строчного кода.

    unclosed — список: если в строке ссылка без закрывающей скобки, в него дописывается её начало (для lint.py).
    """
    scan = CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
    out, pos = [], 0
    while True:
        i = scan.find("](", pos)
        if i < 0:
            return out
        start = i + 2
        if SCHEME.match(scan[start:]):
            depth, j = 1, start
            while j < len(scan) and depth:
                depth += {"(": 1, ")": -1}.get(scan[j], 0)
                j += 1
            pos = j
            continue
        end = scan.find(")", start)
        if end < 0:
            if unclosed is not None:
                unclosed.append(scan[start:])
            return out
        out.append(scan[start:end])
        pos = end + 1


def iter_links(rel, text):
    """Ссылки файла rel: (номер строки в файле, путь от корня базы, via).

    via — link (ссылка тела), record, project (поля-пути шапки). Ссылки в блоках кода, строчном коде, строках `>`
    и внешние не отдаются; якорь `#…` отрезается; пустое поле шапки не отдаётся; путь ссылки тела — относительно
    файла rel, нормализованный.
    """
    lines = text.split("\n")
    end = _header_end(lines)
    if end is not None:
        for i in range(1, end):
            m = _HEADER_PATH.match(lines[i].rstrip("\r"))
            if m and m.group(2):
                yield i + 1, m.group(2), m.group(1)
    base = os.path.dirname(rel)
    first = 0 if end is None else end + 1
    body = "\n".join(lines[first:])
    for n, line, code in lines_outside_code(body):
        if code or line.lstrip().startswith(">") or LINK_DEF.match(line):
            continue
        for target in link_targets(line):
            p = target.split("#", 1)[0]
            if not p or any(c in p for c in " \t(<>") or p.startswith("/"):
                continue
            full = os.path.normpath(os.path.join(base, p)).replace(os.sep, "/")
            yield n + first, full, "link"
