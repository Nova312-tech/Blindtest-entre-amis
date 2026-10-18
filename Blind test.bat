@echo off
chcp 65001 >nul
title Blind test
cd /d "%~dp0"
rem Lance l'atelier du blind test. En cas d'erreur, la fenetre reste ouverte (detail dans journal.txt).
set PYTHON=
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set PYTHON=py -3
if not defined PYTHON python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set PYTHON=python
if not defined PYTHON for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set PYTHON="%%D\python.exe"
if not defined PYTHON (
  echo Python est introuvable. Lancez d'abord "Installer.bat" dans ce dossier.
  pause
  exit /b 1
)
%PYTHON% jeu.py
if errorlevel 1 pause
