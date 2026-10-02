@echo off
rem ===========================================================================
rem  uninstall.cmd — удаление SSH-Connect
rem
rem  1. Проверяет, что программа не запущена (иначе папка занята).
rem  2. Удаляет ярлык 'SSH-Connect.lnk' с рабочего стола.
rem  3. Удаляет саму папку программы (отложенно из %TEMP%, чтобы можно было
rem     удалить папку, из которой запущен этот bat).
rem  Файл ЛИШЬ для Linux-совместимых переносов строк нет — ВАЖНО: CRLF!
rem ===========================================================================

chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_EXE=SSH-Connect.exe"
set "FOLDER=%~dp0"

echo ============================================================
echo   Удаление SSH-Connect
echo ============================================================
echo.

rem --- 1. Не запущена ли программа / терминал --------------------------------
tasklist /FI "IMAGENAME eq SSH-Connect.exe" 2>nul | find /i "SSH-Connect.exe" >nul
if not errorlevel 1 goto running
tasklist /FI "IMAGENAME eq kitty.exe" 2>nul | find /i "kitty.exe" >nul
if not errorlevel 1 goto running

rem --- 2. Ярлык с рабочего стола --------------------------------------------
echo   Удаляю ярлык с рабочего стола...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$l=Join-Path ([Environment]::GetFolderPath('Desktop')) 'SSH-Connect.lnk'; if(Test-Path $l){Remove-Item $l -Force -ErrorAction SilentlyContinue}"
if exist "%USERPROFILE%\Desktop\SSH-Connect.lnk" del /f /q "%USERPROFILE%\Desktop\SSH-Connect.lnk" >nul 2>&1

rem --- 3. Папка программы ----------------------------------------------------
rem  Сам uninstall.cmd открыт cmd.exe внутри удаляемой папки, поэтому папку
rem  удаляет отдельный отвязанный процесс cmd /c (у него нет бат-файла, ему
rem  нечего терять): подождёт 2 c, перейдёт в %TEMP% и выполнит rd.
echo   Удаляю папку программы...
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul & cd /d ""%TEMP%"" & rd /s /q ""%FOLDER%"" >nul 2>&1"

echo   Готово. Папка будет удалена через пару секунд.
ping -n 2 127.0.0.1 >nul
endlocal
exit /b 0

:running
echo   [ОСТАНОВ] Программа ещё запущена.
echo   Закройте SSH-Connect и окно KiTTY, затем запустите uninstall.cmd снова.
echo.
pause
endlocal
exit /b 1