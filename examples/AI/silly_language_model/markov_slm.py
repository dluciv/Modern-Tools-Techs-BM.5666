#!/usr/bin/env -S uv run --quiet --script --
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "flask>=3.1.3",
# ]
# ///
"""
Silly Language Model — OpenAI-совместимый сервер на цепи Маркова.

Этот файл отвечает только за протокол: принимает POST /v1/chat/completions
и отдаёт JSON или SSE-поток в формате, который понимают OpenCode, агент
и любой другой OpenAI-клиент.

Поддерживаются параметры настоящих моделей: max_tokens, temperature
и stream. Но не стоит обманываться: поддержка параметра не делает
модель умной. Температура здесь почти ни на что не влияет — см.
комментарий к DEFAULT_TEMPERATURE в markov_chain.py.

Сам «мозг» — в markov_chain.py, работа с текстом и корпусом — в slm_corpus.py.

Запуск:  uv run --script markov_slm.py
"""

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

from flask import Flask, Response, jsonify, request

from markov_chain import DEFAULT_ORDER, DEFAULT_TEMPERATURE, ImprovedMarkovChain
from slm_corpus import load_corpus

app = Flask(__name__)

MODEL_NAME = "markov-chain-v1"

# Обучение идёт при импорте модуля: сервер поднимается примерно
# через 25 секунд, зато отвечает мгновенно
model = ImprovedMarkovChain(load_corpus(), order=DEFAULT_ORDER)


def generate_streaming_response(
    request_id: str,
    created_time: int,
    model_name: str,
    content: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> Iterator[str]:
    """Генератор стримингового ответа в формате SSE (Server-Sent Events)."""

    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        return {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created_time,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

    # Первый чанк по спецификации OpenAI несёт только роль,
    # без символов ответа — так клиент знает, что начался поток
    yield f"data: {json.dumps(chunk({'role': 'assistant', 'content': ''}), ensure_ascii=False)}\n\n"

    # Разбиваем на слова — так стриминг выглядит естественнее
    for index, word in enumerate(content.split()):
        token = word if index == 0 else " " + word
        yield f"data: {json.dumps(chunk({'content': token}), ensure_ascii=False)}\n\n"
        time.sleep(0.03)  # имитация «раздумий» модели

    # Финальный чанк с finish_reason — это критично для агентов!
    final_chunk = chunk({}, "stop")
    # usage по спеке приходит в последнем чанке потока, иначе клиент
    # (в том числе OpenCode) показывает нули вместо расхода токенов
    final_chunk["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }

    yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def read_number(
    data: dict[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Читает числовое поле запроса, терпимо к мусору в нём.

    Клиент может прислать строку ("0.7"), None или вообще отправить
    не число — тогда берём значение по умолчанию, а не падаем.
    Диапазон, как у OpenAI, зажимаем сами вместо ошибки 400.
    """
    raw = data.get(key)
    if raw is None:
        return default

    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default

    return min(maximum, max(minimum, value))


def request_error(message: str) -> tuple[Any, int]:
    """Ошибка в формате OpenAI — JSON, а не HTML-страница Flask."""
    return (
        jsonify(
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "code": None,
                }
            }
        ),
        400,
    )


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions() -> Any:
    # request.get_json падает с 400, если тело битое или пустое, и Flask
    # отдаёт HTML-страницу. Клиент OpenAI ждёт JSON с полем error,
    # поэтому аккуратно ловим оба случая.
    try:
        data = request.get_json(force=False, silent=True)
    except Exception:  # noqa: BLE001 — приводим к формату OpenAI, см. request_error
        data = None

    if not isinstance(data, dict):
        return request_error("Expected a JSON object in the request body")

    messages = data.get("messages") or []
    if not isinstance(messages, list):
        return request_error("'messages' must be a list")

    user_message = next(
        (
            message["content"]
            for message in reversed(messages)
            if isinstance(message, dict)
            and message.get("role") == "user"
            and isinstance(message.get("content"), str)
        ),
        "",
    )

    # Приводим к целому: клиент может прислать строку, а range() её не берёт
    max_tokens = int(read_number(data, "max_tokens", 100, 1, 100_000))

    # Температура — как у настоящих моделей: 0 = жадный выбор,
    # больше 1 = площе распределение. На этой цепи параметр почти
    # не влияет на результат: 93% состояний имеют единственный переход.
    temperature = read_number(data, "temperature", DEFAULT_TEMPERATURE, 0.0, 2.0)

    response_text = model.respond(
        user_message, max_tokens=max_tokens, temperature=temperature
    )

    request_id = f"chatcmpl-{uuid.uuid4()}"
    created_time = int(time.time())

    # Считаем токены так же, как в обычном ответе, — иначе в потоке
    # расход будет нулевым
    prompt_tokens = sum(
        len(message["content"].split())
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), str)
    )
    completion_tokens = len(response_text.split())

    # Стриминговый ответ
    if data.get("stream", False):
        return Response(
            generate_streaming_response(
                request_id,
                created_time,
                MODEL_NAME,
                response_text,
                prompt_tokens,
                completion_tokens,
            ),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # для nginx
            },
        )

    # Обычный ответ
    return jsonify(
        {
            "id": request_id,
            "object": "chat.completion",
            "created": created_time,
            "model": MODEL_NAME,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text,
                    },
                    "finish_reason": "stop",  # было "length" — это ломает агентов
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


@app.route("/v1/models", methods=["GET"])
def list_models() -> Any:
    return jsonify(
        {
            "object": "list",
            "data": [
                {
                    "id": MODEL_NAME,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "student-demo",
                }
            ],
        }
    )


if __name__ == "__main__":
    # debug=True поднимает интерактивный отладчик, доступный любому,
    # кто дотянулся до порта. Для локальной демки это лишний риск:
    # включайте только на своей машине и только на время отладки.
    app.run(port=8000, debug=False)