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
from rag_parse import ParseError, parse_file
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
    «extra/zayavki.md» превратился бы в абсолютный путь к чужому файлу.
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
    """
    source = lexical_abs(path)
    catalog = config.catalog_dir
    if source == catalog or catalog in source.parents:
        return source  # уже внутри каталога — в том числе за симлинком
    target = catalog / source.name
    if target.exists():
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
    key: str | int = int(args.target) if str(args.target).isdigit() else rel_path(config, Path(args.target).expanduser())
    store = Store(config)
    try:
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
    elif "added" in data:
        doc = data["added"]
        print(f"добавлен: [{doc['id']}] {doc['path']} — {doc['title']} (чанков: {doc['chunks']})")
    elif "removed" in data:
        print(f"удалён: {data['removed']}")
    elif "title" in data:
        print(f"{data['title']} ({data['lang']}, {data['chars']} знаков, чанков: {data['chunks']})")
        for item in data.get("toc", []):
            print(f"  {'  ' * (item['level'] - 1)}{item['heading']}")
        if data.get("warning"):
            print(f"внимание: {data['warning']}")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
