param(
    [string]$OutputRoot = "build\windows"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputRoot = Join-Path $root $OutputRoot
$ffmpeg = Get-Command ffmpeg.exe -ErrorAction Stop
$ffprobe = Get-Command ffprobe.exe -ErrorAction Stop
$cloudflared = Get-Command cloudflared.exe -ErrorAction SilentlyContinue

if (-not $cloudflared) {
    $candidate = Join-Path ${env:ProgramFiles(x86)} "cloudflared\cloudflared.exe"
    if (Test-Path -LiteralPath $candidate) {
        $cloudflared = [PSCustomObject]@{ Source = $candidate }
    } else {
        throw "cloudflared.exe est requis pour construire l'application. Installez-le sur le PC de build puis relancez."
    }
}

$ffmpegDir = Split-Path -Parent $ffmpeg.Source
$ffprobeDir = Split-Path -Parent $ffprobe.Source
if ($ffmpegDir -ne $ffprobeDir) {
    throw "ffmpeg.exe et ffprobe.exe doivent provenir du même dossier."
}

$ffmpegRoot = Split-Path -Parent $ffmpegDir
$licenseDir = Join-Path $root "build\windows\licenses"
New-Item -ItemType Directory -Force -Path (Join-Path $licenseDir "ffmpeg") | Out-Null

$ffmpegLicense = Get-ChildItem -LiteralPath $ffmpegRoot -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^LICENSE(?:\.txt)?$|^COPYING(?:\.txt)?$' } |
    Select-Object -First 1 -ExpandProperty FullName
$ffmpegReadme = Get-ChildItem -LiteralPath $ffmpegRoot -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^README(?:\.txt|\.md)?$' } |
    Select-Object -First 1 -ExpandProperty FullName

if (-not $ffmpegLicense) {
    $ffmpegLicense = Join-Path $licenseDir "ffmpeg\LICENSE"
    Invoke-WebRequest -Uri "https://www.gnu.org/licenses/gpl-3.0.txt" -OutFile $ffmpegLicense
}
if (-not $ffmpegReadme) {
    $ffmpegReadme = Join-Path $licenseDir "ffmpeg\README.txt"
    @(
        "FFmpeg"
        "Composant intégré par Blind test."
        "Source officielle : https://ffmpeg.org/"
        "Licence GPL v3 pour cette distribution de Gyan."
    ) | Set-Content -LiteralPath $ffmpegReadme -Encoding ascii
}
$encoders = (& $ffmpeg.Source -hide_banner -encoders 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0 -or $encoders -notmatch '\blibmp3lame\b') {
    throw "Le build FFmpeg doit inclure l'encodeur libmp3lame utilisé pour créer les extraits audio."
}
$filters = (& $ffmpeg.Source -hide_banner -filters 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0 -or $filters -notmatch '\bloudnorm\b') {
    throw "Le build FFmpeg doit inclure le filtre loudnorm utilisé pour normaliser le son."
}

$cloudVersionLine = (& $cloudflared.Source --version 2>&1 | Select-Object -First 1).ToString()
if ($cloudVersionLine -notmatch 'version\s+([0-9][0-9.]+)') {
    throw "Impossible de déterminer la version de cloudflared : $cloudVersionLine"
}
$cloudVersion = $Matches[1]
$licenseDir = Join-Path $root "build\windows\licenses"
New-Item -ItemType Directory -Force -Path $licenseDir | Out-Null
$cloudLicense = Join-Path $licenseDir "cloudflared-LICENSE"
$cloudLicenseUrl = "https://raw.githubusercontent.com/cloudflare/cloudflared/$cloudVersion/LICENSE"
Invoke-WebRequest -Uri $cloudLicenseUrl -OutFile $cloudLicense

$python = Get-Command python.exe -ErrorAction Stop
& $python.Source -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller n'est pas installé. Lancez : python -m pip install -r requirements-build.txt"
}

$arguments = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", "Blind test",
    "--contents-directory", "_internal",
    "--distpath", (Join-Path $outputRoot "dist"),
    "--workpath", (Join-Path $outputRoot "work"),
    "--specpath", $outputRoot,
    "--add-data", "$root\jeu;jeu",
    "--add-binary", "$($ffmpeg.Source);.",
    "--add-binary", "$($ffprobe.Source);.",
    "--add-binary", "$($cloudflared.Source);.",
    "--add-data", "$ffmpegLicense;licenses\ffmpeg",
    "--add-data", "$ffmpegReadme;licenses\ffmpeg",
    "--add-data", "$cloudLicense;licenses\cloudflared",
    "--add-data", "$root\THIRD_PARTY_NOTICES.txt;licenses"
)

foreach ($dll in Get-ChildItem -LiteralPath $ffmpegDir -Filter "*.dll" -File) {
    $arguments += @("--add-binary", "$($dll.FullName);.")
}

$arguments += (Join-Path $root "jeu.py")
& $python.Source -m PyInstaller @arguments
if ($LASTEXITCODE -ne 0) {
    throw "La création de l'application a échoué."
}

$appDir = Join-Path $outputRoot "dist\Blind test"
if (-not (Test-Path -LiteralPath (Join-Path $appDir "Blind test.exe"))) {
    throw "Blind test.exe n'a pas été créé dans $appDir."
}

$isccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

if (-not $isccCandidates) {
    Write-Host "Blind test.exe est prêt dans : $appDir"
    Write-Warning "Inno Setup 6 n'est pas installé ; l'installateur EXE n'a pas été compilé."
    exit 0
}

$installerDir = Join-Path $outputRoot "installer"
New-Item -ItemType Directory -Force -Path $installerDir | Out-Null
$iscc = $isccCandidates | Select-Object -First 1
& $iscc "/DAppSourceDir=$appDir" "/DAppOutputDir=$installerDir" (Join-Path $root "installer.iss")
if ($LASTEXITCODE -ne 0) {
    throw "La création de l'installateur a échoué."
}

Write-Host "Installateur prêt dans : $(Join-Path $installerDir 'Blind-test-v2-FR-Setup.exe')"
