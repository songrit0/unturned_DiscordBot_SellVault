"""Loads configuration from the .env file."""
import os
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = _int("GUILD_ID")
ADMIN_ROLE_ID = _int("ADMIN_ROLE_ID")

DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=_int("DB_PORT", 3306),
    db=os.getenv("DB_NAME", "unturned"),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
)

SV = os.getenv("SV_PREFIX", "sv_")
RC = os.getenv("RC_PREFIX", "rc_")
COIN_NAME = os.getenv("COIN_NAME", "Coin")

# Base commission % (match SellVault's BaseCommissionPercent) — used only to DISPLAY estimated
# sell payouts in the catalog.
SELL_COMMISSION = float(os.getenv("SELL_COMMISSION", "40"))
