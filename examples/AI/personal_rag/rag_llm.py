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

    Ответ разбирается на две части: текст и вердикт «найдено / не
    найдено». Вердикт смотрится по цитатам — если модель не сослалась ни
    на один фрагмент, значит в фрагментах не было ответа. Дальше этот
    вердикт решает, показывать ли источники: к ответу «ничего не
    нашлось» список источников выглядит как подтверждение и вводит в
    заблуждение.

Температура низкая (0.2 из конфига): в задаче поиска по справочнику
креативность вредна, нужна точность и воспроизводимость.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import requests

from rag_config import Config
from rag_store import Hit, SearchResult


class LLMError(RuntimeError):
    """Модель недоступна или ответила ошибкой. Сообщение — для журнала."""


class OpenAIClient:
    """Минимальный клиент /chat/completions: без стриминга.

    Сетевых ретраев здесь сознательно нет: сервис и так отвечает быстро, а
    бесконечные повторы по дешёвой модели превращают сбой в очередь.
    Ошибка пробрасывается наверх и попадает в журнал сервиса.

    Исключение — обрезанный по длине ответ: для него сделан один
    ограниченный повтор с увеличенным бюджетом токенов (см. chat).
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> str:
        """Один запрос к модели; возвращает текст ответа.

        Модель из конфига — reasoning: сначала она тратит токены на
        размышление и только потом пишет ответ. Бюджет max_tokens при
        этом общий на обе фазы, поэтому при малом значении API отвечает
        finish_reason="length" и content=None — размышление съело токены,
        текста ответа не осталось.

        Такой ответ нельзя отдавать пустой строкой: вызывающий не отличит
        его от «модель не знает ответа» и покажет пользователю пустое
        поле с источниками. Поэтому обрезанный ответ — это один повтор с
        увеличенным бюджетом, а если и он не помог — LLMError, который
        оба вызывающих (сервис и MCP-инструмент) уже умеют показывать
        как ошибку.
        """
        if not self.config.api_key:
            raise LLMError(
                f"нет API-ключа: допишите RAG_API_KEY в {self.config.api_key_file.name} "
                "или выставьте переменную окружения RAG_API_KEY"
            )
        budget = max_tokens or self.config.max_tokens
        limit = self.config.max_tokens_limit
        # Не больше трёх попыток: исходная, удвоенная и максимальная.
        for attempt in range(3):
            payload: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": budget,
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
                choice = data["choices"][0]
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise LLMError(f"неожиданный формат ответа модели: {e}") from e

            finish_reason = choice.get("finish_reason")
            message = choice.get("message") or {}
            usage = data.get("usage") or {}

            if finish_reason == "length":
                # Размышление не влезло в бюджет — пробуем ещё раз с запасом.
                if attempt < 2 and budget < limit:
                    budget = min(budget * 2, limit)
                    continue
                raise LLMError(
                    f"модель обрезала ответ: не уложилась в {budget} токенов "
                    f"(finish_reason=length, usage={usage})"
                )

            refusal = message.get("refusal")
            if refusal:
                raise LLMError(f"модель отказалась отвечать: {refusal}")

            content = message.get("content")
            if content is None or not str(content).strip():
                raise LLMError(
                    f"модель вернула пустой ответ "
                    f"(finish_reason={finish_reason!r}, usage={usage})"
                )
            return str(content).strip()
        # Цикл всегда либо возвращает текст, либо бросает LLMError.
        raise LLMError("не удалось получить ответ модели")


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
  ответа на этот вопрос», и не выдумывай. В этом случае источники
  не указывай: цитата без ответа только вводит в заблуждение.
- Если фрагменты отвечают не на тот вопрос, который задан, — скажи об
  этом прямо, а не подгоняй их под вопрос.
- Отвечай на языке вопроса.
- Коротко: 2-5 предложений. Если вопрос — о процедуре, дай шаги.
"""

# Ответ без цитат — это ответ «в документах нет ответа»: модель получила
# фрагменты, но ни один не подошёл. Цитаты в таком случае только вредят.
_CITATION_RE = re.compile(r"\[\d+\]")

