"""Подключение к базе данных и фабрика сессий SQLAlchemy.

База — файл astex_stock.db рядом с программой. Ничего настраивать не нужно:
файл и таблицы создаются сами при первом запуске.
"""

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).resolve().parent

# Каталог для файла базы. Обычно это папка с программой; переменная DATA_DIR нужна,
# только если базу хотят держать в другом месте — например, на внешнем диске.
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "astex_stock.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    # Разрешаем работать с одним файлом из разных потоков веб-сервера.
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    """Включаем контроль связей между таблицами (в SQLite он выключен по умолчанию)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
Base = declarative_base()


def get_db():
    """Отдаёт сессию базы на один запрос и закрывает её после ответа."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    import models  # noqa: F401  (нужен, чтобы модели зарегистрировались)

    Base.metadata.create_all(bind=engine)
