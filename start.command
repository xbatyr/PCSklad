#!/bin/bash
# ASTEX Stock — запуск на компьютере магазина. Двойной клик по файлу.
cd "$(dirname "$0")" || exit 1

if [ ! -d .venv ]; then
  echo "Первый запуск: создаю окружение…"
  python3 -m venv .venv || { echo "Не найден python3. Установите с python.org"; read -r; exit 1; }
fi
./.venv/bin/pip install -q -r requirements.txt || { echo "Не удалось поставить зависимости"; read -r; exit 1; }

if [ ! -f .env ]; then
  echo "Нет файла .env с паролем. Скопируйте .env.example в .env и впишите пароль."; read -r; exit 1
fi

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
echo
echo "  ASTEX Stock запущен"
echo "  На этом компьютере:  http://localhost:8000"
[ -n "$IP" ] && echo "  С телефона по вайфаю: http://$IP:8000"
echo "  Остановить: Ctrl+C или закрыть окно"
echo
exec ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
