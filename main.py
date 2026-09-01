"""ASTEX Stock — учёт склада, сборки ПК, пересборка (swap) и розничные продажи.

Запуск:  uvicorn main:app --reload
"""

import base64
import binascii
import os
import secrets
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

import excel_io
import models
from database import get_db, init_db
from models import CATEGORIES, Item, PC, now


BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Мини-парсер .env: переменные из окружения имеют приоритет."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# Адреса, доступные без пароля (проверка, что программа жива).
PUBLIC_PATHS = {"/healthz"}

if not ADMIN_PASSWORD:
    raise RuntimeError(
        "Не задан ADMIN_PASSWORD. Положите его в .env рядом с main.py "
        "(ADMIN_PASSWORD=...) или в переменные окружения сервиса."
    )


# Таблицы создаём сразу при импорте — тогда база готова к работе в любом случае.
# Повторный вызов ничего не ломает: существующие таблицы не трогаются.
init_db()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ASTEX Stock", version="1.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

def _login_page() -> str:
    """Страница-визитка вместо голого «401». Видна тому, кто закрыл окно ввода пароля.

    Никаких учётных данных здесь нет и быть не должно — страница публичная.
    """
    return '''<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ASTEX Stock</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
background:#020617;background-image:radial-gradient(50rem 30rem at 20% -10%,rgba(79,70,229,.25),transparent 60%),
radial-gradient(40rem 25rem at 90% 0%,rgba(6,182,212,.16),transparent 55%);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#cbd5e1}
.card{max-width:460px;width:100%;padding:36px;border:1px solid #1e293b;border-radius:18px;
background:rgba(15,23,42,.7);backdrop-filter:blur(12px)}
.logo{width:46px;height:46px;border-radius:14px;display:grid;place-items:center;font-weight:800;
font-size:20px;color:#fff;background:linear-gradient(135deg,#6366f1,#06b6d4)}
h1{margin:20px 0 6px;font-size:22px;color:#fff;font-weight:650}
h1 span{color:#818cf8}
p{margin:0 0 14px;font-size:14px;color:#94a3b8}
.muted{font-size:13px;color:#64748b}
ul{margin:0 0 18px;padding-left:18px;font-size:13.5px;color:#94a3b8}
li{margin:3px 0}
.lock{display:flex;align-items:center;gap:9px;padding:12px 14px;border-radius:12px;
border:1px solid #1e293b;background:rgba(2,6,23,.5);font-size:13px;color:#94a3b8}
.stack{margin-top:20px;padding-top:18px;border-top:1px solid #1e293b;font-size:12px;color:#475569}
</style></head><body><div class="card">
<div class="logo">A</div>
<h1>ASTEX <span>Stock</span></h1>
<p>Учётная система компьютерного магазина: склад комплектующих и периферии,
сборка ПК, пересборка со swap деталей, розничные продажи и аналитика прибыли.</p>
<ul>
<li>Сборка ПК из произвольного числа позиций</li>
<li>Пересборка: снятые детали возвращаются на склад</li>
<li>Продажа в один клик с каскадным списанием</li>
<li>Лента продаж с прибылью по каждой сделке</li>
</ul>
<div class="lock">
<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
Доступ по логину и паролю — обновите страницу, чтобы ввести их</div>
<div class="stack">FastAPI · SQLAlchemy · SQLite/Postgres · Tailwind · Alpine.js</div>
</div></body></html>'''


def _unauthorized() -> Response:
    return HTMLResponse(
        content=_login_page(),
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="ASTEX Stock", charset="UTF-8"'},
    )


def _credentials_ok(header: Optional[str]) -> bool:
    """Разбирает заголовок Authorization: Basic <base64> и сверяет пару логин/пароль."""
    if not header:
        return False
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        user, _, password = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    # compare_digest на обоих полях — чтобы время ответа не выдавало правильный логин.
    ok_user = secrets.compare_digest(user, ADMIN_USER)
    ok_password = secrets.compare_digest(password, ADMIN_PASSWORD)
    return ok_user and ok_password


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic на всё приложение: страницу, API и /docs."""
    if request.url.path in PUBLIC_PATHS or _credentials_ok(request.headers.get("Authorization")):
        return await call_next(request)
    return _unauthorized()


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Открытый health check для хостинга — без пароля, без данных."""
    return {"status": "ok"}


