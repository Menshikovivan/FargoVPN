# FargoVPN — VPN Service Platform

![Version](https://img.shields.io/badge/version-4.3.6-5865F2)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141.1-009688)
![aiogram](https://img.shields.io/badge/aiogram-3.31.0-2CA5E0)
![License](https://img.shields.io/badge/license-Personal%20Use-lightgrey)

**FargoVPN** — self-hosted платформа для управления VPN-сервисом через Telegram, веб-панель администратора и личный PWA/Telegram-кабинет.

Проект объединяет Telegram-бота, веб-панель, Telegram Mini App/PWA, интеграцию с **3x-ui**, приём и проверку чеков, резервное копирование, диагностику, рассылки и автоматические обновления через GitHub Releases.

> **FargoVPN 4.3.6 hardened** — PostgreSQL-релиз с атомарной обработкой платежей, CSRF, одноразовыми ссылками кабинета, шифрованием внешних backup и staged restore. Рекомендуется установка из проверенного локального архива.

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

**Важно:** актуальная 3x-ui может использовать PostgreSQL через `XUI_DB_DSN`; `/etc/x-ui/x-ui.db` поддерживается только для legacy auto-detect. База FargoVPN и база 3x-ui управляются раздельно.

### 💳 Платежи и OCR
- Загрузка чека через Telegram.
- Локальное распознавание через Tesseract (`pytesseract`) и Pillow.
- Проверка суммы, получателя, статуса операции и срока давности.
- Защита от повторной отправки одного и того же чека.
- Сомнительные операции могут оставаться на ручной проверке.

### 💾 Резервное копирование
В 4.3.4 база приложения работает с PostgreSQL. Локальный backup может включать PostgreSQL logical dump, исходники приложения, конфигурационные данные и отдельные данные 3x-ui.

Дополнительная доставка поддерживается через Telegram и Яндекс.Диск. Успешность внешней доставки проверяется отдельно от локального создания backup.

### 🔄 Обновления и rollback
- Источник релизов — GitHub Releases.
- Перед обновлением создаётся резервная копия.
- Архив проверяется перед установкой.
- При ошибке предусмотрен откат.
- Состояние миграции хранится вне рабочего каталога приложения.

---

## 🆕 FargoVPN 4.3.6

### Безопасная установка из архива

До запуска сравните SHA-256 архива с опубликованным по независимому каналу. Не передавайте непроверенный сетевой скрипт прямо в `bash` от root.

### Full

```bash
sha256sum VPN_Service_Platform_4.3.6_FULL.tar.gz
mkdir -p /root/fargovpn-release
tar -xzf VPN_Service_Platform_4.3.6_FULL.tar.gz -C /root/fargovpn-release
cd /root/fargovpn-release/FargoVPN-4.3.6
sudo bash ./install.sh --profile full
```

Если Mask ещё не установлен, полный профиль требует SHA-256 отдельно проверенного `install.sh` Mask: `sudo env MASK_INSTALLER_SHA256=<64 hex> bash ./install.sh --profile full`. Уже установленный Mask повторно не скачивается.

### Lite

```bash
sudo bash ./install.sh --profile lite
```

### Обновление существующей установки

```bash
sudo bash ./install.sh --update-existing /root/vpn_bot
```

При обновлении полный пакет скачивается автоматически; вручную загружать архив на сервер не требуется.

---

## Архитектура GitHub

В `main` хранятся только файлы, нужные для быстрого запуска и публикации актуального релиза:

```text
FargoVPN/
├── install.sh
├── README.md
├── VERSION
├── LICENSE
├── CHANGELOG.md
├── RELEASE_NOTES_4.3.6.md
├── FargoVPN_FULL.tar.gz
├── FargoVPN_FULL.tar.gz.sha256
└── .github/
    └── workflows/
        └── publish-release.yml
```

Полный исходный код и установщик находятся внутри `FargoVPN_FULL.tar.gz`.

Это позволяет держать корень `main` компактным и не загружать десятки отдельных файлов через веб-интерфейс GitHub.

---

## Архитектура приложения

```text
                         ┌──────────────────────┐
                         │      Telegram Bot     │
                         └──────────┬───────────┘
                                    │
┌──────────────────┐       ┌────────▼─────────┐       ┌──────────────────┐
│ Telegram / PWA   │◄─────►│     FargoVPN     │◄─────►│      3x-ui        │
│ Client Cabinet   │       │ Bot + Web + Jobs │       │     REST API      │
└──────────────────┘       └────────┬─────────┘       └────────┬─────────┘
                                    │                           │
                              ┌─────▼─────┐               ┌─────▼─────┐
                              │ PostgreSQL │               │ PostgreSQL│
                              │  app data  │               │  3x-ui DB │
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
| 3x-ui DB | PostgreSQL (`XUI_DB_DSN`), legacy SQLite auto-detect |
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

## PostgreSQL migration

Подробная инструкция находится в [`POSTGRESQL_MIGRATION.md`](./POSTGRESQL_MIGRATION.md).

Ключевой принцип 4.3.4:

> **Если приложение уже работает на PostgreSQL, старая SQLite-база не должна использоваться как источник повторной миграции.**

Миграция старой SQLite-установки выполняется с сохранением исходной базы без её изменения.

Пример создания immutable manifest:

```bash
python migration_tool.py \
  --sqlite /path/to/vpn_bot.db \
  --manifest /tmp/fargovpn-migration-manifest.json \
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
curl --unix-socket /run/vpn-service/fargovpn.sock http://localhost/health
```

Общая диагностика:

```bash
sudo /root/vpn_bot/.venv/bin/python \
  /root/vpn_bot/diagnostics.py --live --json
```

Дополнительные проверки:

```bash
sudo bash /root/vpn_bot/diagnose_panel.sh
sudo bash /root/vpn_bot/check_tls.sh
```

---

## Тесты

В полном исходном пакете находятся регрессионные и контрактные тесты.

```bash
python -m pytest tests -q
```

Test-зависимости:

```bash
python -m pip install -r requirements-test.txt
```

---

## Структура полного пакета

```text
FargoVPN-4.3.6/
├── app/
│   └── VERSION
├── services/
├── scripts/
├── static/
├── tests/
├── tools/
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
├── config.example.py
├── requirements.txt
├── requirements-lite.txt
├── requirements-test.txt
├── SECURITY.md
├── POSTGRESQL_MIGRATION.md
├── CHANGELOG.md
├── RELEASE_NOTES_4.3.6.md
├── LICENSE
└── VERSION
```

---

## Безопасность

Проект включает rate limiting авторизации, PBKDF2-хеширование пароля панели, security headers при HTTPS, защищённую выдачу медиа, проверку обновляемых архивов, backup перед обновлением и отдельное хранение migration state.

Не храните `config.py`, Bot Token, API-токены, пароли PostgreSQL и пароль приложения Яндекс.Диска в публичном репозитории.

Подробнее: [`SECURITY.md`](./SECURITY.md).

---

## Релизы

Официальные релизы:

https://github.com/Menshikovivan/FargoVPN/releases

Актуальный полный пакет в `main`:

```text
FargoVPN_FULL.tar.gz
```

Версионный asset GitHub Release:

```text
VPN_Service_Platform_{version}_FULL.tar.gz
```

---

## Как выпускается новая версия

Источник истины для актуальной публичной версии — ветка `main`.

Начиная с 4.3.4 ручная публикация файлов в `main` не требуется. Главная панель делает это автоматически после успешной публикации GitHub Release.

Для нового релиза из панели:

1. Подготовьте полный архив новой версии.
2. Откройте `Настройки → Обновления` на назначенной GitHub Publisher панели.
3. Загрузите `.tar.gz`.
4. FargoVPN проверит архив и опубликует GitHub Release.
5. Сразу после успешного Release панель одним Git commit обновит публичную `main`.
6. Старые лишние файлы `main` будут удалены из нового дерева; force-push не используется.

Если `main` успела измениться параллельно, публикация `main` остановится без перезаписи чужого commit. Уже созданный Release останется доступен.

Workflow `publish-release.yml` больше не нужен для обычной публикации из панели и может использоваться только как дополнительный CI-механизм.

GitHub Release содержит versioned asset:

```text
VPN_Service_Platform_{version}_FULL.tar.gz
```

GitHub поддерживает прямую ссылку на asset последнего Release через `/releases/latest/download/<asset-name>`; workflow использует versioned asset для системы обновлений FargoVPN. citeturn462422search0turn590512search1

---

## Лицензия

FargoVPN распространяется по **Personal Use License 1.0**.

Разрешено личное некоммерческое использование, изучение и модификация для собственных нужд. Коммерческое использование, публичное распространение, размещение как платного стороннего сервиса и иные варианты за пределами лицензии требуют письменного разрешения правообладателя.

Полный текст: [`LICENSE`](./LICENSE).

---

### FargoVPN 4.3.6

- Исправлена отправка сообщений пользователям через панель при PostgreSQL backend.
- Старые integer-колонки статуса сообщений автоматически приводятся к boolean.
- Ошибки отправки больше не маскируются сообщением `JSON.parse`.

**Telegram + Web Panel + PWA + 3x-ui + PostgreSQL + backups + updates**

Self-hosted. Контроль над инфраструктурой остаётся у владельца сервера.
