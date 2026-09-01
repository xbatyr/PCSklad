"""ASTEX Stock — учёт склада, сборки ПК, пересборка (swap) и розничные продажи.

Запуск:  uvicorn main:app --reload
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

import models
from database import get_db, init_db
from models import CATEGORIES, Item, PC, now


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ASTEX Stock", version="1.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


# =============================================================================
#  Схемы
# =============================================================================

ItemType = Literal["component", "periphery"]
ItemStatus = Literal["in_stock", "in_assembly", "sold"]


class ItemIn(BaseModel):
    """Позиция внутри сборки. id != None — существующая деталь (правка / привязка со склада)."""

    id: Optional[int] = None
    item_type: ItemType = "component"
    category: str = "Other"
    name: str = Field(min_length=1, max_length=200)
    cost_price: int = Field(default=0, ge=0)
    retail_price: Optional[int] = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Наименование не может быть пустым")
        return v

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str) -> str:
        v = (v or "").strip() or "Other"
        if v not in CATEGORIES:
            raise ValueError(f"Неизвестная категория: {v}")
        return v


class ItemCreate(ItemIn):
    """Оприходование одиночного товара на свободный склад."""

    id: Optional[int] = Field(default=None, exclude=True)


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
    """Необязательное тело для продажи: фактическая цена сделки."""

    price: Optional[int] = Field(default=None, ge=0)


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
    created_at: Optional[str] = None
    sold_at: Optional[str] = None


class PCOut(BaseModel):
    id: int
    name: str
    sell_price: int
    status: str
    created_at: Optional[str]
    sold_at: Optional[str]
    cost_price: int
    margin: int
    items: list[ItemOut]


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
        "created_at": _dt(item.created_at),
        "sold_at": _dt(item.sold_at),
    }


def serialize_pc(pc: PC) -> dict:
    return {
        "id": pc.id,
        "name": pc.name,
        "sell_price": pc.sell_price,
        "status": pc.status,
        "created_at": _dt(pc.created_at),
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

    moment = now()
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


# =============================================================================
#  Свободный склад и розница
# =============================================================================


@app.get("/api/items", response_model=list[ItemOut], tags=["items"])
def list_items(
    type: Literal["all", "component", "periphery"] = "all",
    status: Literal["all", "in_stock", "in_assembly", "sold"] = "all",
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
    if q:
        stmt = stmt.where(Item.name.ilike(f"%{q.strip()}%"))
    return [serialize_item(item, pc_name=pc_name) for item, pc_name in db.execute(stmt).all()]


@app.post("/api/items", response_model=ItemOut, status_code=201, tags=["items"])
def create_item(payload: ItemCreate, db: Session = Depends(get_db)):
    """Оприходование одиночного товара на свободный склад."""
    item = Item(
        pc_id=None,
        item_type=payload.item_type,
        category=payload.category,
        name=payload.name,
        cost_price=payload.cost_price,
        retail_price=payload.retail_price,
        status="in_stock",
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
    item.sold_at = now()
    db.commit()
    db.refresh(item)
    return serialize_item(item)


# =============================================================================
#  Аналитика
# =============================================================================


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