# Формулировки «ответа нет», которые модель использует по инструкции.
_NOT_FOUND_MARKERS = (
    "не нашлось ответа",
    "не нашлось ничего",
    "нет ответа",
    "не найдено",
    "ответ не найден",
)


def looks_like_not_found(text: str) -> bool:
    """Похоже ли, что модель ответила «в документах этого нет»."""
    low = text.lower()
    return any(marker in low for marker in _NOT_FOUND_MARKERS)


def build_prompt(
    query: str,
    hits: list[Hit],
    missing: list[str] | None = None,
    *,
    missing_is_key: bool = False,
) -> list[dict[str, str]]:
    """Собирает сообщение: инструкция сверху, фрагменты с номерами ниже.

    Фрагменты отдаются с заголовком и путём к файлу — модель цитирует не
    просто номер, а может пересказать, откуда информация. Это стоит
    одного лишнего блока в промпте: без пути пользователь не может
    проверить источник.

    Если каких-то слов запроса в каталоге нет, они перечисляются в
    примечании. Это важно: поиск по леммам идёт через OR, поэтому запрос
    «квантовые вычисления» находит фрагменты по слову «вычисления» — про
    распределённые вычисления, а не про квантовые. Без примечания модель
    может не заметить подмену темы и ответить уверенно не о том.
    """
    blocks = []
    for hit in hits:
        source = f"{hit.path} :: {hit.heading}" if hit.heading else hit.path
        blocks.append(f"[{hit.rank}] {source}\n{hit.text}")
    context = "\n\n---\n\n".join(blocks)
    note = ""
    if missing:
        listed = ", ".join(f"«{w}»" for w in missing)
        note = f"\n\nПримечание: в каталоге нет слов: {listed}."
        if missing_is_key:
            note += (
                " Учти это, когда отвечаешь: если вопрос именно про эти слова — "
                "честно скажи, что в документах этого нет, и источники не указывай."
            )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Фрагменты:\n\n{context}\n\nВопрос: {query}{note}"},
    ]


@dataclass
class Answer:
    """Ответ модели плюс фрагменты, на которых он построен.

    found — нашлось ли в документах то, о чём спросили. Если нет, ответ
    честно говорит об этом, а источники не показываются: список «источников»
    к ответу «ничего не нашлось» выглядит как подтверждение ответа и
    вводит в заблуждение.
    """

    question: str
    text: str
    hits: list[Hit]
    model: str
    found: bool = True
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "question": self.question,
            "answer": self.text,
            "model": self.model,
            "found": self.found,
        }
        if self.missing:
            data["missing_terms"] = self.missing
            data["note"] = (
                "В каталоге не встречаются слова: "
                + ", ".join(f"«{w}»" for w in self.missing)
                + "."
            )
        data["sources"] = (
            [
                {
                    "n": h.rank,
                    "path": h.path,
                    "title": h.title,
                    "heading": h.heading,
                    "score": round(h.score, 6),
                }
                for h in self.hits
            ]
            if self.found
            else []
        )
        return data


def compose_answer(client: OpenAIClient, query: str, result: SearchResult) -> Answer:
    """Просит модель ответить по найденным фрагментам.

    Фрагменты уже найдены снаружи (store.search). Помимо них модель
    получает примечание о словах запроса, которых нет в каталоге, — иначе
    она не заметит, что найденные фрагменты про другую тему.
    """
    hits = result.hits
    missing = result.terms.missing
    if not hits:
        return Answer(
            question=query,
            text="В документах не нашлось ничего похожего на этот вопрос.",
            hits=[],
            model=client.config.model,
            found=False,
            missing=missing,
        )
    messages = build_prompt(query, hits, missing, missing_is_key=result.not_found)
    text = client.chat(messages)
    # Ответ без цитат — модель не нашла в фрагментах ответа на вопрос.
    found = bool(_CITATION_RE.search(text)) and not looks_like_not_found(text)
    return Answer(
        question=query,
        text=text,
        hits=hits,
        model=client.config.model,
        found=found,
        missing=missing,
    )
