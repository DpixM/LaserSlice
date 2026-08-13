@echo off
setlocal enabledelayedexpansion
title LaserSlice
cd /d "%~dp0"

echo ==================================================
echo    LaserSlice
echo ==================================================
echo.

REM --- 1) Trouver Python ---
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )

if not defined PY (
  echo Python n'est pas installe sur cet ordinateur.
  winget --version >nul 2>&1
  if !errorlevel! == 0 (
    echo Installation automatique de Python en cours...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    echo.
    echo Python vient d'etre installe.
    echo FERME cette fenetre et double-clique a nouveau sur ce fichier.
    echo.
    pause
    exit /b
  ) else (
    echo Je vais ouvrir la page de telechargement de Python.
    echo Installe-le en COCHANT bien "Add python.exe to PATH", puis relance ce fichier.
    start "" https://www.python.org/downloads/
    pause
    exit /b
  )
)

REM --- 2) Environnement isole (cree la 1ere fois) ---
if not exist ".venv\Scripts\python.exe" (
  echo Premiere installation : preparation de l'environnement...
  %PY% -m venv .venv
  if !errorlevel! neq 0 ( echo Echec de la creation de l'environnement. & pause & exit /b )
)
set "VPY=.venv\Scripts\python.exe"

REM --- 3) Composants : (re)installes seulement si requirements.txt a change ---
set "NEED_INSTALL="
if not exist ".venv\req.lock" set "NEED_INSTALL=1"
if exist ".venv\req.lock" ( fc /b requirements.txt ".venv\req.lock" >nul 2>&1 || set "NEED_INSTALL=1" )

if defined NEED_INSTALL (
  echo Installation des composants ^(cela peut prendre quelques minutes la 1ere fois^)...
  "%VPY%" -m pip install --upgrade pip
  "%VPY%" -m pip install -r requirements.txt
  if !errorlevel! neq 0 (
    echo.
    echo L'installation a echoue. Verifie ta connexion internet puis relance.
    pause
    exit /b
  )
  copy /y requirements.txt ".venv\req.lock" >nul
)

REM --- 4) Lancer le vrai logiciel ---
echo Lancement de LaserSlice...
echo.
"%VPY%" app.py
if !errorlevel! neq 0 (
  echo.
  echo LaserSlice s'est ferme avec une erreur ^(code !errorlevel!^).
  pause
)
