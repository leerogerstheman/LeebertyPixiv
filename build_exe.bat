@echo off
rem ============================================================
rem  build_exe.bat —— 重新打包 Windows exe + 发布 zip
rem  用法：双击即可。产物在 dist\PixivCrawler-windows.zip
rem  前置：本机 Python 需已装 PyInstaller 和 Pillow：
rem    python -m pip install pyinstaller pillow
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

echo [1/2] 打包 exe（PyInstaller onedir，含哥特字体 P 图标）...
"%PYEXE%" -m PyInstaller --noconfirm --clean --onedir --windowed --name "PixivCrawler" ^
  --icon "pixiv_crawler.ico" ^
  --add-data "config.json;." ^
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

echo [2/2] 压缩发布 zip ...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\PixivCrawler\*' -DestinationPath 'dist\PixivCrawler-windows.zip' -Force"
if exist "dist\PixivCrawler-windows.zip" (
  echo 完成！发布包：dist\PixivCrawler-windows.zip
) else (
  echo [警告] zip 生成失败，exe 在 dist\PixivCrawler\PixivCrawler.exe
)
pause