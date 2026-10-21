"""
Текст для индексации: морфология, чанкинг, определение языка.

Здесь нет ни базы, ни сети — только работа со строками. Отдельный модуль
нужен, потому что морфология нужна в трёх местах (инструмент добавления,
сервис поиска, MCP-сервер), а держать её в каждом — значит расхождений.

Что здесь происходит и почему:

  * Лемматизация. FTS5 (встроенный в SQLite полнотекстовый индекс) не
    знает русской морфологии: «кошки», «кошку» и «кошек» — три разных
    слова, и запрос «кошка» ничего не найдёт. pymorphy3 сводит слово
    к словарной форме; для английского есть Snowball-стеммер. Леммы
    кладутся в отдельный столбец FTS5 — исходный текст остаётся
    нетронутым, чтобы цитаты в ответах были подлинными.

  * Чанкинг. Эмбеддинг считается не по целому документу (длинный текст
    «размазывается» в одном векторе и перестаёт отвечать за конкретный
    фрагмент), а по кускам. Разрез идёт по разделам (заголовкам), а
    внутри большого раздела — по абзацам с перекрытием: куски на границе
    не теряют контекст начала фразы.

  * Язык. Определяется долей кириллицы — просто и без моделей. Нужен для
    аннотации (модель пишет её на языке документа) и для выбора
    лемматизатора (для русского pymorphy3, для английского — стеммер).
"""

import math
import re
import unicodedata
from dataclasses import dataclass, field

import pymorphy3  # type: ignore[import-untyped]
from snowballstemmer import stemmer as _stemmer  # type: ignore[import-untyped]

# Порог кириллицы, начиная с которого считаем документ русским.
_CYRILLIC_THRESHOLD = 0.3

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# pymorphy3 ленивый: словарь (~50 МБ) читается при первом разборе.
# Держим один экземпляр на процесс — MorphAnalyzer не потокобезопасен,
# но и не пересоздаётся на каждый чанк.
_MORPH: pymorphy3.MorphAnalyzer | None = None
_STEMMER = _stemmer("english")
_STEMMER_RU = _stemmer("russian")


def _morph() -> pymorphy3.MorphAnalyzer:
    global _MORPH
    if _MORPH is None:
        _MORPH = pymorphy3.MorphAnalyzer()
    return _MORPH


