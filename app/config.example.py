"""Пример конфигурации. Установщик создаёт config.py автоматически."""
import aiohttp

SERVICE_NAME = "MyVPN"
BOT_TOKEN = ""
ADMIN_IDS = []
SUBSCRIPTION_DAYS = 30
BOT_WELCOME_TEXT = ""  # optional; {service} is replaced with SERVICE_NAME
BOT_SUPPORT_PROMPT = ""
FAQ_INCY_URL = "https://apps.apple.com/ru/app/incy/id6756943388"
BOT_IDENTITY_REFRESH_SECONDS = 600
BOT_SYNC_INTERVAL_SECONDS = 3600
TELEGRAM_UPDATE_LEASE_SECONDS = 600
TELEGRAM_UPDATE_DEDUP_KEEP_DAYS = 7
DB_PATH = "/root/vpn_bot/data/vpn_bot.db"  # legacy source path; runtime uses DATABASE_URL
DATABASE_URL = "postgresql+psycopg://fargovpn:CHANGE_ME@127.0.0.1:5432/fargovpn"
DATABASE_POOL_SIZE = 4
DATABASE_MAX_OVERFLOW = 4
DATABASE_POOL_TIMEOUT = 15
DB_SLOW_QUERY_SECONDS = 1.0
SLOW_OPERATION_SECONDS = 1.0
BACKUP_DATABASE_TIMEOUT_SECONDS = 300
BASE_URL = "https://panel.example.com/"
MASTER_API_URL = BASE_URL
MASTER_API_TOKEN = ""
SUB_BASE_URL = "https://panel.example.com/sub/"
PAYMENT_PRICE = 150
PAYMENT_PHONE = ""
PAYMENT_BANK = ""
PAYMENT_RECEIVER = ""

# Local receipt OCR. PAYMENT_PHONE and PAYMENT_RECEIVER are the expected
# recipient details. Automatic approval requires all checks to pass.
RECEIPT_OCR_ENABLED = True
RECEIPT_AUTO_APPROVE = True
RECEIPT_MIN_AMOUNT = 150.0  # accepted when recognized amount is >= this value
RECEIPT_MAX_AGE_HOURS = 24
RECEIPT_RECEIVER_ALIASES = ""  # e.g. "Иван И.; И. Иванов"
RECEIPT_ALLOW_MASKED_PHONE = True
RECEIPT_TIMEZONE = "Asia/Almaty"
RECEIPT_OCR_LANGUAGES = "rus+eng"
RECEIPT_OCR_TIMEOUT = 20
# Individual filters. Defaults are intentionally tolerant of mobile-bank
# screenshots: phone and date are optional because some banks do not show them.
RECEIPT_FILTER_NAME = True
RECEIPT_FILTER_PHONE = False
RECEIPT_FILTER_AMOUNT = True
RECEIPT_FILTER_DATE = False
RECEIPT_FILTER_STATUS = True
RECEIPT_FILTER_DUPLICATE = True
RECEIPT_SUBMIT_LIMIT = 5
RECEIPT_SUBMIT_WINDOW_SECONDS = 3600
SUPPORT_MESSAGE_LIMIT = 10
SUPPORT_MESSAGE_WINDOW_SECONDS = 600
REGISTRATION_MAX_ATTEMPTS = 5
REGISTRATION_ATTEMPT_WINDOW_SECONDS = 600
REGISTRATION_BLOCK_SECONDS = 900

API_TIMEOUT = aiohttp.ClientTimeout(total=12.0)

WEB_HOST = '127.0.0.1'
WEB_SOCKET_PATH = '/run/vpn-service/fargovpn.sock'
WEB_SOCKET_GROUP = 'www-data'
APP_LOG_PATH = "/var/log/vpn_bot.log"
WEB_REVERSE_PROXY = True
WEB_PUBLIC_PREFIX = "/fargovpn-admin-example"
WEB_DOMAIN = ""
WEB_TLS_SERVER_NAME = ""
WEB_USERNAME = "admin"
WEB_PASSWORD_HASH = ""
WEB_SECRET_KEY = ""
WEB_COOKIE_HTTPS_ONLY = True
WEB_SESSION_MAX_AGE_SECONDS = 28800
CABINET_LINK_TTL_SECONDS = 86400
# Включайте только на короткое время миграции: старые ссылки не имеют срока действия.
CABINET_ALLOW_LEGACY_TOKENS = False
WEB_TRUST_PROXY_HEADERS = True
NGINX_GUARD_INTERVAL_SECONDS = 300
# Timezone for human-facing timestamps in the administration panel.
# SQLite audit timestamps are stored in UTC and converted for display.
WEB_TIMEZONE = "Asia/Almaty"
WEB_LOGIN_MAX_ATTEMPTS = 5
WEB_LOGIN_WINDOW_SECONDS = 900
WEB_LOGIN_BLOCK_SECONDS = 900
WEB_LOGIN_MAX_BLOCK_SECONDS = 86400
WEB_LOGIN_SECURITY_RETENTION_DAYS = 30

