"""
Точка входа в приложение «ДругИИ».

Запускает все компоненты параллельно:
  - Bluetooth сканер (app.scanner)
  - Telegram бот (app.bot_telegram)
  - MAX bot / webhook сервер (app.bot_max)
  - CLI для оператора (app.cli)

При получении сигналов SIGINT/SIGTERM выполняет graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import NoReturn

from app.bot_max import run_max_bot
from app.bot_telegram import run_telegram_bot
from app.cli import run_cli
from app.config import Config
from app.database import close_db, init_db
from app.greeting import generate_greeting  # noqa: F401 — инициализация модуля
from app.scanner import run_scanner
from app.web import run_web_ui

logger = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    """Настраивает базовое логирование."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def shutdown(sig: signal.Signals, loop: asyncio.AbstractEventLoop) -> None:
    """
    Graceful shutdown: останавливает все задачи и закрывает БД.

    Args:
        sig: Сигнал, вызвавший завершение.
        loop: Текущий event loop.
    """
    logger.info("Получен сигнал %s. Завершение работы...", sig.name)

    tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    await close_db()
    loop.stop()
    logger.info("Приложение завершило работу.")


async def main() -> NoReturn:
    """Главная функция приложения."""
    config = Config.load()
    _setup_logging(config.log_level)

    logger.info("Запуск «ДругИИ»...")
    # Сохраняем PID для перезапуска
    try:
        with open("/tmp/opencode/drugii.pid", "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    await init_db(config)
    logger.info("База данных инициализирована.")

    await asyncio.gather(
        run_scanner(config),
        run_telegram_bot(config),
        run_max_bot(config),
        run_web_ui(config),
        run_cli(config),
    )


def entry_point() -> None:
    """Точка входа: настраивает обработку сигналов и запускает asyncio.run()."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(shutdown(s, loop)),
            )
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        logger.info("Задачи отменены при завершении.")
    finally:
        loop.close()


if __name__ == "__main__":
    entry_point()