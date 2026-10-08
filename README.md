# FargoVPN

> Telegram-бот и веб-панель для управления продажей VPN-подписок с интеграцией 3x-ui и системой обновлений через GitHub Releases.

<!-- Баннер намеренно не добавлен: используйте docs/screenshots/banner.png после подготовки реального изображения. -->

[![Release](https://img.shields.io/github/v/release/Menshikovivan/FargoVPN?display_name=tag&sort=semver)](https://github.com/Menshikovivan/FargoVPN/releases)
[![CI](https://github.com/Menshikovivan/FargoVPN/actions/workflows/ci.yml/badge.svg)](https://github.com/Menshikovivan/FargoVPN/actions/workflows/ci.yml)
[![Last commit](https://img.shields.io/github/last-commit/Menshikovivan/FargoVPN)](https://github.com/Menshikovivan/FargoVPN/commits/main)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Telegram](https://img.shields.io/badge/Telegram-bot-26A5E4?logo=telegram&logoColor=white)](https://telegram.org/)

**Текущая версия: `5.1`**

## Оглавление

- [Что это](#что-это)
- [Возможности](#возможности)
- [Скриншоты](#скриншоты)
- [Архитектура](#архитектура)
- [Требования](#требования)
- [Быстрая установка](#быстрая-установка)
- [Ручная установка](#ручная-установка)
- [Настройка](#настройка)
- [Обновление](#обновление)
- [Откат](#откат)
- [GitHub Releases из панели](#github-releases-из-панели)
- [Структура проекта](#структура-проекта)
- [Диагностика и логи](#диагностика-и-логи)
- [FAQ](#faq)
- [Безопасность](#безопасность)
- [Изменения](#изменения)
- [Разработка](#разработка)
- [Дорожная карта](#дорожная-карта)
- [Лицензия](#лицензия)

## Что это

FargoVPN состоит из двух пользовательских интерфейсов и серверной части:

- Telegram-бот на Python для регистрации пользователей, оформления и продления подписок, оплаты, поддержки и выдачи данных подписки;
- веб-панель администратора на FastAPI для пользователей, сообщений, подписок, оплат, рассылок, резервных копий, диагностики, Push и обновлений;
- интеграция с 3x-ui и его клиентами/inbounds;
- PostgreSQL как основная БД с миграционной совместимостью со старой SQLite-базой;
- установщик и менеджер обновлений с резервным копированием и откатом;
- публикация релизов через GitHub Releases.

Описание выше ограничено возможностями, представленными в исходном коде версии 5.1.

## Возможности

### Telegram-бот

Поддерживаются регистрация и доступ пользователя, работа с подпиской, платежными заявками и чеками, пользовательская поддержка, реферальная логика и выдача данных подключения. Конкретные тексты и поведение зависят от настроек `config.py`.

### Веб-панель

В коде присутствуют разделы обзора/диагностики, пользователи, сообщения, платежи и настройки, а также управление резервными копиями/восстановлением и обновлениями. Для Push используется отдельный Service Worker.

### 3x-ui

`services/xui_api.py` и связанные сервисы работают с 3x-ui API и клиентами inbounds. Настройки URL, авторизации и управляемых inbound задаются в `config.py`.

### Обновления

Источник обновлений — GitHub Releases. Проверка новой версии берёт `VERSION` установленного экземпляра и реальные данные GitHub API. Публикация из панели автоматически пересобирает полный безопасный tree `main` из загруженного архива, удаляет из текущего `main` всё, чего нет в новой версии, создаёт тег `vX.Y`, GitHub Release и assets, а также пытается привести безопасные метаданные репозитория к штатному виду. Перед изменением `main` создаётся backup-ref; при ошибке выполняется автоматический rollback без `force-push`. До завершения API-проверок успех не показывается.

### Резервное копирование и откат

Перед обновлением создаются резервная копия текущей установки и снимок systemd. При ошибке установщик пытается автоматически восстановить предыдущую версию. В панели есть откат к последней валидной копии и отдельное безопасное понижение до опубликованной более старой версии.

## Скриншоты

Реальные изображения пока не добавлены в пакет. Для README подготовлены места в [`docs/screenshots/`](docs/screenshots/). Список нужных кадров — в [`docs/screenshots/README.md`](docs/screenshots/README.md).

## Архитектура

```text
Telegram
   │
   ▼
main.py / services/
   │
   ├── PostgreSQL
   ├── 3x-ui API
   └── payment / receipt / subscription services

Browser
   │ HTTPS
   ▼
External Nginx
   │ Unix socket
   ▼
FastAPI webapp.py
   │
   ├── static/
   ├── Service Worker / Push
   ├── backup / restore
   └── update_manager.py → GitHub Releases
```

Веб-приложение по умолчанию использует Unix-socket `/run/vpn-service/fargovpn.sock`; внешний HTTPS reverse proxy остаётся отдельной частью серверной конфигурации.

## Требования

Для штатного установщика рассчитано окружение Linux с `systemd`, правами `root` и Debian/Ubuntu с `apt`.

Установщик автоматически устанавливает Python-инструменты, `rsync`, `curl`, `openssl`, `socat`, SQLite CLI и Tesseract OCR. PostgreSQL подготавливается штатным скриптом `scripts/setup_postgresql.sh`.

**Nginx устанавливается и настраивается не автоматически.** Перед полноценным запуском панели должен существовать внешний Nginx, а его конфигурация должна проксировать запросы на сокет FargoVPN. Установщик выполняет проверку `nginx -t`, но не подменяет пользовательскую конфигурацию.

Минимальная версия Python для runtime: **3.10**.

## Быстрая установка

Рекомендуемый способ — всегда брать актуальный `install.sh` из `main`, а сам пакет получать из последнего GitHub Release:

```bash
curl -fsSL https://raw.githubusercontent.com/Menshikovivan/FargoVPN/main/install.sh | sudo bash
```

Bootstrap не хранит релизный архив в `main`: он скачивает `FargoVPN_FULL.tar.gz` и SHA-256 из последнего GitHub Release, проверяет контрольную сумму и запускает внутренний установщик.

## Ручная установка

```bash
git clone https://github.com/Menshikovivan/FargoVPN.git
cd FargoVPN
sudo bash install.sh --profile full
```

Установщик:

1. проверяет права и системные зависимости;
2. определяет новую или существующую установку;
3. создаёт резервную копию перед обновлением;
4. синхронизирует исходники, сохраняя `config.py`, базы, `.env`, логи и виртуальное окружение;
5. устанавливает Python-зависимости;
6. выполняет миграции БД;
7. обновляет systemd-службы;
8. выполняет итоговые health-check и проверку версии Service Worker.

## Настройка

Production-конфигурация создаётся установщиком в `config.py` с правами `0600`. Файл `.env.example` в репозитории — только справочник переменных установщика; приложение не читает dotenv автоматически.

### Основные параметры `config.py`

| Параметр | Назначение |
|---|---|
| `SERVICE_NAME` | Название VPN-сервиса в интерфейсах и сообщениях. |
| `BOT_TOKEN` | Telegram Bot Token. Хранить только на сервере. |
| `ADMIN_IDS` | Telegram ID администраторов. |
| `DATABASE_URL` | PostgreSQL DSN FargoVPN. |
| `BASE_URL` | Базовый URL панели/публичной части. |
| `MASTER_API_URL` | URL управляющего API 3x-ui. |
| `MASTER_API_TOKEN` | Токен/учётные данные доступа к 3x-ui API. |
| `SUB_BASE_URL` | Базовый адрес пользовательских подписок. |
| `PAYMENT_PRICE` | Стоимость подписки. |
| `PAYMENT_PHONE` / `PAYMENT_BANK` / `PAYMENT_RECEIVER` | Реквизиты, участвующие в оплате и проверке чеков. |
| `RECEIPT_OCR_ENABLED` | Включение локального OCR чеков. |
| `WEB_HOST` / `WEB_SOCKET_PATH` | Локальный web runtime и Unix-socket. |
| `WEB_PUBLIC_PREFIX` | Публичный prefix панели. |
| `WEB_USERNAME` | Логин администратора панели. |
| `WEB_PASSWORD_HASH` | Хэш пароля панели, не сам пароль. |
| `WEB_SECRET_KEY` | Секрет сессий/куки панели. |
| `XUI_PANEL_URL` | URL панели 3x-ui. |
| `XUI_USERNAME` / `XUI_PASSWORD` | Дополнительная legacy-аутентификация 3x-ui, если используется. |
| `XUI_MANAGED_INBOUND_IDS` | Явный список inbound, если он нужен вместо автоматического выбора. |
| `PUSH_VAPID_PUBLIC_KEY` | VAPID public key для Web Push. |
| `PUSH_VAPID_PRIVATE_KEY_PATH` | Путь к приватному VAPID-ключу вне Git. |
| `GITHUB_REPOSITORY_OWNER` | Владелец GitHub-репозитория обновлений. |
| `GITHUB_REPOSITORY_NAME` | Имя репозитория, по умолчанию `FargoVPN`. |
| `GITHUB_API_TOKEN` | GitHub token для публикации релизов; хранится только на сервере. |
| `GITHUB_RELEASE_TAG_PREFIX` | Префикс тега. Для v5.1 используется `v`, поэтому тег — `v5.1`. |
| `GITHUB_TARGET_BRANCH` | Публичная ветка; для проекта должна быть `main`. |

После установки заполните Telegram, 3x-ui, публичный URL, оплату и параметры панели. Затем проверьте внешний Nginx и откройте панель по настроенному `WEB_PUBLIC_PREFIX`.

## Обновление

### Через веб-панель

Откройте **Обновления**, выполните проверку и установите найденный GitHub Release. Источник определяется реальным ответом GitHub API; версия берётся из `VERSION` установленной системы.

Перед заменой файлов установщик создаёт backup и systemd snapshot. При ошибке выполняется автоматический rollback, а статус задачи сохраняется для панели.

### Через консоль

Для установки в стандартный каталог:

```bash
sudo /root/vpn_bot/install.sh --update-existing /root/vpn_bot
```

Для другой существующей установки подставьте её реальный путь.

## Откат

В панели **Обновления** доступны:

- откат к последнему валидному pre-update backup;
- принудительная установка опубликованной более ранней версии из GitHub Releases.

До замены текущей версии создаётся новый safety backup. Полный rollback включает файлы приложения и соответствующий snapshot systemd в рамках штатного механизма установщика.

## GitHub Releases из панели

Публикация доступна только назначенной главной панели. GitHub token сохраняется в серверном `config.py` и не попадает в `main`.

Для версии `5.1` ожидаемый поток:

```text
загрузка архива
    ↓
проверка VERSION / CHANGELOG / содержимого
    ↓
атомарный commit main
    ↓
создание или проверка tag v5.1
    ↓
GitHub Release
    ↓
загрузка versioned + generic assets
    ↓
проверка имени / размера / digest / tag SHA
    ↓
"Версия 5.1 успешно загружена на GitHub"
```

Релизные архивы находятся только в **GitHub Release assets**, а не в дереве `main`.

## Структура проекта

```text
FargoVPN/
├── .github/
│   ├── ISSUE_TEMPLATE/
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── workflows/ci.yml
├── docs/
│   ├── postgresql-migration.md
│   ├── operations.md
│   └── screenshots/
├── migrations/
├── scripts/
├── services/
├── static/
├── tests/
├── .env.example
├── .gitignore
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── SECURITY.md
├── VERSION
├── requirements.txt
├── requirements-dev.txt
├── install.sh
├── main.py
├── webapp.py
└── update_manager.py
```

`app/VERSION` и `static/VERSION` остаются синхронизированными compatibility-маркерами старого release contract; runtime-источник версии — корневой `VERSION`.

## Диагностика и логи

Базовые проверки:

```bash
sudo systemctl status vpn-service-bot.service
sudo systemctl status vpn-service-web.socket
sudo systemctl status vpn-service-web.service
```

Логи установщика и восстановления:

```text
/var/log/vpn-service-install.log
/var/log/vpn-service-restore.log
```

Лог приложения по умолчанию определяется `APP_LOG_PATH` и в стандартном конфиге указывает на `/var/log/vpn_bot.log`.

Дополнительные команды и operator notes собраны в [`docs/operations.md`](docs/operations.md).

## FAQ

**Почему bootstrap не скачивает архив из `main`?**  Начиная с 5.1 `main` содержит исходники и установочный bootstrap, а release-архивы хранятся только в GitHub Releases. Это уменьшает размер ветки и исключает дублирование бинарного пакета.

**Сохранятся ли настройки при обновлении?**  Да: штатный installer сохраняет `config.py`, runtime data/DB, `.env`, virtualenv и логи, а перед заменой создаёт резервную копию.

**Можно ли удалить GitHub token из `config.py` и продолжить публикацию?**  Нет. Публикация через панель требует GitHub API token с правом записи в выбранный репозиторий.

**Почему `pytest` локально может отличаться от CI?**  Runtime-пакет имеет внешние зависимости; CI устанавливает `requirements-dev.txt` перед запуском тестов. На production-сервере используйте штатный виртуальный environment установщика.

## Безопасность

Подробности: [`SECURITY.md`](SECURITY.md).

Никогда не коммитьте production `config.py`, `.env`, базы, логи или приватные ключи. Удаление секрета только из текущего `main` не делает его безопасным в истории: сначала перевыпустите секрет, затем очищайте Git history по отдельному плану.

## Изменения

- [CHANGELOG.md](CHANGELOG.md) — история изменений в формате Keep a Changelog.
- [GitHub Releases](https://github.com/Menshikovivan/FargoVPN/releases) — опубликованные пакеты и SHA-256.

## Разработка

Установите dev-зависимости и запустите проверки:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
pytest -q
```

Для изменений установщика дополнительно выполните:

```bash
bash -n install.sh
find . -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
python3 -m compileall -q .
```

Вклад принимается через Pull Request. См. [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Дорожная карта

На момент 5.1 публичная дорожная карта с фиксированными сроками не заявлена. Новые задачи и предложения следует оформлять через GitHub Issues/Discussions после включения соответствующих функций репозитория.

Приоритетная release-инженерная задача после 5.1 — сохранить совместимость старых установок и не возвращать артефакты/секреты в `main`.

## Лицензия

Проект распространяется по `FARGOVPN PERSONAL USE LICENSE`, который находится в [`LICENSE`](LICENSE). Это ограничительная лицензия для личного некоммерческого использования; она **не является стандартной OSI open-source лицензией**. Для коммерческого использования, хостинга третьих лиц или распространения требуется отдельное письменное разрешение правообладателя.
