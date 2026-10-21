"""
Хранилище: sqlite-vec (векторы) + FTS5 (полнотекст) в одном файле.

Вместо отдельной векторной базы используется SQLite с двумя
встроенными/встраиваемыми расширениями:

  * vec0 — виртуальная таблица для kNN-поиска по косинусу (sqlite-vec);
  * FTS5 — полнотекстовый индекс, встроенный в сам SQLite.

Почему так, а не Chroma/pgvector: один файл базы, ноль сервисов, ноль
сетевых вызовов, стандартный sqlite3 из коробки. Для каталога в тысячи
документов этого достаточно, а всё остальное (чанкинг, морфология,
гибридный поиск) видно целиком — без «коробки», которая сама всё делает.

Как устроен поиск (гибридный, потому что ни один из способов в одиночку
не работает хорошо):

  1. BM25 по FTS5 — точные слова и словоформы, лемматизированные
     pymorphy3/snowball. Сильное место: термины, которых нет в
     обучающей выборке эмбеддингов (имена, код, канцелярит).
  2. kNN по vec0 — смысловое сходство, работает с синонимами и
     перефразированием, одинаково хорошо для русского и английского.
  3. Слияние через RRF (reciprocal rank fusion): каждый кандидат получает
     сумму w/(60 + ранг) по каждому способу. RRF не требует калибровки
     баллов BM25 и косинуса — они несопоставимы по шкале, а ранги
     сопоставимы всегда.

Документ хранится один раз в таблице documents, его чанки — в chunks;
векторы и FTS-индекс указывают на чанки по rowid. Удаление документа =
удаление строк в трёх таблицах в одной транзакции.
"""

from __future__ import annotations

import array
import json
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]

from rag_config import Config
from rag_text import Chunk, Section, chunk_sections, lemmatize, stem, words

# array.array в typeshed объявлен generic — array.array[float] означает
# «массив C-двоичных float», ровно то, что принимает sqlite-vec.
FloatArray = array.array[float]

# Константа RRF из оригинальной статьи (Cormack et al., 2009): означает,
# что разница между 1-м и 10-м местом умеренная, а не катастрофическая.
RRF_K = 60

# Сколько кандидатов берём с каждой стороны до слияния RRF.
DEFAULT_CANDIDATES = 40


class StoreError(RuntimeError):
    """Ошибка хранилища: сообщение должно содержать, что именно пошло не так."""


# --------------------------------------------------------------------------- #
# Метаданные
# --------------------------------------------------------------------------- #

@dataclass
class Document:
    """Запись о документе в каталоге."""

    id: int
    path: str  # путь относительно каталога документов
    title: str
    lang: str
    format: str
    chars: int
    sha256: str
    mtime: float
    annotation: str = ""
    toc: list[tuple[int, str]] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    added_at: float = 0.0
    updated_at: float = 0.0
    n_chunks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "lang": self.lang,
            "format": self.format,
            "chars": self.chars,
            "annotation": self.annotation,
            "toc": [{"level": lvl, "heading": name} for lvl, name in self.toc],
            "chunks": self.n_chunks,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Hit:
    """Найденный фрагмент: чанк плюс его документ и оценка."""

    doc_id: int
    path: str
    title: str
    heading: str
    text: str
    score: float
    rank: int = 0
    vec_rank: int | None = None
    bm25_rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "path": self.path,
            "title": self.title,
            "heading": self.heading,
            "text": self.text,
            "score": round(self.score, 6),
            "rank": self.rank,
            "vec_rank": self.vec_rank,
            "bm25_rank": self.bm25_rank,
        }


# --------------------------------------------------------------------------- #
# Разбор запроса по словам: есть ли тема в корпусе
# --------------------------------------------------------------------------- #

@dataclass
class TermInfo:
    """Слово запроса и то, как оно выглядит в корпусе.

    df — число чанков, в которых слово встречается (по колонке лемм).
    present — есть ли слово в корпусе вообще. Оно может быть и при df == 0:
    тогда слово найдено по префиксу (см. Store.query_terms), и df равен
    числу совпадений по префиксу.
    """

    surface: str  # слово так, как его написал пользователь
    lemma: str  # лемма — та же нормализация, что и в корпусе
    df: int  # сколько чанков корпуса содержат это слово
    present: bool  # встречается ли слово в корпусе


