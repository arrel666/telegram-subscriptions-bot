"""Configuration without external packages or shell evaluation."""
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re

ROOT = Path(__file__).resolve().parent


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError("В .env ожидается формат KEY=value")
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    owner_id: int
    support: str
    title: str = "Подписки"
    seller: str = ""
    payments_enabled: bool = False
    data_dir: Path = ROOT / "data"

    @classmethod
    def load(cls):
        values = {**read_env(ROOT / ".env"), **os.environ}
        token = values.get("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажите BOT_TOKEN: сначала запустите python3 configure.py")
        data_dir = Path(values.get("DATA_DIR") or str(ROOT / "data"))
        saved = {}
        if not values.get("OWNER_ID") or not values.get("SUPPORT_USERNAME"):
            owner_file = data_dir / "owner.json"
            if owner_file.exists():
                saved = json.loads(owner_file.read_text(encoding="utf-8"))
                if str(saved.get("bot_id")) != token.split(":", 1)[0]:
                    raise ValueError("Сохранённый администратор относится к другому боту")
        owner = int(values.get("OWNER_ID") or saved.get("owner_id", 0))
        support = (values.get("SUPPORT_USERNAME") or saved.get("support", "")).strip().lstrip("@")
        if owner <= 0 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", support):
            raise ValueError("Нужны OWNER_ID и SUPPORT_USERNAME администратора")
        enabled = values.get("PAYMENTS_ENABLED", "false").lower()
        if enabled not in ("true", "false"):
            raise ValueError("PAYMENTS_ENABLED должен быть true или false")
        seller = values.get("SELLER_DETAILS", "").strip()
        title = values.get("SHOP_TITLE", "Подписки").strip()
        if not 1 <= len(title) <= 60 or len(seller) > 500:
            raise ValueError("Проверьте длину SHOP_TITLE и SELLER_DETAILS")
        if enabled == "true" and not seller:
            raise ValueError("Перед включением оплаты заполните SELLER_DETAILS")
        return cls(token, owner, support, title, seller, enabled == "true",
                   data_dir)
