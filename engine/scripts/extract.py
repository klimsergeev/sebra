"""Текст и размер присланного файла по таблице «Типы файлов». В базу ничего не пишет; рабочие файлы — в /tmp/sebra/.

python3 engine/scripts/extract.py <файл> [--text-out <файл в /tmp/sebra/>] [--json]

JSON: {"type", "text", "units", "size", "sheets", "scan", "converted", "attachments", "big", "unreadable", "error"}.
type — значение поля type записи (text, message, image, document, file, audio, video) или archive у архива: архив
записью не становится, запись — на каждый файл из attachments. size — целое в единицах units (0, если units "").
Коды: 0 — и при unreadable: true; 2 — файла нет, --text-out вне /tmp/sebra/.
"""

import argparse
import csv
import email
import email.policy
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile

import common
from common import Fail

WORK = "/tmp/sebra"
TMP_ROOTS = sorted({WORK, os.path.realpath(WORK)})
INSTALL_TIMEOUT = 180
MAX_FILES = 1000
MAX_BYTES = 500 * 1024 * 1024
WORDS_PER_PAGE = 400
SCAN_CHARS = 20

IMAGES = ("jpg", "jpeg", "png", "gif", "webp")
HEIC = ("heic", "heif")
AUDIO = ("mp3", "m4a", "wav", "ogg", "oga", "opus", "flac", "aac", "amr", "wma", "aiff", "aif")
VIDEO = ("mp4", "mov", "avi", "mkv", "webm", "3gp", "m4v", "wmv", "mpeg", "mpg")
HTML = ("html", "htm", "xhtml")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, "ошибка вызова: %s" % message)


class Result(dict):
    def __init__(self, kind):
        super().__init__(type=kind, text="", units="", size=0, sheets=0, scan=False, converted="", attachments=[],
                         big=False, unreadable=False, error="")

    def fail(self, reason):
        self["unreadable"] = True
        self["error"] = reason
        return self


def ext_of(name):
    low = name.lower()
    for double in ("tar.gz",):
        if low.endswith("." + double):
            return double
    return low.rsplit(".", 1)[-1] if "." in os.path.basename(low) else ""


def words(text):
    return len(text.split())


# ---------------------------------------------------------------- текст и HTML


