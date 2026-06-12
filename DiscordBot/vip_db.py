"""
VIP data layer for the Discord bot — buying VIP with coins.

Shares the database with the VIP Unturned plugin (sv_vip_* tables) and the SellVault
coins (sv_coins). buy_vip() runs in ONE transaction so coins are never deducted without
the grant being written. All VIP times are UTC (matching the plugin's UTC_TIMESTAMP()).
"""
import pymysql

import config

SV = config.SV
PACKAGES = f"{SV}vip_packages"
GRANTS = f"{SV}vip_grants"
LOG = f"{SV}vip_log"
COINS = f"{SV}coins"


def _conn(autocommit: bool = True):
    return pymysql.connect(
        host=config.DB["host"], port=config.DB["port"], db=config.DB["db"],
        user=config.DB["user"], password=config.DB["password"],
        charset="utf8mb4", autocommit=autocommit, cursorclass=pymysql.cursors.DictCursor,
    )


def list_packages(enabled_only: bool = True) -> list[dict]:
    where = "WHERE enabled=1" if enabled_only else ""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT id, tier, group_id, days, price_coins, label, sort, enabled "
            f"FROM `{PACKAGES}` {where} ORDER BY sort ASC, price_coins ASC;"
        )
        return list(cur.fetchall())


def list_active_vip_steam_ids() -> list[int]:
    """Every steam_id with at least one currently-active VIP grant (any tier/group).

    Used by the Discord role-sync loop to compute who should hold the VIP role."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT steam_id FROM `{GRANTS}` "
            f"WHERE active=1 AND expires_at > UTC_TIMESTAMP();"
        )
        return [int(r["steam_id"]) for r in cur.fetchall()]


def get_active_grants(steam_id: int) -> list[dict]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT group_id, expires_at FROM `{GRANTS}` "
            f"WHERE steam_id=%s AND active=1 AND expires_at > UTC_TIMESTAMP() "
            f"ORDER BY expires_at DESC;",
            (steam_id,),
        )
        return list(cur.fetchall())


def buy_vip(steam_id: int, package_id: int, actor: str) -> dict:
    """
    Atomically: verify enabled package, verify coins >= price, deduct coins, extend the grant
    (new expiry = later of now / current expiry + days), log 'buy'.

    Returns one of:
      {"ok": True, "tier", "group_id", "days", "price", "balance", "expires_at"}
      {"ok": False, "reason": "package" | "coins", "need"?, "balance"?}
    """
    conn = _conn(autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, tier, group_id, days, price_coins FROM `{PACKAGES}` "
                f"WHERE id=%s AND enabled=1 FOR UPDATE;",
                (package_id,),
            )
            pkg = cur.fetchone()
            if not pkg:
                conn.rollback()
                return {"ok": False, "reason": "package"}

            price = int(pkg["price_coins"])
            days = int(pkg["days"])
            group_id = pkg["group_id"]

            cur.execute(f"SELECT balance FROM `{COINS}` WHERE steam_id=%s FOR UPDATE;", (steam_id,))
            row = cur.fetchone()
            balance = int(row["balance"]) if row else 0
            if balance < price:
                conn.rollback()
                return {"ok": False, "reason": "coins", "need": price, "balance": balance}

            # deduct coins (row guaranteed to exist if balance>=price>0; upsert to be safe)
            cur.execute(
                f"INSERT INTO `{COINS}` (steam_id, balance) VALUES (%s, %s) "
                f"ON DUPLICATE KEY UPDATE balance = balance - %s;",
                (steam_id, -price, price),
            )

            # extend / create the grant
            cur.execute(
                f"INSERT INTO `{GRANTS}` (steam_id, group_id, expires_at, active) "
                f"VALUES (%s, %s, DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s DAY), 1) "
                f"ON DUPLICATE KEY UPDATE "
                f"  expires_at = DATE_ADD(GREATEST(UTC_TIMESTAMP(), "
                f"    CASE WHEN active=1 THEN expires_at ELSE UTC_TIMESTAMP() END), INTERVAL %s DAY), "
                f"  active = 1;",
                (steam_id, group_id, days, days),
            )

            cur.execute(
                f"SELECT expires_at FROM `{GRANTS}` WHERE steam_id=%s AND group_id=%s LIMIT 1;",
                (steam_id, group_id),
            )
            expires_at = cur.fetchone()["expires_at"]

            cur.execute(
                f"INSERT INTO `{LOG}` (steam_id, group_id, action, days, actor) "
                f"VALUES (%s, %s, 'buy', %s, %s);",
                (steam_id, group_id, days, actor[:64]),
            )

        conn.commit()
        return {
            "ok": True, "tier": pkg["tier"], "group_id": group_id, "days": days,
            "price": price, "balance": balance - price, "expires_at": expires_at,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
