#!/bin/bash
# Резервная копия базы в папку backups рядом с проектом.
cd "$(dirname "$0")" || exit 1
mkdir -p backups
STAMP=$(date +%Y-%m-%d_%H%M)
if [ -f astex_stock.db ]; then
  ./.venv/bin/python -c "
import sqlite3, sys
src = sqlite3.connect('astex_stock.db'); dst = sqlite3.connect('backups/astex_stock_$STAMP.db')
src.backup(dst); dst.close(); src.close()
"
  echo "Копия сохранена: backups/astex_stock_$STAMP.db"
else
  echo "Файл базы не найден"
fi
ls -1t backups | tail -n +15 | while read -r f; do rm -f "backups/$f"; done
read -r -p "Готово. Нажмите Enter"