def decode_text(data):
    """UTF-8 (с меткой — UTF-8 или UTF-16 по метке); иначе ValueError."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8")


def html_to_text(markup):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fetch_page

    p = fetch_page.TextParser()
    p.feed(markup)
    p.close()
    return p.text()


def read_text_file(p, res):
    with open(p, "rb") as f:
        data = f.read()
    try:
        text = decode_text(data)
    except (UnicodeDecodeError, UnicodeError):
        return res.fail("не текст в UTF-8: файл этого формата не читается")
    if ext_of(p) in HTML:
        text = html_to_text(text)
    res.update(text=text, units="words", size=words(text))
    return res


# ---------------------------------------------------------------- письма


def read_eml(p, res):
    with open(p, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, ValueError):
            text = body.get_payload(decode=True).decode("utf-8", errors="replace")
        if body.get_content_type() == "text/html":
            text = html_to_text(text)
    head = ["От: %s" % (msg.get("From") or ""), "Дата: %s" % (msg.get("Date") or ""),
            "Тема: %s" % (msg.get("Subject") or "")]
    folder = os.path.join(WORK, "eml-%s" % os.path.basename(p))
    attachments = []
    for n, part in enumerate(msg.iter_attachments(), 1):
        name = os.path.basename(part.get_filename() or "") or "вложение-%d" % n
        if name in (".", ".."):
            name = "вложение-%d" % n
        data = part.get_payload(decode=True)
        if data is None:
            continue
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, name)
        with open(target, "wb") as f:
            f.write(data)
        attachments.append(target)
    text = "\n".join(head) + "\n\n" + text.strip() + "\n"
    res.update(text=text, units="words", size=words(text), attachments=attachments)
    return res


# ---------------------------------------------------------------- офисные форматы (zip + XML)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def xml_root(z, name):
    with z.open(name) as f:
        return ET.parse(f).getroot()


def ooxml_paragraphs(root, para="p", text="t", tab="tab", brk=("br", "cr")):
    out = []
    for el in root.iter():
        if local(el.tag) != para:
            continue
        parts = []
        for sub in el.iter():
            name = local(sub.tag)
            if name == text and sub.text:
                parts.append(sub.text)
            elif name == tab:
                parts.append("\t")
            elif name in brk:
                parts.append("\n")
        out.append("".join(parts))
    return out


def read_docx(p, res):
    with zipfile.ZipFile(p) as z:
        text = "\n".join(ooxml_paragraphs(xml_root(z, "word/document.xml")))
        pages = 0
        if "docProps/app.xml" in z.namelist():
            for el in xml_root(z, "docProps/app.xml").iter():
                if local(el.tag) == "Pages" and (el.text or "").strip().isdigit():
                    pages = int(el.text.strip())
    if not pages:
        pages = math.ceil(words(text) / WORDS_PER_PAGE)
    res.update(text=text + "\n", units="pages", size=pages)
    return res


def odf_text(el):
    """Текст элемента ODF: text:s — пробелы, text:tab — табуляция, text:line-break — перевод строки."""
    parts = [el.text or ""]
    for sub in el:
        name = local(sub.tag)
        if name == "s":
            parts.append(" " * int(sub.get("{urn:oasis:names:tc:opendocument:xmlns:text:1.0}c", "1")))
        elif name == "tab":
            parts.append("\t")
        elif name == "line-break":
            parts.append("\n")
        elif name in ("note", "annotation"):
            pass
        else:
            parts.append(odf_text(sub))
        parts.append(sub.tail or "")
    return "".join(parts)


def odf_paragraphs(root):
    return [odf_text(el) for el in root.iter() if local(el.tag) in ("p", "h")]


def read_odt(p, res):
    with zipfile.ZipFile(p) as z:
        text = "\n".join(odf_paragraphs(xml_root(z, "content.xml")))
        pages = 0
        if "meta.xml" in z.namelist():
            for el in xml_root(z, "meta.xml").iter():
                if local(el.tag) == "document-statistic":
                    for key, value in el.attrib.items():
                        if local(key) == "page-count" and value.isdigit():
                            pages = int(value)
    if not pages:
        pages = math.ceil(words(text) / WORDS_PER_PAGE)
    res.update(text=text + "\n", units="pages", size=pages)
    return res


def numbered(names, pattern):
    found = []
    for n in names:
        m = re.fullmatch(pattern, n)
        if m:
            found.append((int(m.group(1)), n))
    return [n for _, n in sorted(found)]


def read_pptx(p, res):
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        slides = numbered(names, r"ppt/slides/slide(\d+)\.xml")
        notes = {int(re.search(r"(\d+)", n).group(1)): n for n in numbered(names, r"ppt/notesSlides/notesSlide(\d+)\.xml")}
        out = []
        for i, name in enumerate(slides, 1):
            out.append("## Слайд %d" % i)
            out.extend(x for x in ooxml_paragraphs(xml_root(z, name)) if x.strip())
            num = int(re.search(r"(\d+)\.xml$", name).group(1))
            if num in notes:
                note = [x for x in ooxml_paragraphs(xml_root(z, notes[num])) if x.strip()]
                if note:
                    out.append("Заметки: " + " ".join(note))
    res.update(text="\n".join(out) + "\n", units="slides", size=len(slides))
    return res


def read_odp(p, res):
    with zipfile.ZipFile(p) as z:
        root = xml_root(z, "content.xml")
    out, count = [], 0
    for page in root.iter():
        if local(page.tag) != "page":
            continue
        count += 1
        out.append("## Слайд %d" % count)
        notes = []
        for child in page:
            target = notes if local(child.tag) == "notes" else out
            target.extend(x for x in odf_paragraphs(child) if x.strip())
        if notes:
            out.append("Заметки: " + " ".join(notes))
    res.update(text="\n".join(out) + "\n", units="slides", size=count)
    return res


def read_xlsx(p, res):
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in xml_root(z, "xl/sharedStrings.xml"):
                shared.append("".join(t.text or "" for t in si.iter() if local(t.tag) == "t"))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in xml_root(z, "xl/_rels/workbook.xml.rels"):
                target = r.get("Target", "")
                target = target.lstrip("/") if target.startswith("/") else "xl/" + target
                rels[r.get("Id")] = target
        sheets = []
        for el in xml_root(z, "xl/workbook.xml").iter():
            if local(el.tag) == "sheet":
                rid = next((v for k, v in el.attrib.items() if local(k) == "id"), None)
                sheets.append((el.get("name", ""), rels.get(rid)))
        out, cells = [], 0
        for name, target in sheets:
            out.append("## Лист: %s" % name)
            if not target or target not in names:
                continue
            for row in xml_root(z, target).iter():
                if local(row.tag) != "row":
                    continue
                values = []
                for c in row:
                    if local(c.tag) != "c":
                        continue
                    kind, value = c.get("t"), None
                    for sub in c:
                        if local(sub.tag) == "v":
                            value = sub.text
                        elif local(sub.tag) == "is":
                            value = "".join(t.text or "" for t in sub.iter() if local(t.tag) == "t")
                    if kind == "s" and value is not None and value.strip().isdigit():
                        idx = int(value)
                        value = shared[idx] if idx < len(shared) else ""
                    if value not in (None, ""):
                        cells += 1
                        values.append(value)
                if values:
                    out.append("\t".join(values))
    res.update(text="\n".join(out) + "\n", units="cells", size=cells, sheets=len(sheets))
    return res


def read_ods(p, res):
    ns = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    with zipfile.ZipFile(p) as z:
        root = xml_root(z, "content.xml")
    out, cells, count = [], 0, 0
    for table in root.iter(ns + "table"):
        count += 1
        out.append("## Лист: %s" % table.get(ns + "name", ""))
        for row in table.iter(ns + "table-row"):
            values = []
            for cell in row:
                if local(cell.tag) not in ("table-cell", "covered-table-cell"):
                    continue
                text = "\n".join(odf_paragraphs(cell)).strip()
                if not text:
                    continue  # пустые ячейки (и их повтор number-columns-repeated) не считаются
                repeat = int(cell.get(ns + "number-columns-repeated", "1") or 1)
                repeat = min(repeat, 1000)
                cells += repeat
                values.extend([text] * repeat)
            if values:
                rows = min(int(row.get(ns + "number-rows-repeated", "1") or 1), 1000)
                cells += len(values) * (rows - 1)
                out.extend(["\t".join(values)] * rows)
    res.update(text="\n".join(out) + "\n", units="cells", size=cells, sheets=count)
    return res


def read_epub(p, res):
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        opf = None
        if "META-INF/container.xml" in names:
            for el in xml_root(z, "META-INF/container.xml").iter():
                if local(el.tag) == "rootfile":
                    opf = el.get("full-path")
                    break
        chapters = []
        if opf and opf in names:
            root = xml_root(z, opf)
            base = os.path.dirname(opf)
            manifest = {el.get("id"): el.get("href") for el in root.iter() if local(el.tag) == "item"}
            for el in root.iter():
                if local(el.tag) == "itemref" and el.get("idref") in manifest:
                    href = os.path.normpath(os.path.join(base, manifest[el.get("idref")])).replace(os.sep, "/")
                    if href in names:
                        chapters.append(href)
        if not chapters:
            chapters = sorted(n for n in names if ext_of(n) in HTML)
        parts = []
        for name in chapters:
            parts.append(html_to_text(z.read(name).decode("utf-8", errors="replace")))
    text = "\n\n".join(x for x in parts if x.strip()) + "\n"
    res.update(text=text, units="words", size=words(text))
    return res


# ---------------------------------------------------------------- таблицы, RTF


def read_csv(p, res):
    with open(p, "rb") as f:
        data = f.read()
    try:
        text = decode_text(data)
    except (UnicodeDecodeError, UnicodeError):
        return res.fail("таблица не в UTF-8")
    delimiter = "\t" if ext_of(p) == "tsv" else ","
    rows = sum(1 for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in row))
    res.update(text=text, units="rows", size=rows)
    return res


RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "headerl", "headerr",
            "footerl", "footerr", "listtable", "listoverridetable", "rsidtbl", "generator", "xmlnstbl",
            "themedata", "colorschememapping", "latentstyles", "datastore", "object", "fldinst"}
RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}]+)", re.S)


def rtf_to_text(src):
    """Снятие управляющих команд RTF: группы-назначения пропускаются, \\par — перевод строки, \\'hh и \\uN — знаки."""
    out, stack, skip, uc, pending_skip = [], [], False, 1, 0
    codepage = "cp1252"
    m = re.search(r"\\ansicpg(\d+)", src)
    if m:
        codepage = "cp%s" % m.group(1)
    for word, arg, hexa, sym, brace, text in RTF_TOKEN.findall(src):
        if brace == "{":
            stack.append((skip, uc))
            continue
        if brace == "}":
            skip, uc = stack.pop() if stack else (False, 1)
            continue
        if sym:
            if sym == "*":
                skip = True
            elif not skip and sym in "\\{}":
                out.append(sym)
            elif not skip and sym == "~":
                out.append("\u00a0")
            elif not skip and sym in "\n\r":
                out.append("\n")
            continue
        if hexa:
            if pending_skip:
                pending_skip -= 1
            elif not skip:
                try:
                    out.append(bytes([int(hexa, 16)]).decode(codepage))
                except (LookupError, UnicodeDecodeError):
                    out.append(bytes([int(hexa, 16)]).decode("cp1252", errors="replace"))
            continue
        if word:
            if word in RTF_SKIP:
                skip = True
            elif word == "uc" and arg:
                uc = int(arg)
            elif word == "u" and arg and not skip:
                n = int(arg)
                out.append(chr(n + 65536 if n < 0 else n))
                pending_skip = uc
            elif not skip and word in ("par", "line", "row"):
                out.append("\n")
            elif not skip and word in ("tab", "cell"):
                out.append("\t")
            continue
        if text and not skip:
            chunk = text.replace("\r", "").replace("\n", "")
            if pending_skip:
                drop = min(pending_skip, len(chunk))
                chunk, pending_skip = chunk[drop:], pending_skip - drop
            out.append(chunk)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip() + "\n"


