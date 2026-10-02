#!/usr/bin/env -S uv run --quiet --script --
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "requests>=2.34.2",
# ]
# ///
"""
Simple AI agent

Агент умеет вызывать инструменты (tools) двумя способами:

1. Нативный OpenAI-протокол: передаём `tools` в /chat/completions и читаем
   `message.tool_calls`. Так умеют настоящие модели (OpenAI, MiMo, Qwen, ...).
2. Фолбэк: модель отвечает обычным текстом с JSON внутри
   (`{"tool": ..., "args": {...}}`). Нужен для моделей без function calling
   и чтобы посмотреть, как тот же агент работает с ними (--no-native-tools).

Запуск:
    ./simple_ai_agent.py                      # провайдер из OPENCODE_PROVIDER
    ./simple_ai_agent.py routerai-ru
    ./simple_ai_agent.py routerai-ru --model xiaomi/mimo-v2.5
    ./simple_ai_agent.py --no-log             # убрать лог совсем
    ./simple_ai_agent.py --log-file run.log   # писать трассировку в другой файл
    ./simple_ai_agent.py --no-native-tools    # принудительно текстовый протокол
    ./simple_ai_agent.py --list-providers

Журналы включены по умолчанию, как в самом OpenCode:
  * в терминал печатается пересказ обмена (→ model, ← model, ⚡ tool …);
  * в agent_trace.log пишется полный протокол — тела запросов и ответов
    целиком, вместе с заголовками и статусом. История диалога повторяется
    в каждом запросе: так видно, что агент шлёт модели весь контекст.

Три файла рядом:
    simple_ai_agent.py  — этот файл: цикл агента
    safe_agent_tools.py — сами инструменты (калькулятор, ФС, поиск)
    opencode_config.py  — чтение конфигов opencode (ключи, baseURL)
    agent_log.py        — журналы: пересказ в терминал и трассировка в файл
"""
import argparse
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from agent_log import ConversationLog, HttpTrace
from opencode_config import (
    AUTH_FILE,
    Provider,
    available_providers,
    load_provider,
    mask_key,
)
from safe_agent_tools import DocumentationSearch, SafeCalculator, VirtualFileSystem

DEFAULT_PROVIDER = "routerai-ru"
DEFAULT_MAX_STEPS = 8
DEFAULT_LOG_FILE = "agent_trace.log"


# --------------------------------------------------------------------------- #
# Реестр инструментов
# --------------------------------------------------------------------------- #

