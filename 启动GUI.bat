@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title 语音转文字

echo 正在查找可用的 Python ...
set "PY="

rem ---- 候选 1: 便携版自带 Python（放同目录 python\ 下，拷到哪都能用）----
if not defined PY call :try "%~dp0python\python.exe"

rem ---- 候选 2: WorkBuddy 自带 Python ----
if not defined PY call :try "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do call :try "%%~D\python.exe"

rem ---- 候选 3: Windows 的 py 启动器 ----
if not defined PY for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do call :try "%%P"

rem ---- 候选 4: PATH 里的 python（跳过微软商店占位符）----
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do call :try "%%P"

rem ---- 候选 5: 官方安装器的默认位置 ----
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try "%%~D\python.exe"
if not defined PY for /d %%D in ("C:\Python3*") do call :try "%%~D\python.exe"

if not defined PY goto :nopython

echo 使用 Python: %PY%
echo.
"%PY%" -u server.py %*
if errorlevel 1 (
  echo.
  echo 程序异常退出，请把上面的报错信息发给我
  pause
)
exit /b 0

rem ============================================================
:try
if defined PY goto :eof
if not exist %1 goto :eof
rem 微软商店占位符不是真 Python，必须排除
echo %~1 | findstr /i "WindowsApps" >nul && goto :eof
%~1 -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof

:nopython
echo.
echo [错误] 没有找到可用的 Python 3
echo.
echo 解决方法（任选其一）:
echo   1. 把便携版自带的 python 文件夹放回本工具目录下
echo   2. 安装 Python: https://www.python.org/downloads/
echo      安装时务必勾选 "Add python.exe to PATH"
echo   3. 若已安装: 设置 ^> 应用 ^> 高级应用设置 ^> 应用执行别名
echo      关掉 python.exe / python3.exe 的商店占位符
echo.
pause
exit /b 1
