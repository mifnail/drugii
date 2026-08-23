# ДругИИ — система приветствия пользователей через Bluetooth + боты

**ДругИИ** — это асинхронное приложение на Python, которое обнаруживает Bluetooth-устройства пользователей, привязывает их к записям в базе данных и отправляет персонализированные приветствия через Telegram или MAX ботов.

## Архитектура

```
drugii/
├── app/
│   ├── __init__.py          # Пустой файл пакета
│   ├── config.py            # Загрузка конфигурации из .env
│   ├── database.py          # Асинхронная работа с SQLite (aiosqlite)
│   ├── main.py              # Точка входа, запуск всех компонентов
│   ├── scanner.py           # Bluetooth сканер (будет реализован)
│   ├── bot_telegram.py      # Telegram бот (будет реализован)
│   ├── bot_max.py           # MAX бот + webhook сервер (будет реализован)
│   └── cli.py               # CLI для оператора (будет реализован)
├── data/                    # Директория с файлами БД (создаётся автоматически)
├── .env                     # Конфигурация (не в git)
├── .env.example             # Пример конфигурации
├── .gitignore
├── requirements.txt
└── README.md
```

### Компоненты

| Компонент       | Назначение |
|-----------------|------------|
| **scanner**     | Сканирует Bluetooth-устройства, логирует обнаружения в БД |
| **bot_telegram**| Telegram-бот на aiogram, отправляет приветствия |
| **bot_max**     | MAX-бот + aiohttp webhook для приёма сообщений |
| **cli**         | CLI на click для ручного управления оператором |
| **database**    | Асинхронный слой SQLite через aiosqlite |

### Технологии

- **Python 3.13**
- **aiosqlite** — асинхронный SQLite
- **aiogram 3.x** — Telegram Bot API
- **bleak** — Bluetooth Low Energy (BLE) сканер
- **aiohttp** — HTTP клиент/сервер для MAX API
- **python-dotenv** — управление конфигурацией
- **click** — CLI интерфейс

## Запуск

### 1. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 2. Настройка

Скопируйте файл конфигурации и заполните токены:

```bash
cp .env.example .env
```

Отредактируйте `.env`, указав как минимум `BOT_TOKEN_TG` и/или `BOT_TOKEN_MAX`.

### 3. Запуск приложения

```bash
python -m app.main
```

Приложение запустит все компоненты параллельно:
- Bluetooth сканер
- Telegram бот
- MAX webhook сервер
- CLI для оператора

Остановка — по `Ctrl+C` (graceful shutdown).

## База данных

SQLite-файл создаётся автоматически в `data/drugii.db` (путь можно изменить в `.env`).

**Таблицы:**
- `users` — пользователи системы (ФИО, telegram_id, max_chat_id)
- `devices` — Bluetooth-устройства, привязанные к пользователям
- `detections` — лог обнаружений устройств
- `greetings` — история отправленных приветствий

## Лицензия

MIT