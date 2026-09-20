# Fullbox.ru Runbook

## Канонический контур

- Основной публичный адрес проекта: `https://fullbox.ru/`
- Дополнительный адрес: `https://www.fullbox.ru/`
- Канонический бренд и домен для всей дальнейшей работы: `fullbox` / `fullbox.ru`
- Старый доменный контур считать legacy. Не использовать его в новых настройках, ссылках, проверках, инструкциях, nginx-конфигах, сертификатах и документации.

## Текущий рабочий сервер

- Сервер: `93.123.255.241`
- SSH: `ssh user@93.123.255.241`
- Пользователь: `user`
- Путь проекта: `/opt/fullbox`
- Виртуальное окружение: `/opt/fullbox/.venv`
- Сервис приложения: `fullbox`
- Nginx site: `/etc/nginx/sites-available/fullbox_mirror`
- Публичный TLS-сертификат: Let's Encrypt для `fullbox.ru`, `www.fullbox.ru`

## Legacy-контур

- Старый сервер `95.163.227.182` не использовать как основной рабочий контур.
- Любые обращения к старому серверу делать только если задача прямо требует сверки, миграции данных или отключения legacy.
- При написании новых инструкций, чеклистов и заметок не ссылаться на старый домен.
- Обратимое включение/выключение старого сервера описано в `LEGACY_SERVER_REENABLE.md`.

## Что проверять перед любой работой

1. Убедиться, что локально понятно, какие файлы реально меняются: `git status --short`
2. Проверить, не затрагивает ли задача домен, nginx, `.env`, сертификаты или DNS.
3. Если изменения пойдут на сервер `93.123.255.241`, помнить, что `/opt/fullbox` сейчас runtime-копия без `.git`.
4. Если меняются инфраструктурные файлы, сначала делать backup на сервере.
5. После любых правок обновлять `journal.md` и `PROJECT_CONTEXT.md`.

## Базовый SSH-поток работы

```bash
ssh user@93.123.255.241
cd /opt/fullbox
source .venv/bin/activate
python fullbox/manage.py check
```

Полезные команды:

```bash
sudo systemctl status fullbox
sudo systemctl status nginx
sudo systemctl status postgresql
sudo nginx -t
curl -I http://127.0.0.1/
curl -I https://fullbox.ru/
```

## Порядок деплоя кода

Так как на сервере нет `.git`, рабочий deploy сейчас файловый.

1. Локально определить точный список файлов на выкладку.
2. Скопировать их на `93.123.255.241` в `/opt/fullbox`.
3. Если есть миграции, выполнить:

```bash
cd /opt/fullbox
source .venv/bin/activate
python fullbox/manage.py migrate
```

4. Прогнать проверку:

```bash
cd /opt/fullbox
source .venv/bin/activate
python fullbox/manage.py check
```

5. Перезапустить приложение:

```bash
sudo systemctl restart fullbox
```

6. Проверить внешний контур:

```bash
curl -I https://fullbox.ru/
curl -I https://www.fullbox.ru/
```

## Что считать обязательной проверкой после деплоя

- `https://fullbox.ru/` отвечает `200` или ожидаемым redirect
- `/login/` открывается через `https://fullbox.ru/login/`
- `python fullbox/manage.py check` проходит без ошибок
- `systemctl status fullbox` показывает `active`
- `sudo nginx -t` проходит, если трогали nginx

## Правила для домена и TLS

- Основной публичный домен: `fullbox.ru`
- Сертификат хранится в `/etc/letsencrypt/live/fullbox.ru/`
- Nginx уже настроен на redirect `http -> https` для `fullbox.ru` и `www.fullbox.ru`
- Проверка сертификата:

```bash
sudo certbot certificates
openssl x509 -in /etc/letsencrypt/live/fullbox.ru/fullchain.pem -noout -subject -issuer -dates
```

- Ручное продление обычно не нужно, но тест renewal можно делать так:

```bash
sudo certbot renew --dry-run
```

## Если задача касается хостов и CSRF

На сервере проверять:

- `/opt/fullbox/.env`
- `DJANGO_ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`

Для новых публичных hostname сначала обновлять `.env`, затем перезапускать `fullbox`, и только потом проверять внешний запрос по `Host`.

## Если задача касается nginx

Рабочий файл:

- `/etc/nginx/sites-available/fullbox_mirror`

Безопасный порядок:

1. Сделать backup файла
2. Отредактировать конфиг
3. Выполнить `sudo nginx -t`
4. Выполнить `sudo systemctl reload nginx`
5. Проверить `curl -I https://fullbox.ru/`

## Рекомендации на ближайший цикл

- Перевести `/opt/fullbox` на нормальный git checkout, чтобы уйти от ручной runtime-копии
- Синхронизировать новый сервер с текущим локальным деревом, потому что сервер отстает от локальных файлов
- Отдельно спланировать отключение или архивирование legacy-контура старого домена