# =============================================================================
#  Схемы
# =============================================================================

ItemType = Literal["component", "periphery", "service"]
ItemStatus = Literal["in_stock", "in_assembly", "sold"]


class ItemIn(BaseModel):
    """Позиция внутри сборки. id != None — существующая деталь (правка / привязка со склада)."""

    id: Optional[int] = None
    item_type: ItemType = "component"
    category: str = "Other"
    name: str = Field(min_length=1, max_length=200)
    cost_price: int = Field(default=0, ge=0)
    retail_price: Optional[int] = Field(default=None, ge=0)
    source_note: Optional[str] = Field(default=None, max_length=500)
    purchased_at: Optional[datetime] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Наименование не может быть пустым")
        return v

    @field_validator("source_note")
    @classmethod
    def _strip_note(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str) -> str:
        v = (v or "").strip() or "Other"
        if v not in CATEGORIES:
            raise ValueError(f"Неизвестная категория: {v}")
        return v


class ItemCreate(ItemIn):
    """Оприходование товара на свободный склад."""

    id: Optional[int] = Field(default=None, exclude=True)
    quantity: int = Field(default=1, ge=1, le=100, description="Сколько одинаковых позиций создать")


class IntakeIn(BaseModel):
    """Приход б/у системника: разбирается на комплектующие, карточка ПК не создаётся."""

    source_note: Optional[str] = Field(default=None, max_length=500)
    purchased_at: Optional[datetime] = None
    parts: list[ItemIn] = Field(min_length=1)

    @field_validator("source_note")
    @classmethod
    def _strip_note(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None


class IntakeOut(BaseModel):
    created: int
    total_cost: int
    source_note: Optional[str]
    purchased_at: Optional[str]
    items: list["ItemOut"]


class PCIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sell_price: int = Field(default=0, ge=0)
    parts: list[ItemIn] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Название сборки не может быть пустым")
        return v


class SellIn(BaseModel):
    """Тело для продажи: фактическая цена и дата сделки (обе необязательные)."""

    price: Optional[int] = Field(default=None, ge=0)
    sold_at: Optional[datetime] = None


class ServiceSellIn(BaseModel):
    """Разовая услуга: ремонт, чистка, сборка. Себестоимости обычно нет."""

    name: str = Field(min_length=1, max_length=200)
    category: str = "Repair"
    price: int = Field(ge=0)
    cost_price: int = Field(default=0, ge=0)
    sold_at: Optional[datetime] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Название услуги не может быть пустым")
        return v

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str) -> str:
        v = (v or "").strip() or "Repair"
        if v not in CATEGORIES:
            raise ValueError(f"Неизвестная категория: {v}")
        return v


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pc_id: Optional[int]
    pc_name: Optional[str] = None
    item_type: str
    category: str
    name: str
    cost_price: int
    retail_price: Optional[int]
    status: str
    source_note: Optional[str] = None
    purchased_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    sold_at: Optional[str] = None


class PCOut(BaseModel):
    id: int
    name: str
    sell_price: int
    status: str
    created_at: Optional[str]
    updated_at: Optional[str]
    sold_at: Optional[str]
    cost_price: int
    margin: int
    items: list[ItemOut]


class SaleOut(BaseModel):
    kind: Literal["pc", "item"]
    id: int
    name: str
    subtitle: str
    sold_at: Optional[str]
    revenue: int
    cost: int
    profit: int


class SalesOut(BaseModel):
    period: str
    date_from: Optional[str]
    date_to: Optional[str]
    sales: list[SaleOut]
    revenue: int
    cost: int
    profit: int
    count: int


class StatsOut(BaseModel):
    pcs_in_stock: int
    pcs_sold: int
    items_in_stock: int
    items_in_assembly: int
    items_sold: int
    stock_value: int          # заморожено всего (склад + несобранные/непроданные сборки)
    free_stock_value: int     # только свободные позиции
    assembly_value: int       # закуп деталей внутри непроданных ПК
    revenue: int              # выручка (ПК + штучные продажи)
    profit: int               # выручка - себестоимость
    profit_pcs: int
    profit_items: int


# =============================================================================
#  Сериализация
# =============================================================================


