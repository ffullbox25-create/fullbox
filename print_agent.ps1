param(
  [string]$ServerUrl = "https://kondelyabr.ru",
  [string]$Token = "",
  [int]$PollSeconds = 3,
  [string]$AgentName = $env:COMPUTERNAME
)

if (-not $Token) {
  Write-Error "PRINT_AGENT_TOKEN is required. Run with -Token <value>."
  exit 1
}

$nextUrl = "$ServerUrl/orders/processing/print-jobs/next/?token=$Token&agent=$AgentName"
$completeUrl = "$ServerUrl/orders/processing/print-jobs/complete/?token=$Token"

Add-Type -AssemblyName System.Drawing

function Get-NextJob {
  try {
    return Invoke-RestMethod -Method Get -Uri $nextUrl -TimeoutSec 20
  } catch {
    Write-Warning "Failed to fetch print job: $($_.Exception.Message)"
    return $null
  }
}

function Send-JobStatus {
  param(
    [int]$JobId,
    [string]$Status,
    [string]$Error = ""
  )
  $payload = @{
    job_id = $JobId
    status = $Status
    error = $Error
  } | ConvertTo-Json
  try {
    Invoke-RestMethod -Method Post -Uri $completeUrl -ContentType "application/json" -Body $payload -TimeoutSec 20 | Out-Null
  } catch {
    Write-Warning "Failed to report job status: $($_.Exception.Message)"
  }
}

