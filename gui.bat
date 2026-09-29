@echo off
rem ===========================================================
rem  pixiv crawler GUI  (ASCII only - cmd reads .bat as ANSI)
rem ===========================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title pixiv crawler GUI

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

"%PYEXE%" -X utf8 -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo [INFO] This Python has no tkinter - falling back to command line mode.
  echo        Example: crawl.bat "keyword" --pages 2
  echo.
  "%PYEXE%" -X utf8 pixiv_crawler.py crawl -i
) else (
  start "" "%PYEXE%" -X utf8 pixiv_crawler.py gui
)
endlocal