def _dt(value) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def serialize_item(item: Item, pc_name: Optional[str] = None) -> dict:
    return {
        "id": item.id,
        "pc_id": item.pc_id,
        "pc_name": pc_name if pc_name is not None else (item.pc.name if item.pc else None),
        "item_type": item.item_type,
        "category": item.category,
        "name": item.name,
        "cost_price": item.cost_price,
        "retail_price": item.retail_price,
        "status": item.status,
        "source_note": item.source_note,
        "purchased_at": _dt(item.purchased_at),
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
        "sold_at": _dt(item.sold_at),
    }


def serialize_pc(pc: PC) -> dict:
    return {
        "id": pc.id,
        "name": pc.name,
        "sell_price": pc.sell_price,
        "status": pc.status,
        "created_at": _dt(pc.created_at),
        "updated_at": _dt(pc.updated_at),
        "sold_at": _dt(pc.sold_at),
        "cost_price": pc.cost_price,
        "margin": pc.margin,
        "items": [serialize_item(i, pc_name=pc.name) for i in pc.items],
    }


# =============================================================================
#  Страница
# =============================================================================


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/meta", tags=["meta"])
def meta():
    """Справочники категорий для фронтенда."""
    return {
        "component_categories": list(models.COMPONENT_CATEGORIES) + ["Other"],
        "periphery_categories": list(models.PERIPHERY_CATEGORIES) + ["Other"],
        "service_categories": list(models.SERVICE_CATEGORIES) + ["Other"],
    }


# =============================================================================
#  Готовые ПК и сетапы
# =============================================================================


@app.get("/api/pcs", response_model=list[PCOut], tags=["pcs"])
def list_pcs(
    status: Literal["all", "in_stock", "sold"] = "all",
    db: Session = Depends(get_db),
):
    stmt = select(PC).order_by(
        case((PC.status == "in_stock", 0), else_=1),
        PC.created_at.desc(),
        PC.id.desc(),
    )
    if status != "all":
        stmt = stmt.where(PC.status == status)
    return [serialize_pc(pc) for pc in db.scalars(stmt).unique().all()]


@app.get("/api/pcs/{pc_id}", response_model=PCOut, tags=["pcs"])
def get_pc(pc_id: int, db: Session = Depends(get_db)):
    pc = db.get(PC, pc_id)
    if pc is None:
        raise HTTPException(404, "Сборка не найдена")
    return serialize_pc(pc)


@app.post("/api/pcs", response_model=PCOut, status_code=201, tags=["pcs"])
def create_pc(payload: PCIn, db: Session = Depends(get_db)):
    """Создаёт ПК со статусом in_stock и привязывает к нему все переданные позиции."""
    pc = PC(name=payload.name, sell_price=payload.sell_price, status="in_stock")
    db.add(pc)
    db.flush()  # нужен pc.id

    for part in payload.parts:
        if part.id is not None:
            # Забираем существующую позицию со свободного склада в сборку.
            item = _take_existing_item(db, part.id, pc.id)
            _apply_part(item, part)
        else:
            item = Item(
                pc_id=pc.id,
                item_type=part.item_type,
                category=part.category,
                name=part.name,
                cost_price=part.cost_price,
                retail_price=part.retail_price,
                status="in_assembly",
            )
            db.add(item)

    db.commit()
    db.refresh(pc)
    return serialize_pc(pc)


@app.put("/api/pcs/{pc_id}", response_model=PCOut, tags=["pcs"])
def update_pc(pc_id: int, payload: PCIn, db: Session = Depends(get_db)):
    """Пересборка / swap деталей.

    Позиции, исчезнувшие из списка, возвращаются на свободный склад (in_stock, pc_id=NULL).
    Позиции с id обновляются и остаются (или привязываются) к сборке, без id — создаются.
    """
    pc = db.get(PC, pc_id)
    if pc is None:
        raise HTTPException(404, "Сборка не найдена")
    if pc.status == "sold":
        raise HTTPException(400, "Нельзя пересобирать проданный ПК")

    pc.name = payload.name
    pc.sell_price = payload.sell_price

    before = {item.id: item for item in pc.items}
    kept: set[int] = set()

    for part in payload.parts:
        if part.id is not None and part.id in before:
            item = before[part.id]
            _apply_part(item, part)
            item.pc_id = pc.id
            item.status = "in_assembly"
        elif part.id is not None:
            item = _take_existing_item(db, part.id, pc.id)
            _apply_part(item, part)
        else:
            item = Item(
                pc_id=pc.id,
                item_type=part.item_type,
                category=part.category,
                name=part.name,
                cost_price=part.cost_price,
                retail_price=part.retail_price,
                status="in_assembly",
            )
            db.add(item)
            db.flush()
        kept.add(item.id)

    # Всё, что убрали из сборки — обратно на склад.
    for item_id, item in before.items():
        if item_id not in kept:
            item.pc_id = None
            item.status = "in_stock"
            item.sold_at = None

    db.commit()
    db.refresh(pc)
    return serialize_pc(pc)


