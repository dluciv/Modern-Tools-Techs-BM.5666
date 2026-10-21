#!/usr/bin/env -S uv run --quiet --script --
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "flask>=3.1",
#     "fastembed>=0.6",
#     "sqlite-vec>=0.1.6",
#     "pymorphy3>=2.0.1",
#     "snowballstemmer>=3.0",
#     "requests>=2.34",
# ]
# ///
"""
Веб-сервис поиска по документам (RAG) для вызова через curl или MCP.

Два способа спросить:

    curl 'http://127.0.0.1:8081/search?q=зачем+нужен+чанкинг'
    curl 'http://127.0.0.1:8081/ask?q=как+слить+результаты+двух+поисков'

/search отдаёт сырые фрагменты (кто нашёл, с каким весом, откуда) —
пригодится, чтобы понять, почему ответ получился таким. /ask прогоняет
фрагменты через дешёвую модель из конфига и возвращает готовый ответ
с источниками.

Важно про модель: здесь используется НЕ модель сессии OpenCode, а
дешёвая модель из config.toml — поиск должен быть дешёвым. Модель
нужна только для /ask; /search работает вообще без сети (эмбеддинги
локальные).

Запуск:
    ./rag_service.py                    # порт из конфига или 8081
    ./rag_service.py --port 9000
    ./rag_service.py --host 0.0.0.0     # слушать вне localhost
    ./rag_service.py --no-log           # не писать журнал

Журнал по умолчанию печатает короткие строки в терминал (как в агенте и
Элизе), а при log_file в конфиге — ещё и пишет трассировку в файл.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).parent))

from rag_config import load_config
from rag_llm import LLMError, OpenAIClient, compose_answer
from rag_store import Store

# --------------------------------------------------------------------------- #
# Журнал: короткие строки в терминал, трассировка в файл
# --------------------------------------------------------------------------- #

class Journal:
    """Журнал сервиса: пересказ в терминал, трассировка в файл.

    В терминал идёт одна строка на запрос — оператору нужно видеть, что
    сервис жив и сколько времени отвечает. В файл — JSON по одной строке
    на событие: по нему можно разобрать, что именно было найдено и сколько
    стоил вызов модели. Печать с flush=True: при перенаправлении вывода
    в файл stdout буферизуется, и журнал появился бы только после завершения.
    """

    def __init__(self, log_file: Path | None, enabled: bool = True) -> None:
        self.enabled = enabled
        self.log_file = log_file
        self._fh: Any = None
        if enabled and log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._fh = log_file.open("a", encoding="utf-8")

    def event(self, tag: str, message: str, **fields: Any) -> None:
        if not self.enabled:
            return
        print(f"[rag {tag}] {message}", flush=True)
        if self._fh is not None:
            record = {"time": time.time(), "tag": tag, "message": message, **fields}
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #

def create_app(config: Any, journal: Journal) -> Flask:
    """Собирает Flask-приложение. Фабрика — чтобы сервис можно было
    поднять в тестах без порта и без сети."""
    app = Flask(__name__)
    store = Store(config)
    llm = OpenAIClient(config)
    started = time.time()

    @app.get("/health")
    def health() -> Any:
        stats = store.stats()
        return jsonify(
            {
                "status": "ok",
                "uptime_sec": round(time.time() - started, 1),
                **stats,
                "llm": {"base_url": config.base_url, "model": config.model, "key": "есть" if config.api_key else "НЕТ"},
            }
        )

    @app.route("/search", methods=["GET", "POST"])
    def search() -> Any:
        query, k = _query_args()
        t0 = time.time()
        result = store.search(query, k=k)
        elapsed = time.time() - t0
        hits = result.hits
        journal.event(
            "→",
            f"search {query!r} (k={k})",
            query=query, k=k, hits=len(hits), elapsed_ms=round(elapsed * 1000, 1),
        )
        for hit in hits:
            journal.event(
                "·",
                f"  #{hit.rank} {hit.path} :: {hit.heading} (score {hit.score:.5f}, vec={hit.vec_rank}, bm25={hit.bm25_rank})",
                doc_id=hit.doc_id, score=hit.score,
            )
        if result.not_found:
            journal.event(
                "⚠",
                f"темы нет в каталоге, нет слов: {', '.join(result.terms.missing)}",
                query=query, missing=result.terms.missing,
            )
        return jsonify(
            {
                "query": query,
                "elapsed_ms": round(elapsed * 1000, 1),
                "not_found": result.not_found,
                "missing_terms": result.terms.missing,
                "matched_terms": result.matched_terms,
                "note": result.note,
                "hits": [h.to_dict() for h in hits],
            }
        )

    @app.route("/ask", methods=["GET", "POST"])
    def ask() -> Any:
        query, k = _query_args()
        t0 = time.time()
        result = store.search(query, k=k)
        hits = result.hits
        search_ms = round((time.time() - t0) * 1000, 1)
        journal.event("→", f"ask {query!r} (k={k}, найдено {len(hits)})", query=query, hits=len(hits))
        try:
            answer = compose_answer(llm, query, result)
        except LLMError as e:
            # Ошибка модели — не ошибка поиска: фрагменты уже найдены,
            # и мы отдаём их с кодом 502, чтобы вызывающий увидел и то,
            # и другое.
            journal.event("⚠", f"модель: {e}")
            return jsonify({"error": str(e), "hits": [h.to_dict() for h in hits]}), 502
        if answer.found:
            outcome = f"источников {len(hits)}"
        else:
            outcome = "НЕ НАЙДЕНО, источники скрыты"
        journal.event(
            "←",
            f"ответ {len(answer.text)} знаков, {outcome} ({search_ms} мс поиск)",
            model=answer.model, answer_chars=len(answer.text), found=answer.found,
        )
        payload = answer.to_dict()
        payload["search_ms"] = search_ms
        return jsonify(payload)

    @app.get("/documents")
    def documents() -> Any:
        docs = store.documents()
        return jsonify({"documents": [d.to_dict() for d in docs]})

    @app.errorhandler(404)
    def not_found(_e: Any) -> Any:
        return jsonify({"error": "нет такого маршрута: /health, /search, /ask, /documents"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_e: Any) -> Any:
        return jsonify({"error": "метод не поддерживается: /search и /ask принимают GET и POST"}), 405

    @app.errorhandler(BadRequest)
    def bad_request(e: BadRequest) -> Any:
        # Без этого обработчика Flask вернул бы 500 с HTML: BadRequest —
        # наш класс-исключение, а не HTTPException, и по умолчанию он
        # считается сбоем сервера, хотя вина на стороне клиента.
        journal.event("⚠", f"плохой запрос: {e}")
        return jsonify({"error": str(e)}), 400

    return app


def _query_args() -> tuple[str, int]:
    """Достаёт запрос и k из query-строки или JSON-тела — оба вида
    одинаково работают через curl, второй удобнее программам."""
    if request.method == "POST" and request.is_json:
        data = request.get_json(silent=True) or {}
        query = str(data.get("q", data.get("query", ""))).strip()
        k = int(data.get("k", 5))
    else:
        query = str(request.args.get("q", request.args.get("query", ""))).strip()
        k = int(request.args.get("k", 5))
    if not query:
        raise BadRequest("нужен параметр q — что искать")
    return query, max(1, min(k, 50))


class BadRequest(ValueError):
    """Плохой запрос от клиента: текст сообщения уйдёт в ответ как есть."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag_service.py", description="RAG-поиск: HTTP + дешёвая модель")
    parser.add_argument("--config", help="путь к config.toml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="по умолчанию — из конфига, иначе 8081")
    parser.add_argument("--no-log", action="store_true", help="выключить журнал")
    parser.add_argument("--log-file", default=None, help="файл трассировки (по умолчанию — из конфига)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.log_file is not None:
        config.log_file = Path(os.path.expanduser(args.log_file)) if args.log_file else None

    journal = Journal(config.log_file, enabled=not args.no_log)
    app = create_app(config, journal)
    port = args.port or int(os.environ.get("RAG_PORT", "8081"))

    journal.event("·", f"запуск на http://{args.host}:{port} (база: {config.store_dir})")
    journal.event("·", f"модель для /ask: {config.model} @ {config.base_url}")
    try:
        # threaded=False: Store — один экземпляр на процесс, а sqlite-соединение
        # и модель эмбеддингов не рассчитаны на параллельные вызовы из разных
        # потоков. Для справочного сервиса одного потока хватает.
        app.run(host=args.host, port=port, threaded=False)
    except KeyboardInterrupt:
        pass
    finally:
        stats = Store(config).stats()
        journal.event("·", f"остановлено; в базе {stats['documents']} документов и {stats['chunks']} чанков")
        journal.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BadRequest as e:
        print(f"ошибка запроса: {e}", file=sys.stderr)
        raise SystemExit(2) from None
