"""Дословный текст страницы по адресу — для тела записи (WebFetch пересказывает и для цитат не годится).

python3 engine/scripts/fetch_page.py <адрес> [--text-out <файл в /tmp/sebra/>] [--json]

Коды: 0 — текст есть; 1 — ответ не 2xx, тип не HTML, не текст и не PDF, больше 10 МБ, текста нет;
2 — адрес не http:// или https://, --text-out вне /tmp/sebra/; 3 — сеть, DNS, таймаут, нет curl.
В базу ничего не пишет.
"""

import argparse
import codecs
import html
import html.parser
import json
import os
import re
import subprocess
import sys
import tempfile

import common
from common import Fail

WORK = "/tmp/sebra"
TMP_ROOTS = sorted({WORK, os.path.realpath(WORK)})
MAX_CHARS = 200000
SKIP = {"script", "style", "noscript", "template", "svg"}
BLOCK = {"address", "article", "aside", "blockquote", "br", "dd", "details", "dialog", "div", "dl", "dt",
         "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
         "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead",
         "tr", "ul", "title", "caption", "option", "body", "html", "head"}
VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


class TextParser(html.parser.HTMLParser):
    """Текст страницы: без script, style, noscript, template, svg; блочные элементы — перевод строки."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self.in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag in BLOCK:
            self.parts.append("\n")
        if tag == "title":
            self.in_title = True

    def handle_startendtag(self, tag, attrs):
        if tag in BLOCK and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK:
            self.parts.append("\n")
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if not self.skip:
            self.parts.append(data)

    def text(self):
        lines = []
        for line in "".join(self.parts).split("\n"):
            line = re.sub(r"[ \t\r\f\v ]+", " ", line).strip()
            if line or (lines and lines[-1]):
                lines.append(line)
        return "\n".join(lines).strip()


def html_text(data, charset):
    text = data.decode(charset, errors="replace")
    p = TextParser()
    p.feed(text)
    p.close()
    return p.text(), re.sub(r"\s+", " ", html.unescape(p.title)).strip()


def charset_of(content_type, data=None):
    m = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._:-]+)", content_type or "", re.I)
    name = m.group(1) if m else None
    if name is None and data is not None:
        mm = META_CHARSET.search(data[:4096])
        name = mm.group(1).decode("ascii", errors="replace") if mm else None
    try:
        return codecs.lookup(name).name if name else "utf-8"
    except LookupError:
        return "utf-8"


def version():
    try:
        return common.read_text(common.ENGINE_DIR + "/VERSION").strip()
    except OSError:
        return "0"


def fetch(url, tmp):
    cmd = ["curl", "-sSL", "--compressed", "--max-time", "30", "--max-redirs", "10", "--max-filesize", "10000000",
           "-A", "Mozilla/5.0 (X11; Linux x86_64) Sebra/%s" % version(), "-H", "Accept-Language: ru,en;q=0.8",
           "-o", tmp, "-w", "%{http_code}\n%{content_type}\n%{url_effective}", url]
    try:
        cp = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    except FileNotFoundError:
        raise Fail(3, "curl не запускается") from None
    except subprocess.TimeoutExpired:
        raise Fail(3, "curl: нет ответа за 60 секунд") from None
    if cp.returncode == 63:
        raise Fail(1, "страница больше 10 МБ")
    if cp.returncode != 0:
        raise Fail(3, "страница не загрузилась (curl %d): %s" % (cp.returncode, (cp.stderr or "").strip()))
    parts = (cp.stdout or "").split("\n")
    status = int(parts[0]) if parts and parts[0].isdigit() else 0
    return status, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else url


def pdf_text(tmp):
    try:
        cp = subprocess.run(["pdftotext", "-layout", tmp, "-"], capture_output=True, timeout=120)
    except FileNotFoundError:
        raise Fail(3, "нет pdftotext") from None
    except subprocess.TimeoutExpired:
        raise Fail(3, "pdftotext: нет ответа за 120 секунд") from None
    if cp.returncode != 0:
        raise Fail(1, "PDF не читается: %s" % cp.stderr.decode("utf-8", errors="replace").strip())
    return cp.stdout.decode("utf-8", errors="replace")


def run(args):
    if not re.match(r"^https?://", args.url, re.I):
        raise Fail(2, "адрес — только http:// или https://, а не «%s»" % args.url)
    if args.text_out:
        real = os.path.realpath(args.text_out)
        if not any(real.startswith(d + "/") for d in TMP_ROOTS):
            raise Fail(2, "--text-out: только файл в /tmp/sebra/")
    os.makedirs(WORK, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=WORK, prefix="fetch-", suffix=".bin")
    os.close(fd)
    try:
        status, content_type, final_url = fetch(args.url, tmp)
        if not 200 <= status < 300:
            raise Fail(1, "сайт ответил %d" % status)
        with open(tmp, "rb") as f:
            data = f.read()
        kind = (content_type or "").split(";", 1)[0].strip().lower()
        title = ""
        if kind in ("text/html", "application/xhtml+xml") or (not kind and data.lstrip()[:1] == b"<"):
            text, title = html_text(data, charset_of(content_type, data))
        elif kind == "text/plain":
            text = data.decode(charset_of(content_type), errors="replace")
        elif kind == "application/pdf":
            text = pdf_text(tmp)
        else:
            raise Fail(1, "тип страницы «%s» не читается: нужен HTML, текст или PDF" % (kind or "неизвестен"))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if not text.strip():
        raise Fail(1, "текста нет: страница, видимо, собирается браузером")
    words = len(text.split())
    truncated = False
    result = {"url": args.url, "final_url": final_url, "status": status, "content_type": content_type,
              "title": title}
    if args.text_out:
        with open(args.text_out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    else:
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n[… обрезано: показаны первые %d знаков; полный текст — с --text-out]" \
                % MAX_CHARS
            truncated = True
        result["text"] = text
    result["words"] = words
    result["truncated"] = truncated
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.text_out:
        print("%s: %d слов, %s" % (args.text_out, words, title or final_url))
    else:
        print(text)
    return 0


def main():
    p = Parser(prog="fetch_page.py")
    p.add_argument("url")
    p.add_argument("--text-out")
    p.add_argument("--json", action="store_true")
    return run(p.parse_args())


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
