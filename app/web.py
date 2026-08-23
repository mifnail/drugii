"""
Веб-интерфейс оператора «ДругИИ».

Запускает отдельный aiohttp сервер с HTML-панелью и JSON API
для управления связками «пользователь ↔ устройство» и живого
BLE-сканирования.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import NoReturn

import aiohttp
from aiohttp import web

from app.config import Config
from app.database import get_db
from app.scanner import live_scan, rssi_label, rssi_to_distance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML-панель
# ---------------------------------------------------------------------------

_HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ДругИИ — оператор</title>
<style>
*,*::before,*::after{box-sizing:border-box;}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,monospace;background:#1a1a2e;color:#e0e0e0;}
header{background:#16213e;padding:12px 20px;display:flex;align-items:center;gap:12px;}
header h1{font-size:18px;margin:0;color:#e94560;}
header .ver{font-size:11px;color:#888;}
.layout{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px;max-width:1200px;margin:0 auto;}
@media(max-width:768px){.layout{grid-template-columns:1fr;}}
.card{background:#16213e;border-radius:8px;padding:16px;border:1px solid #0f3460;}
.card h2{font-size:15px;margin:0 0 12px;color:#e94560;display:flex;align-items:center;gap:8px;}
.card h2 .badge{font-size:11px;background:#e94560;color:#fff;padding:2px 8px;border-radius:10px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;padding:6px 8px;border-bottom:2px solid #0f3460;color:#aaa;font-weight:500;white-space:nowrap;}
td{padding:5px 8px;border-bottom:1px solid #0f3460;white-space:nowrap;}
td.name{max-width:160px;overflow:hidden;text-overflow:ellipsis;}
tr:hover{background:rgba(233,69,96,.05);}
.btn{display:inline-block;padding:6px 14px;border:1px solid #e94560;border-radius:4px;background:transparent;color:#e94560;cursor:pointer;font:inherit;font-size:13px;}
.btn:hover{background:#e94560;color:#fff;}
.btn:disabled{opacity:.4;cursor:not-allowed;}
.btn-primary{background:#e94560;color:#fff;}
.btn-primary:hover{background:#c73851;}
.btn-sm{padding:3px 8px;font-size:11px;}
.btn-danger{border-color:#ff4757;color:#ff4757;}
.btn-danger:hover{background:#ff4757;color:#fff;}
.alert{padding:8px 12px;border-radius:4px;font-size:13px;margin:8px 0;}
.alert-err{background:rgba(255,71,87,.15);border:1px solid #ff4757;color:#ff4757;}
.alert-ok{background:rgba(46,213,115,.15);border:1px solid #2ed573;color:#2ed573;}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #0f3460;border-top-color:#e94560;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:6px;}
@keyframes spin{to{transform:rotate(360deg)}}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99;align-items:center;justify-content:center;}
.modal-overlay.active{display:flex;}
.modal{background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:20px;min-width:320px;max-width:90vw;}
.modal h3{margin:0 0 12px;color:#e94560;}
.modal label{display:block;margin:8px 0 4px;font-size:13px;color:#aaa;}
.modal select,.modal input{width:100%;padding:7px 10px;border:1px solid #0f3460;border-radius:4px;background:#1a1a2e;color:#e0e0e0;font:inherit;font-size:13px;}
.modal-actions{margin-top:16px;display:flex;gap:8px;justify-content:flex-end;}
.rssi-strong{color:#2ed573;font-weight:600;}
.rssi-good{color:#7bed9f;}
.rssi-ok{color:#ffa502;}
.rssi-weak{color:#ff6348;}
.pulse{animation:pulse 2s ease-in-out infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.empty{text-align:center;color:#666;padding:20px;font-size:13px;}
</style>
</head>
<body>

<header>
<h1>ДругИИ</h1><span class="ver">оператор</span>
<span id="status" style="margin-left:auto;font-size:12px;color:#888;">Готов</span>
</header>

<div class="layout">

<!-- Сканирование -->
<div class="card">
<h2>Сканирование BLE <button class="btn btn-primary" id="scanBtn" onclick="doScan()">Сканировать</button></h2>
<div id="scanAlert"></div>
<div id="scanTableWrap" style="overflow-x:auto;">
<table id="scanTable"><thead><tr><th>RSSI</th><th>~м</th><th>Близость</th><th>MAC</th><th>Имя</th><th></th></tr></thead>
<tbody><tr><td colspan="6" class="empty">Нажмите «Сканировать»</td></tr></tbody></table>
</div>
</div>

<!-- Пользователи и устройства -->
<div class="card">
<h2>Пользователи и устройства <span class="badge" id="userCount">0</span></h2>
<div id="userAlert"></div>
<div id="userList"><div class="empty">Загрузка...</div></div>
</div>

</div>

<!-- Модальное окно привязки -->
<div class="modal-overlay" id="bindModal">
<div class="modal">
<h3>Привязать устройство</h3>
<div id="modalAlert"></div>
<label>MAC-адрес</label>
<input type="text" id="bindMac" readonly>
<label>Имя устройства</label>
<input type="text" id="bindName" placeholder="например: Mi Band 5">
<label>Пользователь</label>
<select id="bindUser"><option value="">— выберите —</option></select>
<div class="modal-actions">
<button class="btn" onclick="closeBind()">Отмена</button>
<button class="btn btn-primary" onclick="doBind()">Привязать</button>
</div>
</div>
</div>

<script>
let __scanResults = [];

function setStatus(t) { document.getElementById('status').textContent = t; }

// ------------------------------------------------------------------ scan

async function doScan() {
  const btn = document.getElementById('scanBtn');
  const alert = document.getElementById('scanAlert');
  const tbody = document.querySelector('#scanTable tbody');

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Сканирую...';
  alert.innerHTML = '';
  tbody.innerHTML = '<tr><td colspan="6" class="empty"><span class="spinner pulse"></span> Сканирование BLE...</td></tr>';
  setStatus('Сканирую BLE...');

  try {
    const r = await fetch('/api/scan', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rounds:2,scan_sec:4})});
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    const data = await r.json();
    __scanResults = data.devices || [];
    renderScanTable(__scanResults);
    setStatus('Готов');
  } catch(e) {
    alert.innerHTML = `<div class="alert alert-err">Ошибка сканирования: ${e.message}</div>`;
    tbody.innerHTML = '<tr><td colspan="6" class="empty">Ошибка</td></tr>';
    setStatus('Ошибка');
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Сканировать';
  }
}

function renderScanTable(devs) {
  const tbody = document.querySelector('#scanTable tbody');
  if (!devs.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">Устройств не найдено</td></tr>';
    return;
  }
  tbody.innerHTML = devs.map((d,i) => `<tr>
    <td class="${d.label.includes('ВПЛОТНУЮ')?'rssi-strong':d.label.includes('близко')?'rssi-good':d.rssi>=-75?'rssi-ok':'rssi-weak'}">${d.rssi}</td>
    <td>${d.distance_m}</td>
    <td>${d.label||'—'}</td>
    <td>${d.mac}</td>
    <td class="name" title="${e(d.name||'')}">${e(d.name)||'—'}</td>
    <td>${d.is_bound
      ? '<span style="color:#2ed573;font-size:11px;">привязан</span>'
      : `<button class="btn btn-sm" onclick="openBind('${d.mac}','${e(d.name||'')}','${d.last_seen||''}')">Привязать</button>`}
    </td>
  </tr>`).join('');
}

// ---------------------------------------------------------------- users

async function loadUsers() {
  try {
    const [users,devices] = await Promise.all([
      fetch('/api/users').then(r=>r.json()),
      fetch('/api/devices').then(r=>r.json())
    ]);
    document.getElementById('userCount').textContent = users.length;
    renderUsers(users, devices);
  } catch(e) {
    document.getElementById('userAlert').innerHTML = `<div class="alert alert-err">Ошибка загрузки: ${e.message}</div>`;
  }
}

function renderUsers(users, devices) {
  const el = document.getElementById('userList');
  // Строим карту user_id -> его устройства
  const devMap = {};
  devices.forEach(d => { if (d.is_active) { (devMap[d.user_id]=devMap[d.user_id]||[]).push(d); } });

  if (!users.length) { el.innerHTML = '<div class="empty">Пользователей нет</div>'; return; }

  el.innerHTML = users.map(u => {
    const devs = devMap[u.id] || [];
    return `<div style="margin-bottom:12px;padding:8px 0;border-bottom:1px solid #0f3460;">
      <div style="display:flex;align-items:baseline;gap:8px;">
        <strong>${e(u.full_name)}</strong>
        <span style="font-size:11px;color:#888;">#${u.id}</span>
        ${u.telegram_id ? '<span style="font-size:10px;color:#7289da;" title="Telegram">TG</span>' : ''}
        ${u.max_chat_id ? '<span style="font-size:10px;color:#ffa502;" title="MAX">MAX</span>' : ''}
      </div>
      ${devs.length ? '<div style="margin-top:4px;">' + devs.map(d =>
        `<div style="display:flex;align-items:center;gap:8px;padding:2px 0;font-size:12px;">
          <code style="background:#0f3460;padding:2px 5px;border-radius:3px;">${d.mac_address}</code>
          ${e(d.device_name)||'—'}
          <button class="btn btn-sm btn-danger" onclick="doUnbind(${d.id})" title="Отвязать">✕</button>
        </div>`
      ).join('') + '</div>'
      : '<div style="margin-top:4px;font-size:11px;color:#666;">нет устройств</div>'}
      <div style="margin-top:6px;">
        <button class="btn btn-sm" onclick="doMerge(${u.id},'${e(u.full_name)}')" title="Объединить с другим пользователем">Объединить</button>
      </div>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------- bind modal

function openBind(mac, name) {
  document.getElementById('bindMac').value = mac;
  document.getElementById('bindName').value = name||'';
  fillBindUserSelect();
  document.getElementById('bindModal').classList.add('active');
  document.getElementById('modalAlert').innerHTML = '';
}

function closeBind() {
  document.getElementById('bindModal').classList.remove('active');
}

function fillBindUserSelect() {
  const sel = document.getElementById('bindUser');
  sel.innerHTML = '<option value="">— выберите —</option>';
  fetch('/api/users').then(r=>r.json()).then(users=>{
    users.forEach(u => { sel.innerHTML += `<option value="${u.id}">#${u.id} — ${e(u.full_name)}</option>`; });
  }).catch(()=>{});
}

async function doBind() {
  const mac = document.getElementById('bindMac').value;
  const name = document.getElementById('bindName').value.trim();
  const uid = document.getElementById('bindUser').value;
  const alert = document.getElementById('modalAlert');
  if (!uid) { alert.innerHTML = '<div class="alert alert-err">Выберите пользователя</div>'; return; }
  try {
    const r = await fetch('/api/bind', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mac, user_id: parseInt(uid), device_name: name||null})
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error||r.statusText);
    closeBind();
    loadUsers(); // обновить список
    // обновить кнопки в таблице сканирования
    __scanResults.forEach(d => { if (d.mac === mac) d.is_bound = true; });
    renderScanTable(__scanResults);
  } catch(e) {
    alert.innerHTML = `<div class="alert alert-err">Ошибка: ${e.message}</div>`;
  }
}

async function doUnbind(deviceId) {
  if (!confirm('Отвязать устройство?')) return;
  try {
    const r = await fetch('/api/unbind', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({device_id: deviceId})
    });
    if (!r.ok) throw new Error((await r.json()).error||r.statusText);
    loadUsers();
  } catch(e) {
    document.getElementById('userAlert').innerHTML = `<div class="alert alert-err">${e.message}</div>`;
  }
}

function doMerge(srcId, srcName) {
  const tid = prompt(`Объединить «${srcName}» (ID ${srcId}) с пользователем ID: (укажите, КУДА перенести)`)?.trim();
  if (!tid || tid == srcId) return;
  const tidNum = parseInt(tid);
  if (!tidNum) return;
  if (!confirm(`Объединить #${srcId} → #${tidNum}?\\nКонтакты и устройства #${srcId} будут перенесены.`)) return;
  fetch('/api/merge', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({source_id: srcId, target_id: tidNum})
  }).then(r=>r.json()).then(data=>{
    if (!data.ok) throw new Error(data.error);
    loadUsers();
  }).catch(e=>{
    document.getElementById('userAlert').innerHTML = `<div class="alert alert-err">Ошибка: ${e.message}</div>`;
  });
}

// ---------------------------------------------------------------- utils
function e(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// ---------------------------------------------------------------- init
loadUsers();
setInterval(loadUsers, 30000); // автообновление раз в 30 сек
</script>

</body>
</html>"""

