"""
Клиент дешёвой модели и сборка ответа по найденным фрагментам.

Здесь два разных слоя, которые легко перепутать:

  * OpenAIClient — тонкая обёртка над OpenAI-совместимым API
    (/chat/completions). Модель для него берётся из конфига — это
    сознательно НЕ модель сессии OpenCode: поиск должен быть дешёвым,
    поэтому туда ставится недорогая и простая модель.

  * compose_answer — промпт и формат ответа. Модели сообщают правила:
    отвечать только по фрагментам, цитировать источники как [1], [2],
    при отсутствии данных честно говорить об этом. Это самая важная
    часть «RAG»: без жёсткого промпта даже хорошая модель начнёт
    додумывать.

Температура низкая (0.2 из конфига): в задаче поиска по справочнику
креативность вредна, нужна точность и воспроизводимость.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from rag_config import Config
from rag_store import Hit


class LLMError(RuntimeError):
    """Модель недоступна или ответила ошибкой. Сообщение — для журнала."""


class OpenAIClient:
    """Минимальный клиент /chat/completions: без ретраев, без стриминга.

    Ретраи здесь сознательно не нужны: сервис и так отвечает быстро, а
    бесконечные повторы по дешёвой модели превращают сбой в очередь.
    Ошибка пробрасывается наверх и попадает в журнал сервиса.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> str:
        """Один запрос к модели; возвращает текст ответа."""
        if not self.config.api_key:
            raise LLMError(
                f"нет API-ключа: допишите RAG_API_KEY в {self.config.api_key_file.name} "
                "или выставьте переменную окружения RAG_API_KEY"
            )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        try:
            response = requests.post(
                f"{self.config.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=payload,
                timeout=self.config.timeout,
            )
        except requests.RequestException as e:
            raise LLMError(f"модель недоступна ({self.config.base_url}): {e}") from e
        if response.status_code != 200:
            raise LLMError(
                f"модель ответила {response.status_code}: {response.text[:200]}"
            )
        try:
            data = response.json()
            return str(data["choices"][0]["message"]["content"] or "").strip()
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError(f"неожиданный формат ответа модели: {e}") from e


# --------------------------------------------------------------------------- #
# Сборка ответа
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
Ты — ассистент по внутренним документам. Отвечай ТОЛЬКО по переданным
фрагментам, ничего не додумывая.

Правила:
- Цитируй источник номером в квадратных скобках: [1], [2]. Номера даны
  во фрагментах ниже.
- Если в фрагментах нет ответа — так и скажи: «В документах не нашлось
  ответа на этот вопрос», и не выдумывай.
- Отвечай на языке вопроса.
- Коротко: 2-5 предложений. Если вопрос — о процедуре, дай шаги.
"""


def build_prompt(query: str, hits: list[Hit]) -> list[dict[str, str]]:
    """Собирает сообщение: инструкция сверху, фрагменты с номерами ниже.

    Фрагменты отдаются с заголовком и путём к файлу — модель цитирует не
    просто номер, а может пересказать, откуда информация. Это стоит
    одного лишнего блока в промпте: без пути пользователь не может
    проверить источник.
    """
    blocks = []
    for hit in hits:
        source = f"{hit.path} :: {hit.heading}" if hit.heading else hit.path
        blocks.append(f"[{hit.rank}] {source}\n{hit.text}")
    context = "\n\n---\n\n".join(blocks)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Фрагменты:\n\n{context}\n\nВопрос: {query}"},
    ]


@dataclass
class Answer:
    """Ответ модели плюс фрагменты, на которых он построен."""

    question: str
    text: str
    hits: list[Hit]
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.text,
            "model": self.model,
            "sources": [
                {
                    "n": h.rank,
                    "path": h.path,
                    "title": h.title,
                    "heading": h.heading,
                    "score": round(h.score, 6),
                }
                for h in self.hits
            ],
        }


def compose_answer(client: OpenAIClient, query: str, hits: list[Hit]) -> Answer:
    """Ищет фрагменты (уже найдены снаружи) и просит модель ответить по ним."""
    if not hits:
        return Answer(
            question=query,
            text="В документах не нашлось ничего похожего на этот вопрос.",
            hits=[],
            model=client.config.model,
        )
    messages = build_prompt(query, hits)
    return Answer(question=query, text=client.chat(messages), hits=hits, model=client.config.model)
