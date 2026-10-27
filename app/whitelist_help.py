"""Public whitelist collections and compact, platform-specific bot help."""
import html
from urllib.parse import urlsplit


_RAW = "https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/"
_HAPP = _RAW + "Export/Happ/GLOBAL/Vless-Reality-White-Lists-Rus-Mobile-Happ-global.json"
FIELDS = (
    ("WHITELIST_ANDROID_URL", "whitelist_android_url", "Подписка для Happ (Android)", _HAPP),
    ("WHITELIST_IOS_URL", "whitelist_ios_url", "Подписка для Happ (iPhone)", _HAPP),
    ("WHITELIST_WINDOWS_URL", "whitelist_windows_url", "Подписка для Happ (Windows)", _HAPP),
    ("WHITELIST_INCY_SUB_URL", "whitelist_incy_sub_url", "Подписка для Incy (iPhone)", _RAW + "Vless-Reality-White-Lists-Rus-Mobile.txt"),
    ("WHITELIST_BACKUP_URL", "whitelist_backup_url", "Запасная подписка", _RAW + "WHITE-CIDR-RU-all.txt"),
    ("WHITELIST_SOURCE_URL", "whitelist_source_url", "Источник подборок", "https://gitlab.com/igareck/vpn-configs-for-russia/-/tree/main"),
    ("WHITELIST_HAPP_URL", "whitelist_happ_url", "Скачать Happ", "https://www.happ.su/main"),
    ("WHITELIST_INCY_URL", "whitelist_incy_url", "Скачать Incy для iPhone", "https://apps.apple.com/ru/app/incy/id6756943388"),
)
DEFAULTS = {key: default for key, _, _, default in FIELDS}
PLATFORMS = {
    "android": ("🤖 Android", "WHITELIST_ANDROID_URL", "Установите Happ для Android с официальной страницы загрузки. При установке APK подтвердите установку из выбранного источника."),
    "ios": ("🍎 iPhone / iPad", "WHITELIST_IOS_URL", "Установите Happ или Incy для iPhone из App Store по кнопкам ниже."),
    "windows": ("💻 Windows", "WHITELIST_WINDOWS_URL", "Установите Happ для Windows с официальной страницы загрузки. Для VPN/TUN-режима может потребоваться запуск от имени администратора."),
}
NOTICE = (
    "Это <b>бесплатные сторонние подборки</b> проекта igareck/vpn-configs-for-russia. "
    "Серверы не принадлежат FargoVPN. Доступность зависит от оператора и региона; "
    "работа при белых списках не гарантируется."
)


def safe_url(value: str) -> str:
    """Validate public links without requesting their content; blank hides a link."""
    raw = str(value or "")
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("Управляющие символы недопустимы в ссылке")
    clean = raw.strip()
    if not clean:
        return ""
    if len(clean.encode("utf-16-le")) // 2 > 1000 or any(c.isspace() for c in clean):
        raise ValueError("Нужна ссылка без пробелов и управляющих символов, не длиннее 1000 символов")
    if "\\" in clean:
        raise ValueError("Обратная косая черта недопустима в ссылке")
    try:
        parts = urlsplit(clean)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Нужен полный URL с http:// или https://")
        if parts.username is not None or parts.password is not None:
            raise ValueError("Логин и пароль нельзя хранить внутри URL")
        parts.port  # Reject malformed ports and IPv6 addresses before Telegram sees them.
    except ValueError as error:
        raise ValueError(str(error)) from error
    return clean


def link(config, key: str) -> str:
    """Use defaults for old installations, but preserve intentionally cleared fields."""
    try:
        return safe_url(getattr(config, key, DEFAULTS[key]))
    except (ValueError, KeyError):
        return ""


def overview_text() -> str:
    return (
        "🛜 <b>Помощь при белых списках</b>\n\n" + NOTICE
        + "\n\nВыберите устройство: получите ссылки на подписки и краткую инструкцию. "
        "Платная подписка FargoVPN для этого раздела не требуется. "
        "Заранее установите приложение и сохраните подписку, пока интернет доступен."
    )


def subscription_url(config, platform: str, app: str, backup: bool = False) -> str:
    """Resolve only configured collections, never a URL supplied by a visitor."""
    if platform not in PLATFORMS or app not in {"happ", "incy"}:
        raise ValueError("Неизвестное устройство или приложение")
    if app == "incy" and platform != "ios":
        raise ValueError("Incy доступен только для iPhone / iPad")
    if backup:
        key = "WHITELIST_BACKUP_URL"
    else:
        key = "WHITELIST_INCY_SUB_URL" if app == "incy" else PLATFORMS[platform][1]
    return link(config, key)


def instruction_text(config, platform: str, *, auto_import: bool = True) -> str:
    """Render two copyable subscription links; source and app links go on buttons."""
    label, key, install = PLATFORMS[platform]
    main = link(config, key)
    backup = link(config, "WHITELIST_BACKUP_URL")
    incy = link(config, "WHITELIST_INCY_SUB_URL") if platform == "ios" else ""
    manual_client = "Happ"
    if platform == "ios" and not auto_import and incy:
        main = incy
        manual_client = "Incy"
    lines = [f"<b>🛜 Белые списки — {label}</b>", "", NOTICE, ""]
    if main:
        lines += [f"<b>Основная подписка {manual_client}:</b>", f"<code>{html.escape(main)}</code>", ""]
    else:
        lines += ["Основная ссылка пока не задана администратором.", ""]
    if backup:
        lines += ["<b>Запасная подписка:</b>", f"<code>{html.escape(backup)}</code>", ""]
    if incy and auto_import:
        lines += ["Для Incy используйте кнопку «Открыть в Incy» ниже: она передаст отдельную универсальную подписку.", ""]
    import_step = (
        "Нажмите кнопку подключения к нужной подборке ниже: ссылка передастся в приложение автоматически. "
        "Если оно не открылось, скопируйте ссылку выше и добавьте подписку по ссылке вручную."
        if auto_import else
        f"Скопируйте ссылку выше. В {manual_client} выберите добавление подписки по ссылке, вставьте её и сохраните."
    )
    if main or backup or incy:
        lines += [
            "<b>Как подключить</b>",
            "1. " + install,
            "2. " + import_step,
            "3. Обновите подписку, проверьте доступность серверов и подключитесь к работающему.",
            "4. Включите подключение и подтвердите системный запрос на VPN, если он появится.",
            "5. Ссылка постоянная: обновление подписки получает свежий список из источника. Если в вашей версии приложения есть автообновление, включите его; иначе обновляйте список вручную. Новую ссылку в боте запрашивать не нужно.",
            "",
            "<b>Если не подключается:</b> обновите подписку и попробуйте другой сервер или запасную ссылку. "
            "Если не загружается сам список, добавьте его заранее через доступный Wi-Fi. "
            "Доступность файла подписки не означает доступность его серверов.",
        ]
    else:
        lines += ["Ссылки временно недоступны. Попробуйте позже или откройте источник подборок."]
    return "\n".join(lines)
