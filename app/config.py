"""
Модуль конфигурации проекта «ДругИИ».

Загружает настройки из .env файла через python-dotenv.
Все параметры доступны через класс Config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    """Конфигурация приложения. Все поля загружаются из переменных окружения."""

    # --- Токены ботов ---
    bot_token_tg: str = field(default="")
    bot_token_max: str = field(default="")

    # --- Прокси для Telegram (xray/v2ray SOCKS5/HTTP), пусто = напрямую ---
    tg_proxy: str = field(default="")

    # --- URL платформы MAX ---
    max_api_url: str = field(default="https://platform-api2.max.ru")

    # --- Режим приёма обновлений MAX: longpoll | webhook ---
    max_mode: str = field(default="longpoll")

    # --- Путь к базе данных ---
    database_path: str = field(default="")

    # --- Параметры сканирования Bluetooth ---
    scan_interval: int = field(default=30)
    greeting_cooldown: int = field(default=3600)
    greeting_min_age: int = field(default=30)

    # --- Webhook сервер ---
    webhook_host: str = field(default="127.0.0.1")
    webhook_port: int = field(default=8080)

    # --- Публичный HTTPS URL вебхука для регистрации на платформе MAX ---
    webhook_public_url: str = field(default="")

    # --- Секрет для аутентификации webhook MAX ---
    webhook_secret: str = field(default="")

    # --- Логирование ---
    log_level: str = field(default="INFO")

    @classmethod
    def load(cls, env_file: str | None = None) -> Config:
        """
        Загружает конфигурацию из .env файла.

        Args:
            env_file: Путь к .env файлу. Если None, ищет .env в корне проекта.

        Returns:
            Экземпляр Config с загруженными значениями.
        """
        if env_file is None:
            # Ищем .env в корне проекта (на два уровня выше данного файла)
            env_file = str(Path(__file__).resolve().parent.parent / ".env")

        load_dotenv(env_file)

        project_root = Path(__file__).resolve().parent.parent
        default_db_path = str(project_root / "data" / "drugii.db")

        return cls(
        bot_token_tg=cls._get_env("BOT_TOKEN_TG", ""),
        bot_token_max=cls._get_env("BOT_TOKEN_MAX", ""),
        tg_proxy=cls._get_env("TG_PROXY", ""),
        webhook_public_url=cls._get_env("WEBHOOK_PUBLIC_URL", ""),
        max_api_url=cls._get_env("MAX_API_URL", "https://platform-api2.max.ru"),
        max_mode=cls._get_env("MAX_MODE", "longpoll"),
        database_path=cls._get_env("DATABASE_PATH", default_db_path),
        scan_interval=cls._get_env_int("SCAN_INTERVAL", 30),
        greeting_cooldown=cls._get_env_int("GREETING_COOLDOWN", 3600),
        greeting_min_age=cls._get_env_int("GREETING_MIN_AGE", 30),
        webhook_host=cls._get_env("WEBHOOK_HOST", "127.0.0.1"),
        webhook_port=cls._get_env_int("WEBHOOK_PORT", 8080),
        webhook_secret=cls._get_env("WEBHOOK_SECRET", ""),
        log_level=cls._get_env("LOG_LEVEL", "INFO"),
    )

    @staticmethod
    def _get_env(key: str, default: str) -> str:
        """Безопасное чтение переменной окружения."""
        return os.environ.get(key, default)

    @staticmethod
    def _get_env_int(key: str, default: int) -> int:
        """
        Безопасное чтение целочисленной переменной окружения.
        При некорректном значении возвращает default и логирует предупреждение.
        """
        raw = os.environ.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            import logging
            logging.getLogger(__name__).warning(
                "Некорректное значение %s=%r, ожидалось число. Использую default=%d.",
                key, raw, default,
            )
            return default