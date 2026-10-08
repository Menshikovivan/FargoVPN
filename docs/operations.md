# Операционные команды

## Быстрая проверка исходного дерева

```bash
bash -n install.sh
find app scripts -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
python3 -m compileall -q app tests scripts
pytest -q
```

## Состояние systemd

```bash
systemctl status vpn-service-bot.service
systemctl status vpn-service-web.socket
systemctl status vpn-service-web.service
systemctl status vpn-service-backup.timer
systemctl status vpn-service-reminders.timer
```

## Логи

Основные пути, создаваемые установщиком:

```text
/var/log/vpn-service-install.log
/var/log/vpn_bot.log
/var/log/vpn-service-restore.log
```

Не публикуйте эти файлы в GitHub: они могут содержать чувствительные эксплуатационные данные.

## Обновление из консоли

Для существующей установки используйте её собственную копию установщика:

```bash
sudo /root/vpn_bot/install.sh --update-existing /root/vpn_bot
```

Если приложение установлено в другом каталоге, используйте фактический путь установки.

## Откат

Перед обновлением установщик создаёт архив `pre_update_*.tar.gz` и снимок systemd. В панели «Обновления» доступен откат к последнему валидному снимку и отдельная установка опубликованной более старой версии.
