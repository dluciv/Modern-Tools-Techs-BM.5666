#!/usr/bin/env -S uv run --quiet --script --
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "flask>=3.1.3",
# ]
# ///
"""
Silly Language Model — OpenAI-совместимый сервер на правилах Элизы.

Этот файл отвечает только за протокол: принимает POST /v1/chat/completions
и отдаёт JSON или SSE-поток в формате, который понимают OpenCode, агент
и любой другой OpenAI-клиент. Ровно тот же протокол, что у сервера на
цепи Маркова из соседней папки, — поэтому модели можно менять местами,
не трогая ни клиента, ни агента.

Поддерживаются параметры настоящих моделей: max_tokens, temperature
и stream. И здесь они работают не декоративно, а по-настоящему:
temperature выбирает между ответами разных правил, max_tokens
обрезает реплику по числу слов. Но стоит помнить, что Элиза умнее
цепи Маркова не потому, что знает больше, а потому, что отвечает
связно: она задаёт вопрос, а не выдаёт случайные слова.

Сам «мозг» — в eliza.py: ключевые слова, таблица отражений и спряжения.
Журналы обмена — в eliza_log.py. Оба включены по умолчанию, как в агенте:

  * в терминал печатается пересказ: что прислал клиент, как Элиза поняла
    реплику (какое правило сработало, какие были варианты ответа и с какими
    вероятностями выбран именно этот) и что она ответила;
  * в eliza_trace.log пишется полный протокол: тела запросов и ответов
    целиком вместе с заголовками и статусом, а для потокового ответа —
    все куски SSE так, как они ушли в сокет.

Тело каждого запроса повторяется целиком, вместе со всей историей
диалога: так видно, что клиент шлёт модели весь контекст, и именно
поэтому диалог дорожает с каждым шагом по инструментам.

Запуск:
    uv run --script eliza_slm.py                      # порт 8000, журналы включены
    uv run --script eliza_slm.py --port 8010
    uv run --script eliza_slm.py --no-log             # убрать журналы совсем
    uv run --script eliza_slm.py --log-file run.log   # трассировка в другой файл
"""

import argparse
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException

from eliza import DEFAULT_TEMPERATURE, Eliza, plural
from eliza_log import ModelLog, ModelTrace

app = Flask(__name__)

MODEL_NAME = "eliza-v1"
DEFAULT_PORT = 8000
DEFAULT_LOG_FILE = "eliza_trace.log"

# Объект создаётся мгновенно: обучать нечего, все знания Элизы — это
# таблица правил в исходном тексте. Сервер поднимается за доли секунды.
#
# Состояние нужно между запросами: Элиза помнит последнюю реплику,
# чтобы не повторяться. У настоящих моделей за это отвечает контекст
# диалога, который присылает клиент, — здесь история живёт на сервере,
# поэтому один инстанс модели лучше не шарить между сессиями.
model = Eliza()

# Журналы живут на уровне модуля: сервер один, значит и журнал один, а его
# счётчик запросов должен совпадать с номером блока в файле. Файл
# трассировки создаётся лениво, при первом обращении (см. eliza_log.py),
# поэтому импорт модуля ничего не пишет на диск.
log = ModelLog()
trace = ModelTrace(path=Path(DEFAULT_LOG_FILE))


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


def request_error(message: str, status: int = 400) -> tuple[Any, int]:
    """Ошибка в формате OpenAI — JSON, а не HTML-страница Flask."""
    return (
        jsonify(
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error" if status < 500 else "server_error",
                    "code": None,
                }
            }
        ),
        status,
    )


def describe_params(
    data: dict[str, Any],
    messages: list[Any],
    max_tokens: int,
    temperature: float,
) -> str:
    """Строка «что именно настроил клиент» — для журнала.

    Отдельная функция, а не строка в обработчике, потому что её читает и
    консольный журнал, и файл трассировки.
    """
    tools = data.get("tools")
    return (
        f"{plural(len(messages), 'сообщение', 'сообщения', 'сообщений')}"
        f" · инструментов: {len(tools) if isinstance(tools, list) else 0}"
        f" · temperature {temperature:.2f}"
        f" · max_tokens {max_tokens}"
        f" · поток: {'да' if data.get('stream', False) else 'нет'}"
    )


# --------------------------------------------------------------------------- #
# Журналы: что пришло от клиента и что ушло в ответ
# --------------------------------------------------------------------------- #