def read_rtf(p, res):
    with open(p, "rb") as f:
        src = f.read().decode("latin-1")
    if not src.lstrip().startswith("{\\rtf"):
        return res.fail("файл не RTF")
    text = rtf_to_text(src)
    res.update(text=text, units="words", size=words(text))
    return res


# ---------------------------------------------------------------- PDF


def read_pdf(p, res, cfg):
    try:
        cp = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True, timeout=300)
    except FileNotFoundError:
        return res.fail("нет программы pdftotext")
    except subprocess.TimeoutExpired:
        return res.fail("pdftotext: нет ответа за 300 секунд")
    if cp.returncode != 0:
        return res.fail("PDF не читается: %s" % cp.stderr.decode("utf-8", errors="replace").strip())
    text = cp.stdout.decode("utf-8", errors="replace")
    pages = 0
    try:
        info = subprocess.run(["pdfinfo", p], capture_output=True, timeout=60)
        m = re.search(rb"^Pages:\s+(\d+)", info.stdout, re.M)
        if info.returncode == 0 and m:
            pages = int(m.group(1))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    if not pages:
        # нет pdfinfo — страницы по разрывам \f, которые pdftotext ставит после каждой страницы
        pages = max(1, text.count("\f"))
    chars = len(re.sub(r"\s", "", text))
    scan = chars / pages < SCAN_CHARS
    res.update(text=text, units="pages", size=pages, scan=scan)
    res["big"] = pages > cfg["big_docs.scan_pages" if scan else "big_docs.pdf_pages"]
    return res


