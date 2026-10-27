"""Meshtastic USB/BLE chat and private-message auto replies."""

import base64
import binascii
from collections import deque
import hmac
import json
import logging
import math
import os
import queue
import re
import sqlite3
import stat
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import meshtastic.ble_interface
import meshtastic.serial_interface
from google.protobuf.descriptor import FieldDescriptor
from meshtastic.protobuf import channel_pb2, config_pb2, mesh_pb2, module_config_pb2, portnums_pb2
from pubsub import pub


CHANNEL_INDEX = int(os.environ.get("LONGFAST_CHANNEL_INDEX", "0"))
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8765"))
WEB_USER = os.environ.get("WEB_USER", "admin")
WEB_PASSWORD = os.environ["WEB_PASSWORD"]
DB_PATH = Path(os.environ.get("DB_PATH", "/data/messages.sqlite3"))
PAGE = Path(__file__).with_name("index.html").read_bytes()
SCRIPT = Path(__file__).with_name("app.js").read_bytes()
LEAFLET_JS = Path(__file__).with_name("leaflet.js").read_bytes()
LEAFLET_CSS = Path(__file__).with_name("leaflet.css").read_bytes()
EMOJI_DATA = Path(__file__).with_name("emoji-data.json").read_bytes()
EMOJI_SET = {item[0] for item in json.loads(EMOJI_DATA)["items"]}
LOG = logging.getLogger("meshtastic-bot")
COMMANDS = queue.Queue(maxsize=100)
INCOMING_TEXT = queue.SimpleQueue()
INCOMING_ROUTING = queue.SimpleQueue()
INCOMING_TRACES = queue.SimpleQueue()
STATE = {"connected": False, "connecting": False, "node_id": None, "error": None,
         "selected_transport": None, "selected_device": None, "devices": [],
         "scanning": False, "notice": None, "known_nodes": [], "last_node_id": None,
         "node_settings": None, "node_settings_revision": 0, "bot_enabled": True,
         "longfast_channel_index": CHANNEL_INDEX}
STATE_LOCK = threading.Lock()
NODE_ID = re.compile(r"![0-9a-fA-F]{8}\Z")
WEATHER_LOCATION = os.environ.get("WEATHER_LOCATION", "Москва").strip()
try:
    WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "55.7558"))
    WEATHER_LON = float(os.environ.get("WEATHER_LON", "37.6173"))
except ValueError as exc:
    raise SystemExit("WEATHER_LAT and WEATHER_LON must be decimal coordinates") from exc
LOCAL_TIMEZONE = os.environ.get("LOCAL_TIMEZONE", "Europe/Moscow")
WEATHER_URL = f"https://wttr.in/{WEATHER_LAT:.5f},{WEATHER_LON:.5f}?format=j1&lang=ru"
TRACE_TIMEOUT_SECONDS = 180
TRACE_COOLDOWN_SECONDS = 35
WEATHER_LOCK = threading.Lock()
WEATHER_CACHE = {"data": None, "fetched_at": 0.0, "expires_at": 0.0, "error": None}

# Only scalar settings that can be edited without replacing nested protobuf messages.
RADIO_SETTING_GROUPS = {
    "device": ("Устройство", "role", "rebroadcast_mode", "node_info_broadcast_secs",
               "tzdef", "led_heartbeat_disabled"),
    "lora": ("LoRa", "region", "use_preset", "modem_preset", "hop_limit",
             "tx_enabled", "tx_power", "channel_num", "sx126x_rx_boosted_gain",
             "ignore_mqtt"),
    "position": ("Геопозиция", "gps_enabled", "gps_mode", "gps_update_interval",
                 "gps_attempt_time", "position_broadcast_secs", "position_broadcast_smart_enabled",
                 "fixed_position", "broadcast_smart_minimum_distance",
                 "broadcast_smart_minimum_interval_secs", "position_flags"),
    "power": ("Питание", "is_power_saving", "on_battery_shutdown_after_secs",
              "wait_bluetooth_secs", "sds_secs", "ls_secs", "min_wake_secs"),
    "display": ("Экран", "screen_on_secs", "auto_screen_carousel_secs", "flip_screen",
                "units", "displaymode", "compass_north_top", "use_12h_clock",
                "use_long_node_name", "enable_message_bubbles"),
    "bluetooth": ("Bluetooth", "enabled", "mode", "fixed_pin"),
    "network": ("Сеть", "wifi_enabled", "wifi_ssid", "wifi_psk", "ntp_server", "eth_enabled"),
    "mqtt": ("MQTT", "enabled", "address", "username", "password", "encryption_enabled",
             "json_enabled", "tls_enabled", "root", "proxy_to_client_enabled",
             "map_reporting_enabled"),
    "telemetry": ("Телеметрия", "device_telemetry_enabled", "device_update_interval",
                  "environment_measurement_enabled", "environment_update_interval",
                  "environment_screen_enabled", "environment_display_fahrenheit",
                  "air_quality_enabled", "air_quality_interval", "power_measurement_enabled",
                  "power_update_interval", "health_measurement_enabled", "health_update_interval"),
    "neighbor_info": ("Соседние ноды", "enabled", "update_interval", "transmit_over_lora"),
    "store_forward": ("Хранение и пересылка", "enabled", "heartbeat", "records",
                      "history_return_max", "history_return_window", "is_server"),
    "range_test": ("Тест дальности", "enabled", "sender", "save"),
}
RADIO_FIELD_LABELS = {
    "role": "Роль", "rebroadcast_mode": "Ретрансляция", "node_info_broadcast_secs": "Интервал информации о ноде, с",
    "tzdef": "Часовой пояс", "led_heartbeat_disabled": "Выключить мигание LED",
    "region": "Регион", "use_preset": "Готовый пресет", "modem_preset": "Пресет модема",
    "hop_limit": "Лимит хопов", "tx_enabled": "Передача включена", "tx_power": "Мощность, dBm",
    "channel_num": "Частотный канал", "sx126x_rx_boosted_gain": "Усиление приёма SX126x",
    "ignore_mqtt": "Игнорировать MQTT", "gps_enabled": "GPS включён", "gps_mode": "Режим GPS",
    "gps_update_interval": "Обновление GPS, с", "gps_attempt_time": "Поиск GPS, с",
    "position_broadcast_secs": "Передача позиции, с", "position_broadcast_smart_enabled": "Умная передача позиции",
    "fixed_position": "Фиксированная позиция", "broadcast_smart_minimum_distance": "Мин. расстояние, м",
    "broadcast_smart_minimum_interval_secs": "Мин. интервал, с", "position_flags": "Флаги позиции",
    "is_power_saving": "Энергосбережение", "on_battery_shutdown_after_secs": "Выключение на батарее, с",
    "wait_bluetooth_secs": "Ожидание Bluetooth, с", "sds_secs": "Сон SDS, с",
    "ls_secs": "Лёгкий сон, с", "min_wake_secs": "Мин. пробуждение, с",
    "screen_on_secs": "Время экрана, с", "auto_screen_carousel_secs": "Смена экранов, с",
    "flip_screen": "Повернуть экран", "units": "Единицы измерения", "displaymode": "Режим экрана",
    "compass_north_top": "Север сверху", "use_12h_clock": "12-часовой формат",
    "use_long_node_name": "Полное имя ноды", "enable_message_bubbles": "Сообщения пузырями",
    "enabled": "Включено", "mode": "Режим", "fixed_pin": "Фиксированный PIN",
    "wifi_enabled": "Wi-Fi включён", "wifi_ssid": "Имя Wi-Fi", "wifi_psk": "Пароль Wi-Fi", "ntp_server": "Сервер времени",
    "eth_enabled": "Ethernet включён", "address": "Адрес сервера", "username": "Пользователь", "password": "Пароль MQTT",
    "encryption_enabled": "Шифрование", "json_enabled": "JSON", "tls_enabled": "TLS",
    "root": "Корень топиков", "proxy_to_client_enabled": "Прокси через клиент",
    "map_reporting_enabled": "Отчёты на карте", "device_telemetry_enabled": "Телеметрия устройства",
    "device_update_interval": "Интервал устройства, с",
    "environment_measurement_enabled": "Датчики среды", "environment_update_interval": "Интервал среды, с",
    "environment_screen_enabled": "Среда на экране", "environment_display_fahrenheit": "Фаренгейт",
    "air_quality_enabled": "Качество воздуха", "air_quality_interval": "Интервал воздуха, с",
    "power_measurement_enabled": "Измерение питания", "power_update_interval": "Интервал питания, с",
    "health_measurement_enabled": "Показатели здоровья", "health_update_interval": "Интервал здоровья, с",
    "update_interval": "Интервал, с", "transmit_over_lora": "Передавать по LoRa",
    "heartbeat": "Периодический сигнал", "records": "Число записей",
    "history_return_max": "Макс. записей в ответе", "history_return_window": "Окно истории",
    "is_server": "Режим сервера", "sender": "Интервал отправки, с", "save": "Сохранять результаты",
}

if WEB_PASSWORD == "replace-with-a-long-random-password" or not WEB_PASSWORD:
    raise SystemExit("Set WEB_PASSWORD in .env")
if not 0 <= CHANNEL_INDEX <= 7:
    raise SystemExit("LONGFAST_CHANNEL_INDEX must be between 0 and 7")
if (not WEATHER_LOCATION or len(WEATHER_LOCATION.encode("utf-8")) > 40 or
        not math.isfinite(WEATHER_LAT) or not -90 <= WEATHER_LAT <= 90 or
        not math.isfinite(WEATHER_LON) or not -180 <= WEATHER_LON <= 180):
    raise SystemExit("Set valid WEATHER_LOCATION, WEATHER_LAT and WEATHER_LON in .env")
try:
    LOCAL_ZONE = ZoneInfo(LOCAL_TIMEZONE)
except (KeyError, ValueError) as exc:
    raise SystemExit("Set valid LOCAL_TIMEZONE in .env, for example Europe/Moscow") from exc
STATE.update(weather_location=WEATHER_LOCATION, map_center=[WEATHER_LAT, WEATHER_LON],
             local_timezone=LOCAL_TIMEZONE)


def set_state(**changes):
    with STATE_LOCK:
        STATE.update(changes)


def state_snapshot():
    with STATE_LOCK:
        return dict(STATE)