@app.before_request
def log_request() -> None:
    """Открывает запись в журнале — всё, что пришло по HTTP.

    Запрос логируется здесь, а не в обработчике, по трём причинам:
    битое тело тоже попадает в журнал, служебные маршруты (например,
    /v1/models) не забываются, и нумерация запросов совпадает с файлом.

    Тело читаем один раз и кэшируем: дальше из этого же кэша
    request.get_json() разберёт его в обработчике.
    """
    g.started = time.monotonic()
    g.reply = ""
    g.reason = ""
    g.finish_reason = ""
    g.usage = None
    # Флаг ставит обработчик, когда отдаёт ответ потоком. Проверять
    # response.is_streamed нельзя: werkzeug так же отдаёт потоком свои
    # HTML-страницы ошибок, и журнал решил бы, что клиент получил SSE.
    g.streamed = False

    body = request.get_data(as_text=True)
    log.request(request.method, request.path, len(body.encode("utf-8")))
    trace.request(
        method=request.method,
        path=request.path,
        headers=dict(request.headers),
        body=body,
    )


@app.after_request
def log_response(response: Response) -> Response:
    """Дописывает в журнал то, что ушло клиенту: статус, время, тело."""
    elapsed_ms = (time.monotonic() - g.get("started", time.monotonic())) * 1000
    usage: tuple[int, int] | None = g.get("usage")
    reason: str = g.get("reason", "")

    if g.get("streamed"):
        # Тело потока собирается по мере генерации и целиком уже не
        # доступно — его копит traced_stream(). Здесь только отметка,
        # что ответ ушёл потоком, а сам поток записан ниже.
        trace.event(f"ответ {response.status_code}, тело отдаётся потоком SSE")
        return response

    body = response.get_data(as_text=True)

    # В консоль идёт ответ модели, а не JSON-обёртка: для пересказа
    # важно что сказала Элиза. Если отвечала не модель (404, 405, 400),
    # показываем короткую отметку — целиком тело уже в файле трассировки.
    content = g.get("reply") or (
        "(HTML-страница ошибки — целиком в файле трассировки)"
        if response.status_code >= 400 and body.lstrip().startswith(("<!doctype", "<html"))
        else body
    )

    log.response(
        content=content,
        status=response.status_code,
        elapsed_ms=elapsed_ms,
        finish_reason=g.get("finish_reason", ""),
        reason=reason,
        usage=usage,
    )
    trace.response(status=response.status_code, elapsed_ms=elapsed_ms, body=body)
    return response


@app.errorhandler(HTTPException)
def log_http_exception(exc: HTTPException) -> Any:
    """Ошибки протокола (404, 405, 415) — тоже в журнал.

    Отдаём стандартный ответ Flask: клиенту нужен его статус, а не
    наша версия в формате OpenAI. Зато в журнале видно, что запрос был,
    и какой код ушёл в ответ.
    """
    log.error(f"HTTP {exc.code}: {exc.description}")
    return exc


@app.errorhandler(Exception)
def log_unexpected(exc: Exception) -> Any:
    """Необработанное исключение: traceback — в stderr, JSON — клиенту.

    Flask на такое исключение отдаёт HTML-страницу, а клиент (агент,
    opencode, curl) ждёт JSON с полем error. И заодно это единственный
    способ узнать, что запрос всё-таки упал: для таких ответов
    after_request не вызывается, и в трассировке осталась бы запись
    запроса без ответа.
    """
    # Полный traceback уходит через логгер Flask: строка «ValueError: …»
    # говорит, что сломалось, но не говорит где — а искать потом неудобно.
    app.logger.exception("необработанная ошибка при обработке запроса: %s", exc)

    detail = f"{type(exc).__name__}: {exc}"
    log.error(detail)
    trace.failure(detail)
    return request_error(str(exc) or detail, status=500)


