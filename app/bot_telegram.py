"""
Модуль Telegram бота.

Использует aiogram 3.x для приёма команд от пользователей
и отправки приветствий.
"""

from __future__ import annotations

import asyncio
import logging
from typing import NoReturn

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession

from app.config import Config
from app.database import get_db

logger = logging.getLogger(__name__)

# Глобальный экземпляр бота — используется send_greeting_telegram
_bot: Bot | None = None


async def send_greeting_telegram(telegram_id: int, message: str) -> None:
    """
    Отправляет приветственное сообщение пользователю в Telegram.

    Args:
        telegram_id: Telegram ID получателя.
        message: Текст сообщения.

    Raises:
        RuntimeError: Если бот не инициализирован.
    """
    if _bot is None:
        raise RuntimeError("Telegram bot не инициализирован.")
    await _bot.send_message(chat_id=telegram_id, text=message)
    logger.debug("Сообщение отправлено в TG chat_id=%s", telegram_id)


# ---------------------------------------------------------------------------
# Обработчики команд
# ---------------------------------------------------------------------------


async def _cmd_start(message: types.Message) -> None:
    """Обработчик команды /start."""
    await message.answer(
        "👋 Привет! Я — ДругИИ, ваш персональный помощник приветствий.\n\n"
        "Чтобы я мог приветствовать вас, пожалуйста, представьтесь командой:\n"
        "/register Имя Фамилия Отчество\n\n"
        "Например: /register Иван Иванов Иванович"
    )


async def _cmd_register(message: types.Message) -> None:
    """
    Обработчик команды /register.

    Формат: /register Имя Фамилия Отчество
    Сохраняет или обновляет ФИО и telegram_id пользователя.
    """
    # Извлекаем текст после команды
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Пожалуйста, укажите ФИО после команды.\n"
            "Пример: /register Иван Иванов Иванович"
        )
        return

    full_name = parts[1].strip()
    if not full_name or len(full_name.split()) < 2:
        await message.answer(
            "Пожалуйста, укажите хотя бы имя и фамилию.\n"
            "Пример: /register Иван Иванов Иванович"
        )
        return

    telegram_id = message.from_user.id
    db = await get_db()

    try:
        # Проверяем, существует ли пользователь с таким telegram_id
        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        existing = await cursor.fetchone()

        if existing:
            await db.execute(
                "UPDATE users SET full_name = ?, updated_at = datetime('now') WHERE telegram_id = ?",
                (full_name, telegram_id),
            )
            await db.commit()
            await message.answer(f"✅ Данные обновлены, {full_name}! Теперь я буду вас узнавать.")
            logger.info("TG user %s обновил ФИО: %s", telegram_id, full_name)
        else:
            # Ищем пользователя с таким же ФИО, но без TG-контакта
            cursor = await db.execute(
                "SELECT id, full_name, max_chat_id, telegram_id FROM users WHERE full_name = ?",
                (full_name,),
            )
            twin = await cursor.fetchone()
            if twin and not twin["telegram_id"]:
                await db.execute(
                    "UPDATE users SET telegram_id = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (telegram_id, twin["id"]),
                )
                await db.commit()
                await message.answer(
                    f"✅ Нашёл ваш профиль, {full_name}!\n"
                    "Теперь ДругИИ знает вас и в Telegram, и в MAX."
                )
                logger.info(
                    "Пользователь #%s (%s) дополнен контактом TG %s",
                    twin["id"], full_name, telegram_id,
                )
            else:
                await db.execute(
                    "INSERT INTO users (full_name, telegram_id) VALUES (?, ?)",
                    (full_name, telegram_id),
                )
                await db.commit()
                await message.answer(f"✅ Приятно познакомиться, {full_name}! Я запомнил вас.")
                logger.info("TG user %s зарегистрирован: %s", telegram_id, full_name)
    except Exception as exc:
        logger.error("Ошибка регистрации TG user %s: %s", telegram_id, exc)
        await message.answer("❌ Произошла ошибка при регистрации. Попробуйте позже.")


async def _cmd_status(message: types.Message) -> None:
    """
    Обработчик команды /status.

    Показывает информацию о пользователе: привязанные устройства,
    последнее приветствие.
    """
    telegram_id = message.from_user.id
    db = await get_db()

    try:
        cursor = await db.execute(
            "SELECT id, full_name FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        user = await cursor.fetchone()
    except Exception as exc:
        logger.error("Ошибка получения пользователя %s: %s", telegram_id, exc)
        await message.answer("❌ Ошибка при получении данных.")
        return

    if user is None:
        await message.answer(
            "Вы ещё не зарегистрированы.\n"
            "Используйте /register Имя Фамилия Отчество"
        )
        return

    # Собираем ответ
    lines = [f"👤 *{user['full_name']}*"]
    lines.append("")

    # Устройства
    try:
        cursor = await db.execute(
            "SELECT mac_address, device_name FROM devices WHERE user_id = ? AND is_active = 1",
            (user["id"],),
        )
        devices = await cursor.fetchall()
    except Exception:
        devices = []

    if devices:
        lines.append("📱 *Привязанные устройства:*")
        for d in devices:
            name = d["device_name"] or "—"
            lines.append(f"  • `{d['mac_address']}` ({name})")
    else:
        lines.append("📱 Устройства не привязаны.")

    lines.append("")

    # Последнее приветствие
    try:
        cursor = await db.execute(
            """
            SELECT sent_at, message_text, sent_via
            FROM greetings
            WHERE user_id = ?
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            (user["id"],),
        )
        last_greeting = await cursor.fetchone()
    except Exception:
        last_greeting = None

    if last_greeting:
        lines.append("💬 *Последнее приветствие:*")
        lines.append(f"  • Время: {last_greeting['sent_at']}")
        lines.append(f"  • Канал: {last_greeting['sent_via']}")
        lines.append(f"  • Текст: _{last_greeting['message_text'][:80]}…_")
    else:
        lines.append("💬 Приветствий пока не было.")

    await message.answer("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Запуск бота
# ---------------------------------------------------------------------------


async def run_telegram_bot(config: Config) -> NoReturn:
    """
    Запускает Telegram бота (aiogram polling).

    Args:
        config: Конфигурация приложения.
    """
    global _bot

    if not config.bot_token_tg:
        logger.warning("BOT_TOKEN_TG не задан. Telegram бот отключён.")
        await asyncio.Event().wait()
        return  # noqa: B012  # never reached, but keeps type-checker happy

    logger.info("bot_telegram: запуск...")

    if config.tg_proxy:
        logger.info("bot_telegram: использую прокси %s", config.tg_proxy)
        session = AiohttpSession(proxy=config.tg_proxy)
        _bot = Bot(token=config.bot_token_tg, session=session)
    else:
        logger.info("bot_telegram: подключение напрямую (TG_PROXY не задан)")
        _bot = Bot(token=config.bot_token_tg)
    dp = Dispatcher()

    dp.message.register(_cmd_start, Command("start"))
    dp.message.register(_cmd_register, Command("register"))
    dp.message.register(_cmd_status, Command("status"))

    logger.info("bot_telegram: запущен (polling)")

    try:
        await dp.start_polling(_bot)
    finally:
        await _bot.session.close()
        _bot = None
        logger.info("bot_telegram: остановлен")