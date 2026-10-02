@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 直播碼率測試
set PYTHONIOENCODING=utf-8

if not exist "stream_probe.py" (
  echo [!] 找不到 stream_probe.py，請把它跟這個 .bat 放在同一個資料夾。
  goto :end
)

rem ---- 找 Python ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [!] 這台電腦還沒有安裝 Python。
  choice /c YN /m "要自動安裝嗎？按 Y 安裝，N 取消"
  if errorlevel 2 goto :end
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo 安裝完成後，請關掉這個視窗，再點一次這個檔案。
  goto :end
)

rem ---- 找 ffmpeg ----
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [!] 這台電腦還沒有安裝 ffmpeg。
  choice /c YN /m "要自動安裝嗎？按 Y 安裝，N 取消"
  if errorlevel 2 goto :end
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo.
  echo 安裝完成後，請關掉這個視窗，再點一次這個檔案。
  goto :end
)

%PY% stream_probe.py

:end
echo.
pause