# ---------------------------------------------------------------------------
# JSON API handlers
# ---------------------------------------------------------------------------


async def api_users(_request: web.Request) -> web.Response:
    """GET /api/users — список всех пользователей."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, full_name, telegram_id, max_chat_id, created_at FROM users ORDER BY id"
    )
    rows = await cursor.fetchall()
    return web.json_response([dict(r) for r in rows])


async def api_devices(_request: web.Request) -> web.Response:
    """GET /api/devices — все устройства с именами владельцев."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT d.id, d.mac_address, d.device_name, d.user_id, d.is_active,
                  u.full_name AS owner_name
           FROM devices d LEFT JOIN users u ON d.user_id = u.id
           ORDER BY d.id"""
    )
    rows = await cursor.fetchall()
    return web.json_response([dict(r) for r in rows])


async def api_scan(request: web.Request) -> web.Response:
    """
    POST /api/scan — живое BLE-сканирование.

    Тело (опционально): {"rounds": 2, "scan_sec": 4}
    Возвращает: {"devices": [{mac, name, rssi, distance_m, label, is_bound}]}
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError):
        body = {}

    rounds = max(1, min(10, int(body.get("rounds", 2))))
    scan_sec = max(1.0, min(15.0, float(body.get("scan_sec", 4.0))))

    try:
        raw = await live_scan(rounds=rounds, scan_sec=scan_sec)
    except Exception as exc:
        logger.error("Ошибка живого сканирования: %s", exc)
        return web.json_response(
            {"error": f"Ошибка сканирования: {exc}", "devices": []}, status=500,
        )

    if not raw:
        return web.json_response(
            {"error": "Сканер занят (фоновое сканирование). Повторите через несколько секунд.",
             "devices": []},
            status=503,
        )

    # Узнаём, какие MAC уже привязаны
    db = await get_db()
    cursor = await db.execute(
        "SELECT mac_address FROM devices WHERE is_active = 1"
    )
    bound_macs = {row["mac_address"] for row in await cursor.fetchall()}

    devices = []
    for mac, info in sorted(
        raw.items(),
        key=lambda kv: kv[1].get("best_rssi") or -999,
        reverse=True,
    ):
        rssi = info.get("best_rssi")
        if rssi is None:
            continue
        devices.append(
            {
                "mac": mac,
                "name": info.get("name"),
                "rssi": rssi,
                "distance_m": round(rssi_to_distance(rssi), 1),
                "label": rssi_label(rssi),
                "is_bound": mac in bound_macs,
            }
        )

    return web.json_response({"devices": devices})


