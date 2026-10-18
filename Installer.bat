@echo off
chcp 65001 >nul
title Installation du Blind test
cd /d "%~dp0"
echo ==============================================================
echo    INSTALLATION DU BLIND TEST
echo    Installe Python et ffmpeg si besoin, puis cree le raccourci
echo    "Blind test" sur le bureau. Comptez 2 a 5 minutes.
echo ==============================================================
echo.

where winget >nul 2>nul
if errorlevel 1 goto manuel

rem --- Python 3.9 ou plus recent ---
set PYOK=0
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set PYOK=1
if %PYOK%==0 python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set PYOK=1
if %PYOK%==1 (
  echo [OK] Python est deja installe.
) else (
  echo [..] Installation de Python...
  winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
  if errorlevel 1 goto echec
)

rem --- ffmpeg (decoupe des extraits, montage video) ---
set FFOK=0
where ffmpeg >nul 2>nul && set FFOK=1
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" set FFOK=1
if %FFOK%==1 (
  echo [OK] ffmpeg est deja installe.
) else (
  echo [..] Installation de ffmpeg...
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
  if errorlevel 1 goto echec
)

rem --- raccourci sur le bureau ---
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'Blind test.lnk')); $s.TargetPath = '%~dp0Blind test.bat'; $s.WorkingDirectory = '%~dp0'; $s.IconLocation = $env:SystemRoot + '\System32\imageres.dll,103'; $s.Description = 'Blind test entre amis'; $s.Save()"
echo [OK] Raccourci "Blind test" cree sur le bureau.
echo.
echo ==============================================================
echo    TERMINE ! Double-cliquez sur "Blind test" sur le bureau.
echo    Au premier lancement, si Windows demande l'autorisation du
echo    pare-feu : cochez "Reseaux prives" et autorisez.
echo ==============================================================
pause
exit /b 0

:echec
echo.
echo L'installation automatique a echoue. Voir la solution manuelle ci-dessous.

:manuel
echo.
echo Installation manuelle (5 minutes) :
echo   1. Python : https://www.python.org/downloads/  (cochez "Add python.exe to PATH")
echo   2. ffmpeg : https://www.gyan.dev/ffmpeg/builds/  (ou copiez ffmpeg.exe et ffprobe.exe
echo      dans ce dossier, a cote de blindtest.py)
echo   3. Double-cliquez ensuite sur "Blind test.bat" dans ce dossier.
pause
exit /b 1
