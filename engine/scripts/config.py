"""Настройки базы: init, get, set, migrate. Единственный, кто пишет config.yaml и строку model: в файлах агентов.

python3 engine/scripts/config.py init --set key=value … [--json]
python3 engine/scripts/config.py get key [--json]
python3 engine/scripts/config.py set key value [key value …] [--json]
python3 engine/scripts/config.py migrate --from X --to Y --migrations <файл> --stash <файл> [--json]
"""

import argparse
import json
import os
import re
import sys

import common
from common import Fail

import yaml


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


def show(value):
    if isinstance(value, bool):
        return common.format_value(value)
    return str(value)


def apply_pairs(values, defaults, pairs):
    """Проверяет все пары, затем меняет values. Ошибки — Fail(1) со списком."""
    errors, parsed = [], {}
    for key, raw in pairs:
        if key not in defaults:
            errors.append("%s: такого ключа нет; ключи: %s" % (key, ", ".join(defaults)))
            continue
        value, err = common.parse_cli_value(key, raw, defaults[key])
        if err:
            errors.append(err)
        else:
            parsed[key] = value
    if not errors:
        candidate = dict(values)
        candidate.update(parsed)
        errors = common.check_config(candidate, defaults)
    if errors:
        raise Fail(1, "\n".join(errors))
    values.update(parsed)
    return parsed


def cmd_init(args):
    if os.path.exists(common.path(common.CONFIG)):
        raise Fail(1, "config.yaml уже есть; менять значения — config.py set")
    defaults = common.defaults()
    pairs = []
    for item in args.set or []:
        if "=" not in item:
            raise Fail(2, "--set: нужно ключ=значение, а не «%s»" % item)
        pairs.append(tuple(item.split("=", 1)))
    values = dict(defaults)
    apply_pairs(values, defaults, pairs)
    text = common.render_config(values)
    agents = common.write_agent_models(common.plan_agent_models(values))
    common.write_atomic(common.CONFIG, text)
    if args.json:
        print(json.dumps({"created": common.CONFIG, "values": values, "agents": agents}, ensure_ascii=False))
    else:
        print("создан config.yaml")
        for key, value in values.items():
            print("%s: %s" % (key, show(value)))
        for rel in agents:
            print("модель агента: %s" % rel)
    return 0


def cmd_get(args):
    defaults = common.defaults()
    if args.key not in defaults:
        raise Fail(2, "%s: такого ключа нет; ключи: %s" % (args.key, ", ".join(defaults)))
    value, source = defaults[args.key], "default"
    if os.path.exists(common.path(common.CONFIG)):
        current = common.load_flat(common.CONFIG)
        if args.key in current:
            value, source = current[args.key], common.CONFIG
    if args.json:
        print(json.dumps({"key": args.key, "value": value, "source": source}, ensure_ascii=False))
    else:
        print(show(value))
    return 0


def cmd_set(args):
    items = args.pairs
    if not items or len(items) % 2:
        raise Fail(2, "set: нужно ключ значение [ключ значение …]")
    if not os.path.exists(common.path(common.CONFIG)):
        raise Fail(1, "нет config.yaml: база не настроена (config.py init)")
    defaults = common.defaults()
    values = common.load_flat(common.CONFIG)
    # недостающие ключи (после ручной порчи) — из умолчаний, иначе шаблон не собрать
    for key, default in defaults.items():
        values.setdefault(key, default)
    pairs = list(zip(items[0::2], items[1::2]))
    changed = apply_pairs(values, defaults, pairs)
    values = {k: values[k] for k in defaults}
    text = common.render_config(values)
    plan = common.plan_agent_models(values)       # сначала проверка всех файлов агентов
    files = common.write_agent_models(plan)       # затем файлы агентов
    old = common.read_text(common.CONFIG)
    if text != old:
        common.write_atomic(common.CONFIG, text)  # последним — config.yaml
        files.append(common.CONFIG)
    if args.json:
        print(json.dumps({"changed": changed, "files": sorted(files)}, ensure_ascii=False))
    else:
        for key, value in changed.items():
            print("%s: %s" % (key, show(value)))
    return 0


MIGRATION_KEYS = {"version", "rename", "remove"}