@app.post("/api/pcs/{pc_id}/sell", response_model=PCOut, tags=["pcs"])
def sell_pc(pc_id: int, payload: Optional[SellIn] = None, db: Session = Depends(get_db)):
    """Продажа ПК в один клик: ПК + все привязанные позиции переходят в sold (одна транзакция)."""
    pc = db.get(PC, pc_id)
    if pc is None:
        raise HTTPException(404, "Сборка не найдена")
    if pc.status == "sold":
        raise HTTPException(400, "Этот ПК уже продан")

    moment = (payload.sold_at if payload and payload.sold_at else None) or now()
    if payload is not None and payload.price is not None:
        pc.sell_price = payload.price
    pc.status = "sold"
    pc.sold_at = moment

    for item in pc.items:  # каскад по WHERE pc_id = pc_id
        item.status = "sold"
        item.sold_at = moment

    db.commit()
    db.refresh(pc)
    return serialize_pc(pc)


@app.post("/api/pcs/{pc_id}/unsell", response_model=PCOut, tags=["pcs"])
def unsell_pc(pc_id: int, db: Session = Depends(get_db)):
    """Отмена продажи: ПК и его позиции возвращаются в наличие (защита от случайного клика)."""
    pc = db.get(PC, pc_id)
    if pc is None:
        raise HTTPException(404, "Сборка не найдена")
    if pc.status != "sold":
        raise HTTPException(400, "Эта сборка и так в наличии")

    pc.status = "in_stock"
    pc.sold_at = None
    for item in pc.items:
        item.status = "in_assembly"
        item.sold_at = None

    db.commit()
    db.refresh(pc)
    return serialize_pc(pc)


@app.delete("/api/pcs/{pc_id}", status_code=204, tags=["pcs"])
def delete_pc(pc_id: int, db: Session = Depends(get_db)):
    """Удаление сборки. Детали не пропадают — возвращаются на свободный склад."""
    pc = db.get(PC, pc_id)
    if pc is None:
        raise HTTPException(404, "Сборка не найдена")
    if pc.status == "sold":
        raise HTTPException(400, "Проданную сборку удалить нельзя — она нужна для отчёта. Сначала отмените продажу")

    for item in pc.items:
        item.pc_id = None
        item.status = "in_stock"
        item.sold_at = None

    db.delete(pc)
    db.commit()
    return Response(status_code=204)


