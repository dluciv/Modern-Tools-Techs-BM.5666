"""
Сервисный модуль: журналы общения агента с моделью.

Зачем они нужны. Обычный режим показывает только итог: вот реплика
пользователя, вот ответ. Но самое интересное в агенте происходит между
репликами — модель просит инструмент, агент выполняет его, результат
уходит обратно в модель, и она формулирует ответ. Здесь два журнала
с разной степенью подробности:

* ConversationLog — короткий пересказ для терминала.
* HttpTrace — полная запись в файл: что агент реально положил в сокет
  и что API вернул в ответ.

Формат строки в консоли — префикс-тег, поэтому вывод годится и для
чтения глазами, и для grep:

    → model   xiaomi/mimo-v2.5 · сообщений: 3 · инструментов: 5
    ← model   4 сообщения
    ⚡ tool   factorial({"n": 5})
    ← result  120
    → model   xiaomi/mimo-v2.5 · сообщений: 5 · инструментов: 5

Модуль ничего не знает про агента — он получает готовые строки и печатает
их или пишет в файл. Оба журнала по умолчанию включены: разговор с
моделью — это и есть содержание работы агента, а не отладочный хвост.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from opencode_config import mask_key


class ConversationLog:
    """Печатает обмен агента с моделью в терминал.

    Каждой строке предшествует тег, чтобы было видно, кто её написал:

        → user    реплика пользователя
        → model   запрос к модели (что именно уходит в API)
        ← model   ответ модели (что вернулось из API)
        ⚡ tool   модель вызвала инструмент — показываем вызов
        ← result результат инструмента, который агент отправит модели
        ! error  ошибка (сеть, разбор ответа, инструмент)
    """

    def __init__(self, enabled: bool = True, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self.stream = stream

    # -- низкоуровневый помощник ------------------------------------------- #

    def _write(self, tag: str, text: str) -> None:
        """Печатает многострочный текст, ставя тег только в начало.

        Модель отвечает абзацами; если вставлять тег в каждую строку,
        вывод превращается в лестницу и читать его невозможно.
        """
        if not self.enabled:
            return

        prefix = f"{tag:>7} │ "
        lines = text.splitlines() or [""]
        for i, line in enumerate(lines):
            # Тег печатаем только у первой строки блока
            head = prefix if i == 0 else " " * len(prefix)
            print(head + line, file=self.stream)

    # -- события разговора --------------------------------------------------- #

    def user(self, text: str) -> None:
        self._write("→ user", text)

    def request(self, model: str, messages: int, tools: int, extra: str = "") -> None:
        """Запрос к модели. messages/tools — чтобы видеть, как растёт диалог."""
        details = f"{model} · сообщений: {messages}"
        if tools:
            details += f" · инструментов: {tools}"
        if extra:
            details += f" · {extra}"
        self._write("→ model", details)

    def response(self, model: str, content: str, tool_calls: list[dict[str, Any]] | None = None) -> None:
        """Ответ модели: её текст и/или запрошенные вызовы инструментов."""
        parts: list[str] = []

        if tool_calls:
            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name", "?")
                args = function.get("arguments", "{}")
                parts.append(f"вызов инструмента: {name}({args})")

        if content:
            parts.append(content)
        elif not tool_calls:
            parts.append("(пустой ответ)")

        self._write("← model", "\n\n".join(parts))

    def tool_call(self, name: str, args: str) -> None:
        self._write("⚡ tool", f"{name}({args})")

    def tool_result(self, result: str) -> None:
        self._write("← result", result)

    def note(self, text: str) -> None:
        """Служебная пометка агента (обрывки диалога, лимит шагов)."""
        self._write("· agent", text)

    def error(self, text: str) -> None:
        self._write("! error", text)


class HttpTrace:
    """Пишет в файл, что реально уходит по HTTP и что приходит в ответ.

    В отличие от ConversationLog это не пересказ, а протокол: тело запроса
    и тело ответа целиком, вместе с заголовками и статусом.

    Обратите внимание: в каждом запросе повторяется вся история диалога,
    а не только последняя реплика. Так и должно быть видно — агент шлёт
    модели целый контекст, и именно поэтому диалог дорожает с каждым
    шагом по инструментам.

    Ключ API маскируется: файл может попасть в репозиторий или на слайд.
    """

    def __init__(self, path: Path | None) -> None:
        # None означает «не писать совсем» — так же, как выключенный лог
        self.path = path
        self._handle: TextIO | None = None
        self._counter = 0
        self._header: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.path is not None

    # -- низкоуровневый помощник ------------------------------------------- #

    def _ensure_open(self) -> None:
        """Создаёт файл при первом обращении, а не в конструкторе.

        Иначе агент, запущенный как библиотека (без run()), писал бы
        в никуда: open() никто бы не вызвал.
        """
        if self._handle is not None or self.path is None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")

        self._rule()
        # astimezone() — чтобы в шапке было осведённое время, а не наивное:
        # файл могут открыть на другой машине и в другом поясе
        self._write(
            f"СЕССИЯ АГЕНТА — запущено {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}"
        )
        self._write()
        for line in self._header:
            self._write(f"  {line}")
        self._write()

    def _write(self, text: str = "") -> None:
        if self.path is None:
            return
        self._ensure_open()
        if self._handle is None:
            return
        # Сбрасываем буфер после каждой записи: если студент нажмёт
        # Ctrl+C посреди работы, файл останется читаемым
        self._handle.write(text + "\n")
        self._handle.flush()

    @staticmethod
    def _pretty(raw: str) -> str:
        """Приводит тело к читаемому виду.

        Ответ не всегда JSON: на 502 или 503 прокси часто отдаёт HTML.
        Такой текст пишем как есть — в этом и смысл трассировки.
        """
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            return raw

    @staticmethod
    def _masked_headers(headers: Mapping[str, str]) -> dict[str, str]:
        masked = dict(headers)
        if "Authorization" in masked:
            # Заголовок приходит как "Bearer <ключ>" — маскируем только ключ
            scheme, _, key = masked["Authorization"].partition(" ")
            masked["Authorization"] = f"{scheme} {mask_key(key)}" if key else scheme
        return masked

    # -- блоки -------------------------------------------------------------- #

    def _rule(self, char: str = "=") -> None:
        self._write(char * 78)

    def open(self, header_lines: Sequence[str]) -> None:
        """Запоминает шапку сессии; сам файл создастся при первом запросе.

        Так трассировка работает и тогда, когда агентом пользуются как
        библиотекой и никто не вызывает open().
        """
        self._header = tuple(header_lines)

    def request(
        self,
        url: str,
        method: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> None:
        self._counter += 1
        self._rule()
        self._write(f"ЗАПРОС #{self._counter}  {method} {url}")
        self._rule()

        self._write("заголовки:")
        for name, value in self._masked_headers(headers).items():
            self._write(f"    {name}: {value}")

        self._write()
        self._write("тело запроса:")
        self._write(self._pretty(json.dumps(payload, ensure_ascii=False)))
        self._write()

    def response(self, status: int, elapsed: float, body: str) -> None:
        self._rule()
        self._write(f"ОТВЕТ #{self._counter}  статус {status}  за {elapsed:.2f}с")
        self._rule()

        self._write("тело ответа:")
        self._write(self._pretty(body))
        self._write()

    def event(self, text: str) -> None:
        """Помечает границу между запросами — например, вызов инструмента."""
        self._rule("-")
        self._write(text)
        self._rule("-")

    def failure(self, text: str) -> None:
        """Запрос не дошёл до ответа: сеть, таймаут, обрыв."""
        self._rule()
        self._write(f"СБОЙ #{self._counter}  {text}")
        self._rule()
        self._write()

    def close(self) -> None:
        if self._handle is None:
            return

        self._write(f"итого запросов: {self._counter}")
        self._rule()
        self._handle.close()
        self._handle = None
