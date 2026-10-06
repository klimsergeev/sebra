"""Строка в журнал дня data/log/<ГГГГ-ММ-ДД>.md. Коммит не делает: журнал уносит commit.py.

python3 engine/scripts/journal.py add --who <исполнитель> --action "<что сделал>" --target <путь> [--note "<пояснение>"] [--json]
python3 engine/scripts/journal.py add --who <исполнитель> --correction --note "<текст поправки>" [--json]

--who — имя агента или навыка seb-…, либо gardener, summary, digester (рутины), setup, engine.
"""

import argparse
import json
import sys

import common
from common import Fail


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


def main():
    p = Parser(prog="journal.py")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.error = p.error
    a.add_argument("--who", required=True)
    a.add_argument("--action")
    a.add_argument("--target")
    a.add_argument("--note")
    a.add_argument("--correction", action="store_true")
    a.add_argument("--json", action="store_true")
    args = p.parse_args()
    if args.cmd != "add":
        raise Fail(2, "команда: add")
    rel, line = common.journal_add(args.who, args.action, args.target, args.note, args.correction)
    if args.json:
        print(json.dumps({"file": rel, "line": line}, ensure_ascii=False))
    else:
        print(line)
    return 0


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