XUI_DB_PATH = "/etc/x-ui/x-ui.db"  # legacy path only; 3x-ui runtime may use PostgreSQL
XUI_PANEL_URL = BASE_URL.rstrip("/")
BOT_PANEL_URL = ""
PUBLIC_PANEL_URL = ""
XUI_CACHE_SECONDS = 15
ONLINE_METRICS_CACHE_SECONDS = 20
XUI_CONTROL_CACHE_SECONDS = 45
XUI_VERIFY_TLS = True
XUI_REQUEST_TIMEOUT_SECONDS = 12
XUI_MIN_REQUEST_INTERVAL_MS = 100
# Новые клиенты добавляются во все включённые inbound.
# При необходимости можно задать явный список ID через XUI_MANAGED_INBOUND_IDS.
XUI_MANAGED_INBOUND_IDS = []
XUI_INBOUND_CACHE_SECONDS = 60
XUI_MANAGED_PROTOCOLS = []  # legacy compatibility setting; ignored for inbound selection
REMINDER_DAYS = [7, 3, 1, 0]
REMINDER_LOCK_PATH = "/run/vpn-service-reminders.lock"
METRICS_STORE_INTERVAL_SECONDS = 60
IDENTITY_IMPORT_MAX_MB = 512
USER_EVENT_KEEP_DAYS = 365
USER_EVENT_MAX_ROWS = 250000
USER_EVENT_JOURNAL_QUEUE_SIZE = 5000
CHAT_MEDIA_MAX_MB = 100
CHAT_MEDIA_CACHE_DIR = "/var/cache/vpn-service/chat-media"
CHAT_MEDIA_CACHE_DAYS = 30
CHAT_MEDIA_CACHE_MAX_MB = 512
BROADCAST_DIR = "/var/lib/vpn-service/broadcasts"
BROADCAST_MEDIA_MAX_MB = 45
BROADCAST_SEND_DELAY_SECONDS = 0.04
BROADCAST_STALE_SECONDS = 7200
SUBSCRIPTION_REFRESH_DIR = "/var/lib/vpn-service/subscription-refresh"
SUBSCRIPTION_REFRESH_SEND_DELAY_SECONDS = 0.04
SUBSCRIPTION_REFRESH_STALE_SECONDS = 7200
XUI_SUBSCRIPTION_SETTINGS_CACHE_SECONDS = 30

# Browser / PWA Web Push. Keys are generated on first install and kept outside source.
PUSH_ENABLED = True
PUSH_VAPID_SUBJECT = ""  # auto-derived from WEB_DOMAIN when empty
PUSH_VAPID_PRIVATE_KEY_PATH = "/var/lib/vpn-service/vapid_private.pem"
PUSH_VAPID_PUBLIC_KEY = ""
PUSH_TTL_SECONDS = 3600
PUSH_MAX_SUBSCRIPTIONS_PER_USER = 8
PUSH_TEST_ENABLED = True

BACKUP_DIR = "/var/backups/vpn-service"
BACKUP_KEEP_DAYS = 14
BACKUP_INTERVAL_DAYS = 3
BACKUP_TELEGRAM = True
BACKUP_TELEGRAM_PART_MB = 45
BACKUP_INCLUDE_VENV = False
BACKUP_LOCK_PATH = "/run/vpn-service-backup.lock"
BACKUP_STATE_PATH = "/var/lib/vpn-service/backup-state.json"
BACKUP_LIVE_STATE_PATH = "/var/lib/vpn-service/backup-live.json"
BACKUP_RETRY_INTERVAL_SECONDS = 900
BACKUP_PENDING_KEEP_DAYS = 30
# Восстановление выполняется общей логикой restore_manager.py из консоли и веб-панели.
BACKUP_ALLOW_INPLACE_RESTORE = False
BACKUP_FORCE_PATH = "/run/vpn-service-backup.force"
RESTORE_STATE_PATH = "/var/lib/vpn-service/restore-state.json"
RESTORE_LOCK_PATH = "/run/vpn-service-restore.lock"
RESTORE_LOG_PATH = "/var/log/vpn-service-restore.log"
RESTORE_ARCHIVE_MAX_MEMBERS = 50000
RESTORE_ARCHIVE_MAX_UNPACKED_MB = 4096
RESTORE_ARCHIVE_MAX_FILE_MB = 512

XUI_POSTGRES_DSN = ""  # optional override; normally read from /etc/default/x-ui
XUI_DB_ENV_FILE = "/etc/default/x-ui"


# GitHub Releases is the single source of platform updates.
# Legacy compatibility fields for deployments that used publisher settings.
# All panels read the latest published release from the configured repository.
UPDATE_DIR = "/var/lib/vpn-service/updates"
UPDATE_PUBLISHER_USERNAME = "configured-locally"
UPDATE_IS_PUBLISHER = False
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_TOKEN = ""
GITHUB_REPOSITORY_OWNER = ""
GITHUB_REPOSITORY_NAME = "FargoVPN"
GITHUB_TARGET_BRANCH = "main"
GITHUB_RELEASE_TAG_PREFIX = "v"
GITHUB_RELEASE_NAME_TEMPLATE = "FargoVPN {version}"
GITHUB_RELEASE_ASSET_NAME = "VPN_Service_Platform_{version}_FULL.tar.gz"
GITHUB_RELEASE_MAKE_LATEST = True
GITHUB_RELEASE_DRAFT = False
GITHUB_RELEASE_PRERELEASE = False
GITHUB_MAIN_SYNC_ENABLED = True
GITHUB_REPOSITORY_DESCRIPTION = "Telegram-бот и веб-панель для управления продажей VPN-подписок с интеграцией 3x-ui."
GITHUB_REPOSITORY_TOPICS = ["fargovpn", "telegram-bot", "vpn", "3x-ui", "xray", "python", "fastapi"]
UPDATE_CHECK_INTERVAL = 60
UPDATE_VERIFY_TLS = True
UPDATE_MAX_ARCHIVE_MB = 1024
UPDATE_STALE_JOB_SECONDS = 7200
PUBLISH_STALE_JOB_SECONDS = 1800

# Preferred deployment: loopback application + external HTTPS reverse proxy.

# Optional cookie authentication for older 3x-ui panels.
XUI_USERNAME = ""
XUI_PASSWORD = ""
