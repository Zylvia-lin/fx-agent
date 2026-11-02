@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Install-FXAgent.ps1" %*
set "FX_INSTALL_EXIT=%ERRORLEVEL%"
if not "%FX_INSTALL_EXIT%"=="0" echo Installation failed. Please read the error above.
echo.
pause
exit /b %FX_INSTALL_EXIT%
