@echo off
rem ===========================================================================
rem  SSH-Connect — аккуратная сборка (PyInstaller, onedir)
rem
rem  Раскладка результата (dist\SSH-Connect\):
rem    SSH-Connect.exe        — сама программа
rem    _internal\             — ТОЛЬКО библиотеки (Python, DLL, зависимости)
rem    web-desktop\ skills\   — файлы программы (рядом с _internal)
rem    kitty.exe kitty.ini move_cursor.py favicon.ico README.md ...
rem    Sessions\              — портативные настройки KiTTY (окно/шрифт)
rem    SSH_env\               — python с uvicorn/fastapi (move_cursor + веб)
rem    user_data\             — папка пользователя (создаётся и программой
rem      SshHostKeys\ Sessions\ Downloads\ ssh_connections.json
rem      move_cursor.json/.log при первом запуске)
rem
rem  Запуск:  build.bat        (сборка)
rem           build.bat zip    (сборка + zip-архив)
rem ===========================================================================

chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "APP=SSH-Connect"
set "VENV=SSH_env"
set "PY=%CD%\%VENV%\Scripts\python.exe"
set "DIST_ROOT=%CD%\dist"
set "DIST=%DIST_ROOT%\%APP%"
set "INTERNAL=%DIST%\_internal"
set "WORK=%CD%\build"

echo ============================================================
echo   Сборка %APP%
echo ============================================================
echo.

rem --- 1. Проверка окружения ------------------------------------------------
echo [1/6] Проверка окружения...
if not exist "%PY%" (
    echo   [ОШИБКА] Не найден venv: %PY%
    echo   Создайте его: python -m venv %VENV%
    echo                 %VENV%\Scripts\pip install -r requirements.txt
    goto :error
)
"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo   Устанавливаю PyInstaller...
    "%PY%" -m pip install --upgrade pyinstaller
    if errorlevel 1 goto :error
)

rem --- 2. Очистка -----------------------------------------------------------
echo [2/6] Очистка build\ и dist\ ...
if exist "%WORK%"      rmdir /s /q "%WORK%"
if exist "%DIST_ROOT%" rmdir /s /q "%DIST_ROOT%"
mkdir "%WORK%"

rem --- 3. PyInstaller -------------------------------------------------------
rem  Без --add-data: файлы программы НЕ попадают в _internal (там только
rem  библиотеки). Их копируем рядом с _internal отдельным шагом.
echo [3/6] PyInstaller (onedir, windowed)...
"%PY%" -m PyInstaller ^
    --noconfirm --clean --onedir --windowed ^
    --name "%APP%" ^
    --contents-directory "_internal" ^
    --icon "%CD%\favicon.ico" ^
    --log-level WARN ^
    --workpath "%WORK%\pyi" ^
    --specpath "%WORK%" ^
    --exclude-module pytest --exclude-module _pytest ^
    "%CD%\main.py"
if errorlevel 1 goto :error
if not exist "%DIST%\%APP%.exe" goto :error

rem --- 4. Файлы программы рядом с _internal ---------------------------------
echo [4/6] Копирую файлы программы и SSH_env рядом с _internal ...
robocopy "web-desktop" "%DIST%\web-desktop" /E /NFL /NDL /NJH /NJS /NP ^
    /XD __pycache__ /XF *.pyc *.log *.log.err .server_token >nul
if errorlevel 8 goto :error
robocopy "skills" "%DIST%\skills" /E /NFL /NDL /NJH /NJS /NP ^
    /XD __pycache__ /XF *.pyc >nul
if errorlevel 8 goto :error

for %%F in (kitty.exe kitty.ini move_cursor.py favicon.ico README.md LICENSE requirements.txt uninstall.cmd) do (
    if exist "%%F" copy /Y "%%F" "%DIST%\" >nul
)
if not exist "%DIST%\kitty.exe" (
    echo   [ПРЕДУПРЕЖДЕНИЕ] kitty.exe не найден — терминал не заработает.
)

rem Портативная сессия KiTTY (размер окна/шрифт терминала) — рядом с kitty.exe
if exist "Sessions" echo   Копирую Sessions\ (настройки окна KiTTY) ... & if exist "Sessions" robocopy "Sessions" "%DIST%\Sessions" /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ >nul & if errorlevel 8 goto :error

rem Python рядом с exe — для move_cursor и веб-интерфейса
if exist "SSH_env" echo   Копирую SSH_env (python + uvicorn/fastapi) ... & if exist "SSH_env" robocopy "SSH_env" "%DIST%\SSH_env" /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ >nul & if errorlevel 8 goto :error

rem --- 5. Папка пользователя user_data --------------------------------------
echo [5/6] Готовлю папку пользователя (user_data) ...
if not exist "%DIST%\user_data"                        mkdir "%DIST%\user_data"
if not exist "%DIST%\user_data\SshHostKeys"            mkdir "%DIST%\user_data\SshHostKeys"
if not exist "%DIST%\user_data\Sessions"               mkdir "%DIST%\user_data\Sessions"
if not exist "%DIST%\user_data\Downloads"              mkdir "%DIST%\user_data\Downloads"
if not exist "%DIST%\user_data\ssh_connections.json"   >"%DIST%\user_data\ssh_connections.json" echo {}

rem --- 6. Итог --------------------------------------------------------------
echo [6/6] Готово.
echo.
echo   Результат: %DIST%
echo     %APP%.exe
echo     _internal\      ^<- только библиотеки
echo     web-desktop\ skills\ kitty.exe Sessions\ SSH_env\ move_cursor.py ...
echo     user_data\      ^<- папка пользователя (SshHostKeys\ Sessions\ Downloads\ ssh_connections.json)
echo.

if /I "%~1"=="zip" (
    echo   Упаковываю в zip...
    tar -a -c -f "%DIST_ROOT%\%APP%.zip" -C "%DIST_ROOT%" "%APP%"
    if errorlevel 1 ( echo   [ОШИБКА] не удалось создать zip & goto :error )
    echo   Архив: %DIST_ROOT%\%APP%.zip
)

echo ============================================================
echo   СБОРКА ЗАВЕРШЕНА УСПЕШНО
echo ============================================================
endlocal
exit /b 0

:error
echo.
echo ============================================================
echo   ОШИБКА СБОРКИ
echo ============================================================
endlocal
exit /b 1