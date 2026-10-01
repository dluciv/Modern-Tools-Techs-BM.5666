"""
Сервисный модуль: чтение настроек opencode.

Здесь нет ничего про агента и про инструменты — только два файла на диске
и умение их прочитать:

  * ~/.local/share/opencode/auth.json   — API-ключи
  * ~/.config/opencode/opencode.jsonc  — провайдеры, baseURL, модели

Отдельный модуль нужен, чтобы в simple_ai_agent.py осталась только логика
агента: диалог с моделью, вызов инструментов, цикл шагов.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _xdg(env_var: str, default: str) -> Path:
    """Путь по XDG-спецификации (с учётом $XDG_DATA_HOME / $XDG_CONFIG_HOME)."""
    value = os.environ.get(env_var)
    return Path(value) if value else Path.home() / default


AUTH_FILE = _xdg("XDG_DATA_HOME", ".local/share") / "opencode" / "auth.json"
CONFIG_FILE = _xdg("XDG_CONFIG_HOME", ".config") / "opencode" / "opencode.jsonc"


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
    """Читает ключ из auth.json, baseURL и модели — из opencode.jsonc.

    Бросает SystemExit с понятным текстом: для учебной демки важнее
    объяснить, что починить, чем красивый traceback.
    """
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
        print(
            f"[info] {provider_id!r} не найден в {AUTH_FILE}, "
            f"но {base_url} локальный — идём без ключа"
        )
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