# ---------------------------------------------------------------- архивы


def safe_member(name):
    parts = name.replace("\\", "/").split("/")
    return name and not name.startswith("/") and ".." not in parts and not re.match(r"^[A-Za-z]:", name)


def unpack_zip(p, folder, res):
    out, total = [], 0
    with zipfile.ZipFile(p) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            is_link = (info.external_attr >> 16) & 0o170000 == 0o120000
            if is_link or not safe_member(info.filename):
                continue
            if len(out) >= MAX_FILES or total + info.file_size > MAX_BYTES:
                res["error"] = "в архиве больше %d файлов или больше 500 МБ: распакована часть" % MAX_FILES
                break
            target = os.path.join(folder, info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            total += info.file_size
            out.append(target)
    return out


def unpack_tar(p, folder, res):
    out, total = [], 0
    with tarfile.open(p) as t:
        for m in t:
            if not m.isfile() or not safe_member(m.name):
                continue  # папки, ссылки, устройства и пути с .. или / в начале — пропуск
            if len(out) >= MAX_FILES or total + m.size > MAX_BYTES:
                res["error"] = "в архиве больше %d файлов или больше 500 МБ: распакована часть" % MAX_FILES
                break
            target = os.path.join(folder, m.name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = t.extractfile(m)
            if src is None:
                continue
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            total += m.size
            out.append(target)
    return out


def read_archive(p, res):
    folder = os.path.join(WORK, "unpacked-%s" % os.path.basename(p))
    if os.path.isdir(folder):
        shutil.rmtree(folder)
    os.makedirs(folder)
    try:
        files = unpack_zip(p, folder, res) if ext_of(p) == "zip" else unpack_tar(p, folder, res)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        return res.fail("архив не распаковывается: %s" % e)
    res["attachments"] = files
    res["text"] = "\n".join(os.path.relpath(f, folder) for f in files) + ("\n" if files else "")
    return res


# ---------------------------------------------------------------- установка на время сессии


def run_quiet(cmd, env=None, timeout=INSTALL_TIMEOUT):
    full = dict(os.environ)
    full.update(env or {})
    try:
        cp = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", env=full, timeout=timeout)
    except FileNotFoundError:
        return 127, "нет программы %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "%s: нет ответа за %d секунд" % (cmd[0], timeout)
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


def apt_install(package):
    """apt-get install на время сессии: root — сам, иначе sudo -n; пустые списки пакетов — apt-get update."""
    prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo", "-n"]
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    code, out = run_quiet(prefix + ["apt-get", "install", "-y", "-q", package], env)
    if code != 0 and "Unable to locate package" in out:
        run_quiet(prefix + ["apt-get", "update", "-q"], env)
        code, out = run_quiet(prefix + ["apt-get", "install", "-y", "-q", package], env)
    return code == 0, out.strip().splitlines()[-1] if out.strip() else "код %d" % code


def read_doc(p, res):
    if shutil.which("antiword") is None:
        ok, why = apt_install("antiword")
        if not ok or shutil.which("antiword") is None:
            return res.fail("нет antiword, установка не вышла: %s" % why)
    code, out = run_quiet(["antiword", p], timeout=300)
    if code != 0:
        return res.fail("antiword не прочитал файл: %s" % out.strip()[:300])
    text = out
    res.update(text=text, units="pages", size=math.ceil(words(text) / WORDS_PER_PAGE))
    return res


HEIF_CONVERT = ("import sys\nfrom PIL import Image\nimport pillow_heif\npillow_heif.register_heif_opener()\n"
                "Image.open(sys.argv[1]).convert('RGB').save(sys.argv[2], 'JPEG')\n")


def read_heic(p, res):
    code, _ = run_quiet(["python3", "-c", "import pillow_heif"], timeout=60)
    if code != 0:
        code, out = run_quiet(["python3", "-m", "pip", "install", "--quiet", "pillow-heif"])
        if code != 0 and "externally-managed-environment" in out:
            # машина сессии одноразовая: системный Python можно дополнить
            code, out = run_quiet(["python3", "-m", "pip", "install", "--quiet", "--break-system-packages",
                                   "pillow-heif"])
        if code != 0:
            return res.fail("pillow-heif не установился: %s" % (out.strip().splitlines() or ["?"])[-1])
    os.makedirs(WORK, exist_ok=True)
    target = os.path.join(WORK, os.path.splitext(os.path.basename(p))[0] + ".jpg")
    # дочерний процесс: свежая установка видна только новому интерпретатору
    code, out = run_quiet(["python3", "-c", HEIF_CONVERT, p, target], timeout=120)
    if code != 0 or not os.path.isfile(target):
        return res.fail("картинка HEIC не переведена в JPEG: %s" % out.strip()[:300])
    res["converted"] = target
    return res


# ---------------------------------------------------------------- выбор по типу


def thresholds():
    try:
        return common.config()
    except Fail:
        return common.defaults()


def extract(p):
    ext = ext_of(p)
    cfg = thresholds()
    if ext in AUDIO or ext in VIDEO:
        res = Result("audio" if ext in AUDIO else "video")
        return res.fail("аудио и видео не читаются")
    if ext in IMAGES:
        return Result("image")
    if ext in HEIC:
        return read_heic(p, Result("image"))
    if ext in ("zip", "tar", "tar.gz", "tgz"):
        return read_archive(p, Result("archive"))
    readers = {"eml": ("message", read_eml), "docx": ("document", read_docx), "odt": ("document", read_odt),
               "doc": ("document", read_doc), "pptx": ("document", read_pptx), "odp": ("document", read_odp),
               "xlsx": ("document", read_xlsx), "ods": ("document", read_ods), "epub": ("document", read_epub),
               "csv": ("document", read_csv), "tsv": ("document", read_csv), "rtf": ("document", read_rtf)}
    if ext == "pdf":
        return read_pdf(p, Result("document"), cfg)
    kind, reader = readers.get(ext, ("file", read_text_file))
    res = Result(kind)
    try:
        reader(p, res)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, OSError, UnicodeDecodeError) as e:
        return res.fail("файл не читается: %s" % (str(e) or type(e).__name__))
    if res["unreadable"]:
        return res
    units = res["units"]
    if units == "words":
        res["big"] = res["size"] > cfg["big_docs.text_words"]
    elif units == "pages":
        res["big"] = res["size"] > cfg["big_docs.doc_pages"]
    elif units == "slides":
        res["big"] = res["size"] > cfg["big_docs.slides"]
    elif units == "rows":
        res["big"] = res["size"] > cfg["big_docs.table_rows"]
    elif units == "cells":
        res["big"] = res["size"] > cfg["big_docs.sheet_cells"] or res["sheets"] > cfg["big_docs.sheets"]
    return res


def run(args):
    if not os.path.isfile(args.file):
        raise Fail(2, "нет файла %s" % args.file)
    if args.text_out:
        real = os.path.realpath(args.text_out)
        if not any(real.startswith(d + "/") for d in TMP_ROOTS):
            raise Fail(2, "--text-out: только файл в /tmp/sebra/")
    os.makedirs(WORK, exist_ok=True)
    res = extract(args.file)
    if res["unreadable"]:
        res["big"] = False
    if args.text_out:
        with open(args.text_out, "w", encoding="utf-8", newline="\n") as f:
            f.write(res["text"])
        del res["text"]
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    elif res["unreadable"]:
        print("не читается: %s" % res["error"])
    elif args.text_out:
        print("%s: %s %s %s%s" % (args.text_out, res["type"], res["size"], res["units"],
                                  ", большой" if res["big"] else ""))
    else:
        sys.stdout.write(res["text"])
    return 0


def main():
    p = Parser(prog="extract.py")
    p.add_argument("file")
    p.add_argument("--text-out")
    p.add_argument("--json", action="store_true")
    return run(p.parse_args())


if __name__ == "__main__":
    common.run_main(main, common.has_json_flag(sys.argv))
