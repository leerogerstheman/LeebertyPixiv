@echo off
rem ===========================================================
rem  pixiv crawler launcher  (ASCII only - cmd reads .bat as ANSI)
rem  Usage:  crawl.bat                     -> interactive prompt
rem          crawl.bat "keyword" --pages 3
rem ===========================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title pixiv crawler

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
  echo.
  echo [ERROR] No usable Python found.
  echo         Install Python 3.9+ from https://www.python.org/downloads/
  echo         and check "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

if "%~1"=="" (
  "%PYEXE%" -X utf8 pixiv_crawler.py crawl -i
) else (
  "%PYEXE%" -X utf8 pixiv_crawler.py crawl %*
)

echo.
echo ---------------------------------------------------------------
echo Done. Library folder: D:\PixivCrawler\library
echo ---------------------------------------------------------------
pause
endlocal
