"""
Модуль MAX бота.

Запускает aiohttp webhook сервер для приёма запросов от MAX
и отправляет приветствия пользователям через MAX API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import NoReturn

import aiohttp
import aiohttp.web

from app.config import Config
from app.database import get_db

logger = logging.getLogger(__name__)

# Глобальный объект сессии aiohttp для отправки сообщений
_session: aiohttp.ClientSession | None = None
_config: Config | None = None


async def _get_session() -> aiohttp.ClientSession:
    """Возвращает глобальную aiohttp сессию (создаёт при необходимости)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def send_greeting_max(chat_id: str, message: str) -> None:
    """
    Отправляет приветственное сообщение пользователю через MAX API.

    URL: ``{max_api_url}/messages``
    Заголовки: ``Authorization: {token}``, ``Content-Type: application/json``
    Тело: ``{"chat_id": chat_id, "text": message}``

    Args:
        chat_id: ID чата пользователя в MAX.
        message: Текст сообщения.

    Raises:
        RuntimeError: Если конфигурация не загружена.
        aiohttp.ClientError: При ошибке HTTP-запроса.
    """
    global _config
    if _config is None:
        raise RuntimeError("MAX bot не инициализирован.")

    if not _config.bot_token_max:
        logger.warning("BOT_TOKEN_MAX не задан. Пропускаем отправку в MAX.")
        return

    session = await _get_session()
    url = f"{_config.max_api_url}/messages"
    headers = {
        "Authorization": f"{_config.bot_token_max}",
        "Content-Type": "application/json",
    }
    payload = {"chat_id": chat_id, "text": message}

    async with session.post(url, headers=headers, json=payload) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise aiohttp.ClientError(
                f"MAX API вернул {resp.status}: {body}"
            )
        logger.debug("Сообщение отправлено в MAX chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# Регистрация webhook
# ---------------------------------------------------------------------------


async def _register_webhook(config: Config) -> None:
    """
    Регистрирует webhook на платформе MAX.

    POST ``/subscriptions`` с URL вида ``http://{host}:{port}/max/webhook``.

    Args:
        config: Конфигурация приложения.
    """
    if not config.bot_token_max:
        logger.info("BOT_TOKEN_MAX не задан. Webhook не регистрируем.")
        return

    if not config.webhook_public_url:
        logger.info(
            "WEBHOOK_PUBLIC_URL не задан — пропускаем регистрацию webhook "
            "на платформе MAX (платформа требует публичный HTTPS URL)."
        )
        return

    session = await _get_session()
    webhook_url = f"{config.webhook_public_url.rstrip('/')}/max/webhook"
    url = f"{config.max_api_url}/subscriptions"
    headers = {
        "Authorization": f"{config.bot_token_max}",
        "Content-Type": "application/json",
    }
    payload = {
        "url": webhook_url,
        "event_types": ["message", "bot_started"],
    }

    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning("Ошибка регистрации webhook: %s %s", resp.status, body)
            else:
                logger.info("Webhook зарегистрирован: %s", webhook_url)
    except Exception as exc:
        logger.warning("Не удалось зарегистрировать webhook: %s", exc)


# ---------------------------------------------------------------------------
# Обработка входящих запросов от MAX
# ---------------------------------------------------------------------------


async def _handle_max_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """
    Обрабатывает входящие webhook-запросы от платформы MAX.

    Поддерживаемые события:
    - ``bot_started`` — регистрация пользователя по chat_id.
    - Текстовое сообщение — если содержит ФИО, регистрирует пользователя.
    - Команда ``статус`` — показывает статус пользователя.

    Args:
        request: Входящий HTTP-запрос.
    """
    global _config

    # --- Аутентификация webhook ---
    if _config and _config.webhook_secret:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {_config.webhook_secret}"
        if auth != expected:
            logger.warning("Webhook-запрос отклонён: неверная аутентификация.")
            return aiohttp.web.json_response(
                {"ok": False, "error": "unauthorized"}, status=401
            )

    # --- Разбор тела запроса ---
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Некорректный webhook-запрос: %s", exc)
        return aiohttp.web.json_response(
            {"ok": False, "error": "invalid json"}, status=400
        )

    event_type = str(body.get("event") or body.get("type", ""))
    raw_chat_id = body.get("chat_id") or body.get("from", {}).get("id", "")
    text = str(body.get("text", ""))

    # --- Валидация chat_id ---
    if not raw_chat_id:
        logger.warning("Webhook без chat_id (event=%s)", event_type)
        return aiohttp.web.json_response(
            {"ok": False, "error": "no chat_id"}, status=400
        )

    if not isinstance(raw_chat_id, (str, int)):
        logger.warning("Webhook с некорректным chat_id типа %s", type(raw_chat_id))
        return aiohttp.web.json_response(
            {"ok": False, "error": "invalid chat_id"}, status=400
        )

    chat_id = str(raw_chat_id)

    logger.debug(
        "Получен webhook от MAX (event=%s, chat_id=%s)", event_type, chat_id
    )

    db = await get_db()

    if event_type == "bot_started":
        # Регистрация пользователя (можно без ФИО)
        try:
            cursor = await db.execute(
                "SELECT id FROM users WHERE max_chat_id = ?",
                (chat_id,),
            )
            existing = await cursor.fetchone()
            if existing:
                logger.info("MAX chat %s уже зарегистрирован.", chat_id)
            else:
                await db.execute(
                    "INSERT INTO users (full_name, max_chat_id) VALUES (?, ?)",
                    (f"MAX-User-{chat_id}", chat_id),
                )
                await db.commit()
                logger.info("MAX chat %s зарегистрирован.", chat_id)
                try:
                    await send_greeting_max(
                        chat_id,
                        "👋 Добро пожаловать в ДругИИ!\n"
                        "Пожалуйста, представьтесь: отправьте сообщение "
                        "с вашим именем и фамилией (например, «Иван Иванов»).",
                    )
                except Exception as exc:
                    logger.error("Ошибка отправки приветствия в MAX: %s", exc)
        except Exception as exc:
            logger.error("Ошибка регистрации MAX chat %s: %s", chat_id, exc)
            return aiohttp.web.json_response(
                {"ok": False, "error": str(exc)}, status=500
            )

    elif text.strip().lower() == "статус":
        # Команда статус
        try:
            cursor = await db.execute(
                "SELECT id, full_name FROM users WHERE max_chat_id = ?",
                (chat_id,),
            )
            user = await cursor.fetchone()
        except Exception as exc:
            logger.error("Ошибка получения пользователя MAX %s: %s", chat_id, exc)
            return aiohttp.web.json_response({"ok": False, "error": str(exc)}, status=500)

        if user is None:
            status_text = (
                "Вы ещё не зарегистрированы.\n"
                "Отправьте своё ФИО, чтобы я вас запомнил."
            )
        else:
            lines = [f"👤 *{user['full_name']}*"]
            try:
                cursor = await db.execute(
                    "SELECT mac_address, device_name FROM devices WHERE user_id = ? AND is_active = 1",
                    (user["id"],),
                )
                devices = await cursor.fetchall()
            except Exception:
                devices = []

            if devices:
                lines.append("📱 *Устройства:*")
                for d in devices:
                    lines.append(f"  • {d['mac_address']} ({d['device_name'] or '—'})")
            else:
                lines.append("📱 Устройства не привязаны.")

            try:
                cursor = await db.execute(
                    "SELECT sent_at FROM greetings WHERE user_id = ? ORDER BY sent_at DESC LIMIT 1",
                    (user["id"],),
                )
                last = await cursor.fetchone()
                lines.append(f"💬 Последнее приветствие: {last['sent_at'] if last else 'не было'}")
            except Exception:
                pass

            status_text = "\n".join(lines)

        try:
            await send_greeting_max(chat_id, status_text)
        except Exception as exc:
            logger.error("Ошибка отправки статуса в MAX %s: %s", chat_id, exc)

    elif text.strip():
        # Текстовое сообщение — пытаемся распознать ФИО
        words = text.strip().split()
        if len(words) >= 2:
            # Есть как минимум имя и фамилия
            full_name = " ".join(words[:3])  # берём до 3 слов
            try:
                cursor = await db.execute(
                    "SELECT id FROM users WHERE max_chat_id = ?",
                    (chat_id,),
                )
                existing = await cursor.fetchone()
                if existing:
                    await db.execute(
                        "UPDATE users SET full_name = ?, updated_at = datetime('now') WHERE max_chat_id = ?",
                        (full_name, chat_id),
                    )
                    await db.commit()
                    reply = f"✅ Данные обновлены, {words[0]}!"
                else:
                    await db.execute(
                        "INSERT INTO users (full_name, max_chat_id) VALUES (?, ?)",
                        (full_name, chat_id),
                    )
                    await db.commit()
                    reply = f"✅ Приятно познакомиться, {words[0]}! Я вас запомнил."
                try:
                    await send_greeting_max(chat_id, reply)
                except Exception as exc:
                    logger.error("Ошибка ответа в MAX: %s", exc)
            except Exception as exc:
                logger.error("Ошибка обработки ФИО в MAX: %s", exc)
        else:
            # Мало слов — просто игнорируем или отвечаем
            try:
                await send_greeting_max(
                    chat_id,
                    "Я вас не совсем понял. Если хотите представиться, "
                    "напишите своё имя и фамилию.\n"
                    "Либо отправьте «статус» для проверки.",
                )
            except Exception:
                pass

    return aiohttp.web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Запуск MAX бота
# ---------------------------------------------------------------------------


async def run_max_bot(config: Config) -> NoReturn:
    """
    Запускает MAX бот: aiohttp webhook сервер и регистрацию webhook.

    Args:
        config: Конфигурация приложения.
    """
    global _config

    _config = config

    if not config.bot_token_max:
        logger.warning("BOT_TOKEN_MAX не задан. MAX бот отключён.")
        await asyncio.Event().wait()
        return  # noqa: B012

    logger.info(
        "bot_max: запуск webhook сервера на %s:%d...",
        config.webhook_host,
        config.webhook_port,
    )

    app = aiohttp.web.Application()
    app.router.add_post("/max/webhook", _handle_max_webhook)

    # Регистрируем webhook на MAX
    try:
        await _register_webhook(config)
    except Exception as exc:
        logger.warning("Не удалось зарегистрировать webhook: %s", exc)
        logger.info("Работаем без webhook — для приёма сообщений нужна ручная регистрация.")

    runner = aiohttp.web.AppRunner(app)

    try:
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, config.webhook_host, config.webhook_port)
        await site.start()
        logger.info("bot_max: запущен (webhook=%s:%d)", config.webhook_host, config.webhook_port)

        # Держим сервер до отмены
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("bot_max: получен сигнал остановки...")
    except Exception as exc:
        logger.error("bot_max: ошибка запуска сервера: %s", exc)
    finally:
        await runner.cleanup()
        if _session is not None and not _session.closed:
            await _session.close()
        logger.info("bot_max: остановлен")