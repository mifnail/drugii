"""
Модуль MAX бота.

Два режима приёма обновлений от платформы MAX (MAX_MODE в .env):

- ``longpoll`` (по умолчанию) — периодический GET ``/updates``.
  Не требует домена, HTTPS и публичного IP — работает полностью локально.
- ``webhook`` — aiohttp-сервер для входящих запросов платформы.
  Требует публичный HTTPS URL (WEBHOOK_PUBLIC_URL) и секрет (WEBHOOK_SECRET).

Отправка сообщений всегда идёт напрямую через ``{max_api_url}/messages``
(без прокси; TG_PROXY на MAX не влияет).
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any, NoReturn

import aiohttp
import aiohttp.web

from app.config import Config
from app.database import get_db

logger = logging.getLogger(__name__)

# Глобальное состояние модуля
_session: aiohttp.ClientSession | None = None
_config: Config | None = None
_ssl_context: ssl.SSLContext | None = None


def _get_ssl_context() -> ssl.SSLContext | None:
    """
    Возвращает SSL-контекст с полным набором корневых сертификатов.

    MAX использует российский национальный ЦС («Russian Trusted Root CA»
    Минцифры), которого нет в наборе Mozilla/certifi. Поэтому базой служит
    системное хранилище (с установленными российскими сертификатами),
    а certifi добавляется поверх для остальных сайтов.
    """
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context

    # Системное хранилище (включая Russian Trusted CA после установки)
    _ssl_context = ssl.create_default_context()
    try:
        import certifi

        _ssl_context.load_verify_locations(cafile=certifi.where())
        logger.debug("SSL: системное хранилище + certifi")
    except ImportError:
        logger.debug("SSL: системное хранилище сертификатов")
    return _ssl_context


async def _get_session() -> aiohttp.ClientSession:
    """Возвращает глобальную aiohttp-сессию (создаёт при необходимости)."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(ssl=_get_ssl_context())
        _session = aiohttp.ClientSession(connector=connector)
    return _session


def _auth_headers(config: Config) -> dict[str, str]:
    """Заголовки авторизации для запросов к MAX API."""
    return {
        "Authorization": f"{config.bot_token_max}",
        "Content-Type": "application/json",
    }


