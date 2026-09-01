@echo off
rem ASTEX Stock - запуск на компьютере магазина. Двойной клик по файлу.
cd /d "%~dp0"

if not exist .venv (
  echo Первый запуск: создаю окружение...
  python -m venv .venv || (echo Не найден Python. Установите с python.org && pause && exit /b 1)
)
.venv\Scripts\pip install -q -r requirements.txt || (echo Не удалось поставить зависимости && pause && exit /b 1)

if not exist .env (
  echo Нет файла .env с паролем. Скопируйте .env.example в .env и впишите пароль.
  pause
  exit /b 1
)

echo.
echo   ASTEX Stock запущен
echo   На этом компьютере:  http://localhost:8000
echo   С телефона по вайфаю: посмотрите свой IP командой ipconfig
echo   Остановить: Ctrl+C или закрыть окно
echo.
.venv\Scripts\uvicorn main:app --host 0.0.0.0 --port 8000
