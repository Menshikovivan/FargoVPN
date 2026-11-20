# Профили маршрутизации FargoVPN

Профиль для Happ и Incy собирается из [RU-Speede-VPN](https://github.com/SpeedeMiner/RU-Speede-VPN) и базы [GeoSite RU Light](https://github.com/SpeedeMiner/Geosite-ru-light). При каждой синхронизации из категории BLOCK исключается домен MapKit. Он продолжает совпадать с категорией RU и идёт напрямую.

- [Профиль для подписок Happ и Incy](https://raw.githubusercontent.com/Menshikovivan/FargoVPN/main/routing/public/profile.json)
- [Ссылка для ручного импорта тестового профиля в Happ](https://raw.githubusercontent.com/Menshikovivan/FargoVPN/main/routing/public/navigator-test-happ.txt)
- [JSON тестового профиля](https://raw.githubusercontent.com/Menshikovivan/FargoVPN/main/routing/public/navigator-test.json)

GitHub Actions проверяет источник каждые шесть часов. Если источник недоступен или исчезла необходимая категория, сборка завершается до публикации и подписка продолжает получать предыдущий профиль. Имена файлов GeoSite содержат хеш базы, поэтому выпущенный профиль ссылается на конкретную версию.

Для ручной проверки запустите `node routing/sync.mjs` из корня репозитория.
