@echo off
rem ===========================================================
rem  pixiv crawler web UI  (ASCII only - cmd reads .bat as ANSI)
rem
rem  Same backend as gui.bat - pixiv_crawler.py is shared - but the
rem  interface is served by the standard library, so this one does NOT
rem  need tkinter. gui.bat falls back to command-line mode when tkinter
rem  is missing; the web interface still works there.
rem
rem  It does not need Pillow either: the desktop preview uses PIL's
rem  ImageTk, while the browser renders the images itself.
rem ===========================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title pixiv crawler web

set "PYEXE="
set "CAND=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if exist "%CAND%" set "PYEXE=%CAND%"
if not defined PYEXE (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYEXE (
      "%%i" -c "import sys" >nul 2>nul && set "PYEXE=%%i"
    )
  )
)
if not defined PYEXE (
  echo [ERROR] No usable Python found. Install Python 3.9+ and add it to PATH.
  pause
  exit /b 1
)

start "" "%PYEXE%" -X utf8 webui.py %*
endlocal
