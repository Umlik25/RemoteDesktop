@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Сброс пароля пользователя App_Remote ===
echo.
python app.py --list-users
echo.
set /p U="Введите имя пользователя для сброса пароля: "
python app.py --reset-password "%U%"
echo.
pause
