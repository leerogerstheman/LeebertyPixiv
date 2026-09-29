@echo off
rem ===========================================================
rem  pixiv crawler self-test  (ASCII only - cmd reads .bat as ANSI)
rem  Checks environment / config / network / credentials / index.
rem  It does NOT download any image.
rem ===========================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title pixiv crawler self-test

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

"%PYEXE%" -X utf8 pixiv_crawler.py selftest %*

echo.
pause
endlocal
