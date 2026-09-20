# Развертывание Fullbox

Актуальный рабочий контур и все дальнейшие инструкции см. в [`FULLBOX_RU_RUNBOOK.md`](./FULLBOX_RU_RUNBOOK.md).

## Коротко

- Канонический публичный домен: `https://fullbox.ru/`
- Текущий рабочий сервер: `93.123.255.241`
- SSH: `ssh user@93.123.255.241`
- Путь проекта: `/opt/fullbox`
- Сервис: `fullbox`

## Минимальный deploy-чеклист

0. Получить отдельное явное подтверждение пользователя на текущую выкладку.
   Без подтверждения не менять файлы на `/opt/fullbox`, не запускать миграции и не перезапускать сервисы.
1. Составить точный allowlist файлов для текущей задачи.
   Выкладка должна быть узкой: только конкретный ЛК, модуль или процесс, который прямо назван пользователем.
2. Проверить, что в allowlist нет файлов чужих ЛК, складских процессов и общих write-path/command-сервисов.
   Для любых `orders`, `sklad`, `shipping`, `processing*`, `reachtruck*`, `head_manager`, `agent`, `labels`, общих `fullbox/settings.py`, `fullbox/urls.py`, `todo`, `employees` нужно отдельное явное согласование.
   Перед копированием обязательно прогнать локальный guard:
   ```
   python scripts/deploy_scope_guard.py --scope client_cabinet --from-file deploy_allowlist.txt
   ```
   Вместо `client_cabinet` указать реальный контур: `teammanager`, `accountant`, `head_manager`, `receiving`, `processing`, `shipping`, `warehouse`, `reachtruck`, `logistics`, `agent_print`.
   Если в allowlist есть общий файл, guard должен запускаться только после отдельного подтверждения:
   ```
   python scripts/deploy_scope_guard.py --scope client_cabinet --allow-shared --shared-reason "добавлен URL только для клиентского ЛК" --from-file deploy_allowlist.txt
   ```
   Если используется deploy-скрипт, отдельно проверить его сервисные действия:
   ```
   python scripts/deploy_script_guard.py tmp/deploy_current_change.py
   ```
3. Подключиться на сервер:
   ```
   ssh user@93.123.255.241
   ```
4. Перейти в каталог проекта:
   ```
   cd /opt/fullbox
   ```
5. Обновить только файлы из allowlist в `/opt/fullbox`.
   Нельзя копировать весь проект, весь каталог приложения или все локальные изменения "заодно".
   Перед заменой каждого файла создать датированную резервную копию. Candidate сначала загрузить
   во временный файл рядом с target, затем выполнить атомарную замену.
6. Сразу после атомарной замены каждого файла приложения нормализовать права и проверить
   читаемость именно от имени пользователя сервиса:
   ```
   sudo chown user:user <файл>
   sudo chmod 644 <файл>
   sudo -u user test -r <файл> || { восстановить файл из резервной копии; остановиться; }
   ```
   Для приёмки зафиксировать владельца и режим каждого файла:
   ```
   stat -c '%U:%G %a %n' <файл>
   ```
   Если хотя бы один файл не читается от `user`, не запускать проверки, reload или restart:
   сначала восстановить его из резервной копии.
7. Если есть миграции:
   ```
   source /opt/fullbox/.venv/bin/activate
   python fullbox/manage.py migrate
   ```
8. Прогнать проверку:
   ```
   source /opt/fullbox/.venv/bin/activate
   python fullbox/manage.py check
   ```
9. Для обычной выкладки кода выполнить graceful reload и убедиться, что master PID не изменился:
   ```
   before_pid=$(systemctl show fullbox --property=MainPID --value)
   sudo systemctl reload fullbox
   systemctl is-active fullbox
   after_pid=$(systemctl show fullbox --property=MainPID --value)
   printf 'fullbox MainPID: before=%s after=%s\n' "$before_pid" "$after_pid"
   test "$before_pid" = "$after_pid"
   ```
   Оба PID и результат сравнения обязательно включить в отчёт о выкладке. Изменившийся PID
   означает, что произошёл restart или аварийная замена процесса, а не штатный reload.
10. Проверить внешний контур:
   ```
   curl -I https://fullbox.ru/
   curl -I https://www.fullbox.ru/
   ```
11. Проверить адресные URL/сценарии именно затронутого ЛК.
12. Заполнить [`deploy-report.md`](./deploy-report.md): allowlist, backup, права каждого файла,
    способ применения (`reload` или `restart`), PID до/после, причины исключений и результаты проверок.

## Важно

- На продакшен не заливаем без отдельного явного подтверждения пользователя прямо перед выкладкой.
- Широкий deploy запрещен: выкладывать только заранее перечисленные файлы текущей задачи.
- Локальные изменения вне текущей задачи не выкладывать и не смешивать с текущим исправлением.
- Старый доменный контур считать legacy
- В новых deploy-инструкциях и проверках использовать только `fullbox.ru`
- На сервере `/opt/fullbox` пока нет `.git`, поэтому текущий deploy файловый, а не `git pull`
- Старые одноразовые deploy-скрипты нельзя запускать повторно без `deploy_scope_guard.py` и `deploy_script_guard.py`.
- Плановые выкладки выполнять до 06:00 или после 20:00 по времени сервера.
- В интервале 06:00–09:00 допустимо только исправление уже сломанного production. В отчёте
  такую выкладку явно пометить как аварийную и описать исходный инцидент.
- Полный `restart` не применяется для обычной выкладки кода: он обрывает активные запросы и кратковременно дает `502`.
- `restart` допустим только когда reload технически недостаточен, например после изменения `.env`, systemd unit или runtime-зависимостей. Для него нужны отдельное подтверждение и явная причина в `deploy_script_guard.py --allow-restart --restart-reason "..."`.
- В каждом отчёте явно указать `reload` или `restart`. Для `restart` обязательно записать
  техническую причину словами и ссылку на отдельное подтверждение пользователя.
