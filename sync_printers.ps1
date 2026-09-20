param(
  [string]$HostName = "95.163.227.182",
  [string]$User = "root",
  [string]$KeyPath = "$env:USERPROFILE\\.ssh\\fullbox_root",
  [string]$RemotePath = "/opt/fullbox/available_printers.json"
)

function Get-PrinterNames {
  try {
    $list = Get-Printer | Select-Object -ExpandProperty Name
    if ($list) {
      return $list
    }
  } catch {
    # fallback below
  }
  try {
    return Get-CimInstance -ClassName Win32_Printer | Select-Object -ExpandProperty Name
  } catch {
    return @()
  }
}

if (-not (Test-Path -LiteralPath $KeyPath)) {
  Write-Error "SSH key not found: $KeyPath"
  exit 1
}

$printers = Get-PrinterNames | ForEach-Object { "$_".Trim() } | Where-Object { $_ }
$unique = @()
$seen = @{}
foreach ($printer in $printers) {
  $key = $printer.ToLowerInvariant()
  if ($seen.ContainsKey($key)) { continue }
  $seen[$key] = $true
  $unique += $printer
}

$payload = @{
  printers = $unique
  meta = @{
    updated_at = (Get-Date).ToString("s")
    updated_by = $env:USERNAME
    host = $env:COMPUTERNAME
  }
}

$tmp = Join-Path $env:TEMP "available_printers.json"
$payload | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path $tmp

& scp -i $KeyPath $tmp "${User}@${HostName}:$RemotePath"
if ($LASTEXITCODE -ne 0) {
  Write-Error "Failed to upload printers to $HostName"
  exit $LASTEXITCODE
}

Write-Host "Printers uploaded to $HostName"
