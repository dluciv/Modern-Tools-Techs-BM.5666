"""
Журналы сервера модели: что прислал клиент и что ушло в ответ.

Журналы агента (`../simplest_AI_agent/agent_log.py`) рассказывают о той же
картине с другой стороны. Агент пишет, что он положил в сокет; сервер —
что из сокета пришло. Положите рядом `agent_trace.log` и `eliza_trace.log`,
собранные на одном и том же диалоге, и получите два взгляда на один разговор:

    агент                                 сервер
    ЗАПРОС #1 POST .../chat/completions    ЗАПРОС #1 POST /v1/chat/completions
      заголовки, тело целиком               заголовки, то же тело
    ОТВЕТ #1  статус 200 за 0.90с          ОТВЕТ #1  статус 200 за 0.00с
      тело целиком                           то же тело + разбор: какое правило
                                            сработало и почему выбран ответ

Как и у агента, здесь два журнала с разной степенью подробности:

* ModelLog  — короткий пересказ для терминала.
* ModelTrace — полная запись в файл: что реально пришло по HTTP, как
  модель поняла реплику и что ушло в ответ.

Модуль ничего не знает про Элизу — он получает готовые строки и печатает
их или пишет в файл. Оба журнала включены по умолчанию: обмен с моделью
и есть содержание работы сервера, а не отладочный хвост.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TextIO

# Колонка с меткой подобрана по самой длинной метке «· сервер» (9 знаков)
TAG_WIDTH = 9

# Сколько символов ответа показываем в терминале. Файл трассировки
# хранит тело целиком, а пересказу длинный JSON только мешает.
MAX_CONSOLE_TEXT = 400


def mask_key(value: str) -> str:
    """Показывает секрет частично: sk-VCK…LyqN.

    Локальному серверу ключ не нужен, но прислать его может любой клиент:
    агент и opencode подставляют Authorization из своих конфигов. Файл
    журнала потом попадает в репозиторий или на слайд, поэтому целиком
    секреты не пишем никогда.
    """
    value = value.strip()
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:6]}…{value[-4:]}"


def human_bytes(size: int) -> str:
    """Размер тела запроса — видно, как растёт контекст диалога."""
    if size < 1024:
        return f"{size} байт"
    return f"{size / 1024:.1f} КБ"


def format_elapsed(milliseconds: float) -> str:
    """Время обмена: у toy-модели оно меньше секунды, и «0.00с» не видно."""
    if milliseconds < 1000:
        return f"{milliseconds:.2f} мс"
    return f"{milliseconds / 1000:.2f}с"


class ModelLog:
    """Печатает обмен сервера с клиентом в терминал.

    Каждой строке предшествует метка, чтобы было видно, кто её написал:

        → клиент   запрос клиента: адрес, размер тела, а ниже — параметры
        · разбор   как модель поняла реплику: правило, хвост, веса, выбор
        ← клиент   ответ: статус, время, расход и сама реплика модели
        ⚡ поток    отчёт по потоковому ответу: сколько чанков и за сколько
        ! ошибка   ошибка (битое тело, неизвестный маршрут, обрыв потока)

    Метки — префиксы, поэтому вывод годится и для чтения глазами, и для
    grep по строке «← клиент».
    """

    def __init__(self, enabled: bool = True, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self.stream = stream
        self._counter = 0

    # -- низкоуровневый помощник ------------------------------------------- #

    def _write(self, tag: str, text: str) -> None:
        """Печатает многострочный текст, ставя метку только в начало.

        Модель отвечает абзацами, а журнал — списком шагов; если вставлять
        метку в каждую строку, вывод превращается в лестницу и читать его
        невозможно.
        """
        if not self.enabled:
            return

        prefix = f"{tag:>{TAG_WIDTH}} │ "
        lines = text.splitlines() or [""]
        for i, line in enumerate(lines):
            # Метка печатаем только у первой строки блока
            head = prefix if i == 0 else " " * len(prefix)
            # flush=True обязателен: сервер часто запускают с перенаправлением
            # вывода в файл или в docker logs, где stdout не терминал и
            # Python буферизует вывод. Без сброса журнал появился бы
            # в файле только через несколько килобайт — или вообще
            # никогда, если сервер остановлен раньше.
            print(head + line, file=self.stream, flush=True)

    def _block(self, tag: str, head: str, body: str = "") -> None:
        """Блок из «шапки» и текста: шапка с меткой, текст с отступом."""
        self._write(tag, f"{head}\n{body}" if body else head)

    @staticmethod
    def _short(text: str, limit: int = MAX_CONSOLE_TEXT) -> str:
        """Обрезает длинный текст: в терминале полный JSON не нужен.

        В файле трассировки тело остаётся целиком — обрезаем только
        пересказ для глаз.
        """
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    # -- события обмена ----------------------------------------------------- #

    def request(self, method: str, path: str, size: int) -> None:
        """Запрос клиента. Размер тела — по нему видно объём контекста."""
        self._counter += 1
        self._block("→ клиент", f"#{self._counter} {method} {path} · {human_bytes(size)}")

    def params(self, text: str) -> None:
        """Параметры того же запроса — второй строкой блока «→ клиент»."""
        self._write("→ клиент", text)

    def brain(self, steps: Sequence[str]) -> None:
        """Разбор ответа модели: правило, хвост, кандидаты, выбор."""
        if not steps:
            return
        self._write("· разбор", "\n".join(steps))

    def response(
        self,
        content: str,
        status: int,
        elapsed_ms: float,
        finish_reason: str = "",
        reason: str = "",
        usage: tuple[int, int] | None = None,
    ) -> None:
        """Ответ, который ушёл клиенту: сначала сводка, потом реплика."""
        head = f"{status} за {format_elapsed(elapsed_ms)}"
        if finish_reason:
            head += f" · finish_reason: {finish_reason}"
        if reason:
            head += f" · «{reason}»"
        if usage:
            head += f" · токены prompt {usage[0]} / ответ {usage[1]}"
        self._block("← клиент", head, self._short(content))

    def sse(self, chunks: int, elapsed_ms: float, interrupted: bool = False) -> None:
        """Потоковый ответ: сколько кусков SSE ушло и за какое время.

        Метод называется sse(), а не stream(), потому что stream у журнала
        уже занят — это поток вывода (тот же смысл, что у агента).
        """
        tail = " · соединение прервано клиентом" if interrupted else ""
        self._write("⚡ поток", f"{chunks} чанков за {format_elapsed(elapsed_ms)}{tail}")

    def error(self, text: str) -> None:
        self._write("! ошибка", self._short(text, 200))


class ModelTrace:
    """Пишет в файл полный протокол обмена с клиентом.

    В отличие от ModelLog это не пересказ, а протокол: тело запроса и тело
    ответа целиком, вместе с заголовками и статусом, плюс разбор ответа
    модели по шагам — какое правило сработало, какие варианты были и с
    какими вероятностями выбран именно этот.

    Обратите внимание на тело запроса: там вся история диалога целиком,
    а не только последняя реплика. Так и должно быть видно — клиент
    шлёт модели весь контекст, и именно поэтому диалог дорожает с каждым
    шагом по инструментам.

    Заголовок Authorization маскируется: файл может попасть в
    репозиторий или на слайд.
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

        Иначе только что импортированный сервер писал бы файл на диск
        ещё до первого запроса.
        """
        if self._handle is not None or self.path is None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")

        self._rule()
        # astimezone() — чтобы в шапке было осведённое время, а не наивное:
        # файл могут открыть на другой машине и в другом поясе
        self._write(
            f"СЕССИЯ МОДЕЛИ — запущено {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}"
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
        # Сбрасываем буфер после каждой записи: если сервер остановлен
        # на Ctrl+C, файл останется читаемым
        self._handle.write(text + "\n")
        self._handle.flush()

    @staticmethod
    def _pretty(raw: str) -> str:
        """Приводит тело к читаемому виду.

        Тело не всегда JSON: клиент может прислать мусор, а мы обязаны
        записать ровно то, что пришло. Такой текст пишем как есть —
        в этом и смысл трассировки.
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

    def _rule(self, char: str = "=") -> None:
        self._write(char * 78)

    # -- блоки -------------------------------------------------------------- #

    def open(self, header_lines: Sequence[str]) -> None:
        """Запоминает шапку сессии; сам файл создастся при первом запросе.

        Так трассировка работает и тогда, когда сервер подняли из чужого
        кода и никто не вызвал open().
        """
        self._header = tuple(header_lines)

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: str,
    ) -> None:
        self._counter += 1
        self._rule()
        self._write(f"ЗАПРОС #{self._counter}  {method} {path}")
        self._rule()

        self._write("заголовки:")
        for name, value in self._masked_headers(headers).items():
            self._write(f"    {name}: {value}")

        self._write()
        self._write("тело запроса:")
        self._write(self._pretty(body) if body.strip() else "(пусто)")
        self._write()

    def brain(
        self,
        path: str,
        reason: str,
        params: str,
        steps: Sequence[str],
    ) -> None:
        """Разбор того, как модель поняла реплику и что выбрала.

        Шаги приходят из eliza.py: там же, где принимается решение,
        добавляется строка «почему». Поэтому объяснение не может
        разойтись с самим решением.
        """
        self._write("разбор моделью:")
        self._write(f"  путь      : {path or '?'}")
        self._write(f"  причина   : {reason or '?'}")
        if params:
            self._write(f"  параметры : {params}")
        for step in steps:
            self._write(f"  {step}")
        self._write()

    def response(self, status: int, elapsed_ms: float, body: str) -> None:
        self._rule()
        self._write(f"ОТВЕТ #{self._counter}  статус {status}  за {format_elapsed(elapsed_ms)}")
        self._rule()

        self._write("тело ответа:")
        self._write(self._pretty(body))
        self._write()

    def stream(self, chunks: int, elapsed_ms: float, raw: str, interrupted: bool = False) -> None:
        """Потоковый ответ — ровно так, как он ушёл в сокет.

        Тело потока нельзя посмотреть заранее: оно собирается по мере
        генерации. Поэтому сюда приходят сами куски SSE, склеенные
        обратно в исходный текст, — видно и границы чанков, и
        финальный `data: [DONE]`.
        """
        self._rule()
        tail = " · соединение прервано клиентом" if interrupted else ""
        self._write(
            f"ОТВЕТ #{self._counter} (поток SSE)  {chunks} чанков  "
            f"за {format_elapsed(elapsed_ms)}{tail}"
        )
        self._rule()

        self._write("тело ответа (SSE, как ушло в сокет):")
        self._write(raw.rstrip("\n"))
        self._write()

    def event(self, text: str) -> None:
        """Помечает границу внутри обмена: например, что ответ ушёл потоком."""
        self._rule("-")
        self._write(text)
        self._rule("-")

    def failure(self, text: str) -> None:
        """Запрос не был обработан: исключение в обработчике."""
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