function Print-Label {
  param(
    [object]$Job
  )

  $printerName = [string]$Job.printer_name
  if (-not $printerName) {
    return @{ ok = $false; error = "Printer name is empty." }
  }

  $printerExists = $false
  if (Get-Command Get-Printer -ErrorAction SilentlyContinue) {
    try {
      Get-Printer -Name $printerName -ErrorAction Stop | Out-Null
      $printerExists = $true
    } catch {
      $printerExists = $false
    }
  } else {
    try {
      $wmi = Get-WmiObject -Class Win32_Printer -Filter ("Name='{0}'" -f $printerName.Replace("'", "''"))
      if ($wmi) { $printerExists = $true }
    } catch {
      $printerExists = $false
    }
  }
  if (-not $printerExists) {
    return @{ ok = $false; error = "Printer not found: $printerName" }
  }

  $base64 = [string]$Job.label_png_base64
  if (-not $base64) {
    return @{ ok = $false; error = "Label image is empty." }
  }

  $widthMm = [int]$Job.label_width_mm
  $heightMm = [int]$Job.label_height_mm
  if (-not $widthMm) { $widthMm = 58 }
  if (-not $heightMm) { $heightMm = 40 }

  $tmpPath = [System.IO.Path]::GetTempFileName()
  $pngPath = [System.IO.Path]::ChangeExtension($tmpPath, "png")
  Move-Item -Path $tmpPath -Destination $pngPath -Force

  try {
    $bytes = [Convert]::FromBase64String($base64)
    [System.IO.File]::WriteAllBytes($pngPath, $bytes)
  } catch {
    Remove-Item -Path $pngPath -Force -ErrorAction SilentlyContinue
    return @{ ok = $false; error = "Failed to decode label image." }
  }

  try {
    $img = [System.Drawing.Image]::FromFile($pngPath)
    $printDoc = New-Object System.Drawing.Printing.PrintDocument
    $printDoc.PrinterSettings.PrinterName = $printerName
    $imgDpiX = [Math]::Round([double]$img.HorizontalResolution, 2)
    $imgDpiY = [Math]::Round([double]$img.VerticalResolution, 2)
    Write-Host ("Image pixels: {0}x{1}px (DPI {2}x{3})" -f $img.Width, $img.Height, $imgDpiX, $imgDpiY)

    $isLandscape = $widthMm -gt $heightMm
    $paperWidthMm = if ($isLandscape) { $heightMm } else { $widthMm }
    $paperHeightMm = if ($isLandscape) { $widthMm } else { $heightMm }
    $widthHundredths = [int][Math]::Round(($paperWidthMm / 25.4) * 100)
    $heightHundredths = [int][Math]::Round(($paperHeightMm / 25.4) * 100)
    $paperSize = New-Object System.Drawing.Printing.PaperSize("Label", $widthHundredths, $heightHundredths)
    $printDoc.DefaultPageSettings.PaperSize = $paperSize
    $printDoc.DefaultPageSettings.Landscape = $isLandscape
    $printDoc.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)
    $selectedResolution = $null
    try {
      $resolutions = $printDoc.PrinterSettings.PrinterResolutions
      if ($resolutions -and $resolutions.Count -gt 0) {
        $best = $resolutions | Sort-Object -Property X,Y -Descending | Select-Object -First 1
        if ($best) {
          $printDoc.DefaultPageSettings.PrinterResolution = $best
          $selectedResolution = $best
        }
      }
    } catch {
    }
    if ($selectedResolution) {
      Write-Host ("Printer resolution: {0} {1}x{2} DPI" -f $selectedResolution.Kind, $selectedResolution.X, $selectedResolution.Y)
    } else {
      Write-Host "Printer resolution: default"
    }
    $activeResolution = $printDoc.DefaultPageSettings.PrinterResolution
    if ($activeResolution -and $activeResolution.X -gt 0 -and $activeResolution.Y -gt 0) {
      $targetPxW = [int][Math]::Round(($widthMm / 25.4) * $activeResolution.X)
      $targetPxH = [int][Math]::Round(($heightMm / 25.4) * $activeResolution.Y)
      Write-Host ("Target size: {0}x{1}px @ {2}x{3} DPI" -f $targetPxW, $targetPxH, $activeResolution.X, $activeResolution.Y)
    } else {
      Write-Host "Target size: unknown (printer resolution not reported)"
    }

    $printDoc.add_PrintPage({
      param($sender, $e)
      $e.Graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::None
      $e.Graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
      $e.Graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
      $e.Graphics.PageUnit = [System.Drawing.GraphicsUnit]::Display
      $pageIsLandscape = $e.PageBounds.Width -gt $e.PageBounds.Height
      if ($pageIsLandscape -eq $isLandscape) {
        $e.Graphics.DrawImage($img, 0, 0, $e.PageBounds.Width, $e.PageBounds.Height)
      } else {
        if ($isLandscape) {
          $e.Graphics.TranslateTransform($e.PageBounds.Width, 0)
          $e.Graphics.RotateTransform(90)
        } else {
          $e.Graphics.TranslateTransform(0, $e.PageBounds.Height)
          $e.Graphics.RotateTransform(-90)
        }
        $e.Graphics.DrawImage($img, 0, 0, $e.PageBounds.Height, $e.PageBounds.Width)
      }
      $e.HasMorePages = $false
    })

    $printDoc.Print()
    $printDoc.Dispose()
    $img.Dispose()
    return @{ ok = $true; error = "" }
  } catch {
    return @{ ok = $false; error = "Print failed: $($_.Exception.Message)" }
  } finally {
    Remove-Item -Path $pngPath -Force -ErrorAction SilentlyContinue
  }
}

Write-Host "Print agent started. Agent=$AgentName"

while ($true) {
  $response = Get-NextJob
  if (-not $response -or -not $response.ok) {
    Start-Sleep -Seconds $PollSeconds
    continue
  }
  if (-not $response.has_job) {
    Start-Sleep -Seconds $PollSeconds
    continue
  }
  $job = $response.job
  if (-not $job) {
    Start-Sleep -Seconds $PollSeconds
    continue
  }
  Write-Host "Printing job $($job.id) for printer '$($job.printer_name)'..."
  $result = Print-Label -Job $job
  if ($result.ok) {
    Write-Host "Job $($job.id) printed."
    Send-JobStatus -JobId $job.id -Status "printed"
  } else {
    Write-Warning "Job $($job.id) failed: $($result.error)"
    Send-JobStatus -JobId $job.id -Status "failed" -Error $result.error
  }
  Start-Sleep -Seconds 1
}
