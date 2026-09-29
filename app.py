import logging
import os
import json
import requests
import tempfile
import re
import base64
import threading
import time
import markdown
import smtplib
from requests.exceptions import HTTPError
from flask import Flask, request
from dotenv import load_dotenv
#from apprise import Apprise
from urllib.parse import quote
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta, timezone
from collections import Counter, OrderedDict
import sqlite3
import hashlib
from urllib.parse import urlparse
import ipaddress

load_dotenv()
app = Flask(__name__)

# Set up logging

#log_directory = '/app/log'
log_directory = 'A:/git/log'
log_filename = os.path.join(log_directory, 'jellyfin_telegram-notifier.log')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Сколько дней держать ротации (по умолчанию 5), можно переопределить через ENV
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "3"))

def _cleanup_rotated_logs(base_log_path: str, retain_days: int = LOG_RETENTION_DAYS) -> None:
    """
    Удаляет файлы вида 'jellyfin_telegram-notifier.log.YYYY-MM-DD' старше retain_days.
    Не трогает основной файл '...log'.
    """
    try:
        dirpath = os.path.dirname(base_log_path) or "."
        basename = os.path.basename(base_log_path)  # jellyfin_telegram-notifier.log
        # Матчим только суффикс .YYYY-MM-DD
        pattern = re.compile(rf"^{re.escape(basename)}\.(\d{{4}}-\d{{2}}-\d{{2}})$")
        cutoff = (datetime.now().date() - timedelta(days=max(0, int(retain_days))))

        for name in os.listdir(dirpath):
            m = pattern.match(name)
            if not m:
                continue
            date_str = m.group(1)
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                full = os.path.join(dirpath, name)
                try:
                    os.remove(full)
                    logging.info(f"Log cleanup: removed old rotation {name}")
                except Exception as ex:
                    logging.warning(f"Log cleanup: failed to remove {name}: {ex}")
    except Exception as ex:
        logging.warning(f"Log cleanup failed: {ex}")

# Ensure the log directory exists
os.makedirs(log_directory, exist_ok=True)

# Create a handler for rotating log files daily
rotating_handler = TimedRotatingFileHandler(log_filename, when="midnight", interval=1, backupCount=7)
rotating_handler.setLevel(logging.INFO)
rotating_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Add the rotating handler to the logger
logging.getLogger().addHandler(rotating_handler)

# Очистить старые ротации (по умолчанию старше 5 дней)
_cleanup_rotated_logs(log_filename, retain_days=LOG_RETENTION_DAYS)

# Constants
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOTIFY_URL = os.environ.get("GOTIFY_URL", "").rstrip("/")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN", "")
JELLYFIN_BASE_URL = os.environ["JELLYFIN_BASE_URL"].rstrip("/")
JELLYFIN_API_KEY = os.environ["JELLYFIN_API_KEY"]
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
MDBLIST_API_KEY = os.environ.get("MDBLIST_API_KEY", "")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_LANGUAGE = os.getenv("TMDB_LANGUAGE", "en-US")  # напр. "ru-RU"
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/tv"
TMDB_BASE = "https://api.themoviedb.org/3"
LANGUAGE = os.environ["LANGUAGE"]
EPISODE_PREMIERED_WITHIN_X_DAYS = int(os.environ["EPISODE_PREMIERED_WITHIN_X_DAYS"])
SEASON_ADDED_WITHIN_X_DAYS = int(os.environ["SEASON_ADDED_WITHIN_X_DAYS"])
SIGNAL_URL = os.environ.get("SIGNAL_URL", "").rstrip("/")
SIGNAL_NUMBER = os.environ.get("SIGNAL_NUMBER", "")
SIGNAL_RECIPIENTS = os.environ.get("SIGNAL_RECIPIENTS", "")
WHATSAPP_API_URL = os.environ.get("WHATSAPP_API_URL", "").rstrip("/")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "")
WHATSAPP_JID = os.environ.get("WHATSAPP_JID", "")
WHATSAPP_GROUP_JID = os.environ.get("WHATSAPP_GROUP_JID", "")
WHATSAPP_API_USERNAME = os.environ.get("WHATSAPP_API_USERNAME", "")
WHATSAPP_API_PWD = os.environ.get("WHATSAPP_API_PWD", "")
MATRIX_URL = os.environ.get("MATRIX_URL", "").rstrip("/")
MATRIX_ACCESS_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN", "")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID", "")
SMTP_SUBJECT = "Новый релиз в Jellyfin"
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_TO   = os.environ.get("SMTP_TO", "")  # список через запятую/пробел
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1") not in ("0", "", "false", "False")   # для STARTTLS (587)
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "0") in ("1", "true", "True")   # для SMTPS (465); если 1, то TLS не используем
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")   # ID канала, например C0123456789
# --- Home Assistant notifications ---
HA_BASE_URL = os.getenv("HA_BASE_URL", "").rstrip("/")          # например: http://192.168.1.10:8123
HA_TOKEN    = os.getenv("HA_TOKEN", "")                         # Long-Lived Access Token из профиля HA
HA_VERIFY_SSL = os.getenv("HA_VERIFY_SSL", "1").lower() in ("1","true","yes","on")
# --- Pushover ---
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "")  # ваш user/group key
PUSHOVER_TOKEN    = os.getenv("PUSHOVER_TOKEN", "")     # ваш app token
PUSHOVER_SOUND    = os.getenv("PUSHOVER_SOUND", "")     # опц.: имя звука (см. API sounds)
PUSHOVER_DEVICE   = os.getenv("PUSHOVER_DEVICE", "")    # опц.: конкретное устройство
PUSHOVER_PRIORITY = int(os.getenv("PUSHOVER_PRIORITY", "0"))  # -2..2
PUSHOVER_HTML     = os.getenv("PUSHOVER_HTML", "0").lower() in ("1","true","yes","on")

# если будете использовать экстренный приоритет (2)
PUSHOVER_EMERGENCY_RETRY  = int(os.getenv("PUSHOVER_EMERGENCY_RETRY",  "60"))   # >= 30 сек
PUSHOVER_EMERGENCY_EXPIRE = int(os.getenv("PUSHOVER_EMERGENCY_EXPIRE", "600"))  # сек
# --- Pushover retry/timing ---
PUSHOVER_TIMEOUT_SEC        = float(os.getenv("PUSHOVER_TIMEOUT_SEC", "10"))   # таймаут одного запроса
PUSHOVER_RETRIES            = int(os.getenv("PUSHOVER_RETRIES", "3"))          # сколько попыток всего
PUSHOVER_RETRY_BASE_DELAY   = float(os.getenv("PUSHOVER_RETRY_BASE_DELAY", "0.7"))  # стартовая пауза, сек
PUSHOVER_RETRY_BACKOFF      = float(os.getenv("PUSHOVER_RETRY_BACKOFF", "1.8"))     # множитель экспоненты

# Куда слать по умолчанию:
# для мобильного приложения указывайте notify/<имя_сервиса>, напр. "notify/mobile_app_m2007j20cg"
# для встроенной «постоянной» нотификации укажите "persistent_notification/create"
HA_DEFAULT_SERVICE = os.getenv("HA_DEFAULT_SERVICE", "persistent_notification/create")
# Показывать ссылку на постер в persistent_notification
HA_PN_IMAGE_LINK = os.getenv("HA_PN_IMAGE_LINK", "1").lower() in ("1","true","yes","on")
HA_PN_IMAGE_LABEL = os.getenv("HA_PN_IMAGE_LABEL", "Poster")  # Заголовок перед ссылкой
# --- Jellyfin: In-App сообщения (в клиент) ---
JELLYFIN_INAPP_ENABLED = os.getenv("JELLYFIN_INAPP_ENABLED", "1") == "1"
JELLYFIN_INAPP_TIMEOUT_MS = int(os.getenv("JELLYFIN_INAPP_TIMEOUT_MS", "800"))      # сколько висит поп-ап
JELLYFIN_INAPP_ACTIVE_WITHIN_SEC = int(os.getenv("JELLYFIN_INAPP_ACTIVE_WITHIN_SEC", "900"))  # «активность» сессии
JELLYFIN_INAPP_TITLE = os.getenv("JELLYFIN_INAPP_TITLE", "Jellyfin")
JELLYFIN_INAPP_FORCE_MODAL = os.getenv("JELLYFIN_INAPP_FORCE_MODAL", "1").lower() in ("1","true","yes","on")
# --- Reddit ---
REDDIT_ENABLED     = os.getenv("REDDIT_ENABLED", "1").lower() in ("1","true","yes","on")
REDDIT_APP_ID      = os.getenv("REDDIT_APP_ID", "")
REDDIT_APP_SECRET  = os.getenv("REDDIT_APP_SECRET", "")
REDDIT_USERNAME    = os.getenv("REDDIT_USERNAME", "")
REDDIT_PASSWORD    = os.getenv("REDDIT_PASSWORD", "")
REDDIT_SUBREDDIT   = os.getenv("REDDIT_SUBREDDIT", "MySubJellynotify")     # без /r/
REDDIT_USER_AGENT  = os.getenv("REDDIT_USER_AGENT", "jellyfin-bot/1.0 (by u/your_username)")
# опционально
REDDIT_SEND_REPLIES = os.getenv("REDDIT_SEND_REPLIES", "1").lower() in ("1","true","yes","on")
REDDIT_SPOILER      = os.getenv("REDDIT_SPOILER", "0").lower() in ("1","true","yes","on")
REDDIT_NSFW         = os.getenv("REDDIT_NSFW", "0").lower() in ("1","true","yes","on")
# --- Reddit post mode ---
# 1 = как сейчас: пост-ссылка (картинка), а описание — отдельным комментарием
# 0 = старый вариант: self-post, сверху ссылка на постер, ниже описание в том же посте
REDDIT_SPLIT_TO_COMMENT = os.getenv("REDDIT_SPLIT_TO_COMMENT", "1").lower() in ("1","true","yes","on")
# --- Synology Chat ---
SYNOCHAT_ENABLED       = os.getenv("SYNOCHAT_ENABLED", "1").lower() in ("1","true","yes","on")
SYNOCHAT_WEBHOOK_URL   = os.getenv("SYNOCHAT_WEBHOOK_URL", "https://vaultwardendr.duckdns.org/webapi/entry.cgi?api=SYNO.Chat.External&method=incoming&version=2&token=%22rSfkUhV6XtEe87OQFai9IUH0C07KvLBZnctQO8COHiNLoLzSPhwCmUp2rN3pVuIz%22").strip()   # полный URL из Incoming Webhook
SYNOCHAT_TIMEOUT_SEC   = float(os.getenv("SYNOCHAT_TIMEOUT_SEC", "8"))
SYNOCHAT_VERIFY_SSL    = os.getenv("SYNOCHAT_VERIFY_SSL", "1").lower() in ("1","true","yes","on")
SYNOCHAT_INCLUDE_POSTER = os.getenv("SYNOCHAT_INCLUDE_POSTER", "1").lower() in ("1","true","yes","on")
SYNOCHAT_CA_BUNDLE = os.getenv("SYNOCHAT_CA_BUNDLE", "").strip()  # путь к .pem (опционально)
SYNOCHAT_RETRIES = int(os.getenv("SYNOCHAT_RETRIES", "3"))
SYNOCHAT_RETRY_BASE_DELAY = float(os.getenv("SYNOCHAT_RETRY_BASE_DELAY", "0.8"))
SYNOCHAT_RETRY_BACKOFF = float(os.getenv("SYNOCHAT_RETRY_BACKOFF", "1.7"))
#выключение контроля добавленного контента
DISABLE_DEDUP = os.getenv("NOTIFIER_DISABLE_DEDUP", "0").lower() in ("1", "true", "yes")
#настройки для фильмов
MOVIE_POLL_ENABLED = os.getenv("MOVIE_POLL_ENABLED", "1").lower() in ("1", "true", "yes")
MOVIE_POLL_INTERVAL_SEC = int(os.getenv("MOVIE_POLL_INTERVAL_SEC", "120"))   # каждые 5 минут
MOVIE_POLL_GRACE_MIN = int(os.getenv("MOVIE_POLL_GRACE_MIN", "45"))  # не трогать фильмы, созданные за последние N минут
MOVIE_POLL_PAGE_SIZE = int(os.getenv("MOVIE_POLL_PAGE_SIZE", "2000"))  # сколько брать за 1 запрос
MOVIE_POLL_MAX_TOTAL = int(os.getenv("MOVIE_POLL_MAX_TOTAL", "0"))    # 0 = не ограничивать общее число
# GC БД качества
QUALITY_GC_ENABLED = os.getenv("QUALITY_GC_ENABLED", "1").lower() in ("1","true","yes","on")
QUALITY_GC_INTERVAL_HOURS = int(os.getenv("QUALITY_GC_INTERVAL_HOURS", "24"))   # как часто чистить
QUALITY_GC_GRACE_DAYS = int(os.getenv("QUALITY_GC_GRACE_DAYS", "1"))            # не трогать записи моложе N дней
QUALITY_GC_PAGE_SIZE = int(os.getenv("QUALITY_GC_PAGE_SIZE", "500"))            # сколько фильмов за раз тянуть из Jellyfin
# Форсированная одноразовая очистка БД качества при старте
FORCE_QUALITY_GC_ON_START = os.getenv("FORCE_QUALITY_GC_ON_START", "0").lower() in ("1","true","yes","on")
# Необязательное переопределение grace-срока именно для форс-запуска (по умолчанию 0 = удалять сразу)
FORCE_QUALITY_GC_GRACE_DAYS = os.getenv("FORCE_QUALITY_GC_GRACE_DAYS")
# Сжать БД после очистки
FORCE_QUALITY_GC_VACUUM = os.getenv("FORCE_QUALITY_GC_VACUUM", "0").lower() in ("1","true","yes","on")
#выключить отправку информации о звуковых дорожках
INCLUDE_AUDIO_TRACKS = os.getenv("INCLUDE_AUDIO_TRACKS", "1").lower() in ("1", "true", "yes", "on")
#подавление дублирующих сообщение webhook если это было обновление контента
SUPPRESS_WEBHOOK_AFTER_QUALITY_UPDATE_MIN = int(os.getenv("SUPPRESS_WEBHOOK_AFTER_QUALITY_UPDATE_MIN", "60"))  # по умолчанию 60 минут
# Опрос сериалов (по новым/изменённым эпизодам)
SERIES_POLL_ENABLED = os.getenv("SERIES_POLL_ENABLED", "1").lower() in ("1","true","yes","on")
SERIES_POLL_INTERVAL_SEC = int(os.getenv("SERIES_POLL_INTERVAL_SEC", "120"))  # период, сек
SERIES_POLL_PAGE_SIZE = int(os.getenv("SERIES_POLL_PAGE_SIZE", "2000"))
SERIES_POLL_MAX_TOTAL = int(os.getenv("SERIES_POLL_MAX_TOTAL", "0"))  # 0 = без ограничения
SERIES_POLL_GRACE_MIN = int(os.getenv("SERIES_POLL_GRACE_MIN", "0"))  # свежие эпизоды отдаём на откуп вебхуку
# Посылать ли уведомление при ПЕРВОМ обнаружении сезона (по умолчанию нет)
SERIES_POLL_INITIAL_ANNOUNCE = os.getenv("SERIES_POLL_INITIAL_ANNOUNCE", "1").lower() in ("1","true","yes","on")
# Блокировать таймеры отправки на время сканирования библиотеки Jellyfin
NOTIFY_BLOCK_DURING_SCAN = os.getenv("NOTIFY_BLOCK_DURING_SCAN", "0").lower() in ("1","true","yes","on")
SCAN_RECHECK_DELAY_SEC = int(os.getenv("SCAN_RECHECK_DELAY_SEC", "5"))   # пауза между проверками
MAX_SCAN_WAIT_MIN = int(os.getenv("MAX_SCAN_WAIT_MIN", "3"))             # 0 = ждать бесконечно
# Какие имена задач считать «сканом» (нижний регистр, через запятую)
SCAN_TASK_NAME_MATCH = [s.strip() for s in os.getenv(
    "SCAN_TASK_NAME_MATCH",
    "scan,library,metadata,refresh"
).lower().split(",") if s.strip()]
EXTERNAL_CACHE_ENABLED = os.getenv("EXTERNAL_CACHE_ENABLED", "1").lower() in ("1","true","yes","on")
TRAILER_CACHE_TTL_DAYS = int(os.getenv("TRAILER_CACHE_TTL_DAYS", "30"))
RATINGS_CACHE_TTL_DAYS = int(os.getenv("RATINGS_CACHE_TTL_DAYS", "14"))
# Пределы для блока аудио-дорожек у сезонов
SEASON_AUDIO_TRACKS_MAX = int(os.getenv("SEASON_AUDIO_TRACKS_MAX", "12"))   # максимум уникальных дорожек
SEASON_AUDIO_SCAN_LIMIT = int(os.getenv("SEASON_AUDIO_SCAN_LIMIT", "50"))   # максимум серий для сканирования (present)
#Для whatsapp повторные отправки
WHATSAPP_IMAGE_RETRY_ATTEMPTS = int(os.getenv("WHATSAPP_IMAGE_RETRY_ATTEMPTS", "3"))
WHATSAPP_IMAGE_RETRY_DELAY_SEC = int(os.getenv("WHATSAPP_IMAGE_RETRY_DELAY_SEC", "2"))
# --- Episode/Season quality polling (по сериям -> уведомление на сезон) ---
EP_QUALITY_POLL_ENABLED = (os.getenv("EP_QUALITY_POLL_ENABLED", "1").lower() in ("1","true","yes","on"))
EP_QUALITY_POLL_INTERVAL_SEC = int(os.getenv("EP_QUALITY_POLL_INTERVAL_SEC", "120"))
EP_QUALITY_POLL_PAGE_SIZE = int(os.getenv("EP_QUALITY_POLL_PAGE_SIZE", "10000"))
EP_QUALITY_POLL_MAX_TOTAL = int(os.getenv("EP_QUALITY_POLL_MAX_TOTAL", "0"))  # 0 = без ограничения
# Для "свежих" эпизодов можно переиспользовать SERIES_POLL_GRACE_MIN
# Опрос музыкальных альбомов (по новым/изменённым альбомам)
ALBUM_POLL_ENABLED = os.getenv("ALBUM_POLL_ENABLED", "1").lower() in ("1","true","yes","on")
ALBUM_POLL_INTERVAL_SEC = int(os.getenv("ALBUM_POLL_INTERVAL_SEC", "300"))  # период, сек
ALBUM_POLL_PAGE_SIZE = int(os.getenv("ALBUM_POLL_PAGE_SIZE", "2000"))
ALBUM_POLL_MAX_TOTAL = int(os.getenv("ALBUM_POLL_MAX_TOTAL", "0"))  # 0 = без ограничения
ALBUM_POLL_GRACE_MIN = int(os.getenv("ALBUM_POLL_GRACE_MIN", "0"))  # свежие альбомы отдаём вебхуку (у нас его нет) -> 0
# Опциональный вывод списка треков в сообщении про новый альбом
ALBUM_TRACKLIST_ENABLED = os.getenv("ALBUM_TRACKLIST_ENABLED", "1").lower() in ("1","true","yes","on")
ALBUM_TRACKLIST_LIMIT = int(os.getenv("ALBUM_TRACKLIST_LIMIT", "5"))  # максимальное число строк
ALBUM_TRACKLIST_SHOW_DURATION = os.getenv("ALBUM_TRACKLIST_SHOW_DURATION", "1").lower() in ("1","true","yes","on")
# --- Books poll ---
BOOK_POLL_ENABLED = os.getenv("BOOK_POLL_ENABLED", "1").lower() in ("1","true","yes","on")
BOOK_POLL_INTERVAL_SEC = int(os.getenv("BOOK_POLL_INTERVAL_SEC", "300"))
BOOK_POLL_PAGE_SIZE = int(os.getenv("BOOK_POLL_PAGE_SIZE", "1000"))
BOOK_POLL_MAX_TOTAL = int(os.getenv("BOOK_POLL_MAX_TOTAL", "0"))  # 0 = без ограничения
BOOK_POLL_GRACE_MIN = int(os.getenv("BOOK_POLL_GRACE_MIN", "0"))  # 0 — сразу оповещаем кодом
# --- MusicVideo (клипы) poll ---
MVID_POLL_ENABLED = os.getenv("MVID_POLL_ENABLED", "1").lower() in ("1","true","yes","on")
MVID_POLL_INTERVAL_SEC = int(os.getenv("MVID_POLL_INTERVAL_SEC", "300"))
MVID_POLL_PAGE_SIZE = int(os.getenv("MVID_POLL_PAGE_SIZE", "1000"))
MVID_POLL_MAX_TOTAL = int(os.getenv("MVID_POLL_MAX_TOTAL", "0"))  # 0 = без ограничения
MVID_POLL_GRACE_MIN = int(os.getenv("MVID_POLL_GRACE_MIN", "0"))  # 0 — оповещаем сразу кодом
# --- Outbound proxy for notifications ---
# Пример: http://user:pass@1.2.3.4:8080  или  socks5h://user:pass@127.0.0.1:1080 или http://192.168.1.34:2088
NOTIFY_PROXY_URL = os.getenv("NOTIFY_PROXY_URL", "").strip()

# Список хостов/масок, для которых прокси НЕ использовать (через запятую).
# Поддержка шаблонов: exact, *.domain.tld, localhost.
NOTIFY_PROXY_NO = [h.strip() for h in os.getenv("NOTIFY_PROXY_NO", "192.168.1.*").split(",") if h.strip()]

# Прогонять ли через прокси локальные/приватные адреса (RFC1918, localhost)
NOTIFY_PROXY_FOR_INTERNAL = os.getenv("NOTIFY_PROXY_FOR_INTERNAL", "0").lower() in ("1","true","yes","on")

# --- Incremental polling (high-watermark) helpers ---
POLL_BACKFILL_MIN = int(os.getenv("POLL_BACKFILL_MIN", "15"))

SERIES_LIBRARY_IDS = os.getenv("SERIES_LIBRARY_IDS", "")  # "libId1,libId2"
TV_PARENT_IDS = [s.strip() for s in SERIES_LIBRARY_IDS.split(",") if s.strip()]
# Вебхука больше нет — всё делает пуллер
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0").lower() in ("1","true","yes","on")
# Глобальные переменные
imgbb_upload_done = threading.Event()   # Сигнал о завершении загрузки
uploaded_image_url = None               # Здесь хранится ссылка после удачной загрузки
# Gotify больше не добавляем в APPRISE_URLS вообще!
#APPRISE_OTHER_URLS = os.environ.get("APPRISE_OTHER_URLS", "")
#APPRISE_URLS = APPRISE_OTHER_URLS.strip()

#apobj = Apprise()
#for url in APPRISE_URLS.split():
#    apobj.add(url)

# Path for the JSON file to store notified items
#notified_items_file = '/app/data/notified_items.json'
notified_items_file = 'A:/git/notified_items.json'

# === SQLite для качества (только для Movie на первом этапе) ===
QUALITY_DB_FILE = os.path.join(os.path.dirname(notified_items_file), "media_quality.db")
os.makedirs(os.path.dirname(QUALITY_DB_FILE), exist_ok=True)

def _utcnow_iso() -> str:
    """
    Возвращает текущий момент в UTC в формате ISO 8601 c 'Z' на конце,
    без микросекунд (например: 2025-08-31T19:45:00Z).
    """
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def _init_quality_db():
    conn = sqlite3.connect(QUALITY_DB_FILE)
    try:
        cur = conn.cursor()
        # снимок по конкретному ItemId (история)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS media_quality (
            item_id TEXT PRIMARY KEY,
            movie_name TEXT,
            year INTEGER,
            video_codec TEXT,
            video_bitrate INTEGER,
            width INTEGER,
            height INTEGER,
            fps REAL,
            bit_depth INTEGER,
            dynamic_range TEXT,
            audio_codec TEXT,
            audio_bitrate INTEGER,
            audio_channels INTEGER,
            container TEXT,
            size_bytes INTEGER,
            duration_sec REAL,
            signature TEXT,
            date_seen TEXT
        )""")
        # "последняя версия" по логическому ключу (tmdb/imdb или name+year)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS content_quality (
            logical_key TEXT PRIMARY KEY,
            last_item_id TEXT,
            movie_name TEXT,
            year INTEGER,
            video_codec TEXT,
            video_bitrate INTEGER,
            width INTEGER,
            height INTEGER,
            fps REAL,
            bit_depth INTEGER,
            dynamic_range TEXT,
            audio_codec TEXT,
            audio_bitrate INTEGER,
            audio_channels INTEGER,
            container TEXT,
            size_bytes INTEGER,
            duration_sec REAL,
            signature TEXT,
            date_seen TEXT
        )""")
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS recent_quality_updates
                    (
                        logical_key
                        TEXT
                        PRIMARY
                        KEY,
                        notified_at
                        TEXT,
                        item_id
                        TEXT
                    )
                    """)
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS season_progress
                    (
                        season_id
                        TEXT
                        PRIMARY
                        KEY,
                        series_id
                        TEXT,
                        series_name
                        TEXT,
                        season_number
                        INTEGER,
                        release_year
                        INTEGER,
                        present
                        INTEGER
                        DEFAULT
                        0, -- сколько фактически есть серий на диске
                        total
                        INTEGER
                        DEFAULT
                        0, -- сколько всего серий (present + missing)
                        last_notified_present
                        INTEGER
                        DEFAULT
                        0, -- до какого значения уже сообщали
                        updated_at
                        TEXT
                    )
                    """)
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS external_cache
                    (
                        cache_key
                        TEXT
                        PRIMARY
                        KEY,  -- уникальный ключ (см. ниже)
                        kind
                        TEXT
                        NOT
                        NULL, -- 'trailer' | 'ratings'
                        subkind
                        TEXT, -- 'movie' | 'show' (для рейтингов/трейлеров)
                        value
                        TEXT, -- для трейлера: URL; для рейтингов: готовый текст
                        updated_at
                        TEXT  -- ISO8601 UTC, когда обновляли
                    )
                    """)
        # метаданные приложения/БД
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_meta
                    (
                        key
                        TEXT
                        PRIMARY
                        KEY,
                        value
                        TEXT
                    )
                    """)
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS season_quality
                    (
                        season_id
                        TEXT
                        PRIMARY
                        KEY,
                        series_id
                        TEXT,
                        series_name
                        TEXT,
                        season_number
                        INTEGER,
                        release_year
                        INTEGER,
                        signature
                        TEXT, -- агрегированный снимок качества по доступным эпизодам
                        updated_at
                        TEXT  -- ISO
                    )""")
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS album_announced
                    (
                        logical_key
                        TEXT
                        PRIMARY
                        KEY,
                        announced_at
                        TEXT,
                        item_id
                        TEXT,
                        album_name
                        TEXT,
                        artist_name
                        TEXT,
                        year
                        INTEGER
                    )
                    """)
        # --- NEW: фильмы, уже «объявленные» (дедуп в БД) ---
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS movie_announced
                    (
                        logical_key
                        TEXT
                        PRIMARY
                        KEY,
                        announced_at
                        TEXT,
                        item_id
                        TEXT,
                        movie_name
                        TEXT,
                        year
                        INTEGER
                    )
                    """)
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS book_announced
                    (
                        logical_key
                        TEXT
                        PRIMARY
                        KEY,
                        announced_at
                        TEXT,
                        item_id
                        TEXT,
                        title
                        TEXT,
                        authors
                        TEXT,
                        year
                        INTEGER
                    )
                    """)
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS musicvideo_announced
                    (
                        logical_key
                        TEXT
                        PRIMARY
                        KEY,
                        announced_at
                        TEXT,
                        item_id
                        TEXT,
                        title
                        TEXT,
                        artist
                        TEXT,
                        year
                        INTEGER
                    )
                    """)
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_meta
                    (
                        key
                        TEXT
                        PRIMARY
                        KEY,
                        value
                        TEXT
                    )
                    """)
        # при первом создании БД зафиксируем флаг «не отправлено»
        cur.execute("""
                    INSERT INTO app_meta(key, value)
                    VALUES ('congrats_sent', '0') ON CONFLICT(key) DO NOTHING
                    """)
        # Миграция: добавим episode_count, если столбца нет
        cur.execute("PRAGMA table_info(season_quality)")
        cols = {r[1] for r in cur.fetchall()}
        if "episode_count" not in cols:
            cur.execute("ALTER TABLE season_quality ADD COLUMN episode_count INTEGER")
        # если нет штампа создания БД — проставим сейчас
        cur.execute("SELECT value FROM app_meta WHERE key='db_created_at'")
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO app_meta(key,value) VALUES('db_created_at', ?)", (_utcnow_iso(),))
        # --- Мягкая миграция: добавляем колонку image_profiles, если её ещё нет
        try:
            cur.execute("ALTER TABLE media_quality ADD COLUMN image_profiles TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE content_quality ADD COLUMN image_profiles TEXT")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()

_init_quality_db()

# Пробуем облегчить запросы
def _iso_utc_now_z(dt=None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.isoformat(timespec='seconds').replace('+00:00','Z')

def _poll_since_get(key: str) -> str:
    """
    Берём сохранённый watermark. Если ещё не было — сеем текущим временем минус POLL_BACKFILL_MIN (мин).
    """
    iso = _meta_get(key)
    if iso:
        return iso
    seeded = _iso_utc_now_z(datetime.now(timezone.utc) - timedelta(minutes=max(POLL_BACKFILL_MIN, 1)))
    _meta_set(key, seeded)
    return seeded

def _poll_since_bump(key: str, now_utc: datetime | None = None):
    _meta_set(key, _iso_utc_now_z(now_utc))
#Оповещение о готовноасти базы данных
def _meta_get(key: str) -> str | None:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_meta WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as ex:
        logging.debug(f"_meta_get({key}) failed: {ex}")
        return None
    finally:
        try: conn.close()
        except: pass

def _meta_set(key: str, value: str):
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO app_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))
        conn.commit()
    except Exception as ex:
        logging.debug(f"_meta_set({key}) failed: {ex}")
    finally:
        try: conn.close()
        except: pass

# Убедимся, что папка /app/data существует
os.makedirs(os.path.dirname(notified_items_file), exist_ok=True)

# Function to load notified items from the JSON file
def load_notified_items():
    # Если файл есть — читаем, иначе просто возвращаем {}
    try:
        if os.path.exists(notified_items_file):
            with open(notified_items_file, 'r', encoding='utf-8') as file:
                return json.load(file) or {}
    except Exception as ex:
        logging.debug(f"notified_items.json read skipped: {ex}")
    return {}

# Function to save notified items to the JSON file
def save_notified_items(notified_items_to_save):
    # Файл больше не используем
    return


notified_items = load_notified_items()

