"""
Модуль для асинхронной работы с SQLite через aiosqlite.

Содержит функции инициализации БД, получения соединения,
а также список таблиц и индексов проекта «ДругИИ».
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.config import Config

# Хранилище для единственного соединения БД в рамках приложения
_db_connection: aiosqlite.Connection | None = None


async def init_db(config: Config) -> None:
    """
    Инициализирует базу данных: создаёт директорию data/,
    открывает соединение и создаёт таблицы с индексами.

    Args:
        config: Конфигурация приложения.
    """
    global _db_connection

    # Создаём директорию для БД, если её нет
    db_path = Path(config.database_path)
    db_dir = db_path.parent
    if not db_dir.exists():
        db_dir.mkdir(parents=True, exist_ok=True)

    # Открываем соединение
    _db_connection = await aiosqlite.connect(str(db_path))
    _db_connection.row_factory = aiosqlite.Row

    await _create_tables(_db_connection)
    await _create_indexes(_db_connection)


async def _create_tables(db: aiosqlite.Connection) -> None:
    """Создаёт таблицы, если их ещё нет."""
    queries = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            telegram_id INTEGER UNIQUE,
            max_chat_id TEXT UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT NOT NULL UNIQUE,
            device_name TEXT,
            user_id INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT NOT NULL,
            device_name TEXT,
            rssi INTEGER,
            detected_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS greetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            device_id INTEGER NOT NULL,
            detection_id INTEGER NOT NULL,
            sent_via TEXT NOT NULL,
            message_text TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (device_id) REFERENCES devices(id),
            FOREIGN KEY (detection_id) REFERENCES detections(id)
        )
        """,
    ]

    for query in queries:
        await db.execute(query)

    await db.commit()


async def _create_indexes(db: aiosqlite.Connection) -> None:
    """Создаёт индексы для ускорения поиска."""
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_detections_mac ON detections(mac_address)",
        "CREATE INDEX IF NOT EXISTS idx_detections_detected_at ON detections(detected_at)",
        "CREATE INDEX IF NOT EXISTS idx_greetings_user_id ON greetings(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_greetings_sent_at ON greetings(sent_at)",
        "CREATE INDEX IF NOT EXISTS idx_devices_mac_address ON devices(mac_address)",
        "CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id)",
    ]

    for idx in indexes:
        await db.execute(idx)

    await db.commit()


async def get_db() -> aiosqlite.Connection:
    """
    Возвращает единственное соединение с БД.

    Returns:
        Экземпляр aiosqlite.Connection.

    Raises:
        RuntimeError: Если init_db() не был вызван перед get_db().
    """
    if _db_connection is None:
        raise RuntimeError(
            "База данных не инициализирована. Вызовите init_db() перед get_db()."
        )
    return _db_connection


async def close_db() -> None:
    """Закрывает соединение с БД, если оно открыто."""
    global _db_connection
    if _db_connection is not None:
        await _db_connection.close()
        _db_connection = None