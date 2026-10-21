"""
Извлечение текста и структуры из файлов.

Задача модуля: файл на вход → заголовок, разделы (структура) и текст на
выход. Поддержаны форматы, которые реально встречаются в рабочем каталоге:
текст и разметка (txt, md, rst), PDF, Word, Excel, PowerPoint. Всё, что
не распознано, пробуется через pandoc (он умеет десятки форматов и почти
всегда уже установлен); если и он не справился — понятная ошибка.

Для каждого формата важна структура, а не только текст: заголовки дают
оглавление и границы чанков, а значит — осмысленные векторы и красивые
цитаты в ответах. Поэтому:

  * Markdown — заголовки ATX (# …) и setext (подчёркивание);
  * Word — стили Heading 1..9 (это и есть реальная структура документа),
    а если стилей нет — нумерованные полужирные строки («Раздел 1.»,
    «1.1. Цели и задачи»): в рабочих программах дисциплин структура
    оформлена именно так, и без неё документ превращается в один
    безголовый кусок текста;
  * PDF — закладки (outline) из самого PDF, если автор их сделал;
  * PowerPoint — заголовок слайда;
  * Excel — листы как разделы, строки таблиц через табуляцию.

Абзацы внутри раздела разделяются пустой строкой: чанкер режет текст по
пустой строке, и склейка абзацев одиночным переводом строки склеила бы
документ в один гигантский чанк, который ни эмбеддер (обрезает до
512 токенов), ни поиск по нему не увидит по-настоящему.

Название документа: берётся первый смыслительный заголовок или, если его
нет, имя файла без расширения. Ручной заголовок всегда можно передать
снаружи (--title) — модуль не принимает решений за человека.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag_text import Section, normalize

# Расширения, которые умеем разбирать сами; остальное — через pandoc.
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".org", ".log", ".csv", ".tsv"}
PANDOC_SUFFIXES = {
    ".adoc", ".org", ".tex", ".html", ".htm", ".epub", ".rtf", ".odt", ".doc"
}
# Форматы, которые разбираются надёжно и без внешних программ.
OFFICE_SUFFIXES = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | OFFICE_SUFFIXES

# Нумерованный заголовок без стиля Heading: «Раздел 1. Характеристики…»,
# «1.1. Цели и задачи…», «2.1.1 Основной курс». Именно так оформлена
# структура рабочих программ дисциплин СПбГУ.
_NUMBERED_HEADING_RE = re.compile(r"^(?:Раздел\s+\d+\.?|\d+(?:\.\d+)*\.?)\s", re.IGNORECASE)


class ParseError(RuntimeError):
    """Файл не разобрался. Сообщение должно содержать путь и причину."""


@dataclass
class ParsedDoc:
    """Результат разбора одного файла."""

    title: str
    sections: list[Section] = field(default_factory=list)
    toc: list[tuple[int, str]] = field(default_factory=list)
    text: str = ""


def _title_from_name(path: Path) -> str:
    """Название по умолчанию — имя файла без расширения, с заглавной буквы."""
    name = path.stem.replace("_", " ").replace("-", " ").strip()
    return name[:1].upper() + name[1:] if name else path.name


def _first_heading(parsed: ParsedDoc, path: Path) -> str:
    """Первый заголовок первого уровня — лучшее название документа.

    Служебные заголовки вида «Раздел 1. Характеристики учебных занятий»
    названием быть не могут: они одинаковы у всех рабочих программ
    дисциплин. Заголовки, начинающиеся с цифры («1 курс (осенний семестр)…»),
    — тоже: это пункты содержания, а не имя документа. Такие пропускаем —
    тогда сработает запасной вариант (имя файла), а для документа с
    настоящим заголовком — он и останется.
    """
    for level, heading in parsed.toc:
        text = heading.strip()
        if level == 1 and text and not text.lower().startswith("раздел") \
                and not text[0].isdigit():
            return text
    return _title_from_name(path)


def _title_from_block(lines: list[str]) -> str:
    """Название из «шапки» документа: строка после «…ДИСЦИПЛИНЫ» / «…ПРАКТИКИ».

    Шапка рабочих программ дисциплин устроена одинаково, даже когда
    стили Heading не проставлены: учреждение → «Р А Б О Ч А Я
    П Р О Г Р А М М А» → «ДИСЦИПЛИНЫ» → название дисциплины →
    английское название. Названия нет ни в имени файла (там префикс
    «РПД»), ни в первом заголовке (там «Раздел 1.» или имя университета),
    зато оно есть в самом документе — берём его оттуда.
    """
    for i, line in enumerate(lines[:-1]):
        # «П Р А К Т И К И» и «учебной дисциплины» сводятся к одному виду.
        flat = re.sub(r"\s+", "", line).lower()
        if not re.search(r"дисциплин[ыей]$|практики$", flat):
            continue
        name = ""
        for candidate in lines[i + 1 :]:
            part = re.sub(r"\s+", " ", candidate).strip()
            if not part:
                continue
            if not name:
                if 3 <= len(part) <= 120:
                    name = part
                    continue
                break  # непустая, но непохожая на название — дальше не ищем
            # Название может кончаться двоеточием, а продолжение идти
            # следующей строкой («…практика:» / «разработка веб-приложений»).
            # Склеиваем, пока строка-начало ждёт продолжения.
            if name.endswith((":", "：")) and 3 <= len(part) <= 120:
                name = f"{name} {part}"
                continue
            break
        if name:
            return name
    return ""


def _reg_number(lines: list[str], path: Path) -> str:
    """Регистрационный номер рабочей программы: из шапки, иначе из имени файла.

    В шапке он бывает выставлен криво («¿050503¿»), пустым или заглушкой
    («XXXXXX», «??????») — такое не годится. Тогда берём цифры в начале
    имени файла: они появляются там именно как номер программы. Если и их
    нет, возвращаем пустую строку — названия без номера лучше, чем
    выдуманный.
    """
    for line in lines:
        m = re.search(r"[Рр]егистрационный номер[^:：]*[:：]\s*(\S+)", line)
        if not m:
            continue
        number = re.sub(r"[^\w./-]", "", m.group(1))
        if number and number[0].isdigit() and len(number) >= 4:
            return number
        break  # номер в шапке есть, но он пустой/буквенный — не выдумываем
    m = re.match(r"(\d{4,})", path.stem)
    return m.group(1) if m else ""


def _title_from_preamble(path: Path, lines: list[str]) -> str:
    """Название из «шапки» (текст до первого заголовка), либо пустая строка.

    Пустая строка — не ошибка, а сигнал «шапка не содержала названия,
    возьми первый заголовок / имя файла»: документы разных типов устроены
    по-разному, и универсального места, где лежит название, не существует.

    Регистрационный номер ставим перед названием: он один у документа,
    помогает отличать одноимённые практики и семинары и так же записан
    в именах файлов каталога.
    """
    title = _title_from_block(lines)
    if not title:
        return ""
    title = re.sub(r"\s+", " ", title).strip().rstrip(":;,. ")
    number = _reg_number(lines, path)
    if number and not title.startswith(number):
        return f"{number} {title}"
    return title


# --------------------------------------------------------------------------- #
# Текст и Markdown
# --------------------------------------------------------------------------- #

_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SETEXT_RE = re.compile(r"^(.+?)\s*\n(=+|-{3,})\s*$", re.MULTILINE)


def parse_text(text: str, path: Path) -> ParsedDoc:
    """Разбирает текстовые форматы: заголовки Markdown, остальное — как есть."""
    lines = text.splitlines()
    sections: list[Section] = []
    toc: list[tuple[int, str]] = []
    current_heading = ""
    current_level = 1
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if current_heading or body:
            sections.append(Section(heading=current_heading, text=body, level=current_level))
        buf.clear()

    for i, line in enumerate(lines):
        m = _ATX_RE.match(line)
        if not m and i + 1 < len(lines) and _SETEXT_RE.match(f"{line}\n{lines[i + 1]}"):
            # Setext-заголовок: текст на одной строке и подчёркивание.
            m = re.match(r"^(.+)$", line)
            level = 1 if lines[i + 1].lstrip().startswith("=") else 2
            flush()
            heading = (m.group(1) if m else "").strip()
            current_heading, current_level = heading, level
            toc.append((level, heading))
            continue
        if m:
            flush()
            current_heading, current_level = m.group(2).strip(), len(m.group(1))
            toc.append((current_level, current_heading))
            continue
        buf.append(line)
    flush()

    # Пустые разделы (заголовок без текста) не несут векторной пользы.
    sections = [s for s in sections if s.text or s.heading]
    parsed = ParsedDoc(title="", sections=sections, toc=toc, text=normalize(text))
    parsed.title = _first_heading(parsed, path)
    return parsed


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def parse_pdf(path: Path) -> ParsedDoc:
    """Текст PDF постранично плюс закладки (outline), если они есть."""
    try:
        from pypdf import (
            PdfReader,
        )
    except ImportError as e:  # pragma: no cover
        raise ParseError("не установлен pypdf: uv add pypdf") from e

    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise ParseError(f"PDF не открылся ({path.name}): {e}") from e

    sections: list[Section] = []
    outline = _pdf_outline(reader)
    for number, page in enumerate(reader.pages, start=1):
        page_text = normalize(page.extract_text() or "")
        if not page_text:
            continue  # страница-картинка: текста нет, сканирование не умеем
        sections.append(Section(heading=f"стр. {number}", text=page_text, level=2))

    toc = outline or [(1, s.heading) for s in sections if s.heading]
    if outline and sections:
        # Есть закладки, но их не с чем сопоставить постранично — оставляем
        # страницы как разделы, а закладки уйдут в оглавление документа.
        pass

    parsed = ParsedDoc(title="", sections=sections, toc=toc)
    # Название берём из «шапки» первой страницы: она одинаково устроена у
    # рабочих программ («…ПРОГРАММА» → «ДИСЦИПЛИНЫ» → название). Первая
    # строка страницы хуже: там чаще всего стоит название университета.
    first_page = sections[0].text.splitlines() if sections else []
    parsed.title = _title_from_preamble(path, first_page)
    if not parsed.title:
        first_line = first_page[0].strip() if first_page else ""
        if 0 < len(first_line) <= 80 and not first_line.endswith((".", ":", ";")):
            parsed.title = first_line
        else:
            parsed.title = _first_heading(parsed, path)
    if not parsed.title or parsed.title.startswith("стр."):
        parsed.title = _title_from_name(path)
    if not sections:
        raise ParseError(
            f"в PDF нет извлекаемого текста ({path.name}) — вероятно, скан. "
            "Такие файлы надо сначала прогнать через OCR."
        )
    return parsed


def _pdf_outline(reader: object) -> list[tuple[int, str]]:
    """Вытаскивает плоское оглавление из закладок PDF.

    Закладки — самая ненадёжная часть PDF: их может не быть вовсе, они
    бывают вложенными списками, а повреждённый файл падает чем угодно.
    Любая ошибка здесь означает просто «оглавления нет» — это не повод
    отказываться разбирать документ.
    """
    try:
        outline = reader.outline  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — см. докстринг: любая ошибка = «оглавления нет»
        return []

    flat: list[tuple[int, str]] = []

    def walk(items: Iterable[Any], level: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
            else:
                title = str(getattr(item, "title", "")).strip()
                if title:
                    flat.append((level, title))

    try:
        walk(outline, 1)
    except Exception:  # noqa: BLE001 — см. докстринг
        return []
    return flat


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #

def _heading_level(text: str) -> int:
    """Уровень заголовка по его номеру: «Раздел 1.» → 1, «1.1.» → 2, «2.1.1» → 3."""
    if text.lower().startswith("раздел"):
        return 1
    m = re.match(r"^(\d+(?:\.\d+)*)\.?\s", text)
    if not m:
        return 2
    return min(3, m.group(1).count(".") + 1)


def _bold_heading(para: object) -> str:
    """Заголовок «жирным текстом» с номером — либо пустая строка.

    Рабочие программы дисциплин СПбГУ не используют стили Heading:
    структура («Раздел 1.», «1.1. Цели и задачи») набрана полужирным
    начертанием. Без такого распознавания документ читается как один
    безголовый кусок текста: в оглавлении пусто, у всех чанков один
    заголовок «(без заголовка)», а структура теряется и для поиска,
    и для цитат в ответах.
    """
    text = para.text.strip()  # type: ignore[attr-defined]
    if not text or not _NUMBERED_HEADING_RE.match(text):
        return ""
    runs = [r for r in para.runs if r.text.strip()]  # type: ignore[attr-defined]
    if not runs or not all(r.bold for r in runs):
        return ""
    return text


def parse_docx(path: Path) -> ParsedDoc:
    """Word: разделы по стилям Heading и по полужирной нумерации, таблицы — как текст ячеек через таб."""
    try:
        import docx
    except ImportError as e:  # pragma: no cover
        raise ParseError("не установлен python-docx: uv add python-docx") from e

    document = docx.Document(str(path))
    sections: list[Section] = []
    toc: list[tuple[int, str]] = []
    heading = ""
    level = 1
    title_line = ""
    buf: list[str] = []
    preamble: list[str] = []  # текст до первого заголовка — «шапка» документа

    def flush() -> None:
        # Абзацы разделяем пустой строкой: чанкер режет текст по пустой
        # строке, и без неё весь раздел склеится в один нечитаемый чанк.
        body = "\n\n".join(buf).strip()
        if heading or body:
            sections.append(Section(heading=heading, text=body, level=level))
        buf.clear()

    for para in document.paragraphs:
        style = (para.style.name or "") if para.style is not None else ""
        if style == "Title":
            # Стиль Title — это «название всего документа», а не раздел:
            # он не попадает в оглавление, но заменяет имя файла, если
            # никто не передал название вручную.
            if not title_line:
                title_line = para.text.strip()
        elif style.startswith("Heading"):
            flush()
            try:
                level = int(style.split()[-1])
            except ValueError:
                level = 1
            heading = para.text.strip()
            toc.append((level, heading))
        elif para.text.strip():
            bold_heading = _bold_heading(para)
            if bold_heading:
                flush()
                level = _heading_level(bold_heading)
                heading = bold_heading
                toc.append((level, heading))
            else:
                buf.append(para.text.strip())
                if not toc:  # до первого заголовка — это «шапка»
                    preamble.extend(t for t in para.text.strip().splitlines() if t.strip())
    flush()

    for table in document.tables:
        rows = []
        for row in table.rows:
            # Переводы строк внутри ячейки схлопываем в пробел: иначе
            # одна ячейка разорвёт строку таблицы на части.
            cells = [" ".join(cell.text.split()) for cell in row.cells]
            rows.append("\t".join(cells))
        if rows:
            # Каждая строка таблицы — отдельный абзац: большие таблицы
            # (тематический план, матрица компетенций) иначе не режутся.
            buf.append("\n\n".join(rows))
    if buf:
        sections.append(Section(heading="Таблицы", text="\n\n".join(buf), level=2))

    parsed = ParsedDoc(title="", sections=sections, toc=toc)
    parsed.title = title_line or _title_from_preamble(path, preamble) or _first_heading(parsed, path)
    return parsed


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #

def parse_xlsx(path: Path) -> ParsedDoc:
    """Excel: лист = раздел, первые строки = шапка, дальше — строки через таб."""
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError as e:  # pragma: no cover
        raise ParseError("не установлен openpyxl: uv add openpyxl") from e

    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    sections: list[Section] = []
    toc: list[tuple[int, str]] = []
    for sheet in book.worksheets:
        rows: list[str] = []
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in row]
            if any(cells):
                rows.append("\t".join(cells).rstrip("\t"))
        # Строки таблицы — отдельные абзацы: иначе лист склеится в один
        # чанк на десятки тысяч символов.
        sections.append(Section(heading=sheet.title, text="\n\n".join(rows), level=1))
        toc.append((1, sheet.title))
    book.close()

    parsed = ParsedDoc(title="", sections=sections, toc=toc)
    parsed.title = _first_heading(parsed, path)
    return parsed


# --------------------------------------------------------------------------- #
# PowerPoint
# --------------------------------------------------------------------------- #

def parse_pptx(path: Path) -> ParsedDoc:
    """PowerPoint: слайд = раздел, заголовок слайда = заголовок раздела."""
    try:
        from pptx import Presentation
    except ImportError as e:  # pragma: no cover
        raise ParseError("не установлен python-pptx: uv add python-pptx") from e

    deck = Presentation(str(path))
    sections: list[Section] = []
    toc: list[tuple[int, str]] = []
    first_title = ""  # заголовок первого слайда — лучшее название презентации
    for number, slide in enumerate(deck.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
                if text:
                    texts.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    texts.append("\t".join(cell.text.strip() for cell in row.cells))
        # Первый текстовый блок слайда — обычно заголовок слайда; он же
        # уходит в название документа, если это первый слайд.
        title = texts.pop(0).splitlines()[0][:80] if texts else ""
        if title and not first_title:
            first_title = title
        sections.append(Section(heading=f"слайд {number}: {title}".strip(": "), text="\n\n".join(texts), level=1))
        toc.append((1, f"Слайд {number}: {title}".strip(": ")))

    parsed = ParsedDoc(title=first_title, sections=sections, toc=toc)
    if not parsed.title:
        parsed.title = _title_from_name(path)
    return parsed


# --------------------------------------------------------------------------- #
# pandoc — универсальный фолбэк
# --------------------------------------------------------------------------- #

def parse_pandoc(path: Path) -> ParsedDoc:
    """Любой формат, который знает pandoc (html, odt, tex, epub, …)."""
    if shutil.which("pandoc") is None:
        raise ParseError(
            f"формат {path.suffix or '(без расширения)'} не поддержан напрямую, "
            f"а pandoc не установлен — чтобы разобрать {path.name}, поставьте pandoc"
        )
    try:
        result = subprocess.run(
            ["pandoc", "-f", "auto", "-t", "markdown", "--wrap=none", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise ParseError(f"pandoc не смог разобрать {path.name}: {e}") from e
    return parse_text(result.stdout, path)


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def parse_file(path: Path) -> ParsedDoc:
    """Разбирает файл по расширению. Неизвестное расширение уходит в pandoc."""
    if not path.is_file():
        raise ParseError(f"нет такого файла: {path}")
    suffix = path.suffix.lower()

    if suffix in {".pdf"}:
        return parse_pdf(path)
    if suffix in {".docx"}:
        return parse_docx(path)
    if suffix in {".xlsx", ".xlsm"}:
        return parse_xlsx(path)
    if suffix in {".pptx"}:
        return parse_pptx(path)
    if suffix in TEXT_SUFFIXES:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Иногда под .txt прячется cp1251 — пробуем и это.
            try:
                text = path.read_text(encoding="cp1251")
            except UnicodeDecodeError as e:
                raise ParseError(f"{path.name}: не похоже на текст в utf-8/cp1251") from e
        return parse_text(text, path)
    if suffix in PANDOC_SUFFIXES:
        return parse_pandoc(path)
    # Последняя попытка: вдруг это текст с необычным расширением.
    try:
        text = path.read_text(encoding="utf-8")
        if "\x00" not in text:
            return parse_text(text, path)
    except (UnicodeDecodeError, OSError):
        pass
    return parse_pandoc(path)