def db_connection():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            text TEXT NOT NULL,
            hops INTEGER,
            packet_id INTEGER,
            reply_to_packet_id INTEGER,
            UNIQUE(sender, packet_id)
        )""")
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS messages_by_time ON messages(received_at DESC, id DESC)")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
        if "delivery_status" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN delivery_status TEXT")
        if "delivery_error" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN delivery_error TEXT")
        if "reply_to_packet_id" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN reply_to_packet_id INTEGER")
        has_reads = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_reads'").fetchone()
        db.execute("""CREATE TABLE IF NOT EXISTS message_reads (
            message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            read_at TEXT NOT NULL
        )""")
        if not has_reads:
            db.execute("INSERT INTO message_reads(message_id,read_at) SELECT id,? FROM messages",
                       (datetime.now(timezone.utc).isoformat(),))
        db.execute("""CREATE TABLE IF NOT EXISTS reply_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            normalized_key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            peer TEXT NOT NULL,
            sender TEXT NOT NULL,
            packet_id INTEGER NOT NULL,
            target_packet_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            UNIQUE(sender, packet_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS reactions_by_target ON reactions(kind, peer, target_packet_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS channel_catalog (
            slot INTEGER PRIMARY KEY, name TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS traceroutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            local_node TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            finished_at TEXT,
            request_packet_id INTEGER,
            forward_route TEXT,
            reverse_route TEXT,
            error TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS traceroutes_by_target ON traceroutes(target, id DESC)")
        db.execute("""UPDATE traceroutes SET status='interrupted',
                    finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                    error='Сервис перезапущен до завершения трассировки'
                    WHERE status IN ('queued', 'running')""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS traceroutes_one_active_target
                    ON traceroutes(target) WHERE status IN ('queued', 'running')""")


def get_setting(key):
    with db_connection() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    with db_connection() as db:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def queue_trace(destination, source, local_node):
    try:
        with db_connection() as db:
            cursor = db.execute(
                """INSERT INTO traceroutes(target,local_node,source,status,requested_at)
                   VALUES(?,?,?,?,?)""",
                (destination, local_node, source, "queued", datetime.now(timezone.utc).isoformat()))
            trace_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return None
    if not enqueue(("trace", destination, trace_id, source)):
        finish_trace(trace_id, "failed", "Очередь команд заполнена")
        return None
    return trace_id


def mark_trace_running(trace_id, packet_id):
    with db_connection() as db:
        db.execute("UPDATE traceroutes SET status='running', request_packet_id=? WHERE id=?",
                   (packet_id, trace_id))


def finish_trace(trace_id, status, error=None, forward=None, reverse=None):
    with db_connection() as db:
        db.execute("""UPDATE traceroutes SET status=?, finished_at=?, error=?,
                    forward_route=?, reverse_route=? WHERE id=?""",
                   (status, datetime.now(timezone.utc).isoformat(), error,
                    json.dumps(forward) if forward is not None else None,
                    json.dumps(reverse) if reverse is not None else None, trace_id))


def list_traces(target=None, limit=30):
    with db_connection() as db:
        rows = db.execute(
            """SELECT id,target,local_node,source,status,requested_at,finished_at,
                      forward_route,reverse_route,error FROM traceroutes
               WHERE (? IS NULL OR target=?) ORDER BY id DESC LIMIT ?""",
            (target, target, limit)).fetchall()
    traces = []
    for row in rows:
        item = dict(row)
        for key in ("forward_route", "reverse_route"):
            item[key] = json.loads(item[key]) if item[key] else None
        traces.append(item)
    return traces


def get_reply_template(text):
    with db_connection() as db:
        row = db.execute("SELECT value FROM reply_templates WHERE normalized_key=?",
                         (text.strip().casefold(),)).fetchone()
    return row["value"] if row else None


def list_templates():
    with db_connection() as db:
        return [dict(row) for row in db.execute(
            "SELECT id, key, value FROM reply_templates ORDER BY key COLLATE NOCASE")]


def get_weather(force=False):
    """Return configured location conditions, cached for ten minutes."""
    with WEATHER_LOCK:
        now = time.monotonic()
        if not force and now < WEATHER_CACHE["expires_at"]:
            if WEATHER_CACHE["data"] is not None:
                return {**WEATHER_CACHE["data"], "stale": WEATHER_CACHE["error"] is not None}
            raise RuntimeError("Погода временно недоступна")
        try:
            request = Request(WEATHER_URL, headers={"User-Agent": "Meshtastic-Bot/1.0"})
            with urlopen(request, timeout=6) as response:
                current = json.load(response)["current_condition"][0]
            description = current.get("lang_ru") or current.get("weatherDesc") or []
            condition = description[0]["value"].strip()
            observed = datetime.now(LOCAL_ZONE)
            data = {
                "city": WEATHER_LOCATION,
                "temperature": float(current["temp_C"]),
                "feels_like": float(current["FeelsLikeC"]),
                "humidity": float(current["humidity"]),
                "precipitation": float(current["precipMM"]),
                "wind_speed": float(current["windspeedKmph"]),
                "condition": condition[:36],
                "observed_at": observed.strftime("%Y-%m-%dT%H:%M"),
                "time_label": observed.strftime("%Z"),
                "source": "wttr.in",
            }
            WEATHER_CACHE.update(data=data, fetched_at=now, expires_at=now + 600, error=None)
            return {**data, "stale": False}
        except Exception as exc:
            LOG.warning("Weather request failed: %s", exc)
            WEATHER_CACHE.update(expires_at=now + 60, error="Погода временно недоступна")
            if WEATHER_CACHE["data"] is not None and now - WEATHER_CACHE["fetched_at"] < 3600:
                return {**WEATHER_CACHE["data"], "stale": True}
            raise RuntimeError("Погода временно недоступна") from exc


def weather_reply(weather):
    time_local = weather["observed_at"].split("T")[-1][:5]
    parts = [f"{weather['city']} {weather['temperature']:+.0f}°C, {weather['condition']}"]
    if isinstance(weather["wind_speed"], (int, float)):
        parts.append(f"ветер {weather['wind_speed']:.0f} км/ч")
    if isinstance(weather["humidity"], (int, float)):
        parts.append(f"влажн. {weather['humidity']:.0f}%")
    parts.append(f"{time_local} {weather['time_label']}")
    if weather["stale"]:
        parts.append("кеш")
    reply = "; ".join(parts) + "."
    if len(reply.encode("utf-8")) > 200:
        return f"{weather['city'][:22]} {weather['temperature']:+.0f}°C, {weather['condition'][:18]}; {time_local} {weather['time_label']}."
    return reply


def stats_reply():
    """Summarize Raspberry Pi host metrics in one Meshtastic text packet."""
    def metric_text(container_path, host_path):
        path = Path(container_path)
        return (path if path.is_file() else Path(host_path)).read_text()

    def cpu_times():
        fields = metric_text("/host/proc-stat", "/proc/stat").splitlines()[0].split()
        if fields[0] != "cpu" or len(fields) < 6:
            raise ValueError("Неверный формат /proc/stat")
        values = [int(value) for value in fields[1:9]]
        return sum(values), values[3] + values[4]

    total_before, idle_before = cpu_times()
    time.sleep(0.3)
    total_after, idle_after = cpu_times()
    elapsed = total_after - total_before
    cpu_percent = round(100 * (1 - (idle_after - idle_before) / elapsed)) if elapsed > 0 else 0

    memory = {}
    for line in metric_text("/host/meminfo", "/proc/meminfo").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"MemTotal", "MemAvailable"}:
            memory[key] = int(value.split()[0])
    ram_total = memory["MemTotal"] / 1024 / 1024
    ram_used = (memory["MemTotal"] - memory["MemAvailable"]) / 1024 / 1024

    uptime = int(float(metric_text("/host/uptime", "/proc/uptime").split()[0]))
    days, remainder = divmod(uptime, 86400)
    hours, _ = divmod(remainder, 3600)
    uptime_text = f"{days}д {hours}ч" if days else f"{hours}ч {remainder % 3600 // 60}м"

    disk = os.statvfs(DB_PATH.parent)
    disk_total = disk.f_blocks * disk.f_frsize / 1024**3
    disk_used = (disk.f_blocks - disk.f_bavail) * disk.f_frsize / 1024**3
    parts = [f"Pi: CPU {cpu_percent}%, load {os.getloadavg()[0]:.1f}",
             f"RAM {ram_used:.1f}/{ram_total:.1f} ГБ"]
    try:
        temperature = int(metric_text("/host/cpu-temp", "/sys/class/thermal/thermal_zone0/temp").strip()) / 1000
        parts.append(f"T {temperature:.1f}°C")
    except (OSError, ValueError):
        pass
    parts.extend((f"диск {disk_used:.0f}/{disk_total:.0f} ГБ", f"работает {uptime_text}"))
    return "; ".join(parts) + "."


def list_usb_devices():
    """Find a Compose-mapped radio or Linux host serial ports."""
    mapped = Path("/dev/meshtastic")
    paths = [mapped]
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        paths.extend(sorted(by_id.iterdir()))
    paths.extend(sorted(Path("/dev").glob("ttyACM*")))
    paths.extend(sorted(Path("/dev").glob("ttyUSB*")))
    devices = []
    seen = set()
    for path in paths:
        try:
            device = path.stat()
        except OSError:
            continue
        if (not stat.S_ISCHR(device.st_mode) or os.major(device.st_rdev) not in (166, 188)
                or device.st_rdev in seen):
            continue
        seen.add(device.st_rdev)
        devices.append({"name": "Heltec USB" if path == mapped else path.name,
                        "address": str(path), "transport": "usb"})
    return devices


def valid_usb_device(path):
    return (isinstance(path, str) and path in {d["address"] for d in list_usb_devices()})


def enqueue(command):
    try:
        COMMANDS.put_nowait(command)
        return True
    except queue.Full:
        set_state(error="Очередь команд заполнена")
        return False


def save_message(kind, sender, recipient, text, hops, packet_id=None, received_at=None,
                 delivery_status=None, reply_to_packet_id=None):
    with db_connection() as db:
        cursor = db.execute(
            """INSERT OR IGNORE INTO messages
               (received_at, kind, sender, recipient, text, hops, packet_id, delivery_status,
                reply_to_packet_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (received_at or datetime.now(timezone.utc).isoformat(), kind, sender, recipient,
             text, hops, packet_id, delivery_status, reply_to_packet_id),
        )
        if cursor.rowcount == 0 and packet_id and reply_to_packet_id:
            db.execute("""UPDATE messages SET reply_to_packet_id=?
                          WHERE sender=? AND packet_id=? AND reply_to_packet_id IS NULL""",
                       (reply_to_packet_id, sender, packet_id))
        return cursor.rowcount == 1


def save_reaction(kind, peer, sender, packet_id, target_packet_id, emoji, received_at=None):
    with db_connection() as db:
        db.execute("""INSERT OR IGNORE INTO reactions
                      (received_at, kind, peer, sender, packet_id, target_packet_id, emoji)
                      VALUES (?, ?, ?, ?, ?, ?, ?)""",
                   (received_at or datetime.now(timezone.utc).isoformat(), kind, peer,
                    sender, packet_id, target_packet_id, emoji))


def valid_reaction(emoji):
    if not isinstance(emoji, str):
        return False
    try:
        byte_length = len(emoji.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return (1 <= byte_length <= 200
            and not any(char.isspace() or ord(char) < 32 for char in emoji)
            and (emoji in EMOJI_SET or
                 any(0x1F000 <= ord(char) <= 0x1FAFF or 0x2600 <= ord(char) <= 0x27BF
                     or ord(char) in (0xA9, 0xAE, 0x203C, 0x2049, 0x2122, 0x2139)
                     for char in emoji)))


def update_delivery(sender, packet_id, status, error=None):
    with db_connection() as db:
        db.execute("""UPDATE messages SET delivery_status=?, delivery_error=?
                      WHERE sender=? AND packet_id=?
                        AND delivery_status IN ('pending', 'relayed', 'unconfirmed')""",
                   (status, error, sender, packet_id))


def expire_delivery():
    cutoff = datetime.fromtimestamp(time.time() - 180, timezone.utc).isoformat()
    with db_connection() as db:
        db.execute("""UPDATE messages SET delivery_status='unconfirmed'
                      WHERE delivery_status IN ('pending', 'relayed') AND received_at<?""",
                   (cutoff,))


def list_chats():
    own_id = get_setting("my_node_id") or state_snapshot()["node_id"]
    if not own_id:
        return []
    with db_connection() as db:
        rows = db.execute("""SELECT id, received_at, text, peer FROM (
            SELECT id, received_at, text,
                   CASE WHEN sender=? THEN recipient ELSE sender END AS peer,
                   ROW_NUMBER() OVER (
                       PARTITION BY CASE WHEN sender=? THEN recipient ELSE sender END
                       ORDER BY received_at DESC, id DESC
                   ) AS position
            FROM messages WHERE kind='dm'
        ) WHERE position=1 ORDER BY received_at DESC, id DESC LIMIT 100""",
                          (own_id, own_id)).fetchall()
        counts = db.execute("""SELECT CASE WHEN m.sender=? THEN m.recipient ELSE m.sender END AS peer,
                              COUNT(*) AS unread FROM messages m LEFT JOIN message_reads r ON r.message_id=m.id
                              WHERE m.kind='dm' AND m.sender<>? AND r.message_id IS NULL
                              GROUP BY peer""", (own_id, own_id)).fetchall()
    unread = {row["peer"]: row["unread"] for row in counts}
    return [dict(row) | {"unread": unread.get(row["peer"], 0)} for row in rows]


def unread_counts():
    own_id = get_setting("my_node_id") or state_snapshot()["node_id"]
    with db_connection() as db:
        rows = db.execute("""SELECT m.kind, m.recipient, COUNT(*) AS unread
                             FROM messages m LEFT JOIN message_reads r ON r.message_id=m.id
                             WHERE m.kind IN ('longfast','channel') AND m.sender<>?
                               AND r.message_id IS NULL
                             GROUP BY m.kind,m.recipient""", (own_id,)).fetchall()
    return {(row["kind"], row["recipient"]): row["unread"] for row in rows}


def channel_peer(index):
    return f"#{index}"


def enabled_channel(index):
    channels = (state_snapshot().get("node_settings") or {}).get("channels") or []
    return any(channel["index"] == index and channel["role"] != 0 for channel in channels)


def list_channels():
    unread = unread_counts()
    with db_connection() as db:
        saved = {row["slot"]: row["name"] for row in db.execute(
            "SELECT slot, name FROM channel_catalog")}
        previews = {int(row["recipient"][1:]): row["text"] for row in db.execute(
            """SELECT recipient, text FROM messages WHERE kind='channel'
               ORDER BY received_at, id""")}
    channels = (state_snapshot().get("node_settings") or {}).get("channels") or []
    active = {channel["index"]: channel for channel in channels if channel["role"] != 0}
    slots = sorted(set(saved) | set(previews) | set(active))
    return [{"index": index, "name": active.get(index, {}).get("name") or saved.get(index) or
             ("LongFast" if index == CHANNEL_INDEX else f"Канал {index}"),
             "active": index in active, "preview": previews.get(index, "Общий канал"),
             "unread": unread.get(("channel", channel_peer(index)), 0)}
            for index in slots if index != CHANNEL_INDEX]


def remember_channels(snapshot):
    with db_connection() as db:
        for channel in snapshot.get("channels") or []:
            if channel["role"] != 0:
                db.execute("INSERT OR REPLACE INTO channel_catalog(slot,name) VALUES(?,?)",
                           (channel["index"], channel["name"] or f"Канал {channel['index']}"))


def chat_scope(kind, peer, own_id):
    if kind == "dm":
        return "kind=? AND ((sender=? AND recipient=?) OR (sender=? AND recipient=?))", [
            kind, own_id, peer, peer, own_id]
    if kind == "channel":
        return "kind=? AND recipient=?", [kind, peer]
    return "kind=?", [kind]


def get_chat_messages(kind, peer, before, after=None, start_unread=False):
    own_id = get_setting("my_node_id") or state_snapshot()["node_id"]
    scope, scope_params = chat_scope(kind, peer, own_id)
    with db_connection() as db:
        first_unread = db.execute(
            f"""SELECT m.id,m.received_at FROM messages m
                 LEFT JOIN message_reads r ON r.message_id=m.id
                 WHERE {scope} AND m.sender<>? AND r.message_id IS NULL
                 ORDER BY m.received_at,m.id LIMIT 1""",
            [*scope_params, own_id]).fetchone()
        unread_count = db.execute(
            f"""SELECT COUNT(*) FROM messages m LEFT JOIN message_reads r ON r.message_id=m.id
                 WHERE {scope} AND m.sender<>? AND r.message_id IS NULL""",
            [*scope_params, own_id]).fetchone()[0]
        query = ("SELECT id, received_at, sender, recipient, text, hops, packet_id, "
                 "reply_to_packet_id, delivery_status, delivery_error FROM messages WHERE " + scope)
        params = list(scope_params)
        forward = bool(after or (start_unread and first_unread and not before))
        if before:
            query += " AND (received_at, id) < (SELECT received_at, id FROM messages WHERE id=?)"
            params.append(before)
        elif after:
            query += " AND (received_at, id) > (SELECT received_at, id FROM messages WHERE id=?)"
            params.append(after)
        elif forward:
            query += " AND (received_at, id) >= (?,?)"
            params.extend([first_unread["received_at"], first_unread["id"]])
        query += " ORDER BY received_at " + ("ASC, id ASC" if forward else "DESC, id DESC") + " LIMIT 51"
        rows = db.execute(query, params).fetchall()
        page = rows[:50] if forward else list(reversed(rows[:50]))
        messages = [dict(row) for row in page]
        if messages:
            placeholders = ",".join("?" for _ in messages)
            unread_ids = {row["id"] for row in db.execute(
                f"""SELECT m.id FROM messages m LEFT JOIN message_reads r ON r.message_id=m.id
                    WHERE m.id IN ({placeholders}) AND m.sender<>? AND r.message_id IS NULL""",
                [*(message["id"] for message in messages), own_id])}
            for message in messages:
                message["unread"] = message["id"] in unread_ids
        if forward:
            has_newer = len(rows) > 50
            if messages:
                older = db.execute(
                    "SELECT 1 FROM messages WHERE " + scope +
                    " AND (received_at,id)<(?,?) LIMIT 1",
                    [*scope_params, messages[0]["received_at"], messages[0]["id"]]).fetchone()
                has_more = bool(older)
            else:
                has_more = False
        else:
            has_more = len(rows) > 50
            has_newer = False
        packet_ids = [message["packet_id"] for message in messages if message["packet_id"]]
        reply_ids = list({message["reply_to_packet_id"] for message in messages
                          if message["reply_to_packet_id"]})
        reactions = {}
        if packet_ids:
            placeholders = ",".join("?" for _ in packet_ids)
            reaction_rows = db.execute(
                f"""SELECT target_packet_id, sender, emoji FROM reactions
                    WHERE kind=? AND peer=? AND target_packet_id IN ({placeholders})
                    ORDER BY id""", [kind, peer if kind in ("dm", "channel") else "^all", *packet_ids])
            for reaction in reaction_rows:
                reactions.setdefault(reaction["target_packet_id"], {})[reaction["sender"]] = reaction["emoji"]
        reply_targets = {}
        if reply_ids:
            placeholders = ",".join("?" for _ in reply_ids)
            reply_query = f"""SELECT id, sender, text, packet_id FROM messages
                              WHERE kind=? AND packet_id IN ({placeholders})"""
            reply_params = [kind, *reply_ids]
            if kind == "dm":
                reply_query += " AND ((sender=? AND recipient=?) OR (sender=? AND recipient=?))"
                reply_params.extend([own_id, peer, peer, own_id])
            elif kind == "channel":
                reply_query += " AND recipient=?"
                reply_params.append(peer)
            reply_query += " ORDER BY id DESC"
            for target in db.execute(reply_query, reply_params):
                reply_targets.setdefault(target["packet_id"],
                                         {"id": target["id"], "sender": target["sender"],
                                          "text": target["text"]})
    for message in messages:
        reply_id = message.pop("reply_to_packet_id")
        message["reply_to"] = reply_targets.get(reply_id) if reply_id else None
        if reply_id and message["reply_to"] is None:
            message["reply_to"] = {"id": None, "sender": None, "text": None}
        message["reactions"] = [{"sender": sender, "emoji": emoji}
                                for sender, emoji in reactions.get(message["packet_id"], {}).items()]
        message["can_react"] = bool(message.pop("packet_id"))
    return {"messages": messages, "has_more": has_more, "has_newer": has_newer,
            "unread_count": unread_count,
            "first_unread_id": first_unread["id"] if first_unread else None}


def known_nodes(interface, own_num):
    def number(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    nodes = []
    for num, entry in list((getattr(interface, "nodesByNum", None) or {}).items()):
        if not isinstance(num, int) or not isinstance(entry, dict):
            continue
        user = entry.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        node_id = f"!{num:08x}"
        name = user.get("longName")
        short_name = user.get("shortName")
        last_heard = entry.get("lastHeard")
        hops_away = entry.get("hopsAway")
        raw_position = entry.get("position") or {}
        metrics = entry.get("deviceMetrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        position = None
        if isinstance(raw_position, dict):
            latitude = raw_position.get("latitude")
            longitude = raw_position.get("longitude")
            if latitude is None and isinstance(raw_position.get("latitudeI"), int):
                latitude = raw_position["latitudeI"] / 1e7
            if longitude is None and isinstance(raw_position.get("longitudeI"), int):
                longitude = raw_position["longitudeI"] / 1e7
            if (isinstance(latitude, (int, float)) and not isinstance(latitude, bool) and
                    isinstance(longitude, (int, float)) and not isinstance(longitude, bool) and
                    -90 <= latitude <= 90 and -180 <= longitude <= 180 and
                    (latitude != 0 or longitude != 0)):
                position = {"lat": latitude, "lon": longitude,
                            "altitude": number(raw_position.get("altitude")),
                            "time": number(raw_position.get("time"))}
        nodes.append({"id": node_id,
                      "name": name if isinstance(name, str) and name.strip() else node_id,
                      "short_name": short_name if isinstance(short_name, str) else "",
                      "last_heard": number(last_heard),
                      "hops_away": hops_away if isinstance(hops_away, int) and not isinstance(hops_away, bool) else None,
                      "hardware_model": str(user.get("hwModel")) if user.get("hwModel") is not None else None,
                      "role": str(user.get("role")) if user.get("role") is not None else None,
                      "snr": number(entry.get("snr")),
                      "rssi": number(entry.get("rssi")),
                      "channel": number(entry.get("channel")),
                      "device_metrics": {
                          "battery_level": number(metrics.get("batteryLevel")),
                          "voltage": number(metrics.get("voltage")),
                          "channel_utilization": number(metrics.get("channelUtilization")),
                          "air_util_tx": number(metrics.get("airUtilTx")),
                          "uptime_seconds": number(metrics.get("uptimeSeconds")),
                      },
                      "is_self": num == own_num,
                      "position": position})
    return sorted(nodes, key=lambda n: (n["last_heard"] or 0), reverse=True)


def radio_setting_descriptor(section):
    root = (module_config_pb2.ModuleConfig.DESCRIPTOR if section in
            {"mqtt", "telemetry", "neighbor_info", "store_forward", "range_test"}
            else config_pb2.Config.DESCRIPTOR)
    return root.fields_by_name[section].message_type


def radio_settings_snapshot(interface):
    node = interface.localNode
    own_num = interface.myInfo.my_node_num
    own_entry = (interface.nodesByNum or {}).get(own_num) or {}
    user = own_entry.get("user") or {}
    own_position = own_entry.get("position") or {}
    if not isinstance(own_position, dict):
        own_position = {}
    groups = []
    for section, (title, *field_names) in RADIO_SETTING_GROUPS.items():
        container = node.moduleConfig if section in {"mqtt", "telemetry", "neighbor_info", "store_forward", "range_test"} else node.localConfig
        message = getattr(container, section)
        fields = []
        for name in field_names:
            descriptor = message.DESCRIPTOR.fields_by_name[name]
            secret = (section, name) in {("network", "wifi_psk"), ("mqtt", "password")}
            value = "" if secret else getattr(message, name)
            if descriptor.type == FieldDescriptor.TYPE_ENUM:
                kind = "enum"
                options = [{"value": item.number, "label": item.name}
                           for item in descriptor.enum_type.values]
            elif descriptor.type == FieldDescriptor.TYPE_BOOL:
                kind, options = "bool", None
            elif descriptor.type in {FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE}:
                kind, options = "float", None
            elif descriptor.type == FieldDescriptor.TYPE_STRING:
                kind, options = "string", None
            else:
                kind, options = "integer", None
            fields.append({"name": name, "label": RADIO_FIELD_LABELS.get(name, name),
                           "type": kind, "value": value, "options": options, "secret": secret})
        groups.append({"section": section, "title": title, "fields": fields})
    channels = []
    for channel in node.channels or []:
        channels.append({"index": channel.index, "role": channel.role,
                         "name": channel.settings.name,
                         "uplink_enabled": channel.settings.uplink_enabled,
                         "downlink_enabled": channel.settings.downlink_enabled,
                         "has_psk": bool(channel.settings.psk)})
    return {"node_id": f"!{own_num:08x}", "owner": {
                "long_name": user.get("longName", ""),
                "short_name": user.get("shortName", "")},
            "position": {"latitude": own_position.get("latitude"),
                         "longitude": own_position.get("longitude"),
                         "altitude": own_position.get("altitude")},
            "groups": groups, "channels": channels,
            "channel_roles": [{"value": item.number, "label": item.name}
                              for item in channel_pb2.Channel.Role.DESCRIPTOR.values]}


def validate_setting_change(section, changes):
    if section not in RADIO_SETTING_GROUPS or not isinstance(changes, dict) or not changes:
        raise ValueError("Неизвестная группа настроек")
    allowed = set(RADIO_SETTING_GROUPS[section][1:])
    if set(changes) - allowed or len(changes) > len(allowed):
        raise ValueError("Неизвестный параметр ноды")
    descriptors = radio_setting_descriptor(section).fields_by_name
    clean = {}
    for name, value in changes.items():
        field = descriptors[name]
        if field.type == FieldDescriptor.TYPE_BOOL:
            if not isinstance(value, bool):
                raise ValueError(f"{name}: ожидается переключатель")
        elif field.type == FieldDescriptor.TYPE_ENUM:
            if (not isinstance(value, int) or isinstance(value, bool) or
                    value not in field.enum_type.values_by_number):
                raise ValueError(f"{name}: неверный пункт списка")
        elif field.type == FieldDescriptor.TYPE_STRING:
            if not isinstance(value, str) or len(value.encode("utf-8")) > 120:
                raise ValueError(f"{name}: текст длиннее 120 байт")
        elif field.type in {FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE}:
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{name}: ожидалось число")
        else:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name}: ожидалось целое число")
            lower = 0 if field.type in {FieldDescriptor.TYPE_UINT32, FieldDescriptor.TYPE_UINT64} else -(2 ** 31)
            upper = 2 ** 32 - 1 if lower == 0 else 2 ** 31 - 1
            if not lower <= value <= upper:
                raise ValueError(f"{name}: число вне диапазона")
        if name == "hop_limit" and not 0 <= value <= 7:
            raise ValueError("Лимит хопов: от 0 до 7")
        if name == "fixed_pin" and value != 0 and not 100000 <= value <= 999999:
            raise ValueError("PIN должен содержать 6 цифр")
        clean[name] = value
    return clean


def hop_reply(sender_num, received_hops, interface):
    """Prefer the radio's route estimate; use this packet's path if it is unknown."""
    entry = (getattr(interface, "nodesByNum", None) or {}).get(sender_num) or {}
    route_hops = entry.get("hopsAway") if isinstance(entry, dict) else None
    if isinstance(route_hops, int) and not isinstance(route_hops, bool) and 0 <= route_hops <= 7:
        return f"До твоей ноды: {route_hops} хоп(ов)."
    if received_hops is not None:
        return f"Твоё сообщение пришло за {received_hops} хоп(ов)."
    return "Число хопов до твоей ноды пока недоступно."


def ping_reply(sender_num, received_hops, packet, interface):
    """Add this packet's last-hop radio signal to the existing hop response."""
    def signal(field, digits, unit):
        value = packet.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return f"{value:.{digits}f} {unit}"
        return "нет данных"

    hops = hop_reply(sender_num, received_hops, interface)
    snr = signal("rxSnr", 1, "дБ")
    rssi = signal("rxRssi", 0, "dBm")
    return f"{hops} Последний хоп: SNR {snr}, RSSI {rssi}."


def on_text(packet, interface):
    # The radio may publish its buffered packets during SerialInterface/BLEInterface
    # construction. Process them after the connection has supplied our node ID.
    INCOMING_TEXT.put((interface, packet))


def on_routing(packet, interface):
    INCOMING_ROUTING.put((interface, packet))


def on_traceroute(packet, interface):
    INCOMING_TRACES.put((interface, packet))


def trace_paths(packet, own_num, destination_num):
    discovery = (packet.get("decoded") or {}).get("traceroute") or {}
    if not isinstance(discovery, dict):
        raise ValueError("Ответ трассировки не удалось разобрать")

    def node_id(num):
        return f"!{num:08x}"

    def route_nodes(key, start, end):
        hops = discovery.get(key) or []
        if not isinstance(hops, list) or not all(
                isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 0xFFFFFFFF for n in hops):
            return None
        return [start, *hops, end]

    forward = route_nodes("route", own_num, destination_num)
    if forward is None:
        raise ValueError("Ответ трассировки содержит некорректный маршрут")
    backward = None
    if "routeBack" in discovery or "snrBack" in discovery:
        backward = route_nodes("routeBack", destination_num, own_num)
    return list(map(node_id, forward)), list(map(node_id, backward)) if backward else None


def trace_reply(forward, backward):
    messages = ["Трасса туда: " + " → ".join(forward)]
    if backward:
        messages.append("Трасса обратно: " + " → ".join(backward))
    else:
        messages.append("Обратный путь нода не сообщила.")
    return messages


def process_routing(packet, own_id):
    decoded = packet.get("decoded") or {}
    packet_id = decoded.get("requestId")
    routing = decoded.get("routing") or {}
    if not isinstance(packet_id, int) or not isinstance(routing, dict):
        return
    reason = routing.get("errorReason", "NONE")
    if reason != "NONE":
        update_delivery(own_id, packet_id, "failed", str(reason)[:80])
        return
    with db_connection() as db:
        message = db.execute("""SELECT kind, recipient FROM messages
                                WHERE sender=? AND packet_id=?""",
                             (own_id, packet_id)).fetchone()
    if message is None:
        return
    if message["kind"] in ("longfast", "channel"):
        update_delivery(own_id, packet_id, "relayed")
    elif packet.get("from") == int(message["recipient"][1:], 16):
        update_delivery(own_id, packet_id, "delivered")
    else:
        update_delivery(own_id, packet_id, "relayed")


def process_text(packet, interface, connected_at):
    decoded = packet.get("decoded") or {}
    text = decoded.get("text")
    if decoded.get("emoji") and not isinstance(text, str):
        payload = decoded.get("payload")
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
    if not isinstance(text, str):
        return

    own_num = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
    if own_num is None:
        LOG.warning("Skipping text packet: radio has no own node number")
        return
    sender_num = packet.get("from")
    recipient_num = packet.get("to")
    if sender_num is None or sender_num == own_num:
        return

    sender = packet.get("fromId") or f"!{sender_num:08x}"
    own_id = f"!{own_num:08x}"
    packet_id = packet.get("id")
    start, remaining = packet.get("hopStart"), packet.get("hopLimit")
    hops = start - remaining if isinstance(start, int) and isinstance(remaining, int) and start >= remaining else None
    rx_time = packet.get("rxTime")
    valid_rx_time = isinstance(rx_time, int) and not isinstance(rx_time, bool) and 1577836800 <= rx_time <= time.time() + 86400
    received_at = datetime.fromtimestamp(rx_time, timezone.utc).isoformat() if valid_rx_time else None
    # Backlog is imported into SQLite but must not trigger old auto replies.
    is_live = not valid_rx_time or rx_time >= connected_at - 2
    channel_index = packet.get("channel", 0)
    broadcast_channel = (recipient_num == 0xFFFFFFFF and
                         isinstance(channel_index, int) and not isinstance(channel_index, bool) and
                         0 <= channel_index <= 7 and enabled_channel(channel_index))
    channel_kind = "longfast" if channel_index == CHANNEL_INDEX else "channel"
    channel_recipient = "^all" if channel_index == CHANNEL_INDEX else channel_peer(channel_index)

    if decoded.get("emoji"):
        target_id = decoded.get("replyId")
        if (isinstance(packet_id, int) and packet_id > 0 and
                isinstance(target_id, int) and target_id > 0 and valid_reaction(text)):
            if recipient_num == own_num:
                save_reaction("dm", sender, sender, packet_id, target_id, text, received_at)
            elif broadcast_channel:
                save_reaction(channel_kind, channel_recipient, sender, packet_id, target_id,
                              text, received_at)
        return

    reply_to_packet_id = decoded.get("replyId")
    if (not isinstance(reply_to_packet_id, int) or isinstance(reply_to_packet_id, bool) or
            not 0 < reply_to_packet_id <= 0xFFFFFFFF):
        reply_to_packet_id = None

    if recipient_num == own_num:
        is_new = save_message("dm", sender, own_id, text, hops, packet_id, received_at,
                              reply_to_packet_id=reply_to_packet_id)
        if is_new and is_live and state_snapshot()["bot_enabled"]:
            answer = get_reply_template(text)
            if answer is None:
                command_text = text.strip().casefold()
                if command_text == "w":
                    enqueue(("weather", sender))
                    return
                if command_text == "date":
                    enqueue(("date", sender))
                    return
                if command_text == "stats":
                    enqueue(("stats", sender))
                    return
                if command_text in {"ping", "p"}:
                    answer = ping_reply(sender_num, hops, packet, interface)
                elif command_text == "trace":
                    if queue_trace(sender, "bot", own_id) is None:
                        enqueue(("bot_send", "dm", sender, "Трассировка до этой ноды уже выполняется."))
                    return
                else:
                    return
            enqueue(("bot_send", "dm", sender, answer))
    elif broadcast_channel:
        save_message(channel_kind, sender, channel_recipient, text, hops, packet_id, received_at,
                     reply_to_packet_id=reply_to_packet_id)


def close_interface(interface):
    if interface is not None:
        try:
            interface.close()
        except Exception:
            LOG.exception("Could not close Bluetooth connection")


def radio_worker():
    pub.subscribe(on_text, "meshtastic.receive.text")
    pub.subscribe(on_routing, "meshtastic.receive.routing")
    pub.subscribe(on_traceroute, "meshtastic.receive.traceroute")
    selected = get_setting("selected_device")
    transport = get_setting("selected_transport") or "ble"
    if selected is None:
        selected = os.environ.get("MESHTASTIC_BLE_DEVICE", "")
    set_state(selected_device=selected or None,
              selected_transport=transport if selected else None,
              last_node_id=get_setting("my_node_id"))
    interface = None
    retry_at = 0.0
    nodes_refreshed_at = 0.0
    connected_at = 0.0
    delivery_checked_at = 0.0
    pending_traces = deque()
    trace_requests = {}
    last_trace_sent_at = 0.0

    def interrupt_current_traces(reason):
        for trace in pending_traces:
            finish_trace(trace["history_id"], "interrupted", reason)
        for trace in trace_requests.values():
            finish_trace(trace["history_id"], "interrupted", reason)
        pending_traces.clear()
        trace_requests.clear()

    while True:
        try:
            command = COMMANDS.get(timeout=1)
        except queue.Empty:
            command = None

        if command:
            action = command[0]
            if action == "disconnect":
                selected = ""
                set_setting("selected_device", "")
                close_interface(interface)
                interface = None
                interrupt_current_traces("Нода отключена")
                set_state(connected=False, connecting=False, node_id=None,
                          selected_device=None, selected_transport=None,
                          known_nodes=[], node_settings=None, error=None, notice="Нода отключена")
            elif action == "connect":
                transport, selected = command[1:3]
                set_setting("selected_device", selected)
                set_setting("selected_transport", transport)
                close_interface(interface)
                interface = None
                interrupt_current_traces("Подключение к другой ноде")
                retry_at = 0.0
                set_state(connected=False, connecting=True, node_id=None,
                          selected_device=selected, selected_transport=transport,
                          known_nodes=[], node_settings=None, error=None, notice=None)
            elif action == "scan":
                if interface is not None:
                    set_state(scanning=False, error="Сначала отключите текущую ноду")
                else:
                    try:
                        devices = meshtastic.ble_interface.BLEInterface.scan()
                        set_state(devices=[{"name": d.name or d.address, "address": d.address} for d in devices],
                                  scanning=False, error=None)
                    except Exception as exc:
                        LOG.exception("BLE scan failed")
                        set_state(scanning=False, error=f"Поиск BLE: {exc}"[:180])
            elif action == "channel_config":
                _, index, response = command
                if interface is None:
                    response.put({"error": "Нода отключена"})
                else:
                    try:
                        node = interface.localNode
                        if node.channels is None or index >= len(node.channels):
                            raise ValueError("Канал пока не загружен из ноды")
                        channel = node.channels[index]
                        response.put({"index": index, "role": channel.role,
                                      "name": channel.settings.name,
                                      "uplink_enabled": channel.settings.uplink_enabled,
                                      "downlink_enabled": channel.settings.downlink_enabled,
                                      "psk_hex": channel.settings.psk.hex()})
                    except Exception as exc:
                        response.put({"error": str(exc)[:160]})
            elif action == "node_settings":
                _, section, changes = command
                if interface is None:
                    set_state(error="Нет соединения с нодой; настройки не отправлены")
                else:
                    try:
                        node = interface.localNode
                        container = (node.moduleConfig if section in
                                     {"mqtt", "telemetry", "neighbor_info", "store_forward", "range_test"}
                                     else node.localConfig)
                        message = getattr(container, section)
                        backup = type(message)()
                        backup.CopyFrom(message)
                        try:
                            for name, value in changes.items():
                                setattr(message, name, value)
                            node.writeConfig(section)
                        except Exception:
                            message.CopyFrom(backup)
                            raise
                        set_state(node_settings=radio_settings_snapshot(interface),
                                  node_settings_revision=time.time(), error=None,
                                  notice=f"Параметры «{RADIO_SETTING_GROUPS[section][0]}» отправлены в ноду")
                    except Exception as exc:
                        LOG.exception("Node config update failed")
                        set_state(error=f"Настройки ноды: {exc}"[:180])
            elif action == "node_owner":
                _, long_name, short_name = command
                if interface is None:
                    set_state(error="Нет соединения с нодой; имя не отправлено")
                else:
                    try:
                        own_entry = (interface.nodesByNum or {}).get(interface.myInfo.my_node_num) or {}
                        current_user = own_entry.get("user") or {}
                        interface.localNode.setOwner(
                            long_name=long_name, short_name=short_name,
                            is_licensed=bool(current_user.get("isLicensed")))
                        user = own_entry.setdefault("user", {})
                        user["longName"], user["shortName"] = long_name, short_name
                        set_state(node_settings=radio_settings_snapshot(interface),
                                  node_settings_revision=time.time(), error=None,
                                  notice="Имя ноды отправлено")
                    except Exception as exc:
                        LOG.exception("Node owner update failed")
                        set_state(error=f"Имя ноды: {exc}"[:180])
            elif action == "node_fixed_position":
                _, latitude, longitude, altitude = command
                if interface is None:
                    set_state(error="Нет соединения с нодой; координаты не отправлены")
                else:
                    try:
                        interface.localNode.setFixedPosition(latitude, longitude, altitude)
                        interface.localNode.localConfig.position.fixed_position = True
                        own_entry = (interface.nodesByNum or {}).get(interface.myInfo.my_node_num)
                        if isinstance(own_entry, dict):
                            own_entry["position"] = {"latitude": latitude, "longitude": longitude,
                                                     "altitude": altitude}
                        set_state(node_settings=radio_settings_snapshot(interface),
                                  node_settings_revision=time.time(),
                                  notice="Фиксированные координаты отправлены в ноду", error=None)
                    except Exception as exc:
                        LOG.exception("Fixed position update failed")
                        set_state(error=f"Координаты ноды: {exc}"[:180])
            elif action in ("node_channel", "channel_create"):
                _, index, changes = command
                if interface is None:
                    set_state(error="Нет соединения с нодой; канал не изменён")
                else:
                    try:
                        node = interface.localNode
                        if node.channels is None or index >= len(node.channels):
                            raise ValueError("Канал пока не загружен из ноды")
                        channel = node.channels[index]
                        if action == "channel_create" and channel.role != 0:
                            raise ValueError("Выбранный слот уже занят; обновите список каналов")
                        backup = type(channel)()
                        backup.CopyFrom(channel)
                        try:
                            if "role" in changes:
                                channel.role = changes["role"]
                            for name in ("name", "uplink_enabled", "downlink_enabled"):
                                if name in changes:
                                    setattr(channel.settings, name, changes[name])
                            if "psk_hex" in changes:
                                channel.settings.psk = bytes.fromhex(changes["psk_hex"])
                            node.writeChannel(index)
                        except Exception:
                            channel.CopyFrom(backup)
                            raise
                        snapshot = radio_settings_snapshot(interface)
                        remember_channels(snapshot)
                        set_state(node_settings=snapshot,
                                  node_settings_revision=time.time(), error=None,
                                  notice=f"Канал {index} отправлен в ноду")
                    except Exception as exc:
                        LOG.exception("Node channel update failed")
                        set_state(error=f"Канал: {exc}"[:180])
            elif action == "trace":
                _, destination, history_id, source = command
                if source == "bot" and not state_snapshot()["bot_enabled"]:
                    finish_trace(history_id, "interrupted", "Автоответы бота выключены")
                elif interface is None:
                    finish_trace(history_id, "interrupted", "Нода отключена")
                    if source == "bot":
                        set_state(error="Нода отключена; трассировка не запущена")
                else:
                    if len(pending_traces) < 8:
                        pending_traces.append({"destination": destination,
                                               "history_id": history_id, "source": source})
                    else:
                        finish_trace(history_id, "failed", "Очередь трассировок заполнена")
                        if source == "bot" and state_snapshot()["bot_enabled"]:
                            enqueue(("bot_send", "dm", destination,
                                     "Очередь трассировок заполнена. Повтори запрос позже."))
            elif action == "react":
                _, kind, peer, message_id, emoji = command
                if interface is None:
                    set_state(error="Нет соединения с нодой; реакция не отправлена")
                else:
                    try:
                        own_id = f"!{interface.myInfo.my_node_num:08x}"
                        with db_connection() as db:
                            original = db.execute(
                                "SELECT kind, sender, recipient, packet_id FROM messages WHERE id=?",
                                (message_id,)).fetchone()
                        if (original is None or original["kind"] != kind or
                                not original["packet_id"] or
                                (kind == "dm" and
                                 {original["sender"], original["recipient"]} != {own_id, peer}) or
                                (kind == "channel" and original["recipient"] != peer)):
                            raise ValueError("Исходное сообщение больше не доступно")
                        reaction = mesh_pb2.MeshPacket()
                        reaction.channel = int(peer[1:]) if kind == "channel" else (CHANNEL_INDEX if kind == "longfast" else 0)
                        reaction.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
                        reaction.decoded.payload = emoji.encode("utf-8")
                        reaction.decoded.emoji = 1
                        reaction.decoded.reply_id = original["packet_id"]
                        destination = peer if kind == "dm" else "^all"
                        sent = interface._sendPacket(reaction, destinationId=destination, wantAck=True)
                        save_reaction(kind, peer if kind in ("dm", "channel") else "^all", own_id,
                                      sent.id, original["packet_id"], emoji)
                        set_state(notice="Реакция передана в радио", error=None)
                    except Exception as exc:
                        LOG.exception("Reaction send failed")
                        set_state(error=f"Отправка реакции: {exc}"[:180])
            elif action in {"send", "bot_send", "weather", "date", "stats"}:
                if action != "send" and not state_snapshot()["bot_enabled"]:
                    continue
                if interface is None:
                    set_state(error="Нет соединения с нодой; сообщение не отправлено")
                else:
                    reply_to_message_id = None
                    if action == "weather":
                        _, destination = command
                        kind = "dm"
                        try:
                            text = weather_reply(get_weather())
                        except RuntimeError:
                            text = f"Погода: {WEATHER_LOCATION} сейчас недоступна. Попробуй позже."
                    elif action == "date":
                        _, destination = command
                        kind = "dm"
                        text = datetime.now(LOCAL_ZONE).strftime(
                            "Дата и время: %d.%m.%Y %H:%M:%S %Z")
                    elif action == "stats":
                        _, destination = command
                        kind = "dm"
                        try:
                            text = stats_reply()
                        except (OSError, ValueError, KeyError) as exc:
                            LOG.warning("Host stats unavailable: %s", exc)
                            text = "Статистика Raspberry Pi сейчас недоступна."
                    else:
                        _, kind, destination, text, *extra = command
                        reply_to_message_id = extra[0] if extra else None
                    try:
                        reply_to_packet_id = None
                        if reply_to_message_id is not None:
                            own_id = f"!{interface.myInfo.my_node_num:08x}"
                            with db_connection() as db:
                                target = db.execute(
                                    "SELECT kind, sender, recipient, packet_id FROM messages WHERE id=?",
                                    (reply_to_message_id,)).fetchone()
                            if (target is None or target["kind"] != kind or
                                    not target["packet_id"] or
                                    (kind == "dm" and
                                     {target["sender"], target["recipient"]} != {own_id, destination}) or
                                    (kind == "channel" and target["recipient"] != destination)):
                                raise ValueError("Сообщение для ответа недоступно")
                            reply_to_packet_id = target["packet_id"]
                        reply_args = {"replyId": reply_to_packet_id} if reply_to_packet_id else {}
                        if kind == "dm":
                            sent = interface.sendText(text, destinationId=destination,
                                                      wantAck=True, **reply_args)
                        else:
                            index = int(destination[1:]) if kind == "channel" else CHANNEL_INDEX
                            if not enabled_channel(index):
                                raise ValueError("Канал больше не активен на ноде")
                            sent = interface.sendText(text, channelIndex=index,
                                                      wantAck=True, **reply_args)
                        save_message(kind, state_snapshot()["node_id"], destination, text, None,
                                     packet_id=sent.id, delivery_status="pending",
                                     reply_to_packet_id=reply_to_packet_id)
                        set_state(notice="Сообщение передано в радио", error=None)
                    except Exception as exc:
                        LOG.exception("Send failed")
                        set_state(error=f"Отправка: {exc}"[:180])

        # Drain packets buffered by the radio while the SDK loaded its config.
        # SQLite's packet identity constraint makes reconnect imports idempotent.
        while True:
            try:
                source, packet = INCOMING_TEXT.get_nowait()
            except queue.Empty:
                break
            if interface is not None and source is interface:
                try:
                    process_text(packet, interface, connected_at)
                except Exception:
                    LOG.exception("Could not import text packet")

        while True:
            try:
                source, packet = INCOMING_ROUTING.get_nowait()
            except queue.Empty:
                break
            if interface is not None and source is interface:
                try:
                    process_routing(packet, f"!{interface.myInfo.my_node_num:08x}")
                    request_id = (packet.get("decoded") or {}).get("requestId")
                    trace = trace_requests.get(request_id)
                    reason = ((packet.get("decoded") or {}).get("routing") or {}).get("errorReason", "NONE")
                    if trace and reason != "NONE":
                        trace_requests.pop(request_id)
                        finish_trace(trace["history_id"], "failed", str(reason)[:120])
                        if trace["source"] == "bot" and state_snapshot()["bot_enabled"]:
                            enqueue(("bot_send", "dm", trace["destination"],
                                     f"Трассировка не удалась: {reason}."))
                except Exception:
                    LOG.exception("Could not process delivery receipt")

        while True:
            try:
                source, packet = INCOMING_TRACES.get_nowait()
            except queue.Empty:
                break
            if interface is not None and source is interface:
                try:
                    request_id = (packet.get("decoded") or {}).get("requestId")
                    trace = trace_requests.get(request_id)
                    if trace and packet.get("from") == trace["node_num"]:
                        trace_requests.pop(request_id)
                        try:
                            forward, backward = trace_paths(
                                packet, interface.myInfo.my_node_num, trace["node_num"])
                            finish_trace(trace["history_id"], "success", forward=forward,
                                         reverse=backward)
                            if trace["source"] == "bot" and state_snapshot()["bot_enabled"]:
                                for reply in trace_reply(forward, backward):
                                    enqueue(("bot_send", "dm", trace["destination"], reply))
                        except ValueError as exc:
                            finish_trace(trace["history_id"], "failed", str(exc))
                            if trace["source"] == "bot" and state_snapshot()["bot_enabled"]:
                                enqueue(("bot_send", "dm", trace["destination"], str(exc)))
                except Exception:
                    LOG.exception("Could not process traceroute response")

        for request_id, trace in list(trace_requests.items()):
            if time.monotonic() - trace["sent_at"] >= TRACE_TIMEOUT_SECONDS:
                trace_requests.pop(request_id)
                finish_trace(trace["history_id"], "timeout", "Нет ответа от ноды за 3 минуты")
                if trace["source"] == "bot" and state_snapshot()["bot_enabled"]:
                    enqueue(("bot_send", "dm", trace["destination"],
                             "Трассировка: ответа от ноды нет за 3 минуты."))

        if interface is not None and pending_traces and time.monotonic() - last_trace_sent_at >= TRACE_COOLDOWN_SECONDS:
            pending = pending_traces.popleft()
            if pending["source"] == "bot" and not state_snapshot()["bot_enabled"]:
                finish_trace(pending["history_id"], "interrupted", "Автоответы бота выключены")
                continue
            destination = pending["destination"]
            try:
                request = interface.sendData(
                    mesh_pb2.RouteDiscovery(), destinationId=destination,
                    portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                    wantResponse=True, channelIndex=CHANNEL_INDEX, hopLimit=7)
                trace_requests[request.id] = {
                    "destination": destination,
                    "node_num": int(destination[1:], 16),
                    "sent_at": time.monotonic(),
                    "history_id": pending["history_id"],
                    "source": pending["source"],
                }
                mark_trace_running(pending["history_id"], request.id)
                last_trace_sent_at = time.monotonic()
            except Exception as exc:
                LOG.exception("Traceroute request failed")
                finish_trace(pending["history_id"], "failed", str(exc)[:120])
                if pending["source"] == "bot" and state_snapshot()["bot_enabled"]:
                    enqueue(("bot_send", "dm", destination,
                             "Не удалось запустить трассировку."))

        if time.monotonic() - delivery_checked_at >= 15:
            expire_delivery()
            delivery_checked_at = time.monotonic()

        if interface is not None:
            try:
                if transport == "ble":
                    healthy = bool(interface.client and interface.client.bleak_client.is_connected)
                else:
                    healthy = bool(getattr(interface, "stream", None) and
                                   interface.stream.is_open and os.path.exists(selected))
                if not healthy:
                    LOG.warning("Radio connection lost")
                    close_interface(interface)
                    interface = None
                    interrupt_current_traces("Соединение с нодой потеряно")
                    set_state(connected=False, node_id=None, known_nodes=[], node_settings=None,
                              error="Соединение с нодой потеряно")
                    retry_at = time.monotonic() + 10
            except Exception as exc:
                LOG.exception("BLE connection check failed")
                close_interface(interface)
                interface = None
                interrupt_current_traces("Соединение с нодой потеряно")
                set_state(connected=False, node_id=None, known_nodes=[], node_settings=None, error=str(exc)[:180])
                retry_at = time.monotonic() + 10

        if interface is not None and time.monotonic() - nodes_refreshed_at >= 5:
            try:
                set_state(known_nodes=known_nodes(interface, interface.myInfo.my_node_num))
                nodes_refreshed_at = time.monotonic()
            except Exception:
                LOG.exception("Could not refresh node list")
                nodes_refreshed_at = time.monotonic()

        if selected and interface is None and time.monotonic() >= retry_at:
            try:
                set_state(connecting=True, error=None)
                connected_at = time.time()
                if transport == "usb":
                    if not valid_usb_device(selected):
                        raise FileNotFoundError(f"USB-порт {selected} не найден")
                    interface = meshtastic.serial_interface.SerialInterface(devPath=selected)
                else:
                    interface = meshtastic.ble_interface.BLEInterface(selected)
                own_num = interface.myInfo.my_node_num
                own_id = f"!{own_num:08x}"
                set_setting("my_node_id", own_id)
                snapshot = radio_settings_snapshot(interface)
                remember_channels(snapshot)
                set_state(connected=True, connecting=False, node_id=own_id,
                          last_node_id=own_id,
                          known_nodes=known_nodes(interface, own_num),
                          node_settings=snapshot,
                          node_settings_revision=time.time(), error=None,
                          notice=f"Подключено: {selected}")
                nodes_refreshed_at = time.monotonic()
                LOG.info("Connected via %s to %s as %s", transport, selected, own_id)
            except Exception as exc:
                LOG.exception("BLE connection failed")
                close_interface(interface)
                interface = None
                retry_at = time.monotonic() + 10
                set_state(connected=False, connecting=False, node_id=None, known_nodes=[], node_settings=None,
                          error=f"Подключение: {exc}"[:180])


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return False
        user, separator, password = decoded.partition(":")
        return bool(separator) and hmac.compare_digest(user, WEB_USER) and hmac.compare_digest(password, WEB_PASSWORD)

    def respond(self, status, body, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; object-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, status, value):
        self.respond(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def require_auth(self):
        if self.authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Meshtastic inbox", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/metrics":
            snapshot = state_snapshot()
            nodes = snapshot.get("known_nodes") or []
            metrics = (
                "# HELP meshtastic_radio_connected Whether the Meshtastic radio is connected.\n"
                "# TYPE meshtastic_radio_connected gauge\n"
                f"meshtastic_radio_connected {int(bool(snapshot.get('connected')))}\n"
                "# HELP meshtastic_radio_connecting Whether the service is connecting to the radio.\n"
                "# TYPE meshtastic_radio_connecting gauge\n"
                f"meshtastic_radio_connecting {int(bool(snapshot.get('connecting')))}\n"
                "# HELP meshtastic_known_nodes Number of nodes in the connected radio NodeDB.\n"
                "# TYPE meshtastic_known_nodes gauge\n"
                f"meshtastic_known_nodes {len(nodes)}\n"
            ).encode("utf-8")
            return self.respond(200, metrics, "text/plain; version=0.0.4; charset=utf-8")
        if not self.require_auth():
            return

        if url.path == "/":
            return self.respond(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/app.js":
            return self.respond(200, SCRIPT, "text/javascript; charset=utf-8")
        if url.path == "/leaflet.js":
            return self.respond(200, LEAFLET_JS, "text/javascript; charset=utf-8")
        if url.path == "/leaflet.css":
            return self.respond(200, LEAFLET_CSS, "text/css; charset=utf-8")
        if url.path == "/emoji-data.json":
            return self.respond(200, EMOJI_DATA, "application/json; charset=utf-8")
        if url.path == "/api/state":
            return self.json_response(200, state_snapshot())
        if url.path == "/api/ports":
            return self.json_response(200, {"devices": list_usb_devices()})
        if url.path == "/api/chats":
            return self.json_response(200, {"chats": list_chats()})
        if url.path == "/api/channels":
            return self.json_response(200, {"channels": list_channels(),
                                            "longfast_unread": unread_counts().get(("longfast", "^all"), 0)})
        if url.path == "/api/channel-config":
            raw_index = parse_qs(url.query).get("index", [""])[0]
            if not raw_index.isdecimal() or not 0 <= int(raw_index) <= 7:
                return self.json_response(400, {"error": "Номер канала: 0–7"})
            if not state_snapshot()["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            response = queue.Queue(maxsize=1)
            if not enqueue(("channel_config", int(raw_index), response)):
                return self.json_response(503, {"error": "Очередь команд заполнена"})
            try:
                config = response.get(timeout=8)
            except queue.Empty:
                return self.json_response(503, {"error": "Нода не ответила вовремя"})
            return self.json_response(503 if "error" in config else 200, config)
        if url.path == "/api/templates":
            return self.json_response(200, {"templates": list_templates()})
        if url.path == "/api/traces":
            target = parse_qs(url.query).get("target", [None])[0]
            if target is not None and not NODE_ID.fullmatch(target):
                return self.json_response(400, {"error": "Неверный ID ноды"})
            return self.json_response(200, {"traces": list_traces(target)})
        if url.path == "/api/weather":
            try:
                return self.json_response(200, {"weather": get_weather(
                    force=parse_qs(url.query).get("refresh", [""])[0] == "1")})
            except RuntimeError as exc:
                return self.json_response(503, {"error": str(exc)})
        if url.path == "/api/messages":
            params = parse_qs(url.query)
            kind = params.get("kind", ["longfast"])[0]
            if kind not in {"longfast", "dm", "channel"}:
                return self.json_response(400, {"error": "Неверный раздел"})
            peer = params.get("peer", [None])[0]
            if kind == "dm" and (not peer or not NODE_ID.fullmatch(peer)):
                return self.json_response(400, {"error": "Укажите ID собеседника"})
            if kind == "channel" and (not isinstance(peer, str) or not re.fullmatch(r"#[0-7]", peer)):
                return self.json_response(400, {"error": "Укажите канал"})
            before_raw = params.get("before", [""])[0]
            after_raw = params.get("after", [""])[0]
            if any(value and (not value.isdecimal() or int(value) <= 0)
                   for value in (before_raw, after_raw)) or (before_raw and after_raw):
                return self.json_response(400, {"error": "Неверный курсор истории"})
            result = get_chat_messages(kind, peer, int(before_raw) if before_raw else None,
                                       int(after_raw) if after_raw else None,
                                       params.get("unread", [""])[0] == "1")
            result["state"] = state_snapshot()
            return self.json_response(200, result)
        return self.respond(404, b'{"error":"not found"}')

    def do_POST(self):
        if not self.require_auth():
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self.json_response(403, {"error": "Неверный источник запроса"})
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            return self.json_response(415, {"error": "Ожидается JSON"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Размер запроса вне диапазона")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Ожидается объект JSON")
        except ValueError as exc:
            return self.json_response(400, {"error": str(exc)})

        path = urlsplit(self.path).path
        if path == "/api/scan":
            if state_snapshot()["connected"]:
                return self.json_response(409, {"error": "Сначала отключите ноду"})
            set_state(scanning=True, devices=[], error=None)
            command = ("scan",)
        elif path == "/api/connect":
            transport, device = data.get("transport"), data.get("device")
            if transport not in {"ble", "usb"}:
                return self.json_response(400, {"error": "Выберите Bluetooth или USB"})
            if not isinstance(device, str) or not 1 <= len(device.strip()) <= 100:
                return self.json_response(400, {"error": "Укажите ноду или USB-порт"})
            if transport == "usb" and not valid_usb_device(device.strip()):
                return self.json_response(400, {"error": "USB-порт не найден на Pi"})
            command = ("connect", transport, device.strip())
        elif path == "/api/disconnect":
            command = ("disconnect",)
        elif path in {"/api/read", "/api/read-all"}:
            kind, peer, ids = data.get("kind"), data.get("peer"), data.get("ids")
            if (kind not in {"dm", "longfast", "channel"} or
                    (kind == "dm" and (not isinstance(peer, str) or not NODE_ID.fullmatch(peer))) or
                    (kind == "channel" and (not isinstance(peer, str) or not re.fullmatch(r"#[0-7]", peer))) or
                    (path == "/api/read" and
                     (not isinstance(ids, list) or not 1 <= len(ids) <= 100 or
                      any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in ids)))):
                return self.json_response(400, {"error": "Неверный запрос прочтения"})
            own_id = get_setting("my_node_id") or state_snapshot()["node_id"]
            scope, scope_params = chat_scope(kind, peer, own_id)
            with db_connection() as db:
                ids_filter = ""
                params = [datetime.now(timezone.utc).isoformat(), *scope_params, own_id]
                if path == "/api/read":
                    placeholders = ",".join("?" for _ in ids)
                    ids_filter = f" AND id IN ({placeholders})"
                    params.extend(ids)
                db.execute(
                    """INSERT OR IGNORE INTO message_reads(message_id,read_at)
                       SELECT id,? FROM messages WHERE """ + scope +
                    " AND sender<>?" + ids_filter, params)
            return self.json_response(200, {"read": True})
        elif path == "/api/traces":
            snapshot = state_snapshot()
            if not snapshot["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            target = data.get("target")
            if not isinstance(target, str) or not NODE_ID.fullmatch(target) or target == snapshot["node_id"]:
                return self.json_response(400, {"error": "Укажите ID другой ноды"})
            trace_id = queue_trace(target, "web", snapshot["node_id"])
            if trace_id is None:
                return self.json_response(409, {"error": "Трассировка до этой ноды уже выполняется или очередь заполнена"})
            return self.json_response(202, {"queued": True, "id": trace_id})
        elif path == "/api/node-settings":
            if not state_snapshot()["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            section = data.get("section")
            try:
                changes = validate_setting_change(section, data.get("changes"))
            except (ValueError, TypeError) as exc:
                return self.json_response(400, {"error": str(exc)})
            command = ("node_settings", section, changes)
        elif path == "/api/node-owner":
            if not state_snapshot()["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            long_name, short_name = data.get("long_name"), data.get("short_name")
            if (not isinstance(long_name, str) or not long_name.strip() or
                    len(long_name.strip().encode("utf-8")) > 39 or
                    not isinstance(short_name, str) or not short_name.strip() or
                    len(short_name.strip().encode("utf-8")) > 4):
                return self.json_response(400, {"error": "Имя: до 39 байт; короткое имя: до 4 байт"})
            command = ("node_owner", long_name.strip(), short_name.strip())
        elif path == "/api/node-fixed-position":
            if not state_snapshot()["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            latitude, longitude, altitude = (data.get("latitude"), data.get("longitude"),
                                             data.get("altitude"))
            if (not isinstance(latitude, (int, float)) or isinstance(latitude, bool) or
                    not math.isfinite(latitude) or not -90 <= latitude <= 90 or
                    not isinstance(longitude, (int, float)) or isinstance(longitude, bool) or
                    not math.isfinite(longitude) or not -180 <= longitude <= 180 or
                    (latitude == 0 and longitude == 0) or
                    not isinstance(altitude, int) or isinstance(altitude, bool) or
                    not -1000 <= altitude <= 10000):
                return self.json_response(400, {"error": "Проверьте широту, долготу и высоту"})
            command = ("node_fixed_position", float(latitude), float(longitude), altitude)
        elif path == "/api/node-channel":
            if not state_snapshot()["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            index, changes = data.get("index"), data.get("changes")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 7:
                return self.json_response(400, {"error": "Номер канала: 0–7"})
            if not isinstance(changes, dict) or not changes or set(changes) - {
                    "role", "name", "uplink_enabled", "downlink_enabled", "psk_hex"}:
                return self.json_response(400, {"error": "Неверные поля канала"})
            if "role" in changes:
                role = changes["role"]
                if (not isinstance(role, int) or isinstance(role, bool) or
                        role not in (0, 1, 2) or (index == 0 and role != 1) or
                        (index != 0 and role == 1)):
                    return self.json_response(400, {"error": "Первый канал должен быть основным; остальные — дополнительными"})
                channels = (state_snapshot().get("node_settings") or {}).get("channels") or []
                roles = {channel["index"]: channel["role"] for channel in channels}
                roles[index] = role
                active = [slot for slot, slot_role in roles.items() if slot_role != 0]
                if active and sorted(active) != list(range(max(active) + 1)):
                    return self.json_response(400, {"error": "Активные каналы должны идти подряд, начиная с 0"})
            if "name" in changes and (not isinstance(changes["name"], str) or
                                      len(changes["name"].encode("utf-8")) >= 12):
                return self.json_response(400, {"error": "Имя канала: менее 12 байт UTF-8"})
            if any(name in changes and not isinstance(changes[name], bool)
                   for name in ("uplink_enabled", "downlink_enabled")):
                return self.json_response(400, {"error": "Неверный переключатель канала"})
            if "psk_hex" in changes and (not isinstance(changes["psk_hex"], str) or
                                         not re.fullmatch(r"(?:[0-9a-fA-F]{2}|[0-9a-fA-F]{32}|[0-9a-fA-F]{64})", changes["psk_hex"])):
                return self.json_response(400, {"error": "Ключ: 2, 32 или 64 символа HEX"})
            command = ("node_channel", index, changes)
        elif path == "/api/channels/create":
            snapshot = state_snapshot()
            if not snapshot["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            name, psk_base64 = data.get("name"), data.get("psk_base64")
            if (not isinstance(name, str) or not name.strip() or
                    len(name.strip().encode("utf-8")) >= 12):
                return self.json_response(400, {"error": "Имя канала: от 1 до 11 байт UTF-8"})
            if not isinstance(psk_base64, str):
                return self.json_response(400, {"error": "Ключ должен быть в формате Base64"})
            try:
                psk_bytes = base64.b64decode(psk_base64, validate=True)
            except (ValueError, binascii.Error):
                return self.json_response(400, {"error": "Неверный ключ Base64"})
            if (base64.b64encode(psk_bytes).decode("ascii") != psk_base64 or
                    not (len(psk_bytes) in (16, 32) or
                         (len(psk_bytes) == 1 and psk_bytes[0] <= 10))):
                return self.json_response(400, {"error": "Ключ: Base64 для 1, 16 или 32 байт"})
            channels = (snapshot.get("node_settings") or {}).get("channels") or []
            free = next((channel["index"] for channel in channels
                         if channel["index"] > 0 and channel["role"] == 0), None)
            if free is None:
                return self.json_response(409, {"error": "На ноде нет свободных каналов"})
            if not enqueue(("channel_create", free,
                            {"role": 2, "name": name.strip(), "psk_hex": psk_bytes.hex()})):
                return self.json_response(503, {"error": "Очередь команд заполнена"})
            return self.json_response(202, {"queued": True, "index": free})
        elif path == "/api/send":
            kind, message = data.get("kind"), data.get("text")
            if not state_snapshot()["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            if kind not in {"dm", "longfast", "channel"} or not isinstance(message, str) or not message.strip() or len(message.strip().encode("utf-8")) > 200:
                return self.json_response(400, {"error": "Текст должен занимать от 1 до 200 байт UTF-8"})
            destination = data.get("destination") if kind in ("dm", "channel") else "^all"
            if kind == "dm" and (not isinstance(destination, str) or not NODE_ID.fullmatch(destination)):
                return self.json_response(400, {"error": "ID получателя: ! и восемь шестнадцатеричных цифр"})
            if kind == "channel" and (not isinstance(destination, str) or not re.fullmatch(r"#[0-7]", destination)
                                      or not enabled_channel(int(destination[1:]))):
                return self.json_response(400, {"error": "Канал неактивен на ноде"})
            reply_to_message_id = data.get("reply_to_id")
            if reply_to_message_id is not None:
                if (not isinstance(reply_to_message_id, int) or isinstance(reply_to_message_id, bool)
                        or reply_to_message_id <= 0):
                    return self.json_response(400, {"error": "Неверный ID сообщения для ответа"})
                own_id = get_setting("my_node_id") or state_snapshot()["node_id"]
                with db_connection() as db:
                    target = db.execute(
                        "SELECT kind, sender, recipient, packet_id FROM messages WHERE id=?",
                        (reply_to_message_id,)).fetchone()
                if (target is None or target["kind"] != kind or not target["packet_id"] or
                        (kind == "dm" and
                         {target["sender"], target["recipient"]} != {own_id, destination}) or
                        (kind == "channel" and target["recipient"] != destination)):
                    return self.json_response(404, {"error": "Сообщение для ответа не найдено"})
            command = ("send", kind, destination, message.strip(), reply_to_message_id)
        elif path == "/api/react":
            kind, peer, message_id, emoji = (data.get("kind"), data.get("peer"),
                                             data.get("message_id"), data.get("emoji"))
            if not state_snapshot()["connected"]:
                return self.json_response(409, {"error": "Сначала подключите ноду"})
            if (kind not in {"dm", "longfast", "channel"} or
                    not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0 or
                    not valid_reaction(emoji) or
                    (kind == "dm" and (not isinstance(peer, str) or not NODE_ID.fullmatch(peer))) or
                    (kind == "channel" and (not isinstance(peer, str) or not re.fullmatch(r"#[0-7]", peer)
                                            or not enabled_channel(int(peer[1:]))))):
                return self.json_response(400, {"error": "Неверная реакция или сообщение"})
            own_id = get_setting("my_node_id") or state_snapshot()["node_id"]
            with db_connection() as db:
                original = db.execute(
                    "SELECT kind, sender, recipient, packet_id FROM messages WHERE id=?",
                    (message_id,)).fetchone()
            if (original is None or original["kind"] != kind or
                    not original["packet_id"] or
                    (kind == "dm" and
                     {original["sender"], original["recipient"]} != {own_id, peer}) or
                    (kind == "channel" and original["recipient"] != peer)):
                return self.json_response(404, {"error": "Сообщение без ID пакета не найдено"})
            command = ("react", kind, peer if kind in ("dm", "channel") else "^all", message_id, emoji)
        elif path == "/api/bot":
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                return self.json_response(400, {"error": "Укажите состояние бота"})
            set_setting("bot_enabled", "1" if enabled else "0")
            set_state(bot_enabled=enabled, notice="Автоответы бота включены" if enabled else "Автоответы бота выключены")
            return self.json_response(200, {"enabled": enabled})
        elif path == "/api/templates":
            key, value, template_id = data.get("key"), data.get("value"), data.get("id")
            if (not isinstance(key, str) or not key.strip() or
                    len(key.strip().encode("utf-8")) > 100 or
                    not isinstance(value, str) or not value.strip() or
                    len(value.strip().encode("utf-8")) > 200):
                return self.json_response(400, {"error": "Ключ: до 100 байт; ответ: до 200 байт UTF-8"})
            if template_id is not None and (not isinstance(template_id, int) or template_id <= 0):
                return self.json_response(400, {"error": "Неверный ID шаблона"})
            try:
                with db_connection() as db:
                    if template_id:
                        cursor = db.execute("UPDATE reply_templates SET key=?, normalized_key=?, value=? WHERE id=?",
                                            (key.strip(), key.strip().casefold(), value.strip(), template_id))
                        if cursor.rowcount == 0:
                            return self.json_response(404, {"error": "Шаблон не найден"})
                    else:
                        db.execute("INSERT INTO reply_templates(key,normalized_key,value) VALUES(?,?,?)",
                                   (key.strip(), key.strip().casefold(), value.strip()))
            except sqlite3.IntegrityError:
                return self.json_response(409, {"error": "Такой ключ уже существует"})
            return self.json_response(200, {"saved": True})
        elif path == "/api/templates/delete":
            template_id = data.get("id")
            if not isinstance(template_id, int) or template_id <= 0:
                return self.json_response(400, {"error": "Неверный ID шаблона"})
            with db_connection() as db:
                cursor = db.execute("DELETE FROM reply_templates WHERE id=?", (template_id,))
            return self.json_response(200 if cursor.rowcount else 404,
                                      {"deleted": bool(cursor.rowcount)})
        else:
            return self.json_response(404, {"error": "Не найдено"})
        if not enqueue(command):
            if command[0] == "scan":
                set_state(scanning=False)
            return self.json_response(503, {"error": "Очередь команд заполнена"})
        return self.json_response(202, {"queued": True})

    def log_message(self, format_string, *args):
        LOG.info("web: " + format_string, *args)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    set_state(bot_enabled=get_setting("bot_enabled") != "0")
    threading.Thread(target=radio_worker, name="meshtastic-ble", daemon=True).start()
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    LOG.info("Web UI listening on %s:%d", WEB_HOST, WEB_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
