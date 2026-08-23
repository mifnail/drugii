"""
Модуль интеграции с GigaChat (Сбер).

Генерирует персонализированные приветствия через нейросеть.
При недоступности API автоматически возвращается к шаблонам.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Шаблоны-заглушки (используются, если GigaChat недоступен)
# ---------------------------------------------------------------------------

_FALLBACK_TEMPLATES: list[str] = [
    "Добро пожаловать, {name}! Мы очень рады видеть вас здесь сегодня.",
    "Приветствуем, {name}! Ваше присутствие делает этот день особенным.",
    "Здравствуйте, {name}! Всегда приятно, когда вы рядом.",
    "Рады приветствовать вас, {name}! Хорошего дня и отличного настроения!",
    "{name}, с возвращением! Мы уже заждались.",
    "Ура, {name} снова здесь! Добро пожаловать!",
    "{name}, привет! Мы счастливы видеть вас снова.",
    "Самые тёплые пожелания, {name}! Рады вашему приходу.",
]

# ---------------------------------------------------------------------------
# Промпт для GigaChat
# ---------------------------------------------------------------------------

_GREETING_SYSTEM_PROMPT = (
    "Ты — «ДругИИ», дружелюбный ассистент, который приветствует людей. "
    "Твоя задача — написать ОДНО тёплое, неформальное приветственное сообщение "
    "на русском языке. Обращайся к человеку по имени. "
    "Будь искренним и разнообразным в формулировках. "
    "НЕ используй обращения «Уважаемый», «Господин». "
    "НЕ добавляй подпись, эмодзи или дополнительный текст после приветствия. "
    "НЕ спрашивай «как дела» — просто поприветствуй. "
    "Длина: 1–2 предложения, не более 200 символов."
)


def _is_configured(config: Config) -> bool:
    """Проверяет, заданы ли все параметры GigaChat."""
    return bool(config.gigachat_credentials)


def _patch_no_proxy() -> None:
    """Добавляет api.giga.chat в NO_PROXY (htpx использует системный прокси)."""
    current = os.environ.get("NO_PROXY", "")
    host = "api.giga.chat"
    if host not in current:
        os.environ["NO_PROXY"] = f"{current},{host}".strip(",")


async def generate_ai_greeting(
    config: Config,
    full_name: str,
    device_name: str | None = None,
) -> str | None:
    """
    Генерирует приветствие через GigaChat.

    Если GigaChat не настроен или вернул ошибку — возвращает None
    (вызывающий код должен использовать шаблон-заглушку).

    Args:
        config: Конфигурация приложения.
        full_name: ФИО пользователя.
        device_name: Имя BLE-устройства (опционально).

    Returns:
        Строка приветствия или None.
    """
    if not _is_configured(config):
        return None

    # Берём только имя (первое слово)
    short_name = full_name.split()[0] if full_name else "друг"

    user_prompt = (
        f"Поприветствуй человека по имени {short_name}."
    )
    if device_name:
        user_prompt += (
            f" Его устройство «{device_name}» только что появилось рядом — "
            f"можешь игриво упомянуть это."
        )

    _patch_no_proxy()

    def _sync_call() -> str:
        from gigachat import GigaChat  # type: ignore[import-untyped]
        from gigachat.models import Chat, Messages, MessagesRole  # type: ignore[import-untyped]

        client = GigaChat(
            base_url=config.gigachat_base_url,
            credentials=config.gigachat_credentials,
            scope=config.gigachat_scope,
            model=config.gigachat_model,
            verify_ssl_certs=False,
            timeout=15.0,
        )

        chat = Chat(
            model=config.gigachat_model,
            messages=[
                Messages(role=MessagesRole.SYSTEM, content=_GREETING_SYSTEM_PROMPT),
                Messages(role=MessagesRole.USER, content=user_prompt),
            ],
        )
        resp = client.chat(chat)
        return resp.choices[0].message.content.strip()

    try:
        text = await asyncio.to_thread(_sync_call)
        if text:
            return text
    except Exception as exc:
        logger.warning("GigaChat недоступен (fallback на шаблоны): %s", exc)

    return None


def generate_fallback_greeting(full_name: str) -> str:
    """Шаблонное приветствие (заглушка при недоступности ИИ)."""
    import random
    short_name = full_name.split()[0] if full_name else "Дорогой друг"
    return random.choice(_FALLBACK_TEMPLATES).format(name=short_name)