def _take_existing_item(db: Session, item_id: int, pc_id: int) -> Item:
    """Привязывает свободную (или уже принадлежащую этой сборке) позицию к ПК."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, f"Позиция #{item_id} не найдена")
    if item.status == "sold":
        raise HTTPException(400, f"Позиция «{item.name}» уже продана")
    if item.pc_id is not None and item.pc_id != pc_id:
        raise HTTPException(400, f"Позиция «{item.name}» уже установлена в другую сборку")
    item.pc_id = pc_id
    item.status = "in_assembly"
    return item


def _apply_part(item: Item, part: ItemIn) -> None:
    item.item_type = part.item_type
    item.category = part.category
    item.name = part.name
    item.cost_price = part.cost_price
    item.retail_price = part.retail_price
    if part.source_note is not None:
        item.source_note = part.source_note
    if part.purchased_at is not None:
        item.purchased_at = part.purchased_at


# =============================================================================
#  Свободный склад и розница
# =============================================================================


@app.get("/api/items", response_model=list[ItemOut], tags=["items"])
def list_items(
    type: Literal["all", "component", "periphery", "service"] = "all",
    status: Literal["all", "in_stock", "in_assembly", "sold"] = "all",
    category: str = Query(default="all", description="Подфильтр по категории"),
    q: Optional[str] = Query(default=None, description="Поиск по наименованию"),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Item, PC.name)
        .join(PC, Item.pc_id == PC.id, isouter=True)
        .order_by(Item.id.desc())
    )
    if type != "all":
        stmt = stmt.where(Item.item_type == type)
    if status != "all":
        stmt = stmt.where(Item.status == status)
    if category and category != "all":
        if category not in CATEGORIES:
            raise HTTPException(422, f"Неизвестная категория: {category}")
        stmt = stmt.where(Item.category == category)
    if q:
        stmt = stmt.where(Item.name.ilike(f"%{q.strip()}%"))
    return [serialize_item(item, pc_name=pc_name) for item, pc_name in db.execute(stmt).all()]


@app.post("/api/items", response_model=list[ItemOut], status_code=201, tags=["items"])
def create_items(payload: ItemCreate, db: Session = Depends(get_db)):
    """Оприходование товара на свободный склад. quantity > 1 создаёт несколько одинаковых позиций."""
    created = [
        Item(
            pc_id=None,
            item_type=payload.item_type,
            category=payload.category,
            name=payload.name,
            cost_price=payload.cost_price,
            retail_price=payload.retail_price,
            status="in_stock",
        )
        for _ in range(payload.quantity)
    ]
    db.add_all(created)
    db.commit()
    return [serialize_item(item) for item in created]


@app.post("/api/intake", response_model=IntakeOut, status_code=201, tags=["items"])
def intake_pc(payload: IntakeIn, db: Session = Depends(get_db)):
    """Приход б/у системника: раскладывается на комплектующие на свободном складе.

    Карточка ПК не создаётся — каждая деталь становится самостоятельной позицией,
    которую можно поставить в любую сборку или продать отдельно.
    """
    moment = payload.purchased_at or now()
    created: list[Item] = []
    for part in payload.parts:
        created.append(Item(
            pc_id=None,
            item_type=part.item_type,
            category=part.category,
            name=part.name,
            cost_price=part.cost_price,
            retail_price=part.retail_price,
            status="in_stock",
            source_note=part.source_note or payload.source_note,
            purchased_at=part.purchased_at or moment,
        ))
    db.add_all(created)
    db.commit()
    return IntakeOut(
        created=len(created),
        total_cost=sum(i.cost_price for i in created),
        source_note=payload.source_note,
        purchased_at=_dt(moment),
        items=[serialize_item(i) for i in created],
    )


@app.post("/api/services/sell", response_model=ItemOut, status_code=201, tags=["items"])
def sell_service(payload: ServiceSellIn, db: Session = Depends(get_db)):
    """Разовая услуга (ремонт, чистка, сборка) — сразу продаётся и попадает в прибыль."""
    moment = payload.sold_at or now()
    item = Item(
        pc_id=None,
        item_type="service",
        category=payload.category,
        name=payload.name,
        cost_price=payload.cost_price,
        retail_price=payload.price,
        status="sold",
        purchased_at=moment,
        sold_at=moment,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return serialize_item(item)


@app.put("/api/items/{item_id}", response_model=ItemOut, tags=["items"])
def update_item(item_id: int, payload: ItemCreate, db: Session = Depends(get_db)):
    """Правка карточки товара (наименование, категория, цены)."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Позиция не найдена")
    if item.status == "sold":
        raise HTTPException(400, "Проданную позицию редактировать нельзя")
    # Позицию, стоящую в сборке, править можно: цена и категория меняются со склада,
    # себестоимость сборки пересчитается автоматически.
    _apply_part(item, payload)
    db.commit()
    db.refresh(item)
    return serialize_item(item)


@app.post("/api/items/{item_id}/quick-sell", response_model=ItemOut, tags=["items"])
def quick_sell_item(item_id: int, payload: Optional[SellIn] = None, db: Session = Depends(get_db)):
    """Прямая розничная продажа позиции со свободного склада."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Позиция не найдена")
    if item.status == "sold":
        raise HTTPException(400, "Позиция уже продана")
    if item.status == "in_assembly":
        raise HTTPException(400, "Позиция стоит в сборке — сначала снимите её через пересборку ПК")

    if payload is not None and payload.price is not None:
        item.retail_price = payload.price
    item.status = "sold"
    item.sold_at = (payload.sold_at if payload and payload.sold_at else None) or now()
    db.commit()
    db.refresh(item)
    return serialize_item(item)


@app.post("/api/items/{item_id}/unsell", response_model=ItemOut, tags=["items"])
def unsell_item(item_id: int, db: Session = Depends(get_db)):
    """Отмена розничной продажи — позиция возвращается на свободный склад."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Позиция не найдена")
    if item.status != "sold":
        raise HTTPException(400, "Эта позиция не продана")
    if item.pc_id is not None:
        raise HTTPException(400, "Позиция продана в составе ПК — отмените продажу сборки целиком")

    item.status = "in_stock"
    item.sold_at = None
    db.commit()
    db.refresh(item)
    return serialize_item(item)


