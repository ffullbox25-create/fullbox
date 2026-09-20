# Legacy Server Re-Enable

## Что сделано

Старый сервер `95.163.227.182` переведен в выключенный, но обратимый режим.

Отключено:

- `fullbox` service: `inactive`, `disabled`
- `nginx`: `inactive`, `disabled`

Оставлено включенным:

- `postgresql`: `active`, `enabled`
- SSH-доступ на сервер

Это означает:

- `kondelyabr.ru` и `www.kondelyabr.ru` больше не обслуживаются старым сервером
- вернуть старый контур можно без восстановления из backup, обычным запуском сервисов
- база на старом сервере сохранена

## Снимок состояния перед отключением

На старом сервере сохранен файл:

- `/root/fullbox_legacy_shutdown/state_before_20260408_1943*.txt`

В нем лежат статусы сервисов и слушающие порты до отключения.

## Как снова включить старый сервер

Подключение:

```bash
ssh -i ~/.ssh/fullbox_root root@95.163.227.182
```

Или по паролю `root`, если ключ недоступен.

Команды включения:

```bash
systemctl enable --now nginx
systemctl enable --now fullbox
systemctl status nginx --no-pager
systemctl status fullbox --no-pager
```

Проверка на самом сервере:

```bash
curl -k -I https://127.0.0.1/
curl -k -I https://127.0.0.1/login/
ss -ltnp | grep -E ':80 |:443 |:8000 '
```

Проверка снаружи:

```bash
curl -k -I https://kondelyabr.ru/
curl -k -I https://www.kondelyabr.ru/
```

## Как снова выключить старый сервер

```bash
systemctl disable --now fullbox
systemctl disable --now nginx
systemctl is-active nginx postgresql fullbox
systemctl is-enabled nginx postgresql fullbox
```

Ожидаемое состояние после повторного выключения:

- `nginx`: `inactive`, `disabled`
- `fullbox`: `inactive`, `disabled`
- `postgresql`: `active`, `enabled`

## Важные замечания

- Старый сервер сейчас считается только legacy/fallback-контуром
- Основной боевой контур: `https://fullbox.ru/` на сервере `93.123.255.241`
- Если старый сервер включать повторно, это не меняет DNS и не влияет на `fullbox.ru`
- Если нужно будет вернуть старый домен в работу, достаточно поднять `nginx` и `fullbox`, пока DNS `kondelyabr.ru` все еще указывает на `95.163.227.182`
