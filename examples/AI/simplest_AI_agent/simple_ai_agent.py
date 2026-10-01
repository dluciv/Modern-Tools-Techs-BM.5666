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

Настройки провайдера не хранятся в коде, а берутся из конфигов opencode:

  * API-ключ      — ~/.local/share/opencode/auth.json
  * baseURL, модели — ~/.config/opencode/opencode.jsonc

Запуск:
    ./simple_ai_agent.py                      # провайдер из OPENCODE_PROVIDER
    ./simple_ai_agent.py routerai-ru
    ./simple_ai_agent.py routerai-ru --model xiaomi/mimo-v2.5
    ./simple_ai_agent.py --no-native-tools    # принудительно текстовый протокол
    ./simple_ai_agent.py --list-providers
"""
import argparse
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from safe_agent_tools import VirtualFileSystem, DocumentationSearch, SafeCalculator

DEFAULT_PROVIDER = "routerai-ru"
DEFAULT_MAX_STEPS = 8


def _xdg(env_var: str, default: str) -> Path:
    """Путь по XDG-спецификации (с учётом $XDG_DATA_HOME / $XDG_CONFIG_HOME)."""
    value = os.environ.get(env_var)
    return Path(value) if value else Path.home() / default


AUTH_FILE = _xdg("XDG_DATA_HOME", ".local/share") / "opencode" / "auth.json"
CONFIG_FILE = _xdg("XDG_CONFIG_HOME", ".config") / "opencode" / "opencode.jsonc"


# --------------------------------------------------------------------------- #
# Чтение конфигов opencode
# --------------------------------------------------------------------------- #

def strip_jsonc(text: str) -> str:
    """Убирает // и /* */ комментарии из JSONC.

    Важно: нельзя тупо regex-ать `//`, потому что в baseURL живёт
    "https://routerai.ru/api/v1" — такой URL разъехался бы на два.
    Поэтому идём посимвольно и следим за кавычками.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    last_comma = -1  # позиция последней запятой вне строки

    while i < n:
        ch = text[i]

        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            last_comma = -1
            out.append(ch)
            i += 1
            continue

        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue

        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            last_comma = -1
            continue

        if ch == ",":
            last_comma = len(out)
            out.append(ch)
            i += 1
            continue

        if ch in "}]":
            # Хвостовая запятая: {"a": 1,} — JSON её не терпит, а редакторы любят
            if last_comma != -1:
                out[last_comma] = " "
            last_comma = -1

        if not ch.isspace():
            last_comma = -1

        out.append(ch)
        i += 1

    return "".join(out)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался JSON-объект в верхнем уровне {path}")
    return data


@dataclass(frozen=True)
class Provider:
    """Провайдер LLM, собранный из конфигов opencode."""

    id: str
    name: str
    base_url: str
    api_key: str
    models: list[str]


def available_providers() -> list[str]:
    """Провайдеры, которые известны хотя бы одному из конфигов."""
    names: set[str] = set()

    if AUTH_FILE.exists():
        try:
            names |= set(load_json(AUTH_FILE))
        except json.JSONDecodeError:
            pass

    if CONFIG_FILE.exists():
        try:
            names |= set(load_json(CONFIG_FILE).get("providers", {}))
        except json.JSONDecodeError:
            pass

    return sorted(names)


def load_provider(provider_id: str) -> Provider:
    """Читает ключ из auth.json, baseURL и модели — из opencode.jsonc."""
    try:
        auth = load_json(AUTH_FILE)
    except FileNotFoundError as e:
        raise SystemExit(
            f"Ошибка: {e}\n"
            f"Ожидаемый путь: {AUTH_FILE}\n"
            "Войдите в opencode командой `/login`, чтобы он создал файл."
        ) from e
    except json.JSONDecodeError as e:
        raise SystemExit(f"Не удалось разобрать {AUTH_FILE}: {e}") from e

    try:
        config = load_json(CONFIG_FILE)
    except FileNotFoundError as e:
        raise SystemExit(f"Ошибка: {e}\nОжидаемый путь: {CONFIG_FILE}") from e
    except json.JSONDecodeError as e:
        raise SystemExit(f"Не удалось разобрать {CONFIG_FILE}: {e}") from e

    provider_cfg = config.get("providers", {}).get(provider_id)
    if provider_cfg is None:
        known = ", ".join(available_providers()) or "(пусто)"
        raise SystemExit(
            f"Провайдер {provider_id!r} не найден в {CONFIG_FILE} (секция providers).\n"
            f"Доступные провайдеры: {known}"
        )

    base_url = provider_cfg.get("settings", {}).get("baseURL")
    if not base_url:
        raise SystemExit(
            f"У провайдера {provider_id!r} в {CONFIG_FILE} нет settings.baseURL"
        )
    base_url = base_url.rstrip("/")

    # Ключ лежит в auth.json, но у локального сервера (ollama, учебный
    # марковский сервер) авторизации нет — там ключ просто не нужен.
    if provider_id in auth:
        entry = auth[provider_id]
        api_key = entry.get("key") if isinstance(entry, dict) else entry
        if not api_key:
            raise SystemExit(f"В {AUTH_FILE} у провайдера {provider_id!r} нет поля 'key'")
    elif is_local(base_url):
        api_key = ""
        print(f"[info] {provider_id!r} не найден в {AUTH_FILE}, но {base_url} локальный — идём без ключа")
    else:
        known = ", ".join(available_providers()) or "(пусто)"
        raise SystemExit(
            f"Провайдер {provider_id!r} не найден в {AUTH_FILE}.\n"
            f"Доступные провайдеры: {known}\n"
            f"Добавьте его через `/login` в opencode."
        )

    return Provider(
        id=provider_id,
        name=provider_cfg.get("name", provider_id),
        base_url=base_url,
        api_key=api_key,
        models=list(provider_cfg.get("models", {})),
    )


