#!/usr/bin/env -S uv run --quiet --script --
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "fastembed>=0.6",
#     "sqlite-vec>=0.1.6",
#     "pymorphy3>=2.0.1",
#     "snowballstemmer>=3.0",
#     "pypdf>=6",
#     "python-docx>=1.2",
#     "openpyxl>=3.1",
#     "python-pptx>=1.0",
# ]
# ///
"""
Инструмент добавления, обновления и удаления документов в RAG-хранилище.

Этот CLI вызывается не человеком, а агентом (см. скилл rag/SKILL.md):
агент читает файл, пишет по нему аннотацию и передаёт её сюда. Поэтому
все команды говорят на JSON — модели удобно, а человеку хватит --json=0
для читаемого вида.

Ключевое решение архитектуры: аннотацию пишет модель сессии OpenCode
(та же, что отвечает пользователю), а не отдельная «модель аннотаций».
Так не нужно ни отдельного API-ключа, ни второго сервиса, и качество
аннотации такое же, как у обычных ответов агента.

Команды:
    inspect ФАЙЛ              разобрать и показать структуру (ничего не писать)
    add ФАЙЛ --annotation ... добавить документ в каталог и в базу
    update ФАЙЛ               перечитать файл, если изменился — переиндексировать
    remove ФАЙЛ|ID            удалить документ из базы (файл не трогает)
    list                      что лежит в базе
    search ЗАПРОС             найти фрагменты (без ответа модели — для отладки)
    scan                      сравнить каталог и базу: новое/изменённое/удалённое
    plan КАТАЛОГ              манифест по каталогу: сводка по каждому файлу
    apply МАНИФЕСТ            загрузить в базу документы из манифеста
    reindex                   пере-чанковать документы текущим парсером
    prune                     убрать осиротевшие векторы/FTS-строки

Пакетный сценарий — тот, которым пользуются чаще всего, — состоит из
трёх шагов и от модели требует только содержание документов:

    plan КАТАЛОГ -o plan.json     разобрать все файлы и описать их
    (модель вписывает аннотации)  единственное место, где нужно думать
    apply plan.json               положить всё в каталог и в базу
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

# Локальные модули: uv run --script не кладёт каталог скрипта в sys.path,
# поэтому добавляем вручную — иначе import rag_store упадёт.
sys.path.insert(0, str(Path(__file__).parent))

from rag_config import Config, load_config
from rag_parse import SUPPORTED_SUFFIXES, ParseError, parse_file
from rag_store import Store
from rag_text import Digest, detect_lang, estimate_tokens


def sha256_of(path: Path) -> str:
    """Хеш содержимого — по нему decide, изменился ли файл."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def lexical_abs(path: Path) -> Path:
    """Абсолютный путь без разыменования симлинков.

    Нужно, чтобы файл за симлинком внутри каталога считался частью
    каталога: Path.resolve() следовал бы по симлинку наружу, и путь
    превратился бы в абсолютный путь к чужому файлу.
    os.path.abspath делает только normpath — ровно то, что нужно.
    """
    return Path(os.path.abspath(path))


def rel_path(config: Config, path: Path) -> str:
    """Путь документа относительно каталога — так он хранится в базе.

    Если файл лежит вне каталога, возвращается его абсолютный путь:
    база не знает, как его назвать, и это честнее, чем молча укоротить.
    """
    try:
        return str(lexical_abs(path).relative_to(config.catalog_dir))
    except ValueError:
        return str(lexical_abs(path))


def resolve_key(config: Config, target: str, known: set[str]) -> str:
    """Ключ документа в базе по тому, что дал пользователь.

    Указанное имя может быть путём относительно текущего каталога (так
    работают все файловые команды) либо тем, как документ записан в базе —
    относительно каталога документов (так печатает `list`). Второе поддерживаем:
    иначе `remove zapiska.md` после `list` превращается в гонку «попадёт ли
    текущий каталог в каталог документов».
    """
    given = Path(target).expanduser()
    candidates = [given] if given.is_absolute() else [given, config.catalog_dir / given]
    for candidate in candidates:
        rel = rel_path(config, candidate)
        if rel in known:
            return rel
    raise SystemExit(f"документа {target} в базе нет")


