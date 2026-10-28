"""Telegram device views. All ownership is resolved anew for every action."""
import html
from datetime import datetime
from services import billing, xui_api
from services.subscription_status import has_subscription
from time_utils import from_timestamp
from aiogram.utils.keyboard import InlineKeyboardBuilder

PAGE_SIZE = 6

def _device_name(item):
    model = str(item.get('deviceModel') or '').strip()[:80]
    os_name = str(item.get('deviceOs') or '').strip()[:40]
    version = str(item.get('osVersion') or '').strip()[:25]
    parts = [p for p in (model, ' '.join(p for p in (os_name, version) if p)) if p]
    return ' · '.join(parts) or 'Неизвестное устройство'

def _when(value):
    try:
        stamp = int(value or 0)
        return from_timestamp(stamp / 1000).strftime('%d.%m.%Y %H:%M') if stamp > 0 else 'неизвестно'
    except (ValueError, TypeError, OverflowError, OSError):
        return 'неизвестно'

def get_context(actor, db_path, get_user):
    actor = int(actor)
    if actor <= 0 or not get_user(actor):
        raise PermissionError('Профиль не найден')
    owner = int(billing.profile_owner(actor, db_path))
    profile = get_user(owner)
    if not profile or not has_subscription(profile):
        raise PermissionError('Профиль подписки не найден')
    email = str(profile.get('email') or '').strip()
    uid = str(profile.get('uuid') or '').strip()
    if not email or not uid:
        raise ValueError('Не удалось определить подписку')
    resolved = xui_api.resolve_hwid_client_email_sync(email, uid)
    return owner, resolved

def list_for_actor(actor, db_path, get_user):
    owner, email = get_context(actor, db_path, get_user)
    devices = xui_api.list_client_hwids_sync(email)
    if not isinstance(devices, list):
        raise ValueError('Список устройств недоступен')
    return owner, devices

def delete_for_actor(actor, device_id, db_path, get_user):
    owner, email = get_context(actor, db_path, get_user)
    if owner != int(actor):
        raise PermissionError('Удалять устройства может только владелец профиля')
    devices = xui_api.list_client_hwids_sync(email)
    device_id = int(device_id)
    if device_id <= 0 or sum(1 for d in devices if int(d.get('id') or 0) == device_id) != 1:
        raise ValueError('Устройство больше не найдено')
    # A family link may have changed while the API request was in flight.
    current_owner, current_email = get_context(actor, db_path, get_user)
    if current_owner != owner or current_email != email:
        raise PermissionError('Профиль изменился; откройте список заново')
    xui_api.remove_client_hwid_sync(email, device_id)

def render_list(actor, owner, devices, page=1):
    page_count = max(1, (len(devices) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(int(page), page_count))
    selected = devices[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    lines = ['<b>📱 Устройства</b>', f'Зарегистрировано: <b>{len(devices)}</b>',
             'Последнее обнаружение не означает, что устройство сейчас онлайн.', '']
    if not devices:
        lines.append('Пока нет зарегистрированных устройств. Не все приложения передают данные об устройстве.')
    for index, item in enumerate(selected, (page - 1) * PAGE_SIZE + 1):
        name = html.escape(_device_name(item))
        agent = html.escape(str(item.get('userAgent') or '').strip()[:70])
        when = html.escape(_when(item.get('lastSeen')))
        lines.append(f'<b>{index}. {name}</b>')
        lines.append(f'{agent + " · " if agent else ""}Последнее обнаружение: {when}')
        lines.append('')
    if devices:
        lines.append('Не все приложения передают модель и операционную систему.')
    if int(actor) != int(owner):
        lines.append('Удалять записи устройств может владелец семейного профиля.')
    builder = InlineKeyboardBuilder()
    if int(actor) == int(owner):
        for item in selected:
            try: device_id = int(item.get('id') or 0)
            except (TypeError, ValueError): continue
            if device_id > 0:
                builder.button(text=('Удалить: ' + _device_name(item))[:60], callback_data=f'device:ask:{device_id}')
    if page > 1: builder.button(text='← Назад', callback_data=f'device:page:{page-1}')
    if page < page_count: builder.button(text='Вперёд →', callback_data=f'device:page:{page+1}')
    builder.button(text='🏠 Главное меню', callback_data='menu:home')
    builder.adjust(1)
    return '\n'.join(lines), builder.as_markup()

def render_confirmation(item):
    name = html.escape(_device_name(item))
    return (f'<b>Удалить запись устройства?</b>\n\n{name}\n\n'
            'Это не гарантирует отключение VPN. При следующем обновлении подписки устройство может появиться снова.')

def confirmation_keyboard(device_id):
    builder = InlineKeyboardBuilder()
    builder.button(text='Да, удалить запись', callback_data=f'device:delete:{int(device_id)}')
    builder.button(text='Отмена', callback_data='device:page:1')
    builder.adjust(1)
    return builder.as_markup()
