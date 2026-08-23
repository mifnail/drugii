"""
Модуль Bluetooth сканера.

Отвечает за периодическое сканирование BLE-устройств через bleak,
запись обнаружений в БД и передачу данных в движок приветствий.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import NoReturn

from app.config import Config
from app.database import get_db
from app.greeting import process_new_detection

logger = logging.getLogger(__name__)

# Регулярное выражение для валидации MAC-адреса (XX:XX:XX:XX:XX:XX)
_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def _validate_mac(mac: str) -> bool:
    """
    Проверяет, соответствует ли строка формату MAC-адреса.

    Args:
        mac: Нормализованная строка MAC-адреса.

    Returns:
        True, если формат корректен.
    """
    return bool(_MAC_RE.match(mac))


def _sanitize_device_name(raw: str | None, max_length: int = 100) -> str | None:
    """
    Очищает имя BLE-устройства: обрезает длину, удаляет управляющие символы.

    Args:
        raw: Сырое имя устройства от BLE.
        max_length: Максимальная допустимая длина.

    Returns:
        Очищенная строка или None.
    """
    if raw is None:
        return None
    # Удаляем управляющие символы (кроме пробелов и печатных)
    clean = "".join(ch for ch in raw if ch.isprintable() or ch in " ").strip()
    if not clean:
        return None
    return clean[:max_length]


def _normalize_mac(raw: str) -> str:
    """
    Приводит MAC-адрес к формату ``XX:XX:XX:XX:XX:XX`` (верхний регистр).

    Args:
        raw: Сырой MAC-адрес (например, ``aa:bb:cc:dd:ee:ff`` или ``AA-BB-CC-DD-EE-FF``).

    Returns:
        Нормализованный MAC-адрес.
    """
    clean = raw.replace("-", ":").replace(" ", "").upper()
    # Если уже есть двоеточия — проверяем и возвращаем
    if ":" in clean:
        return clean
    # Иначе разбиваем по 2 символа
    parts = [clean[i: i + 2] for i in range(0, len(clean), 2)]
    return ":".join(parts)


async def _write_detections(
    mac_address: str,
    device_name: str | None,
    rssi: int | None,
) -> int | None:
    """
    Записывает обнаружение устройства в таблицу ``detections``.

    Args:
        mac_address: MAC-адрес в формате XX:XX:XX:XX:XX:XX.
        device_name: Имя устройства (может быть None).
        rssi: Уровень сигнала (может быть None).

    Returns:
        ID созданной записи или None при ошибке.
    """
    db = await get_db()
    now = datetime.now(timezone.utc)
    try:
        cursor = await db.execute(
            """
            INSERT INTO detections (mac_address, device_name, rssi, detected_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                mac_address,
                _sanitize_device_name(device_name),
                rssi,
                now.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        await db.commit()
        return cursor.lastrowid
    except Exception as exc:
        logger.error("Ошибка записи обнаружения %s: %s", mac_address, exc)
        return None


async def handle_new_detections(config: Config) -> None:
    """
    Обрабатывает обнаружения, для которых ещё не было отправлено приветствие.

    Ищет в таблице ``detections`` записи, у которых нет связанной записи
    в ``greetings.greetings``, и для каждой запускает полный цикл приветствия.

    Args:
        config: Конфигурация приложения.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT d.id, d.mac_address, d.device_name, d.rssi
            FROM detections d
            LEFT JOIN greetings g ON g.detection_id = d.id
            WHERE g.id IS NULL
            ORDER BY d.detected_at ASC
            """
        )
        rows = await cursor.fetchall()
    except Exception as exc:
        logger.error("Ошибка поиска необработанных обнаружений: %s", exc)
        return

    if not rows:
        return

    logger.info("Найдено %d необработанных обнаружений.", len(rows))

    for row in rows:
        try:
            await process_new_detection(
                detection_id=row["id"],
                mac_address=row["mac_address"],
                device_name=row["device_name"],
                rssi=row["rssi"],
                config=config,
            )
        except Exception as exc:
            logger.error(
                "Ошибка обработки обнаружения #%s (%s): %s",
                row["id"],
                row["mac_address"],
                exc,
            )


async def run_scanner(config: Config) -> NoReturn:
    """
    Запускает бесконечный цикл сканирования BLE-устройств.

    - Использует библиотеку ``bleak`` для сканирования.
    - Длительность одного сканирования = ``scan_interval / 2``.
    - Найденные устройства записываются в таблицу ``detections``.
    - После каждого сканирования вызывает :func:`handle_new_detections`.
    - Между сканированиями ожидает ``scan_interval`` секунд.

    Args:
        config: Конфигурация приложения.
    """
    logger.info(
        "scanner: запущен (интервал=%dс, cooldown=%dс, min_age=%dс)",
        config.scan_interval,
        config.greeting_cooldown,
        config.greeting_min_age,
    )

    # Пробуем импортировать bleak — если нет, сканер не работает
    try:
        from bleak import BleakScanner
    except ImportError:
        logger.error(
            "Библиотека bleak не установлена. "
            "BLE-сканер не будет работать. "
            "Установите: pip install bleak"
        )
        # Всё равно продолжаем — handle_new_detections может обработать
        # обнаружения, записанные другим способом
        while True:
            await asyncio.sleep(config.scan_interval)

    scan_duration = max(1, config.scan_interval // 2)

    while True:
        try:
            logger.debug("Начало сканирования BLE (duration=%dс)...", scan_duration)
            devices = await BleakScanner.discover(
                timeout=scan_duration,
                return_adv=True,
            )
        except Exception as exc:
            logger.warning(
                "Ошибка при сканировании Bluetooth: %s. "
                "Повтор через %d с.",
                exc,
                config.scan_interval,
            )
            await asyncio.sleep(config.scan_interval)
            continue

        # devices — dict[bleak.BLEDevice, AdvertisementData]
        # или list[BLEDevice] в зависимости от return_adv
        if isinstance(devices, dict):
            found = list(devices.keys())
        else:
            found = list(devices)

        logger.info("Обнаружено BLE-устройств: %d", len(found))

        for dev in found:
            try:
                mac = _normalize_mac(dev.address)
                if not _validate_mac(mac):
                    logger.debug("Пропущен некорректный MAC: %s", dev.address)
                    continue
                name = _sanitize_device_name(dev.name or None)
                rssi_val = getattr(dev, "rssi", None)
                await _write_detections(mac, name, rssi_val)
            except Exception as exc:
                logger.error(
                    "Ошибка обработки устройства %s: %s",
                    getattr(dev, "address", "?"),
                    exc,
                )

        # Обрабатываем новые обнаружения (те, что без greeting)
        try:
            await handle_new_detections(config)
        except Exception as exc:
            logger.error("Ошибка в handle_new_detections: %s", exc)

        # Ожидание до следующего цикла
        logger.debug("Ожидание %d с до следующего сканирования...", config.scan_interval)
        await asyncio.sleep(config.scan_interval)