def detect_lang(text: str) -> str:
    """Определяет язык текста: доля букв кириллицы среди всех букв."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    return "ru" if cyrillic / len(letters) >= _CYRILLIC_THRESHOLD else "en"


def lemmatize(text: str) -> str:
    """Сводит слова к словарным формам для полнотекстового индекса.

    Каждое слово обрабатывается по-своему: кириллица — pymorphy3,
    латиница — snowball. Смешанный текст («Страница API возвращает JSON»)
    обрабатывается верно, потому что разбор идёт по словам, а не по
    всему тексту сразу.
    """
    out: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        if "Ѐ" <= word[0] <= "ӿ":
            # pymorphy3 не знает слова — отдаёт его же, это нормально.
            out.append(_morph().parse(word)[0].normal_form)
        else:
            out.append(_STEMMER.stemWord(word))
    return " ".join(out)


def words(text: str) -> list[str]:
    """Простая токенизация: слова для подсчёта размера чанка."""
    return _WORD_RE.findall(text)


def stem(word: str) -> str:
    """Основа слова — чтобы мягко искать варианты написания.

    Лемма (pymorphy3) хороша для словоизменения, но не для словообразования:
    запрос «криптография» и корпусное «криптографический» — разные леммы,
    и без запаса слово выглядит отсутствующим. Основа обрезается по обоим
    концам сразу, поэтому «криптография» и «криптографический» дают одну
    основу «криптограф», а «Гёделя» и «Геделя» — «гедел».

    «ё» приводится к «е»: в корпусе «ё» пишут по-разному, и без этого
    запрос про «Гёделя» не нашёл бы «Геделя».
    """
    word = word.lower().replace("ё", "е")
    if not word:
        return ""
    if "Ѐ" <= word[0] <= "ӿ":
        return _STEMMER_RU.stemWord(word)
    return _STEMMER.stemWord(word)


@dataclass
class Chunk:
    """Кусок документа, который станет вектором в базе."""

    ordinal: int
    heading: str  # путь заголовков до этого куска: "Установка > Linux"
    text: str
    lang: str

    @property
    def lemmas(self) -> str:
        """Лемматизированная версия — то, что ищется по FTS5."""
        return lemmatize(self.text)


@dataclass
class Section:
    """Раздел документа: заголовок и его текст (до следующего заголовка)."""

    heading: str
    text: str
    level: int = 1


def heading_path(section: Section, stack: dict[int, str]) -> str:
    """Полный путь заголовков до раздела: «Родитель > Текущий».

    Уровень заголовка определяет, сколько уровней стека остаётся:
    заголовок второго уровня прячет всё, что было на его уровне и ниже.
    Так в цитате видно, где именно находится найденный фрагмент.
    """
    trail = [stack[lvl] for lvl in sorted(stack) if lvl < section.level and stack[lvl]]
    if section.heading:
        trail.append(section.heading)
    return " > ".join(trail)


def chunk_sections(sections: list[Section], max_chars: int, overlap: int = 100) -> list[Chunk]:
    """Разрезает разделы на чанки не длиннее max_chars.

    Сначала разделы склеиваются в блоки по max_chars (маленькие разделы
    идут вместе, чтобы не плодить крошечные векторы), потом каждый блок,
    если всё же слишком длинный, разрезается по абзацам с перекрытием
    overlap символов. Перекрытие важно: вопрос может попасть ровно на
    границу двух кусков, и без общего края ни один вектор не «услышит»
    обе половины.

    Заголовок чанка — полный путь по уровням первого раздела блока (см.
    heading_path), вычисленный в момент добавления раздела в буфер: к
    моменту сброса стек уже мог сдвинуться вслед за следующим заголовком.

    Собственные заголовки склеенных разделов остаются в тексте чанка.
    Это принципиально для индекса: термин может жить только в заголовке
    («Зачем нужен чанкинг»), и без него в тексте BM25 его не найдёт.
    """
    chunks: list[Chunk] = []
    buf: list[tuple[Section, str]] = []  # раздел + его путь заголовков на момент взятия
    buf_len = 0
    stack: dict[int, str] = {}  # уровень → заголовок: текущее место в оглавлении

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        heading = buf[0][1] or "(без заголовка)"
        parts = []
        for section, _path in buf:
            body = section.text.strip()
            if not body:
                continue
            # Заголовок раздела идёт первой строкой его текста — и в вектор,
            # и в FTS (см. Store._store_chunks), но хранится в чанке как есть.
            parts.append(f"{section.heading}\n{body}" if section.heading else body)
        text = "\n\n".join(parts)
        chunks.extend(_split_long(heading, text, max_chars, overlap, len(chunks)))
        buf, buf_len = [], 0

    for section in sections:
        # Новый заголовок того же или более высокого уровня затирает
        # всё, что было «глубже» — иначе стек протухает вглубь документа.
        for lvl in [l for l in stack if l >= section.level]:
            del stack[lvl]
        if section.heading:
            stack[section.level] = section.heading
        section_len = len(section.text)
        if buf and buf_len + section_len > max_chars:
            flush()
        buf.append((section, heading_path(section, stack)))
        buf_len += section_len
        # Один раздел сам по себе длиннее лимита — режем его в flush.
        if buf_len > max_chars:
            flush()
    flush()

    lang = detect_lang(" ".join(c.text for c in chunks))
    for chunk in chunks:
        chunk.lang = lang
    return chunks


def _split_long(
    heading: str, text: str, max_chars: int, overlap: int, start_ordinal: int
) -> list[Chunk]:
    """Разрезает длинный текст по абзацам с перекрытием."""
    if len(text) <= max_chars:
        return [Chunk(ordinal=start_ordinal, heading=heading, text=text, lang="")]

    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[Chunk] = []
    buf = ""
    prev_tail = ""  # хвост предыдущего куска — начало перекрытия
    for para in paragraphs:
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) > max_chars and buf:
            chunks.append(
                Chunk(
                    ordinal=start_ordinal + len(chunks),
                    heading=heading,
                    text=(prev_tail + "\n\n" + buf).strip(),
                    lang="",
                )
            )
            # Хвост обрезается по границе слова: цитата, начинающаяся с
            # середины слова («ть проблемой»), выглядит испорченной.
            prev_tail = _tail_at_word(buf, overlap)
            buf = para
        else:
            buf = candidate
    if buf.strip():
        chunks.append(
            Chunk(
                ordinal=start_ordinal + len(chunks),
                heading=heading,
                text=(prev_tail + "\n\n" + buf).strip(),
                lang="",
            )
        )
    return chunks


def _tail_at_word(text: str, overlap: int) -> str:
    """Последние overlap символов, но не посреди слова."""
    if len(text) <= overlap:
        return text
    tail = text[-overlap:]
    # Ищем первый пробел с начала хвоста — всё до него отбрасываем.
    space = tail.find(" ")
    return tail[space + 1 :] if 0 <= space < 40 else tail


def estimate_tokens(text: str) -> int:
    """Грубая оценка длины в токенах: ~4 символа на токен для смешанного текста.

    Нужна только для предупреждений («документ длинный, поиск по нему будет
    медленным») — точный подсчёт стоил бы зависимости tikoken ради одного числа.
    """
    return math.ceil(len(text) / 4)


@dataclass
class Digest:
    """Краткое описание документа — то, что показывается модели сессии.

    Модель по этому описанию пишет аннотацию, не читая весь документ:
    заголовок, структура и начало текста обычно достаточно, чтобы понять,
    о чём файл.
    """

    path: str
    title: str
    lang: str
    format: str
    chars: int
    n_sections: int
    n_chunks: int
    toc: list[tuple[int, str]] = field(default_factory=list)
    head: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "title": self.title,
            "lang": self.lang,
            "format": self.format,
            "chars": self.chars,
            "sections": self.n_sections,
            "chunks": self.n_chunks,
            "toc": [{"level": lvl, "heading": name} for lvl, name in self.toc],
            "head": self.head,
        }


def normalize(text: str) -> str:
    """Нормализует пробелы и переводы строк: текст из PDF/Office часто грязный."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
