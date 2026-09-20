# Fullbox Agent (Windows)

MVP local agent: tray app only. The tray hosts scanner, printer, realtime, and local bridge loops in the user session.

## Requirements
- Windows 11
- .NET 8 SDK (for build)

## Build
```powershell
.\build.ps1
```

## Tray app
```powershell
C:\path\to\fullbox_agent\out\tray\Fullbox.Agent.Tray.exe
```

## Config
Config file is stored at:
```
C:\ProgramData\FullboxAgent\config.json
```

Minimal fields:
```json
{
  "agentId": "pc-001",
  "name": "Warehouse-PC-01",
  "host": "WMS-PC01",
  "baseUrl": "https://fullbox.ru",
  "token": "CHANGE_ME",
  "printToken": "PRINT_AGENT_TOKEN",
  "printAgentName": "FullboxAgent",
  "pingIntervalSec": 10,
  "pollIntervalSec": 5,
  "printPollIntervalSec": 2,
  "realtimeEnabled": false,
  "commandFallbackPollIntervalSec": 30,
  "printFallbackPollIntervalSec": 60,
  "com": {
    "enabled": true,
    "portName": "COM3",
    "baudRate": 9600,
    "eol": "CrLf",
    "idleMs": 200
  }
}
```

## Notes
- Print jobs are claimed by `agentId`; `printAgentName` is kept as a display/legacy config field.
- The setup disables the old `FullboxAgent` Windows service when present and installs tray autostart only.
- Uses WebSocket (`/ws/agent/`) for command/print wakeups and scan events when `realtimeEnabled` is enabled.
- Keeps HTTP polling/endpoints as a fallback (`/agent/commands/`, `/agent/events/`, `/orders/processing/print-jobs/next/`).
- Safe server rollout: keep `realtimeEnabled=false` by default, then enable one test PC together with `FULLBOX_REALTIME_ENABLED=true` and `FULLBOX_REALTIME_AGENT_IDS=pc-test`.