# 2. Словарь переводов
MESSAGES = {
    "en": {
        "new_movie_title": "🍿New Movie Added🍿",
        "new_season_title": "📺New Season Added📺",
        "new_episode_title": "📺New Episode Added📺",
        "new_album_title": "🎵New Album Added🎵",
        "new_runtime": "🕒Runtime🕒",
        "new_ratings_movie": "⭐Ratings movie⭐",
        "new_ratings_show": "⭐Ratings show⭐",
        "new_trailer": "Trailer",
        "new_release_date": "Release Date",
        "new_series": "Series",
        "new_episode_t": "Episode Title",
        "audio_tracks": "Audio tracks",
        "image_profiles": "Image profiles",
        "quality_updated": "🔼Quality update🔼",
        "season_added_progress": "Added {added} of {total} episodes",
        "season_added_count_only": "Added {added} episodes",
        "new_track_count": "Tracks",
        "album_tracklist": "Tracklist",
        "album_tracklist_more": "…and {n} more",
        "new_book_title": "📖New book Added📖",
        "new_authors": "Author(s)",
        "new_isbn": "ISBN",
        "new_book_header": "📖New book Added📖",
        "new_audiobook_header": "💿New audiobook added💿",
        "new_musicvideo_header": "🎶New music video added🎶",
        "new_musicvideo_artist": "Artist",
        "new_musicvideo_album": "Album",
        "onboarding_congrats": "🎉 Congratulations! The app is ready to use.",
    },
    "ru": {
        "new_movie_title": "🍿Новый фильм добавлен🍿",
        "new_season_title": "📺Новый сезон добавлен📺",
        "new_episode_title": "📺Новый эпизод добавлен📺",
        "new_album_title": "🎵Новый альбом добавлен🎵",
        "new_runtime": "🕒Продолжительность🕒",
        "new_ratings_movie": "⭐Рейтинги фильма⭐",
        "new_ratings_show": "⭐Рейтинги сериала⭐",
        "new_trailer": "Трейлер",
        "new_release_date": "Дата выхода",
        "new_series": "Сериал",
        "new_episode_t": "Название эпизода",
        "audio_tracks": "Аудио-дорожки",
        "image_profiles": "Профили изображения",
        "quality_updated": "🔼Обновление качества🔼",
        "season_added_progress": "Добавлено {added} из {total} серий",
        "season_added_count_only": "Добавлено {added} серий",
        "new_track_count": "Количество треков",
        "album_tracklist": "Список треков",
        "album_tracklist_more": "…и ещё {n}",
        "new_book_title": "📖Новая книга добавлена📖",
        "new_authors": "Автор(ы)",
        "new_isbn": "ISBN",
        "new_book_header": "📖Новая книга добавлена📖",
        "new_audiobook_header": "💿Новая аудиокнига добавлена💿",
        "new_musicvideo_header": "🎶Новый клип добавлен🎶",
        "new_musicvideo_artist": "Исполнитель",
        "new_musicvideo_album": "Альбом",
        "onboarding_congrats": "🎉 Поздравляю! Программа готова к работе.",
    }
}
#Выбираем рабочий язык: если заданный отсутствует в MESSAGES — ставим en
LANG = LANGUAGE if LANGUAGE in MESSAGES else "en"

def t(key: str) -> str:
    """
    Возвращает перевод по ключу для текущего языка LANG.
    Если ключ отсутствует — падает KeyError, чтобы вы не пропустили необходимость перевода.
    """
    return MESSAGES[LANG][key]

#Обнаружение сканирования
def _task_name_matches(name: str | None) -> bool:
    if not name:
        return False
    n = name.lower()
    return any(seg in n for seg in SCAN_TASK_NAME_MATCH)

def is_jellyfin_scanning() -> tuple[bool, str | None]:
    """
    Пытаемся понять, выполняется ли сейчас скан/рефреш медиатеки/метаданных в Jellyfin.
    Jellyfin 12 использует канонический GET /ScheduledTasks; состояние выполнения
    определяется по State/IsRunning в элементах ответа.
    Возврат: (True/False, краткое описание)
    """
    headers = {'accept': 'application/json'}
    params = {'ApiKey': JELLYFIN_API_KEY}

    try:
        url = f"{JELLYFIN_BASE_URL}/ScheduledTasks"
        r = requests.get(url, headers=headers, params=params, timeout=8)
        r.raise_for_status()
        data = r.json() or []
        for task in data:
            name = task.get("Name") or task.get("Key") or ""
            state = (task.get("State") or "").lower()
            is_running = bool(task.get("IsRunning")) or state in ("running", "executing", "inprogress")
            if is_running and _task_name_matches(name):
                prog = task.get("CurrentProgressPercentage") or task.get("Progress") or task.get("PercentComplete")
                desc = f"{name} {prog}%" if prog is not None else name
                return True, desc
    except Exception as ex:
        logging.debug(f"Jellyfin scheduled tasks check failed: {ex}")

    return False, None

def wait_until_scan_idle(reason: str = ""):
    """
    Если включён NOTIFY_BLOCK_DURING_SCAN — ждём завершения скана Jellyfin.
    MAX_SCAN_WAIT_MIN=0 => ждём бесконечно.
    """
    if not NOTIFY_BLOCK_DURING_SCAN:
        return
    start = time.time()
    first_log = True
    while True:
        running, detail = is_jellyfin_scanning()
        if not running:
            if not first_log:
                logging.info("Jellyfin scan finished, resume timers.")
            return
        if first_log:
            logging.info(f"Timers paused: Jellyfin is scanning ({detail or 'library task running'})"
                         + (f" [reason: {reason}]" if reason else ""))
            first_log = False
        if MAX_SCAN_WAIT_MIN and (time.time() - start) > MAX_SCAN_WAIT_MIN * 60:
            logging.warning("Max wait for scan reached; resuming timers anyway.")
            return
        time.sleep(max(SCAN_RECHECK_DELAY_SEC, 1))

def _movie_poll_loop():
    while True:
        try:
            wait_until_scan_idle("movie poll")
            poll_recent_movies_once()
        except Exception as ex:
            logging.warning(f"Movie poll loop error: {ex}")
        time.sleep(MOVIE_POLL_INTERVAL_SEC)

def _series_poll_loop():
    while True:
        try:
            wait_until_scan_idle("series poll")
            poll_recent_episodes_once()
        except Exception as ex:
            logging.warning(f"Series poll loop error: {ex}")
        time.sleep(SERIES_POLL_INTERVAL_SEC)

def _wa_get_jid_from_env():
    """
    Возвращает JID из окружения.
    Если задана группа — возвращаем группу.
    Иначе личный чат из WHATSAPP_JID или WHATSAPP_NUMBER.
    """
    group_jid = WHATSAPP_GROUP_JID.strip()
    if group_jid:
        if not group_jid.endswith("@g.us"):
            # допустим, передали только id без @g.us
            group_jid = re.sub(r"[^\w\-]", "", group_jid) + "@g.us"
        return group_jid

    # Личный
    raw = (WHATSAPP_JID or WHATSAPP_NUMBER).strip()
    if not raw:
        return None
    if raw.endswith("@s.whatsapp.net"):
        return raw
    # очищаем до цифр и добавляем домен
    local = re.sub(r"\D", "", raw)
    return f"{local}@s.whatsapp.net" if local else None

def jellyfin_get_tmdb_id(item_id: str) -> str | None:
    """
    Возвращает TMDB ID для любого элемента Jellyfin по его Id.
    Читает Items?Ids=...&Fields=ProviderIds и берёт нужный ключ из ProviderIds.
    """
    try:
        params = {
            "ApiKey": JELLYFIN_API_KEY,
            "Ids": item_id,
            "Fields": "ProviderIds"
        }
        url = f"{JELLYFIN_BASE_URL}/Items"
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        items = (r.json() or {}).get("Items") or []
        if not items:
            return None
        prov = items[0].get("ProviderIds") or {}
        # разные сервера/версии могут звать ключ по-разному — учтём варианты
        return prov.get("Tmdb") or prov.get("TmdbId") or prov.get("TMDB") or None
    except Exception as ex:
        logging.warning(f"Failed to read ProviderIds for {item_id}: {ex}")
        return None


def fetch_mdblist_ratings(content_type: str, tmdb_id: str) -> str:
    """
    Запрос к https://api.mdblist.com/tmdb/{type}/{tmdbId}
    и формирование текста с найденными рейтингами.
    Возвращает строку вида:
      "- IMDb: 7.8\n- Rotten Tomatoes: 84%\n…"
    или пустую строку при ошибке/отсутствии данных.
    """
    url = f"https://api.mdblist.com/tmdb/{content_type}/{tmdb_id}?apikey={MDBLIST_API_KEY}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        ratings = data.get("ratings")
        if not isinstance(ratings, list):
            return ""

        lines = []
        for r in ratings:
            source = r.get("source")
            value = r.get("value")
            if source is None or value is None:
                continue
            lines.append(f"- {source}: {value}")

        return "\n".join(lines)
    except requests.RequestException as e:
        app.logger.warning(f"MDblist API error for {content_type}/{tmdb_id}: {e}")
        return ""

def upload_image_to_imgbb(image_bytes):
    """
    Загружает изображение на imgbb.com (до 3 попыток) и устанавливает событие по завершении.
    """
    global uploaded_image_url
    uploaded_image_url = None
    imgbb_upload_done.clear()  # Сброс события

    # Проверка наличия ключа API
    if not IMGBB_API_KEY:
        logging.debug("IMGBB_API_KEY не задан — пропускаем загрузку на imgbb.")
        imgbb_upload_done.set()  # Сигнал о завершении (пропуск загрузки)
        return None

    url = "https://api.imgbb.com/1/upload"
    payload = {
        "key": IMGBB_API_KEY,
        "image": base64.b64encode(image_bytes).decode('utf-8')
    }

    for attempt in range(1, 4):
        try:
            logging.info(f"Попытка загрузки на imgbb #{attempt}")
            response = requests.post(url, data=payload, timeout=20)
            response.raise_for_status()
            data = response.json()
            uploaded_image_url = data['data']['url']
            logging.info(f"Изображение успешно загружено на imgbb: {uploaded_image_url}")
            break
        except Exception as ex:
            logging.warning(f"Ошибка загрузки на imgbb (попытка {attempt}): {ex}")
            if attempt < 3:
                time.sleep(2)  # Пауза между попытками

    imgbb_upload_done.set()  # Сигнал, что загрузка завершена (успешно или нет)
    return uploaded_image_url

def wait_for_imgbb_upload(timeout: float | None = 10.0):
    """
    Ждать завершения загрузки на imgbb ограниченное время.
    Возвращает URL или None по таймауту/ошибке.
    """
    signaled = imgbb_upload_done.wait(timeout=timeout if timeout is not None else None)
    if not signaled:
        logging.warning("IMGBB wait timed out; continue without image.")
    return uploaded_image_url


def get_jellyfin_image_and_upload_imgbb(photo_id):
    jellyfin_image_url = f"{JELLYFIN_BASE_URL}/Items/{photo_id}/Images/Primary"
    try:
        resp = requests.get(jellyfin_image_url, timeout=10)
        resp.raise_for_status()
        return upload_image_to_imgbb(resp.content)
    except Exception as ex:
        logging.warning(f"Ошибка скачивания из Jellyfin: {ex}")
        # ВАЖНО: разблокировать потенциальных ожидателей imgbb
        try:
            imgbb_upload_done.set()
        except Exception:
            pass
        return None

def send_discord_message(photo_id, message, title="Jellyfin", uploaded_url=None):
    """
    Отправляет уведомление в Discord через Webhook.
    Картинку берём НАПРЯМУЮ из Jellyfin и прикрепляем как файл.
    Embed ссылается на неё через attachment://filename.
    """
    if not DISCORD_WEBHOOK_URL:
        logging.warning("DISCORD_WEBHOOK_URL not set, skipping Discord notification.")
        return None

    # 1) тянем постер из Jellyfin
    jellyfin_image_url = f"{JELLYFIN_BASE_URL}/Items/{photo_id}/Images/Primary"
    image_bytes = None
    filename = "poster.jpg"
    mimetype = "image/jpeg"
    try:
        r = requests.get(jellyfin_image_url, timeout=30)
        r.raise_for_status()
        image_bytes = r.content
        ct = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
        if "png" in ct:
            filename, mimetype = "poster.png", "image/png"
        elif "webp" in ct:
            filename, mimetype = "poster.webp", "image/webp"
    except Exception as ex:
        logging.warning(f"Discord: failed to fetch image from Jellyfin: {ex}")

    # 2) готовим payload
    payload = {
        "username": title,
        "content": message
    }

    # если есть картинка — добавим embed, указывающий на attachment
    if image_bytes:
        payload["embeds"] = [{
            "image": {"url": f"attachment://{filename}"}
        }]

    try:
        if image_bytes:
            # multipart: payload_json + файл
            files = {
                "file": (filename, image_bytes, mimetype)
            }
            resp = requests.post(
                DISCORD_WEBHOOK_URL,
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files=files,
                timeout=30
            )
        else:
            # без картинки — обычный JSON
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)

        resp.raise_for_status()
        logging.info("Discord notification sent successfully")
        return resp
    except Exception as ex:
        logging.warning(f"Error sending to Discord: {ex}")
        return None

def clean_markdown_for_apprise(text):
    """
    Упрощает markdown-подобные уведомления для plain text и приводит ссылки к единому виду:
    - [текст](url) -> url
    - Убирает повторяющиеся подряд одинаковые url
    - Добавляет префикс '🎥 <перевод new_trailer>:' перед каждой ссылкой (без дублирования)
    - Очищает лишние пробелы по краям строк
    """
    if not text:
        return text

    # 0) Получаем локализованную метку для "Трейлер"
    try:
        trailer_label = t("new_trailer")
    except Exception:
        trailer_label = MESSAGES.get(LANG, {}).get("new_trailer", "Trailer")
    if not trailer_label:
        trailer_label = "Trailer"
    # 1) [текст](url) -> url
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'\2', text)

    # 2) Убираем подряд идущие повторы одного и того же URL
    text = re.sub(r'(https?://\S+)(\s*\1)+', r'\1', text)

    # 3) Сначала убираем уже проставленные префиксы, чтобы не получить дубликаты,
    #    затем добавим их единообразно
    prefix_pattern = rf'🎥\s*{re.escape(trailer_label)}[:]?\s*'
    text = re.sub(rf'{prefix_pattern}(https?://\S+)', r'\1', text)

    # 4) Префиксуем ТОЛЬКО не-musicbrainz ссылки (через колбэк)
    def _prefix_non_mb(m):
        url = m.group(1)
        if re.search(r'https?://(?:[^/\s)]+\.)*musicbrainz\.org(?=[/\s)]|$)', url, re.IGNORECASE):
            return url
        return f'🎥 {trailer_label}: {url}'

    text = re.sub(r'(https?://\S+)', _prefix_non_mb, text)
    # 5) Чистим лишние пробелы по краям строк (сохраняем переносы)
    text = '\n'.join(line.strip() for line in text.split('\n'))

    # Убрать *жирный* и _курсив_
    text = re.sub(r'(\*|_){1,3}(.+?)\1{1,3}', r'\2', text)

    return text

def _extract_bold_line(line: str) -> str | None:
    m = re.fullmatch(r"\*\s*(.+?)\s*\*", (line or "").strip())
    return m.group(1).strip() if m else None

def make_jf_inapp_payload_from_caption(caption: str) -> tuple[str, str]:
    """
    Из Markdown-сообщения собирает:
      header -> первая жирная строка (*...*)
      title  -> вторая жирная строка (*...*)
      overview -> все строки после title до следующей жирной секции/конца
    Возвращает (header, text) где text = "title\\n\\noverview" (без Markdown).
    Если чего-то нет — gracefully деградируем.
    """
    caption = caption or ""
    lines = caption.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # 1) найти header
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    header = _extract_bold_line(lines[i]) if i < len(lines) else None
    if header is None:
        # нет жирной строки — берём первый непустой как "title", а header — дефолт
        first_non_empty = next((ln for ln in lines if ln.strip()), "")
        title_plain = clean_markdown_for_apprise(first_non_empty)
        header_plain = "Jellyfin"
        return header_plain, title_plain

    i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1

    # 2) найти title (вторая жирная строка)
    title_md = _extract_bold_line(lines[i]) if i < len(lines) else None
    i += 1 if title_md is not None else 0

    # 3) собрать overview до следующей жирной секции
    overview_parts = []
    while i < len(lines):
        ln = lines[i]
        if _extract_bold_line(ln) is not None:
            break  # началась следующая секция (*...*)
        overview_parts.append(ln)
        i += 1

    # 4) очистить Markdown → plain
    header_plain = clean_markdown_for_apprise(header)
    title_plain  = clean_markdown_for_apprise(title_md) if title_md else ""
    overview_plain = clean_markdown_for_apprise("\n".join(overview_parts)).strip()

    # Итоговый текст для Jellyfin: только название и описание
    text = title_plain if title_plain else ""
    if overview_plain:
        text = (text + ("\n\n" if text else "")) + overview_plain

    # Fallback, если вдруг всё пусто
    if not text:
        text = clean_markdown_for_apprise(caption)[:500]

    return header_plain or "Jellyfin", text

def _split_caption_for_reddit(caption: str) -> tuple[str, str]:
    """
    Возвращает (title, body_md) для Reddit:
      - title: первая жирная строка (*...*) — «шапка» (например, New Movie Added)
      - body_md: caption БЕЗ «шапки». Начинается с второй жирной строки (название), затем текст.
    Если «шапки» нет — title='Jellyfin', body=исходный caption.
    """
    import re
    caption = (caption or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = caption.split("\n")

    # найти первую жирную строку (*...*)
    header = None
    hdr_idx = None
    for i, ln in enumerate(lines):
        m = re.fullmatch(r"\*\s*(.+?)\s*\*", ln.strip())
        if m:
            header = m.group(1).strip()
            hdr_idx = i
            break

    if header is None:
        return "Jellyfin", caption

    # тело = всё, кроме первой жирной строки (шапки)
    body = "\n".join(lines[:hdr_idx] + lines[hdr_idx+1:])
    # подчистим ведущие пустые строки
    while body.startswith("\n"):
        body = body[1:]
    while body.startswith("\n\n"):
        body = body[2:]
    return header or "Jellyfin", body.strip()



def sanitize_whatsapp_text(text: str) -> str:
    if not text:
        return text

    # Берём язык из переменной окружения
    lang = os.environ.get("LANGUAGE", "en")
    trailer_label = MESSAGES.get(lang, {}).get("new_trailer")

    # 1) Превращаем [любой текст](https://...) в просто https://...
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\2', text)

    # 2) Убираем подряд идущие повторы одного и того же URL
    text = re.sub(r'(https?://\S+)(\s*\1)+', r'\1', text)


    # 3) Сносим уже поставленные префиксы (на всякий)
    prefix_re = rf'🎥\s*{re.escape(trailer_label)}:?[\s]*'
    text = re.sub(rf'{prefix_re}(https?://\S+)', r'\1', text)

    # 4) Префиксуем ТОЛЬКО не-musicbrainz ссылки (через колбэк)
    def _prefix_non_mb(m):
        url = m.group(1)
        if re.search(r'https?://(?:[^/\s)]+\.)*musicbrainz\.org(?=[/\s)]|$)', url, re.IGNORECASE):
            return url
        return f'🎥 {trailer_label} {url}'

    text = re.sub(r'(https?://\S+)', _prefix_non_mb, text)

    # 5) Чистим лишние пробелы
    text = re.sub(r'[ \t]+', ' ', text).strip()

    return text

def markdown_to_pushover_html(text: str) -> str:
    """
    Конвертирует «упрощённый Markdown» ваших уведомлений в HTML,
    совместимый с Pushover (поддерживаются: <b>, <i>, <u>, <a>).
    - Ссылки [текст](url) -> <a href="url">текст</a>
    - Жирный: **…** и строка формата *…* на отдельной строке -> <b>…</b>
    - Курсив: *…* и _…_ -> <i>…</i>
    - Заголовки '# ' в начале строки -> <b>…</b>
    - Маркеры списков "- " / "* " -> "• "
    - Бэктики `…` — убираются (содержимое оставляем как есть, уже экранировано)
    - Переходы строк: \n (теги <br> Pushover не поддерживает)
    Весь неразмеченный текст HTML-экранируется.
    """
    if not text:
        return ""

    s = text.replace("\r\n", "\n").replace("\r", "\n")

    def _esc(t: str) -> str:
        return (t.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
                 .replace('"', "&quot;"))

    # 0) Экранируем всё (чтобы не ломать HTML), дальше вставляем ТОЛЬКО наши теги
    s = _esc(s)

    import re

    # 1) Ссылки: [text](https://url)
    def _link_repl(m: re.Match) -> str:
        txt = m.group(1)
        url = m.group(2)
        # эскейп для href
        url = url.replace("&", "&amp;").replace('"', "&quot;").strip()
        return f'<a href="{url}">{txt}</a>'
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", _link_repl, s)

    # 2) Жирный: **…**
    s = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<b>{m.group(1)}</b>", s)

    # 3) Жирная «цельная строка» в стиле ваших заголовков: *…* на отдельной строке
    s = re.sub(r"(?m)^\*\s*(.+?)\s*\*$", lambda m: f"<b>{m.group(1)}</b>", s)

    # 4) Жирный альтернативный: __…__
    s = re.sub(r"__(.+?)__", lambda m: f"<b>{m.group(1)}</b>", s)

    # 5) Курсив: *…* (внутри строки) — после обработки «цельной строки»
    s = re.sub(r"\*(.+?)\*", lambda m: f"<i>{m.group(1)}</i>", s)

    # 6) Курсив: _…_
    s = re.sub(r"_(.+?)_", lambda m: f"<i>{m.group(1)}</i>", s)

    # 7) Заголовки: '# ' в начале строки -> <b>…</b>
    s = re.sub(r"(?m)^#\s+(.*)$", lambda m: f"<b>{m.group(1)}</b>", s)

    # 8) Маркеры списков -> буллет
    s = re.sub(r"(?m)^\s*[-*]\s+", "• ", s)

    # 9) Убрать инлайн-кодовые бэктики (содержимое уже экранировано на шаге 0)
    s = re.sub(r"`(.+?)`", r"\1", s)

    # 10) Схлопываем лишние тройные переводы в двойные (аккуратнее выглядит)
    s = re.sub(r"\n{3,}", "\n\n", s)

    return s


def send_email_with_image_jellyfin(photo_id: str, subject: str, body_markdown: str):
    """
    Отправляет email с:
      - text/plain (plain-версия текста)
      - text/html (Markdown → HTML)
      - inline-изображением из Jellyfin (через CID)
    Возвращает True/False.
    """
    if not (SMTP_HOST and SMTP_FROM and SMTP_TO):
        logging.debug("Email disabled or misconfigured; skip.")
        return False

    # plain-версия (без форматирования) — используем ваш очиститель
    body_plain = clean_markdown_for_apprise(body_markdown or "")

    # HTML-версия — рендерим из Markdown
    # extensions для более приятных списков/переносов
    body_html_rendered = markdown.markdown(
        body_markdown or "",
        extensions=["extra", "sane_lists", "nl2br"]
    )

    # Тянем картинку из Jellyfin (с повторами)
    img_bytes = None
    img_subtype = "jpeg"
    try:
        img_bytes = _fetch_jellyfin_image_with_retries(photo_id, attempts=3, timeout=10, delay=1.5)
        # subtype подберём осторожно (если есть headers в ретрае — можно хранить вместе)
        # здесь предполагаем jpeg; при желании можно расширить определение
    except Exception as ex:
        logging.warning(f"Email: failed to fetch Jellyfin image: {ex}")

    msg = EmailMessage()
    msg["Subject"] = subject or SMTP_SUBJECT
    msg["From"]    = SMTP_FROM
    recipients = [x.strip() for x in re.split(r"[,\s]+", SMTP_TO) if x.strip()]
    msg["To"]     = ", ".join(recipients)
    msg["Date"]   = formatdate(localtime=True)

    # 1) text/plain
    msg.set_content(body_plain or "")

    # 2) text/html (+ inline image при наличии)
    if img_bytes:
        cid = make_msgid()  # вида <...@domain>
        html_part = f"""\
<html>
  <body>
    <div>{body_html_rendered}</div>
    <p><img src="cid:{cid[1:-1]}" alt="poster"></p>
  </body>
</html>"""
        msg.add_alternative(html_part, subtype="html")
        try:
            # прикрепляем картинку к HTML-части как related
            msg.get_payload()[1].add_related(img_bytes, maintype="image", subtype=img_subtype, cid=cid)
        except Exception as ex:
            logging.warning(f"Email: cannot embed inline image (fallback as attachment): {ex}")
            msg.add_attachment(img_bytes, maintype="image", subtype=img_subtype, filename="poster.jpg")
    else:
        # нет картинки — просто HTML без тега <img>
        msg.add_alternative(f"<html><body>{body_html_rendered}</body></html>", subtype="html")

    # Отправка
    try:
        if SMTP_USE_SSL or SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                if SMTP_USE_TLS:
                    s.starttls()
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        logging.info("Email notification (Markdown->HTML) sent successfully")
        return True
    except Exception as ex:
        logging.warning(f"Email send failed: {ex}")
        return False

def _slack_try_join_channel(channel_id: str) -> bool:
    """
    Пытается добавить бота в PUBLIC-канал (требует scope channels:join).
    Для приватных каналов не сработает — нужно вручную /invite в Slack.
    """
    if not (SLACK_BOT_TOKEN and channel_id):
        return False
    try:
        resp = requests.post(
            "https://slack.com/api/conversations.join",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"channel": channel_id},
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            logging.debug(f"Slack join failed/ignored: {data.get('error')}")
            return False
        return True
    except Exception as ex:
        logging.debug(f"Slack join error: {ex}")
        return False

def send_slack_text_only(message_markdown: str) -> bool:
    """
    Фоллбэк на чат без файла. Использует chat.postMessage.
    """
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        logging.debug("Slack disabled/misconfigured; skip text.")
        return False

    url = "https://slack.com/api/chat.postMessage"
    # Slack понимает mrkdwn (не совсем Markdown). Можно слегка «очистить» текст:
    text_plain = sanitize_whatsapp_text(message_markdown) or ""

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "channel": SLACK_CHANNEL_ID,
        "text": text_plain,
        "mrkdwn": True,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logging.warning(f"Slack chat.postMessage error: {data}")
            return False
        logging.info("Slack text message sent successfully")
        return True
    except Exception as ex:
        logging.warning(f"Slack text send failed: {ex}")
        return False


def send_slack_message_with_image_from_jellyfin(photo_id: str, caption_markdown: str) -> bool:
    """
    Slack: загрузка файла по новому потоку:
      1) files.getUploadURLExternal (получаем upload_url и file_id)
      2) POST байтов картинки на upload_url
      3) files.completeUploadExternal (channel_id + initial_comment)
    Фоллбэк: отправляем просто текст через chat.postMessage.
    """
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        logging.debug("Slack disabled/misconfigured; skip.")
        return False

    # 1) достаём картинку из Jellyfin
    img_bytes = None
    filename = "poster.jpg"
    mimetype = "image/jpeg"
    try:
        if "_fetch_jellyfin_primary" in globals():
            b, mt, fn = _fetch_jellyfin_primary(photo_id)
            img_bytes, mimetype, filename = b, mt, fn
        else:
            jf_url = f"{JELLYFIN_BASE_URL}/Items/{photo_id}/Images/Primary"
            r = requests.get(jf_url, timeout=30)
            r.raise_for_status()
            img_bytes = r.content
            ct = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
            if "png" in ct:
                filename, mimetype = "poster.png", "image/png"
            elif "webp" in ct:
                filename, mimetype = "poster.webp", "image/webp"
    except Exception as ex:
        logging.warning(f"Slack: failed to fetch image from Jellyfin: {ex}")

    if not img_bytes:
        # нет картинки — отправим текст
        return send_slack_text_only(caption_markdown)

    # 2) files.getUploadURLExternal
    auth_h = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    try:
        resp = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=auth_h,
            data={"filename": filename, "length": str(len(img_bytes))},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logging.warning(f"Slack getUploadURLExternal error: {data}")
            return send_slack_text_only(caption_markdown)
        upload_url = data["upload_url"]
        file_id    = data["file_id"]
    except Exception as ex:
        logging.warning(f"Slack getUploadURLExternal failed: {ex}")
        return send_slack_text_only(caption_markdown)

    # 3) POST файла на upload_url
    try:
        # можно сырыми байтами:
        up_headers = {"Content-Type": mimetype}
        up = requests.post(upload_url, data=img_bytes, headers=up_headers, timeout=60)
        # альтернативно: multipart (иногда помогает при прокси):
        # up = requests.post(upload_url, files={"filename": (filename, img_bytes, mimetype)}, timeout=60)
        if up.status_code != 200:
            logging.warning(f"Slack upload_url returned {up.status_code}: {up.text[:200]}")
            return send_slack_text_only(caption_markdown)
    except Exception as ex:
        logging.warning(f"Slack raw upload failed: {ex}")
        return send_slack_text_only(caption_markdown)

    # 4) files.completeUploadExternal (шарим файл в канал + комментарий)
    def _complete_upload():
        comp_payload = {
            "files": [{"id": file_id, "title": filename}],
            "channel_id": SLACK_CHANNEL_ID,
            "initial_comment": sanitize_whatsapp_text(caption_markdown) or "",
        }
        return requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={**auth_h, "Content-Type": "application/json; charset=utf-8"},
            json=comp_payload,
            timeout=30,
        )

    # попытка заранее присоединиться (на случай публичного канала)
    _slack_try_join_channel(SLACK_CHANNEL_ID)

    try:
        comp = _complete_upload()
        comp.raise_for_status()
        comp_data = comp.json()
        if not comp_data.get("ok"):
            if comp_data.get("error") == "not_in_channel":
                # пробуем присоединиться и повторить один раз
                if _slack_try_join_channel(SLACK_CHANNEL_ID):
                    comp = _complete_upload()
                    comp.raise_for_status()
                    comp_data = comp.json()
                    if comp_data.get("ok"):
                        logging.info("Slack image sent successfully (after join).")
                        return True
                logging.warning("Slack: bot is not in the channel. Invite the app (/invite @Bot) and retry.")
            else:
                logging.warning(f"Slack completeUploadExternal error: {comp_data}")
            return send_slack_text_only(caption_markdown)

        logging.info("Slack image (external upload flow) sent successfully")
        return True

    except Exception as ex:
        logging.warning(f"Slack completeUploadExternal failed: {ex}")
        return send_slack_text_only(caption_markdown)

def send_notification(photo_id, caption):
    uploaded_url = get_jellyfin_image_and_upload_imgbb(photo_id)
    """
    1. Всегда отправляет в Telegram напрямую (send_telegram_photo).
    2. Независимо — отправляет напрямую в Gotify (если включен).
    3. Остальные сервисы — через Apprise.
    """
    # Текст без Markdown (подходит для plain-транспорта, в т.ч. WhatsApp)
    caption_plain = clean_markdown_for_apprise(caption)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg_response = send_telegram_photo(photo_id, caption)
        if tg_response and tg_response.ok:
            logging.info("Notification sent via Telegram")
        else:
            # ФОЛБЭК: разбиваем на два сообщения (фото -> текст)
            logging.warning("Telegram (photo+caption) failed; trying split: photo-only then text…")
            ok_photo = send_telegram_photo_only(photo_id)
            ok_text  = send_telegram_text(caption)
            if ok_photo and ok_text:
                logging.info("Telegram split (photo then text) sent successfully")
            else:
                logging.warning("Telegram split fallback failed")
