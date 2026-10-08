# PostgreSQL и миграция

FargoVPN использует PostgreSQL как основную рабочую БД. Установщик подготавливает локальный PostgreSQL и выполняет необходимые миграции через `init_db.py`.

Для отдельной подготовки PostgreSQL на Debian/Ubuntu:

```bash
sudo FARGOVPN_DB_NAME=fargovpn \
     FARGOVPN_DB_USER=fargovpn \
     FARGOVPN_DB_HOST=127.0.0.1 \
     FARGOVPN_DB_PORT=5432 \
     FARGOVPN_DB_PASSWORD='CHANGE_ME' \
     bash scripts/setup_postgresql.sh
```

Скрипт печатает итоговый `DATABASE_URL`. Пароль в истории shell и в публичных файлах не сохраняйте.

При обновлении существующей установки установщик делает резервную копию и сохраняет действующий `config.py`. Историческая SQLite-база `data/vpn_bot.db`, если она присутствует у старой установки, используется только как источник миграции в соответствии с текущей логикой установщика.
