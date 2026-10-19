"""Small WebAuthn/passkey adapter for the FargoVPN control panel.

Cryptographic verification is delegated to py_webauthn. Challenges and their
origin/RP binding are kept in the signed session and are one-time/short-lived.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

import config


def _b64u_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(value)).decode("ascii").rstrip("=")


def _b64u_decode(value: str) -> bytes:
    text = str(value or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def webauthn_context(request: Any) -> tuple[str, str]:
    """Return an HTTPS origin and RP ID consistent with the public panel domain."""
    configured = str(
        getattr(config, "WEB_DOMAIN", "") or getattr(config, "BOT_PANEL_URL", "") or ""
    ).strip()
    headers = getattr(request, "headers", {})
    request_host = str(headers.get("host", "") or "").strip()
    if configured:
        source = configured if "://" in configured else "https://" + configured
        parsed = urlsplit(source)
        if not parsed.hostname:
            raise ValueError("WEB_DOMAIN не содержит корректный домен")
        # Never bind credentials to a different Host header than the configured
        # public hostname. A reverse proxy may add a port; hostname is the RP ID.
        if request_host:
            incoming = urlsplit("//" + request_host).hostname
            if incoming and incoming.casefold() != parsed.hostname.casefold():
                raise ValueError("Домен запроса не совпадает с WEB_DOMAIN")
        if parsed.scheme.lower() != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Passkeys доступны только по HTTPS")
        origin_netloc = parsed.netloc
        origin = f"{parsed.scheme.lower()}://{origin_netloc}"
        rp_id = parsed.hostname.lower()
    else:
        # Trust proxy scheme only when the app is explicitly configured to sit
        # behind its reverse proxy. Host is still taken from the request itself.
        proxy_scheme = ""
        if bool(getattr(config, "WEB_TRUST_PROXY_HEADERS", True)):
            proxy_scheme = str(headers.get("x-forwarded-proto", "")).split(",", 1)[0].strip().lower()
        scheme = proxy_scheme or str(getattr(request.url, "scheme", "http") or "http").lower()
        parsed = urlsplit("//" + request_host) if request_host else urlsplit(str(request.url))
        if not parsed.hostname:
            raise ValueError("Не удалось определить домен панели; задайте WEB_DOMAIN")
        rp_id = parsed.hostname.lower()
        origin = f"{scheme}://{parsed.netloc}"
        if scheme != "https" and rp_id not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Passkeys доступны только по HTTPS")
    return rp_id, origin


def get_webauthn() -> dict[str, Any]:
    """Import the optional library lazily so the rest of the panel remains usable."""
    try:
        from webauthn import (
            base64url_to_bytes,
            generate_authentication_options,
            generate_registration_options,
            options_to_json,
            verify_authentication_response,
            verify_registration_response,
        )
        from webauthn.helpers.structs import (
            AuthenticatorAttachment,
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
    except ImportError as error:
        raise RuntimeError("Модуль WebAuthn не установлен; обновите зависимости FargoVPN") from error
    return locals()


def stable_user_id(username: str) -> bytes:
    """Opaque stable WebAuthn user handle (never expose a Telegram or OS account ID)."""
    return hashlib.sha256(("fargovpn-panel:" + str(username)).encode("utf-8")).digest()[:32]


def registration_options(username: str, rp_id: str, rp_name: str, exclude_ids: list[str]) -> dict[str, Any]:
    api = get_webauthn()
    descriptors = [
        api["PublicKeyCredentialDescriptor"](id=_b64u_decode(value))
        for value in exclude_ids if value
    ]
    options = api["generate_registration_options"](
        rp_id=rp_id,
        rp_name=str(rp_name or "FargoVPN"),
        user_name=str(username),
        user_display_name=str(username),
        user_id=stable_user_id(username),
        timeout=60_000,
        authenticator_selection=api["AuthenticatorSelectionCriteria"](
            authenticator_attachment=api["AuthenticatorAttachment"].PLATFORM,
            resident_key=api["ResidentKeyRequirement"].REQUIRED,
            user_verification=api["UserVerificationRequirement"].REQUIRED,
        ),
        exclude_credentials=descriptors,
    )
    result = json.loads(api["options_to_json"](options))
    return result


def authentication_options(rp_id: str) -> dict[str, Any]:
    api = get_webauthn()
    options = api["generate_authentication_options"](
        rp_id=rp_id,
        timeout=60_000,
        user_verification=api["UserVerificationRequirement"].REQUIRED,
    )
    return json.loads(api["options_to_json"](options))


def verify_registration(
    credential: dict[str, Any], challenge: str, rp_id: str, origin: str
) -> Any:
    api = get_webauthn()
    return api["verify_registration_response"](
        credential=credential,
        expected_challenge=api["base64url_to_bytes"](challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
        require_user_verification=True,
    )


def verify_authentication(
    credential: dict[str, Any], challenge: str, rp_id: str, origin: str,
    public_key: str, sign_count: int,
) -> Any:
    api = get_webauthn()
    return api["verify_authentication_response"](
        credential=credential,
        expected_challenge=api["base64url_to_bytes"](challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=_b64u_decode(public_key),
        credential_current_sign_count=max(0, int(sign_count or 0)),
        require_user_verification=True,
    )


def encode_bytes(value: bytes) -> str:
    return _b64u_encode(value)


def clean_transports(value: Any) -> str:
    transports = value if isinstance(value, list) else []
    allowed = {"usb", "nfc", "ble", "internal", "hybrid", "smart-card"}
    normalized = sorted({str(item) for item in transports if str(item) in allowed})
    return json.dumps(normalized, separators=(",", ":"))