#    tg_GOTIFY = send_gotify_message(photo_id, caption)

    # Gotify: только если параметры заданы
#    gotify_message = clean_markdown_for_apprise(caption)
#    gotify_response = None
    if GOTIFY_URL and GOTIFY_TOKEN:
        gotify_response = send_gotify_message(photo_id, caption, uploaded_url=uploaded_url)
        if gotify_response and gotify_response.ok:
            logging.info("Notification sent via Gotify")
        else:
            logging.warning("Notification failed via Gotify")

    # ======= ДОБАВЛЕНО ДЛЯ DISCORD =======
    if DISCORD_WEBHOOK_URL:
        discord_response = send_discord_message(photo_id, caption, uploaded_url=uploaded_url)
        if discord_response and discord_response.ok:
            logging.info("Notification sent via Discord")
        else:
            logging.warning("Notification failed via Discord")
    # =====================================
    # ======= SLACK: файл-изображение с комментарием =======
    try:
        if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
            ok = send_slack_message_with_image_from_jellyfin(photo_id, caption)
            if ok:
                logging.info("Notification sent via Slack")
            else:
                logging.warning("Notification failed via Slack")
        else:
            logging.debug("Slack disabled or not configured; skip.")
    except Exception as sl_ex:
        logging.warning(f"Slack send failed: {sl_ex}")
    # ======================================================
#Отпрака в reddit
    try:
        if REDDIT_ENABLED:
            # Заголовок = «шапка» (первая жирная строка), тело = caption БЕЗ «шапки»
            post_title, body_md = _split_caption_for_reddit(caption or "")
            external_url = uploaded_url or None  # прямой URL на постер (если есть)

            if REDDIT_SPLIT_TO_COMMENT and external_url:
                # Режим 1: пост-ссылка (картинка), описание — комментарием
                send_reddit_link_post_with_comment(
                    title=post_title,
                    url=external_url,
                    body_markdown=body_md
                )
            else:
                # Режим 0: обычный self-post; если есть URL — поставим его первой строкой в самом посте
                send_reddit_post(
                    title=post_title,
                    body_markdown=body_md,
                    external_image_url=external_url  # может быть None — тогда просто текст
                )
    except Exception as ex:
        logging.warning(f"Reddit wrapper failed: {ex}")
#отправка в jellyfin
    try:
        if JELLYFIN_INAPP_ENABLED:
            # Для клиентов Jellyfin лучше plain text без Markdown
            jf_header, jf_text = make_jf_inapp_payload_from_caption(caption or "")
            send_jellyfin_inapp_message(
                message=jf_text,
                title=jf_header
            )
    except Exception as ex:
        logging.warning(f"Jellyfin in-app notify failed: {ex}")
#Отправка в home assistant
    try:
        if HA_BASE_URL and HA_TOKEN:
            _title = "Jellyfin"
            # Можно красиво вытащить заголовок из первой жирной строки, если хотите:
            # m = re.match(r"\*\s*(.+?)\s*\*", caption); _title = (m.group(1)[:120] if m else _title)

            # uploaded_url — это ваш URL постера (если он есть)
            send_homeassistant_message(
                message=caption,
                title=_title,
                service_path=None,  # берётся из HA_DEFAULT_SERVICE
                notification_id="jellyfin",  # опционально для persistent_notification
                image_url=uploaded_url  # <-- вот тут передаём картинку
            )
    except Exception as ex:
        logging.warning(f"Home Assistant notify wrapper failed: {ex}")
#Отправка в pushover
    try:
        if PUSHOVER_USER_KEY and PUSHOVER_TOKEN:
            _title = "Jellyfin"
            # опционально: вытащим заголовок из первой жирной строки сообщения
            img_bytes = _safe_fetch_jellyfin_image_bytes(photo_id)  # <— напрямую из Jellyfin
            # uploaded_url — ваш уже известный URL постера (если есть)
            html_msg = markdown_to_pushover_html(caption or "")
            send_pushover_message(
                message=html_msg,
                title=_title,
                image_bytes=img_bytes,  # <— передаём байты, никаких i.ibb.co
                sound=(PUSHOVER_SOUND or None),
                priority=PUSHOVER_PRIORITY,
                device=(PUSHOVER_DEVICE or None),
                html=True
            )
    except Exception as ex:
        logging.warning(f"Pushover wrapper failed: {ex}")
    # ======= MATRIX (REST): СНАЧАЛА изображение из Jellyfin, затем текст =======
    try:
        if MATRIX_URL and MATRIX_ACCESS_TOKEN and MATRIX_ROOM_ID:
            ok = send_matrix_image_then_text_from_jellyfin(photo_id, caption)
            if ok:
                logging.info("Notification sent via Matrix (REST, image from Jellyfin then text)")
            else:
                logging.warning("Matrix (REST, Jellyfin): image+text flow failed; trying text-only fallback")
                send_matrix_text_rest(caption)
        else:
            logging.debug("Matrix disabled or not configured; skip.")
    except Exception as m_ex:
        logging.warning(f"Matrix send failed: {m_ex}")
    # ========================================================================
    # --- ОТПРАВКА В SIGNAL ---
    # Plain text для Signal (без Markdown)
    if SIGNAL_URL and SIGNAL_NUMBER:
        signal_resp = send_signal_message_with_image(
            photo_id,
            clean_markdown_for_apprise(caption),
            SIGNAL_NUMBER,
            SIGNAL_RECIPIENTS
        )
        if signal_resp and signal_resp.ok:
            logging.info("Notification sent via Signal")
        else:
            logging.warning("Notification failed via Signal")
    # --------------------------
    # ======= Synology Chat =======
    try:
        if SYNOCHAT_ENABLED and SYNOCHAT_WEBHOOK_URL:
            # plain-текст (Chat не рендерит Markdown как Telegram)
            caption_plain = clean_markdown_for_apprise(caption or "")
            file_url = uploaded_url if (SYNOCHAT_INCLUDE_POSTER and uploaded_url) else None
            send_synology_chat_message(caption_plain, file_url=file_url)
    except Exception as ex:
        logging.warning(f"Synology Chat wrapper failed: {ex}")
    # =============================

    # ======= EMAIL: письмо с inline-картинкой из Jellyfin =======
    try:
        email_ok = send_email_with_image_jellyfin(photo_id, subject=SMTP_SUBJECT, body_markdown=caption)
        if email_ok:
            logging.info("Notification sent via Email")
        else:
            logging.warning("Notification failed via Email")
    except Exception as em_ex:
        logging.warning(f"Email send failed: {em_ex}")

    # ======= WHATSAPP: сначала картинка с подписью (с ретраями), при провале — текст =======
    try:
        wa_jid = _wa_get_jid_from_env()
        if WHATSAPP_API_URL and wa_jid:
            ok_img = send_whatsapp_image_with_retries(
                caption=caption,
                phone_jid=wa_jid,
                image_url=uploaded_url
            )
            if not ok_img:
                logging.warning("WhatsApp image failed after retries; sending text-only fallback")
                send_whatsapp_text_via_rest(caption, phone_jid=wa_jid)
        else:
            logging.debug("WhatsApp disabled or no JID; skip WhatsApp send.")
    except Exception as wa_ex:
        logging.warning(f"WhatsApp send block failed: {wa_ex}")

#    other_services = [url for url in APPRISE_URLS.split() if url]  # убираем пустые строки
#    if other_services:
#        apprise_obj = Apprise()
#        for url in other_services:
#            apprise_obj.add(url)

        # Готовим временный файл для картинки (если фото есть)

#    base_photo_url = f"{JELLYFIN_BASE_URL}/Items/{photo_id}/Images/Primary"
#    attach_param = None
#    try:
#        image_response = requests.get(base_photo_url, timeout=10)
#        if image_response.ok:
#            # Сохраняем изображение во временный файл
#            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
#                tmp.write(image_response.content)
#                tmp_path = tmp.name
#            attach_param = tmp_path
#        else:
#            attach_param = None
#    except Exception as ex:
#        logging.warning(f"Cannot download image: {ex}")
#        attach_param = None

#    caption_plain = clean_markdown_for_apprise(caption)
#    result = apobj.notify(
#        body=caption_plain,
#        title="",
#        attach=attach_param
#    )

#    if attach_param and os.path.exists(attach_param):
#        try:
#            os.remove(attach_param)
#        except Exception as ex:
#            logging.warning(f"Cannot remove temp image: {ex}")

#    if result:
#        logging.info("Notification sent via Apprise")
#    else:
#        logging.warning("Notification failed via Apprise")
#    return None
def _fetch_jellyfin_image_with_retries(photo_id: str, attempts: int = 3, timeout: int = 10, delay: float = 1.5):
    """
    Пытается скачать Primary-постер из Jellyfin с повторами.
    Возвращает bytes или None.
    """
    url = f"{JELLYFIN_BASE_URL}/Items/{photo_id}/Images/Primary"
    last_err = None
    for i in range(1, attempts + 1):
        try:
            # Быстрая проверка доступности (необязательно, но полезно)
            head = requests.head(url, timeout=timeout)
            if head.ok:
                resp = requests.get(url, timeout=timeout)
                resp.raise_for_status()
                return resp.content
            else:
                last_err = f"HTTP {head.status_code}"
        except Exception as ex:
            last_err = ex
        logging.warning(f"Jellyfin image try {i}/{attempts} failed: {last_err}")
        if i < attempts:
            time.sleep(delay)
    return None

def send_telegram_photo(photo_id, caption):
    try:
        # Ограничиваем caption до 1024 символов
    #    if caption and len(caption) > 1024:
    #        caption = caption[:1023] + "..."  # добавляем троеточие, если обрезаем

#        base_photo_url = f"{JELLYFIN_BASE_URL}/Items/{photo_id}/Images"
#        primary_photo_url = f"{base_photo_url}/Primary"

        # Download the image from the jellyfin
#        image_response = requests.get(primary_photo_url)

        # Пытаемся получить картинку с повторами
        image_bytes = _fetch_jellyfin_image_with_retries(photo_id, attempts=3, timeout=10, delay=1.5)
        if not image_bytes:
            logging.warning("Telegram: Jellyfin image unavailable after retries")
            return None

        # Upload the image to the Telegram bot
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "parse_mode": "Markdown"
        }

        files = {'photo': ('photo.jpg', image_bytes, 'image/jpeg')}
        response = requests.post(url, data=data, files=files, timeout=30)
        logging.info("Telegram notification sent successfully")
        return response

    except Exception as ex:
        logging.warning(f"Error sending to Telegram: {ex}")
        return None

def send_telegram_photo_only(photo_id):
    """
    Отправляет ТОЛЬКО фото (без caption) в Telegram.
    Возвращает response при успехе, иначе None.
    """
    try:
        image_bytes = _fetch_jellyfin_image_with_retries(photo_id, attempts=3, timeout=10, delay=1.5)
        if not image_bytes:
            logging.warning("Telegram(photo-only): Jellyfin image unavailable after retries")
            return None

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        data = {"chat_id": TELEGRAM_CHAT_ID}
        files = {'photo': ('photo.jpg', image_bytes, 'image/jpeg')}
        resp = requests.post(url, data=data, files=files, timeout=30)
        resp.raise_for_status()
        logging.info("Telegram photo-only sent successfully")
        return resp
    except Exception as ex:
        logging.warning(f"Telegram photo-only failed: {ex}")
        return None


def send_telegram_text(message: str):
    """
    Отправляет ТОЛЬКО текст в Telegram.
    Сначала пробуем Markdown, при ошибке парсинга падаем в plain-text (очищенный).
    Возвращает response при успехе, иначе None.
    """
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        # Попытка №1: Markdown
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=30)
        if resp.ok:
            logging.info("Telegram text sent (Markdown)")
            return resp

        # Если не ok — пробуем plain
        logging.warning(f"Telegram text markdown failed: {resp.status_code} {resp.text}")
        raise HTTPError(response=resp)

    except Exception as md_ex:
        try:
            # Попытка №2: plain (очищаем markdown, ссылки приводим к простому виду)
            plain = clean_markdown_for_apprise(message) or message
            resp2 = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": plain
            }, timeout=30)
            resp2.raise_for_status()
            logging.info("Telegram text sent (plain fallback)")
            return resp2
        except Exception as ex2:
            logging.warning(f"Telegram text send failed: {ex2}")
            return None

