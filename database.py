"""Подключение к SQLite и фабрика сессий SQLAlchemy."""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "astex_stock.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # SQLite + многопоточный uvicorn: разрешаем использовать соединение из разных потоков.
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    """Включаем контроль внешних ключей (в SQLite он выключен по умолчанию)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
Base = declarative_base()


def get_db():
    """FastAPI-зависимость: сессия на один HTTP-запрос."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    import models  # noqa: F401  (регистрация моделей в метаданных)

    Base.metadata.create_all(bind=engine)
