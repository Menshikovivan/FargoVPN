PANEL_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; form-action 'self'; "
    "img-src 'self' data: blob: https://t.me https://*.telegram.org; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline' https://telegram.org; "
    "connect-src 'self' https://api.telegram.org https://telegram.org https://fcm.googleapis.com wss://push.services.mozilla.com; frame-ancestors 'self'"
)