def load_migrations(file):
    try:
        with open(file, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise Fail(2, "нет файла %s" % file) from None
    try:
        data = common.load_yaml(text)
    except yaml.YAMLError as e:
        raise Fail(1, "%s не разбирается: %s" % (file, common.yaml_error(e))) from None
    data = data or []
    if not isinstance(data, list):
        raise Fail(1, "%s: нужен список версий" % file)
    out, seen = [], set()
    for item in data:
        if not isinstance(item, dict) or "version" not in item:
            raise Fail(1, "%s: у записи нет version" % file)
        extra = set(item) - MIGRATION_KEYS
        if extra:
            raise Fail(1, "%s: у %s лишние поля: %s" % (file, item["version"], ", ".join(sorted(map(str, extra)))))
        version = item["version"]
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise Fail(1, "%s: версия не вида \"X.Y.Z\": %s" % (file, version))
        if version in seen:
            raise Fail(1, "%s: версия %s повторяется" % (file, version))
        seen.add(version)
        rename = item.get("rename") or {}
        if not isinstance(rename, dict):
            raise Fail(1, "%s: rename у %s — не словарь" % (file, version))
        remove = item.get("remove") or []
        if not isinstance(remove, list) or not all(isinstance(k, str) for k in remove):
            raise Fail(1, "%s: remove у %s — не список ключей" % (file, version))
        out.append((common.version_tuple(version), {str(k): str(v) for k, v in rename.items()}))
    return out


def rename_keys(mapping, old, new, renamed, values):
    if old in mapping:
        value = mapping.pop(old)
        if new in mapping:
            # имя уже занято: старое значение не теряется, а уходит в отложенные
            return value
        mapping[new] = value
        if values:
            renamed[old] = new
    return None


def cmd_migrate(args):
    src, dst = common.version_tuple(args.src), common.version_tuple(args.dst)
    migrations = load_migrations(args.migrations)
    if not os.path.exists(common.path(common.CONFIG)):
        raise Fail(1, "нет config.yaml")
    values = common.load_flat(common.CONFIG)
    stash = {}
    if os.path.exists(args.stash):
        try:
            with open(args.stash, encoding="utf-8") as f:
                text = f.read().strip()
            stash = json.loads(text) if text else {}
        except (OSError, ValueError) as e:
            raise Fail(1, "%s не читается: %s" % (args.stash, e)) from None
        if not isinstance(stash, dict):
            raise Fail(1, "%s: нужен объект JSON" % args.stash)
    defaults = common.defaults()

    if src < dst:
        steps = sorted((m for m in migrations if src < m[0] <= dst), key=lambda m: m[0])
        pairs = [(old, new) for _, ren in steps for old, new in ren.items()]
    elif src > dst:
        steps = sorted((m for m in migrations if dst < m[0] <= src), key=lambda m: m[0], reverse=True)
        pairs = [(new, old) for _, ren in steps for old, new in ren.items()]
    else:
        pairs = []

    renamed, stashed, restored, added = {}, [], [], []
    for old, new in pairs:
        left = rename_keys(values, old, new, renamed, True)
        if left is not None:
            stash[old] = left
            stashed.append(old)
        left = rename_keys(stash, old, new, renamed, False)
        if left is not None:
            stash[old] = left
    for key in list(values):
        if key not in defaults:
            stash[key] = values.pop(key)
            stashed.append(key)
    for key, default in defaults.items():
        if key in values:
            continue
        if key in stash and common.check_value(key, stash[key], default) is None:
            values[key] = stash.pop(key)
            restored.append(key)
        else:
            values[key] = default
            added.append(key)
    values = {k: values[k] for k in defaults}

    text = common.render_config(values)
    plan = common.plan_agent_models(values)   # файлы агентов уже новой версии: их скопировал update_engine.py
    agents = common.write_agent_models(plan)
    common.write_atomic(common.CONFIG, text)
    tmp = args.stash + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stash, f, ensure_ascii=False)
    os.replace(tmp, args.stash)

    result = {"added": added, "renamed": renamed, "stashed": stashed, "restored": restored, "agents": agents}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("добавлены: %s" % (", ".join(added) or "нет"))
        print("переименованы: %s" % (", ".join("%s → %s" % kv for kv in renamed.items()) or "нет"))
        print("отложены: %s" % (", ".join(stashed) or "нет"))
        print("возвращены из отложенных: %s" % (", ".join(restored) or "нет"))
        print("модели агентов переписаны: %s" % (", ".join(agents) or "нет"))
    return 0


def main():
    p = Parser(prog="config.py")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("init")
    s.add_argument("--set", action="append")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("get")
    s.add_argument("key")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("pairs", nargs="*")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("migrate")
    s.add_argument("--from", dest="src", required=True)
    s.add_argument("--to", dest="dst", required=True)
    s.add_argument("--migrations", required=True)
    s.add_argument("--stash", required=True)
    s.add_argument("--json", action="store_true")
    for sp in sub.choices.values():
        sp.error = p.error
    args = p.parse_args()
    handlers = {"init": cmd_init, "get": cmd_get, "set": cmd_set, "migrate": cmd_migrate}
    if args.cmd not in handlers:
        raise Fail(2, "команда: init, get, set или migrate")
    return handlers[args.cmd](args)


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
