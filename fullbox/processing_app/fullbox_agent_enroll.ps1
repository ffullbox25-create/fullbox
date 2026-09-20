[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Set-JsonProperty {
    param(
        [Parameter(Mandatory = $true)] [object] $Object,
        [Parameter(Mandatory = $true)] [string] $Name,
        [Parameter(Mandatory = $true)] [AllowNull()] $Value
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
    else {
        $property.Value = $Value
    }
}

function Get-JsonPropertyValue {
    param(
        [Parameter(Mandatory = $true)] [object] $Object,
        [Parameter(Mandatory = $true)] [string] $Name
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Write-JsonAtomically {
    param(
        [Parameter(Mandatory = $true)] [object] $Object,
        [Parameter(Mandatory = $true)] [string] $Path
    )

    $tempPath = "$Path.enrollment.tmp"
    $json = $Object | ConvertTo-Json -Depth 32
    $utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
    try {
        [IO.File]::WriteAllText($tempPath, $json, $utf8)
        [IO.File]::Replace($tempPath, $Path, $null, $true)
    }
    finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-FullboxAgent {
    Get-Process -Name 'Fullbox.Agent.Tray' -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue

    $service = Get-Service -Name 'FullboxAgent' -ErrorAction SilentlyContinue
    if ($null -ne $service -and $service.Status -ne 'Stopped') {
        Stop-Service -Name 'FullboxAgent' -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 700
}

function Start-FullboxAgent {
    param([Parameter(Mandatory = $true)] [string] $TrayPath)

    $service = Get-Service -Name 'FullboxAgent' -ErrorAction SilentlyContinue
    if ($null -ne $service -and $service.StartType -ne 'Disabled') {
        Start-Service -Name 'FullboxAgent' -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $TrayPath) {
        Start-Process -FilePath $TrayPath
    }
}

if (-not (Test-IsAdministrator)) {
    if (-not $PSCommandPath) {
        throw 'Запустите мастер привязки от имени администратора.'
    }
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $PSCommandPath)
    )
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments
    exit 0
}

$baseUrl = 'https://lk.fullbox.ru'
$agentRoot = Join-Path $env:ProgramData 'FullboxAgent'
$configPath = Join-Path $agentRoot 'config.json'
$trayPath = Join-Path $agentRoot 'bin\Fullbox.Agent.Tray.exe'

if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'Конфигурация Fullbox Agent не найдена. Сначала установите агент со страницы Fullbox.'
}
if (-not (Test-Path -LiteralPath $trayPath)) {
    throw 'Fullbox Agent не найден. Сначала установите актуальную версию агента.'
}

Write-Host ''
Write-Host 'Привязка Fullbox Agent к этому компьютеру' -ForegroundColor Cyan
Write-Host 'Код одноразовый. Токен устройства на экран не выводится.'
Write-Host ''

$enrollmentCode = (Read-Host 'Введите код активации со страницы Fullbox').Trim()
if ([string]::IsNullOrWhiteSpace($enrollmentCode) -or $enrollmentCode.Length -gt 128) {
    throw 'Код активации пустой или имеет неверный формат.'
}

try {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw 'Не удалось прочитать config.json. Переустановите агент и повторите привязку.'
}

$agentId = [string](Get-JsonPropertyValue -Object $config -Name 'agentId')
if ([string]::IsNullOrWhiteSpace($agentId)) {
    $agentId = 'pc-' + ([Guid]::NewGuid().ToString('N').Substring(0, 8))
}
if ($agentId -notmatch '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$') {
    throw 'В конфигурации найден недопустимый ID агента. Переустановите агент.'
}

$agentName = [string](Get-JsonPropertyValue -Object $config -Name 'name')
if ([string]::IsNullOrWhiteSpace($agentName)) {
    $agentName = $env:COMPUTERNAME
}
$agentVersion = (Get-Item -LiteralPath $trayPath).VersionInfo.ProductVersion
if ([string]::IsNullOrWhiteSpace($agentVersion)) {
    $agentVersion = 'unknown'
}

$exchangeBody = @{
    enrollment_code = $enrollmentCode
    agent_id = $agentId
    name = $agentName
    host = $env:COMPUTERNAME
    version = $agentVersion
} | ConvertTo-Json -Depth 4

Write-Host 'Проверяем одноразовый код...'
try {
    $exchange = Invoke-RestMethod `
        -Uri "$baseUrl/agent/enroll/" `
        -Method Post `
        -ContentType 'application/json; charset=utf-8' `
        -Body $exchangeBody `
        -TimeoutSec 30
}
catch {
    throw ('Код не принят сервером: {0}. Создайте новый код и повторите.' -f $_.Exception.Message)
}

$deviceToken = [string]$exchange.device_token
if (-not $exchange.ok -or [string]::IsNullOrWhiteSpace($deviceToken) -or $deviceToken.Length -lt 32) {
    throw 'Сервер не выдал токен устройства. Создайте новый код и повторите.'
}
if ([string]$exchange.agent_id -ne $agentId) {
    throw 'Сервер вернул другой ID агента. Конфигурация не изменена.'
}

$agentHeaders = @{
    'X-Agent-Token' = $deviceToken
    'X-Agent-ID' = $agentId
}
$printHeaders = @{
    'X-Print-Token' = $deviceToken
    'X-Print-Agent' = $agentId
}
$pingBody = @{
    agent_id = $agentId
    name = $agentName
    host = $env:COMPUTERNAME
    version = $agentVersion
    meta = @{ enrollment_verified = $true }
} | ConvertTo-Json -Depth 5

Write-Host 'Проверяем каналы сканирования и печати...'
try {
    $ping = Invoke-RestMethod `
        -Uri "$baseUrl/agent/ping/" `
        -Method Post `
        -Headers $agentHeaders `
        -ContentType 'application/json; charset=utf-8' `
        -Body $pingBody `
        -TimeoutSec 30
    $printCheck = Invoke-RestMethod `
        -Uri "$baseUrl/orders/processing/print-agent/verify/" `
        -Method Get `
        -Headers $printHeaders `
        -TimeoutSec 30
}
catch {
    $deviceToken = $null
    throw ('Сервер не подтвердил новый токен: {0}. Создайте новый код и повторите.' -f $_.Exception.Message)
}
if (-not $ping.ok -or -not $printCheck.ok -or [string]$printCheck.token_type -ne 'device') {
    $deviceToken = $null
    throw 'Проверка каналов сканирования и печати не пройдена. Конфигурация не изменена.'
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = Join-Path $agentRoot "config.before-enrollment-$stamp.json"
Copy-Item -LiteralPath $configPath -Destination $backupPath -Force

try {
    Set-JsonProperty -Object $config -Name 'agentId' -Value $agentId
    Set-JsonProperty -Object $config -Name 'baseUrl' -Value $baseUrl
    Set-JsonProperty -Object $config -Name 'token' -Value $deviceToken
    Set-JsonProperty -Object $config -Name 'printToken' -Value $deviceToken

    Stop-FullboxAgent
    Write-JsonAtomically -Object $config -Path $configPath
    Start-FullboxAgent -TrayPath $trayPath
}
catch {
    $failure = $_.Exception.Message
    try {
        Stop-FullboxAgent
        Copy-Item -LiteralPath $backupPath -Destination $configPath -Force
        Start-FullboxAgent -TrayPath $trayPath
    }
    catch {
        Write-Warning 'Автоматическое восстановление не удалось. Верните резервную копию config.json вручную.'
    }
    $deviceToken = $null
    throw ('Не удалось сохранить привязку: {0}' -f $failure)
}

$deviceToken = $null
$enrollmentCode = $null
Write-Host ''
Write-Host 'Готово: компьютер привязан индивидуальным токеном.' -ForegroundColor Green
Write-Host ('ID агента: {0}' -f $agentId)
Write-Host ('Резервная копия: {0}' -f $backupPath)
Write-Host 'Проверьте зелёный значок Fullbox Agent и выполните одну тестовую печать.'
Write-Host ''
Read-Host 'Нажмите Enter, чтобы закрыть окно' | Out-Null