def send_matrix_text_rest(message_markdown: str):
    """
    Отправляет ТОЛЬКО текст в Matrix через REST (v3).
    1) Пытается правильный PUT по спецификации.
    2) Если прокси блокирует PUT (405) — делает POST фоллбэк на тот же путь.
    Возвращает объект response при успехе, иначе None.
    """
    if not (MATRIX_URL and MATRIX_ACCESS_TOKEN and MATRIX_ROOM_ID):
        logging.debug("Matrix not configured; skip.")
        return None

    try:
        # room_id вида "!MNddurK...:example.org" нужно URL-энкодить полностью
        room_enc = quote(MATRIX_ROOM_ID, safe="")
        base = f"{MATRIX_URL.rstrip('/')}/_matrix/client/v3/rooms/{room_enc}/send/m.room.message"

        headers = {
            "Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }

        # Чистим Markdown для plain-текста (Matrix клиенты корректно покажут)
        body_plain = clean_markdown_for_apprise(message_markdown) or ""
        payload = {"msgtype": "m.text", "body": body_plain}

        # Уникальный txnId (в миллисекундах)
        txn_id = f"{int(time.time() * 1000)}txt"
        url = f"{base}/{txn_id}"

        # 1) Правильный путь: PUT (спецификация)
        try:
            resp = requests.put(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            logging.info("Matrix text sent successfully via PUT v3")
            return resp
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 405:
                # 2) Фоллбэк: POST тем же урлом (некоторые reverse-proxy режут PUT)
                logging.warning("Matrix PUT blocked (405). Trying POST fallback…")
                resp2 = requests.post(url, headers=headers, json=payload, timeout=30)
                resp2.raise_for_status()
                logging.info("Matrix text sent successfully via POST fallback")
                return resp2
            else:
                logging.warning(f"Matrix text send failed via PUT: {e}")
                return None

    except Exception as ex:
        logging.warning(f"Matrix text send failed: {ex}")
        return None

def matrix_upload_image_rest(image_bytes: bytes, filename: str, mimetype: str = "image/jpeg") -> str | None:
    """
    Загружает картинку в Matrix content repo и возвращает mxc:// URI.
    Пробуем v3, при 404/405/501 — фоллбэк на r0.
    """
    if not (MATRIX_URL and MATRIX_ACCESS_TOKEN):
        logging.debug("Matrix not configured for media upload; skip.")
        return None

    headers = {"Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}", "Content-Type": mimetype}
    base = MATRIX_URL.rstrip("/")
    url_v3 = f"{base}/_matrix/media/v3/upload?filename={quote(filename)}"

    try:
        r = requests.post(url_v3, headers=headers, data=image_bytes, timeout=30)
        r.raise_for_status()
        return r.json().get("content_uri")
    except requests.exceptions.HTTPError as e:
        code = getattr(e.response, "status_code", None)
        if code in (404, 405, 501):
            logging.warning(f"media/v3/upload returned {code}, trying r0…")
            try:
                url_r0 = f"{base}/_matrix/media/r0/upload?filename={quote(filename)}"
                r2 = requests.post(url_r0, headers=headers, data=image_bytes, timeout=30)
                r2.raise_for_status()
                return r2.json().get("content_uri")
            except Exception as ex2:
                logging.warning(f"Matrix r0 upload failed: {ex2}")
                return None
        logging.warning(f"Matrix v3 upload failed: {e}")
        return None
    except Exception as ex:
        logging.warning(f"Matrix upload failed: {ex}")
        return None


def _matrix_send_event_rest(room_id: str, event_type: str, content: dict):
    """
    Отправляет событие в комнату:
      PUT /_matrix/client/v3/rooms/{roomId}/send/{eventType}/{txnId}
    При 405 — POST на тот же путь.
    Возвращает response или None.
    """
    if not (MATRIX_URL and MATRIX_ACCESS_TOKEN and room_id):
        return None

    room_enc = quote(room_id, safe="")
    base = f"{MATRIX_URL.rstrip('/')}/_matrix/client/v3/rooms/{room_enc}/send/{event_type}"
    txn_id = f"{int(time.time()*1000)}evt"
    url = f"{base}/{txn_id}"
    headers = {"Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}", "Content-Type": "application/json"}

    try:
        resp = requests.put(url, headers=headers, json=content, timeout=30)
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        if getattr(e.response, "status_code", None) == 405:
            logging.warning("PUT blocked (405). Trying POST fallback…")
            try:
                resp2 = requests.post(url, headers=headers, json=content, timeout=30)
                resp2.raise_for_status()
                return resp2
            except Exception as ex2:
                logging.warning(f"Matrix POST fallback failed: {ex2}")
                return None
        logging.warning(f"Matrix send event failed via PUT: {e}")
        return None
    except Exception as ex:
        logging.warning(f"Matrix send event failed: {ex}")
        return None

def _fetch_jellyfin_primary(photo_id: str):
    """
    Возвращает (bytes, mimetype, filename) для Primary-постера из Jellyfin.
    """
    url = f"{JELLYFIN_BASE_URL}/Items/{photo_id}/Images/Primary"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    mimetype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
    ext = ".jpg"
    if "png" in mimetype:
        ext = ".png"
    elif "webp" in mimetype:
        ext = ".webp"
    filename = f"poster{ext}"
    return resp.content, mimetype, filename


def send_matrix_image_then_text_from_jellyfin(photo_id: str, caption_markdown: str) -> bool:
    """
    1) Тянем постер из Jellyfin
    2) Загружаем в Matrix (media repo) -> mxc://
    3) Отправляем m.image (body = имя файла)
    4) Отдельным сообщением отправляем текст (m.text)
    """
    if not (MATRIX_URL and MATRIX_ACCESS_TOKEN and MATRIX_ROOM_ID):
        logging.debug("Matrix not configured; skip.")
        return False

    # 1) картинка из Jellyfin
    try:
        img_bytes, mimetype, filename = _fetch_jellyfin_primary(photo_id)
    except Exception as ex:
        logging.warning(f"Matrix(JF): cannot fetch image from Jellyfin: {ex}")
        # хотя бы текст отправим
        resp_txt = send_matrix_text_rest(caption_markdown)
        return bool(resp_txt and resp_txt.ok)

    # 2) upload -> mxc://
    mxc_uri = matrix_upload_image_rest(img_bytes, filename, mimetype)
    if not mxc_uri:
        logging.warning("Matrix(JF): media upload failed; sending text only.")
        resp_txt = send_matrix_text_rest(caption_markdown)
        return bool(resp_txt and resp_txt.ok)

    # 3) m.image (ВАЖНО: body — имя файла)
    content_img = {
        "msgtype": "m.image",
        "body": filename,
        "url": mxc_uri,
        "info": {
            "mimetype": mimetype,
            "size": len(img_bytes),
        },
    }
    resp_img = _matrix_send_event_rest(MATRIX_ROOM_ID, "m.room.message", content_img)
    img_ok = bool(resp_img and resp_img.ok)

    # 4) затем текст отдельным сообщением
    resp_txt = send_matrix_text_rest(caption_markdown)
    txt_ok = bool(resp_txt and resp_txt.ok)

    if img_ok and txt_ok:
        logging.info("Matrix(JF): image then text sent successfully.")
    else:
        logging.warning("Matrix(JF): image+text flow partially/fully failed.")
    return img_ok and txt_ok

def send_gotify_message(photo_id, message, title="Jellyfin", priority=5, uploaded_url=None):
    """
    Отправка в Gotify. Если картинка не готова — шлём текст без изображения.
    """
    if not GOTIFY_URL or not GOTIFY_TOKEN:
        logging.warning("GOTIFY_URL or GOTIFY_TOKEN not set, skipping Gotify notification.")
        return None

    # Если URL ещё не известен — подождём чуть-чуть, но не блокируемся надолго.
    if uploaded_url is None:
        uploaded_url = wait_for_imgbb_upload(timeout=0.5)

    if uploaded_url:
        message = f"![Poster]({uploaded_url})\n\n{message}"
        big_image_url = uploaded_url
    else:
        big_image_url = None
        logging.debug("IMGBB URL missing — sending Gotify text-only.")

    gotify_url = GOTIFY_URL.rstrip('/')
    url = f"{gotify_url}/message?token={GOTIFY_TOKEN}"

    data = {
        "title": title,
        "message": message,
        "priority": priority,
        "extras": {
            "client::display": {"contentType": "text/markdown"}
        }
    }
    if big_image_url:
        data["extras"]["client::notification"] = {"bigImageUrl": big_image_url}
    headers = {"X-Gotify-Format": "markdown"}

    try:
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()
        logging.info("Gotify notification sent successfully")
        return response
    except Exception as ex:
        logging.warning(f"Error sending to Gotify: {ex}")
        return None

def send_pushover_message(message: str,
                          title: str | None = None,
                          image_url: str | None = None,
                          image_bytes: bytes | None = None,
                          *,
                          sound: str | None = None,
                          priority: int | None = None,
                          device: str | None = None,
                          html: bool = False) -> bool:
    """
    Отправка уведомления в Pushover с ретраями на временные ошибки/таймауты.
    - Ретрай при: requests.Timeout/ConnectionError, HTTP 5xx, HTTP 429.
    - Пауза: экспоненциальная (base * backoff^(attempt-1)).
    """
    try:
        if not (PUSHOVER_USER_KEY and PUSHOVER_TOKEN):
            return False

        endpoint = "https://api.pushover.net/1/messages.json"
        data = {
            "token":   PUSHOVER_TOKEN,
            "user":    PUSHOVER_USER_KEY,
            "message": (message or "")[:1024],
        }
        if title:
            data["title"] = title[:250]
        if device:
            data["device"] = device
        if sound:
            data["sound"] = sound
        if priority is not None:
            data["priority"] = str(priority)
            if int(priority) == 2:
                data["retry"]  = str(max(30, int(PUSHOVER_EMERGENCY_RETRY)))
                data["expire"] = str(max(1,  int(PUSHOVER_EMERGENCY_EXPIRE)))
        if html:
            data["html"] = "1"

        files = None
        # используем уже подготовленные байты; fallback на скачивание по URL оставляем коротким
        if image_bytes:
            files = {"attachment": ("poster.jpg", image_bytes, "image/jpeg")}
        elif image_url:
            try:
                ir = requests.get(image_url, timeout=6)
                ir.raise_for_status()
                content = ir.content
                if len(content) <= 5242880:
                    mime = ir.headers.get("Content-Type") or "image/jpeg"
                    files = {"attachment": ("poster.jpg", content, mime)}
                else:
                    logging.warning("Pushover: image > 5MB, sending without attachment.")
            except Exception as ex:
                logging.warning(f"Pushover: image fetch failed: {ex}")

        # --- Ретраи на отправку ---
        import time
        from requests.exceptions import Timeout, ConnectionError

        attempts = max(1, PUSHOVER_RETRIES)
        delay = max(0.0, PUSHOVER_RETRY_BASE_DELAY)
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.post(
                    endpoint,
                    data=data,
                    files=files,
                    timeout=PUSHOVER_TIMEOUT_SEC,
                    allow_redirects=True
                )
                # успех
                if resp.status_code == 200:
                    logging.info("Pushover notification sent")
                    return True

                # решаем, нужно ли повторять
                retryable_http = resp.status_code in (429, 500, 502, 503, 504)
                if not retryable_http or attempt == attempts:
                    logging.warning(f"Pushover failed {resp.status_code}: {resp.text[:300]}")
                    return False

                logging.warning(f"Pushover HTTP {resp.status_code}, retry {attempt}/{attempts}...")
            except (Timeout, ConnectionError) as ex:
                if attempt == attempts:
                    logging.warning(f"Pushover notify error: {ex}")
                    return False
                logging.warning(f"Pushover network error, retry {attempt}/{attempts}: {ex}")
            except Exception as ex:
                # прочее — не ретраим
                logging.warning(f"Pushover notify error: {ex}")
                return False

            # пауза перед следующей попыткой
            time.sleep(delay)
            delay *= max(1.0, PUSHOVER_RETRY_BACKOFF)

        return False  # теоретически не дойдём

    except Exception as ex:
        logging.warning(f"Pushover notify error: {ex}")
        return False


def _safe_fetch_jellyfin_image_bytes(item_id: str) -> bytes | None:
    """
    Скачивает постер напрямую из Jellyfin, возвращает bytes либо None.
    """
    try:
        url = f"{JELLYFIN_BASE_URL}/Items/{item_id}/Images/Primary"
        # если требуется ключ в query, раскомментируй следующую строку:
        # url = f"{url}?ApiKey={JELLYFIN_API_KEY}"
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        return r.content
    except Exception as ex:
        logging.debug(f"Pushover: Jellyfin image fetch failed for {item_id}: {ex}")
        return None



def send_homeassistant_message(message: str,
                               title: str | None = None,
                               service_path: str | None = None,
                               notification_id: str | None = None,
                               image_url: str | None = None) -> bool:
    """
    Универсальная отправка сервиса Home Assistant.
    По умолчанию используется persistent_notification/create.
    - Для persistent_notification: поддерживаются message, title, notification_id.
      Картинки не поддерживаются — можем (опционально) добавить ссылку в текст.
    - Для прочих сервисов, если они умеют поле 'image', передадим его в 'data.image'.
    """
    try:
        if not HA_BASE_URL or not HA_TOKEN:
            return False

        service_path = (service_path or HA_DEFAULT_SERVICE).strip().strip("/")
        domain, _, service = service_path.partition("/")
        if not domain or not service:
            logging.warning(f"Home Assistant: invalid service_path '{service_path}'")
            return False

        url = f"{HA_BASE_URL}/api/services/{domain}/{service}"
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        }

        # Базовый payload
        final_message = message

        # Если это persistent_notification — добавим ссылку на картинку (если включено)
        if domain == "persistent_notification" and image_url and HA_PN_IMAGE_LINK:
            final_message = f"{message}\n\n{HA_PN_IMAGE_LABEL}: {image_url}"

        payload = {"message": final_message}
        if title:
            payload["title"] = title
        if domain == "persistent_notification" and notification_id:
            payload["notification_id"] = notification_id

        # Для других доменов попробуем вложить картинку стандартным образом
        if domain != "persistent_notification" and image_url:
            payload["data"] = {"image": image_url}

        resp = requests.post(url, headers=headers, json=payload, timeout=8, verify=HA_VERIFY_SSL)
        if resp.status_code != 200:
            logging.warning(f"Home Assistant notify failed {resp.status_code}: {resp.text[:300]}")
            return False

        logging.info(f"Home Assistant notification sent via {domain}/{service}")
        return True

    except Exception as ex:
        logging.warning(f"Home Assistant notify error: {ex}")
        return False

def send_signal_message_with_image(photo_id, message, SIGNAL_NUMBER, SIGNAL_RECIPIENTS, api_url=SIGNAL_URL):
    """
    Отправляет текст и изображение из Jellyfin в Signal через base64_attachments.
    """
    # Скачиваем изображение из Jellyfin
    jellyfin_image_url = f"{JELLYFIN_BASE_URL}/Items/{photo_id}/Images/Primary"
    try:
        image_resp = requests.get(jellyfin_image_url)
        image_resp.raise_for_status()
        image_bytes = image_resp.content
        # Кодируем в base64
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        data = {
            "message": message,
            "number": SIGNAL_NUMBER,
            "recipients": SIGNAL_RECIPIENTS if isinstance(SIGNAL_RECIPIENTS, list) else [SIGNAL_RECIPIENTS],
            "base64_attachments": [image_b64],
        }

        resp = requests.post(api_url, json=data)
        resp.raise_for_status()
        logging.info("Signal image message sent successfully")
        return resp
    except Exception as ex:
        logging.warning(f"Error sending Signal image message: {ex}")
        return None


def send_whatsapp_image_via_rest(
    caption: str,
    phone_jid: str = None,
    image_url: str = None,
    view_once: bool = False,
    compress: bool = False,
    duration: int = 0,
    is_forwarded: bool = False,
):
    img_url = wait_for_imgbb_upload()
    if not img_url:
        logging.warning("Изображение не загружено — пропускаем отправку в WhatsApp.")
        return
    if not WHATSAPP_API_URL:
        logging.warning("WHATSAPP_API_URL not set, skipping WhatsApp image.")
        return None

    phone_jid = phone_jid or _wa_get_jid_from_env()
    if not phone_jid:
        logging.warning("WhatsApp JID is empty, skip sending image.")
        return None

    url = f"{WHATSAPP_API_URL.rstrip('/')}/send/image"
    auth = (WHATSAPP_API_USERNAME, WHATSAPP_API_PWD)

    form = {
        "phone": phone_jid,
        "caption": sanitize_whatsapp_text(caption or ""),
        "view_once": str(bool(view_once)).lower(),
        "compress": str(bool(compress)).lower(),
        "duration": str(int(duration)),
        "is_forwarded": str(bool(is_forwarded)).lower(),
    }

    files = None
    jellyfin_used = False

    if image_url:
        form["image_url"] = image_url
    else:
        logging.warning("WhatsApp image: image_url не задан, пропускаем отправку изображения.")
        return None

    try:
        resp = requests.post(url, data=form, files=files, auth=auth, timeout=30)
        resp.raise_for_status()
        logging.info("WhatsApp image sent successfully")
        return resp
    except requests.exceptions.RequestException as e:
        logging.warning(f"Error sending WhatsApp image: {e}")
        return None

def send_whatsapp_text_via_rest(message: str, phone_jid: str | None = None):
    """
    Шлёт ТОЛЬКО текст. Сначала /send/text, при 404 — /send/message.
    Возвращает response или None.
    """
    if not WHATSAPP_API_URL:
        logging.debug("WhatsApp API URL not set; skip text.")
        return None

    phone_jid = phone_jid or _wa_get_jid_from_env()
    if not phone_jid:
        logging.debug("WhatsApp JID empty; skip text.")
        return None

    base = WHATSAPP_API_URL.rstrip("/")
    url_text = f"{base}/send/text"
    url_msg  = f"{base}/send/message"
    auth = (WHATSAPP_API_USERNAME, WHATSAPP_API_PWD) if (WHATSAPP_API_USERNAME or WHATSAPP_API_PWD) else None

    form = {
        "phone": phone_jid,
        "message": sanitize_whatsapp_text(message or "")
    }

    try:
        r = requests.post(url_text, data=form, auth=auth, timeout=20)
        if r.status_code == 404:
            r = requests.post(url_msg, data=form, auth=auth, timeout=20)
        r.raise_for_status()
        logging.info("WhatsApp text sent successfully")
        return r
    except Exception as ex:
        logging.warning(f"WhatsApp text send failed: {ex}")
        return None

def send_whatsapp_image_with_retries(
    caption: str,
    phone_jid: str | None,
    image_url: str | None = None
) -> bool:
    """
    Пытается отправить изображение с подписью несколько раз.
    True при успехе, False если все попытки провалились.
    """
    attempts = max(1, WHATSAPP_IMAGE_RETRY_ATTEMPTS)
    delay = max(0, WHATSAPP_IMAGE_RETRY_DELAY_SEC)

    for i in range(1, attempts + 1):
        try:
            resp = send_whatsapp_image_via_rest(
                caption=caption,
                phone_jid=phone_jid,
                image_url=image_url
            )
            ok = (resp is not None) and (getattr(resp, "ok", True))
            if ok:
                logging.info(f"WhatsApp image sent on attempt {i}")
                return True
            else:
                logging.warning(f"WhatsApp image attempt {i} failed (no/negative response)")
        except Exception as ex:
            logging.warning(f"WhatsApp image attempt {i} exception: {ex}")
        if i < attempts:
            time.sleep(delay)
    return False

def jellyfin_list_recent_episodes_for_series(series_id: str, *, limit: int = 50) -> list[dict]:
    """
    Возвращает до `limit` самых свежих эпизодов конкретного сериала.
    Поля минимальные: SeasonId/ParentId, DateCreated.
    """
    try:
        params = {
            "ApiKey": JELLYFIN_API_KEY,
            "Fields": "DateCreated,ParentId,SeasonId,ProductionYear",
            "IsMissing": "false",
            "IsUnaired": "false",
            "IsVirtualUnaired": "false",
            "LocationTypes": "FileSystem",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Limit": str(max(limit, 1)),
            # На этом эндпоинте тоже допустим, и снижает нагрузку на подсчёте:
            "EnableTotalRecordCount": "false",
        }
        url = f"{JELLYFIN_BASE_URL}/Shows/{series_id}/Episodes"
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json() or {}
        return data.get("Items") or []
    except Exception as ex:
        logging.warning(f"list_recent_episodes_for_series({series_id}) failed: {ex}")
        return []

def build_season_announce_message(*, series_name_cleaned: str, season_name: str,
                                  release_year, overview_to_use: str,
                                  present: int, total: int,
                                  tmdb_id: str | None, trailer_url: str | None,
                                  season_id: str) -> str:
    msg = f"*{t('new_season_title')}*\n\n*{series_name_cleaned}* *({release_year})*\n\n*{season_name}*"
    if total and present >= 0:
        msg += f"\n\n{t('season_added_progress').format(added=present, total=total)}"
    elif present > 0:
        msg += f"\n\n{t('season_added_count_only').format(added=present)}"
    if overview_to_use:
        msg += f"\n\n{overview_to_use}"

    # рейтинги TMDB/mdblist
    if tmdb_id:
        ratings_text = safe_fetch_mdblist_ratings("show", tmdb_id)
        if ratings_text:
            msg += f"\n\n*{t('new_ratings_show')}*\n{ratings_text}"

    # трейлер
    if trailer_url:
        msg += f"\n\n[🎥]({trailer_url})[{t('new_trailer')}]({trailer_url})"

    # блок «разрешение» (best-effort)
    try:
        res_label = _season_resolution_label(season_id)
        if res_label:
            L = _labels()
            msg += f"\n\n*{L['resolution']}*\n{res_label}"
    except Exception as ex:
        logging.debug(f"(Season) resolution block failed for {season_id}: {ex}")

    # аудиодорожки (по флагу)
    if INCLUDE_AUDIO_TRACKS:
        tracks_block = build_audio_tracks_block_for_season(season_id)
        if tracks_block:
            msg += tracks_block

    return msg


def get_item_details(item_id):
    headers = {'accept': 'application/json', }
    params = {'ApiKey': JELLYFIN_API_KEY, }
    url = f"{JELLYFIN_BASE_URL}/Items?Recursive=true&Fields=DateCreated, Overview&Ids={item_id}"
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()  # Check if request was successful
    return response.json()

def jellyfin_count_tracks_in_album(album_id: str) -> int | None:
    """Возвращает количество песен в музыкальном альбоме.
    Сначала пытаемся взять ChildCount у самого альбома; если нет — считаем дочерние Audio-элементы.
    """
    try:
        # 1) Попробуем получить сам альбом с ChildCount
        params = {'ApiKey': JELLYFIN_API_KEY, 'Ids': album_id, 'Fields': 'ChildCount'}
        url = f"{JELLYFIN_BASE_URL}/Items"
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        items = (r.json() or {}).get('Items') or []
        if items:
            cc = items[0].get('ChildCount')
            if isinstance(cc, int) and cc >= 0:
                return cc

        # 2) Фолбэк: считаем дочерние элементы-аудиотреки
        params = {
            'ApiKey': JELLYFIN_API_KEY,
            'ParentId': album_id,
            'IncludeItemTypes': 'Audio',
            'Recursive': 'false',
            'IsMissing': 'false',
            'LocationTypes': 'FileSystem',
            'Fields': 'LocationType,Path',
        }
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        return len((r.json() or {}).get('Items') or [])
    except Exception as ex:
        logging.warning(f"Album track count failed for {album_id}: {ex}")
        return None

def jellyfin_list_tracks_in_album(album_id: str, *, limit: int | None = None) -> list[dict]:
    """
    Возвращает список треков (минимальные поля) для альбома.
    Поля: Name, IndexNumber, RunTimeTicks
    """
    try:
        params = {
            'ApiKey': JELLYFIN_API_KEY,
            'ParentId': album_id,
            'IncludeItemTypes': 'Audio',
            'Recursive': 'false',
            'IsMissing': 'false',
            'LocationTypes': 'FileSystem',
            'SortBy': 'IndexNumber,Name',
            'SortOrder': 'Ascending',
            'Fields': 'IndexNumber,RunTimeTicks'
        }
        if limit and limit > 0:
            params['Limit'] = str(limit)
        url = f"{JELLYFIN_BASE_URL}/Items"
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        return (r.json() or {}).get('Items') or []
    except Exception as ex:
        logging.warning(f"Album track list failed for {album_id}: {ex}")
        return []


def is_within_last_x_days(date_str, x):
    days_ago = datetime.now() - timedelta(days=x)
    return date_str >= days_ago.isoformat()


def is_not_within_last_x_days(date_str, x):
    days_ago = datetime.now() - timedelta(days=x)
    return date_str < days_ago.isoformat()


def get_youtube_trailer_url(query):
    base_search_url = "https://www.googleapis.com/youtube/v3/search"
    if not YOUTUBE_API_KEY:
        return None
    api_key = YOUTUBE_API_KEY

    params = {
        'part': 'snippet',
        'q': query,
        'type': 'video',
        'key': api_key
    }

    response = requests.get(base_search_url, params=params)
    response.raise_for_status()  # Check for HTTP errors before processing the data
    response_data = response.json()
    video_id = response_data.get("items", [{}])[0].get('id', {}).get('videoId')

    return f"https://www.youtube.com/watch?v={video_id}" if video_id else "Video not found!"


def item_already_notified(item_type, item_name, release_year):
    # В режиме теста всегда считаем, что ещё не отправляли
    if DISABLE_DEDUP:
        logging.debug("Dedup is disabled: treating as NOT notified.")
        return False

    key = f"{item_type}:{item_name}:{release_year}"
    return notified_items.get(key) is True


def mark_item_as_notified(item_type, item_name, release_year, max_items=100):
    # В режиме теста ничего не пишем в файл
    if DISABLE_DEDUP:
        logging.debug("Dedup is disabled: NOT recording notified key.")
        return

    key = f"{item_type}:{item_name}:{release_year}"
    notified_items[key] = True

    # ограничиваем размер «памяти»
    if len(notified_items) > max_items:
        # если уже храните timestamp — удаляйте самый старый; иначе просто pop первого
        notified_items.pop(next(iter(notified_items)))
    save_notified_items(notified_items)

# Сравнение данных о видео

def _get_item_media_info_movie(item_id: str) -> dict:
    """
    Тянем MediaSources/MediaStreams для фильма и уплощаем в dict.
    Дополнительно возвращаем:
      - список аудио-дорожек: audio_tracks + их количество
      - профили изображения: image_profiles (['DV','HDR10',...]) и image_profile_str ("DV, HDR10")
    """
    try:
        headers = {'accept': 'application/json'}
        params = {'ApiKey': JELLYFIN_API_KEY}
        url = f"{JELLYFIN_BASE_URL}/Items?Ids={item_id}&Fields=MediaSources,RunTimeTicks"
        r = requests.get(url, headers=headers, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        item = (data.get("Items") or [{}])[0]
        sources = item.get("MediaSources") or []
        if not sources:
            return {}
        src = sources[0]

        container = src.get("Container")
        overall_bitrate = src.get("Bitrate")
        size_bytes = src.get("Size")
        duration_ticks = src.get("RunTimeTicks") or item.get("RunTimeTicks")
        duration_sec = duration_ticks / 10_000_000 if duration_ticks else None

        vcodec = None; vbitrate = None; width = None; height = None; dyn = None; vdepth = None; fps = None
        acodec = None; abitrate = None; channels = None

        audio_tracks = []
        image_profiles = None  # NEW

        for s in (src.get("MediaStreams") or []):
            stype = s.get("Type")
            if stype == "Video" and vcodec is None:
                # ... ваш код извлечения полей ...
                try:
                    image_profiles = _detect_image_profiles_from_fields(s)
                except Exception:
                    image_profiles = None
                # >>> ФОЛБЭК
                if not image_profiles:
                    image_profiles = ["SDR"]
                # <<<
                vcodec = s.get("Codec")
                vbitrate = s.get("BitRate") or s.get("bitrate") or overall_bitrate
                width = s.get("Width"); height = s.get("Height")
                fps = s.get("AverageFrameRate") or s.get("RealFrameRate")
                vdepth = s.get("BitDepth") or s.get("VideoBitDepth")
                dyn = s.get("ColorTransfer") or s.get("VideoRange") or s.get("ColorPrimaries")
                if isinstance(dyn, str):
                    u = dyn.upper()
                    dyn = "HDR" if ("PQ" in u or "HLG" in u or "HDR" in u or "BT2020" in u) else "SDR"

                # NEW: профили изображения (DV / HDR10+ / HDR10 / HLG / HDR / SDR)
                try:
                    image_profiles = _detect_image_profiles_from_fields(s)
                except Exception:
                    image_profiles = None

            elif stype == "Audio":
                # основной аудио-снимок (для сводки)
                if acodec is None:
                    acodec = s.get("Codec")
                    abitrate = s.get("BitRate") or s.get("bitrate")
                    channels = s.get("Channels")
                # читаем «человеческое» имя дорожки
                label = s.get("DisplayTitle") or s.get("Title")
                if not label:
                    lang = s.get("Language")
                    codec = s.get("Codec")
                    ch = s.get("Channels")
                    layout = s.get("ChannelLayout")
                    parts = []
                    if lang:   parts.append(str(lang).upper())
                    if codec:  parts.append(str(codec).upper())
                    if ch:     parts.append(f"{ch}ch")
                    if layout: parts.append(layout)
                    label = " ".join(parts) or "Audio"
                audio_tracks.append(label)

        approx_kbps = None
        if (not vbitrate) and size_bytes and duration_sec and duration_sec > 0:
            approx_kbps = int((size_bytes * 8) / duration_sec / 1000)

        return {
            "video_codec": vcodec,
            "video_bitrate": vbitrate,
            "approx_video_kbps": approx_kbps,
            "width": width,
            "height": height,
            "fps": fps,
            "bit_depth": vdepth,
            "dynamic_range": dyn or "SDR",
            "audio_codec": acodec,
            "audio_bitrate": abitrate,
            "audio_channels": channels,
            "container": container,
            "size_bytes": size_bytes,
            "duration_sec": duration_sec,
            # аудио-дорожки
            "audio_tracks": audio_tracks,
            "audio_track_count": len(audio_tracks),
            # NEW: профили изображения с фолбэком
            "image_profiles": image_profiles or ["SDR"],
            "image_profile_str": ", ".join(image_profiles or ["SDR"]),
        }
    except Exception as ex:
        logging.warning(f"Media info fetch failed for movie {item_id}: {ex}")
        return {}

def build_audio_tracks_block(q: dict) -> str:
    tracks = (q or {}).get("audio_tracks") or []
    if not tracks:
        return ""
    header = t("audio_tracks")
    lines = "\n".join(f"- {name}" for name in tracks)
    return f"\n\n*{header} ({len(tracks)})*\n{lines}"

def _quality_signature(q: dict) -> str:
    """
    Компактный ключ качества: достаточно чувствителен к реальной замене файла.
    """
    def part(x): return "-" if x in (None, "", 0) else str(x)
    vbr = q.get("video_bitrate") or q.get("approx_video_kbps")
    return "|".join([
        part(q.get("video_codec")),
        f"{part(q.get('width'))}x{part(q.get('height'))}",
        part(vbr),
        part(q.get("dynamic_range")),
        part(q.get("bit_depth")),
        part(q.get("fps")),
        part(q.get("audio_codec")),
        part(q.get("audio_channels")),
        part(q.get("audio_bitrate")),
        part(q.get("container")),
        part(q.get("size_bytes")),
    ])

def _quality_is_substantial(q: dict | None) -> bool:
    """False, если «пустой» снимок (Jellyfin ещё не распарсил потоки)."""
    if not q: return False
    return any([
        q.get("video_codec"),
        (q.get("width") and q.get("height")),
        q.get("audio_codec"),
        q.get("container"),
        q.get("size_bytes"),
    ])

def _fmt_mbps(q: dict) -> str:
    vbr = q.get("video_bitrate")
    if vbr:
        try: return f"{int(vbr)/1000:.1f} Mbps"
        except: return f"{vbr} kbps"
    kbps = q.get("approx_video_kbps")
    return f"{kbps/1000:.1f} Mbps (≈)" if kbps else "-"

def _movie_logical_key(*, tmdb_id: str | None, imdb_id: str | None, name: str, year: int | None) -> str:
    if tmdb_id: return f"movie:tmdb:{tmdb_id}"
    if imdb_id: return f"movie:imdb:{imdb_id}"
    # фолбэк: name+year в нижнем регистре
    key_name = re.sub(r"\s+", " ", (name or "").strip().lower())
    return f"movie:nameyear:{key_name}:{year or ''}"

def store_quality_snapshot_movie(*, item_id: str, name: str, year: int | None,
                                 tmdb_id: str | None, imdb_id: str | None) -> dict:
    """
    1) Тянем качество из Jellyfin
    2) Upsert в media_quality (по ItemId)
    3) Сравниваем и upsert в content_quality (по логическому ключу)
    Возвращаем флаги: logical_inserted, logical_changed, old_quality, new_quality
    """
    q = _get_item_media_info_movie(item_id)
    sig = _quality_signature(q)
    now = datetime.now().isoformat(timespec='seconds')

    profiles_str = (q.get("image_profile_str") or
                    ",".join(_profiles_from_q(q)))  # "DV,HDR10" или "SDR"

    result = {
        "logical_inserted": False,
        "logical_changed": False,
        "old_quality": None,
        "new_quality": q,
        "old_signature": None,
        "new_signature": sig,
        "logical_key": None
    }

    logical_key = _movie_logical_key(tmdb_id=tmdb_id, imdb_id=imdb_id, name=name, year=year)
    result["logical_key"] = logical_key

    conn = sqlite3.connect(QUALITY_DB_FILE)
    try:
        cur = conn.cursor()
        # --- media_quality по ItemId
        cur.execute("SELECT signature FROM media_quality WHERE item_id=?", (item_id,))
        if cur.fetchone() is None:
            cur.execute("""INSERT INTO media_quality
                           (item_id, movie_name, year, video_codec, video_bitrate, width, height, fps, bit_depth,
                            dynamic_range,
                            audio_codec, audio_bitrate, audio_channels, container, size_bytes, duration_sec, signature,
                            date_seen, image_profiles)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (item_id, name, year, q.get("video_codec"), q.get("video_bitrate"),
                         q.get("width"), q.get("height"), q.get("fps"), q.get("bit_depth"), q.get("dynamic_range"),
                         q.get("audio_codec"), q.get("audio_bitrate"), q.get("audio_channels"),
                         q.get("container"), q.get("size_bytes"), q.get("duration_sec"), sig, now, profiles_str)
                        )
        else:
            cur.execute("""UPDATE media_quality
                           SET movie_name=?,
                               year=?,
                               video_codec=?,
                               video_bitrate=?,
                               width=?,
                               height=?,
                               fps=?,
                               bit_depth=?,
                               dynamic_range=?,
                               audio_codec=?,
                               audio_bitrate=?,
                               audio_channels=?,
                               container=?,
                               size_bytes=?,
                               duration_sec=?,
                               signature=?,
                               date_seen=?,
                               image_profiles=?
                           WHERE item_id = ?""",
                        (name, year, q.get("video_codec"), q.get("video_bitrate"),
                         q.get("width"), q.get("height"), q.get("fps"), q.get("bit_depth"), q.get("dynamic_range"),
                         q.get("audio_codec"), q.get("audio_bitrate"), q.get("audio_channels"),
                         q.get("container"), q.get("size_bytes"), q.get("duration_sec"), sig, now, profiles_str,
                         item_id)
                        )

        # --- content_quality по logical_key
        cur.execute("""SELECT signature,
                              last_item_id,
                              video_codec,
                              video_bitrate,
                              width,
                              height,
                              fps,
                              bit_depth,
                              dynamic_range,
                              image_profiles,
                              audio_codec,
                              audio_bitrate,
                              audio_channels,
                              container,
                              size_bytes,
                              duration_sec
                       FROM content_quality
                       WHERE logical_key = ?""", (logical_key,))
        row = cur.fetchone()
        if row is None:
            if _quality_is_substantial(q):
                cur.execute("""INSERT INTO content_quality
                               (logical_key, last_item_id, movie_name, year, video_codec, video_bitrate, width, height,
                                fps, bit_depth,
                                dynamic_range, image_profiles, audio_codec, audio_bitrate, audio_channels, container,
                                size_bytes, duration_sec, signature, date_seen)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (logical_key, item_id, name, year, q.get("video_codec"), q.get("video_bitrate"),
                             q.get("width"), q.get("height"), q.get("fps"), q.get("bit_depth"), q.get("dynamic_range"),
                             profiles_str, q.get("audio_codec"), q.get("audio_bitrate"), q.get("audio_channels"),
                             q.get("container"), q.get("size_bytes"), q.get("duration_sec"), sig, now)
                            )
                result["logical_inserted"] = True
        else:
            old_sig, old_item_id = row[0], row[1]
            old_q = {
                "video_codec": row[2], "video_bitrate": row[3], "width": row[4], "height": row[5],
                "fps": row[6], "bit_depth": row[7], "dynamic_range": row[8],
                "image_profiles": ([p.strip() for p in row[9].split(",")] if row[9] else None),
                "audio_codec": row[10], "audio_bitrate": row[11], "audio_channels": row[12],
                "container": row[13], "size_bytes": row[14], "duration_sec": row[15]
            }
            result["old_signature"] = old_sig
            result["old_quality"] = old_q

            if old_sig != sig and _quality_is_substantial(old_q) and _quality_is_substantial(q):
                result["logical_changed"] = True
                cur.execute("""UPDATE content_quality
                               SET last_item_id=?,
                                   movie_name=?,
                                   year=?,
                                   video_codec=?,
                                   video_bitrate=?,
                                   width=?,
                                   height=?,
                                   fps=?,
                                   bit_depth=?,
                                   dynamic_range=?,
                                   image_profiles=?,
                                   audio_codec=?,
                                   audio_bitrate=?,
                                   audio_channels=?,
                                   container=?,
                                   size_bytes=?,
                                   duration_sec=?,
                                   signature=?,
                                   date_seen=?
                               WHERE logical_key = ?""",
                            (item_id, name, year, q.get("video_codec"), q.get("video_bitrate"),
                             q.get("width"), q.get("height"), q.get("fps"), q.get("bit_depth"), q.get("dynamic_range"),
                             profiles_str, q.get("audio_codec"), q.get("audio_bitrate"), q.get("audio_channels"),
                             q.get("container"), q.get("size_bytes"), q.get("duration_sec"),
                             sig, now, logical_key)
                            )
        conn.commit()
    finally:
        conn.close()
    return result

def _labels():
    if LANG == "ru":
        return {
            "changes": "Изменения качества",
            "resolution": "Разрешение",
            "video_codec": "Видео-кодек",
            "bitrate": "Битрейт (видео)",
            "dynamic_range": "Динамический диапазон",
            "audio": "Аудио",
            "container": "Контейнер",
            "fps": "Кадровая частота",
            "bit_depth": "Битовая глубина",
        }
    return {
        "changes": "Quality changes",
        "resolution": "Resolution",
        "video_codec": "Video codec",
        "bitrate": "Bitrate (video)",
        "dynamic_range": "Dynamic range",
        "audio": "Audio",
        "container": "Container",
        "fps": "Frame rate",
        "bit_depth": "Bit depth",
    }

def build_quality_changes_block(old_q: dict, new_q: dict) -> str:
    L = _labels()
    lines = []

    def arrow(a, b):
        return f"{a} → {b}"

    # Разрешение
    # --- Resolution (с ярлыками) ---
    res_old = _res_display_from_q(old_q)
    res_new = _res_display_from_q(new_q)
    if res_old != res_new:
        lines.append(f"- {L['resolution']}: {arrow(res_old, res_new)}")

    # Видео-кодек
    vc_old = (old_q.get("video_codec") or "-").upper()
    vc_new = (new_q.get("video_codec") or "-").upper()
    if vc_old != vc_new:
        lines.append(f"- {L['video_codec']}: {arrow(vc_old, vc_new)}")

    # Динамический диапазон (SDR/HDR и т.п.)
    dr_old = old_q.get("dynamic_range") or "-"
    dr_new = new_q.get("dynamic_range") or "-"
    if dr_old != dr_new:
        lines.append(f"- {L['dynamic_range']}: {arrow(dr_old, dr_new)}")

    # Профили изображения (SDR/HDR/HDR10/HDR10+/DV/HLG)
    old_profiles = ", ".join(_profiles_from_q(old_q))
    new_profiles = ", ".join(_profiles_from_q(new_q))
    if old_profiles != new_profiles:
        lines.append(f"- {t('image_profiles')}: {old_profiles} → {new_profiles}")
    logging.debug(f"Quality delta profiles: old='{old_profiles}' new='{new_profiles}'")
    if not lines:
        return ""
    return f"\n\n*{L['changes']}*\n" + "\n".join(lines)

def build_initial_quality_changes_block(new_q: dict) -> str:
    """
    Блок качества для НОВОГО фильма без стрелок и без 'Dynamic range'.
    Показываем: Resolution, Video codec, Image profiles.
    """
    L = _labels()
    lines = []

    # Resolution
    # Resolution -> ярлык (или WxH при неизвестном)
    res_new = _res_display_from_q(new_q)
    if res_new != "-":
        lines.append(f"- {L['resolution']}: {res_new}")

    # Video codec
    vc_new = (new_q.get("video_codec") or "-").upper()
    if vc_new != "-":
        lines.append(f"- {L['video_codec']}: {vc_new}")

    # Image profiles (SDR/HDR/HDR10/HDR10+/DV/HLG)
    profiles = ", ".join(_profiles_from_q(new_q))
    lines.append(f"- {t('image_profiles')}: {profiles}")

    if not lines:
        return ""
    return f"\n\n*{L['changes']}*\n" + "\n".join(lines)



def maybe_notify_movie_quality_change(*, item_id: str, movie_name_cleaned: str, release_year: int | None,
                                      tmdb_id: str | None, imdb_id: str | None,
                                      overview: str | None, runtime: str | None) -> bool:
    """
    Если качество фильма изменилось (по логическому ключу) — отправляем уведомление
    тем же шаблоном, что и для нового фильма, + блок 'Изменения качества'.
    Возвращает True, если отправили (обычное сообщение 'New Movie' слать не надо).
    """
    res = store_quality_snapshot_movie(
        item_id=item_id, name=movie_name_cleaned, year=release_year,
        tmdb_id=tmdb_id, imdb_id=imdb_id
    )
    if not res.get("logical_changed"):
        return False

    old_q = res.get("old_quality")
    new_q = res.get("new_quality")
    # Если старый снимок «пустой» — это первый нормальный парс нового фильма; считаем, что это НЕ апгрейд.
    if not _quality_is_substantial(old_q):
        logging.info("(Movie guard) Old quality is empty -> treat as NEW content, not a quality update.")
        return False

    # Собираем «как при добавлении»
    notification_message = (
        f"*{t('quality_updated')}*\n\n*{movie_name_cleaned}* *({release_year})*\n\n{overview or ''}\n\n"
        f"*{t('new_runtime')}*\n{runtime or ''}"
    )

    # рейтинги (если есть tmdb_id)
    if tmdb_id:
        ratings_text = safe_fetch_mdblist_ratings("movie", tmdb_id)
        if ratings_text:
            notification_message += f"\n\n*{t('new_ratings_movie')}*\n{ratings_text}"

    # трейлер
    trailer_url = safe_get_trailer_prefer_tmdb(f"{movie_name_cleaned} Trailer {release_year}",
                                context="webhook", subkind="movie", tmdb_id=tmdb_id)
    if trailer_url:
        notification_message += f"\n\n[🎥]({trailer_url})[{t('new_trailer')}]({trailer_url})"

    # добавим блок «что изменилось»
    # добавим блок «что изменилось»
    delta = build_quality_changes_block(old_q, new_q)
    if delta:
        notification_message += delta

    # ДОБАВЬ УСЛОВИЕ:
    if INCLUDE_AUDIO_TRACKS:
        tracks_block = build_audio_tracks_block(new_q)
        if tracks_block:
            notification_message += tracks_block

    send_notification(item_id, notification_message)
    touch_quality_update_marker(res.get("logical_key") or _movie_logical_key(
        tmdb_id=tmdb_id, imdb_id=imdb_id, name=movie_name_cleaned, year=release_year
    ), item_id=item_id)
    logging.info(f"(Movie) Quality update sent for {movie_name_cleaned} ({release_year}); logical_key={res.get('logical_key')}")
    return True

def _format_runtime_from_ticks(runtime_ticks) -> str:
    if not runtime_ticks:
        return ""
    try:
        total_sec = int(runtime_ticks) // 10_000_000
        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return ""

def _format_title_with_year(title: str, year) -> str:
    """
    Возвращает 'Title (YYYY)' если год задан, иначе просто 'Title'.
    Год может прийти None/''/0/строкой.
    """
    try:
        y = ("" if year is None else str(year)).strip()
    except Exception:
        y = ""
    return f"{title} ({y})" if y else title


def poll_recent_movies_once():
    """
    Пагинированно тянем фильмы и проверяем апгрейды качества.
    Новые (очень свежие) пропускаем — их объявит вебхук.
    """
    page_size = MOVIE_POLL_PAGE_SIZE
    # совместимость: если задан старый MOVIE_POLL_LIMIT и MAX_TOTAL == 0, используем его как предел
    max_total = MOVIE_POLL_MAX_TOTAL  # 0 = без ограничения

    start = 0
    fetched = 0
    now_utc = datetime.now(timezone.utc)

    while True:
        # Ограничение на последнюю страницу, если нужно
        current_limit = page_size
        if max_total and (max_total - fetched) < page_size:
            current_limit = max_total - fetched
            if current_limit <= 0:
                break

        try:
            since_iso = _poll_since_get("movie_poll_since")  # NEW
            params = {
                "ApiKey": JELLYFIN_API_KEY,
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "SortBy": "DateModified,DateCreated",
                "SortOrder": "Descending",
                "Limit": str(current_limit),
                "StartIndex": str(start),
                # DateCreated нужен для грейс-фильтра (чтобы вебхук объявлял «новые»)
                "Fields": "RunTimeTicks,ProviderIds,ProductionYear,Overview,DateCreated",
                # NEW: инкрементальный фильтр
                "MinDateLastSaved": since_iso,
                "EnableTotalRecordCount": "false",
            }
            url = f"{JELLYFIN_BASE_URL}/Items"
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            payload = r.json() or {}
            items = payload.get("Items") or []
        except Exception as ex:
            logging.warning(f"Movie poll: failed page start={start}: {ex}")
            break

        if not items:
            break

        for it in items:
            try:
                # --- грейс: свежие новинки не трогаем (пусть вебхук пошлёт 'New Movie Added')

                # -------------------------------------------------------------

                item_id = it.get("Id")
                name = it.get("Name") or ""
                year = it.get("ProductionYear")
                prov = it.get("ProviderIds") or {}
                tmdb_id = prov.get("Tmdb") or prov.get("TmdbId")
                imdb_id = prov.get("Imdb") or prov.get("ImdbId")

                # Имя без года в скобках (как в вебхуке)
                name_clean = name.replace(f" ({year})", "").strip()

                # Overview/Runtime для текста
                overview = it.get("Overview") or ""
                runtime_str = _format_runtime_from_ticks(it.get("RunTimeTicks"))

                # Проверяем и отправляем ТОЛЬКО апдейты качества (не «новый фильм»)
                sent = maybe_notify_movie_quality_change(
                    item_id=item_id,
                    movie_name_cleaned=name_clean,
                    release_year=year,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    overview=overview,
                    runtime=runtime_str
                )
                if sent:
                    # запись в БД уже обновлена; повтора на следующем проходе не будет
                    continue

                # --- NEW: если это «новый фильм» и по нему ещё не было анонса — шлём «New Movie Added»
                if not item_already_notified("Movie", name, year):
                    logical_key = _movie_logical_key(
                        tmdb_id=tmdb_id,
                        imdb_id=imdb_id,
                        name=name_clean,
                        year=year
                    )
                    # Если только что был quality-update — не дублируем «новый фильм»
                    if was_quality_update_recent(logical_key):
                        logging.info(
                            f"(Movie poll) Suppressed 'new movie' due to recent quality update (logical_key={logical_key})")
                    else:
                        # --- Pre-DB cutoff: baseline записываем в БД (movie_announced)
                        try:
                            db_created_iso = _db_get_created_at_iso()
                            db_created_dt = _parse_iso_dt(db_created_iso)
                            created_iso = it.get("DateCreated")
                            created_dt = _parse_iso_dt(created_iso)

                            # Если уже ставили baseline в БД — молча пропускаем
                            if _movie_announced_get(logical_key):
                                continue

                            if db_created_dt and created_dt and (created_dt < db_created_dt):
                                _movie_announced_mark(
                                    logical_key,
                                    item_id=item_id,
                                    name=name_clean,
                                    year=year
                                )
                                logging.debug(f"(Movie poll) Pre-DB cutoff baseline set: {name_clean} ({year})")
                                continue
                        except Exception as ex:
                            logging.warning(f"Movie cutoff check failed for {item_id}: {ex}")

                        notification_message = (
                            f"*{t('new_movie_title')}*\n\n"
                            f"*{name_clean}* *({year})*\n\n"
                            f"{overview}\n\n"
                            f"*{t('new_runtime')}*\n{runtime_str}"
                        )

                        # Рейтинги (MDBList), если доступны
                        try:
                            ratings_text = safe_fetch_mdblist_ratings("movie", tmdb_id) if tmdb_id else ""
                            if ratings_text:
                                notification_message += f"\n\n*{t('new_ratings_movie')}*\n{ratings_text}"
                        except Exception as ex:
                            logging.warning(f"Movie poll: ratings fetch failed for {name_clean} ({year}): {ex}")

                        # Трейлер — предпочтительно по TMDb
                        try:
                            trailer_url = safe_get_trailer_prefer_tmdb(
                                f"{name_clean} Trailer {year}",
                                context="poll",
                                subkind="movie",
                                tmdb_id=tmdb_id
                            )
                            if trailer_url:
                                notification_message += f"\n\n[🎥]({trailer_url})[{t('new_trailer')}]({trailer_url})"
                        except Exception as ex:
                            logging.warning(f"Movie poll: trailer fetch failed for {name_clean} ({year}): {ex}")

                        # Первичный блок качества (baseline), плюс дорожки — как в вебхуке
                        # Качество: как в maybe_notify_movie_quality_change — через store_quality_snapshot_movie
                        try:
                            res_q = store_quality_snapshot_movie(
                                item_id=item_id,
                                name=name_clean,
                                year=year,
                                tmdb_id=tmdb_id,
                                imdb_id=imdb_id
                            )
                            new_q = (res_q.get("new_quality") or {})
                            old_q = res_q.get("old_quality")

                            if old_q:
                                # Если ранее в БД есть слепок — показать «Изменения качества»,
                                # а если изменений нет — показать первичный блок
                                delta = build_quality_changes_block(old_q, new_q)
                                if delta:
                                    notification_message += delta
                                else:
                                    init_block = build_initial_quality_changes_block(new_q)
                                    if init_block:
                                        notification_message += init_block
                            else:
                                # Иначе — «первичный» компактный блок качества
                                init_block = build_initial_quality_changes_block(new_q)
                                if init_block:
                                    notification_message += init_block

                            if INCLUDE_AUDIO_TRACKS:
                                tracks_block = build_audio_tracks_block(new_q)
                                if tracks_block:
                                    notification_message += tracks_block

                        except Exception as ex:
                            logging.warning(
                                f"Movie poll: failed to build quality block for {name_clean} ({year}): {ex}")

                        send_notification(item_id, notification_message)
                        _movie_announced_mark(logical_key, item_id=item_id, name=name_clean, year=year)
                        logging.info(f"(Movie poll) NEW movie announced: {name_clean} ({year})")
                        continue
                # --- /NEW

            except Exception as ex:
                logging.warning(f"Movie poll: item {it.get('Id')} failed: {ex}")

        n = len(items)
        fetched += n
        start += n
        logging.debug(f"Movie poll: page fetched {n} items (total {fetched})")

        # если страница неполная — дальше элементов нет
        if n < current_limit:
            break

        # мягкое дыхание между страницами (не обязательно)
        time.sleep(0.1)

    # ... в самом конце функции:
    _meta_set('touched_movies','1')
    _maybe_send_onboarding_congrats()

    _poll_since_bump("movie_poll_since", now_utc)

def _detect_image_profiles_from_fields(s: dict) -> list[str]:
    """
    Детект DV / HDR10+ / HDR10 / HLG / HDR / SDR по полям видео-потока.
    Если ничего не нашли — возвращаем ['SDR'].
    """
    txt_parts = []
    for k in ("ColorTransfer","VideoRange","VideoRangeType","ColorPrimaries","ColorSpace",
              "Profile","Hdr","Hdr10Plus","DolbyVision","DoVi","VideoDoViProfile"):
        v = s.get(k)
        if isinstance(v, bool):
            v = "1" if v else "0"
        if v is not None:
            txt_parts.append(str(v))
    txt = " ".join(txt_parts).upper()

    prof = []
    def add(tag):
        if tag not in prof:
            prof.append(tag)

    if "DOLBY VISION" in txt or "DOVI" in txt or "VIDEO DOVIPROFILE" in txt or re.search(r"\bDV\b", txt or ""):
        add("DV")
    if "HDR10+" in txt or "HDR10PLUS" in txt or "HDR10 PLUS" in txt:
        add("HDR10+")
    if "HDR10" in txt:
        add("HDR10")
    if "HLG" in txt:
        add("HLG")
    if ("HDR" in txt or "PQ" in txt or "BT2020" in txt) and not any(p in prof for p in ("DV","HDR10+","HDR10","HLG")):
        add("HDR")
    if not prof:
        add("SDR")

    order = {"DV":0,"HDR10+":1,"HDR10":2,"HLG":3,"HDR":4,"SDR":5}
    prof.sort(key=lambda x: order.get(x, 99))
    return prof

def _profiles_from_q(q: dict | None) -> list[str]:
    order = {"DV": 0, "HDR10+": 1, "HDR10": 2, "HLG": 3, "HDR": 4, "SDR": 5}
    if not q:
        return ["SDR"]

    profs = (q.get("image_profiles") or [])
    if not profs:
        dr = (q.get("dynamic_range") or "").upper()
        if "DV" in dr or "DOLBY" in dr:
            profs = ["DV"]
        elif "HDR10+" in dr:
            profs = ["HDR10+"]
        elif "HDR10" in dr:
            profs = ["HDR10"]
        elif "HLG" in dr:
            profs = ["HLG"]
        elif "HDR" in dr:
            profs = ["HDR"]
        else:
            profs = ["SDR"]

    # ← вот эти две строки ключевые:
    profs = [str(p).strip().upper() for p in profs if str(p).strip()]
    profs = list(dict.fromkeys(profs))

    profs.sort(key=lambda p: order.get(p, 99))
    return profs

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def _iso_to_dt(s: str | None) -> datetime | None:
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None

def touch_quality_update_marker(logical_key: str, item_id: str | None = None):
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("""INSERT INTO recent_quality_updates (logical_key, notified_at, item_id)
                       VALUES (?, ?, ?)
                       ON CONFLICT(logical_key) DO UPDATE SET
                         notified_at=excluded.notified_at,
                         item_id=excluded.item_id
                    """, (logical_key, _now_utc_iso(), item_id))
        conn.commit()
    except Exception as ex:
        logging.warning(f"touch_quality_update_marker failed: {ex}")
    finally:
        try: conn.close()
        except: pass

def was_quality_update_recent(logical_key: str) -> bool:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT notified_at FROM recent_quality_updates WHERE logical_key=?", (logical_key,))
        row = cur.fetchone()
    except Exception as ex:
        logging.warning(f"was_quality_update_recent check failed: {ex}")
        return False
    finally:
        try: conn.close()
        except: pass

    if not row:
        return False
    ts = _iso_to_dt(row[0])
    if not ts:
        return False
    return (datetime.now(timezone.utc) - ts) < timedelta(minutes=SUPPRESS_WEBHOOK_AFTER_QUALITY_UPDATE_MIN)

def _parse_iso_utc(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _movie_poll_loop():
    while True:
        try:
            poll_recent_movies_once()
        except Exception as ex:
            logging.warning(f"Movie poll loop error: {ex}")
        time.sleep(MOVIE_POLL_INTERVAL_SEC)

if MOVIE_POLL_ENABLED:
    threading.Thread(target=_movie_poll_loop, name="movie-poll", daemon=True).start()
    logging.info(f"Movie quality polling enabled every {MOVIE_POLL_INTERVAL_SEC}s (limit={MOVIE_POLL_MAX_TOTAL})")

def _resolution_label(width: int | None, height: int | None) -> str | None:
    """
    Возвращает текстовый ярлык разрешения (8K, 5K, 4K (UltraHD), 1440p, 1080p, 720p, 576p, 480p, 360p, 240p).
    Использует высоту кадра с допуском (~8%) на «неканонические» значения (например, 2026 ≈ 2160).
    Если не попадает ни в один диапазон — вернёт None.
    """
    if not height:
        return None

    # (target_height, label)
    targets = [
        (4320, "8K"),
        (2880, "5K"),
        (2160, "4K (UltraHD)"),
        (1440, "1440p"),
        (1080, "1080p"),
        (720,  "720p"),
        (576,  "576p"),
        (480,  "480p"),
        (360,  "360p"),
        (240,  "240p"),
    ]
    for h, label in targets:
        tol = max(int(h * 0.08), 12)  # ~8% или минимум 12px
        if abs(height - h) <= tol:
            return label
    return None

def _res_display_from_q(q: dict | None) -> str:
    """
    Человекочитаемое разрешение для отображения:
    - если удалось сопоставить ярлык 8K/4K/... -> вернуть его
    - иначе вернуть 'WxH'
    - если данных нет -> '-'
    """
    if not q:
        return "-"
    w, h = q.get("width"), q.get("height")
    if not (w and h):
        return "-"
    label = _resolution_label(w, h)
    return label or f"{w}x{h}"

#Разрешение для сериалов
def _get_item_resolution_label(item_id: str) -> str | None:
    """
    Возвращает человекочитаемое разрешение (например, '1080p'/'4K' или 'WxH') для одного элемента Jellyfin.
    Берём первый MediaSource -> первый Video stream.
    """
    try:
        params = {'ApiKey': JELLYFIN_API_KEY, 'Ids': item_id, 'Fields': 'MediaSources'}
        url = f"{JELLYFIN_BASE_URL}/Items"
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        item = (r.json().get("Items") or [{}])[0]
        sources = item.get("MediaSources") or []
        if not sources:
            return None
        streams = sources[0].get("MediaStreams") or []
        v = next((s for s in streams if (s.get("Type") == "Video")), None)
        if not v:
            return None
        w = v.get("Width") or v.get("PixelWidth")
        h = v.get("Height") or v.get("PixelHeight")
        q = {"width": w, "height": h}
        res = _res_display_from_q(q)
        return None if (not res or res == "-") else res
    except Exception as ex:
        logging.debug(f"_get_item_resolution_label failed for {item_id}: {ex}")
        return None


def _season_resolution_label(season_id: str, *, scan_limit: int | None = None) -> str | None:
    """
    Агрегированное разрешение сезона: берём присутствующие эпизоды, считаем (w,h),
    выбираем самое распространённое; при равенстве — с наибольшей высотой.
    """
    try:
        eps = _season_fetch_episodes(season_id, max_items=scan_limit)
        present = [ep for ep in eps if _episode_has_file(ep)]
        if not present:
            return None

        from collections import Counter
        dims = []

        for ep in present:
            sources = ep.get("MediaSources") or []
            if not sources:
                continue
            streams = sources[0].get("MediaStreams") or []
            v = next((s for s in streams if (s.get("Type") == "Video")), None)
            if not v:
                continue
            w = v.get("Width") or v.get("PixelWidth")
            h = v.get("Height") or v.get("PixelHeight")
            if w and h:
                try:
                    dims.append((int(w), int(h)))
                except Exception:
                    pass

        if not dims:
            return None

        cnt = Counter(dims)
        # самое частое; при равном счёте берём с max высотой
        best = max(cnt.items(), key=lambda kv: (kv[1], kv[0][1]))[0]  # -> (w,h)
        label = _resolution_label(best[0], best[1]) or f"{best[0]}x{best[1]}"
        return label
    except Exception as ex:
        logging.debug(f"_season_resolution_label failed for season {season_id}: {ex}")
        return None

#Очистка базы данных
def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace("+00:00", "Z")

def _iso_to_dt(s: str | None) -> datetime | None:
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception: return None

def _collect_current_movie_keys_and_ids() -> tuple[set[str], set[str]]:
    """
    Возвращает (set логических ключей, set ItemId) для ВСЕХ фильмов в Jellyfin.
    Ключ строим через _movie_logical_key(...) по ProviderIds/Tmdb/Imdb -> name+year.
    """
    current_keys: set[str] = set()
    current_ids: set[str] = set()

    start = 0
    page_size = QUALITY_GC_PAGE_SIZE

    while True:
        try:
            params = {
                "ApiKey": JELLYFIN_API_KEY,
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": str(page_size),
                "StartIndex": str(start),
                "Fields": "ProviderIds,ProductionYear"
            }
            url = f"{JELLYFIN_BASE_URL}/Items"
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            payload = r.json() or {}
            items = payload.get("Items") or []
        except Exception as ex:
            logging.warning(f"Quality GC: failed to list movies page start={start}: {ex}")
            break

        if not items:
            break

        for it in items:
            item_id = it.get("Id")
            name = it.get("Name") or ""
            year = it.get("ProductionYear")
            prov = it.get("ProviderIds") or {}
            tmdb_id = prov.get("Tmdb") or prov.get("TmdbId")
            imdb_id = prov.get("Imdb") or prov.get("ImdbId")
            # имя без суффикса "(year)"
            name_clean = name.replace(f" ({year})", "").strip()

            key = _movie_logical_key(
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                name=name_clean,
                year=year
            )
            current_keys.add(key)
            if item_id:
                current_ids.add(item_id)

        n = len(items)
        start += n
        if n < page_size:
            break

    return current_keys, current_ids

def _collect_current_movie_keys_and_ids() -> tuple[set[str], set[str]]:
    """
    Возвращает (set логических ключей, set ItemId) для ВСЕХ фильмов в Jellyfin.
    Ключ строим через _movie_logical_key(...) по ProviderIds/Tmdb/Imdb -> name+year.
    """
    current_keys: set[str] = set()
    current_ids: set[str] = set()

    start = 0
    page_size = QUALITY_GC_PAGE_SIZE

    while True:
        try:
            params = {
                "ApiKey": JELLYFIN_API_KEY,
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": str(page_size),
                "StartIndex": str(start),
                "Fields": "ProviderIds,ProductionYear"
            }
            url = f"{JELLYFIN_BASE_URL}/Items"
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            payload = r.json() or {}
            items = payload.get("Items") or []
        except Exception as ex:
            logging.warning(f"Quality GC: failed to list movies page start={start}: {ex}")
            break

        if not items:
            break

        for it in items:
            item_id = it.get("Id")
            name = it.get("Name") or ""
            year = it.get("ProductionYear")
            prov = it.get("ProviderIds") or {}
            tmdb_id = prov.get("Tmdb") or prov.get("TmdbId")
            imdb_id = prov.get("Imdb") or prov.get("ImdbId")
            # имя без суффикса "(year)"
            name_clean = name.replace(f" ({year})", "").strip()

            key = _movie_logical_key(
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                name=name_clean,
                year=year
            )
            current_keys.add(key)
            if item_id:
                current_ids.add(item_id)

        n = len(items)
        start += n
        if n < page_size:
            break

    return current_keys, current_ids

def gc_quality_db_once():
    """
    Удаляет устаревшие записи:
      - content_quality: логические ключи, которых нет в библиотеке и last seen старше GRACE
      - media_quality: item_id, которых нет в библиотеке и last seen старше GRACE
      - recent_quality_updates: маркеры по отсутствующим ключам
    """
    try:
        current_keys, current_ids = _collect_current_movie_keys_and_ids()
        cutoff = datetime.now(timezone.utc) - timedelta(days=QUALITY_GC_GRACE_DAYS)

        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()

        # --- content_quality
        cur.execute("SELECT logical_key, date_seen FROM content_quality")
        rows = cur.fetchall()
        to_del_keys = []
        for logical_key, date_seen in rows:
            if logical_key in current_keys:
                continue
            dt = _iso_to_dt(date_seen)
            if (dt is None) or (dt < cutoff):
                to_del_keys.append(logical_key)

        if to_del_keys:
            for key in to_del_keys:
                cur.execute("DELETE FROM content_quality WHERE logical_key=?", (key,))
            logging.info(f"Quality GC: removed {len(to_del_keys)} content_quality rows")

        # --- media_quality
        cur.execute("SELECT item_id, date_seen FROM media_quality")
        rows = cur.fetchall()
        to_del_ids = []
        for item_id, date_seen in rows:
            if item_id in current_ids:
                continue
            dt = _iso_to_dt(date_seen)
            if (dt is None) or (dt < cutoff):
                to_del_ids.append(item_id)

        if to_del_ids:
            for iid in to_del_ids:
                cur.execute("DELETE FROM media_quality WHERE item_id=?", (iid,))
            logging.info(f"Quality GC: removed {len(to_del_ids)} media_quality rows")

        # --- recent_quality_updates (маркеры подавления вебхука)
        if to_del_keys:
            for key in to_del_keys:
                cur.execute("DELETE FROM recent_quality_updates WHERE logical_key=?", (key,))

        conn.commit()

        # по желанию можно иногда делать VACUUM (редко)
        # cur.execute("VACUUM")  # если база компактность важна

    except Exception as ex:
        logging.warning(f"Quality GC error: {ex}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

_init_quality_db()

# --- Форсированная очистка при старте (одноразово) ---
if FORCE_QUALITY_GC_ON_START:
    old_grace = QUALITY_GC_GRACE_DAYS
    try:
        # Если не задано явно — чистим без «грейса» (сразу)
        QUALITY_GC_GRACE_DAYS = int(FORCE_QUALITY_GC_GRACE_DAYS) if FORCE_QUALITY_GC_GRACE_DAYS is not None else 0
    except Exception:
        logging.warning(f"FORCE_QUALITY_GC_GRACE_DAYS is not an int: {FORCE_QUALITY_GC_GRACE_DAYS}")
        QUALITY_GC_GRACE_DAYS = 0

    logging.info(f"Quality DB GC (startup forced): grace={QUALITY_GC_GRACE_DAYS}d, vacuum={FORCE_QUALITY_GC_VACUUM}")
    try:
        gc_quality_db_once()  # удалит записи по фильмам, которых уже нет в Jellyfin
        if FORCE_QUALITY_GC_VACUUM:
            try:
                conn = sqlite3.connect(QUALITY_DB_FILE)
                conn.execute("VACUUM")
                conn.close()
                logging.info("Quality DB GC (startup) VACUUM done.")
            except Exception as ex:
                logging.warning(f"Quality DB GC (startup) VACUUM failed: {ex}")
    finally:
        # вернём обычный grace для фоновой очистки
        QUALITY_GC_GRACE_DAYS = old_grace

#Работа я youtube и рейтингом

# --- SAFE trailer & ratings helpers ---
_youtube_forbid_until = 0.0
_trailer_cache = {}   # на процесс
_ratings_cache = {}   # на процесс

def safe_get_trailer(query: str, *, context: str = "", subkind: str | None = None, tmdb_id: str | None = None) -> str | None:
    """
    Ищем трейлер безопасно + кэшируем в БД.
    Идентификатор кэша берём по приоритету: tmdb_id → нормализованный query.
    subkind: 'movie' | 'show' (для удобства TTL/аналитики; к ключу не обязательно)
    """
    # правила отключения
    try:
        if os.getenv("TRAILER_FETCH_ENABLED", "1").lower() not in ("1","true","yes","on"):
            return None
        if os.getenv("DISABLE_TRAILER_IN_POLLS", "1").lower() in ("1","true","yes","on") and context == "series_poll":
            return None
    except Exception:
        pass

    # ключ кэша
    identity = tmdb_id or query.strip()
    # 1) читаем кэш
    cached_val, cached_at = _extcache_read("trailer", subkind, identity)
    if _is_fresh(cached_at, TRAILER_CACHE_TTL_DAYS) and cached_val:
        return cached_val

    # 2) не свежий — пробуем обновить из сети (с 403-предохранителем)
    try:
        # локальный «стоп» по 403
        import time as _t
        forbid_until = globals().get("_youtube_forbid_until", 0.0)
        if _t.time() < forbid_until:
            return cached_val or None

        url = get_youtube_trailer_url(query)  # твоя исходная функция
        if url:
            _extcache_write("trailer", subkind, identity, url)
            return url
        # если не нашли — вернём устаревшее, чтобы не ломать текст
        return cached_val or None

    except requests.HTTPError as ex:
        resp = getattr(ex, "response", None)
        if getattr(resp, "status_code", None) == 403:
            # при 403 — ставим «форбид» и используем кэш
            import time as _t
            suspend_min = int(os.getenv("TRAILER_FORBID_SUSPEND_MIN", "60"))
            globals()["_youtube_forbid_until"] = _t.time() + suspend_min * 60
            logging.warning(f"YouTube 403; suspend {suspend_min} min; use cache if any")
            return cached_val or None
        logging.warning(f"YouTube HTTP error: {ex}")
        return cached_val or None
    except Exception as ex:
        logging.warning(f"YouTube trailer fetch failed: {ex}")
        return cached_val or None

def safe_fetch_mdblist_ratings(kind: str, tmdb_id: str | None) -> str:
    """
    Возвращает текст рейтингов. Сначала читаем кэш из БД, если он свежий.
    Если кэш просрочен — пробуем обновить; при ошибке отдаём устаревшее.
    kind: 'movie' | 'show'
    """
    if not tmdb_id:
        return ""
    # 1) читаем кэш
    cached_val, cached_at = _extcache_read("ratings", kind, tmdb_id)
    if _is_fresh(cached_at, RATINGS_CACHE_TTL_DAYS) and cached_val:
        return cached_val

    # 2) пробуем обновить из сети
    fresh = ""
    try:
        fresh = fetch_mdblist_ratings(kind, tmdb_id) or ""
    except Exception as ex:
        logging.warning(f"MDblist ratings fetch failed: {ex}")

    if fresh:
        _extcache_write("ratings", kind, tmdb_id, fresh)
        return fresh

    # 3) если не удалось обновить — вернём устаревшее (не ломаем уведомления)
    return cached_val or ""

#Работа с сезонными уведомлениями
def jellyfin_count_present_episodes_in_season(season_id: str) -> int | None:
    try:
        params = {
            "ApiKey": JELLYFIN_API_KEY,
            "ParentId": season_id,
            "IncludeItemTypes": "Episode",
            "Recursive": "false",
            "LocationTypes": "FileSystem",
            "IsMissing": "false",
            "Limit": "1",
        }
        url = f"{JELLYFIN_BASE_URL}/Items"
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json() or {}
        cnt = data.get("TotalRecordCount")
        return int(cnt) if isinstance(cnt, int) else len(data.get("Items") or [])
    except requests.HTTPError as ex:
        status = getattr(getattr(ex, "response", None), "status_code", None)
        if status in (400, 404):
            # сигнал «сезон удалён»
            return -1
        logging.warning(f"Failed to count PRESENT episodes for season {season_id}: {ex}")
        return None
    except Exception as ex:
        logging.warning(f"Failed to count PRESENT episodes for season {season_id}: {ex}")
        return None

def jellyfin_count_missing_episodes_in_season(season_id: str) -> int | None:
    try:
        params = {
            "ApiKey": JELLYFIN_API_KEY,
            "ParentId": season_id,
            "IncludeItemTypes": "Episode",
            "Recursive": "false",
            "IsMissing": "true",
            "IsUnaired": "false",
            "IsVirtualUnaired": "false",
            "LocationTypes": "Virtual",
            "Limit": "1",
        }
        url = f"{JELLYFIN_BASE_URL}/Items"
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json() or {}
        cnt = data.get("TotalRecordCount")
        return int(cnt) if isinstance(cnt, int) else len(data.get("Items") or [])
    except requests.HTTPError as ex:
        status = getattr(getattr(ex, "response", None), "status_code", None)
        if status in (400, 404):
            # сигнал «сезон удалён»
            return -1
        logging.warning(f"Failed to count MISSING episodes for season {season_id}: {ex}")
        return None
    except Exception as ex:
        logging.warning(f"Failed to count MISSING episodes for season {season_id}: {ex}")
        return None

def jellyfin_get_season_counts_resilient(season_id: str) -> tuple[int, int] | tuple[int, int, bool]:
    attempts = max(int(os.getenv("SEASON_EP_COUNT_RETRY_ATTEMPTS", "5")), 1)
    delay = max(int(os.getenv("SEASON_EP_COUNT_RETRY_DELAY_SEC", "3")), 0)

    present, total = 0, 0
    for i in range(1, attempts + 1):
        p = jellyfin_count_present_episodes_in_season(season_id)
        if p == -1:
            # сезон удалён — чистим прогресс и прекращаем
            _sp_delete(season_id)
            logging.info(f"Season {season_id} removed from Jellyfin — purged from DB.")
            return (-1, -1)  # сигнал наверх

        m = jellyfin_count_missing_episodes_in_season(season_id)
        if m == -1:
            _sp_delete(season_id)
            logging.info(f"Season {season_id} removed from Jellyfin — purged from DB.")
            return (-1, -1)

        if isinstance(p, int):
            present = p
        if isinstance(m, int):
            total = present + m
        else:
            total = present

        if total > 0 and (present > 0 or i == attempts):
            if i > 1:
                logging.debug(f"Season counts after {i} attempts: present={present}, total={total}")
            break
        time.sleep(delay)

    return (present, total)

def _iter_changed_series_ids(since_iso: str | None, *, start: int, limit: int) -> list[str]:
    """
    Фаза 1: возвращает список Series-IDs, которые могли измениться после since_iso.
    Делает лёгкий запрос к /Items (Series) и фильтрует по DateLastMediaAdded/DateModified на клиенте,
    если сервер падает на minDateLastSaved.
    """
    base_params = {
        "ApiKey": JELLYFIN_API_KEY,
        "IncludeItemTypes": "Series",
        "Recursive": "true",
        "SortBy": "DateModified,DateCreated",
        "SortOrder": "Descending",
        "Limit": str(limit),
        "StartIndex": str(start),
        "Fields": "DateLastMediaAdded,DateLastSaved",
        "EnableTotalRecordCount": "false",
    }

    def _fetch_once(params: dict) -> list[dict]:
        url = f"{JELLYFIN_BASE_URL}/Items"
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        payload = r.json() or {}
        return payload.get("Items") or []

    # обходим несколько ParentId (если заданы), иначе один проход без ParentId
    parents = TV_PARENT_IDS if globals().get("TV_PARENT_IDS") else [None]

    out_ids: list[str] = []
    since_dt = _parse_iso_utc(since_iso) if since_iso else None

    for parent in parents:
        params = dict(base_params)
        if parent:
            params["ParentId"] = parent

        # пробуем с minDateLastSaved (нижний регистр!)
        if since_iso:
            params["minDateLastSaved"] = since_iso  # см. офиц. список параметров ItemsApiGetItemsRequest :contentReference[oaicite:4]{index=4}

        try:
            items = _fetch_once(params)
        except requests.HTTPError as ex:
            # Если 5xx — повторяем без minDateLastSaved
            if getattr(getattr(ex, "response", None), "status_code", 0) >= 500:
                params.pop("minDateLastSaved", None)
                items = _fetch_once(params)
            else:
                raise

        for it in items:
            sid = it.get("Id")
            if not sid:
                continue
            # Клиентский отсев по времени
            if since_dt:
                d = it.get("DateLastMediaAdded") or it.get("DateLastSaved") or it.get("DateModified") or it.get("DateCreated")
                dt = _parse_iso_utc(d)
                if dt and dt < since_dt:
                    continue
            out_ids.append(sid)

    # убираем дубли, сохраняем порядок
    seen = set()
    uniq = []
    for sid in out_ids:
        if sid not in seen:
            uniq.append(sid); seen.add(sid)
    return uniq

def _fetch_recent_episodes_for_series(series_id: str, *, limit: int = 60) -> list[dict]:
    """
    Возвращает свежие эпизоды одного сериала (последние N), отсортированные по дате.
    Используем /Items с ParentId=series_id (быстрее и стабильнее).
    """
    params = {
        "ApiKey": JELLYFIN_API_KEY,
        "ParentId": series_id,
        "IncludeItemTypes": "Episode",
        "Recursive": "true",
        "SortBy": "DateCreated,DateModified",
        "SortOrder": "Descending",
        "Limit": str(limit),
        "StartIndex": "0",
        "Fields": "ParentId,SeriesId,SeasonName,DateCreated,ProductionYear,Overview",
        "EnableTotalRecordCount": "false",
    }
    url = f"{JELLYFIN_BASE_URL}/Items"
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return (r.json() or {}).get("Items") or []


def poll_recent_episodes_once():
    """
    Фаза 1: ищем изменённые сериалы (быстро и дёшево для API).
    Фаза 2: по каждому сериалу вытягиваем последние эпизоды, группируем по сезону и шлём ОДНО уведомление
            «Новый сезон: добавлено N из M». Совсем свежие (грейс) пропускаем — их объявит вебхук.
    """
    page_size = SERIES_POLL_PAGE_SIZE
    max_total = SERIES_POLL_MAX_TOTAL or 0
    start = 0
    fetched = 0
    now_utc = datetime.now(timezone.utc)
    since_iso = _poll_since_get("series_poll_since")  # есть в файле :contentReference[oaicite:6]{index=6}

    processed_seasons: set[str] = set()
    since_dt = _parse_iso_utc(since_iso) if since_iso else None

    while True:
        current_limit = page_size if (not max_total or (max_total - fetched) >= page_size) else (max_total - fetched)
        if current_limit <= 0:
            break

        try:
            series_ids = _iter_changed_series_ids(since_iso, start=start, limit=current_limit)
        except Exception as ex:
            logging.warning(f"Series poll (phase-1 series) failed start={start}: {ex}")
            break

        if not series_ids:
            break

        for series_id in series_ids:
            try:
                eps = _fetch_recent_episodes_for_series(series_id, limit=max(int(os.getenv("SERIES_SERIES_EP_FETCH_LIMIT", "60")), 20))
            except Exception as ex:
                logging.warning(f"Series poll: episodes fetch failed for series {series_id}: {ex}")
                continue

            # фильтруем только те эпизоды, которые не моложе since (и не моложе грейса)
            for ep in eps:
                try:
                    created_dt = _parse_iso_utc(ep.get("DateCreated"))
                    if since_dt and created_dt and created_dt < since_dt:
                        continue
                    if created_dt and (now_utc - created_dt) < timedelta(minutes=SERIES_POLL_GRACE_MIN):
                        # пусть вебхук объявляет «совсем свежие»
                        continue

                    season_id = ep.get("ParentId") or ep.get("SeasonId")
                    if not season_id or season_id in processed_seasons:
                        continue

                    # детали сезона/сериала
                    season_details = get_item_details(season_id)
                    s_item = (season_details.get("Items") or [{}])[0]
                    series_id2 = s_item.get("SeriesId") or series_id
                    season_name = s_item.get("Name") or ep.get("SeasonName") or "Season"
                    release_year = s_item.get("ProductionYear") or ep.get("ProductionYear")

                    series_details = get_item_details(series_id2) if series_id2 else {"Items": [{}]}
                    series_item = (series_details.get("Items") or [{}])[0]
                    series_name = series_item.get("Name") or ""
                    overview_to_use = s_item.get("Overview") or series_item.get("Overview") or ""

                    # антиспам-ключ, как в вебхуке
                    series_name_cleaned = series_name.replace(f" ({release_year})", "").strip()
                    key_name = f"{series_name_cleaned} {season_name}".strip()

                    # Раньше тут был ранний continue, который блокировал прогресс-апдейты
                    # (если сезон уже объявлялся через webhook). Теперь НЕ прерываемся:
                    # просто пройдём дальше и решим через _sp_should_notify(), вырос ли present.
                    if item_already_notified("Season", key_name, release_year):
                        logging.debug(
                            f"Series poll: {key_name} was announced earlier; checking for progress bump via _sp_should_notify.")

                    # считаем «сколько есть / сколько всего»
                    wait_until_scan_idle("season counts build")
                    present, total = jellyfin_get_season_counts_resilient(season_id)

                    # сезон удалён — пропускаем
                    if isinstance(present, int) and isinstance(total, int) and present == -1 and total == -1:
                        processed_seasons.add(season_id)
                        continue

                    # baseline: если сезон старше даты создания БД — пометить и не слать
                    row_existing = _sp_get(season_id)
                    if row_existing is None:
                        try:
                            db_created_iso = _db_get_created_at_iso()
                            db_created_dt = _parse_iso_dt(db_created_iso)
                            season_created_iso = s_item.get("DateCreated") or ((get_item_details(season_id).get("Items") or [{}])[0]).get("DateCreated")
                            season_created_dt = _parse_iso_dt(season_created_iso)
                            if db_created_dt and season_created_dt and (season_created_dt < db_created_dt):
                                _sp_upsert(
                                    season_id,
                                    present=present, total=total,
                                    series_id=series_id2,
                                    season_number=int(s_item.get("IndexNumber")) if s_item.get("IndexNumber") is not None else None,
                                    series_name=series_name_cleaned,
                                    release_year=release_year,
                                    mark_notified=True
                                )
                                logging.info(f"(Series poll) Season pre-DB cutoff baseline: {series_name_cleaned} {season_name} — {present}/{total}")
                                processed_seasons.add(season_id)
                                continue
                        except Exception as ex:
                            logging.debug(f"Series poll baseline check failed for {season_id}: {ex}")

                    # текст и отправка
                    tmdb_id = jellyfin_get_tmdb_id(series_id2)
                    trailer_url = safe_get_trailer_prefer_tmdb(
                        f"{series_name_cleaned} Trailer {release_year}",
                        subkind="show",
                        tmdb_id=tmdb_id,
                        context=""
                    )
                    notification_message = build_season_announce_message(
                        series_name_cleaned=series_name_cleaned,
                        season_name=season_name,
                        release_year=release_year,
                        overview_to_use=overview_to_use,
                        present=present, total=total,
                        tmdb_id=tmdb_id, trailer_url=trailer_url,
                        season_id=season_id
                    )

                    if _fetch_jellyfin_image_with_retries(season_id, attempts=1, timeout=3):
                        send_notification(season_id, notification_message)
                    else:
                        send_notification(series_id2, notification_message)
                        logging.warning(f"(Series poll) {series_name_cleaned} {season_name} image missing; using series image")

                    _sp_upsert(
                        season_id,
                        present=present, total=total,
                        series_id=series_id2,
                        season_number=int(s_item.get("IndexNumber")) if s_item.get("IndexNumber") is not None else None,
                        series_name=series_name_cleaned,
                        release_year=release_year,
                        mark_notified=True
                    )
                    logging.info(f"(Series poll) Season announced: {series_name_cleaned} {season_name} — {present} / {total}")
                    processed_seasons.add(season_id)

                except Exception as ex:
                    logging.warning(f"Series poll: season from ep {ep.get('Id')} failed: {ex}")

        # пагинация по сериалам
        fetched += len(series_ids)
        start += len(series_ids)
        if len(series_ids) < current_limit:
            break

    _meta_set('touched_series', '1')
    _maybe_send_onboarding_congrats()
    _poll_since_bump("series_poll_since", now_utc)



def _series_poll_loop():
    while True:
        try:
            poll_recent_episodes_once()
        except Exception as ex:
            logging.warning(f"Series poll loop error: {ex}")
        time.sleep(SERIES_POLL_INTERVAL_SEC)

if SERIES_POLL_ENABLED:
    threading.Thread(target=_series_poll_loop, name="series-poll", daemon=True).start()
    logging.info(f"Series polling enabled every {SERIES_POLL_INTERVAL_SEC}s "
                 f"(page={SERIES_POLL_PAGE_SIZE}, max_total={SERIES_POLL_MAX_TOTAL}, grace={SERIES_POLL_GRACE_MIN}m)")


def _sq_get(season_id: str) -> dict | None:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT season_id, series_id, series_name, season_number, release_year, signature, updated_at, episode_count
            FROM season_quality WHERE season_id=?""", (season_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "season_id": row[0],
            "series_id": row[1],
            "series_name": row[2],
            "season_number": row[3],
            "release_year": row[4],
            "signature": row[5],
            "updated_at": row[6],
            "episode_count": row[7],
        }
    except Exception as ex:
        logging.warning(f"_sq_get failed: {ex}")
        return None
    finally:
        try: conn.close()
        except: pass


def _sq_upsert(season_id: str, *, signature: str,
               episode_count: int | None,
               series_id: str | None = None,
               series_name: str | None = None,
               season_number: int | None = None,
               release_year: int | None = None):
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        nowz = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        cur.execute("""
            INSERT INTO season_quality (season_id, series_id, series_name, season_number, release_year, signature, updated_at, episode_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id) DO UPDATE SET
              signature=excluded.signature,
              updated_at=excluded.updated_at,
              episode_count=excluded.episode_count,
              series_id=COALESCE(excluded.series_id, season_quality.series_id),
              series_name=COALESCE(excluded.series_name, season_quality.series_name),
              season_number=COALESCE(excluded.season_number, season_quality.season_number),
              release_year=COALESCE(excluded.release_year, season_quality.release_year)
        """, (season_id, series_id, series_name, season_number, release_year, signature, nowz, episode_count))
        conn.commit()
    except Exception as ex:
        logging.warning(f"_sq_upsert failed: {ex}")
    finally:
        try: conn.close()
        except: pass


def _sp_get(season_id: str) -> dict | None:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT season_id, series_id, series_name, season_number, release_year,
                   present, total, last_notified_present, updated_at
            FROM season_progress WHERE season_id=?""", (season_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "season_id": row[0], "series_id": row[1], "series_name": row[2],
            "season_number": row[3], "release_year": row[4],
            "present": row[5], "total": row[6],
            "last_notified_present": row[7], "updated_at": row[8],
        }
    except Exception as ex:
        logging.warning(f"_sp_get failed: {ex}")
        return None
    finally:
        try: conn.close()
        except: pass

def _sp_upsert(season_id: str, *, present: int, total: int,
               series_id: str | None = None, season_number: int | None = None,
               series_name: str | None = None, release_year: int | None = None,
               mark_notified: bool = False):
    """
    Записывает прогресс сезона ТОЛЬКО при фактических изменениях.
    - без mark_notified: обновляем, если изменились present/total
    - с mark_notified: дополнительно пишем last_notified_present=present (если отличается)
    Это предотвращает бессмысленные перезаписи и «мигание» mtime у файла БД.
    """
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        nowz = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

        # читаем текущее состояние
        cur.execute("""
            SELECT present, total, last_notified_present
            FROM season_progress WHERE season_id=?
        """, (season_id,))
        row = cur.fetchone()

        if row:
            old_present, old_total, old_last = (row[0] or 0), (row[1] or 0), (row[2] or 0)

            # решаем, нужно ли писать
            need_update = False
            set_last = None

            if present != old_present or total != old_total:
                need_update = True
            if mark_notified and old_last != present:
                need_update = True
                set_last = present

            if not need_update:
                # ничего не изменилось — выходим без записи
                return

            if mark_notified:
                cur.execute("""
                    UPDATE season_progress
                    SET present=?, total=?, last_notified_present=?, updated_at=?,
                        series_id=COALESCE(?, series_id),
                        series_name=COALESCE(?, series_name),
                        season_number=COALESCE(?, season_number),
                        release_year=COALESCE(?, release_year)
                    WHERE season_id=?
                """, (present, total, (set_last if set_last is not None else old_last), nowz,
                      series_id, series_name, season_number, release_year, season_id))
            else:
                cur.execute("""
                    UPDATE season_progress
                    SET present=?, total=?, updated_at=?,
                        series_id=COALESCE(?, series_id),
                        series_name=COALESCE(?, series_name),
                        season_number=COALESCE(?, season_number),
                        release_year=COALESCE(?, release_year)
                    WHERE season_id=?
                """, (present, total, nowz,
                      series_id, series_name, season_number, release_year, season_id))
        else:
            # первая запись по сезону
            cur.execute("""
                INSERT INTO season_progress (
                    season_id, series_id, series_name, season_number, release_year,
                    present, total, last_notified_present, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (season_id, series_id, series_name, season_number, release_year,
                  int(present), int(total),
                  int(present) if mark_notified else 0,
                  nowz))
        conn.commit()
    except Exception as ex:
        logging.warning(f"_sp_upsert failed: {ex}")
    finally:
        try: conn.close()
        except: pass

def _sp_should_notify(season_id: str, present_now: int) -> bool:
    row = _sp_get(season_id)
    if row is None:
        # впервые видим сезон: слать только если разрешено и есть хоть что-то
        return SERIES_POLL_INITIAL_ANNOUNCE and present_now > 0
    last = int(row.get("last_notified_present") or 0)
    return present_now > last

#Сохранение трейлеров и рейтингов в базу данных
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

def _movie_announced_get(logical_key: str) -> dict | None:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("""SELECT logical_key, announced_at, item_id, movie_name, year
                       FROM movie_announced WHERE logical_key=?""", (logical_key,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "logical_key": row[0],
            "announced_at": row[1],
            "item_id": row[2],
            "movie_name": row[3],
            "year": row[4],
        }
    except Exception as ex:
        logging.debug(f"_movie_announced_get failed: {ex}")
        return None
    finally:
        try: conn.close()
        except: pass


def _movie_announced_mark(logical_key: str, *, item_id: str | None, name: str | None, year: int | None):
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        nowz = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        cur.execute("""
            INSERT INTO movie_announced (logical_key, announced_at, item_id, movie_name, year)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(logical_key) DO UPDATE SET
              announced_at = excluded.announced_at,
              item_id      = COALESCE(excluded.item_id, movie_announced.item_id),
              movie_name   = COALESCE(excluded.movie_name, movie_announced.movie_name),
              year         = COALESCE(excluded.year, movie_announced.year)
        """, (logical_key, nowz, item_id, name, year))
        conn.commit()
    except Exception as ex:
        logging.debug(f"_movie_announced_mark failed: {ex}")
    finally:
        try: conn.close()
        except: pass

def _extcache_key(kind: str, subkind: str | None, identity: str) -> str:
    # Единый формат ключа
    s = subkind or "-"
    return f"{kind}:{s}:{identity}".strip()

def _extcache_read(kind: str, subkind: str | None, identity: str) -> tuple[str | None, str | None]:
    """
    Возвращает (value, updated_at_iso) из external_cache или (None, None).
    """
    if not EXTERNAL_CACHE_ENABLED:
        return (None, None)
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        ck = _extcache_key(kind, subkind, identity)
        cur.execute("SELECT value, updated_at FROM external_cache WHERE cache_key=?", (ck,))
        row = cur.fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception as ex:
        logging.warning(f"_extcache_read fail: {ex}")
        return (None, None)
    finally:
        try: conn.close()
        except: pass

def _extcache_write(kind: str, subkind: str | None, identity: str, value: str | None):
    if not EXTERNAL_CACHE_ENABLED:
        return
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        ck = _extcache_key(kind, subkind, identity)
        cur.execute("""
            INSERT INTO external_cache (cache_key, kind, subkind, value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (ck, kind, subkind or "", value or "", _utcnow_iso()))
        conn.commit()
    except Exception as ex:
        logging.warning(f"_extcache_write fail: {ex}")
    finally:
        try: conn.close()
        except: pass

def _is_fresh(updated_iso: str | None, ttl_days: int) -> bool:
    if not updated_iso:
        return False
    try:
        dt = datetime.fromisoformat(updated_iso.replace('Z', '+00:00'))
        return datetime.now(timezone.utc) - dt <= timedelta(days=max(ttl_days, 0))
    except Exception:
        return False

#Поиск трейлеров на tmdb
def _tmdb_pick_best_video(results: list[dict]) -> str | None:
    """
    Выбираем лучшую ссылку на трейлер из TMDB /videos.
    Приоритет: YouTube + type=Trailer + official=true → YouTube + Trailer → YouTube → Vimeo.
    """
    if not results:
        return None

    def to_url(site: str | None, key: str | None) -> str | None:
        if not site or not key:
            return None
        s = site.lower()
        if s == "youtube":
            return f"https://www.youtube.com/watch?v={key}"
        if s == "vimeo":
            return f"https://vimeo.com/{key}"
        return None

    # нормализуем
    vids = []
    for v in results:
        vids.append({
            "site": (v.get("site") or v.get("Site") or "").strip(),
            "type": (v.get("type") or v.get("Type") or "").strip(),
            "official": bool(v.get("official") if v.get("official") is not None else v.get("Official")),
            "key": v.get("key") or v.get("Key"),
            "size": v.get("size") or v.get("Size") or 0,
            "published_at": v.get("published_at") or v.get("PublishedAt") or "",
        })

    # 1) YouTube + Trailer + official
    for v in vids:
        if v["site"].lower() == "youtube" and v["type"].lower() == "trailer" and v["official"]:
            u = to_url(v["site"], v["key"])
            if u: return u
    # 2) YouTube + Trailer
    for v in vids:
        if v["site"].lower() == "youtube" and v["type"].lower() == "trailer":
            u = to_url(v["site"], v["key"])
            if u: return u
    # 3) любой YouTube
    for v in vids:
        if v["site"].lower() == "youtube":
            u = to_url(v["site"], v["key"])
            if u: return u
    # 4) Vimeo (на всякий случай)
    for v in vids:
        if v["site"].lower() == "vimeo":
            u = to_url(v["site"], v["key"])
            if u: return u
    return None


def _tmdb_fetch_trailer_url(subkind: str, tmdb_id: str, season_number: int | None = None) -> str | None:
    """
    subkind: 'movie' | 'show'
    Для фильмов: /movie/{id}/videos
    Для сериалов: /tv/{id}/videos, при необходимости пробуем /tv/{id}/season/{n}/videos
    """
    if not TMDB_API_KEY or not tmdb_id:
        return None
    try:
        params = {
            "api_key": TMDB_API_KEY,
            "language": TMDB_LANGUAGE,
            # включить ролики без языковой метки
            "include_video_language": f"{TMDB_LANGUAGE},null"
        }
        if subkind == "movie":
            url = f"{TMDB_BASE}/movie/{tmdb_id}/videos"
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json() or {}
            return _tmdb_pick_best_video(data.get("results") or [])
        else:
            # пробуем уровень сериала
            url = f"{TMDB_BASE}/tv/{tmdb_id}/videos"
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json() or {}
            url_pick = _tmdb_pick_best_video(data.get("results") or [])
            if url_pick:
                return url_pick
            # при необходимости — уровень сезона
            if season_number is not None:
                url = f"{TMDB_BASE}/tv/{tmdb_id}/season/{int(season_number)}/videos"
                r = requests.get(url, params=params, timeout=10)
                r.raise_for_status()
                data = r.json() or {}
                return _tmdb_pick_best_video(data.get("results") or [])
            return None
    except Exception as ex:
        logging.warning(f"TMDB trailer fetch failed ({subkind}:{tmdb_id} s{season_number}): {ex}")
        return None

def safe_get_trailer_prefer_tmdb(
    title: str,
    *,
    year: int | None = None,
    subkind: str,                 # 'movie' | 'show'
    tmdb_id: str | None = None,
    season_number: int | None = None,
    context: str = ""
) -> str | None:
    """
    1) Читаем кэш external_cache('trailer', subkind, identity) — identity=tmdb_id или title+year.
    2) Если кэш свежий — отдаём.
    3) Иначе пробуем TMDB → если нашли — пишем в кэш и отдаём.
    4) Иначе fallback: YouTube-поиск через safe_get_trailer(query, ...), тоже кладём в кэш.
    """
    # формируем identity и query для кэша/фоллбэка
    identity = (tmdb_id or "").strip() or f"{title.strip()} ({year})".strip()
    cached_val, cached_at = _extcache_read("trailer", subkind, identity)
    if _is_fresh(cached_at, TRAILER_CACHE_TTL_DAYS) and cached_val:
        return cached_val

    # 1) TMDB
    url_tmdb = None
    try:
        url_tmdb = _tmdb_fetch_trailer_url(subkind, tmdb_id, season_number) if tmdb_id else None
    except Exception as ex:
        logging.warning(f"safe_get_trailer_prefer_tmdb: TMDB branch failed: {ex}")

    if url_tmdb:
        _extcache_write("trailer", subkind, identity, url_tmdb)
        return url_tmdb

    # 2) Fallback: YouTube по названию + году
    q_year = f" {year}" if year else ""
    query = f"{title} Trailer{q_year}".strip()
    url_yt = safe_get_trailer(query, context=context, subkind=subkind, tmdb_id=tmdb_id)
    if url_yt:
        _extcache_write("trailer", subkind, identity, url_yt)
        return url_yt

    # 3) Ничего не нашли: вернём устаревшее, если было
    return cached_val or None

#Получение звуковых дорожек для сериалов
def _label_audio_stream(stream: dict) -> str:
    """
    Формирует «человеческую» подпись дорожки как в фильмах:
    DisplayTitle/Title -> иначе LANG CODEC Ch Layout (например: ENG AC3 6ch 5.1)
    """
    label = stream.get("DisplayTitle") or stream.get("Title")
    if label:
        return str(label)
    lang = stream.get("Language")
    codec = stream.get("Codec")
    ch = stream.get("Channels")
    layout = stream.get("ChannelLayout")
    parts = []
    if lang:   parts.append(str(lang).upper())
    if codec:  parts.append(str(codec).upper())
    if ch:     parts.append(f"{ch}ch")
    if layout: parts.append(str(layout))
    return " ".join(parts) or "Audio"

def _collect_season_audio_labels(season_id: str) -> list[str]:
    """
    Собирает уникальные названия аудио-дорожек из фактически присутствующих эпизодов сезона.
    Берём не более SEASON_AUDIO_SCAN_LIMIT эпизодов и не более SEASON_AUDIO_TRACKS_MAX уникальных дорожек.
    """
    labels_seen = []
    label_set = set()

    # запросим эпизоды сезона с MediaSources (как у тебя уже делается)
    eps = _season_fetch_episodes(season_id)  # должен возвращать Items с MediaSources/LocationType/Path
    # фильтруем только присутствующие (есть файл)
    present_eps = [ep for ep in eps if _episode_has_file(ep)]

    # ограничим количество эпизодов для разбора, чтобы не грузить лишнее
    for ep in present_eps[:max(SEASON_AUDIO_SCAN_LIMIT, 1)]:
        sources = ep.get("MediaSources") or []
        if not sources:
            continue
        # возьмём первую «основную» дорожку источника
        src = sources[0]
        for s in (src.get("MediaStreams") or []):
            if s.get("Type") != "Audio":
                continue
            lbl = _label_audio_stream(s)
            if lbl not in label_set:
                label_set.add(lbl)
                labels_seen.append(lbl)
                if len(labels_seen) >= SEASON_AUDIO_TRACKS_MAX:
                    return labels_seen
    return labels_seen

def _season_fetch_episodes(season_id: str, *, max_items: int | None = None) -> list[dict]:
    """
    Возвращает список эпизодов сезона с нужными полями для анализа аудио:
    - Берём ТОЛЬКО фактически присутствующие эпизоды (IsMissing=false, LocationTypes=FileSystem)
    - Тянем поля MediaSources/LocationType/Path/IndexNumber/Name
    - Пагинация до max_items (по умолчанию SEASON_AUDIO_SCAN_LIMIT или 50)
    """
    try:
        per_page = 200
        # ограничим объём: нам для списка дорожек достаточно просканировать часть сезона
        default_scan_limit = 50
        try:
            default_scan_limit = max(int(globals().get("SEASON_AUDIO_SCAN_LIMIT", 50)), 1)
        except Exception:
            pass
        cap = int(max_items) if isinstance(max_items, int) and max_items > 0 else default_scan_limit

        all_eps: list[dict] = []
        start = 0
        while True:
            # не запрашиваем больше, чем осталось до cap
            limit = per_page if (len(all_eps) + per_page) <= cap else (cap - len(all_eps))
            if limit <= 0:
                break

            params = {
                "ApiKey": JELLYFIN_API_KEY,
                "ParentId": season_id,
                "IncludeItemTypes": "Episode",
                "Recursive": "false",
                # ключевые фильтры: только реальные файлы
                "IsMissing": "false",
                "LocationTypes": "FileSystem",
                # сортируем по номеру эпизода
                "SortBy": "IndexNumber,DateCreated",
                "SortOrder": "Ascending",
                "StartIndex": str(start),
                "Limit": str(limit),
                # поля, нужные для аудио-аналитики
                "Fields": "MediaSources,LocationType,Path,IndexNumber,Name"
            }
            url = f"{JELLYFIN_BASE_URL}/Items"
            r = requests.get(url, params=params, timeout=12)
            r.raise_for_status()
            data = r.json() or {}
            items = data.get("Items") or []
            if not items:
                break

            all_eps.extend(items)
            start += len(items)
            if len(items) < limit:
                break  # последняя страница

        return all_eps
    except requests.HTTPError as ex:
        status = getattr(getattr(ex, "response", None), "status_code", None)
        if status in (400, 404):
            return []  # сезон удалён — молча возвращаем пусто
        logging.warning(f"_season_fetch_episodes failed (season {season_id}): {ex}")
        return []
    except Exception as ex:
        logging.warning(f"_season_fetch_episodes failed (season {season_id}): {ex}")
        return []


def _episode_has_file(ep: dict) -> bool:
    """
    Возвращает True, если у эпизода есть реальный файл.
    Проверяем:
      - LocationType == FileSystem/File (на всякий случай)
      - или задан Path
      - или есть MediaSources (не пусто) с признаками файла
    """
    try:
        lt = (ep.get("LocationType") or "").strip().lower()
        if lt in ("filesystem", "file"):
            return True

        if ep.get("Path"):
            return True

        ms = ep.get("MediaSources") or []
        if ms:
            for src in ms:
                # Признаки реального файла в источнике
                src_lt = (src.get("LocationType") or "").strip().lower()
                if src_lt in ("filesystem", "file"):
                    return True
                if src.get("Path"):
                    return True
                # Наличие контейнера/размера часто говорит о локальном файле
                if src.get("Container") or src.get("Size"):
                    return True
        return False
    except Exception:
        return False

def _plural_episodes(n: int, lang: str) -> str:
    lang = (lang or "").lower()
    if lang.startswith("ru"):
        n10, n100 = n % 10, n % 100
        if n10 == 1 and n100 != 11:
            return "эпизод"
        if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
            return "эпизода"
        return "эпизодов"
    return "episode" if n == 1 else "episodes"

def _collect_season_audio_label_counts(season_id: str) -> tuple[OrderedDict[str, int], int]:
    """
    Возвращает (OrderedDict[display_label -> count], present_episodes_count).
    Группирует метки дорожек с учётом нормализации (_normalize_audio_label),
    чтобы 'HDRezka' и 'HDrezka' считались одной дорожкой.
    """
    try:
        eps = _season_fetch_episodes(season_id)
        present_eps = [ep for ep in eps if _episode_has_file(ep)]
        scan_limit = max(int(globals().get("SEASON_AUDIO_SCAN_LIMIT", 50)), 1)

        # norm_label -> [display_label (первая встреченная), count]
        groups: dict[str, list] = {}

        for ep in present_eps[:scan_limit]:
            sources = ep.get("MediaSources") or []
            if not sources:
                continue
            src = sources[0]
            for s in (src.get("MediaStreams") or []):
                if s.get("Type") != "Audio":
                    continue
                raw_label = _label_audio_stream(s)
                norm = _normalize_audio_label(raw_label)
                if norm not in groups:
                    # сохраним человекочитаемую метку «как встретилась впервые»
                    groups[norm] = [raw_label.strip(), 1]
                else:
                    groups[norm][1] += 1

        # Сортировка: по убыванию счётчика, затем по метке (нормализованной)
        sorted_items = sorted(groups.items(), key=lambda kv: (-kv[1][1], kv[0]))
        ordered = OrderedDict((disp, cnt) for (_norm, (disp, cnt)) in sorted_items)
        return ordered, len(present_eps)
    except Exception as ex:
        logging.warning(f"_collect_season_audio_label_counts failed for {season_id}: {ex}")
        return OrderedDict(), 0

def build_audio_tracks_block_for_season(season_id: str) -> str:
    """
    Формирует текстовый блок аудио-дорожек для сезона в виде:
      *Audio tracks* (N)
      - RUS AC3 5.1 × 5 эпизодов
      - ENG EAC3 6ch × 3 эпизода
      ...
    """
    try:
        labels_counts, present_cnt = _collect_season_audio_label_counts(season_id)
        if not labels_counts:
            return ""

        lang = os.environ.get("LANGUAGE", "en")
        # заголовок (fallback, если нет ключа локализации)
        header = (MESSAGES.get(LANG, {}) or {}).get("audio_tracks_header") or \
                 ( "Аудио-дорожки" if lang.lower().startswith("ru") else "Audio tracks" )

        max_labels = max(int(globals().get("SEASON_AUDIO_TRACKS_MAX", 12)), 1)
        lines = [f"\n\n*{header}* ({min(len(labels_counts), max_labels)})"]

        i = 0
        for label, count in labels_counts.items():
            if i >= max_labels:
                break
            lines.append(f"- {label} × {count} {_plural_episodes(count, lang)}")
            i += 1

        return "\n".join(lines)
    except Exception as ex:
        logging.warning(f"Season audio block build failed for {season_id}: {ex}")
        return ""

def _normalize_audio_label(label: str) -> str:
    """
    Приводит метку к каноническому виду для сравнения:
    - casefold (регистронезависимо)
    - нормализация длинных тире к '-'
    - единые пробелы вокруг дефиса и внутри строки
    """
    s = (label or "").strip()
    s = re.sub(r"[–—−]", "-", s)              # все тире -> '-'
    s = re.sub(r"\s*-\s*", " - ", s)          # пробелы вокруг дефиса
    s = re.sub(r"\s+", " ", s)                # схлопнуть пробелы
    return s.casefold()                        # регистронезависимо

#Контроль базы данных (дата создания)
def _db_get_created_at_iso() -> str | None:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_meta WHERE key='db_created_at'")
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as ex:
        logging.warning(f"db_created_at read failed: {ex}")
        return None
    finally:
        try: conn.close()
        except: pass

def _parse_iso_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None

def _sp_delete(season_id: str):
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("DELETE FROM season_progress WHERE season_id=?", (season_id,))
        conn.commit()
    except Exception as ex:
        logging.warning(f"_sp_delete failed for {season_id}: {ex}")
    finally:
        try: conn.close()
        except: pass

#Уведомление об обновлении сезонов
def _episode_media_quality_signature_from_ep(ep: dict) -> str:
    """
    Строит сигнатуру качества эпизода по первому MediaSource (без сетевых запросов).
    """
    try:
        sources = ep.get("MediaSources") or []
        if not sources:
            return ""
        src = sources[0]
        streams = src.get("MediaStreams") or []

        v = next((s for s in streams if s.get("Type")=="Video"), None)
        a = next((s for s in streams if s.get("Type")=="Audio"), None)

        q = {}
        # контейнер/размер/битрейт
        q["container"] = (src.get("Container") or "").lower()
        try: q["size_bytes"] = int(src.get("Size") or 0)
        except Exception: q["size_bytes"] = 0
        try: q["video_bitrate_kbps"] = int((src.get("Bitrate") or 0)) // 1000
        except Exception: q["video_bitrate_kbps"] = None

        if v:
            q["video_codec"] = (v.get("Codec") or "").lower()
            q["width"]  = v.get("Width") or v.get("PixelWidth")
            q["height"] = v.get("Height") or v.get("PixelHeight")
            q["bit_depth"] = v.get("BitDepth")
            # fps
            fps = v.get("RealFrameRate") or v.get("AverageFrameRate") or v.get("FrameRate")
            try: q["fps"] = float(fps) if fps is not None else None
            except Exception: q["fps"] = None
            # профили HDR/DV
            q["image_profiles"] = _detect_image_profiles_from_fields(v)

        if a:
            q["audio_codec"] = (a.get("Codec") or "").lower()
            try: q["audio_channels"] = int(a.get("Channels") or 0)
            except Exception: q["audio_channels"] = None
            try: q["audio_bitrate_kbps"] = int((a.get("BitRate") or 0)) // 1000
            except Exception: q["audio_bitrate_kbps"] = None

        sig = _quality_signature(q)  # используем вашу нормализацию
        return sig or ""
    except Exception as ex:
        logging.debug(f"episode quality signature failed: {ex}")
        return ""

def _season_quality_signature(season_id: str, *, scan_limit: int | None = None) -> str:
    """
    Агрегированная сигнатура сезона = sha1 от отсортированного списка сигнатур эпизодов (с файлами).
    """
    eps = _season_fetch_episodes(season_id)
    present_eps = [ep for ep in eps if _episode_has_file(ep)]
    lim = max(int(globals().get("SEASON_QUALITY_SIG_LIMIT", 80)), 1)
    if scan_limit is not None:
        lim = max(int(scan_limit), 1)

    sigs = []
    for ep in present_eps[:lim]:
        s = _episode_media_quality_signature_from_ep(ep)
        if s:
            sigs.append(s)

    if not sigs:
        return ""

    sigs.sort()
    joined = "||".join(sigs).encode("utf-8", errors="ignore")
    return hashlib.sha1(joined).hexdigest()


def _season_quality_snapshot(season_id: str, *, scan_limit: int | None = None) -> tuple[str, int]:
    sig = _season_quality_signature(season_id, scan_limit=scan_limit)
    eps = _season_fetch_episodes(season_id)
    present = len([ep for ep in eps if _episode_has_file(ep)])
    return (sig, present)

def _notify_season_quality_updated(season_id: str):
    # детали сезона/сериала
    season_details = get_item_details(season_id)
    s_item = (season_details.get("Items") or [{}])[0]
    series_id = s_item.get("SeriesId")
    season_name = s_item.get("Name") or "Season"
    release_year = s_item.get("ProductionYear")

    series_details = get_item_details(series_id) if series_id else {"Items":[{}]}
    series_item = (series_details.get("Items") or [{}])[0]
    series_name = series_item.get("Name") or ""
    overview = s_item.get("Overview") or series_item.get("Overview") or ""

    series_name_cleaned = series_name.replace(f" ({release_year})","").strip()

    # рейтинги + трейлер
    tmdb_id = jellyfin_get_tmdb_id(series_id) if 'jellyfin_get_tmdb_id' in globals() else None
    trailer_url = safe_get_trailer_prefer_tmdb(f"{series_name_cleaned} Trailer {release_year}",
                                               subkind="show", tmdb_id=tmdb_id, context="")

    msg = f"*{t('quality_updated')}*\n\n*{series_name_cleaned}* *({release_year})*\n\n*{season_name}*"
    if overview:
        msg += f"\n\n{overview}"
    if tmdb_id:
        ratings_text = safe_fetch_mdblist_ratings("show", tmdb_id)
        if ratings_text:
            msg += f"\n\n*{t('new_ratings_show')}*\n{ratings_text}"
    if trailer_url:
        msg += f"\n\n[🎥]({trailer_url})[{t('new_trailer')}]({trailer_url})"

    try:
        res_label = _season_resolution_label(season_id)
        if res_label:
            L = _labels()
            msg += f"\n\n*{L['resolution']}*\n{res_label}"
    except Exception as ex:
        logging.debug(f"(Season) resolution block failed for {season_id}: {ex}")

    if INCLUDE_AUDIO_TRACKS:
        tracks_block = build_audio_tracks_block_for_season(season_id)
        if tracks_block:
            msg += tracks_block

    # постер сезона, если нет — постер сериала
    if _fetch_jellyfin_image_with_retries(season_id, attempts=1, timeout=3):
        send_notification(season_id, msg)
    else:
        send_notification(series_id, msg)
        logging.warning(f"(EpQuality poll) season image missing; used series image for {series_name_cleaned} {season_name}")

def _maybe_notify_season_quality_change(season_id: str) -> bool:
    # Текущий снимок
    new_sig, new_count = _season_quality_snapshot(season_id)
    if not new_sig:
        return False  # ждём, когда Jellyfin отдаст MediaSources/файлы

    row = _sq_get(season_id)
    if row is None:
        # Baseline: фиксируем сигнатуру и count, без уведомления
        try:
            sd = get_item_details(season_id)
            s_item = (sd.get("Items") or [{}])[0]
            _sq_upsert(
                season_id,
                signature=new_sig,
                episode_count=new_count,
                series_id=s_item.get("SeriesId"),
                series_name=None,
                season_number=int(s_item.get("IndexNumber")) if s_item.get("IndexNumber") is not None else None,
                release_year=s_item.get("ProductionYear"),
            )
        except Exception:
            _sq_upsert(season_id, signature=new_sig, episode_count=new_count)
        return False

    old_sig = row.get("signature") or ""
    old_count = row.get("episode_count")
    # 1) Если изменилось число эпизодов в сезоне — обновляем baseline и выходим БЕЗ уведомления
    if (old_count is None) or (old_count != new_count):
        _sq_upsert(season_id, signature=new_sig, episode_count=new_count)
        logging.info(f"(EpQuality) suppressed due to episode_count change: {old_count} -> {new_count} for season {season_id}")
        return False

    # 2) Если сигнатура не изменилась — выходим
    if old_sig == new_sig:
        return False

    # 3) Чистое изменение качества при стабильном числе эпизодов — отправляем уведомление
    _notify_season_quality_updated(season_id)
    _sq_upsert(season_id, signature=new_sig, episode_count=new_count)
    return True

_last_epq_since = datetime.now(timezone.utc)

def poll_episode_quality_once():
    """
    Ищем эпизоды по DateModified (самые свежие изменения), собираем уникальные сезоны,
    и для каждого сезона проверяем изменения агрегированного качества.
    Новые (очень свежие) эпизоды пропускаем — их анонсирует вебхук/серийный поллер.
    """
    page_size = EP_QUALITY_POLL_PAGE_SIZE
    max_total = EP_QUALITY_POLL_MAX_TOTAL or 0
    start = 0
    fetched = 0
    now_utc = datetime.now(timezone.utc)
    processed_seasons: set[str] = set()
    triggered = 0

    while True:
        current_limit = page_size if (not max_total or (max_total - fetched) >= page_size) else (max_total - fetched)
        if current_limit <= 0:
            break
        try:
            since_iso = _poll_since_get("epq_poll_since")  # NEW
            params = {
                "ApiKey": JELLYFIN_API_KEY,
                "IncludeItemTypes": "Episode",
                "Recursive": "true",
                "SortBy": "DateModified,DateCreated",
                "SortOrder": "Descending",
                "Limit": str(current_limit),
                "StartIndex": str(start),
                "Fields": "ParentId,DateCreated",
                "EnableTotalRecordCount": "false",
            }
            url = f"{JELLYFIN_BASE_URL}/Items"
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            payload = r.json() or {}
            items = payload.get("Items") or []
        except Exception as ex:
            logging.warning(f"EpQuality poll: failed page start={start}: {ex}")
            break

        if not items:
            break

        for it in items:
            season_id = it.get("ParentId") or it.get("SeasonId")
            if not season_id or season_id in processed_seasons:
                continue

            # грейс для «совсем новых» эпизодов
            created_iso = it.get("DateCreated")
            created_dt = _parse_iso_utc(created_iso)
            if created_dt and (now_utc - created_dt) < timedelta(minutes=SERIES_POLL_GRACE_MIN):
                continue

            try:
                if _maybe_notify_season_quality_change(season_id):
                    triggered += 1
                processed_seasons.add(season_id)
            except Exception as ex:
                logging.warning(f"EpQuality poll: season {season_id} failed: {ex}")

        n = len(items)
        fetched += n
        start += n
        if n < current_limit:
            break  # последняя страница

    global _last_epq_since
#    logging.info(f"(EpQuality poll) processed={len(processed_seasons)}, triggered={triggered}, since={_last_epq_since.isoformat()}")
    _last_epq_since = now_utc

    _poll_since_bump("epq_poll_since", now_utc)


def _ep_quality_poll_loop():
    while True:
        try:
            poll_episode_quality_once()
        except Exception as ex:
            logging.warning(f"EpQuality poll loop error: {ex}")
        time.sleep(EP_QUALITY_POLL_INTERVAL_SEC)

if EP_QUALITY_POLL_ENABLED:
    threading.Thread(target=_ep_quality_poll_loop, name="ep-quality-poll", daemon=True).start()
    logging.info(f"Episode/Season quality polling enabled every {EP_QUALITY_POLL_INTERVAL_SEC}s "
                 f"(page={EP_QUALITY_POLL_PAGE_SIZE}, max_total={EP_QUALITY_POLL_MAX_TOTAL}, grace={SERIES_POLL_GRACE_MIN}m)")

#Отправка информации о новых альбомах
def _album_announced_get(logical_key: str) -> dict | None:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("""SELECT logical_key, announced_at, item_id, album_name, artist_name, year
                       FROM album_announced WHERE logical_key=?""", (logical_key,))
        row = cur.fetchone()
        if not row:
            return None
        return {"logical_key": row[0], "announced_at": row[1], "item_id": row[2],
                "album_name": row[3], "artist_name": row[4], "year": row[5]}
    except Exception as ex:
        logging.debug(f"_album_announced_get failed: {ex}")
        return None
    finally:
        try: conn.close()
        except: pass

def _album_announced_mark(logical_key: str, *, item_id: str | None, album: str | None,
                          artist: str | None, year: int | None):
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        nowz = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        cur.execute("""
            INSERT INTO album_announced (logical_key, announced_at, item_id, album_name, artist_name, year)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(logical_key) DO UPDATE SET
              announced_at = excluded.announced_at,
              item_id      = COALESCE(excluded.item_id, album_announced.item_id),
              album_name   = COALESCE(excluded.album_name, album_announced.album_name),
              artist_name  = COALESCE(excluded.artist_name, album_announced.artist_name),
              year         = COALESCE(excluded.year, album_announced.year)
        """, (logical_key, nowz, item_id, album, artist, year))
        conn.commit()
    except Exception as ex:
        logging.debug(f"_album_announced_mark failed: {ex}")
    finally:
        try: conn.close()
        except: pass

def _album_logical_key(*, musicbrainz_id: str | None, artist: str, album: str, year: int | None) -> str:
    if musicbrainz_id:
        return f"album:mb:{musicbrainz_id}"
    a = re.sub(r"\s+", " ", (artist or "").strip().lower())
    n = re.sub(r"\s+", " ", (album  or "").strip().lower())
    return f"album:nameyear:{a}–{n}:{year or ''}"

def poll_recent_albums_once():
    """
    Пагинированно тянем MusicAlbum и отправляем уведомления о НОВЫХ альбомах.
    Свежие (очень недавно созданные) можно пропускать через GRACE (у нас по-умолчанию 0).
    """
    page_size = ALBUM_POLL_PAGE_SIZE
    max_total = ALBUM_POLL_MAX_TOTAL  # 0 = без ограничения

    start = 0
    fetched = 0
    now_utc = datetime.now(timezone.utc)

    while True:
        current_limit = page_size if not max_total else max(0, max_total - fetched)
        if current_limit == 0:
            break

        try:
            params = {
                'ApiKey': JELLYFIN_API_KEY,
                'IncludeItemTypes': 'MusicAlbum',
                'Recursive': 'true',
                'SortBy': 'DateModified,DateCreated',
                'SortOrder': 'Descending',
                'Limit': str(current_limit),
                'StartIndex': str(start),
                'Fields': 'ProviderIds,ProductionYear,Overview,DateCreated,RunTimeTicks,Artists,AlbumArtist',
            }
            url = f"{JELLYFIN_BASE_URL}/Items"
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            items = (r.json() or {}).get('Items') or []
        except Exception as ex:
            logging.warning(f"Album poll: failed page start={start}: {ex}")
            break

        if not items:
            break

        for it in items:
            try:
                item_id = it.get('Id')
                album_name = (it.get('Name') or '').strip()
                year = it.get('ProductionYear')
                # artist: пробуем AlbumArtist, затем первый из Artists
                artist = (it.get('AlbumArtist') or '').strip()
                if not artist:
                    artists = it.get('Artists') or []
                    artist = (artists[0] if artists else '') or ''

                name_clean = re.sub(r"\s+", " ", album_name).strip()
                artist_clean = re.sub(r"\s+", " ", artist).strip()
                key_name = f"{artist_clean} – {name_clean}".strip(" –")

                prov = it.get('ProviderIds') or {}
                mb_id = prov.get('MusicBrainzAlbum')
                logical_key = _album_logical_key(musicbrainz_id=mb_id, artist=artist_clean, album=name_clean, year=year)

                # 1) Уже объявлен? — выходим молча
                if _album_announced_get(logical_key):
                    continue

                # GRACE: очень свежие пусть пропускаем, если включили
                created_iso = it.get('DateCreated')
                created_dt = _parse_iso_dt(created_iso)
                if ALBUM_POLL_GRACE_MIN and created_dt:
                    if (now_utc - created_dt).total_seconds() < ALBUM_POLL_GRACE_MIN * 60:
                        continue

                # --- Срез по дате создания БД (без UnboundLocalError) ---
                db_created_iso = None
                db_created_dt = None

                try:
                    db_created_iso = _db_get_created_at_iso()
                    db_created_dt = _parse_iso_dt(db_created_iso)
                except Exception as ex:
                    logging.warning(f"Album cutoff: DB date parse failed for {item_id}: {ex}")

                try:
                    created_iso = it.get('DateCreated')  # может быть None/пусто
                    created_dt = _parse_iso_dt(created_iso) if created_iso else None
                except Exception as ex:
                    logging.warning(f"Album cutoff: item date parse failed for {item_id}: {ex}")

                # ВАЖНО: проверяем И ТОЛЬКО ЗДЕСЬ, уже вне try/except
                if db_created_dt and created_dt and (created_dt < db_created_dt):
                    _album_announced_mark(
                        logical_key,
                        item_id=item_id,
                        album=name_clean,
                        artist=artist_clean,
                        year=year
                    )
                    logging.debug(f"(Album poll) Pre-DB cutoff baseline set: {artist_clean} – {name_clean} ({year})")
                    continue

                # Сообщение
                overview = it.get('Overview') or ''
                runtime = _format_runtime_from_ticks(it.get('RunTimeTicks')) if 'RunTimeTicks' in it else None
                prov = it.get('ProviderIds') or {}
                mb_id = prov.get('MusicBrainzAlbum')
                mb_link = f"https://musicbrainz.org/release/{mb_id}" if mb_id else ''

                title_line = _format_title_with_year(name_clean, year)

                notification_message = (
                    f"*{t('new_album_title')}*\n\n"
                    f"*{artist_clean}*\n\n"
                    f"*{title_line}*\n\n"
                    f"{(overview + '\n\n') if overview else ''}"
                )
                if runtime:
                    notification_message += f"*{t('new_runtime')}*\n{runtime}\n\n"

                # Количество треков
                tracks = jellyfin_count_tracks_in_album(item_id)
                if tracks is not None:
                    notification_message += f"*{t('new_track_count')}*\n{tracks}\n\n"

                # Опционально: список треков (точный расчёт «сколько не показали»)
                if ALBUM_TRACKLIST_ENABLED:
                    try:
                        # ВАЖНО: берём ровно лимит — без +1
                        raw_tracks = jellyfin_list_tracks_in_album(item_id, limit=ALBUM_TRACKLIST_LIMIT)
                        if raw_tracks:
                            lines = []
                            for i, tr in enumerate(raw_tracks, 1):
                                idx = tr.get("IndexNumber") or i
                                title = tr.get("Name") or f"Track {i}"
                                if ALBUM_TRACKLIST_SHOW_DURATION:
                                    dur = _format_runtime_from_ticks(
                                        tr.get("RunTimeTicks")) if "RunTimeTicks" in tr else None
                                else:
                                    dur = None
                                line = f"{idx:02d}. {title}" + (f" — {dur}" if dur else "")
                                lines.append(line)

                            if lines:
                                notification_message += f"*{t('album_tracklist')}*\n\n" + "\n".join(lines) + "\n"

                            # tracks — это ОБЩЕЕ количество, уже получено выше через jellyfin_count_tracks_in_album(item_id)
                            displayed = len(lines)
                            if isinstance(tracks, int):
                                remaining = max(0, tracks - displayed)
                                if remaining > 0:
                                    more_tpl = t('album_tracklist_more')  # содержит {n}
                                    notification_message += more_tpl.replace("{n}", str(remaining)) + "\n"

                            notification_message += "\n"
                    except Exception as ex:
                        logging.warning(f"Album tracklist render failed for {item_id}: {ex}")

                if mb_link:
                    notification_message += f"[MusicBrainz]({mb_link})\n"

                send_notification(item_id, notification_message)
                _album_announced_mark(
                    logical_key,
                    item_id=item_id,
                    album=name_clean,
                    artist=artist_clean,
                    year=year
                )
                logging.info(f"(Album poll) NEW album: {artist_clean} – {name_clean} ({year})")
            except Exception as ex:
                logging.warning(f"Album poll: item {it.get('Id')} failed: {ex}")

        n = len(items)
        fetched += n
        start += n
        if max_total and fetched >= max_total:
            break
        if n < current_limit:
            break

    _meta_set('touched_albums', '1')
    _maybe_send_onboarding_congrats()

def _album_poll_loop():
    while True:
        try:
            wait_until_scan_idle("album poll")
            poll_recent_albums_once()
        except Exception as ex:
            logging.warning(f"Album poll loop error: {ex}")
        time.sleep(ALBUM_POLL_INTERVAL_SEC)

if ALBUM_POLL_ENABLED:
    threading.Thread(target=_album_poll_loop, name="album-poll", daemon=True).start()
    logging.info(f"Album polling enabled every {ALBUM_POLL_INTERVAL_SEC}s "
                 f"(page={ALBUM_POLL_PAGE_SIZE}, max_total={ALBUM_POLL_MAX_TOTAL}, grace={ALBUM_POLL_GRACE_MIN}m)")

#Отправка книг
def _book_announced_get(logical_key: str) -> dict | None:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("""SELECT logical_key, announced_at, item_id, title, authors, year
                       FROM book_announced WHERE logical_key=?""", (logical_key,))
        row = cur.fetchone()
        if not row:
            return None
        return {"logical_key": row[0], "announced_at": row[1], "item_id": row[2],
                "title": row[3], "authors": row[4], "year": row[5]}
    except Exception as ex:
        logging.debug(f"_book_announced_get failed: {ex}")
        return None
    finally:
        try: conn.close()
        except: pass

def _book_announced_mark(logical_key: str, *, item_id: str | None, title: str | None,
                         authors: str | None, year: int | None):
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        nowz = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        cur.execute("""
            INSERT INTO book_announced (logical_key, announced_at, item_id, title, authors, year)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(logical_key) DO UPDATE SET
              announced_at = excluded.announced_at,
              item_id      = COALESCE(excluded.item_id, book_announced.item_id),
              title        = COALESCE(excluded.title, book_announced.title),
              authors      = COALESCE(excluded.authors, book_announced.authors),
              year         = COALESCE(excluded.year, book_announced.year)
        """, (logical_key, nowz, item_id, title, authors, year))
        conn.commit()
    except Exception as ex:
        logging.debug(f"_book_announced_mark failed: {ex}")
    finally:
        try: conn.close()
        except: pass

def _book_logical_key(*, isbn: str | None, title: str, authors: str, year: int | None) -> str:
    if isbn:
        return f"book:isbn:{isbn.strip()}"
    a = re.sub(r"\s+", " ", (authors or "").strip().lower())
    t = re.sub(r"\s+", " ", (title   or "").strip().lower())
    return f"book:titleauthoryear:{t}–{a}:{year or ''}"

def _extract_book_authors(it: dict) -> list[str]:
    ppl = it.get("People") or []
    authors = [p.get("Name") for p in ppl if (p.get("Type") or "").lower() == "author" and p.get("Name")]
    if not authors:
        authors = [p.get("Name") for p in ppl if p.get("Name")]
    # если совсем пусто — вернём []
    return [a for a in authors if a]

def _extract_isbn(it: dict) -> str | None:
    prov = it.get("ProviderIds") or {}
    # встречаются разные ключи, попробуем несколько
    for k in ("Isbn", "ISBN", "Isbn13", "ISBN13"):
        if prov.get(k):
            return str(prov[k]).strip()
    return None

def poll_recent_books_once():
    """
    Ищем новые Book/AudioBook в Jellyfin и шлём ОДНО сообщение на книгу/аудиокнигу.
    Для аудиокниг части группируются: «… Часть 1–3». Дедуп — в таблице book_announced.
    Заголовок:
      - обычная книга:   t('new_book_header')      => «Новая книга добавлена»
      - аудиокнига:      t('new_audiobook_header') => «Новая аудиокнига добавлена»
    """
    page_size = BOOK_POLL_PAGE_SIZE
    max_total = BOOK_POLL_MAX_TOTAL  # 0 = без ограничения

    start = 0
    fetched = 0
    now_utc = datetime.now(timezone.utc)

    # Копим группы на весь проход (объединим части, пришедшие на разных страницах)
    groups: dict[str, dict] = {}  # logical_key -> агрегат

    while True:
        current_limit = page_size if not max_total else max(0, max_total - fetched)
        if current_limit == 0:
            break

        try:
            params = {
                "ApiKey": JELLYFIN_API_KEY,
                "IncludeItemTypes": "Book,AudioBook",
                "Recursive": "true",
                "SortBy": "DateModified,DateCreated",
                "SortOrder": "Descending",
                "Limit": str(current_limit),
                "StartIndex": str(start),
                # важно: People/ProviderIds/DateCreated/Overview
                "Fields": "People,ProviderIds,ProductionYear,Overview,DateCreated",
            }
            url = f"{JELLYFIN_BASE_URL}/Items"
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            items = (r.json() or {}).get("Items") or []
        except Exception as ex:
            logging.warning(f"Book poll: failed page start={start}: {ex}")
            break

        if not items:
            break

        for it in items:
            try:
                item_id = it.get("Id")
                raw_title = (it.get("Name") or "").strip()
                year = it.get("ProductionYear")
                overview = (it.get("Overview") or "").strip()

                # Авторы / ISBN
                authors_list = _extract_book_authors(it)
                authors = ", ".join(a for a in authors_list if a) if authors_list else ""
                isbn = _extract_isbn(it)

                title_clean = re.sub(r"\s+", " ", raw_title).strip()
                authors_clean = re.sub(r"\s+", " ", authors).strip()

                media_type = (it.get("Type") or "").lower()
                if media_type == "audiobook":
                    base_title, part_num, part_label = _strip_book_part_suffix(title_clean)
                else:
                    base_title, part_num, part_label = title_clean, None, None

                # Логический ключ (по ISBN, иначе title+authors+year; для аудиокниг — БЕЗ номера части)
                logical_key = _book_logical_key(
                    isbn=isbn,
                    title=base_title,
                    authors=authors_clean,
                    year=year,
                )

                # Уже объявляли? — молча пропускаем
                if _book_announced_get(logical_key):
                    continue

                # Парсим даты безопасно
                created_iso = it.get("DateCreated")
                created_dt = None
                db_created_dt = None
                try:
                    created_dt = _parse_iso_dt(created_iso) if created_iso else None
                except Exception as ex:
                    logging.debug(f"Book cutoff: item date parse failed for {item_id}: {ex}")
                try:
                    db_created_iso = _db_get_created_at_iso()
                    db_created_dt = _parse_iso_dt(db_created_iso)
                except Exception as ex:
                    logging.debug(f"Book cutoff: DB date parse failed: {ex}")

                # Pre-DB cutoff → baseline в БД
                if db_created_dt and created_dt and (created_dt < db_created_dt):
                    _book_announced_mark(
                        logical_key,
                        item_id=item_id,
                        title=base_title,
                        authors=authors_clean,
                        year=year,
                    )
                    logging.debug(f"(Book poll) Pre-DB baseline set: {authors_clean} – {base_title} ({year})")
                    continue

                # GRACE (если включён)
                if BOOK_POLL_GRACE_MIN and created_dt:
                    if (now_utc - created_dt).total_seconds() < BOOK_POLL_GRACE_MIN * 60:
                        continue

                # Копим в группу (одно сообщение на книгу/аудиокнигу)
                g = groups.setdefault(
                    logical_key,
                    {
                        "item_ids": [],
                        "base_title": base_title,
                        "authors": authors_clean,
                        "year": year,
                        "parts": [],
                        "label": part_label,
                        "overview": "",
                        "isbn": isbn,
                        "is_audiobook": (media_type == "audiobook"),
                    },
                )
                g["item_ids"].append(item_id)
                if overview and not g["overview"]:
                    g["overview"] = overview
                if isinstance(part_num, int):
                    g["parts"].append(part_num)
                # если у какого-то экземпляра нет ISBN, а у другого есть — сохраним имеющийся
                if not g["isbn"] and isbn:
                    g["isbn"] = isbn
                # если в группе смешанные типы (не должно быть, но на всякий)
                g["is_audiobook"] = g.get("is_audiobook") or (media_type == "audiobook")

            except Exception as ex:
                logging.warning(f"Book poll: item {it.get('Id')} failed: {ex}")

        n = len(items)
        fetched += n
        start += n
        if max_total and fetched >= max_total:
            break
        if n < current_limit:
            break

    _meta_set('touched_books', '1')
    _maybe_send_onboarding_congrats()


    # ---- СБРОС ГРУПП: одно сообщение на книгу/аудиокнигу ----
    for lk, g in groups.items():
        title_for_msg = g["base_title"]
        if g["parts"]:
            rng = _format_number_ranges(g["parts"])
            if rng:
                label = g["label"] or "Часть"
                title_for_msg = f"{title_for_msg}. {label} {rng}"

        title_line = _format_title_with_year(title_for_msg, g["year"])
        header_key = "new_audiobook_header" if g.get("is_audiobook") else "new_book_header"

        msg = (
            f"*{t(header_key)}*\n\n"
            f"*{title_line}*\n"
        )
        if g["authors"]:
            msg += f"\n*{t('new_authors')}*\n{g['authors']}\n"
        if g["isbn"]:
            msg += f"\n*{t('new_isbn')}*\n{g['isbn']}\n"
        if g["overview"]:
            msg += f"\n{g['overview']}\n"

        first_id = (g["item_ids"][0] if g["item_ids"] else None) or "books"
        send_notification(first_id, msg)

        _book_announced_mark(
            lk,
            item_id=first_id,
            title=g["base_title"],
            authors=g["authors"],
            year=g["year"],
        )
        logging.info(f"(Book poll) NEW book group: {g['authors']} – {title_for_msg} ({g['year']})")



def _book_poll_loop():
    while True:
        try:
            wait_until_scan_idle("book poll")
            poll_recent_books_once()
        except Exception as ex:
            logging.warning(f"Book poll loop error: {ex}")
        time.sleep(BOOK_POLL_INTERVAL_SEC)

if BOOK_POLL_ENABLED:
    threading.Thread(target=_book_poll_loop, name="book-poll", daemon=True).start()
    logging.info(f"Book polling enabled every {BOOK_POLL_INTERVAL_SEC}s "
                 f"(page={BOOK_POLL_PAGE_SIZE}, max_total={BOOK_POLL_MAX_TOTAL}, grace={BOOK_POLL_GRACE_MIN}m)")

# --- ГРУППИРОВКА ЧАСТЕЙ АУДИОКНИГ ---
_ROMAN_MAP = {"I":1,"V":5,"X":10,"L":50,"C":100,"D":500,"M":1000}

def _roman_to_int(s: str) -> int:
    s = s.upper()
    total = 0
    prev = 0
    for ch in reversed(s):
        val = _ROMAN_MAP.get(ch, 0)
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total

# паттерн: ... "Часть 1", "Part II", "Том 3", "Книга 01", "Disc 2", "CD 3", "Серия 4" в конце строки
_PART_SUFFIX_RX = re.compile(r"""(?ix)
    ^\s*
    (?P<base>.*?)
    (?:[\s\.\-–—,:]*)?
    (?:
        (?P<label>част(?:ь|и)|том|книга|part|disc|cd|серия)
        \s*
        (?P<num>[IVXLCM]+|\d+)
    )
    \s*$
""")

def _strip_book_part_suffix(title: str) -> tuple[str, int|None, str|None]:
    m = _PART_SUFFIX_RX.match(title or "")
    if not m:
        return (title or "").strip(), None, None
    base = (m.group("base") or "").strip().rstrip(" .–—-,:")
    raw_label = (m.group("label") or "").lower()
    if "част" in raw_label: label = "Часть"
    elif "том" in raw_label: label = "Том"
    elif "книга" in raw_label: label = "Книга"
    elif "сер" in raw_label: label = "Серия"
    else: label = "Part"
    num_s = (m.group("num") or "").strip().upper()
    num = _roman_to_int(num_s) if re.fullmatch(r"[IVXLCM]+", num_s) else (int(num_s) if num_s.isdigit() else None)
    return base, num, label

def _format_number_ranges(nums: list[int]) -> str:
    if not nums: return ""
    xs = sorted(set(int(n) for n in nums if isinstance(n, int)))
    if not xs: return ""
    ranges = []
    a = b = xs[0]
    for n in xs[1:]:
        if n == b + 1:
            b = n
        else:
            ranges.append((a, b))
            a = b = n
    ranges.append((a, b))
    parts = [f"{i}" if i==j else f"{i}-{j}" for i, j in ranges]
    return ", ".join(parts)

#Работа с музыкальными видео
def _musicvideo_announced_get(logical_key: str) -> dict | None:
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        cur.execute("""SELECT logical_key, announced_at, item_id, title, artist, year
                       FROM musicvideo_announced WHERE logical_key=?""", (logical_key,))
        row = cur.fetchone()
        if not row:
            return None
        return {"logical_key": row[0], "announced_at": row[1], "item_id": row[2],
                "title": row[3], "artist": row[4], "year": row[5]}
    except Exception as ex:
        logging.debug(f"_musicvideo_announced_get failed: {ex}")
        return None
    finally:
        try: conn.close()
        except: pass

def _musicvideo_announced_mark(logical_key: str, *, item_id: str | None,
                               title: str | None, artist: str | None, year: int | None):
    try:
        conn = sqlite3.connect(QUALITY_DB_FILE)
        cur = conn.cursor()
        nowz = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        cur.execute("""
            INSERT INTO musicvideo_announced (logical_key, announced_at, item_id, title, artist, year)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(logical_key) DO UPDATE SET
              announced_at = excluded.announced_at,
              item_id      = COALESCE(excluded.item_id, musicvideo_announced.item_id),
              title        = COALESCE(excluded.title, musicvideo_announced.title),
              artist       = COALESCE(excluded.artist, musicvideo_announced.artist),
              year         = COALESCE(excluded.year, musicvideo_announced.year)
        """, (logical_key, nowz, item_id, title, artist, year))
        conn.commit()
    except Exception as ex:
        logging.debug(f"_musicvideo_announced_mark failed: {ex}")
    finally:
        try: conn.close()
        except: pass

def _musicvideo_logical_key(*, artist: str, title: str, year: int | None) -> str:
    a = re.sub(r"\s+", " ", (artist or "").strip().lower())
    t = re.sub(r"\s+", " ", (title  or "").strip().lower())
    return f"mvid:{a}–{t}:{year or ''}"

def poll_recent_musicvideos_once():
    """
    Ищем новые клипы (MusicVideo) в Jellyfin и шлём уведомления.
    Дедуп — в таблице musicvideo_announced. Pre-DB cutoff — baseline в БД.
    """
    page_size = MVID_POLL_PAGE_SIZE
    max_total = MVID_POLL_MAX_TOTAL  # 0 = без ограничения

    start = 0
    fetched = 0
    now_utc = datetime.now(timezone.utc)

    while True:
        current_limit = page_size if not max_total else max(0, max_total - fetched)
        if current_limit == 0:
            break

        try:
            params = {
                "ApiKey": JELLYFIN_API_KEY,
                "IncludeItemTypes": "MusicVideo",
                "Recursive": "true",
                "SortBy": "DateModified,DateCreated",
                "SortOrder": "Descending",
                "Limit": str(current_limit),
                "StartIndex": str(start),
                # Полезные поля для сообщения/логики:
                "Fields": "Artists,Album,ProviderIds,ProductionYear,Overview,DateCreated,RunTimeTicks"
            }
            url = f"{JELLYFIN_BASE_URL}/Items"
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            items = (r.json() or {}).get("Items") or []
        except Exception as ex:
            logging.warning(f"MusicVideo poll: failed page start={start}: {ex}")
            break

        if not items:
            break

        for it in items:
            try:
                item_id = it.get("Id")
                title = (it.get("Name") or "").strip()
                year = it.get("ProductionYear")
                overview = (it.get("Overview") or "").strip()

                # Исполнитель
                artists = it.get("Artists") or []
                artist = (artists[0] if artists else "") or ""
                artist_clean = re.sub(r"\s+", " ", artist).strip()

                title_clean = re.sub(r"\s+", " ", title).strip()

                # Логический ключ
                logical_key = _musicvideo_logical_key(
                    artist=artist_clean,
                    title=title_clean,
                    year=year
                )

                # Уже объявляли? — молча пропускаем
                if _musicvideo_announced_get(logical_key):
                    continue

                # Даты безопасно
                created_iso = it.get("DateCreated")
                created_dt = None
                db_created_dt = None
                try:
                    created_dt = _parse_iso_dt(created_iso) if created_iso else None
                except Exception as ex:
                    logging.debug(f"MVID cutoff: item date parse failed for {item_id}: {ex}")
                try:
                    db_created_iso = _db_get_created_at_iso()
                    db_created_dt = _parse_iso_dt(db_created_iso)
                except Exception as ex:
                    logging.debug(f"MVID cutoff: DB date parse failed: {ex}")

                # Pre-DB cutoff → baseline в БД (не спамим)
                if db_created_dt and created_dt and (created_dt < db_created_dt):
                    _musicvideo_announced_mark(
                        logical_key,
                        item_id=item_id,
                        title=title_clean,
                        artist=artist_clean,
                        year=year
                    )
                    logging.debug(f"(MusicVideo poll) Pre-DB baseline set: {artist_clean} – {title_clean} ({year})")
                    continue

                # GRACE (если включён)
                if MVID_POLL_GRACE_MIN and created_dt:
                    if (now_utc - created_dt).total_seconds() < MVID_POLL_GRACE_MIN * 60:
                        continue

                # Альбом клипа (если Jellyfin отдал)
                album = (it.get("Album") or "").strip()

                # Длительность
                runtime = _format_runtime_from_ticks(it.get("RunTimeTicks")) if "RunTimeTicks" in it else None

                # Сообщение
                title_line = _format_title_with_year(title_clean, year)
                msg = (
                    f"*{t('new_musicvideo_header')}*\n\n"
                )
                if artist_clean:
                    msg += f"*{t('new_musicvideo_artist')}*\n{artist_clean}\n\n"
                msg += f"*{title_line}*\n\n"
                if album:
                    msg += f"*{t('new_musicvideo_album')}*\n{album}\n\n"
                if runtime:
                    msg += f"*{t('new_runtime')}*\n{runtime}\n\n"
                if overview:
                    msg += f"{overview}\n"

                send_notification(item_id, msg)

                _musicvideo_announced_mark(
                    logical_key,
                    item_id=item_id,
                    title=title_clean,
                    artist=artist_clean,
                    year=year
                )
                logging.info(f"(MusicVideo poll) NEW clip: {artist_clean} – {title_clean} ({year})")
            except Exception as ex:
                logging.warning(f"MusicVideo poll: item {it.get('Id')} failed: {ex}")

        n = len(items)
        fetched += n
        start += n
        if max_total and fetched >= max_total:
            break
        if n < current_limit:
            break

    _meta_set('touched_mvids', '1')
    _maybe_send_onboarding_congrats()

def _musicvideo_poll_loop():
    while True:
        try:
            wait_until_scan_idle("musicvideo poll")
            poll_recent_musicvideos_once()
        except Exception as ex:
            logging.warning(f"MusicVideo poll loop error: {ex}")
        time.sleep(MVID_POLL_INTERVAL_SEC)

if MVID_POLL_ENABLED:
    threading.Thread(target=_musicvideo_poll_loop, name="mvid-poll", daemon=True).start()
    logging.info(f"MusicVideo polling enabled every {MVID_POLL_INTERVAL_SEC}s "
                 f"(page={MVID_POLL_PAGE_SIZE}, max_total={MVID_POLL_MAX_TOTAL}, grace={MVID_POLL_GRACE_MIN}m)")



def _maybe_send_onboarding_congrats():
    try:
        # уже слали?
        if _meta_get('congrats_sent') == '1':
            return

        # какие опросчики включены — таких и ждём
        needed = []
        if 'MOVIE_POLL_ENABLED' in globals() and MOVIE_POLL_ENABLED:
            needed.append('movies')
        if 'SERIES_POLL_ENABLED' in globals() and SERIES_POLL_ENABLED:
            needed.append('series')
        if 'ALBUM_POLL_ENABLED' in globals() and ALBUM_POLL_ENABLED:
            needed.append('albums')
        if 'BOOK_POLL_ENABLED' in globals() and BOOK_POLL_ENABLED:
            needed.append('books')
        if 'MVID_POLL_ENABLED' in globals() and MVID_POLL_ENABLED:
            needed.append('mvids')

        # если ничего не включено — не шлём
        if not needed:
            return

        # все ли «к себе сходили» хотя бы один раз?
        for k in needed:
            if _meta_get(f'touched_{k}') != '1':
                return

        # всё, готово — шлём и помечаем
        send_notification("system", t("onboarding_congrats"))
        _meta_set('congrats_sent', '1')
        logging.info("Onboarding: congrats notification sent.")
    except Exception as ex:
        logging.warning(f"Onboarding congrats check failed: {ex}")

#отправка сообщения в jellyfin
def _jf_list_active_sessions(active_within_sec: int) -> list:
    """Возвращает список активных сессий Jellyfin за N секунд."""
    try:
        params = {
            "ApiKey": JELLYFIN_API_KEY,
            "ActiveWithinSeconds": str(active_within_sec)
        }
        r = requests.get(f"{JELLYFIN_BASE_URL}/Sessions", params=params, timeout=10)
        r.raise_for_status()
        return r.json() or []
    except Exception as ex:
        logging.warning(f"JF sessions fetch failed: {ex}")
        return []

def _jf_send_session_message(session_id: str, header: str, text: str, timeout_ms: int) -> bool:
    try:
        url = f"{JELLYFIN_BASE_URL}/Sessions/{session_id}/Message"
        headers = {"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'}
        payload = {"Header": header or "", "Text": text or ""}

        # Добавляем TimeoutMs только если явно хотим «toast»
        # Если включён форс-модалки или timeout_ms <= 0 — НЕ добавляем поле вовсе
        if not JELLYFIN_INAPP_FORCE_MODAL and (timeout_ms is not None) and (int(timeout_ms) > 0):
            payload["TimeoutMs"] = int(timeout_ms)

        r = requests.post(url, headers=headers, json=payload, timeout=8)
        if r.status_code not in (200, 204):
            logging.warning(f"JF message {session_id} failed {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as ex:
        logging.warning(f"JF session message error {session_id}: {ex}")
        return False

def send_jellyfin_inapp_message(message: str, title: str | None = None) -> bool:
    """Отправляет сообщение во ВСЕ активные сессии (за заданный период)."""
    if not (JELLYFIN_INAPP_ENABLED and JELLYFIN_BASE_URL and JELLYFIN_API_KEY):
        return False
    header = (title or JELLYFIN_INAPP_TITLE or "Jellyfin")[:120]
    sessions = _jf_list_active_sessions(JELLYFIN_INAPP_ACTIVE_WITHIN_SEC)
    if not sessions:
        logging.info("Jellyfin in-app: нет активных сессий — сообщение пропущено")
        return False

    ok_any = False
    for s in sessions:
        sid = s.get("Id") or s.get("SessionId") or s.get("Id")
        if not sid:
            continue
        if _jf_send_session_message(sid, header, message, JELLYFIN_INAPP_TIMEOUT_MS):
            ok_any = True

    if ok_any:
        logging.info(f"Jellyfin in-app: отправлено в {len(sessions)} сесс.")
    else:
        logging.warning("Jellyfin in-app: все попытки доставки неуспешны")
    return ok_any

#Отправка в reddit
_reddit_oauth_cache = {"token": None, "exp": 0}

def _reddit_get_token() -> str | None:
    """
    Получить (и кэшировать) bearer-токен через password grant для script-app.
    Нужен скоуп 'submit'.
    """
    try:
        import time
        now = int(time.time())
        if _reddit_oauth_cache["token"] and now < _reddit_oauth_cache["exp"] - 20:
            return _reddit_oauth_cache["token"]

        if not all([REDDIT_APP_ID, REDDIT_APP_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD]):
            return None

        data = {
            "grant_type": "password",
            "username": REDDIT_USERNAME,
            "password": REDDIT_PASSWORD,
        }
        # Basic-авторизация client_id:client_secret + обязательный User-Agent
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            data=data,
            auth=(REDDIT_APP_ID, REDDIT_APP_SECRET),
            headers={"User-Agent": REDDIT_USER_AGENT},
            timeout=12
        )
        r.raise_for_status()
        j = r.json()
        tok = j.get("access_token")
        exp = now + int(j.get("expires_in", 3600))
        if tok:
            _reddit_oauth_cache.update({"token": tok, "exp": exp})
        return tok
    except Exception as ex:
        logging.warning(f"Reddit OAuth failed: {ex}")
        return None


def send_reddit_post(title: str, body_markdown: str, external_image_url: str | None = None) -> bool:
    """
    Публикует self-post в Reddit. Если передан external_image_url,
    ставим его первой строкой (без Markdown) — Reddit обычно покажет превью.
    """
    try:
        if not (REDDIT_ENABLED and REDDIT_SUBREDDIT):
            return False

        token = _reddit_get_token()
        if not token:
            return False

        headers = {"Authorization": f"bearer {token}", "User-Agent": REDDIT_USER_AGENT}

        text = body_markdown or ""
        if external_image_url:
            url = external_image_url.strip()
            link_line = f"[Poster]({url})"
            # чтобы не дублировать, если уже вставлено
            if not (text.startswith(link_line) or text.startswith(url)):
                text = link_line + ("\n\n" if text else "") + text

        data = {
            "sr": REDDIT_SUBREDDIT,
            "kind": "self",
            "title": (title or "")[:300],
            "text": text,
            "resubmit": "true",
            "sendreplies": "true" if REDDIT_SEND_REPLIES else "false",
            "spoiler": "true" if REDDIT_SPOILER else "false",
            "nsfw": "true" if REDDIT_NSFW else "false",
            "api_type": "json",
        }

        r = requests.post("https://oauth.reddit.com/api/submit", headers=headers, data=data, timeout=20)
        if r.status_code != 200:
            logging.warning(f"Reddit submit HTTP {r.status_code}: {r.text[:300]}")
            return False

        jr = r.json().get("json", {})
        errs = jr.get("errors") or []
        if errs:
            logging.warning(f"Reddit submit errors: {errs}")
            return False

        logging.info("Reddit post submitted successfully")
        return True

    except Exception as ex:
        logging.warning(f"Reddit submit failed: {ex}")
        return False

def send_reddit_link_post_with_comment(title: str, url: str, body_markdown: str | None = None) -> bool:
    """
    Делает ссылочный пост (kind=link) с изображением-URL.
    Reddit отрисует превью/картинку. Затем добавляем комментарий с текстом.
    """
    try:
        if not (REDDIT_ENABLED and REDDIT_SUBREDDIT and url):
            return False

        token = _reddit_get_token()
        if not token:
            return False

        headers = {"Authorization": f"bearer {token}", "User-Agent": REDDIT_USER_AGENT}

        submit_data = {
            "sr": REDDIT_SUBREDDIT,
            "kind": "link",
            "title": (title or "")[:300],
            "url": url.strip(),
            "resubmit": "true",
            "sendreplies": "true" if REDDIT_SEND_REPLIES else "false",
            "spoiler": "true" if REDDIT_SPOILER else "false",
            "nsfw": "true" if REDDIT_NSFW else "false",
            "api_type": "json",
        }
        r = requests.post("https://oauth.reddit.com/api/submit", headers=headers, data=submit_data, timeout=20)
        if r.status_code != 200:
            logging.warning(f"Reddit link submit HTTP {r.status_code}: {r.text[:300]}")
            return False

        jr = r.json().get("json", {})
        errs = jr.get("errors") or []
        if errs:
            logging.warning(f"Reddit link submit errors: {errs}")
            return False

        data = jr.get("data") or {}
        thing_id = data.get("name") or (f"t3_{data.get('id')}" if data.get('id') else None)

        if thing_id and body_markdown:
            cdata = {"thing_id": thing_id, "text": body_markdown, "api_type": "json"}
            cr = requests.post("https://oauth.reddit.com/api/comment", headers=headers, data=cdata, timeout=20)
            if cr.status_code != 200:
                logging.warning(f"Reddit comment HTTP {cr.status_code}: {cr.text[:300]}")
            else:
                ce = (cr.json().get("json") or {}).get("errors") or []
                if ce:
                    logging.warning(f"Reddit comment errors: {ce}")

        logging.info("Reddit link post submitted successfully")
        return True

    except Exception as ex:
        logging.warning(f"Reddit link submit failed: {ex}")
        return False

#Отправка в synology chat
def _synochat_resp_ok(resp) -> tuple[bool, str]:
    """Проверяем, что Synology Chat реально принял сообщение."""
    if resp is None:
        return False, "no response"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    # Попытка разобрать JSON
    try:
        j = resp.json()
        if isinstance(j, dict) and j.get("success") is True:
            return True, ""
        # Иногда возвращают {"success":false,"error":{...}}
        return False, f"API: {j}"
    except Exception:
        # Бывают «простые» ответы (редко)
        t = (resp.text or "").strip()
        if '"success":true' in t.lower() or t.upper() == "OK":
            return True, ""
        return False, f"Body: {t[:200]}"

def _synochat_resp_ok(resp):
    if resp is None:
        return False, "no response", None
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}", None
    try:
        j = resp.json()
        if isinstance(j, dict):
            if j.get("success") is True:
                return True, "", None
            # иногда: {"success":false,"error":{"code":...,"errors": "..."}}
            code = (j.get("error") or {}).get("code")
            return False, f"API: {j}", code
    except Exception:
        pass
    t = (resp.text or "").strip().lower()
    if '"success":true' in t or t == "ok":
        return True, "", None
    return False, f"Body: {resp.text[:200]}", None


def send_synology_chat_message(text: str, file_url: str | None = None) -> bool:
    """
    Synology Chat Incoming Webhook.
    1) Не отправляем пустой payload: если text пуст — достраиваем из caption.
    2) Попытка №1: form (payload=<json>), №2: JSON body.
    3) Ретраим 117/411/429/5xx.
    """
    try:
        if not (SYNOCHAT_ENABLED and SYNOCHAT_WEBHOOK_URL):
            return False

        # verify: True / False / CA bundle
        verify_param = True
        if not SYNOCHAT_VERIFY_SSL:
            try:
                import urllib3
                from urllib3.exceptions import InsecureRequestWarning
                urllib3.disable_warnings(InsecureRequestWarning)
            except Exception:
                pass
            verify_param = False
        elif SYNOCHAT_CA_BUNDLE:
            verify_param = SYNOCHAT_CA_BUNDLE

        proxies = _notify_proxies_for(SYNOCHAT_WEBHOOK_URL)

        # --- Страховка от пустого текста ---
        safe_text = (text or "").strip()
        if not safe_text:
            # Попробуем извлечь «заголовок + описание» из последнего caption-стиля
            # (первая жирная строка — header, вторая — title; дальше overview)
            try:
                hdr, body = make_jf_inapp_payload_from_caption(text or "")
                safe_text = (body or hdr or "Notification").strip()
            except Exception:
                safe_text = "Notification"

        # Если после этого и poster не включён — не шлём вовсе
        if not safe_text and not file_url:
            logging.debug("Synology Chat: empty payload suppressed")
            return False

        payload = {"text": safe_text}
        if file_url:
            payload["file_url"] = file_url

        import time
        attempts = max(1, SYNOCHAT_RETRIES)
        delay = max(0.0, SYNOCHAT_RETRY_BASE_DELAY)

        for attempt in range(1, attempts + 1):
            # --- Попытка №1: form ---
            r1 = requests.post(
                SYNOCHAT_WEBHOOK_URL,
                data={"payload": json.dumps(payload, ensure_ascii=False)},
                timeout=SYNOCHAT_TIMEOUT_SEC,
                verify=verify_param,
                proxies=proxies,
            )
            ok, detail, code = _synochat_resp_ok(r1)
            if ok:
                logging.info("Synology Chat notification sent")
                return True

            # --- Попытка №2: JSON body ---
            r2 = requests.post(
                SYNOCHAT_WEBHOOK_URL,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=SYNOCHAT_TIMEOUT_SEC,
                verify=verify_param,
                proxies=proxies,
            )
            ok2, detail2, code2 = _synochat_resp_ok(r2)
            if ok2:
                logging.info("Synology Chat notification sent (json)")
                return True

            # Решаем, ретраить ли
            retry_code = code2 if code2 is not None else code
            # 117 = busy/network; 411 = rate-limit "create post too fast"; 429/5xx уже будут как HTTP в detail
            should_retry = (retry_code in (117, 411)) or ("HTTP 5" in str(detail) or "HTTP 429" in str(detail2))

            if not should_retry or attempt == attempts:
                logging.warning(f"Synology Chat failed: {detail} | {detail2}")
                return False

            logging.warning(f"Synology Chat temporary error (code={retry_code}), retry {attempt}/{attempts}...")
            time.sleep(delay)
            delay *= max(1.0, SYNOCHAT_RETRY_BACKOFF)

        return False

    except Exception as ex:
        logging.warning(f"Synology Chat error: {ex}")
        return False

#Отправка через прокси
def _host_matches(pattern: str, host: str) -> bool:
    p = pattern.lower().strip()
    h = (host or "").lower().strip()
    if p == h:
        return True
    if p.startswith("*.") and h.endswith(p[1:]):  # *.example.com
        return True
    # CIDR: 192.168.1.0/24
    try:
        if "/" in p:
            import ipaddress
            net = ipaddress.ip_network(p, strict=False)
            ip = ipaddress.ip_address(h)
            return ip in net
    except Exception:
        pass
    # Простая маска: 192.168.1.*
    if p.endswith(".*") and h.startswith(p[:-1]):
        return True
    return False

def _is_private_host(host: str) -> bool:
    """True, если host — приватный IP/localhost/однословный локальный хост."""
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        # не IP — hostname
        if host in ("localhost",):
            return True
        # однословные имена типа 'nas' обычно локальные
        if "." not in host:
            return True
        # популярные локальные зоны
        if host.endswith((".local", ".home", ".lan")):
            return True
        return False

def _notify_proxies_for(url: str) -> dict | None:
    """
    Вернёт dict для requests.proxies или None, если прокси не нужен.
    """
    if not NOTIFY_PROXY_URL:
        return None
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""

    # Bypass: приватные/локальные — если не включили форс
    if not NOTIFY_PROXY_FOR_INTERNAL and _is_private_host(host):
        return None

    # Bypass: по списку исключений
    for pat in NOTIFY_PROXY_NO:
        if _host_matches(pat, host):
            return None

    return {"http": NOTIFY_PROXY_URL, "https": NOTIFY_PROXY_URL}






@app.route("/webhook", methods=["POST"])
def announce_new_releases_from_jellyfin():
    try:
        payload = json.loads(request.data)
        item_type = payload.get("ItemType")
        tmdb_id = payload.get("Provider_tmdb")
        item_name = payload.get("Name")
        release_year = payload.get("Year")
        series_name = payload.get("SeriesName")
        season_epi = payload.get("EpisodeNumber00")
        season_num = payload.get("SeasonNumber00")

        if item_type == "Movie":
            movie_id = payload.get("ItemId")
            overview = payload.get("Overview")
            runtime = payload.get("RunTime")

            movie_name = item_name
            movie_name_cleaned = movie_name.replace(f" ({release_year})", "").strip()

            tmdb_id_payload = payload.get("Provider_tmdb") or payload.get("TmdbId")
            imdb_id_payload = payload.get("Provider_imdb") or payload.get("ImdbId")

            # --- НОВОЕ: если недавно отправляли quality-update по сканеру/гварду — глушим вебхук
            logical_key = _movie_logical_key(tmdb_id=tmdb_id_payload, imdb_id=imdb_id_payload,
                                             name=movie_name_cleaned, year=release_year)
            if was_quality_update_recent(logical_key):
                logging.info(
                    f"(Webhook/Movie) Suppressed 'new movie' due to recent quality update (logical_key={logical_key})")
                return "Suppressed: recent quality update"

            # 1) Сначала проверим апгрейд качества — это должно срабатывать даже если фильм уже «был отправлен»
            if maybe_notify_movie_quality_change(
                    item_id=movie_id,
                    movie_name_cleaned=movie_name_cleaned,
                    release_year=release_year,
                    tmdb_id=tmdb_id_payload,
                    imdb_id=imdb_id_payload,
                    overview=overview,
                    runtime=runtime
            ):
                return "Movie quality update sent"

            # 2) Иначе — стандартное уведомление о новом фильме (как было)
            if not item_already_notified(item_type, item_name, release_year):
                trailer_url = safe_get_trailer_prefer_tmdb(f"{movie_name_cleaned} Trailer {release_year}",
                                context="webhook", subkind="movie", tmdb_id=tmdb_id)

                notification_message = (
                    f"*{t('new_movie_title')}*\n\n*{movie_name_cleaned}* *({release_year})*\n\n{overview}\n\n"
                    f"*{t('new_runtime')}*\n{runtime}"
                )

                if tmdb_id:
                    mdblist_type = item_type.lower()
                    ratings_text = safe_fetch_mdblist_ratings(mdblist_type, tmdb_id)
                    if ratings_text:
                        notification_message += f"\n\n*{t('new_ratings_movie')}*\n{ratings_text}"

                if trailer_url:
                    notification_message += f"\n\n[🎥]({trailer_url})[{t('new_trailer')}]({trailer_url})"

                # --- Quality changes в сообщении о НОВОМ фильме ---
                try:
                    # текущее качество
                    new_q = _get_item_media_info_movie(movie_id)

                    # попробуем найти старый слепок по логическому ключу (если фильм когда-то был)
                    logical_key = _movie_logical_key(
                        tmdb_id=tmdb_id_payload,
                        imdb_id=imdb_id_payload,
                        name=movie_name_cleaned,
                        year=release_year
                    )
                    old_q = None
                    try:
                        conn = sqlite3.connect(QUALITY_DB_FILE)
                        cur = conn.cursor()
                        cur.execute("""SELECT video_codec,
                                              video_bitrate,
                                              width,
                                              height,
                                              fps,
                                              bit_depth,
                                              dynamic_range,
                                              image_profiles,
                                              audio_codec,
                                              audio_bitrate,
                                              audio_channels,
                                              container,
                                              size_bytes,
                                              duration_sec
                                       FROM content_quality
                                       WHERE logical_key = ?""", (logical_key,))
                        row = cur.fetchone()
                        if row:
                            old_q = {
                                "video_codec": row[0], "video_bitrate": row[1], "width": row[2], "height": row[3],
                                "fps": row[4], "bit_depth": row[5], "dynamic_range": row[6],
                                "image_profiles": ([p.strip() for p in row[7].split(",")] if row[7] else None),
                                "audio_codec": row[8], "audio_bitrate": row[9], "audio_channels": row[10],
                                "container": row[11], "size_bytes": row[12], "duration_sec": row[13],
                            }
                    except Exception as ex:
                        logging.warning(f"Quality (new movie) old snapshot read failed: {ex}")
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass

                    # Сформируем блок:
                    delta_block = build_quality_changes_block(old_q,
                                                              new_q) if old_q else build_initial_quality_changes_block(
                        new_q)
                    if not delta_block:
                        # на всякий случай: если почему-то блок пуст, покажем инициализационный
                        delta_block = build_initial_quality_changes_block(new_q)
                    notification_message += delta_block

                    # (по желанию) список аудио-дорожек, если разрешён флагом
                    if INCLUDE_AUDIO_TRACKS:
                        tracks_block = build_audio_tracks_block(new_q)
                        if tracks_block:
                            notification_message += tracks_block

                except Exception as ex:
                    logging.warning(f"Quality (new movie) block build failed: {ex}")
                # --- /Quality changes ---

                send_notification(movie_id, notification_message)
                mark_item_as_notified(item_type, item_name, release_year)
                logging.info(f"(Movie) {movie_name} {release_year} notification was sent.")
                return "Movie notification was sent"

        if item_type == "Season":
            # формируем более информативный ключ для антиспама
            season = item_name  # например, "Сезон 1"
            series_title_for_key = (series_name or "").strip()
            key_name = f"{series_title_for_key} {season}".strip()

            if not item_already_notified(item_type, key_name, release_year):
                season_id = payload.get("ItemId")
                season = item_name
                season_details = get_item_details(season_id)
                series_id = season_details["Items"][0].get("SeriesId")
                series_details = get_item_details(series_id)
                # Remove release_year from series_name if present
                series_name_cleaned = series_name.replace(f" ({release_year})", "").strip()

                # Get TMDb ID via external API
                tmdb_id = jellyfin_get_tmdb_id(series_id)
                trailer_url = safe_get_trailer_prefer_tmdb(f"{series_name_cleaned} Trailer {release_year}",
                                subkind="show", tmdb_id=tmdb_id, context="")

                # **Новые строки**: получаем рейтинги для сериала
                ratings_text = safe_fetch_mdblist_ratings("show", tmdb_id)
                # Если есть рейтинги — добавляем пустую строку после них
                ratings_section = f"{ratings_text}\n\n" if ratings_text else ""

                # Get series overview if season overview is empty
                overview_to_use = payload.get("Overview") if payload.get("Overview") else series_details["Items"][0].get(
                    "Overview")

                # считаем «сколько есть / сколько всего» по сезону (используй твой resilient-хелпер)
                present, total = jellyfin_get_season_counts_resilient(season_id)

                if total >= present and total > 0:
                    episodes_segment = f"\n\n{t('season_added_progress').format(added=present, total=total)}"
                elif present > 0:
                    episodes_segment = f"\n\n{t('season_added_count_only').format(added=present)}"
                else:
                    episodes_segment = ""

                notification_message = (
                    f"*{t('new_season_title')}*\n\n*{series_name_cleaned}* *({release_year})*\n\n"
                    f"*{season}*{episodes_segment}\n\n{overview_to_use}")

                if ratings_text:
                    notification_message += f"\n\n*{t('new_ratings_show')}*\n{ratings_text}"

                if trailer_url:
                    notification_message += f"\n\n[🎥]({trailer_url})[{t('new_trailer')}]({trailer_url})"

                # Проверим, есть ли постер сезона — если нет, шлём с постером сериала
                if _fetch_jellyfin_image_with_retries(season_id, attempts=1, timeout=3):
                    send_notification(season_id, notification_message)
                else:
                    send_notification(series_id, notification_message)
                    logging.warning(
                        f"{series_name_cleaned} {season} image does not exist, falling back to series image")

                mark_item_as_notified(item_type, key_name, release_year)
                logging.info(f"(Season) {series_name_cleaned} {season} notification was sent")
                return "Season notification was sent"

        if item_type == "Episode":
            if not item_already_notified(item_type, item_name, release_year):
                item_id = payload.get("ItemId")
                file_details = get_item_details(item_id)
                season_id = file_details["Items"][0].get("SeasonId")
                episode_premiere_date = file_details["Items"][0].get("PremiereDate", "0000-00-00T").split("T")[0]
                season_details = get_item_details(season_id)
                series_id = season_details["Items"][0].get("SeriesId")
                season_date_created = season_details["Items"][0].get("DateCreated", "0000-00-00T").split("T")[0]
                epi_name = item_name
                overview = payload.get("Overview")

#                if not DEBUG_DISABLE_DATE_CHECKS:
                if not is_not_within_last_x_days(season_date_created, SEASON_ADDED_WITHIN_X_DAYS):
                    logging.info(f"(Episode) {series_name} Season {season_num} "
                                 f"was added within the last {SEASON_ADDED_WITHIN_X_DAYS} "
                                 f"days. Not sending notification.")
                    return (f"Season was added within the last {SEASON_ADDED_WITHIN_X_DAYS} "
                            f"days. Not sending notification.")

                if episode_premiere_date and is_within_last_x_days(episode_premiere_date,
                                                                   EPISODE_PREMIERED_WITHIN_X_DAYS):

                    notification_message = (
                        f"*{t('new_episode_title')}*\n\n*{t('new_release_date')}*: {episode_premiere_date}\n\n*{t('new_series')}*: {series_name} *S*"
                        f"{season_num}*E*{season_epi}\n*{t('new_episode_t')}*: {epi_name}\n\n{overview}\n\n"
                    )
                    # Постер сезона может отсутствовать — проверим заранее и при необходимости уйдём на постер сериала
                    if _fetch_jellyfin_image_with_retries(season_id, attempts=1, timeout=3):
                        send_notification(season_id, notification_message)
                    else:
                        send_notification(series_id, notification_message)
                        logging.warning(
                            f"(Episode) {series_name} season image does not exist, falling back to series image")

                    mark_item_as_notified(item_type, item_name, release_year)
                    logging.info(f"(Episode) {series_name} S{season_num}E{season_epi} notification sent!")
                    return "Notification sent!"

                else:
                    logging.info(f"(Episode) {series_name} S{season_num}E{season_epi} "
                                 f"was premiered more than {EPISODE_PREMIERED_WITHIN_X_DAYS} "
                                 f"days ago. Not sending notification.")
                    return (f"Episode was added more than {EPISODE_PREMIERED_WITHIN_X_DAYS} "
                            f"days ago. Not sending notification.")

        if item_type == "MusicAlbum":
            # читаем исполнителя/альбом заранее, чтобы сформировать ключ
            album_name = payload.get("Name")
            artist = payload.get("Artist")
            key_name = f"{artist} – {album_name}".strip()

            if not item_already_notified(item_type, key_name, release_year):
                album_id = payload.get("ItemId")
                album_name = payload.get("Name")
                artist = payload.get("Artist")
                year = payload.get("Year")
                overview = payload.get("Overview")
                runtime = payload.get("RunTime")
                musicbrainzalbum_id = payload.get("Provider_musicbrainzalbum")

                # Формируем ссылку на MusicBrainz, если есть ID
                mb_link = f"https://musicbrainz.org/release/{musicbrainzalbum_id}" if musicbrainzalbum_id else ""

                # Шаблон уведомления
                notification_message = (
                    f"*{t('new_album_title')}*\n\n"
                    f"*{artist}*\n\n"
                    f"*{album_name} ({year})*\n\n"
                    f"{overview and overview + '\n\n' or ''}"
                    f"*{t('new_runtime')}*\n{runtime}\n\n"
                    f"{f'[MusicBrainz]({mb_link})' if mb_link else ''}\n"
                )

                send_notification(album_id, notification_message)
                mark_item_as_notified(item_type, key_name, release_year)
                logging.info(f"(Album) {artist} – {album_name} ({year}) notification sent.")
                return "Album notification was sent to telegram"

        if item_type == "Movie":
            logging.info(f"(Movie) {item_name} Notification Was Already Sent")
        elif item_type == "Season":
            logging.info(f"(Season) {series_name} {item_name} Notification Was Already Sent")
        elif item_type == "Episode":
            logging.info(f"(Episode) {series_name} S{season_num}E{season_epi} Notification Was Already Sent")
        else:
            logging.error('Item type not supported')
        return "Item type not supported."

    # Handle specific HTTP errors
    except HTTPError as http_err:
        logging.error(f"HTTP error occurred: {http_err}")
        return str(http_err)

    # Handle generic exceptions
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        return f"Error: {str(e)}"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