def make_digest(config: Config, path: Path, parsed: Any) -> Digest:
    """Сводка документа: заголовок, оглавление, начало текста.

    Начало текста (head) отдаётся, чтобы модель сессии поняла содержание,
    не читая весь файл: 1500 символов обычно хватает, чтобы понять тему.
    """
    from rag_text import (
        chunk_sections,
    )

    chunks = chunk_sections(parsed.sections, config.chunk_chars)
    body = "\n\n".join(s.text for s in parsed.sections)
    return Digest(
        path=str(path),
        title=parsed.title,
        lang=detect_lang(body),
        format=path.suffix.lower(),
        chars=len(body),
        n_sections=len(parsed.sections),
        n_chunks=len(chunks),
        toc=parsed.toc,
        head=body[:1500],
    )


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #

def cmd_inspect(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Разбирает файл и печатает сводку. Ничего не пишет в базу."""
    path = Path(args.path).expanduser()
    parsed = parse_file(path)
    digest = make_digest(config, path, parsed)
    out = digest.to_dict()
    out["warning"] = _warnings(digest)
    return out


def _warnings(digest: Digest) -> str:
    """Предупреждения, которые стоит показать модели: длинный документ, скан."""
    notes = []
    if digest.chars > 200_000:
        notes.append(
            f"документ очень большой ({digest.chars} знаков, ~{estimate_tokens('x' * digest.chars)} токенов) — "
            "индексация займёт заметное время"
        )
    if digest.n_chunks == 0:
        notes.append("из файла не извлекся текст — возможно, это скан или бинарник")
    return "; ".join(notes)


def _place_in_catalog(config: Config, path: Path) -> Path:
    """Копирует файл в каталог, если он лежит вне его.

    Каталог — это и есть хранилище документов: сервис ищет только в нём.
    Поэтому файл извне сюда копируется (а не индексируется по-стороннему
    пути, который завтра может исчезнуть). Симлинк не делаем: он ломается
    при переносе каталога. Сравнение лексическое (см. lexical_abs): файл
    за симлинком внутри каталога уже «свой», копировать его не нужно.

    Тот же файл, уже лежащий в каталоге, повторно не копируется: иначе
    повторный импорт одной и той же папки наплодил бы копии «-2», «-3».
    """
    source = lexical_abs(path)
    catalog = config.catalog_dir
    if source == catalog or catalog in source.parents:
        return source  # уже внутри каталога — в том числе за симлинком
    target = catalog / source.name
    if target.exists():
        if sha256_of(target) == sha256_of(source):
            return target  # тот же самый документ — копия не нужна
        # Не затираем чужой файл молча: имя конфликтует — добавляем числовой суффикс.
        stem, suffix = target.stem, target.suffix
        n = 2
        while target.exists():
            target = target.with_name(f"{stem}-{n}{suffix}")
            n += 1
    catalog.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def cmd_add(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Кладёт документ: копирует в каталог (если нужно), парсит, эмбеддит."""
    path = Path(args.path).expanduser()
    if not path.is_file():
        raise SystemExit(f"нет такого файла: {path}")
    stored_path = _place_in_catalog(config, path)
    parsed = parse_file(stored_path)
    store = Store(config)
    try:
        existing = store.by_path(rel_path(config, stored_path))
        if existing is not None and not args.force:
            raise SystemExit(
                f"документ уже есть в базе (id={existing.id}): {existing.path}. "
                "Чтобы перечитать файл, используйте update."
            )
        annotation = args.annotation or ""
        if not annotation:
            raise SystemExit(
                "без аннотации документ не добавить: скилл просит модель сессии "
                "написать её и передать через --annotation"
            )
        digest = make_digest(config, stored_path, parsed)
        doc = store.upsert(
            rel_path=rel_path(config, stored_path),
            title=args.title or parsed.title,
            sections=parsed.sections,
            toc=parsed.toc,
            annotation=annotation,
            lang=digest.lang,
            fmt=stored_path.suffix.lower(),
            sha256=sha256_of(stored_path),
            mtime=stored_path.stat().st_mtime,
        )
        return {"added": doc.to_dict()}
    finally:
        store.close()


def cmd_update(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Перечитывает файл: если хеш изменился — переиндексирует, иначе сообщает."""
    path = Path(args.path).expanduser()
    if not path.is_file():
        raise SystemExit(f"нет такого файла: {path}")
    path = _place_in_catalog(config, path)  # вне каталога — скопирует внутрь
    key = rel_path(config, path)
    store = Store(config)
    try:
        existing = store.by_path(key)
        if existing is None:
            raise SystemExit(f"документа {key} нет в базе — используйте add")
        new_sha = sha256_of(path)
        if new_sha == existing.sha256 and not args.force:
            return {"updated": False, "reason": "файл не изменился", "doc": existing.to_dict()}
        parsed = parse_file(path)
        digest = make_digest(config, path, parsed)
        annotation = args.annotation if args.annotation is not None else existing.annotation
        doc = store.upsert(
            rel_path=key,
            title=args.title or parsed.title,
            sections=parsed.sections,
            toc=parsed.toc,
            annotation=annotation,
            lang=digest.lang,
            fmt=path.suffix.lower(),
            sha256=new_sha,
            mtime=path.stat().st_mtime,
        )
        return {"updated": True, "doc": doc.to_dict()}
    finally:
        store.close()


def cmd_remove(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Удаляет документ из базы. Файл в каталоге остаётся — база и каталог независимы."""
    store = Store(config)
    try:
        key: str | int = (
            int(args.target) if str(args.target).isdigit()
            else resolve_key(config, args.target, {d.path for d in store.documents()})
        )
        found = store.remove(key)
        if not found:
            raise SystemExit(f"документа {key} в базе нет")
        return {"removed": key}
    finally:
        store.close()


def cmd_list(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    store = Store(config)
    try:
        docs = store.documents()
        return {"documents": [d.to_dict() for d in docs], "stats": store.stats()}
    finally:
        store.close()


def cmd_search(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    store = Store(config)
    try:
        hits = store.search(args.query, k=args.k)
        return {"query": args.query, "hits": [h.to_dict() for h in hits]}
    finally:
        store.close()


def cmd_scan(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Сравнивает каталог с базой: что добавилось, изменилось, пропало.

    Это команда «поддержи базу в актуальном состоянии»: файлы в каталоге
    меняются сами (обновились документы, подключился новый симлинк), и
    агенту проще один раз спросить «что изменилось?», чем перечитывать
    каждый файл.
    """
    store = Store(config)
    try:
        known = {d.path: d for d in store.documents()}
        new, changed = [], []
        seen: set[str] = set()
        for file in store.iter_catalog():
            key = rel_path(config, file)
            seen.add(key)
            doc = known.get(key)
            if doc is None:
                new.append(key)
            elif sha256_of(file) != doc.sha256:
                changed.append(key)
        missing = sorted(set(known) - seen)
        report = {"new": new, "changed": changed, "missing": missing}
        if args.apply:
            # --apply добавляет новое и переиндексирует изменённое, но без
            # аннотаций (их пишет модель сессии, см. скилл) — поэтому новое
            # здесь только перечисляется, а изменённое обновляется с прежней
            # аннотацией.
            updated = []
            for key in changed:
                file = config.catalog_dir / key
                parsed = parse_file(file)
                digest = make_digest(config, file, parsed)
                store.upsert(
                    rel_path=key,
                    title=parsed.title,
                    sections=parsed.sections,
                    toc=parsed.toc,
                    annotation=known[key].annotation,
                    lang=digest.lang,
                    fmt=file.suffix.lower(),
                    sha256=sha256_of(file),
                    mtime=file.stat().st_mtime,
                )
                updated.append(key)
            report["reindexed"] = updated
        return report
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Пакетная работа: план → разметка → применение
# --------------------------------------------------------------------------- #

def _walk_documents(root: Path) -> list[Path]:
    """Поддерживаемые файлы под корнем, по возрастанию пути.

    Скрытые файлы и каталоги пропускаются: там обычно лежат артефакты
    редакторов, а не документы.
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        found.append(path)
    return found


def cmd_plan(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Собирает манифест по каталогу: что вообще есть и как это читается.

    Ничего не пишет в базу и ничего не меняет на диске — только читает
    файлы и описывает их. Дальше модель (или субагент-разметчик) вписывает
    в манифест аннотации, а команда apply кладёт всё в базу.

    Смысл разделения: механику (обход каталога, разбор форматов, выбор
    названий, копирование, чанкинг, эмбеддинги) делает инструмент
    одинаково и безошибочно, а содержание читает и пересказывает модель.
    """
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"каталог не найден: {root}")
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for file in _walk_documents(root):
        rel = str(file.relative_to(root))
        try:
            parsed = parse_file(file)
            digest = make_digest(config, file, parsed)
        except ParseError as e:
            errors.append({"path": rel, "error": str(e)})
            continue
        body = "\n\n".join(s.text for s in parsed.sections)
        entries.append({
            "path": str(file),
            "rel": rel,
            "title": digest.title,
            "lang": digest.lang,
            "chars": digest.chars,
            "chunks": digest.n_chunks,
            "toc": [{"level": lvl, "heading": name} for lvl, name in digest.toc],
            "head": body[: args.head_chars],
            "warning": _warnings(digest),
            "annotation": "",
        })
    manifest = {"root": str(root), "files": entries}
    parts: list[str] = []
    if args.split and args.split > 1:
        if not args.output:
            raise SystemExit("для --split нужен -o: имя файла манифеста")
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # Части — нарезанные манифесты: их удобно раздать нескольким
        # разметчикам параллельно, каждый работает со своим файлом.
        size = (len(entries) + args.split - 1) // args.split
        for i in range(args.split):
            chunk = entries[i * size : (i + 1) * size]
            if not chunk:
                continue
            part_path = out.with_name(f"{out.stem}.part-{i + 1}{out.suffix or '.json'}")
            part_path.write_text(
                json.dumps({"root": str(root), "files": chunk}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            parts.append(str(part_path))
        return {"root": str(root), "files": entries, "manifest": str(out), "parts": parts, "errors": errors}
    return {"root": str(root), "files": entries, "parts": parts, "errors": errors}


def _read_manifests(paths: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Читает манифесты и возвращает пары (корень, запись о файле).

    Битый или чужой файл — это ошибка вызывающего, а не сбой программы:
    трейсбек здесь никому не нужен, достаточно внятного сообщения в stderr
    и кода возврата 1 (main превращает SystemExit со строкой в код 1).
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    for path in paths:
        source = Path(path).expanduser()
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"манифест не прочитан: {source} — {e.strerror or e}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"манифест не разобран ({source}): {e.msg}, строка {e.lineno}")
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise SystemExit(f"это не манифест из plan: в {source} нет поля files-списка")
        root = str(data.get("root", ""))
        for entry in data["files"]:
            if isinstance(entry, dict):
                entries.append((root, entry))
    return entries


def cmd_apply(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Кладёт в базу всё, что перечислено в манифестах.

    Один процесс на весь список: модель эмбеддингов грузится один раз, а
    не по разу на документ. Ошибка в одном файле не роняет остальные —
    она попадает в список failed, а процесс идёт дальше.
    """
    entries = _read_manifests(args.manifests)
    store = Store(config)
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    try:
        for root, entry in entries:
            raw = str(entry.get("path", ""))
            source = Path(raw).expanduser()
            if not source.is_absolute() and root:
                source = Path(root) / raw
            if not source.is_file():
                failed.append({"path": raw, "error": "файл не найден"})
                continue
            annotation = str(entry.get("annotation") or "").strip()
            if not annotation:
                missing.append(raw)
                continue
            try:
                stored = _place_in_catalog(config, source)
                key = rel_path(config, stored)
                existing = store.by_path(key)
                if existing and existing.sha256 == sha256_of(stored) and not args.force:
                    skipped.append({"path": key, "id": existing.id, "reason": "уже в базе, файл не менялся"})
                    continue
                parsed = parse_file(stored)
                digest = make_digest(config, stored, parsed)
                title = str(entry.get("title") or "").strip() or parsed.title
                doc = store.upsert(
                    rel_path=key,
                    title=title,
                    sections=parsed.sections,
                    toc=parsed.toc,
                    annotation=annotation,
                    lang=digest.lang,
                    fmt=stored.suffix.lower(),
                    sha256=sha256_of(stored),
                    mtime=stored.stat().st_mtime,
                )
                added.append(doc.to_dict())
            except ParseError as e:
                failed.append({"path": raw, "error": str(e)})
    finally:
        store.close()
    return {
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "missing_annotation": missing,
    }


def cmd_prune(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Убирает осиротевшие векторы/FTS-строки, оставшиеся от старых ошибок.

    Ничего не пересчитывает и эмбеддингов не трогает: удаляются только
    записи, которым в таблице chunks уже нет соответствия. Приносит
    пользу ровно один раз — после бага, из-за которого чанки удалялись
    минуя виртуальные таблицы; дальше команда просто ничего не находит.
    """
    store = Store(config)
    try:
        return store.prune()
    finally:
        store.close()


def cmd_reindex(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Перечитывает файлы каталога и пере-чанковывает их текущим парсером.

    Нужен после правки rag_parse.py: хеш файла не меняется, поэтому
    update и scan --apply не срабатывают, а содержимое в базе уже не
    соответствует тому, как файл разбирается сейчас. Аннотация и
    заголовок при этом сохраняются — перечитывается только текст.
    """
    store = Store(config)
    try:
        docs = store.documents()
        if args.paths:
            # Путь можно дать как есть (относительно текущего каталога)
            # и как он лежит в базе — относительно каталога документов:
            # список файлов печатает `list` именно в таком виде.
            known = {d.path for d in docs}
            wanted: set[str] = set()
            unknown: list[dict[str, str]] = []
            for raw in args.paths:
                try:
                    wanted.add(resolve_key(config, raw, known))
                except SystemExit:
                    unknown.append({"path": raw, "error": "нет такого документа в базе"})
            docs = [d for d in docs if d.path in wanted]
        else:
            unknown = []
        reindexed: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for doc in docs:
            file = config.catalog_dir / doc.path
            if not file.is_file():
                failed.append({"path": doc.path, "error": "файл не найден в каталоге"})
                continue
            try:
                parsed = parse_file(file)
                digest = make_digest(config, file, parsed)
                # Название по умолчанию не трогаем: его могли поправить вручную
                # (--title), и перезаписывать чужую правку нельзя. С --titles
                # название пересчитывается — так чинят авто-заголовки, когда
                # алгоритм в парсере уже исправили.
                new = store.upsert(
                    rel_path=doc.path,
                    title=parsed.title if args.titles else doc.title,
                    sections=parsed.sections,
                    toc=parsed.toc,
                    annotation=doc.annotation,
                    lang=digest.lang,
                    fmt=file.suffix.lower(),
                    sha256=sha256_of(file),
                    mtime=file.stat().st_mtime,
                )
            except ParseError as e:
                failed.append({"path": doc.path, "error": str(e)})
                continue
            reindexed.append({
                "path": doc.path,
                "title": new.title,
                "chunks_before": doc.n_chunks,
                "chunks_after": new.n_chunks,
            })
        return {"reindexed": reindexed, "failed": failed + unknown}
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Разбор аргументов
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag_tool.py",
        description="Инструмент RAG-хранилища: добавление, обновление, удаление, поиск.",
    )
    parser.add_argument("--config", help="путь к config.toml (по умолчанию — рядом со скриптом)")
    # default=SUPPRESS вместо default=True: иначе подкоманда (которая
    # парсится последней) затирает своим дефолтом значение, переданное
    # до её имени — «tool --no-json list» печатал бы JSON.
    parser.add_argument("--json", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help="вывод в JSON (по умолчанию); --no-json — человекочитаемый")
    # Общий префикс для подкоманд: иначе --no-json работал бы только перед
    # именем подкоманды, а писать «list --no-json» привычнее.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", parents=[common], help="разобрать файл и показать сводку (ничего не писать)")
    p.add_argument("path")

    p = sub.add_parser("add", parents=[common], help="добавить документ (аннотацию пишет модель сессии)")
    p.add_argument("path")
    p.add_argument("--annotation", default="", help="аннотация документа, 2-4 предложения")
    p.add_argument("--title", default="", help="название (по умолчанию — первый заголовок файла)")
    p.add_argument("--force", action="store_true", help="перезаписать, если уже есть")

    p = sub.add_parser("update", parents=[common], help="перечитать файл и переиндексировать, если изменился")
    p.add_argument("path")
    p.add_argument("--annotation", default=None, help="новая аннотация (по умолчанию — прежняя)")
    p.add_argument("--title", default="")
    p.add_argument("--force", action="store_true", help="переиндексировать, даже если хеш тот же")

    p = sub.add_parser("remove", parents=[common], help="удалить документ из базы (файл остаётся)")
    p.add_argument("target", help="путь относительно каталога или id")

    sub.add_parser("list", parents=[common], help="документы в базе")

    p = sub.add_parser("search", parents=[common], help="найти фрагменты (без ответа модели)")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)

    p = sub.add_parser("scan", parents=[common], help="сравнить каталог и базу")
    p.add_argument("--apply", action="store_true", help="переиндексировать изменённые файлы")

    p = sub.add_parser("plan", parents=[common],
                       help="собрать манифест по каталогу (ничего не пишет в базу)")
    p.add_argument("root", help="каталог с документами (обход рекурсивный)")
    p.add_argument("-o", "--output", default="", help="куда сохранить манифест (по умолчанию — stdout)")
    p.add_argument("--split", type=int, default=0,
                   help="нарезать манифест на N частей (для параллельной разметки)")
    p.add_argument("--head-chars", type=int, default=2500,
                   help="сколько символов начала текста класть в манифест")

    p = sub.add_parser("apply", parents=[common],
                       help="загрузить в базу документы из манифеста (по аннотациям)")
    p.add_argument("manifests", nargs="+", help="файлы манифеста (можно несколько)")
    p.add_argument("--force", action="store_true", help="перезаписать, если документ уже есть")

    p = sub.add_parser("reindex", parents=[common],
                       help="перечитать файлы каталога и пере-чанковать их (после правки парсера)")
    p.add_argument("paths", nargs="*", help="пути относительно каталога; без них — все документы")
    p.add_argument("--all", action="store_true", help="переиндексировать все документы базы")
    p.add_argument("--titles", action="store_true",
                   help="пересчитать и названия (по умолчанию названия сохраняются)")

    p = sub.add_parser("prune", parents=[common],
                       help="убрать осиротевшие векторы и FTS-строки без чанков")

    return parser


def print_human(data: dict[str, Any]) -> None:
    """Человекочитаемый вид для --no-json: коротко, без вложенных структур."""
    if "documents" in data:
        for doc in data["documents"]:
            print(f"[{doc['id']}] {doc['path']} — {doc['title']} ({doc['lang']}, чанков: {doc['chunks']})")
        stats = data.get("stats", {})
        print(f"итого: {stats.get('documents', 0)} документов, {stats.get('chunks', 0)} чанков")
    elif "hits" in data:
        for hit in data["hits"]:
            print(f"#{hit['rank']} {hit['path']} :: {hit['heading']} (score {hit['score']:.5f})")
            print(f"    {hit['text'][:120]}...")
    elif "new" in data:
        for key in ("new", "changed", "missing", "reindexed"):
            if data.get(key):
                print(f"{key}:")
                for item in data[key]:
                    print(f"  {item}")
        if not any(data.get(k) for k in ("new", "changed", "missing", "reindexed")):
            print("каталог и база совпадают")
    elif "updated" in data:
        doc = data["doc"]
        if data["updated"]:
            print(f"обновлён: [{doc['id']}] {doc['path']} — {doc['title']} (чанков: {doc['chunks']})")
        else:
            print(f"не тронут: {doc['path']} — {data['reason']}")
    elif "added" in data and isinstance(data["added"], dict):
        doc = data["added"]
        print(f"добавлен: [{doc['id']}] {doc['path']} — {doc['title']} (чанков: {doc['chunks']})")
    elif "orphan_vec" in data:
        print(f"удалено сирот: векторов {data['orphan_vec']}, FTS-строк {data['orphan_fts']}")
        if data.get("chunks_without_vec"):
            print(f"внимание: чанков без вектора {data['chunks_without_vec']} — их нужно переиндексировать (reindex)")
    elif "removed" in data:
        print(f"удалён: {data['removed']}")
    elif "title" in data:
        print(f"{data['title']} ({data['lang']}, {data['chars']} знаков, чанков: {data['chunks']})")
        for item in data.get("toc", []):
            print(f"  {'  ' * (item['level'] - 1)}{item['heading']}")
        if data.get("warning"):
            print(f"внимание: {data['warning']}")
    elif "reindexed" in data:
        for item in data["reindexed"]:
            print(f"пере-чанкован: {item['path']} — чанков {item['chunks_before']} → {item['chunks_after']}")
        for item in data.get("failed", []):
            print(f"ошибка: {item['path']} — {item['error']}")
    elif "added" in data and isinstance(data["added"], list):
        for doc in data["added"]:
            print(f"добавлен: [{doc['id']}] {doc['path']} — {doc['title']} (чанков: {doc['chunks']})")
        for item in data.get("skipped", []):
            print(f"пропущен: {item['path']} ({item['reason']})")
        for item in data.get("failed", []):
            print(f"ошибка: {item['path']} — {item['error']}")
        for item in data.get("missing_annotation", []):
            print(f"нет аннотации: {item}")
    elif "files" in data:
        print(f"каталог: {data.get('root', '?')}")
        for item in data["files"]:
            print(f"  {item['rel']} — {item['title']} ({item['chars']} знаков, чанков: {item['chunks']})")
        for part in data.get("parts", []):
            print(f"часть: {part}")
        for item in data.get("errors", []):
            print(f"ошибка: {item['path']} — {item['error']}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", True)  # SUPPRESS: флаг мог не передать ни один парсер
    try:
        config = load_config(args.config)
    except Exception as e:  # noqa: BLE001 — любая ошибка конфига должна быть понятной
        print(f"ошибка конфигурации: {e}", file=sys.stderr)
        return 2
    handlers = {
        "inspect": cmd_inspect,
        "add": cmd_add,
        "update": cmd_update,
        "remove": cmd_remove,
        "list": cmd_list,
        "search": cmd_search,
        "scan": cmd_scan,
        "plan": cmd_plan,
        "apply": cmd_apply,
        "reindex": cmd_reindex,
        "prune": cmd_prune,
    }
    try:
        result = handlers[args.command](config, args)
    except ParseError as e:
        print(f"не удалось разобрать файл: {e}", file=sys.stderr)
        return 1
    except SystemExit as e:
        if e.code and isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        raise
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_human(result)
    # Пакетные команды умеют частично провалиться: apply — из-за
    # недостающих аннотаций, reindex — из-за нечитаемых файлов. Код
    # возврата говорит вызывающему, что «всё прошло» неправда.
    if result.get("failed") or result.get("missing_annotation"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