async def send_greeting_max(chat_id: str, message: str) -> None:
    """
    Отправляет сообщение пользователю через MAX API (напрямую, без прокси).

    Args:
        chat_id: ID чата пользователя в MAX.
        message: Текст сообщения.

    Raises:
        RuntimeError: Если бот не инициализирован.
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

    # Пытаемся сначала как chat_id, затем (для числовых значений) как user_id.
    # Разные события платформы отдают разные идентификаторы.
    attempts: list[dict[str, Any]] = [{"chat_id": chat_id}]
    if str(chat_id).isdigit():
        attempts.append({"user_id": int(chat_id)})

    last_error: Exception | None = None
    for payload in attempts:
        async with session.post(
            url, headers=_auth_headers(_config), json={**payload, "text": message}
        ) as resp:
            if resp.status < 400:
                logger.debug(
                    "Сообщение отправлено в MAX (%s=%s)",
                    next(iter(payload)), chat_id,
                )
                return
            body = await resp.text()
            last_error = aiohttp.ClientError(
                f"MAX API вернул {resp.status}: {body}"
            )
            # Unknown recipient — пробуем следующий вариант идентификатора
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# Разбор обновлений
# ---------------------------------------------------------------------------


def _extract_chat_id(update: dict[str, Any]) -> str:
    """
    Извлекает chat_id из объекта Update в максимально терпимом формате.

    Приоритет источников:
    1. update.chat_id
    2. message.sender.chat_id — диалог отправителя с ботом
    3. message.recipient.chat_id
    4. from/user.id (последний ресурс: это user_id, а не chat_id,
       send_greeting_max сделает фолбэк на user_id при ошибке)
    """
    chat_id = update.get("chat_id")
    if chat_id:
        return str(chat_id)

    message = update.get("message") or {}
    sender = message.get("sender") or {}
    if sender.get("chat_id"):
        return str(sender["chat_id"])

    recipient = message.get("recipient") or {}
    if recipient.get("chat_id"):
        return str(recipient["chat_id"])

    for holder in (update, message):
        for key in ("from", "user", "sender"):
            obj = holder.get(key) or {}
            if isinstance(obj, dict) and obj.get("id"):
                return str(obj["id"])

    return ""


def _extract_text(update: dict[str, Any]) -> str:
    """Извлекает текст сообщения из объекта Update."""
    message = update.get("message") or {}
    body = message.get("body") or {}
    text = body.get("text") or update.get("text") or ""
    return str(text)


def _normalize_command(text: str) -> str:
    """
    Приводит команды вида ``/register ...`` к простому тексту ФИО.

    Позволяет пользователям MAX использовать ту же команду, что и в Telegram.
    """
    stripped = text.strip()
    for prefix in ("/register", "/start", "/status"):
        if stripped.lower().startswith(prefix):
            stripped = stripped[len(prefix):].strip()
            break
    return stripped


def _extract_event_type(update: dict[str, Any]) -> str:
    """Извлекает тип события из объекта Update."""
    return str(update.get("update_type") or update.get("type") or "")


# ---------------------------------------------------------------------------
# Бизнес-логика обработки событий
# ---------------------------------------------------------------------------


async def _handle_update(event_type: str, chat_id: str, text: str) -> None:
    """
    Обрабатывает одно событие от пользователя MAX.

    Поддерживаемые сценарии:
    - ``bot_started`` — регистрация по chat_id + приветствие.
    - «статус» — сводка по пользователю.
    - Сообщение из 2–3 слов — трактуется как ФИО и сохраняется.

    Args:
        event_type: Тип события (bot_started, message_created, ...).
        chat_id: ID чата пользователя.
        text: Текст сообщения (может быть пустым).
    """
    db = await get_db()

    if event_type == "bot_started":
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
        return

    if text.strip().lower() == "статус":
        try:
            cursor = await db.execute(
                "SELECT id, full_name FROM users WHERE max_chat_id = ?",
                (chat_id,),
            )
            user = await cursor.fetchone()
        except Exception as exc:
            logger.error("Ошибка получения пользователя MAX %s: %s", chat_id, exc)
            return

        if user is None:
            status_text = (
                "Вы ещё не зарегистрированы.\n"
                "Отправьте своё ФИО, чтобы я вас запомнил."
            )
        else:
            lines = [f"👤 {user['full_name']}"]
            try:
                cursor = await db.execute(
                    "SELECT mac_address, device_name FROM devices "
                    "WHERE user_id = ? AND is_active = 1",
                    (user["id"],),
                )
                devices = await cursor.fetchall()
            except Exception:
                devices = []

            if devices:
                lines.append("📱 Устройства:")
                for d in devices:
                    lines.append(f"  • {d['mac_address']} ({d['device_name'] or '—'})")
            else:
                lines.append("📱 Устройства не привязаны.")

            try:
                cursor = await db.execute(
                    "SELECT sent_at FROM greetings WHERE user_id = ? "
                    "ORDER BY sent_at DESC LIMIT 1",
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
        return

    if text.strip():
        # Текстовое сообщение — пытаемся распознать ФИО
        words = text.strip().split()
        if len(words) >= 2:
            full_name = " ".join(words[:3])
            try:
                cursor = await db.execute(
                    "SELECT id FROM users WHERE max_chat_id = ?",
                    (chat_id,),
                )
                existing = await cursor.fetchone()
                if existing:
                    await db.execute(
                        "UPDATE users SET full_name = ?, updated_at = datetime('now') "
                        "WHERE max_chat_id = ?",
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
            try:
                await send_greeting_max(
                    chat_id,
                    "Я вас не совсем понял. Если хотите представиться, "
                    "напишите своё имя и фамилию.\n"
                    "Либо отправьте «статус» для проверки.",
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Long polling режим
# ---------------------------------------------------------------------------


async def _run_long_polling(config: Config) -> NoReturn:
    """
    Бесконечный цикл получения обновлений через GET ``/updates``.

    Не требует домена и публичного HTTPS — работает локально за NAT.

    Args:
        config: Конфигурация приложения.
    """
    session = await _get_session()
    marker: str | None = None
    backoff = 5

    logger.info("bot_max: long polling запущен (%s/updates)", config.max_api_url)

    while True:
        params: dict[str, str | int] = {
            "timeout": 30,
            "types": "message_created,bestarted,bot_started",
        }
        if marker:
            params["marker"] = marker

        try:
            async with session.get(
                f"{config.max_api_url}/updates",
                headers=_auth_headers(config),
                params=params,
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "MAX updates вернул %s: %s. Повтор через %d c.",
                        resp.status, body[:200], backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                data = await resp.json(content_type=None)
                backoff = 5

            updates = data.get("updates") or []
            marker = data.get("update_marker") or marker

            for update in updates:
                event_type = _extract_event_type(update)
                chat_id = _extract_chat_id(update)
                text = _extract_text(update)

                if not chat_id:
                    logger.warning("Обновление без chat_id (event=%s)", event_type)
                    continue

                logger.debug(
                    "MAX update (event=%s, chat_id=%s)", event_type, chat_id
                )
                try:
                    await _handle_update(
                        event_type, chat_id, _normalize_command(text)
                    )
                except Exception as exc:
                    logger.error("Ошибка обработки обновления MAX: %s", exc)

        except asyncio.CancelledError:
            logger.info("bot_max: long polling остановлен.")
            raise
        except Exception as exc:
            logger.warning(
                "Ошибка long polling MAX: %s. Повтор через %d c.", exc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Webhook режим
# ---------------------------------------------------------------------------


async def _handle_max_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """
    Обрабатывает входящий webhook-запрос от платформы MAX.

    Аутентификация: заголовок ``Authorization: Bearer <WEBHOOK_SECRET>``,
    если секрет задан в конфигурации.
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
    except Exception as exc:
        logger.warning("Некорректный webhook-запрос: %s", exc)
        return aiohttp.web.json_response(
            {"ok": False, "error": "invalid json"}, status=400
        )

    event_type = str(body.get("event") or body.get("type", ""))
    raw_chat_id = body.get("chat_id") or body.get("from", {}).get("id", "")
    text = _normalize_command(str(body.get("text", "")))

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

    try:
        await _handle_update(event_type, str(raw_chat_id), text)
    except Exception as exc:
        logger.error("Ошибка обработки webhook: %s", exc)
        return aiohttp.web.json_response({"ok": False, "error": str(exc)}, status=500)

    return aiohttp.web.json_response({"ok": True})


