"""
Сервисный модуль: журнал общения агента с моделью.

Зачем он нужен. Обычный режим показывает только итог: вот реплика
пользователя, вот ответ. Но самый интересный в агенте происходит между
репликами — модель просит инструмент, агент выполняет его, результат
уходит обратно в модель, и она формулирует ответ. Этот модуль печатает
каждый шаг в терминал с пометкой в начале строки.

Формат строки — префикс-тег, поэтому вывод годится и для чтения глазами,
и для grep:

    → model   xiaomi/mimo-v2.5 · tools: 5
    ← model   3 сообщения
    ⚡ tool   factorial({"n": 5})
    ← result  120
    → model   xiaomi/mimo-v2.5 · tools: 5

Модуль ничего не знает про агента — он получает готовые строки и печатает
их. Умолчание — молчать (log.enabled = False), чтобы не мешать обычному
разговору.
"""

from typing import Any, TextIO


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

    def __init__(self, enabled: bool = False, stream: TextIO | None = None) -> None:
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
