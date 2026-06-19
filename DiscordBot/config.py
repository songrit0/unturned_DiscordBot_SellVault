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

# Item ids treated as cash bills — separated from /shop into a dedicated bills-shop channel.
# Must match the plugin's NoCommissionItemIds so sells pay full face value.
BILL_ITEM_IDS = tuple(int(x) for x in os.getenv("BILL_ITEM_IDS", "4254,4255,4256,4257,4258").split(",") if x.strip())

# Web shop URL — shown in welcome embed, commands embed, and as a clickable link button.
WEB_SHOP_URL = os.getenv("WEB_SHOP_URL", "https://meowpow.shop")

# Periodic Discord-username backfill: how often the bot sweeps sv_links rows whose
# discord_username is NULL and fills them via the Discord HTTP API. 0 disables the loop
# (CLI `python -m backfill_usernames` still works).
USERNAME_BACKFILL_INTERVAL_MIN = _int("USERNAME_BACKFILL_INTERVAL_MIN", 30)

# P2P market feed: the bot auto-announces new player listings (sv_p2p_listings) to this
# Discord channel and attaches a Buy button. 0 disables the announce loop.
P2P_FEED_CHANNEL_ID = _int("P2P_FEED_CHANNEL_ID")

# In-game chat webhook relay channel: bot replies with an EN->TH translation under any
# webhook message shaped "💬 Name: text". 0 disables it.
GAME_CHAT_CHANNEL_ID = _int("GAME_CHAT_CHANNEL_ID", 1508058571533979748)

# Shop-api base URL the bot calls to execute a P2P buy (logic stays server-side). Stable
# reserved-ngrok domain in prod. Empty disables the Buy button's API call.
API_BASE_URL = os.getenv("API_BASE_URL", "").rstrip("/")

# Shared secret sent as the X-Bot-Secret header so the api's BotGuard trusts bot calls.
# No default — must be set in .env to match the api's BOT_API_SECRET.
BOT_API_SECRET = os.getenv("BOT_API_SECRET", "")

# VIP role sync: the bot keeps a single Discord role in lockstep with VIP status in
# sv_vip_grants (any active grant -> has the role; otherwise -> role removed). Operates on
# GUILD_ID. Requires the privileged Server Members intent (portal + code), the bot to have
# Manage Roles, and the bot's top role to sit ABOVE VIP_ROLE_ID in the role hierarchy.
VIP_ROLE_ID = _int("VIP_ROLE_ID", 1515091339597975794)        # role given to active VIPs
VIP_ROLE_SYNC_INTERVAL_MIN = _int("VIP_ROLE_SYNC_INTERVAL_MIN", 5)  # reconcile period
VIP_ROLE_SYNC_ENABLED = _int("VIP_ROLE_SYNC_ENABLED", 1)      # 0 disables the loop entirely
# When 1, the loop logs intended add/remove without touching the Discord API (validation only).
VIP_ROLE_SYNC_DRYRUN = _int("VIP_ROLE_SYNC_DRYRUN", 0)