NO_ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Tool:
    """Инструмент: описание для модели + реализация на Python."""

    name: str
    description: str
    parameters: dict[str, Any]
    call: Callable[..., str]

    def schema(self) -> dict[str, Any]:
        """Описание в формате OpenAI function calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def build_tools() -> dict[str, Tool]:
    """Собирает инструменты агента. Единственный источник правды:
    из этого словаря строятся и tools для API, и текстовая инструкция.
    """
    fs = VirtualFileSystem()
    calc = SafeCalculator()
    docs = DocumentationSearch()

    return {
        "list_files": Tool(
            name="list_files",
            description="List all available files in the virtual filesystem",
            parameters=NO_ARGS,
            call=fs.list_files,
        ),
        "read_file": Tool(
            name="read_file",
            description="Read the contents of a specific file",
            parameters={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "File name, e.g. README.md"},
                },
                "required": ["filename"],
                "additionalProperties": False,
            },
            call=fs.read_file,
        ),
        "calculate": Tool(
            name="calculate",
            description='Evaluate a mathematical expression (e.g. "2 + 2", "10 * 5")',
            parameters={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Arithmetic expression"},
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            call=calc.calculate,
        ),
        "factorial": Tool(
            name="factorial",
            description="Calculate factorial of a number",
            parameters={
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "Integer between 0 and 20"},
                },
                "required": ["n"],
                "additionalProperties": False,
            },
            call=calc.factorial,
        ),
        "search_docs": Tool(
            name="search_docs",
            description="Search Python documentation",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look up, e.g. 'sorted'"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            call=docs.search,
        ),
    }


def text_tools_prompt(tools: dict[str, Tool]) -> str:
    """Та же инструкция, но для моделей без native tool calling."""
    lines = ["You have access to the following tools:", ""]

    for tool in tools.values():
        props = tool.parameters.get("properties", {})
        args = ", ".join(
            f"{arg_name}: {arg_schema.get('type', 'string')}"
            for arg_name, arg_schema in props.items()
        )
        lines.append(f"{tool.name}({args}) - {tool.description}")

    lines += [
        "",
        "To use a tool, respond with JSON in this exact format:",
        '{"tool": "tool_name", "args": {"arg_name": "value"}}',
        "",
        "Example:",
        '{"tool": "calculate", "args": {"expression": "5 * 3 + 10"}}',
        "",
        "If you don't need a tool, respond normally.",
    ]
    return "\n".join(lines)


def iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Вытаскивает из текста все JSON-объекты.

    json.JSONDecoder умеет сам находить конец объекта (с учётом вложенности,
    кавычек и экранирования), поэтому просто пробуем декодировать от каждой
    открывающей скобки. Наивный text[text.find('{'):text.rfind('}') + 1]
    ломается, как только в ответе есть обычный текст или два JSON-объекта.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def format_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in args.items())


# --------------------------------------------------------------------------- #
# Агент
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = "You are a helpful assistant that can use tools to answer questions."


class SafeAgent:
    def __init__(
        self,
        provider: Provider,
        model_name: str,
        max_steps: int = DEFAULT_MAX_STEPS,
        native_tools: bool = True,
        log: ConversationLog | None = None,
        trace: HttpTrace | None = None,
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self.max_steps = max_steps
        self.native_tools = native_tools
        self.log = log or ConversationLog()
        self.trace = trace or HttpTrace(path=None)
        self.conversation: list[dict[str, Any]] = []

        self.tools = build_tools()
        self.add_system_message(SYSTEM_PROMPT)

    # -- протокол ----------------------------------------------------------- #

    def add_system_message(self, content: str) -> None:
        parts = [content]
        if not self.native_tools:
            # Описание инструментов в текстовом виде нужно только без native tools
            parts.append(text_tools_prompt(self.tools))
        self.conversation.append({"role": "system", "content": "\n\n".join(parts)})

    def complete(self) -> dict[str, Any]:
        """Один запрос к /chat/completions. Возвращает сообщение ассистента
        ровно в том виде, в котором его вернул API (с tool_calls, если есть).
        """
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": self.conversation,
            "max_tokens": 1000,
        }
        if self.native_tools:
            payload["tools"] = [tool.schema() for tool in self.tools.values()]
            payload["tool_choice"] = "auto"

        self.log.request(
            self.model_name,
            messages=len(self.conversation),
            tools=len(self.tools) if self.native_tools else 0,
            extra="native tools" if self.native_tools else "текстовый протокол",
        )

        headers = {"Content-Type": "application/json"}
        if self.provider.api_key:
            headers["Authorization"] = f"Bearer {self.provider.api_key}"

        url = f"{self.provider.base_url}/chat/completions"

        # Трассировку пишем ДО отправки: если запрос упал на сети или по
        # таймауту, мы всё равно хотим видеть, что именно уходило
        self.trace.request(url=url, method="POST", headers=headers, payload=payload)

        started = time.monotonic()
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=180)
        except requests.RequestException as exc:
            self.trace.failure(f"{type(exc).__name__}: {exc}")
            raise
        elapsed = time.monotonic() - started

        self.trace.response(
            status=response.status_code, elapsed=elapsed, body=response.text
        )

        if not response.ok:
            # Тело ответа у 503 часто пустое — тогда причина не в нашем запросе
            detail = response.text.strip()[:400] or "(тело ответа пустое)"
            raise RuntimeError(f"HTTP {response.status_code} за {elapsed:.1f}с: {detail}")

        message = dict(response.json()["choices"][0]["message"])
        message.setdefault("role", "assistant")

        self.log.response(
            self.model_name,
            content=message.get("content") or "",
            tool_calls=message.get("tool_calls"),
        )
        return message

    # -- разбор намерения модели --------------------------------------------- #

    def extract_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Понимает, что модель хочет запустить инструмент.

        Сначала смотрим нативные tool_calls. Только если их нет — ищем JSON
        в тексте (фолбэк для моделей без function calling).
        """
        calls: list[dict[str, Any]] = []

        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function", {})
            arguments = function.get("arguments") or "{}"
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                self.log.error(f"не удалось разобрать аргументы: {arguments!r}")
                args = {}
            calls.append(
                {
                    "id": raw_call.get("id"),
                    "name": function.get("name", ""),
                    "args": args,
                }
            )

        if calls:
            return calls

        for obj in iter_json_objects(message.get("content") or ""):
            if "tool" in obj:
                calls.append(
                    {
                        "id": None,  # нет tool_call_id — ответим обычным сообщением
                        "name": obj.get("tool", ""),
                        "args": obj.get("args") or {},
                    }
                )
                break

        return calls

    def run_tool(self, name: str, args: dict[str, Any]) -> str:
        """Выполняет инструмент. Ошибки не бросаем наружу: модель должна
        увидеть текст ошибки и попробовать исправиться."""
        tool = self.tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'. Available tools: {', '.join(self.tools)}"

        try:
            return tool.call(**args)
        except TypeError as e:
            # Модель передала не те аргументы: показываем ей, чего ждали
            return f"Error: wrong arguments for '{name}': {e}"
        except Exception as e:  # noqa: BLE001 — ловим всё намеренно
            # Инструмент не должен ронять агента: модель получит текст ошибки
            # и сможет либо поправиться, либо ответить пользователю
            return f"Error: '{name}' failed: {e}"

    # -- цикл агента -------------------------------------------------------- #

    def chat(self, user_message: str) -> str:
        """Обрабатывает реплику пользователя, включая все вызовы инструментов.

        Модель может запросить инструмент несколько раз подряд (например,
        прочитать файл и посчитать пример из него) — поэтому цикл, а не
        одиночная проверка. Останавливаемся, когда модель ответила без
        вызова инструмента, либо упёрлись в max_steps.
        """
        self.log.user(user_message)
        self.conversation.append({"role": "user", "content": user_message})

        for step in range(1, self.max_steps + 1):
            message = self.complete()
            self.conversation.append(message)

            calls = self.extract_tool_calls(message)
            if not calls:
                return message.get("content") or "(модель не вернула текст)"

            for call in calls:
                self.trace.event(
                    f"модель просит инструмент: {call['name']}"
                    f"({json.dumps(call['args'], ensure_ascii=False)})"
                )
                self.log.tool_call(call["name"], json.dumps(call["args"], ensure_ascii=False))

                result = self.run_tool(call["name"], call["args"])
                self.log.tool_result(result)

                if call["id"] is not None:
                    # Нативный вызов: ответ должен идти в поле role=tool
                    self.conversation.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": result,
                        }
                    )
                else:
                    # Текстовый фолбэк: tool_call_id нет, отвечаем как пользователь
                    self.conversation.append(
                        {
                            "role": "user",
                            "content": f"Tool execution result: {result}",
                        }
                    )

            # Если остались шаги — покажем, что диалог продолжается
            if step < self.max_steps:
                self.log.note(f"шаг {step}/{self.max_steps}, возвращаю результат модели")

        limit_message = f"(достигнут лимит шагов: {self.max_steps})"
        self.log.note(limit_message)
        return limit_message

    # -- REPL ---------------------------------------------------------------- #

    def run(self) -> None:
        protocol = "native tools" if self.native_tools else "text JSON fallback"
        key_info = (
            f"{mask_key(self.provider.api_key)} (из {AUTH_FILE})"
            if self.provider.api_key
            else "не требуется (локальный сервер)"
        )
        session_info = [
            ("провайдер", self.provider.name),
            ("модель", self.model_name),
            ("base URL", self.provider.base_url),
            ("протокол", protocol),
            ("ключ", key_info),
            ("лимит шагов", self.max_steps),
            ("инструменты", ", ".join(self.tools)),
        ]
        self.trace.open([f"{label:<12} : {value}" for label, value in session_info])

        print(f"Safe Agent started: {self.provider.name} / {self.model_name}")
        print(f"  base URL: {self.provider.base_url}")
        print(f"  API key : {key_info}")
        print(f"  protocol: {protocol}")
        print(f"  tools   : {', '.join(self.tools)}")
        print(f"  лог     : {'обмен печатается' if self.log.enabled else 'выключен (--no-log)'}")
        if self.trace.enabled and self.trace.path is not None:
            print(f"  трассировка: всё, что уходит по HTTP и приходит обратно — {self.trace.path}")
        print("Type 'quit' to exit.\n")

        try:
            while True:
                try:
                    user_input = input("You: ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break

                if user_input.strip().lower() in {"quit", "exit"}:
                    break

                try:
                    answer = self.chat(user_input)
                    print(f"\nAgent: {answer}\n")
                except Exception as e:  # noqa: BLE001 — REPL не должен падать
                    # Ошибка сети или API: показываем её, но продолжаем диалог,
                    # чтобы студент мог увидеть агента целиком, а не traceback
                    self.log.error(str(e))
                    self.trace.event(f"ошибка: {e}")
                    print(f"[ошибка] {e}\n")
        finally:
            # Закрываем файл даже на Ctrl+C: на диске останется всё,
            # что успело уйти в сеть
            self.trace.close()


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Простейший агент с инструментами (конфиг берётся из opencode)",
    )
    parser.add_argument(
        "provider",
        nargs="?",
        default=os.environ.get("OPENCODE_PROVIDER", DEFAULT_PROVIDER),
        help=f"id провайдера из конфигов opencode (по умолчанию {DEFAULT_PROVIDER})",
    )
    parser.add_argument(
        "--model",
        help="id модели; по умолчанию — первая модель провайдера из opencode.jsonc",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help=f"максимум итераций с вызовом инструмента за один ход (по умолчанию {DEFAULT_MAX_STEPS})",
    )
    parser.add_argument(
        "--no-native-tools",
        action="store_true",
        help="не отправлять tools в API: общаться только текстовым JSON-протоколом",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="не печатать обмен с моделью и не писать трассировку в файл (по умолчанию включено)",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        metavar="PATH",
        help=f"куда писать всё, что уходит по HTTP и возвращается (по умолчанию {DEFAULT_LOG_FILE})",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="показать провайдеров, найденных в конфигах opencode",
    )
    args = parser.parse_args()

    if args.list_providers:
        for name in available_providers():
            print(name)
        return

    provider = load_provider(args.provider)

    model_name = args.model
    if not model_name:
        if not provider.models:
            raise SystemExit(
                f"У провайдера {provider.id!r} нет моделей в конфиге. "
                "Укажите модель явно: --model <id>"
            )
        model_name = provider.models[0]
    elif provider.models and model_name not in provider.models:
        print(
            f"[warn] модели {model_name!r} нет в конфиге провайдера {provider.id!r} "
            f"(в конфиге: {', '.join(provider.models)}) — пробуем как есть"
        )

    # Логи включаем по умолчанию: разговор с моделью — это и есть работа
    # агента. --no-log убирает и консольный лог, и файл трассировки.
    verbose = not args.no_log

    SafeAgent(
        provider=provider,
        model_name=model_name,
        max_steps=args.max_steps,
        native_tools=not args.no_native_tools,
        log=ConversationLog(enabled=verbose),
        trace=HttpTrace(path=Path(args.log_file) if verbose else None),
    ).run()


if __name__ == "__main__":
    main()
