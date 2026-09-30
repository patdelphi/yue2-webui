@echo off
chcp 65001 >nul
setlocal

REM ========================================
REM YuE2 Cloudflare Tunnel 启动脚本
REM 作用: 把本机 WebUI(127.0.0.1:9898) 暴露为 https://yue2.patdelphi.xyz
REM 配置: %USERPROFILE%\.cloudflared\config.yml
REM       ingress: yue2.patdelphi.xyz -> http://127.0.0.1:9898
REM 说明: 隧道不做开机自启, 需要外网访问时手动运行本脚本;
REM       保持本窗口开启即保持隧道存活, 按 Ctrl+C 停止
REM ========================================

echo ========================================
echo YuE2 Cloudflare Tunnel
echo ========================================
echo.

set "CLOUDFLARED=C:\Program Files (x86)\cloudflared\cloudflared.exe"
set "CFG=%USERPROFILE%\.cloudflared\config.yml"
set "LOG=%USERPROFILE%\.cloudflared\yue2-tunnel.log"

REM 检查 cloudflared 是否已安装
if not exist "%CLOUDFLARED%" (
    echo [错误] 未找到 cloudflared: "%CLOUDFLARED%"
    echo 请先安装: winget install --id Cloudflare.cloudflared
    pause
    exit /b 1
)

REM 检查隧道配置文件是否存在
if not exist "%CFG%" (
    echo [错误] 未找到隧道配置: "%CFG%"
    pause
    exit /b 1
)

REM 检查本机 WebUI 是否在监听 9898（未启动仅警告，隧道本身仍可运行）
netstat -ano | findstr "127.0.0.1:9898" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [警告] 本机 127.0.0.1:9898 未在监听, 请先运行 run.bat 启动 WebUI
    echo.
)

echo 隧道: yue2.patdelphi.xyz -^> 127.0.0.1:9898
echo 日志: %LOG%
echo 访问地址: https://yue2.patdelphi.xyz
echo 按 Ctrl+C 停止隧道
echo.

"%CLOUDFLARED%" --config "%CFG%" tunnel run >>"%LOG%" 2>&1

if errorlevel 1 (
    echo.
    echo [错误] 隧道进程异常退出, 详见日志: %LOG%
    pause
)
