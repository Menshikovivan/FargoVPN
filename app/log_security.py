"""Redact known credentials and Telegram bot URLs before logs reach handlers."""
import logging
import os
import re
import config

_SECRET_KEYS = ('BOT_TOKEN', 'MASTER_API_TOKEN', 'XUI_PASSWORD', 'GITHUB_API_TOKEN',
                'WEB_PASSWORD', 'WEB_SECRET_KEY', 'DATABASE_URL', 'XUI_POSTGRES_DSN')

def redact(text):
    result = re.sub(r'(https?://api\.telegram\.org/bot)[^/\s]+', r'\1[REDACTED]', str(text))
    for key in _SECRET_KEYS:
        value = str(getattr(config, key, '') or '')
        if len(value) >= 5:
            result = result.replace(value, '[REDACTED]')
    for key in ('DATABASE_URL', 'FARGOVPN_DATABASE_URL'):
        value = os.getenv(key, '')
        if value:
            result = result.replace(value, '[REDACTED]')
    return result

class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True

def install_log_redaction():
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, SecretFilter) for item in handler.filters):
            handler.addFilter(SecretFilter())
    for name in ('httpx', 'httpcore'):
        logger = logging.getLogger(name)
        if not any(isinstance(item, SecretFilter) for item in logger.filters):
            logger.addFilter(SecretFilter())