async def _register_webhook(config: Config) -> None:
    """
    Регистрирует webhook на платформе MAX (POST ``/subscriptions``).
    Только для режима webhook при наличии WEBHOOK_PUBLIC_URL.
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
    payload = {
        "url": webhook_url,
        "event_types": ["message_created", "bot_started"],
    }

    try:
        async with session.post(url, headers=_auth_headers(config), json=payload) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning("Ошибка регистрации webhook: %s %s", resp.status, body)
            else:
                logger.info("Webhook зарегистрирован: %s", webhook_url)
    except Exception as exc:
        logger.warning("Не удалось зарегистровать webhook: %s", exc)


async def _run_webhook_mode(config: Config) -> NoReturn:
    """
    Запускает aiohttp webhook сервер (режим ``webhook``).
    """
    logger.info(
        "bot_max: запуск webhook сервера на %s:%d...",
        config.webhook_host,
        config.webhook_port,
    )

    app = aiohttp.web.Application()
    app.router.add_post("/max/webhook", _handle_max_webhook)

    await _register_webhook(config)

    runner = aiohttp.web.AppRunner(app)
    try:
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, config.webhook_host, config.webhook_port)
        await site.start()
        logger.info(
            "bot_max: запущен (webhook=%s:%d)", config.webhook_host, config.webhook_port
        )
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("bot_max: получен сигнал остановки...")
    except Exception as exc:
        logger.error("bot_max: ошибка запуска сервера: %s", exc)
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Точка входа модуля
# ---------------------------------------------------------------------------


async def run_max_bot(config: Config) -> NoReturn:
    """
    Запускает MAX бота в режиме, заданном ``MAX_MODE``:

    - ``longpoll`` (по умолчанию) — GET ``/updates``, работает локально;
    - ``webhook`` — HTTP-сервер для платформы.

    Args:
        config: Конфигурация приложения.
    """
    global _config

    _config = config

    if not config.bot_token_max:
        logger.warning("BOT_TOKEN_MAX не задан. MAX бот отключён.")
        await asyncio.Event().wait()
        return  # noqa: B012

    try:
        if config.max_mode == "webhook":
            await _run_webhook_mode(config)
        else:
            await _run_long_polling(config)
    finally:
        if _session is not None and not _session.closed:
            await _session.close()
        logger.info("bot_max: остановлен")
