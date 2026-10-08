# Участие в разработке FargoVPN

Спасибо за вклад.

## Перед Pull Request

1. Создайте отдельную ветку от `main`.
2. Не добавляйте реальные `config.py`, `.env`, базы, логи, архивы или ключи.
3. Запустите локальные проверки:

```bash
bash -n install.sh
find app scripts -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
python3 -m compileall -q app tests scripts
pytest -q
```

4. Для изменений установщика отдельно проверьте сценарии новой установки, обновления и отката на тестовой машине.

## Коммиты

Используйте Conventional Commits, например:

```text
chore: clean repository structure
docs: rewrite README
fix: harden github release publication
feat: add panel update verification
release: 5.1.4
```

## Pull Request

Опишите причину изменения, затронутые компоненты и результаты проверок. Для изменений обновления приложите сценарий обновления с предыдущей версии.