@app.delete("/api/items/{item_id}", status_code=204, tags=["items"])
def delete_item(item_id: int, db: Session = Depends(get_db)):
    """Удаление позиции со свободного склада (для ошибок ввода)."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Позиция не найдена")
    if item.status == "sold":
        raise HTTPException(400, "Проданную позицию удалить нельзя — она нужна для отчёта. Сначала отмените продажу")
    if item.status == "in_assembly":
        raise HTTPException(400, "Позиция стоит в сборке — сначала снимите её через «Пересобрать»")

    db.delete(item)
    db.commit()
    return Response(status_code=204)


# =============================================================================
#  Excel
# =============================================================================


def _xlsx_response(data: bytes, filename: str) -> StreamingResponse:
    from urllib.parse import quote
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8\'\'{quote(filename)}"},
    )


@app.get("/api/export.xlsx", tags=["excel"])
def export_excel(db: Session = Depends(get_db)):
    """Выгрузка всей базы в Excel: три листа — сборки, склад, продажи."""
    pcs = db.scalars(select(PC).order_by(PC.id)).unique().all()
    items = db.scalars(select(Item).order_by(Item.id)).all()
    data = excel_io.build_workbook(list(pcs), list(items))
    return _xlsx_response(data, f"ASTEX Stock {now():%d.%m.%Y}.xlsx")


@app.get("/api/import-template.xlsx", tags=["excel"])
def import_template():
    """Пустой шаблон для заполнения перед загрузкой."""
    return _xlsx_response(excel_io.build_import_template(), "Шаблон загрузки склада.xlsx")


@app.post("/api/import", tags=["excel"])
async def import_excel(
    file: UploadFile = File(...),
    dry_run: bool = Query(default=False, description="Только проверить файл, ничего не создавая"),
    db: Session = Depends(get_db),
):
    """Загрузка товаров из Excel. Только добавляет — существующие позиции не трогает."""
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Нужен файл .xlsx. Сохраните таблицу в этом формате.")

    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 10 МБ — это слишком много для склада.")

    try:
        rows, notes = excel_io.parse_import(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if dry_run:
        return {
            "dry_run": True,
            "would_create": len(rows),
            "total_cost": sum(r["cost_price"] for r in rows),
            "notes": notes,
            "preview": rows[:10],
        }

    moment = now()
    created = [Item(pc_id=None, status="in_stock", purchased_at=r["purchased_at"] or moment,
                    **{k: v for k, v in r.items() if k != "purchased_at"}) for r in rows]
    db.add_all(created)
    db.commit()
    return {
        "dry_run": False,
        "created": len(created),
        "total_cost": sum(i.cost_price for i in created),
        "notes": notes,
    }


# =============================================================================
#  Аналитика
# =============================================================================


PERIOD_LABELS = {"today": "сегодня", "week": "за 7 дней", "month": "за 30 дней", "all": "за всё время"}


def _period_start(period: str) -> Optional[datetime]:
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=6)
    if period == "month":
        return today - timedelta(days=29)
    return None


@app.get("/api/sales", response_model=SalesOut, tags=["stats"])
def sales(
    period: Literal["today", "week", "month", "all", "custom"] = "all",
    date_from: Optional[datetime] = Query(default=None, description="Начало периода (для period=custom)"),
    date_to: Optional[datetime] = Query(default=None, description="Конец периода включительно"),
    db: Session = Depends(get_db),
):
    """Единая лента продаж: сборки, штучная розница и услуги, от свежих к старым."""
    if period == "custom" or date_from or date_to:
        since = date_from
        until = date_to
        if until is not None and until.hour == 0 and until.minute == 0 and until.second == 0:
            # Пришла голая дата — включаем весь этот день целиком.
            until = until + timedelta(days=1) - timedelta(seconds=1)
        label = "выбранный период"
    else:
        since = _period_start(period)
        until = None
        label = PERIOD_LABELS[period]

    feed: list[dict] = []

    pc_stmt = select(PC).where(PC.status == "sold")
    if since is not None:
        pc_stmt = pc_stmt.where(PC.sold_at >= since)
    if until is not None:
        pc_stmt = pc_stmt.where(PC.sold_at <= until)
    for pc in db.scalars(pc_stmt).unique().all():
        feed.append({
            "kind": "pc",
            "id": pc.id,
            "name": pc.name,
            "subtitle": f"сборка, {len(pc.items)} поз.",
            "sold_at": _dt(pc.sold_at),
            "revenue": pc.sell_price,
            "cost": pc.cost_price,
            "profit": pc.sell_price - pc.cost_price,
        })

    item_stmt = select(Item).where(Item.status == "sold", Item.pc_id.is_(None))
    if since is not None:
        item_stmt = item_stmt.where(Item.sold_at >= since)
    if until is not None:
        item_stmt = item_stmt.where(Item.sold_at <= until)
    for item in db.scalars(item_stmt).all():
        revenue = item.retail_price or 0
        kind_label = "услуга" if item.item_type == "service" else "розница"
        feed.append({
            "kind": "item",
            "id": item.id,
            "name": item.name,
            "subtitle": f"{kind_label}, {item.category}",
            "sold_at": _dt(item.sold_at),
            "revenue": revenue,
            "cost": item.cost_price,
            "profit": revenue - item.cost_price,
        })

    feed.sort(key=lambda s: s["sold_at"] or "", reverse=True)
    return SalesOut(
        period=label,
        date_from=_dt(since),
        date_to=_dt(until),
        sales=feed,
        revenue=sum(s["revenue"] for s in feed),
        cost=sum(s["cost"] for s in feed),
        profit=sum(s["profit"] for s in feed),
        count=len(feed),
    )


@app.get("/api/stats", response_model=StatsOut, tags=["stats"])
def stats(db: Session = Depends(get_db)):
    def scalar(stmt) -> int:
        return int(db.scalar(stmt) or 0)

    pcs_in_stock = scalar(select(func.count(PC.id)).where(PC.status == "in_stock"))
    pcs_sold = scalar(select(func.count(PC.id)).where(PC.status == "sold"))

    items_in_stock = scalar(select(func.count(Item.id)).where(Item.status == "in_stock"))
    items_in_assembly = scalar(select(func.count(Item.id)).where(Item.status == "in_assembly"))
    items_sold = scalar(select(func.count(Item.id)).where(Item.status == "sold"))

    free_stock_value = scalar(select(func.sum(Item.cost_price)).where(Item.status == "in_stock"))
    assembly_value = scalar(select(func.sum(Item.cost_price)).where(Item.status == "in_assembly"))

    # Проданные сборки: выручка — цена ПК, себестоимость — закуп его деталей.
    revenue_pcs = scalar(select(func.sum(PC.sell_price)).where(PC.status == "sold"))
    cost_pcs = scalar(
        select(func.sum(Item.cost_price)).where(Item.status == "sold", Item.pc_id.isnot(None))
    )

    # Штучные розничные продажи со свободного склада.
    revenue_items = scalar(
        select(func.sum(func.coalesce(Item.retail_price, 0))).where(
            Item.status == "sold", Item.pc_id.is_(None)
        )
    )
    cost_items = scalar(
        select(func.sum(Item.cost_price)).where(Item.status == "sold", Item.pc_id.is_(None))
    )

    return StatsOut(
        pcs_in_stock=pcs_in_stock,
        pcs_sold=pcs_sold,
        items_in_stock=items_in_stock,
        items_in_assembly=items_in_assembly,
        items_sold=items_sold,
        stock_value=free_stock_value + assembly_value,
        free_stock_value=free_stock_value,
        assembly_value=assembly_value,
        revenue=revenue_pcs + revenue_items,
        profit=(revenue_pcs - cost_pcs) + (revenue_items - cost_items),
        profit_pcs=revenue_pcs - cost_pcs,
        profit_items=revenue_items - cost_items,
    )
