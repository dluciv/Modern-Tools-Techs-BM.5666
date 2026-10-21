#!/usr/bin/env -S uv run --quiet --script --
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "mcp>=1.9,<2",
#     "fastembed>=0.6",
#     "sqlite-vec>=0.1.6",
#     "pymorphy3>=2.0.1",
#     "snowballstemmer>=3.0",
#     "requests>=2.34",
# ]
# ///
"""
MCP-сервер поверх того же ядра поиска, что и веб-сервис.

Ключевая мысль примера: MCP — это просто ещё один транспорт. Ни
хранилища, ни промпты, ни модель здесь не изменились: те же rag_store и
rag_llm, что использует curl-сервис. Разница только во входе/выходе:
stdio вместо HTTP, именованные инструменты вместо маршрутов.

Подключение к OpenCode (в opencode.json или глобально):

    "mcp": {
      "servers": {
        "personal-rag": {
          "type": "local",
          "command": ["uv", "run", "--script",
                      "/путь/к/examples/AI/personal_rag/rag_mcp.py"]
        }
      }
    }

Инструменты:
    search(query, k=5)  — сырые фрагменты, как /search
    ask(question, k=5)  — ответ дешёвой модели с источниками, как /ask
    documents()         — список документов в базе
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))

from rag_config import load_config
from rag_llm import LLMError, OpenAIClient, compose_answer
from rag_store import Store

# Один экземпляр на процесс: Store держит sqlite-соединение, а OpenAIClient —
# конфиг. Оба создаются лениво при первом вызове инструмента, чтобы запуск
# MCP-сервера (который OpenCode делает на старте сессии) не стоил загрузки
# модели эмбеддингов.
config = load_config(None)
_store: Store | None = None
_llm: OpenAIClient | None = None

mcp = FastMCP(
    "personal-rag",
    instructions=(
        "Поиск по локальному каталогу документов (RU/EN). "
        "search возвращает фрагменты с путями к файлам, "
        "ask — готовый ответ дешёвой модели со ссылками на источники."
    ),
)


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(config)
    return _store


def llm() -> OpenAIClient:
    global _llm
    if _llm is None:
        _llm = OpenAIClient(config)
    return _llm


@mcp.tool()
def search(query: str, k: int = 5) -> str:
    """Найти фрагменты документов по запросу (гибридно: векторы + BM25 по леммам).

    Возвращает JSON: путь к файлу, заголовок раздела, текст фрагмента и оценку.
    """
    hits = store().search(query, k=k)
    return json.dumps(
        {"query": query, "hits": [h.to_dict() for h in hits]}, ensure_ascii=False, indent=2
    )


@mcp.tool()
def ask(question: str, k: int = 5) -> str:
    """Ответить на вопрос по документам: найдёт фрагменты и спросит дешёвую модель.

    Возвращает JSON с answer и sources (номера цитат соответствуют фрагментам).
    """
    hits = store().search(question, k=k)
    try:
        answer = compose_answer(llm(), question, hits)
    except LLMError as e:
        # Модель может быть не настроена — фрагменты всё равно отдаём,
        # чтобы поиск оставался полезным.
        return json.dumps(
            {"error": str(e), "hits": [h.to_dict() for h in hits]}, ensure_ascii=False, indent=2
        )
    return json.dumps(answer.to_dict(), ensure_ascii=False, indent=2)


@mcp.tool()
def documents() -> str:
    """Список документов в базе: путь, название, язык, число чанков."""
    stats: dict[str, Any] = store().stats()
    docs = [d.to_dict() for d in store().documents()]
    return json.dumps({"stats": stats, "documents": docs}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # transport="stdio": сервер общается с OpenCode по стандартному вводу/
    # выводу — никакого порта и никакой настройки файрвола.
    mcp.run(transport="stdio")