def traced_stream(
    source: Iterator[str],
    reply: str,
    reason: str,
    finish_reason: str,
    usage: tuple[int, int],
) -> Iterator[str]:
    """Прогоняет поток ответа через журнал, не меняя ни байта.

    Поток уходит клиенту кусками по мере генерации, и посмотреть его
    целиком заранее нельзя. Поэтому мы копируем каждый кусок, отдаём
    клиенту и только после последнего записываем поток в трассировку
    ровно таким, каким он ушёл в сокет.

    Блок finally нужен для обрыва: клиент может закрыть соединение
    раньше конца, тогда генератор закроют — но половина потока уже
    отправлена, и это тоже надо записать.
    """
    started = time.monotonic()
    pieces: list[str] = []
    chunks = 0
    completed = False

    try:
        for piece in source:
            chunks += 1
            pieces.append(piece)
            yield piece
        completed = True
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000
        interrupted = not completed
        log.sse(chunks=chunks, elapsed_ms=elapsed_ms, interrupted=interrupted)
        log.response(
            content=reply,
            status=200,
            elapsed_ms=elapsed_ms,
            finish_reason=finish_reason,
            reason=reason,
            usage=usage,
        )
        trace.stream(
            chunks=chunks,
            elapsed_ms=elapsed_ms,
            raw="".join(pieces),
            interrupted=interrupted,
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

    # Температура — как у настоящих моделей: 0 = самое точное правило,
    # больше 1 = более общий ответ. Здесь параметр честно влияет на
    # выбор между репликами разных правил — в отличие от цепи Маркова,
    # где 93% состояний имеют единственный переход.
    temperature = read_number(data, "temperature", DEFAULT_TEMPERATURE, 0.0, 2.0)

    params = describe_params(data, messages, max_tokens, temperature)
    log.params(params)

    # decide() вместо respond(): модель отвечает и объясняет, как
    # пришла к ответу. Разбор уходит и в консоль, и в файл — он и
    # есть главное, что в этой модели стоит посмотреть.
    decision = model.decide(user_message, max_tokens=max_tokens, temperature=temperature)
    response_text = decision.reply
    log.brain(decision.steps)
    trace.brain(decision.path, decision.reason, params, decision.steps)

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

    # Данные для after_request: он дописывает в журнал ответ, а ответ
    # этот собирается здесь. Через g — потому что обработчик один, а
    # журналирование общее на все ответы, включая 400 и 404.
    g.reply = response_text
    g.reason = decision.reason
    g.finish_reason = "stop"  # было "length" — это ломает агентов
    g.usage = (prompt_tokens, completion_tokens)

    # Стриминговый ответ
    if data.get("stream", False):
        g.streamed = True
        return Response(
            traced_stream(
                generate_streaming_response(
                    request_id,
                    created_time,
                    MODEL_NAME,
                    response_text,
                    prompt_tokens,
                    completion_tokens,
                ),
                reply=response_text,
                reason=decision.reason,
                finish_reason=g.finish_reason,
                usage=g.usage,
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
                    "finish_reason": g.finish_reason,
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сервер модели на правилах Элизы (OpenAI-совместимый API)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"порт, на котором поднимется сервер (по умолчанию {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="не печатать обмен в терминал и не писать трассировку в файл",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        metavar="PATH",
        help=(
            "куда писать всё, что пришло по HTTP и ушло в ответ "
            f"(по умолчанию {DEFAULT_LOG_FILE})"
        ),
    )
    args = parser.parse_args()

    # Журналы включаем по умолчанию: разговор с моделью — это и есть
    # работа сервера. --no-log убирает и консоль, и файл трассировки.
    log.enabled = not args.no_log
    trace.path = None if args.no_log else Path(args.log_file)

    session_info = [
        ("модель", MODEL_NAME),
        ("мозг", "таблица правил Элизы (eliza.py), обучение не требуется"),
        ("адрес", f"http://127.0.0.1:{args.port}/v1"),
        ("журнал", "выключен (--no-log)" if args.no_log else f"обмен в терминале + {args.log_file}"),
        ("выбор ответа", "вес = score ** (1 / temperature), temperature из запроса"),
    ]
    trace.open([f"{label:<15} : {value}" for label, value in session_info])

    # flush=True — по той же причине, что и в журнале: сервер запускают
    # с перенаправлением вывода, где print() без сброса копится в буфере
    print(f"Eliza SLM: {MODEL_NAME}")
    print(f"  адрес  : http://127.0.0.1:{args.port}/v1")
    print(f"  журнал : {'выключен (--no-log)' if args.no_log else f'обмен печатается, трассировка — {args.log_file}'}")
    print("Остановить: Ctrl+C\n", flush=True)

    try:
        # threaded=False: сервер однопоточный. Память Элизы (что она уже
        # говорила) и счётчик запросов в журнале тогда остаются
        # последовательными, а в журнал не попадают перепутанные друг с
        # другом запросы.
        #
        # debug=True поднимает интерактивный отладчик, доступный любому,
        # кто дотянулся до порта. Для локальной демки это лишний риск:
        # включайте только на своей машине и только на время отладки.
        app.run(port=args.port, debug=False, threaded=False)
    finally:
        # Закрываем файл даже на Ctrl+C: на диске останется всё,
        # что успело уйти клиентам
        trace.close()


if __name__ == "__main__":
    main()
