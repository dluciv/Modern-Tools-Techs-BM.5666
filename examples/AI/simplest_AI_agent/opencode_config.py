"""
Сервисный модуль: чтение настроек opencode.

Здесь нет ничего про агента и про инструменты — только хранилища opencode
на диске и умение их прочитать:

  * ~/.local/share/opencode/opencode.db  — база opencode v2, API-ключи
  * ~/.config/opencode/opencode.jsonc    — провайдеры, baseURL, модели

Про ключи стоит знать главное. В v1 они лежали в отдельном файле
auth.json. В v2 opencode хранит всё в одной базе, а ключи — в таблице
credential, где значение записано строкой JSON вида {"type": "key", ...}.
Поэтому здесь нужен sqlite3 — это стандартная библиотека Python, ничего
доустанавливать не придётся.

Базу мы открываем только на чтение (mode=ro) и только на время чтения:
чужую базу не трогаем.

Отдельный модуль нужен, чтобы в simple_ai_agent.py осталась только логика
агента: диалог с моделью, вызов инструментов, цикл шагов.
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _xdg(env_var: str, default: str) -> Path:
    """Путь по XDG-спецификации (с учётом $XDG_DATA_HOME / $XDG_CONFIG_HOME)."""
    value = os.environ.get(env_var)
    return Path(value) if value else Path.home() / default


DB_FILE = _xdg("XDG_DATA_HOME", ".local/share") / "opencode" / "opencode.db"
CONFIG_FILE = _xdg("XDG_CONFIG_HOME", ".config") / "opencode" / "opencode.jsonc"

# Старый путь v1. Нужен только как запасной вариант: если у кого-то
# остался opencode v1, ключи лежат именно там.
LEGACY_AUTH_FILE = _xdg("XDG_DATA_HOME", ".local/share") / "opencode" / "auth.json"


# --------------------------------------------------------------------------- #
# Разбор JSONC
# --------------------------------------------------------------------------- #

def strip_jsonc(text: str) -> str:
    """Убирает // и /* */ комментарии из JSONC, оставляя валидный JSON.

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
    """Читает .json или .jsonc и отдаёт словарь."""
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    if not isinstance(data, dict):
        # ValueError, а не TypeError: JSON разобран, но это не объект —
        # наш load_json ждёт словарь (config.get(...) дальше по коду)
        raise ValueError(f"Ожидался JSON-объект в верхнем уровне {path}")
    return data


def is_local(url: str) -> bool:
    """Локальный сервер — ключ ему не нужен."""
    return bool(re.search(r"://(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)", url))


def mask_key(api_key: str) -> str:
    """Показывает ключ частично — чтобы не утекать в терминал/логи."""
    if len(api_key) <= 10:
        return "*" * len(api_key)
    return f"{api_key[:6]}…{api_key[-4:]}"


# --------------------------------------------------------------------------- #
# API-ключи
# --------------------------------------------------------------------------- #

# Колонка, где лежит сам ключ. Всё остальное (id, label, time_created)
# — служебное, для работы демки не нужно.
_KEY_QUERY = "SELECT integration_id, value FROM credential WHERE active IS NOT 0"


def _from_database() -> dict[str, str]:
    """Читает ключи из opencode.db.

    Открываем в режиме mode=ro — sqlite3 не создаст файл, если его нет,
    и не заблокирует базу, пока ею пользуется сам opencode.
    """
    uri = f"file:{DB_FILE}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(_KEY_QUERY).fetchall()
    finally:
        # Закрываем сразу: база может быть занята работающим opencode
        connection.close()

    keys: dict[str, str] = {}
    for integration_id, value in rows:
        if not integration_id or not isinstance(value, str):
            continue

        # В v2 значение — это JSON-строка {"type": "key", "key": "sk-..."}.
        # В будущем opencode может положить туда что-то другое (OAuth,
        # refresh-токен), поэтому аккуратно проверяем тип и не падаем.
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or parsed.get("type") != "key":
            continue

        key = parsed.get("key")
        if isinstance(key, str) and key:
            keys[integration_id] = key

    return keys


def _from_legacy_file() -> dict[str, str]:
    """Читает ключи из auth.json — формат opencode v1.

    Оставлен для совместимости: у кого-то в аудитории может стоять v1.
    В v2 этого файла уже нет.
    """
    try:
        data = load_json(LEGACY_AUTH_FILE)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}

    keys: dict[str, str] = {}
    for integration_id, entry in data.items():
        # В v1 значение бывает строкой, а бывает объектом {"key": ...}
        key = entry.get("key") if isinstance(entry, dict) else entry
        if isinstance(key, str) and key:
            keys[integration_id] = key
    return keys