@dataclass
class QueryTerms:
    """Разбор запроса: какие слова корпус знает, а какие нет.

    Нужен, чтобы поиск не притворялся, что нашёл тему, которой в
    каталоге нет. Слова запроса и слова корпуса нормализованы одинаково
    (rag_text.lemmatize), поэтому «квантовые вычисления» превращается в
    «квантовый вычисление», и по корпусу видно: «квантовый» не встречается
    ни разу, а «вычисление» — 84 раза.
    """

    query: str
    terms: list[TermInfo]

    @property
    def missing(self) -> list[str]:
        """Слова запроса, которых нет в корпусе (в исходной форме)."""
        return [t.surface for t in self.terms if not t.present]

    @property
    def rarest(self) -> TermInfo | None:
        """Самое редкое слово запроса — обычно именно оно несёт тему.

        «Вычисление» встречается в 84 чанках и само по себе ни о чём не
        говорит, а «квантовый» не встречается ни разу: вопрос про
        квантовые вычисления — это про то, чего в каталоге нет.
        """
        if not self.terms:
            return None
        return min(self.terms, key=lambda t: t.df)

    @property
    def not_found(self) -> bool:
        """Темы, похоже, нет в корпусе: самое редкое слово запроса отсутствует.

        Правило намеренно грубое и не блокирующее: это сигнал для
        показа пользователю и для подсказки модели, а не запрет. Ложные
        срабатывания бывают (слово есть в другой словоформе или корпус
        пишет синоним), поэтому решение «найдено / не найдено» остаётся
        за моделью, которая читает сами фрагменты.
        """
        rarest = self.rarest
        if rarest is None:
            return False
        return not rarest.present


@dataclass
class SearchResult:
    """Результат поиска: фрагменты + разбор запроса по словам."""

    query: str
    hits: list[Hit]
    terms: QueryTerms

    @property
    def not_found(self) -> bool:
        """Темы нет в корпусе (см. QueryTerms.not_found)."""
        return self.terms.not_found

    @property
    def matched_terms(self) -> list[str]:
        """Слова запроса, которые корпус знает."""
        return [t.surface for t in self.terms.terms if t.present]

    @property
    def note(self) -> str:
        """Пояснение к результату по-человечески (пусто, если вопросов нет).

        Флаги в JSON легко потерять, а фрагменты — легко принять за ответ.
        Одна строка прямо говорит, что нашлось и почему.
        """
        missing = self.terms.missing
        if not missing:
            return ""
        listed = ", ".join(f"«{w}»" for w in missing)
        if self.not_found:
            return (
                f"Темы в каталоге нет: в документах не встречаются слова {listed}. "
                "Найденные фрагменты совпали только по остальным словам запроса."
            )
        return f"В каталоге не встречаются слова {listed}."


# --------------------------------------------------------------------------- #
# Эмбеддинги (ленивая загрузка модели)
# --------------------------------------------------------------------------- #

class Embedder:
    """Обёртка над fastembed.

    Модель (~500 МБ ONNX) качается при первом запуске в cache_dir и дальше
    живёт в кэше. Загрузка занимает секунды, поэтому модель грузится
    лениво — при первом обращении, а не при импорте модуля. Повторное
    использование одного экземпляра обязательно: загрузка модели на каждый
    запрос превратила бы добавление документа в минуты.
    """

    def __init__(self, model_name: str, cache_dir: Path) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: Any = None
        self.dim: int | None = None  # становится известно после загрузки

    def _load(self) -> Any:
        if self._model is None:
            try:
                from fastembed import (
                    TextEmbedding,
                )
            except ImportError as e:  # pragma: no cover
                raise StoreError("не установлен fastembed: uv add fastembed") from e
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(self.model_name, cache_dir=str(self.cache_dir))
        return self._model

    def embed(self, texts: list[str]) -> list[FloatArray]:
        """Векторы для списка текстов; возвращаем array('f') — sqlite-vec принимает их напрямую."""
        if not texts:
            return []
        model = self._load()
        vectors = [FloatArray("f", v) for v in model.embed(texts, batch_size=32)]
        if self.dim is None:
            self.dim = len(vectors[0])
        return vectors

    def embed_one(self, text: str) -> FloatArray:
        """Вектор для одного текста (запроса)."""
        return self.embed([text])[0]

    def known_dim(self) -> int | None:
        """Размерность модели из её описания в fastembed — без загрузки весов.

        Нужна при создании схемы: vec0 требует объявить размерность в DDL,
        а загружать ради этого полмодели — бессмысленно. Список моделей
        лежит внутри пакета fastembed статикой, сетевых вызовов нет.
        """
        if self.dim is not None:
            return self.dim
        try:
            from fastembed import TextEmbedding
            for info in TextEmbedding.list_supported_models():
                if info["model"] == self.model_name:
                    return int(info["dim"])
        except Exception:  # noqa: BLE001 — любой сбой означает «просто не знаем»
            return None
        return None


