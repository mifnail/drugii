"""
Модуль движка приветствий «ДругИИ».

Содержит функции генерации приветственных сообщений и полный
цикл обработки нового обнаружения BLE-устройства.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.ai import generate_ai_greeting, generate_fallback_greeting
from app.bot_max import send_greeting_max
from app.bot_telegram import send_greeting_telegram
from app.config import Config
from app.database import get_db

logger = logging.getLogger(__name__)


async def generate_greeting(
    config: Config,
    user_full_name: str,
    device_name: str | None = None,
) -> str:
    """
    Генерирует приветствие: через GigaChat, с fallback на шаблоны.

    Args:
        config: Конфигурация приложения.
        user_full_name: Полное имя пользователя (ФИО).
        device_name: Название обнаруженного BLE-устройства (опционально).

    Returns:
        Строка приветствия.
    """
    ai_text = await generate_ai_greeting(config, user_full_name, device_name)
    if ai_text:
        return ai_text
    return generate_fallback_greeting(user_full_name)


async def process_new_detection(
    detection_id: int,
    mac_address: str,
    device_name: str | None,
    rssi: int | None,
    config: Config,
) -> bool:
    """
    Полный цикл обработки нового обнаружения BLE-устройства:

    1. Ищет устройство в таблице ``devices`` (``is_active=1``).
    2. Если устройство не зарегистрировано — возвращает ``False``.
    3. Проверяет ``greeting_cooldown`` — не отправлять чаще раза в указанный
       интервал.
    4. Проверяет ``greeting_min_age`` — возраст обнаружения должен быть не
       меньше заданного значения.
    5. Генерирует приветствие через :func:`generate_greeting`.
    6. Отправляет в Telegram и MAX (если привязаны).
    7. Сохраняет запись в ``greetings``.
    8. Возвращает ``True``, если приветствие было отправлено.

    Args:
        detection_id: ID записи в таблице detections (уже созданной сканером).
        mac_address: MAC-адрес устройства (XX:XX:XX:XX:XX:XX).
        device_name: Имя устройства (опционально).
        rssi: Уровень сигнала (опционально).
        config: Конфигурация приложения.

    Returns:
        ``True``, если приветствие отправлено, иначе ``False``.
    """
    db = await get_db()
    now = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 1. Поиск устройства в БД
    # ------------------------------------------------------------------
    try:
        row = await db.execute(
            """
            SELECT id, user_id, device_name
            FROM devices
            WHERE mac_address = ? AND is_active = 1
            """,
            (mac_address,),
        )
        device = await row.fetchone()
    except Exception as exc:
        logger.error("Ошибка поиска устройства %s: %s", mac_address, exc)
        return False

    if device is None:
        logger.info("Устройство %s не зарегистрировано. Пропускаем.", mac_address)
        return False

    device_id: int = device["id"]
    user_id: int = device["user_id"]
    registered_device_name: str | None = device["device_name"]

    # ------------------------------------------------------------------
    # 2. Проверка greeting_cooldown
    # ------------------------------------------------------------------
    try:
        row = await db.execute(
            """
            SELECT sent_at FROM greetings
            WHERE user_id = ?
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        last_greeting = await row.fetchone()
    except Exception as exc:
        logger.error("Ошибка проверки кулдауна для user_id=%s: %s", user_id, exc)
        return False

    if last_greeting is not None:
        try:
            last_sent = datetime.fromisoformat(last_greeting["sent_at"])
            # Если last_sent без таймзоны — считаем в UTC
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            diff_seconds = (now - last_sent).total_seconds()
            if diff_seconds < config.greeting_cooldown:
                logger.debug(
                    "Кулдаун для user_id=%s ещё не прошёл "
                    "(осталось %.0f с). Пропускаем.",
                    user_id,
                    config.greeting_cooldown - diff_seconds,
                )
                return False
        except (ValueError, TypeError) as exc:
            logger.warning("Ошибка парсинга даты последнего приветствия: %s", exc)

    # ------------------------------------------------------------------
    # 3. Проверка greeting_min_age (возраст обнаружения)
    # ------------------------------------------------------------------
    # detection только что создан; ждём greeting_min_age секунд
    # Если min_age > 0 — делаем короткую задержку
    if config.greeting_min_age > 0:
        logger.debug(
            "Ожидание %d с (greeting_min_age) перед отправкой…",
            config.greeting_min_age,
        )
        await asyncio.sleep(config.greeting_min_age)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 4. Генерация приветствия
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Берём ФИО пользователя
    try:
        row = await db.execute(
            "SELECT full_name, telegram_id, max_chat_id FROM users WHERE id = ?",
            (user_id,),
        )
        user = await row.fetchone()
    except Exception as exc:
        logger.error("Ошибка получения пользователя user_id=%s: %s", user_id, exc)
        return False

    if user is None:
        logger.warning("Пользователь user_id=%s не найден.", user_id)
        return False

    full_name: str = user["full_name"]
    telegram_id: int | None = user["telegram_id"]
    max_chat_id: str | None = user["max_chat_id"]

    # Используем имя устройства из БД, если не передано новое
    effective_device_name = device_name or registered_device_name
    greeting_text = await generate_greeting(config, full_name, effective_device_name)

    # ------------------------------------------------------------------
    # 5. Отправка приветствия
    # ------------------------------------------------------------------
    sent_via_parts: list[str] = []
    send_errors: list[str] = []

    if telegram_id is not None:
        try:
            await send_greeting_telegram(telegram_id, greeting_text)
            sent_via_parts.append("telegram")
            logger.info("Приветствие отправлено в TG user_id=%s", user_id)
        except Exception as exc:
            send_errors.append(f"telegram: {exc}")
            logger.error("Ошибка отправки в TG user_id=%s: %s", user_id, exc)

    if max_chat_id is not None:
        try:
            await send_greeting_max(max_chat_id, greeting_text)
            sent_via_parts.append("max")
            logger.info("Приветствие отправлено в MAX user_id=%s", user_id)
        except Exception as exc:
            send_errors.append(f"max: {exc}")
            logger.error("Ошибка отправки в MAX user_id=%s: %s", user_id, exc)

    if not sent_via_parts:
        logger.warning(
            "Приветствие не отправлено пользователю user_id=%s "
            "(нет привязанных мессенджеров).",
            user_id,
        )
        return False

    # ------------------------------------------------------------------
    # 6. Сохранение записи в greetings
    # ------------------------------------------------------------------
    try:
        await db.execute(
            """
            INSERT INTO greetings (user_id, device_id, detection_id, sent_via, message_text, sent_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                device_id,
                detection_id,
                "+".join(sorted(sent_via_parts)),
                greeting_text,
                now.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        await db.commit()
        logger.debug(
            "Запись greeting сохранена (user=%s, device=%s, detection=%s)",
            user_id,
            device_id,
            detection_id,
        )
    except Exception as exc:
        logger.error("Ошибка сохранения greeting: %s", exc)
        # Само приветствие уже могло быть отправлено — не откатываем

    return True