async def api_bind(request: web.Request) -> web.Response:
    """
    POST /api/bind — привязка устройства к пользователю.

    Тело: {"mac": "...", "user_id": N, "device_name": "..." | null}
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError):
        return web.json_response({"error": "Неверный JSON"}, status=400)

    mac = str(body.get("mac", "")).strip()
    user_id = body.get("user_id")
    device_name = body.get("device_name") or None

    if not mac:
        return web.json_response({"error": "Поле mac обязательно"}, status=400)
    if not isinstance(user_id, int) or user_id <= 0:
        return web.json_response({"error": "Поле user_id должно быть числом"}, status=400)

    # Нормализуем MAC
    mac = mac.replace("-", ":").replace(" ", "").upper()
    if ":" not in mac and len(mac) == 12:
        mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        )
        if not await cursor.fetchone():
            return web.json_response({"error": f"Пользователь #{user_id} не найден"}, status=404)

        # Проверяем, не привязан ли уже этот MAC (деактивируем старую запись)
        old = await (await db.execute(
            "SELECT id FROM devices WHERE mac_address = ? AND is_active = 1", (mac,)
        )).fetchone()
        if old:
            await db.execute("UPDATE devices SET is_active = 0 WHERE id = ?", (old["id"],))

        cursor = await db.execute(
            "INSERT INTO devices (mac_address, device_name, user_id, is_active) VALUES (?, ?, ?, 1)",
            (mac, device_name, user_id),
        )
        await db.commit()
        return web.json_response({"ok": True, "device_id": cursor.lastrowid})
    except Exception as exc:
        logger.error("Ошибка привязки устройства: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)


async def api_unbind(request: web.Request) -> web.Response:
    """
    POST /api/unbind — деактивация устройства.

    Тело: {"device_id": N}
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError):
        return web.json_response({"error": "Неверный JSON"}, status=400)

    device_id = body.get("device_id")
    if not isinstance(device_id, int) or device_id <= 0:
        return web.json_response({"error": "Поле device_id должно быть числом"}, status=400)

    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE devices SET is_active = 0 WHERE id = ?", (device_id,)
        )
        if cursor.rowcount == 0:
            return web.json_response({"error": f"Устройство #{device_id} не найдено"}, status=404)
        await db.commit()
        return web.json_response({"ok": True})
    except Exception as exc:
        logger.error("Ошибка деактивации устройства: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)


async def api_merge(request: web.Request) -> web.Response:
    """
    POST /api/merge — объединение двух профилей одного человека.

    Тело: {"source_id": N, "target_id": N}
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError):
        return web.json_response({"error": "Неверный JSON"}, status=400)

    source_id = body.get("source_id")
    target_id = body.get("target_id")
    if not isinstance(source_id, int) or source_id <= 0:
        return web.json_response({"error": "source_id обязателен"}, status=400)
    if not isinstance(target_id, int) or target_id <= 0:
        return web.json_response({"error": "target_id обязателен"}, status=400)
    if source_id == target_id:
        return web.json_response({"error": "source_id и target_id совпадают"}, status=400)

    db = await get_db()
    try:
        src = await (await db.execute(
            "SELECT id, full_name, telegram_id, max_chat_id FROM users WHERE id = ?",
            (source_id,),
        )).fetchone()
        dst = await (await db.execute(
            "SELECT id, full_name, telegram_id, max_chat_id FROM users WHERE id = ?",
            (target_id,),
        )).fetchone()

        if not src:
            return web.json_response({"error": f"Источник #{source_id} не найден"}, status=404)
        if not dst:
            return web.json_response({"error": f"Цель #{target_id} не найдена"}, status=404)

        if src["telegram_id"] and not dst["telegram_id"]:
            await db.execute(
                "UPDATE users SET telegram_id = ?, updated_at = datetime('now') WHERE id = ?",
                (src["telegram_id"], target_id),
            )
        if src["max_chat_id"] and not dst["max_chat_id"]:
            await db.execute(
                "UPDATE users SET max_chat_id = ?, updated_at = datetime('now') WHERE id = ?",
                (src["max_chat_id"], target_id),
            )

        await db.execute(
            "UPDATE devices SET user_id = ? WHERE user_id = ?",
            (target_id, source_id),
        )
        await db.execute(
            "UPDATE greetings SET user_id = ? WHERE user_id = ?",
            (target_id, source_id),
        )
        await db.execute(
            "UPDATE users SET full_name = full_name || ' (объединён с #' || ? || ')', "
            "telegram_id = NULL, max_chat_id = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (target_id, source_id),
        )
        await db.commit()
        logger.info("Пользователь #%s объединён с #%s через веб", source_id, target_id)
        return web.json_response({"ok": True})
    except Exception as exc:
        logger.error("Ошибка объединения #%s → #%s: %s", source_id, target_id, exc)
        return web.json_response({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def run_web_ui(config: Config) -> NoReturn:
    """
    Запускает веб-интерфейс оператора на отдельном порту.

    Args:
        config: Конфигурация приложения.
    """
    logger.info(
        "web_ui: запуск на http://%s:%d",
        config.webhook_host,
        config.web_ui_port,
    )

    app = web.Application()
    app.router.add_get("/", _serve_dashboard)
    app.router.add_get("/api/users", api_users)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/scan", api_scan)
    app.router.add_post("/api/bind", api_bind)
    app.router.add_post("/api/unbind", api_unbind)
    app.router.add_post("/api/merge", api_merge)

    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, config.webhook_host, config.web_ui_port)
        await site.start()
        logger.info("web_ui: готов — %s:%d", config.webhook_host, config.web_ui_port)
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("web_ui: остановка...")
    except Exception as exc:
        logger.error("web_ui: ошибка сервера: %s", exc)
    finally:
        await runner.cleanup()


async def _serve_dashboard(_request: web.Request) -> web.Response:
    """GET / — отдаёт HTML-панель."""
    return web.Response(
        text=_HTML_PAGE,
        content_type="text/html",
    )