def load_api_keys() -> dict[str, str]:
    """Все API-ключи, которые opencode знает: {провайдер: ключ}.

    Сначала пробуем базу v2, и только если её нет — старый auth.json.
    """
    if DB_FILE.exists():
        return _from_database()
    return _from_legacy_file()


def key_source() -> str:
    """Откуда на самом деле взяты ключи — для честной надписи в логе."""
    return str(DB_FILE) if DB_FILE.exists() else f"{LEGACY_AUTH_FILE} (v1)"


# --------------------------------------------------------------------------- #
# Провайдеры
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Provider:
    """Провайдер LLM, собранный из конфигов opencode."""

    id: str
    name: str
    base_url: str
    api_key: str
    models: list[str]


def available_providers() -> list[str]:
    """Провайдеры, которые известны базе ключей или конфигу."""
    names: set[str] = set()

    try:
        names |= set(load_api_keys())
    except sqlite3.Error:
        # База занята или повреждена — не падаем из-за списка, просто
        # покажем то, что смогли прочитать
        pass

    if CONFIG_FILE.exists():
        try:
            names |= set(load_json(CONFIG_FILE).get("providers", {}))
        except json.JSONDecodeError:
            pass

    return sorted(names)


def configured_base_url(provider_id: str) -> str:
    """baseURL провайдера из opencode.jsonc.

    Отдельная функция нужна для --list-providers: там мы хотим увидеть
    провайдера, даже если он не запускается. KeyError, а не SystemExit, —
    вызывающий код сам решает, что написать.
    """
    config = _read_config()
    return str(config.get("providers", {})[provider_id]["settings"]["baseURL"]).rstrip("/")


def _read_config() -> dict[str, Any]:
    """Читает opencode.jsonc с понятными сообщениями об ошибках."""
    try:
        return load_json(CONFIG_FILE)
    except FileNotFoundError as e:
        raise SystemExit(f"Ошибка: {e}\nОжидаемый путь: {CONFIG_FILE}") from e
    except json.JSONDecodeError as e:
        raise SystemExit(f"Не удалось разобрать {CONFIG_FILE}: {e}") from e


def load_provider(provider_id: str) -> Provider:
    """Читает ключ из opencode.db, baseURL и модели — из opencode.jsonc.

    Бросает SystemExit с понятным текстом: для учебной демки важнее
    объяснить, что починить, чем красивый traceback.
    """
    try:
        api_keys = load_api_keys()
    except sqlite3.Error as e:
        raise SystemExit(f"Не удалось прочитать {DB_FILE}: {e}") from e

    config = _read_config()

    provider_cfg = config.get("providers", {}).get(provider_id)
    if provider_cfg is None:
        # Перечисляем именно настроенных провайдеров, а не всех, у кого
        # нашёлся ключ: у провайдера без settings.baseURL запустить агента
        # всё равно нельзя. Ключ без настройки — это половина провайдера.
        configured = ", ".join(sorted(config.get("providers", {}))) or "(пусто)"
        raise SystemExit(
            f"Провайдер {provider_id!r} не найден в {CONFIG_FILE} (секция providers).\n"
            f"Настроенные провайдеры: {configured}\n"
            f"Добавьте секцию providers.{provider_id} с settings.baseURL."
        )

    base_url = provider_cfg.get("settings", {}).get("baseURL")
    if not base_url:
        raise SystemExit(
            f"У провайдера {provider_id!r} в {CONFIG_FILE} нет settings.baseURL"
        )
    base_url = base_url.rstrip("/")

    # Ключ лежит в базе opencode, но у локального сервера (ollama, учебный
    # марковский сервер) авторизации нет — там ключ просто не нужен.
    if provider_id in api_keys:
        api_key = api_keys[provider_id]
    elif is_local(base_url):
        api_key = ""
        print(
            f"[info] у провайдера {provider_id!r} нет ключа, "
            f"но {base_url} локальный — идём без авторизации"
        )
    else:
        # Считаем по-настоящему: перечисляем тех, у кого ключ в базе
        # действительно есть, а не всех настроенных подряд.
        with_key = ", ".join(sorted(api_keys)) or "(ни у кого)"
        raise SystemExit(
            f"Для провайдера {provider_id!r} не найден API-ключ в {key_source()}.\n"
            f"Ключ есть у: {with_key}\n"
            f"Добавьте ключ командой `/login` в opencode."
        )

    return Provider(
        id=provider_id,
        name=provider_cfg.get("name", provider_id),
        base_url=base_url,
        api_key=api_key,
        models=list(provider_cfg.get("models", {})),
    )
