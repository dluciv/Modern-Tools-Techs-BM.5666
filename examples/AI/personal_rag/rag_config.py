"""
Конфигурация простой RAG-системы.

Файл конфигурации — обычный TOML рядом с кодом (config.toml). Его должен
уметь отредактировать человек, поэтому:

  * он снабжён комментариями по-русски;
  * пути в нём могут быть относительными (относятся к каталогу самого
    конфига, а не к текущей директории запуска — иначе сервис перестал
    бы находить каталог документов, запущенный из systemd или cron);
  * поддерживается "~" (домашний каталог);
  * секрет (API-ключ) лежит не в конфиге, а в отдельном человеко-
    редактируемом файле рядом (по умолчанию keys.env), который добавлен
    в .gitignore. Так ключ не уедет в git вместе с конфигом, но и не
    спрятан в недрах opencode-базы: его видно и можно поправить руками.

Переменные окружения имеют приоритет над файлом — это стандартная
практика для сервисов: конфиг по умолчанию, окружение для переопределения
при деплое. Особенно важен RAG_API_KEY: ключ не должен требоваться
в конфиге вообще.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

# Имена переменных окружения, которые переопределяют содержимое config.toml.
# Сами имена не выдуманы: они перечислены в README и в примере keys.env.
ENV_OVERRIDES = {
    "RAG_CATALOG_DIR": ("catalog", "dir"),
    "RAG_STORE_DIR": ("store", "dir"),
    "RAG_BASE_URL": ("llm", "base_url"),
    "RAG_MODEL": ("llm", "model"),
    "RAG_API_KEY": ("llm", "api_key"),
    "RAG_LOG_FILE": ("service", "log_file"),
}


@dataclass
class Config:
    """Всё, что нужно знать сервису и инструментам, чтобы запуститься."""

    # Базовый каталог, от которого разрешаются относительные пути конфига.
    base_dir: Path
    # Где лежат сами документы (каталог может содержать симлинки на другие
    # каталоги — это и есть способ «подключить» чужие подкаталоги).
    catalog_dir: Path
    follow_symlinks: bool
    # Где лежит sqlite-база с векторами и индексом.
    store_dir: Path
    # Модель эмбеддингов (локальная, ONNX, качается один раз в cache_dir).
    embedding_model: str
    cache_dir: Path
    # Дешёвая модель для /ask — OpenAI-совместимый API.
    base_url: str
    model: str
    api_key: str
    api_key_file: Path
    timeout: float
    max_tokens: int
    temperature: float
    # Параметры гибридного поиска (BM25 + kNN, слияние через RRF).
    vector_weight: float
    bm25_weight: float
    candidates: int
    max_distance: float
    chunk_chars: int
    # Куда писать журнал сервиса (пусто — только терминал).
    log_file: Path | None
    # Все пути из конфига «как написано» — пригодится для /health и README.
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class ConfigError(RuntimeError):
    """Конфигурация не читается. Сообщение должно содержать, что именно не так."""


def _resolve(base_dir: Path, value: str) -> Path:
    """Путь из конфига → абсолютный.

    "~" раскрывается домашним каталогом; остальное считается относительно
    каталога конфига. Склейка через `/` работает и на абсолютных путях
    (Path('/a') / '/b' == Path('/b')), так что писать абсолютный путь
    в конфиге тоже можно.
    """
    return (base_dir / os.path.expanduser(value)).resolve()


def _read_key_file(path: Path) -> str:
    """Читает ключ из человеко-редактируемого файла вида `KEY=значение`.

    Формат — как в .env: пустые строки и комментарии (#) игнорируются,
    можно писать export перед именем. Берётся первая строка с ключом
    RAG_API_KEY; если её нет, возвращается пустая строка (не ошибка:
    локальный поиск работает и без ключа, нужен он только для /ask).
    """
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == "RAG_API_KEY":
            return value.strip().strip("'\"")
    return ""


def load_config(path: str | Path | None = None) -> Config:
    """Загружает конфиг: путь → аргумент, или RAG_CONFIG, или config.toml рядом с модулем."""
    if path is None:
        path = os.environ.get("RAG_CONFIG")
    if path is None:
        cfg_file = Path(__file__).with_name("config.toml")
    else:
        cfg_file = Path(os.path.expanduser(path))
        if not cfg_file.is_file():
            raise ConfigError(f"нет файла конфигурации: {cfg_file}")
    try:
        raw = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"не читается {cfg_file}: {e}") from e

    base_dir = cfg_file.parent.resolve()

    def section(name: str) -> dict[str, Any]:
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"секция [{name}] в {cfg_file} должна быть таблицей")
        return value

    def apply_env(sec: dict[str, Any]) -> None:
        """Переменные окружения перекрывают значения из файла."""
        for env_name, (sec_name, key) in ENV_OVERRIDES.items():
            if env_name in os.environ and sec is section(sec_name):
                sec[key] = os.environ[env_name]

    for sec in (section("catalog"), section("store"), section("llm"), section("service")):
        apply_env(sec)

    catalog = section("catalog")
    store = section("store")
    embedding = section("embedding")
    llm = section("llm")
    service = section("service")
    search = section("search")

    api_key_file = _resolve(base_dir, str(llm.get("key_file", "keys.env")))
    api_key = str(llm.get("api_key", "")) or _read_key_file(api_key_file)

    log_file_raw = str(service.get("log_file", "")).strip()

    return Config(
        base_dir=base_dir,
        catalog_dir=_resolve(base_dir, str(catalog.get("dir", "catalog"))),
        follow_symlinks=bool(catalog.get("follow_symlinks", True)),
        store_dir=_resolve(base_dir, str(store.get("dir", "data"))),
        embedding_model=str(
            embedding.get("model", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        ),
        cache_dir=_resolve(base_dir, str(embedding.get("cache_dir", ".model_cache"))),
        base_url=str(llm.get("base_url", "https://routerai.ru/api/v1")).rstrip("/"),
        model=str(llm.get("model", "inclusionai/ling-3.0-flash")),
        api_key=api_key,
        api_key_file=api_key_file,
        timeout=float(llm.get("timeout", 60)),
        max_tokens=int(llm.get("max_tokens", 600)),
        temperature=float(llm.get("temperature", 0.2)),
        vector_weight=float(search.get("vector_weight", 1.0)),
        bm25_weight=float(search.get("bm25_weight", 1.0)),
        candidates=int(search.get("candidates", 40)),
        max_distance=float(search.get("max_distance", 0.8)),
        chunk_chars=int(search.get("chunk_chars", 1200)),
        log_file=_resolve(base_dir, log_file_raw) if log_file_raw else None,
        raw=raw,
    )
