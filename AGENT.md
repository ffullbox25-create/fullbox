# Fullbox Device Agent (MVP)

Минимальный API для локального агента оборудования (Windows service + tray app).

## Настройка сервера
- Добавьте в окружение: `FULLBOX_AGENT_TOKEN=<secret>`
- Для печати из очереди используется `PRINT_AGENT_TOKEN` (existing print jobs API).
- Без токена API доступен только в `DEBUG=True`.

## Endpoints
Все запросы требуют заголовок `X-Agent-Token`.

### POST `/agent/ping/`
Тело (JSON):
```json
{
  "agent_id": "pc-001",
  "name": "Склад‑ПК‑01",
  "host": "WMS-PC01",
  "version": "0.1.0",
  "meta": {
    "printers": ["Zebra ZD420", "Microsoft Print to PDF"],
    "scanners": ["Proton IMS-2290HD_K"],
    "com": {"port": "COM3", "baud": 115200}
  }
}
```
Ответ: `{ "ok": true }`

### GET `/agent/commands/?agent_id=pc-001&limit=5`
Возвращает список команд, переводит их в статус `delivered`.
Ответ:
```json
{
  "ok": true,
  "commands": [
    {
      "id": 12,
      "command": "printer.pause",
      "payload": {"printer": "Zebra ZD420"},
      "created_at": "2026-02-02T18:40:00+03:00"
    }
  ]
}
```

### POST `/agent/commands/<id>/ack/`
Тело (JSON):
```json
{
  "agent_id": "pc-001",
  "ok": true,
  "result": {"status": "paused"},
  "error": ""
}
```
Ответ: `{ "ok": true }`

### POST `/agent/events/`
Тело (JSON):
```json
{
  "agent_id": "pc-001",
  "event_type": "scan",
  "payload": {
    "value": "0104601234567890215...",
    "source": "com",
    "port": "COM3"
  }
}
```
Ответ: `{ "ok": true }`

## Примечания
- Пока используется polling (без WebSocket). Интерфейс команд оставлен нейтральным для дальнейшего апгрейда.
- Команды с пустым `agent_id` в БД считаются общими и будут подобраны первым агентом.