def is_local(url: str) -> bool:
    """Локальный сервер — ключ ему не нужен."""
    return bool(re.search(r"://(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)", url))


def mask_key(api_key: str) -> str:
    """Показывает ключ частично — чтобы не утекать в терминал/логи."""
    if len(api_key) <= 10:
        return "*" * len(api_key)
    return f"{api_key[:6]}…{api_key[-4:]}"


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
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self.max_steps = max_steps
        self.native_tools = native_tools
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

        headers = {"Content-Type": "application/json"}
        if self.provider.api_key:
            headers["Authorization"] = f"Bearer {self.provider.api_key}"

        response = requests.post(
            f"{self.provider.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=180,
        )

        if not response.ok:
            raise RuntimeError(
                f"HTTP {response.status_code} от {self.provider.base_url}:\n{response.text[:500]}"
            )

        message = dict(response.json()["choices"][0]["message"])
        message.setdefault("role", "assistant")
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
                print(f"  [warn] не удалось разобрать аргументы: {arguments!r}")
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
            return f"Error: wrong arguments for '{name}': {e}"
        except Exception as e:
            return f"Error: '{name}' failed: {e}"

    # -- цикл агента -------------------------------------------------------- #

    def chat(self, user_message: str) -> str:
        """Обрабатывает реплику пользователя, включая все вызовы инструментов.

        Модель может запросить инструмент несколько раз подряд (например,
        прочитать файл и посчитать пример из него) — поэтому цикл, а не
        одиночная проверка. Останавливаемся, когда модель ответила без
        вызова инструмента, либо упёрлись в max_steps.
        """
        self.conversation.append({"role": "user", "content": user_message})

        for _ in range(self.max_steps):
            message = self.complete()
            self.conversation.append(message)

            calls = self.extract_tool_calls(message)
            if not calls:
                return message.get("content") or "(модель не вернула текст)"

            for call in calls:
                result = self.run_tool(call["name"], call["args"])
                print(f"  [tool] {call['name']}({format_args(call['args'])}) -> {result}")

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

        return f"(достигнут лимит шагов: {self.max_steps})"

    # -- REPL ---------------------------------------------------------------- #

    def run(self) -> None:
        protocol = "native tools" if self.native_tools else "text JSON fallback"
        key_info = (
            f"{mask_key(self.provider.api_key)} (из {AUTH_FILE})"
            if self.provider.api_key
            else "не требуется (локальный сервер)"
        )
        print(f"Safe Agent started: {self.provider.name} / {self.model_name}")
        print(f"  base URL: {self.provider.base_url}")
        print(f"  API key : {key_info}")
        print(f"  protocol: {protocol}")
        print(f"  tools   : {', '.join(self.tools)}")
        print("Type 'quit' to exit.\n")

        while True:
            try:
                user_input = input("You: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if user_input.strip().lower() in {"quit", "exit"}:
                break

            try:
                print(f"Agent: {self.chat(user_input)}")
            except Exception as e:
                print(f"[error] {e}")


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
                f"У провайдера {provider.id!r} нет моделей в {CONFIG_FILE}. "
                "Укажите модель явно: --model <id>"
            )
        model_name = provider.models[0]
    elif provider.models and model_name not in provider.models:
        print(
            f"[warn] модели {model_name!r} нет в конфиге провайдера {provider.id!r} "
            f"(в конфиге: {', '.join(provider.models)}) — пробуем как есть"
        )

    SafeAgent(
        provider=provider,
        model_name=model_name,
        max_steps=args.max_steps,
        native_tools=not args.no_native_tools,
    ).run()


if __name__ == "__main__":
    main()
