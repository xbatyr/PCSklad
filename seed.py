"""Демо-данные для ASTEX Stock.

    python seed.py           # заполнить пустую базу
    python seed.py --force   # снести существующие данные и залить заново
"""

import sys

from database import SessionLocal, init_db
from models import Item, PC

# 1 готовая сборка: 8 комплектующих + монитор + мышь
DEMO_PC = {
    "name": "ASTEX Gaming 5600 / RTX 4060",
    "sell_price": 620_000,
    "parts": [
        ("component", "CPU", "AMD Ryzen 5 5600", 62_000, 78_000),
        ("component", "Motherboard", "MSI B550M PRO-VDH", 48_000, 59_000),
        ("component", "RAM", "Kingston Fury Beast 16GB DDR4 3200 (2x8)", 24_000, 32_000),
        ("component", "SSD", "Samsung 980 NVMe 1TB", 33_000, 42_000),
        ("component", "GPU", "Palit RTX 4060 Dual 8GB", 175_000, 205_000),
        ("component", "PSU", "Deepcool PK650D 650W 80+ Bronze", 26_000, 34_000),
        ("component", "Case", "Zalman i3 Neo Black", 21_000, 28_000),
        ("component", "Cooler", "ID-Cooling SE-224-XT", 12_000, 17_000),
        ("periphery", "Monitor", "Xiaomi G24i 180Hz 24\"", 58_000, 72_000),
        ("periphery", "Mouse", "Logitech G102 Lightsync", 9_500, 14_000),
    ],
}

# 3 свободные позиции на складе
DEMO_STOCK = [
    ("component", "GPU", "Gigabyte RTX 3060 Eagle 12GB", 145_000, 178_000),
    ("periphery", "Keyboard", "Redragon Kumara K552 RGB", 13_500, 19_000),
    ("periphery", "Headset", "HyperX Cloud Stinger 2", 17_000, 24_000),
]


def main() -> None:
    force = "--force" in sys.argv
    init_db()
    db = SessionLocal()
    try:
        if db.query(PC).count() or db.query(Item).count():
            if not force:
                print("В базе уже есть данные. Запустите «python seed.py --force», чтобы перезалить.")
                return
            db.query(Item).delete()
            db.query(PC).delete()
            db.commit()
            print("Старые данные удалены.")

        pc = PC(name=DEMO_PC["name"], sell_price=DEMO_PC["sell_price"], status="in_stock")
        db.add(pc)
        db.flush()

        from models import now
        moment = now()
        for item_type, category, name, cost, retail in DEMO_PC["parts"]:
            db.add(Item(
                pc_id=pc.id, item_type=item_type, category=category, name=name,
                cost_price=cost, retail_price=retail, status="in_assembly",
                purchased_at=moment,
            ))

        for item_type, category, name, cost, retail in DEMO_STOCK:
            db.add(Item(
                pc_id=None, item_type=item_type, category=category, name=name,
                cost_price=cost, retail_price=retail, status="in_stock",
                purchased_at=moment,
            ))

        db.commit()
        db.refresh(pc)
        cost = f"{pc.cost_price:,}".replace(",", " ")
        print(f"Готово: сборка «{pc.name}» — {len(pc.items)} поз., себестоимость {cost} ₸")
        print(f"        + {len(DEMO_STOCK)} свободные позиции на складе")
    finally:
        db.close()


if __name__ == "__main__":
    main()
