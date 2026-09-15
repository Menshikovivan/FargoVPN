# FargoVPN — VPN Service Platform

![Version](https://img.shields.io/badge/version-4.2-5865F2)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141.1-009688)
![aiogram](https://img.shields.io/badge/aiogram-3.31.0-2CA5E0)
![License](https://img.shields.io/badge/license-Personal%20Use-lightgrey)

**FargoVPN** — self-hosted платформа для управления VPN-сервисом через Telegram, веб-панель администратора и личный PWA/Telegram-кабинет.

Проект объединяет Telegram-бота, веб-панель, Telegram Mini App/PWA, интеграцию с **3x-ui**, приём и проверку чеков, резервное копирование, диагностику, рассылки и автоматические обновления через GitHub Releases.

> **FargoVPN 4.2** — PostgreSQL-first релиз: безопаснее обрабатывает существующие PostgreSQL-установки, миграцию старых SQLite-баз и lifecycle обновлений, сохраняя отдельную SQLite-базу 3x-ui.

---

## Возможности

### 🤖 Telegram-бот
- Управление подпиской и покупка/продление VPN-доступа.
- Приём фотографий чеков.
- Связь с администратором и история сообщений.
- Инструкции по подключению и помощь при проблемах.
- Идентификация клиента по Telegram ID.

### 🖥 Веб-панель
- Пользователи: создание, продление, привязка Telegram, блокировка, удаление и работа с клиентами 3x-ui.
- Платежи и проверка чеков.
- Сообщения и журнал переписки.
- Массовые рассылки.
- Мониторинг, диагностика и просмотр системных журналов.
- Резервные копии и восстановление.
- Управление обновлениями и откатом.
- Настройки сервиса и интеграций.
- PWA/Web Push и личный кабинет.

### 🔌 3x-ui
- Работа с 3x-ui через API.
- Получение актуальных данных о клиентах, трафике и подключениях.
- Синхронизация данных между FargoVPN и 3x-ui.

**Важно:** `/etc/x-ui/x-ui.db` остаётся отдельной SQLite-базой 3x-ui. PostgreSQL-миграция FargoVPN не переносит `x-ui.db` и не должна менять структуру базы 3x-ui.

### 💳 Платежи и OCR
- Загрузка чека через Telegram.
- Локальное распознавание через Tesseract (`pytesseract`) и Pillow.
- Проверка суммы, получателя, статуса операции и срока давности.
- Защита от повторной отправки одного и того же чека.
- Сомнительные операции могут оставаться на ручной проверке.

### 💾 Резервное копирование
В 4.2 база приложения работает с PostgreSQL. Локальный backup может включать PostgreSQL logical dump, исходники приложения, конфигурационные данные и отдельные данные 3x-ui.

Дополнительная доставка поддерживается через Telegram и Яндекс.Диск. Локально проверенный архив не считается неуспешным только из-за временной недоступности внешнего хранилища.

### 🔄 Обновления и rollback
- Источник релизов — GitHub Releases.
- Перед обновлением создаётся резервная копия.
- Архив проверяется перед установкой.
- При ошибке предусмотрен откат.
- Состояние миграции хранится вне рабочего каталога приложения.

---

## 🆕 FargoVPN 4.2

### PostgreSQL-first lifecycle

- Уже настроенный `DATABASE_URL` определяется до миграции.
- Если приложение уже использует PostgreSQL, старая SQLite-копия не импортируется поверх актуальных данных.
- Legacy SQLite-установка без PostgreSQL может быть автоматически мигрирована.
- Состояние миграции хранится в `/var/lib/vpn-service/migration-state/` и не удаляется при `rsync --delete`.
- Существующий PostgreSQL DSN сохраняется при обновлении.
- `/etc/x-ui/x-ui.db` остаётся SQLite и не мигрируется.

### Совместимость PostgreSQL

В 4.2 сохранены исправления для:

- генерации ID без зависимости от SQLite `lastrowid`;
- PostgreSQL-safe группировки страницы сообщений;
- SQLite-style datetime/scalar MAX совместимости;
- построения кнопок личного кабинета.

Подробности: [`RELEASE_NOTES_4.2.md`](./RELEASE_NOTES_4.2.md).

---

## Архитектура

```text
                         ┌──────────────────────┐
                         │      Telegram Bot     │
                         └──────────┬───────────┘
                                    │
┌──────────────────┐       ┌────────▼─────────┐       ┌──────────────────┐
│ Telegram / PWA   │◄─────►│     FargoVPN     │◄─────►│      3x-ui        │
│ Client Cabinet   │       │ Bot + Web + Jobs │       │     REST API       │
└──────────────────┘       └────────┬─────────┘       └────────┬─────────┘
                                    │                           │
                              ┌─────▼─────┐               ┌─────▼─────┐
                              │ PostgreSQL │               │  x-ui.db  │
                              │  app data  │               │  SQLite   │
                              └─────┬─────┘               └───────────┘
                                    │
                              ┌─────▼─────────┐
                              │ Backup / Push  │
                              │ Telegram/Yandex│
                              └───────────────┘
```

---

## Технологии

| Компонент | Технология |
|---|---|
| Telegram | Python + aiogram 3.31 |
| Web | FastAPI 0.141 + Uvicorn |
| Application DB | PostgreSQL |
| 3x-ui DB | SQLite (`x-ui.db`) |
| PostgreSQL runtime | SQLAlchemy 2.x + psycopg 3 |
| HTTP | httpx + aiohttp |
| OCR | Tesseract + pytesseract + Pillow |
| System monitoring | psutil |
| Deployment | Bash + systemd |
| Updates | GitHub Releases |
| PWA | Service Worker + Web Push |

---

## Требования

- Linux VPS/сервер с `systemd`.
- Root-доступ для установки.
- Python 3.10+.
- Установленная и доступная 3x-ui с API.
- Telegram Bot Token.
- Для полного профиля — Nginx L4 Stream Router Mask согласно логике установщика.

PostgreSQL может быть установлен скриптами проекта либо уже существовать на сервере.

---

## Установка

### Full

```bash
cd /root
# распакуйте архив FargoVPN-4.2
cd FargoVPN-4.2
sudo bash install.sh --profile full
```

### Lite

```bash
sudo bash install.sh --profile lite
```

### Обновление существующей установки

```bash
sudo bash install.sh --update-existing /root/vpn_bot
```

При обновлении установщик проверяет текущую конфигурацию, создаёт backup, определяет backend БД, при необходимости выполняет SQLite → PostgreSQL migration, обновляет файлы и запускает финальные проверки.

---

## PostgreSQL migration

Подробная инструкция находится в [`POSTGRESQL_MIGRATION.md`](./POSTGRESQL_MIGRATION.md).

Ключевой принцип 4.2:

> **Если приложение уже работает на PostgreSQL, старая SQLite-база не должна использоваться как источник повторной миграции.**

Миграция старой SQLite-установки выполняется с сохранением исходной базы без её изменения.

Пример создания immutable manifest:

```bash
python migration_tool.py \
  --sqlite /path/to/vpn_bot.db \
  --manifest migration_manifest.json \
  --manifest-only
```

---

## Системные службы

```text
vpn-service-bot.service
vpn-service-web.service
vpn-service-web.socket
vpn-service-backup.service
vpn-service-backup.timer
vpn-service-reminders.service
vpn-service-reminders.timer
vpn-service-update@.service
vpn-service-broadcast@.service
vpn-service-nginx-guard.service
```

Проверка:

```bash
sudo systemctl status vpn-service-bot --no-pager
sudo systemctl status vpn-service-web --no-pager
sudo systemctl status vpn-service-web.socket --no-pager
```

---

## Диагностика

Web health:

```bash
curl -fsS http://127.0.0.1:8088/health
```

Общая диагностика:

```bash
sudo /root/vpn_bot/.venv/bin/python \
  /root/vpn_bot/diagnostics.py --live --json
```

Дополнительные проверки:

```bash
sudo bash diagnose_panel.sh
sudo bash check_tls.sh
```

---

## Тесты

В репозитории есть регрессионные и контрактные тесты для PostgreSQL, миграции, referral/rebind, backup/push performance, release contract и других сценариев.

```bash
python -m pytest tests -q
```

---

## Структура проекта

```text
FargoVPN-4.2/
├── app/
├── services/
│   ├── xui_api.py
│   ├── subscriptions.py
│   ├── media.py
│   ├── receipt_ocr.py
│   └── telegram_events.py
├── tests/
├── scripts/
│   └── setup_postgresql.sh
├── main.py
├── webapp.py
├── cabinet_service.py
├── db.py
├── diagnostics.py
├── migration_tool.py
├── identity_migration.py
├── backup.py
├── push_service.py
├── update_manager.py
├── update_worker.py
├── rollback_worker.py
├── broadcast_manager.py
├── broadcast_worker.py
├── sync_users.py
├── referral_codes.py
├── referral_rewards.py
├── install.sh
├── web_start.sh
├── service-worker.js
├── SECURITY.md
├── POSTGRESQL_MIGRATION.md
├── CHANGELOG.md
└── VERSION
```

---

## Безопасность

Проект включает rate limiting авторизации, PBKDF2-хеширование пароля панели, security headers при HTTPS, защищённую выдачу медиа, проверку обновляемых архивов, backup перед обновлением и отдельное хранение migration state.

Не храните `config.py`, Bot Token, API-токены и пароли в публичном репозитории.

Подробнее: [`SECURITY.md`](./SECURITY.md).

---

## Документация

- [`POSTGRESQL_MIGRATION.md`](./POSTGRESQL_MIGRATION.md) — миграция и восстановление PostgreSQL.
- [`SECURITY.md`](./SECURITY.md) — безопасность.
- [`CHANGELOG.md`](./CHANGELOG.md) — история изменений.
- [`RELEASE_NOTES_4.2.md`](./RELEASE_NOTES_4.2.md) — изменения версии 4.2.
- [`LICENSE`](./LICENSE) — Personal Use License 1.0.

---

## Релизы

Официальные релизы и архивы:

https://github.com/Menshikovivan/FargoVPN/releases

Архив полного профиля:

```text
VPN_Service_Platform_{version}_FULL.tar.gz
```

---

## Лицензия

FargoVPN распространяется по **Personal Use License 1.0**.

Разрешено личное некоммерческое использование, изучение и модификация для собственных нужд. Коммерческое использование, публичное распространение, размещение как платного стороннего сервиса и иные варианты за пределами лицензии требуют письменного разрешения правообладателя.

Полный текст: [`LICENSE`](./LICENSE).

---

### FargoVPN 4.2

**Telegram + Web Panel + PWA + 3x-ui + PostgreSQL + backups + updates**

Self-hosted. Контроль над инфраструктурой остаётся у владельца сервера.
