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

# Глобальная блокировка сканирования — BlueZ не позволяет
# двум клиентам сканировать одновременно
_scan_lock = asyncio.Lock()

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
            async with _scan_lock:
                logger.debug("Начало сканирования BLE (duration=%dс)...", scan_duration)
                devices = await BleakScanner.discover(timeout=scan_duration)
        except Exception as exc:
            logger.warning(
                "Ошибка при сканировании Bluetooth: %s. "
                "Повтор через %d с.",
                exc,
                config.scan_interval,
            )
            await asyncio.sleep(config.scan_interval)
            continue

        # devices — list[BLEDevice]
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


# ---------------------------------------------------------------------------
# Вспомогательные функции для живого сканирования (используются CLI и веб-UI)
# ---------------------------------------------------------------------------

# Ориентировочная мощность передатчика на расстоянии 1 м (dBm)
_TX_POWER = -59
# Показатель затухания сигнала в помещении
_PATH_LOSS_N = 2.2


def rssi_to_distance(rssi: int) -> float:
    """Грубая оценка расстояния до устройства по уровню сигнала (м)."""
    return 10 ** ((_TX_POWER - rssi) / (10 * _PATH_LOSS_N))


def rssi_label(rssi: int) -> str:
    """Человеческое описание близости устройства к сканеру."""
    if rssi >= -55:
        return "ВПЛОТНУЮ — поднесено к сканеру"
    if rssi >= -65:
        return "очень близко (~0.5 м)"
    if rssi >= -75:
        return "рядом (~1–2 м)"
    if rssi >= -85:
        return "в той же комнате"
    return "далеко"


async def live_scan(rounds: int = 2, scan_sec: float = 4.0) -> dict[str, dict]:
    """
    Живое BLE-сканирование через callback-API для получения RSSI.

    Выполняет несколько циклов сканирования и агрегирует лучший RSSI
    для каждого замеченного MAC-адреса.

    Args:
        rounds: Количество циклов сканирования.
        scan_sec: Длительность одного цикла (секунды).

    Returns:
        Словарь ``{mac: {"name": str | None, "best_rssi": int}}``.
    """
    try:
        from bleak import BleakScanner
    except ImportError:
        logger.warning("bleak не установлен — живое сканирование недоступно")
        return {}

# Ждём разблокировки сканера (фоновый сканер может быть занят)
    try:
        await asyncio.wait_for(_scan_lock.acquire(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("live_scan: не удалось захватить блокировку — сканер занят")
        return {}

    try:
        aggregated: dict[str, dict] = {}

        for round_no in range(1, rounds + 1):
            found: dict[str, tuple[int | None, str | None]] = {}

            def _on_device(device, adv) -> None:
                rssi = getattr(adv, "rssi", None)
                name = getattr(adv, "local_name", None) or getattr(device, "name", None)
                prev = found.get(device.address)
                if prev is None or (rssi is not None and (prev[0] is None or rssi > prev[0])):
                    found[device.address] = (rssi, name)

            scanner = BleakScanner(detection_callback=_on_device)
            await scanner.start()
            await asyncio.sleep(scan_sec)
            await scanner.stop()

            for mac, (rssi, name) in found.items():
                entry = aggregated.setdefault(mac, {"name": None, "best_rssi": None})
                if name and not entry["name"]:
                    entry["name"] = name
                if rssi is not None and (
                    entry["best_rssi"] is None or rssi > entry["best_rssi"]
                ):
                    entry["best_rssi"] = rssi

            logger.debug(
                "live_scan: цикл %d/%d — %d уникальных устройств",
                round_no, rounds, len(aggregated),
            )

        return aggregated
    finally:
        _scan_lock.release()