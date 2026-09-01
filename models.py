"""ORM-модели ASTEX Stock."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from database import Base

# --- Справочники -------------------------------------------------------------

ITEM_TYPES = ("component", "periphery", "service")

COMPONENT_CATEGORIES = ("CPU", "GPU", "RAM", "SSD", "HDD", "Motherboard", "PSU", "Case", "Cooler")
PERIPHERY_CATEGORIES = ("Monitor", "Mouse", "Keyboard", "Headset", "Speakers", "Microphone", "Mousepad")
SERVICE_CATEGORIES = ("Repair", "Maintenance", "Assembly", "Diagnostics", "Software")
CATEGORIES = COMPONENT_CATEGORIES + PERIPHERY_CATEGORIES + SERVICE_CATEGORIES + ("Other",)

PC_STATUSES = ("in_stock", "sold")
ITEM_STATUSES = ("in_stock", "in_assembly", "sold")


def now() -> datetime:
    """Локальное время магазина (наивный datetime) — так даты корректно читаются в UI."""
    return datetime.now()


# --- Таблицы -----------------------------------------------------------------


class PC(Base):
    """Готовый ПК или сетап, собранный из позиций таблицы items."""

    __tablename__ = "pcs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    sell_price = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="in_stock", index=True)
    created_at = Column(DateTime, nullable=False, default=now)
    updated_at = Column(DateTime, nullable=False, default=now, onupdate=now)
    sold_at = Column(DateTime, nullable=True)

    items = relationship(
        "Item",
        back_populates="pc",
        order_by="Item.id",
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint("status IN ('in_stock', 'sold')", name="ck_pcs_status"),
    )

    @property
    def cost_price(self) -> int:
        """Себестоимость сборки — сумма закупа всех привязанных позиций."""
        return sum(item.cost_price for item in self.items)

    @property
    def margin(self) -> int:
        return self.sell_price - self.cost_price


class Item(Base):
    """Комплектующая, периферия или услуга: на складе, в сборке или продана."""

    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    pc_id = Column(Integer, ForeignKey("pcs.id", ondelete="SET NULL"), nullable=True, index=True)
    item_type = Column(String(20), nullable=False, default="component")
    category = Column(String(40), nullable=False, default="Other")
    name = Column(String(200), nullable=False)
    cost_price = Column(Integer, nullable=False, default=0)
    retail_price = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="in_stock", index=True)

    # Откуда пришла позиция: «б/у системник от клиента, 12.03», номер закупа и т.п.
    source_note = Column(Text, nullable=True)

    purchased_at = Column(DateTime, nullable=True)   # дата закупа, задаётся вручную
    created_at = Column(DateTime, nullable=False, default=now)
    updated_at = Column(DateTime, nullable=False, default=now, onupdate=now)
    sold_at = Column(DateTime, nullable=True)

    pc = relationship("PC", back_populates="items")

    __table_args__ = (
        CheckConstraint(
            "item_type IN ('component', 'periphery', 'service')", name="ck_items_type"
        ),
        CheckConstraint("status IN ('in_stock', 'in_assembly', 'sold')", name="ck_items_status"),
        Index("ix_items_status_type", "status", "item_type"),
    )