# --------------------------------------------------------------------------- #
# Само хранилище
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    lang        TEXT NOT NULL DEFAULT '',
    format      TEXT NOT NULL DEFAULT '',
    chars       INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL DEFAULT '',
    mtime       REAL NOT NULL DEFAULT 0,
    annotation  TEXT NOT NULL DEFAULT '',
    toc         TEXT NOT NULL DEFAULT '[]',
    sections    TEXT NOT NULL DEFAULT '[]',
    added_at    REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    doc_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal  INTEGER NOT NULL,
    heading  TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);

-- Векторная таблица: строка на чанк, вектор float[dim], косинусное расстояние.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    embedding float[{dim}] distance_metric=cosine
);

-- Полнотекстовый индекс по леммам и исходному тексту.
-- unicode61 + remove_diacritics: диакритика снимается, кириллица
-- токенизируется как обычные слова.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    lemmas,
    text,
    tokenize='unicode61 remove_diacritics 2'
);
"""


class Store:
    """Открытое хранилище: одна база, один embedder на процесс."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.config.store_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config.store_dir / "rag.sqlite3"
        self.embedder = Embedder(config.embedding_model, config.cache_dir)
        self._db: sqlite3.Connection | None = None
        # Кэш частот слов: подсчёт df — запрос к FTS, а запросы повторяются
        # (один и тот же вопрос из разных инструментов).
        self._df_cache: dict[str, int] = {}

    # -- соединение и схема ------------------------------------------------ #

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            self._db = conn
            self._create_schema()
        return self._db

    def _create_schema(self) -> None:
        """Создаёт таблицы; размерность векторов фиксируется при создании.

        Размерность — свойство модели, а не базы: vec0 требует объявить её
        в DDL, а векторы разной размерности несопоставимы. Поэтому dim
        записывается в PRAGMA user_version при первом создании; при
        расхождении с текущей моделью падаем с понятной ошибкой, а не с
        «incompatible dtype» из глубин sqlite-vec.
        """
        conn = self._db
        assert conn is not None  # вызывается только из db-свойства после присвоения
        stored = int(conn.execute("PRAGMA user_version").fetchone()[0])
        dim = self.embedder.known_dim() or 384
        if stored == 0:
            # Первое создание: фиксируем размерность текущей модели.
            conn.executescript(_SCHEMA.format(dim=dim))
            conn.execute(f"PRAGMA user_version = {dim}")
            conn.commit()
        else:
            if stored != dim:
                raise StoreError(
                    f"база {self.db_path.name} создана моделями размерности {stored}, "
                    f"а текущая модель '{self.embedder.model_name}' даёт {dim}. "
                    "Верните прежнюю модель в конфиге или начните базу заново "
                    "(удалите файл базы)."
                )
            # Схема идемпотентна (IF NOT EXISTS) — так база «лечится» после
            # частичного создания или обновления кода.
            conn.executescript(_SCHEMA.format(dim=stored))

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # -- каталог ----------------------------------------------------------- #

    def iter_catalog(self) -> Iterator[Path]:
        """Обходит каталог документов, заходя в симлинки подкаталогов.

        Симлинки — штатный способ подключить чужой каталог (например,
        симлинк ~/Документы/курс на каталог примера). Обход свой, а не
        os.walk(followlinks=True): так есть защита от петель симлинков.
        """
        root = self.config.catalog_dir
        if not root.is_dir():
            return
        seen_dirs: set[Path] = set()

        def walk(directory: Path) -> Iterator[Path]:
            try:
                real = directory.resolve()
            except OSError:
                return
            if real in seen_dirs:
                return
            seen_dirs.add(real)
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                return
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    if entry.is_symlink() and not self.config.follow_symlinks:
                        continue
                    yield from walk(entry)
                elif entry.is_file():
                    yield entry

        yield from walk(root)

    # -- добавление и обновление ------------------------------------------- #

    def upsert(
        self,
        rel_path: str,
        title: str,
        sections: list[Section],
        toc: list[tuple[int, str]],
        annotation: str,
        lang: str,
        fmt: str,
        sha256: str,
        mtime: float,
    ) -> Document:
        """Кладёт или обновляет документ целиком: метаданные + чанки + векторы.

        Старые чанки удаляются вместе с векторами и FTS-записями — замена
        всегда полная: так проще и надёжнее, чем точечный патч чанков,
        который мог бы оставить осиротевшие векторы.
        """
        text = "\n\n".join(s.text for s in sections)
        chunks = chunk_sections(sections, self.config.chunk_chars)
        conn = self.db
        now = time.time()
        with conn:
            row = conn.execute(
                "SELECT id, added_at FROM documents WHERE path = ?", (rel_path,)
            ).fetchone()
            doc_id = int(row["id"]) if row else None
            added_at = float(row["added_at"]) if row else now
            toc_json = json.dumps(toc, ensure_ascii=False)
            sections_json = json.dumps(sections_to_dicts(sections), ensure_ascii=False)
            if doc_id is None:
                cur = conn.execute(
                    "INSERT INTO documents (path, title, lang, format, chars, sha256, mtime,"
                    " annotation, toc, sections, added_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rel_path, title, lang, fmt, len(text), sha256, mtime,
                     annotation, toc_json, sections_json, added_at, now),
                )
                doc_id = last_id(cur)
            else:
                # Сначала вычищаем векторы и FTS-строки: виртуальные таблицы
                # не знают каскадов, а rowid у чанков переиспользуются —
                # если оставить осиротевшие строки, повторная запись того же
                # rowid упадёт на UNIQUE chunks_vec.
                stale = [
                    int(r["id"])
                    for r in conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))
                ]
                for cid in stale:
                    conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (cid,))
                    conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (cid,))
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
                conn.execute(
                    "UPDATE documents SET title = ?, lang = ?, format = ?, chars = ?,"
                    " sha256 = ?, mtime = ?, annotation = ?, toc = ?, sections = ?,"
                    " updated_at = ? WHERE id = ?",
                    (title, lang, fmt, len(text), sha256, mtime,
                     annotation, toc_json, sections_json, now, doc_id),
                )
            self._store_chunks(doc_id, chunks)
        return self.get(doc_id)

    def _store_chunks(self, doc_id: int, chunks: list[Chunk]) -> None:
        """Пишет чанки и их векторы. Вызывается внутри транзакции вызывающего.

        Индексируется не только текст чанка, но и его заголовок: часто
        термин живёт именно в заголовке («Зачем нужен чанкинг»), а в теле
        раздела его может и не быть. Заголовок идёт первым строкой —
        и в FTS, и в эмбеддинг, — но в базе хранится чистый текст,
        чтобы цитаты в ответах оставались подлинными.
        """
        if not chunks:
            return
        indexed = [f"{c.heading}\n{c.text}" if c.heading else c.text for c in chunks]
        vectors = self.embedder.embed(indexed)
        fts_rows: list[tuple[int, str, str]] = []
        for chunk, source, vector in zip(chunks, indexed, vectors, strict=True):
            cur = self.db.execute(
                "INSERT INTO chunks (doc_id, ordinal, heading, text) VALUES (?, ?, ?, ?)",
                (doc_id, chunk.ordinal, chunk.heading, chunk.text),
            )
            chunk_id = last_id(cur)
            self.db.execute(
                "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)", (chunk_id, vector)
            )
            fts_rows.append((chunk_id, lemmatize(source), source))
        self.db.executemany(
            "INSERT INTO chunks_fts (rowid, lemmas, text) VALUES (?, ?, ?)", fts_rows
        )

    def prune(self) -> dict[str, int]:
        """Убирает осиротевшие строки в chunks_vec / chunks_fts.

        Сироты появляются, если чанки удаляются минуя виртуальные таблицы
        (как в старом баге upsert): rowid у чанков переиспользуются, и
        осиротевший вектор мешает новой записи того же rowid, а попав в
        kNN-выдачу — отвечает пустотой, потому что строки чанка уже нет.
        Безопасно запускать сколько угодно раз: удаляется только то, чему
        соответствующего чанка в базе нет.
        """
        conn = self.db
        chunk_ids = {int(r["id"]) for r in conn.execute("SELECT id FROM chunks")}
        orphans = {
            table: sorted({int(r[0]) for r in conn.execute(f"SELECT rowid FROM {table}")} - chunk_ids)
            for table in ("chunks_vec", "chunks_fts")
        }
        removed = 0
        with conn:
            for table, ids in orphans.items():
                for rowid in ids:
                    conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
                    removed += 1
        # Чанк без вектора невидим для поиска, но виден в цитатах —
        # это тоже расхождение, его показываем, а не чиним молча.
        missing = len(chunk_ids - {int(r[0]) for r in conn.execute("SELECT rowid FROM chunks_vec")})
        return {
            "orphan_vec": len(orphans["chunks_vec"]),
            "orphan_fts": len(orphans["chunks_fts"]),
            "removed": removed,
            "chunks_without_vec": missing,
        }

    def remove(self, path_or_id: str | int) -> bool:
        """Удаляет документ и все его чанки. Возвращает, был ли он в базе."""
        conn = self.db
        key = str(path_or_id)
        row = (
            conn.execute("SELECT id FROM documents WHERE id = ?", (key,)).fetchone()
            if key.isdigit()
            else conn.execute("SELECT id FROM documents WHERE path = ?", (key,)).fetchone()
        )
        if row is None:
            return False
        doc_id = int(row["id"])
        chunk_ids = [
            int(r["id"])
            for r in conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))
        ]
        with conn:
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            for cid in chunk_ids:
                conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (cid,))
                conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (cid,))
        return True

    # -- чтение ------------------------------------------------------------ #

    def get(self, doc_id: int) -> Document:
        row = self.db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise StoreError(f"нет документа с id={doc_id}")
        return row_to_document(row, self.db)

    def by_path(self, rel_path: str) -> Document | None:
        row = self.db.execute("SELECT * FROM documents WHERE path = ?", (rel_path,)).fetchone()
        return row_to_document(row, self.db) if row else None

    def documents(self) -> list[Document]:
        rows = self.db.execute("SELECT * FROM documents ORDER BY path").fetchall()
        return [row_to_document(r, self.db) for r in rows]

    def stats(self) -> dict[str, Any]:
        conn = self.db
        docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        return {
            "db": str(self.db_path),
            "documents": int(docs),
            "chunks": int(chunks),
            "embedding_model": self.embedder.model_name,
            "dim": self.embedder.dim or self.embedder.known_dim(),
            "catalog": str(self.config.catalog_dir),
        }

    # -- поиск ------------------------------------------------------------- #

    def query_terms(self, query: str) -> QueryTerms:
        """Разбирает запрос по словам и проверяет каждое по корпусу.

        Слово считается найденным, если его лемма встречается в колонке
        лемм FTS-индекса. Если точного совпадения нет, делается мягкая
        проверка по основе слова (rag_text.stem) — по леммам и по сырому
        тексту. Она нужна для расхождений словоформ: корпус пишет
        «Геделя», а пользователь спрашивает про «Гёделя» (ё ≠ е), и без
        запаса слово выглядит отсутствующим.

        Короткие слова (2 символа и короче) пропускаются: это почти всегда
        либо служебные слова, либо аббревиатуры («NP», «n»), которых в
        прозрачном корпусе просто нет, — и из-за них запрос выглядел бы
        «не найденным» на пустом месте.

        Числа и прочие токены без букв тоже пропускаются: номера документов
        («082426») в тексте фрагментов не встречаются.
        """
        terms: list[TermInfo] = []
        for surface in words(query.lower()):
            if not any(ch.isalpha() for ch in surface):
                continue
            if len(surface) <= 2:
                continue
            lemma = lemmatize(surface)
            if not lemma:
                continue
            df = self._df(lemma)
            present = df > 0
            if not present:
                # Мягкий запас на словоизменение и словообразование:
                # «Гёделя» → «гедел*», «криптография» → «криптограф*».
                # Основу короче 5 букв не ищем: она слишком коротка, чтобы
                # надёжно отличать слова — по «шор» нашлось бы «шорение».
                base = stem(surface)
                if len(base) >= 5:
                    df = max(self._df_prefix("lemmas", base), self._df_prefix("text", base))
                    present = df > 0
            terms.append(TermInfo(surface=surface, lemma=lemma, df=df, present=present))
        return QueryTerms(query=query, terms=terms)

    def _df(self, lemma: str) -> int:
        """Сколько чанков корпуса содержат слово (по колонке лемм)."""
        cached = self._df_cache.get(lemma)
        if cached is not None:
            return cached
        try:
            row = self.db.execute(
                "SELECT count(*) AS n FROM chunks_fts WHERE chunks_fts MATCH ?",
                ('"' + lemma.replace('"', '""') + '"',),
            ).fetchone()
            df = int(row["n"])
        except sqlite3.OperationalError:
            # Слово непригодно для синтаксиса FTS — считаем, что его нет.
            df = 0
        self._df_cache[lemma] = df
        return df

    def _df_prefix(self, column: str, prefix: str) -> int:
        """Сколько чанков содержат слово, начинающееся с префикса."""
        key = f"{column}:{prefix}"
        cached = self._df_cache.get(key)
        if cached is not None:
            return cached
        try:
            row = self.db.execute(
                f"SELECT count(*) AS n FROM chunks_fts WHERE {column} MATCH ?",
                ('"' + prefix.replace('"', '""') + '"*',),
            ).fetchone()
            df = int(row["n"])
        except sqlite3.OperationalError:
            df = 0
        self._df_cache[key] = df
        return df

    def search(self, query: str, k: int = 5) -> SearchResult:
        """Гибридный поиск: BM25 по леммам + kNN по векторам, слияние RRF.

        Возвращает не только фрагменты, но и разбор запроса по словам
        (QueryTerms): поиск по леммам идёт через OR, поэтому запрос про
        отсутствующую тему («квантовые вычисления») находит фрагменты по
        второму слову. Разбор запроса показывает, что темы в корпусе нет.
        """
        query = query.strip()
        terms = self.query_terms(query)
        if not query:
            return SearchResult(query=query, hits=[], terms=terms)
        limit = max(self.config.candidates, k)

        bm25_hits = self._bm25(query, limit)

        # Порог max_distance отсекает векторный мусор (kNN обязан вернуть
        # k ближайших векторов даже для бессмысленного запроса), но фрагмент,
        # подтверждённый точным совпадением слов в BM25, остаётся в любом
        # случае: расстояние 0.83 для короткого чанка — обычное дело, а BM25
        # нашёл в нём слова запроса дословно. Без этой оговорки порог
        # отбрасывал правильные ответы на точечные вопросы («когда возможна
        # повторная эскалация» — верный фрагмент, но модель не видит в нём
        # общего смысла).
        bm25_ids = set(bm25_hits)
        vec_hits = [
            (chunk_id, distance)
            for chunk_id, distance in self._knn(query, limit)
            if distance <= self.config.max_distance or chunk_id in bm25_ids
        ]

        scores: dict[int, float] = {}
        vec_rank: dict[int, int] = {}
        bm25_rank: dict[int, int] = {}
        for rank, (chunk_id, distance) in enumerate(vec_hits, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + self.config.vector_weight / (RRF_K + rank)
            vec_rank[chunk_id] = rank
        for rank, chunk_id in enumerate(bm25_hits, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + self.config.bm25_weight / (RRF_K + rank)
            bm25_rank[chunk_id] = rank

        ordered = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:k]
        if not ordered:
            return SearchResult(query=query, hits=[], terms=terms)

        placeholders = ",".join("?" for _ in ordered)
        rows = self.db.execute(
            f"""
            SELECT c.id, c.doc_id, c.heading, c.text, d.path, d.title
            FROM chunks c JOIN documents d ON d.id = c.doc_id
            WHERE c.id IN ({placeholders})
            """,
            ordered,
        ).fetchall()
        by_id = {int(r["id"]): r for r in rows}
        hits: list[Hit] = []
        for rank, chunk_id in enumerate(ordered, start=1):
            row = by_id.get(chunk_id)
            if row is None:
                continue
            hits.append(
                Hit(
                    doc_id=int(row["doc_id"]),
                    path=str(row["path"]),
                    title=str(row["title"]),
                    heading=str(row["heading"]),
                    text=str(row["text"]),
                    score=scores[chunk_id],
                    rank=rank,
                    vec_rank=vec_rank.get(chunk_id),
                    bm25_rank=bm25_rank.get(chunk_id),
                )
            )
        return SearchResult(query=query, hits=hits, terms=terms)

    def _bm25(self, query: str, limit: int) -> list[int]:
        """FTS5-поиск по леммам запроса. Пустой результат — не ошибка:
        например, если в запросе только слова, которых нет в каталоге."""
        terms = lemmatize(query).split()
        if not terms:
            return []
        # FTS5-синтаксис: каждое слово в кавычках, объединённых OR —
        # взвешенный поиск, где любое совпадение уже хорошо.
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        try:
            rows = self.db.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?"
                " ORDER BY bm25(chunks_fts) LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [int(r["rowid"]) for r in rows]

    def _knn(self, query: str, limit: int) -> list[tuple[int, float]]:
        """Векторный поиск: (chunk_id, косинусное расстояние), по удалённости.

        Первый вызов может занять секунды (загрузка модели). Порог
        max_distance здесь НЕ применяется — он в search, где фильтр
        учитывает ещё и BM25-подтверждение. Замеры на этом каталоге:
        релевантные запросы дают расстояние 0.35–0.74, нерелевантные — от 0.8.
        """
        vector = self.embedder.embed_one(query)
        rows = self.db.execute(
            "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            (vector, limit),
        ).fetchall()
        return [(int(r["rowid"]), float(r["distance"])) for r in rows]


def sections_to_dicts(sections: list[Section]) -> list[dict[str, Any]]:
    """Разделы → JSON-дружественный вид для хранения в documents.sections."""
    return [{"heading": s.heading, "level": s.level, "text": s.text} for s in sections]


def last_id(cur: sqlite3.Cursor) -> int:
    """id только что вставленной строки.

    cur.lastrowid объявлен как int | None, а после успешного INSERT он
    всегда int. Хелпер, чтобы не размножать int(...) с подавлением типа.
    """
    if cur.lastrowid is None:  # pragma: no cover — после INSERT недостижимо
        raise StoreError("INSERT не вернул id строки")
    return int(cur.lastrowid)


def row_to_document(row: sqlite3.Row, conn: sqlite3.Connection) -> Document:
    """Строка documents → объект Document, подтягивая число чанков."""
    n_chunks = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE doc_id = ?", (row["id"],)
    ).fetchone()["n"]
    return Document(
        id=int(row["id"]),
        path=str(row["path"]),
        title=str(row["title"]),
        lang=str(row["lang"]),
        format=str(row["format"]),
        chars=int(row["chars"]),
        sha256=str(row["sha256"]),
        mtime=float(row["mtime"]),
        annotation=str(row["annotation"]),
        toc=[(int(level), str(heading)) for level, heading in json.loads(row["toc"])],
        sections=json.loads(row["sections"]),
        added_at=float(row["added_at"]),
        updated_at=float(row["updated_at"]),
        n_chunks=int(n_chunks),
    )
