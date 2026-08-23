"""
Модуль CLI для оператора.

Предоставляет интерфейс командной строки для управления
пользователями, устройствами и просмотра логов обнаружений и приветствий.
"""

from __future__ import annotations

import asyncio
import logging
from typing import NoReturn

import click

from app.config import Config
from app.database import get_db, init_db

logger = logging.getLogger(__name__)


def _with_db(fn):
    """
    Обёртка для асинхронных команд CLI: инициализирует БД перед выполнением.

    Args:
        fn: Асинхронная функция команды.
    """
    async def wrapper() -> None:
        await init_db(Config.load())
        await fn()
    return wrapper

# ---------------------------------------------------------------------------
# Вспомогательные функции (работа с БД)
# ---------------------------------------------------------------------------


async def _get_all_users() -> list[dict]:
    """Возвращает список всех пользователей."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, full_name, telegram_id, max_chat_id, created_at FROM users ORDER BY id"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _get_user_by_id(user_id: int) -> dict | None:
    """Возвращает пользователя по ID."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, full_name, telegram_id, max_chat_id, created_at FROM users WHERE id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _add_user(full_name: str, telegram_id: int | None, max_chat_id: str | None) -> int:
    """Добавляет пользователя и возвращает его ID."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO users (full_name, telegram_id, max_chat_id) VALUES (?, ?, ?)",
        (full_name, telegram_id, max_chat_id),
    )
    await db.commit()
    return cursor.lastrowid


async def _get_all_devices() -> list[dict]:
    """Возвращает список всех устройств."""
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT d.id, d.mac_address, d.device_name, d.user_id, d.is_active,
               u.full_name AS user_name
        FROM devices d
        LEFT JOIN users u ON u.id = d.user_id
        ORDER BY d.id
        """
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _add_device(mac_address: str, device_name: str | None, user_id: int) -> int:
    """Привязывает устройство к пользователю и возвращает ID устройства."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO devices (mac_address, device_name, user_id, is_active) VALUES (?, ?, ?, 1)",
        (mac_address, device_name, user_id),
    )
    await db.commit()
    return cursor.lastrowid


async def _deactivate_device(device_id: int) -> bool:
    """Деактивирует устройство (is_active=0). Возвращает True, если строка изменена."""
    db = await get_db()
    cursor = await db.execute(
        "UPDATE devices SET is_active = 0 WHERE id = ?",
        (device_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def _get_recent_detections(limit: int) -> list[dict]:
    """Возвращает последние N обнаружений."""
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT d.id, d.mac_address, d.device_name, d.rssi, d.detected_at,
               g.id IS NOT NULL AS has_greeting
        FROM detections d
        LEFT JOIN greetings g ON g.detection_id = d.id
        ORDER BY d.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _get_recent_greetings(limit: int) -> list[dict]:
    """Возвращает последние N приветствий."""
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT g.id, g.user_id, g.device_id, g.detection_id,
               g.sent_via, g.message_text, g.sent_at,
               u.full_name AS user_name
        FROM greetings g
        LEFT JOIN users u ON u.id = g.user_id
        ORDER BY g.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _send_test_greeting(user_id: int) -> str:
    """Отправляет тестовое приветствие пользователю."""
    from app.greeting import generate_greeting
    from app.bot_max import send_greeting_max
    from app.bot_telegram import send_greeting_telegram

    db = await get_db()
    cursor = await db.execute(
        "SELECT full_name, telegram_id, max_chat_id FROM users WHERE id = ?",
        (user_id,),
    )
    user = await cursor.fetchone()
    if user is None:
        return f"Пользователь с ID {user_id} не найден."

    text = generate_greeting(user["full_name"])
    sent = []

    if user["telegram_id"]:
        try:
            await send_greeting_telegram(user["telegram_id"], text)
            sent.append("telegram")
        except Exception as exc:
            sent.append(f"telegram_err:{exc}")

    if user["max_chat_id"]:
        try:
            await send_greeting_max(user["max_chat_id"], text)
            sent.append("max")
        except Exception as exc:
            sent.append(f"max_err:{exc}")

    if not sent:
        return f"У пользователя {user['full_name']} нет привязанных мессенджеров."

    return f"Тестовое приветствие отправлено {user['full_name']} через: {', '.join(sent)}"


# ---------------------------------------------------------------------------
# Click CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """CLI для управления системой «ДругИИ»."""


# ---------- user ----------


@cli.group()
def user() -> None:
    """Управление пользователями."""


@user.command(name="list")
def user_list() -> None:
    """Показать список всех пользователей."""
    async def _run() -> None:
        users = await _get_all_users()
        if not users:
            click.echo("Пользователи не найдены.")
            return
        click.echo(f"{'ID':>3}  {'ФИО':<30}  {'Telegram ID':<15}  {'MAX Chat ID':<20}  {'Создан'}")
        click.echo("-" * 90)
        for u in users:
            click.echo(
                f"{u['id']:>3}  {u['full_name']:<30}  "
                f"{str(u['telegram_id'] or '—'):<15}  "
                f"{str(u['max_chat_id'] or '—'):<20}  "
                f"{u['created_at']}"
            )
    asyncio.run(_with_db(_run)())


@user.command(name="add")
@click.option("--full-name", "-n", prompt="ФИО", help="Полное имя пользователя")
@click.option("--telegram-id", "-t", default=None, type=int, prompt="Telegram ID (оставьте пустым, если нет)")
@click.option("--max-chat-id", "-m", default=None, prompt="MAX Chat ID (оставьте пустым, если нет)")
def user_add(full_name: str, telegram_id: int | None, max_chat_id: str | None) -> None:
    """Добавить нового пользователя (интерактивно)."""
    async def _run() -> None:
        uid = await _add_user(full_name, telegram_id, max_chat_id or None)
        click.echo(f"✅ Пользователь добавлен с ID {uid}")
    asyncio.run(_with_db(_run)())


@user.command(name="show")
@click.argument("user_id", type=int)
def user_show(user_id: int) -> None:
    """Показать пользователя с его устройствами."""
    async def _run() -> None:
        user = await _get_user_by_id(user_id)
        if user is None:
            click.echo(f"❌ Пользователь с ID {user_id} не найден.")
            return
        click.echo(f"ID: {user['id']}")
        click.echo(f"ФИО: {user['full_name']}")
        click.echo(f"Telegram ID: {user['telegram_id'] or '—'}")
        click.echo(f"MAX Chat ID: {user['max_chat_id'] or '—'}")
        click.echo(f"Создан: {user['created_at']}")
        click.echo("")
        click.echo("Устройства:")

        db = await get_db()
        cursor = await db.execute(
            "SELECT id, mac_address, device_name, is_active FROM devices WHERE user_id = ?",
            (user_id,),
        )
        devices = await cursor.fetchall()
        if not devices:
            click.echo("  (нет устройств)")
        else:
            for d in devices:
                status = "✅" if d["is_active"] else "❌"
                click.echo(
                    f"  {status} #{d['id']} {d['mac_address']} "
                    f"({d['device_name'] or '—'})"
                )
    asyncio.run(_with_db(_run)())


# ---------- device ----------


@cli.group()
def device() -> None:
    """Управление устройствами."""


@device.command(name="list")
def device_list() -> None:
    """Показать список всех устройств."""
    async def _run() -> None:
        devices = await _get_all_devices()
        if not devices:
            click.echo("Устройства не найдены.")
            return
        click.echo(f"{'ID':>3}  {'MAC':<18}  {'Имя':<20}  {'User':>4}  {'Владелец':<25}  {'Активно'}")
        click.echo("-" * 90)
        for d in devices:
            active = "✅" if d["is_active"] else "❌"
            click.echo(
                f"{d['id']:>3}  {d['mac_address']:<18}  "
                f"{str(d['device_name'] or '—'):<20}  "
                f"{d['user_id']:>4}  "
                f"{str(d['user_name'] or '—'):<25}  "
                f"{active}"
            )
    asyncio.run(_with_db(_run)())


@device.command(name="add")
@click.option("--mac", "-m", prompt="MAC-адрес", help="MAC-адрес устройства")
@click.option("--name", "-n", prompt="Имя устройства (оставьте пустым, если нет)", default=None)
@click.option("--user-id", "-u", prompt="ID пользователя", type=int, help="ID пользователя-владельца")
def device_add(mac: str, name: str | None, user_id: int) -> None:
    """Привязать устройство к пользователю (интерактивно)."""
    # Нормализуем MAC
    mac = mac.replace("-", ":").replace(" ", "").upper()
    if ":" not in mac:
        parts = [mac[i : i + 2] for i in range(0, len(mac), 2)]
        mac = ":".join(parts)

    async def _run() -> None:
        did = await _add_device(mac, name or None, user_id)
        click.echo(f"✅ Устройство добавлено с ID {did}")
    asyncio.run(_with_db(_run)())


@device.command(name="remove")
@click.argument("device_id", type=int)
def device_remove(device_id: int) -> None:
    """Деактивировать устройство (is_active=0)."""
    async def _run() -> None:
        ok = await _deactivate_device(device_id)
        if ok:
            click.echo(f"✅ Устройство #{device_id} деактивировано.")
        else:
            click.echo(f"❌ Устройство с ID {device_id} не найдено.")
    asyncio.run(_with_db(_run)())


# ---------- device scan (привязка по близости) ----------


# Ориентировочная мощность передатчика на расстоянии 1 м (dBm)
_TX_POWER = -59
# Показатель затухания сигнала в помещении
_PATH_LOSS_N = 2.2


def _rssi_to_distance(rssi: int) -> float:
    """
    Грубая оценка расстояния до устройства по уровню сигнала.

    Args:
        rssi: Мощность принятого сигнала в dBm (отрицательная).

    Returns:
        Расстояние в метрах (очень приблизительно).
    """
    return 10 ** ((_TX_POWER - rssi) / (10 * _PATH_LOSS_N))


def _rssi_label(rssi: int) -> str:
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


async def _live_scan_rounds(rounds: int, scan_sec: int) -> dict[str, dict]:
    """
    Выполняет несколько циклов BLE-сканирования и агрегирует лучший RSSI.

    Args:
        rounds: Количество циклов.
        scan_sec: Длительность одного цикла в секундах.

    Returns:
        Словарь {mac: {"name": ..., "best_rssi": ...}}.
    """
    from bleak import BleakScanner

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

        click.echo(f"  цикл {round_no}/{rounds}: всего уникальных устройств {len(aggregated)}")

    return aggregated


def _print_scan_table(
    devices: dict[str, dict], min_rssi: int, user_macs: set[str]
) -> list[str]:
    """
    Печатает таблицу устройств, отсортированную по силе сигнала.

    Args:
        devices: Результат агрегированного сканирования.
        min_rssi: Минимальный RSSI для отображения.
        user_macs: MAC-адреса, уже привязанные к пользователям.

    Returns:
        Список MAC-адресов в порядке нумерации строк таблицы.
    """
    rows = [
        (mac, d["name"], d["best_rssi"])
        for mac, d in devices.items()
        if d["best_rssi"] is not None and d["best_rssi"] >= min_rssi
    ]
    rows.sort(key=lambda x: x[2], reverse=True)

    header = (
        f"{'№':>2}  {'RSSI':>5}  {'~м':>5}  {'Близость':<30}  "
        f"{'MAC':<18}  {'Имя':<25}  {'В системе'}"
    )
    click.echo(header)
    click.echo("-" * len(header))

    ordered: list[str] = []
    for i, (mac, name, rssi) in enumerate(rows, start=1):
        dist = f"{_rssi_to_distance(rssi):.1f}"
        bound = "✅ да" if mac in user_macs else "—"
        click.echo(
            f"{i:>2}  {rssi:>4}  {dist:>5}  {_rssi_label(rssi):<30}  "
            f"{mac:<18}  {str(name or '—'):<25}  {bound}"
        )
        ordered.append(mac)

    if not rows:
        click.echo("Устройств с сигналом лучше заданного порога не найдено.")
    return ordered


@device.command(name="scan")
@click.option("--rounds", "-r", default=3, type=int, show_default=True,
              help="Количество циклов сканирования")
@click.option("--scan-sec", "-s", default=4, type=int, show_default=True,
              help="Длительность одного цикла, сек")
@click.option("--min-rssi", default=-90, type=int, show_default=True,
              help="Не показывать устройства слабее порога (dBm)")
@click.option("--bind", "-b", is_flag=True,
              help="После сканирования предложить привязать выбранное устройство пользователю")
def device_scan(rounds: int, scan_sec: int, min_rssi: int, bind: bool) -> None:
    """
    Живой обзор BLE-устройств по уровню приближения к сканеру.

    Попросите пользователя поднести устройство к сканеру — оно поднимется
    на верх таблицы (самый сильный сигнал). Запустите с флагом --bind,
    чтобы сразу привязать выбранную строку к пользователю.
    """
    async def _run() -> None:
        # Уже привязанные MAC — помечаем в таблице
        db = await get_db()
        cursor = await db.execute("SELECT mac_address FROM devices WHERE is_active = 1")
        user_macs = {row["mac_address"] for row in await cursor.fetchall()}

        click.echo(f"Сканирование: {rounds} цикла(ов) по {scan_sec} сек…")
        devices = await _live_scan_rounds(rounds, scan_sec)
        click.echo()
        ordered = _print_scan_table(devices, min_rssi, user_macs)

        if not bind or not ordered:
            return

        # --- Привязка выбранного устройства ---
        click.echo()
        choice = click.prompt(
            "№ строки для привязки (Enter — выход)",
            default="", show_default=False, type=str,
        ).strip()
        if not choice or not choice.isdigit() or not (1 <= int(choice) <= len(ordered)):
            click.echo("Привязка отменена.")
            return

        mac = ordered[int(choice) - 1]
        info = devices[mac]
        default_name = info["name"] or ""

        if mac in user_macs:
            click.echo(f"⚠️  Устройство {mac} уже привязано в системе.")

        friendly_name = click.prompt(
            "Имя устройства (Enter — оставить как есть)", default=default_name,
            show_default=bool(default_name),
        )
        user_id = click.prompt("ID пользователя-владельца", type=int)

        did = await _add_device(mac, friendly_name or None, user_id)
        click.echo(f"✅ Устройство {mac} привязано к пользователю #{user_id} (ID записи {did})")

    asyncio.run(_with_db(_run)())


# ---------- detections ----------


@cli.command()
@click.argument("limit", type=int, default=10)
def detections(limit: int) -> None:
    """Показать последние N обнаружений."""
    async def _run() -> None:
        rows = await _get_recent_detections(limit)
        if not rows:
            click.echo("Обнаружения не найдены.")
            return
        click.echo(f"{'ID':>3}  {'MAC':<18}  {'Имя':<20}  {'RSSI':>5}  {'Время':<22}  {'Обработано'}")
        click.echo("-" * 85)
        for r in rows:
            processed = "✅" if r["has_greeting"] else "❌"
            click.echo(
                f"{r['id']:>3}  {r['mac_address']:<18}  "
                f"{str(r['device_name'] or '—'):<20}  "
                f"{str(r['rssi'] or '—'):>5}  "
                f"{r['detected_at']:<22}  "
                f"{processed}"
            )
    asyncio.run(_with_db(_run)())


# ---------- greetings ----------


@cli.command()
@click.argument("limit", type=int, default=10)
def greetings(limit: int) -> None:
    """Показать последние N приветствий."""
    async def _run() -> None:
        rows = await _get_recent_greetings(limit)
        if not rows:
            click.echo("Приветствия не найдены.")
            return
        click.echo(f"{'ID':>3}  {'UserID':>6}  {'Пользователь':<25}  {'Канал':<12}  {'Время':<22}")
        click.echo("-" * 75)
        for r in rows:
            # Обрезаем текст для вывода
            click.echo(
                f"{r['id']:>3}  {r['user_id']:>6}  "
                f"{str(r['user_name'] or '—'):<25}  "
                f"{r['sent_via']:<12}  "
                f"{r['sent_at']:<22}"
            )
    asyncio.run(_with_db(_run)())


# ---------- test greeting ----------


@cli.command(name="greeting-test")
@click.argument("user_id", type=int)
def greeting_test(user_id: int) -> None:
    """Отправить тестовое приветствие пользователю."""
    async def _run() -> None:
        result = await _send_test_greeting(user_id)
        click.echo(result)
    asyncio.run(_with_db(_run)())


# ---------------------------------------------------------------------------
# Точка входа для CLI (запускается из main параллельно)
# ---------------------------------------------------------------------------


async def run_cli(config: Config) -> NoReturn:
    """
    Запускает CLI в фоновом режиме (бесконечное ожидание).

    При прямом запуске (``python -m app.cli``) отрабатывает блок
    ``if __name__ == "__main__"``, а эта функция используется
    только при параллельном запуске через ``main.py``.

    Args:
        config: Конфигурация приложения (передаётся для единообразия).
    """
    logger.info("cli: запущен в фоновом режиме (команды через python -m app.cli)")
    await asyncio.Event().wait()


# ---------------------------------------------------------------------------
# Точка входа для прямого запуска
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()