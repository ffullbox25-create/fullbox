$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$trayProj = Join-Path $root "src\\Fullbox.Agent.Tray\\Fullbox.Agent.Tray.csproj"
$setupProj = Join-Path $root "src\\Fullbox.Agent.Setup\\Fullbox.Agent.Setup.csproj"
$outDir = Join-Path $root "out"
$trayOut = Join-Path $outDir "tray"
$setupOut = Join-Path $outDir "setup"
$distDir = Join-Path $root "dist"
$bundlePath = Join-Path $root "fullbox_agent_bundle.zip"

New-Item -ItemType Directory -Path $trayOut -Force | Out-Null
New-Item -ItemType Directory -Path $setupOut -Force | Out-Null
New-Item -ItemType Directory -Path $distDir -Force | Out-Null

dotnet publish $trayProj -c Release -r win-x64 -p:PublishSingleFile=true -p:SelfContained=true -o $trayOut

Copy-Item -Path (Join-Path $trayOut "*") -Destination $distDir -Recurse -Force

$configPath = Join-Path $distDir "config.json"
if (-not (Test-Path $configPath)) {
  $configPath = Join-Path $distDir "config.sample.json"
}

$runtimeSources = Get-ChildItem -LiteralPath $distDir -File | Where-Object {
  ($_.Name -eq "Fullbox.Agent.Tray.exe" `
    -or $_.Extension -eq ".dll" `
    -or $_.Name.EndsWith(".runtimeconfig.json", [System.StringComparison]::OrdinalIgnoreCase)) `
    -and -not $_.Name.StartsWith("Fullbox.Agent.Service.", [System.StringComparison]::OrdinalIgnoreCase)
}

$bundleSources = @(
  $configPath,
  (Join-Path $distDir "install_agent.cmd"),
  (Join-Path $distDir "README.txt")
) + $runtimeSources.FullName

if (Test-Path $bundlePath) {
  Remove-Item $bundlePath -Force
}
Compress-Archive -Path $bundleSources -DestinationPath $bundlePath

dotnet publish $setupProj -c Release -r win-x64 -p:PublishSingleFile=true -p:SelfContained=true -o $setupOut

Write-Host "Build complete:"
Write-Host "  Tray    -> $trayOut"
Write-Host "  Setup   -> $setupOut"
Write-Host "  Bundle  -> $bundlePath"
