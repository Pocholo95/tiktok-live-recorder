<#
.SYNOPSIS
  Converts every pending raw recording of a TikTok Live Recorder Hub on this
  PC (GPU) and uploads the MP4 back, replacing the raw file on the server.

.DESCRIPTION
  For each recording the hub lists as "por convertir", oldest first:
    1. downloads the raw .ts (resumes a partial download),
    2. converts it with ffmpeg using the exact arguments the hub provides
       (same command the library page shows),
    3. uploads the MP4 (the hub validates it before replacing the raw),
    4. deletes the local files (unless -KeepFiles).
  A failure on one recording is logged and the next one continues; nothing
  is deleted on the server unless the hub accepted the upload.

  Needs ffmpeg in PATH and curl.exe (included in Windows 10/11).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File convert-pending.ps1
.EXAMPLE
  .\convert-pending.ps1 -Encoder av1_nvenc -Limit 3
#>
param(
    [string]$Hub = "__HUB_URL__",
    [string]$WorkDir = (Join-Path $env:USERPROFILE "TikTokConvert"),
    [ValidateSet("h264_nvenc", "av1_nvenc", "libx264")]
    [string]$Encoder = "h264_nvenc",
    [ValidateSet("720x1280", "1080x1920", "1280x720")]
    [string]$Size = "720x1280",
    [ValidateSet("high", "normal", "light")]
    [string]$Quality = "normal",
    [int]$Limit = 0,
    [switch]$KeepFiles,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Hub = $Hub.TrimEnd("/")

# Built in two pieces so the hub's placeholder substitution can't rewrite it.
$placeholder = "__HUB" + "_URL__"
if ($Hub -eq "" -or $Hub -eq $placeholder) {
    Write-Host "Falta la direccion del hub. Ejemplo: -Hub http://192.168.1.10:8000"
    exit 2
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$LogFile = Join-Path $WorkDir "convert.log"

function Log([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

foreach ($tool in @("ffmpeg", "curl.exe")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Log "ERROR: no se encontro $tool en el PATH."
        exit 2
    }
}

# Only one run at a time (a scheduled run can overlap a long manual one).
$LockFile = Join-Path $WorkDir ".lock"
if (Test-Path $LockFile) {
    $otherPid = Get-Content $LockFile -ErrorAction SilentlyContinue
    if ($otherPid -and (Get-Process -Id $otherPid -ErrorAction SilentlyContinue)) {
        Log "Ya hay otra conversion corriendo (PID $otherPid). Saliendo."
        exit 0
    }
}
Set-Content -Path $LockFile -Value $PID

# If the hub address redirects (e.g. http -> https behind a proxy), use the
# final address: uploads (PUT) must never be sent to a redirecting URL.
function Resolve-HubUrl([string]$Url) {
    # curl reports where a 3xx would go without following it.
    $target = & curl.exe -s -o NUL -w "%{redirect_url}" "$Url/api/recordings?state=raw"
    if ($LASTEXITCODE -ne 0) { throw "no se pudo conectar con el hub en $Url (curl $LASTEXITCODE)" }
    if ($target) {
        $final = ($target -replace "/api/recordings.*$", "").TrimEnd("/")
        Log "El hub redirige a $final; se usa esa direccion."
        return $final
    }
    return $Url
}

$failed = 0
$done = 0
try {
    $Hub = Resolve-HubUrl $Hub
    Log "Hub: $Hub | carpeta: $WorkDir | $Encoder $Size $Quality"

    $pending = (Invoke-RestMethod -Uri "$Hub/api/recordings?state=raw").recordings
    $convertArgs = (Invoke-RestMethod -Uri "$Hub/api/convert/args?encoder=$Encoder&size=$Size&quality=$Quality").args
    if ($Limit -gt 0) { $pending = @($pending | Select-Object -First $Limit) }

    if (-not $pending -or @($pending).Count -eq 0) {
        Log "No hay grabaciones por convertir."
        exit 0
    }
    Log ("Por convertir: {0}" -f @($pending).Count)

    foreach ($rec in $pending) {
        $id = $rec.id
        $raw = Join-Path $WorkDir $rec.file_name
        $mp4 = Join-Path $WorkDir $rec.output_name
        $tmp = Join-Path $WorkDir ($rec.output_name -replace "\.mp4$", ".converting.mp4")
        Log ("[#{0}] @{1} {2}" -f $id, $rec.username, $rec.file_name)

        if ($DryRun) { continue }

        try {
            # Someone may have uploaded it by hand since the list was fetched.
            $current = Invoke-RestMethod -Uri "$Hub/api/recordings/$id"
            if ($current.format -eq "mp4" -or $current.status -ne "completed") {
                Log "  ya no esta pendiente, se salta."
                continue
            }

            # 1. Download (resume a partial file; skip if already complete).
            $expected = $rec.size_bytes
            $haveAll = (Test-Path $raw) -and $expected -and ((Get-Item $raw).Length -eq $expected)
            if (-not $haveAll) {
                Log "  descargando..."
                & curl.exe -f -sS -L --retry 3 -C - -o $raw "$Hub/media/$id/download"
                if ($LASTEXITCODE -ne 0) { throw "la descarga fallo (curl $LASTEXITCODE)" }
                if ($expected -and (Get-Item $raw).Length -ne $expected) {
                    throw "la descarga quedo incompleta"
                }
            }

            # 2. Convert with the hub's exact arguments.
            Log "  convirtiendo..."
            $argsForThis = $convertArgs | ForEach-Object {
                $_.Replace("{input}", $raw).Replace("{output}", $tmp)
            }
            & ffmpeg -y -loglevel error -stats @argsForThis
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path $tmp)) {
                throw "ffmpeg fallo (codigo $LASTEXITCODE)"
            }
            Move-Item -Force $tmp $mp4

            # 3. Upload; the hub checks it before replacing the raw.
            Log "  subiendo..."
            $response = Join-Path $WorkDir ".upload-response.json"
            $status = & curl.exe -sS -T $mp4 -H "Content-Type: video/mp4" -o $response -w "%{http_code}" "$Hub/recordings/$id/converted"
            $code = $LASTEXITCODE
            $detail = ""
            if (Test-Path $response) { $detail = Get-Content $response -Raw; Remove-Item $response }
            if ($code -ne 0) { throw "la subida fallo (curl $code)" }
            # Only a 200 means the hub accepted and swapped it in; a redirect
            # or anything else is a failure, never a silent success.
            if ($status -ne "200") { throw "el hub respondio $status a la subida: $detail" }

            # Trust but verify before deleting anything local.
            $after = Invoke-RestMethod -Uri "$Hub/api/recordings/$id"
            if ($after.format -ne "mp4") {
                throw "el hub no muestra la grabacion como MP4 despues de subirla"
            }

            # 4. Clean up.
            if (-not $KeepFiles) {
                Remove-Item -Force $raw, $mp4 -ErrorAction SilentlyContinue
            }
            $done++
            Log "  listo."
        }
        catch {
            $failed++
            Remove-Item -Force $tmp -ErrorAction SilentlyContinue
            Log "  ERROR: $($_.Exception.Message) (los archivos locales quedan en $WorkDir)"
        }
    }
}
finally {
    Remove-Item -Force $LockFile -ErrorAction SilentlyContinue
}

Log ("Terminado: {0} convertidas, {1} con error." -f $done, $failed)
if ($failed -gt 0) { exit 1 }
