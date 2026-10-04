@echo off
rem ============================================================
rem  build_exe.bat —— 安全打包 Windows exe + 发布 zip
rem
rem  安全保证：发布包一律使用 config.release.json（干净模板，无凭据），
rem  绝不会把你的 config.json（含 cookie / token）打进 exe。
rem  打包前/后各做一次凭据检查，任一不过就中止。
rem
rem  用法：双击即可。产物：dist\PixivCrawler-windows.zip
rem  前置：python -m pip install pyinstaller pillow
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

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
  echo [错误] 找不到 Python，无法打包。
  pause & exit /b 1
)

echo [0/3] 安全自检：确认 release 模板里没有凭据 ...
"%PYEXE%" -c "import json,sys; d=json.load(open('config.release.json',encoding='utf-8')); bad=[k for k in ('cookie_phpsessid','refresh_token') if str(d.get(k) or '').strip()]; print('[X] 发布模板里含凭据：'+','.join(bad)+' —— 已中止打包') if bad else print('[OK] 发布模板干净，可以打包'); sys.exit(1) if bad else None"
if errorlevel 1 (
  echo.
  echo [中止] 为避免把登录态发到公网，打包已停止。请先清空 config.release.json 里的凭据。
  pause & exit /b 1
)

echo [1/3] 打包 exe（PyInstaller onedir，内置干净配置）...
"%PYEXE%" -m PyInstaller --noconfirm --clean --onedir --windowed --name "PixivCrawler" ^
  --icon "pixiv_crawler.ico" ^
  --add-data "config.release.json;config.json" ^
  --add-data "artists.py;." ^
  --add-data "ugoira.py;." ^
  --add-data "browser_cookie.py;." ^
  --add-data "docs;docs" ^
  --add-data "README.md;." ^
  pixiv_crawler.py
if errorlevel 1 (
  echo [错误] PyInstaller 失败。
  pause & exit /b 1
)

echo [2/3] 复检：确认打出来的 config.json 不含凭据 ...
"%PYEXE%" -c "import json,sys; d=json.load(open(r'dist\PixivCrawler\_internal\config.json',encoding='utf-8')); bad=[k for k in ('cookie_phpsessid','refresh_token') if str(d.get(k) or '').strip()]; print('[X] 打包结果含凭据：'+','.join(bad)) if bad else print('[OK] 打包结果干净'); sys.exit(1) if bad else None"
if errorlevel 1 (
  echo [中止] 打包结果含凭据，已停止（未生成 zip）。
  pause & exit /b 1
)

echo [3/3] 压缩发布 zip ...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\PixivCrawler\*' -DestinationPath 'dist\PixivCrawler-windows.zip' -Force"
if exist "dist\PixivCrawler-windows.zip" (
  echo.
  echo 完成！发布包：dist\PixivCrawler-windows.zip
) else (
  echo [警告] zip 生成失败，exe 在 dist\PixivCrawler\PixivCrawler.exe
)
pause