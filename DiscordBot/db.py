"""
MySQL data layer for the Unturned economy Discord bot (unified market model).

Shares the same database as the SellVault + RedeemCode plugins. Each call opens a
short-lived connection. All functions are synchronous — call via asyncio.to_thread(...).

Single market list (sv_market): item_id, name, price, amount (live stock), image_url.
  - buying a single item: stock -1
  - selling in-game (sell box): stock +1   (done by the plugin)
  - amount == 0  -> hidden from shop/sell-list
Packages (sv_packages / sv_package_items): bundles bought with coins (no stock).
"""
import secrets
import string
import pymysql

import config

SV = config.SV
RC = config.RC


def _conn():
    return pymysql.connect(
        host=config.DB["host"], port=config.DB["port"], db=config.DB["db"],
        user=config.DB["user"], password=config.DB["password"],
        charset="utf8mb4", autocommit=True, cursorclass=pymysql.cursors.DictCursor,
    )


def _gen_code(n: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def ensure_schema():
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS `{SV}coins` (
            `steam_id` BIGINT UNSIGNED PRIMARY KEY,
            `balance` BIGINT NOT NULL DEFAULT 0) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        f"""CREATE TABLE IF NOT EXISTS `{SV}links` (
            `steam_id` BIGINT UNSIGNED PRIMARY KEY, `discord_id` BIGINT UNSIGNED NOT NULL UNIQUE,
            `linked_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        f"""CREATE TABLE IF NOT EXISTS `{SV}link_codes` (
            `code` VARCHAR(32) PRIMARY KEY, `discord_id` BIGINT UNSIGNED NOT NULL,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        # unified market list (buy single + sell in box). amount = live stock.
        f"""CREATE TABLE IF NOT EXISTS `{SV}market` (
            `item_id` INT UNSIGNED PRIMARY KEY, `name` VARCHAR(64) NOT NULL,
            `price` DOUBLE NOT NULL DEFAULT 0, `amount` INT NOT NULL DEFAULT 0,
            `image_url` VARCHAR(512) NULL, `enabled` TINYINT(1) NOT NULL DEFAULT 1)
            ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        f"""CREATE TABLE IF NOT EXISTS `{SV}packages` (
            `id` INT AUTO_INCREMENT PRIMARY KEY, `name` VARCHAR(64) NOT NULL,
            `price` BIGINT NOT NULL, `image_url` VARCHAR(512) NULL,
            `enabled` TINYINT(1) NOT NULL DEFAULT 1) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        f"""CREATE TABLE IF NOT EXISTS `{SV}package_items` (
            `id` INT AUTO_INCREMENT PRIMARY KEY, `package_id` INT NOT NULL,
            `item_id` INT UNSIGNED NOT NULL, `amount` INT UNSIGNED NOT NULL DEFAULT 1,
            INDEX `idx_pkg` (`package_id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        f"""CREATE TABLE IF NOT EXISTS `{SV}market_log` (
            `id` INT AUTO_INCREMENT PRIMARY KEY, `steam_id` BIGINT UNSIGNED NOT NULL,
            `item_id` INT UNSIGNED NOT NULL, `amount` INT NOT NULL, `coins` BIGINT NOT NULL,
            `kind` VARCHAR(8) NOT NULL, `at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_steam` (`steam_id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        f"""CREATE TABLE IF NOT EXISTS `{RC}codes` (
            `id` INT AUTO_INCREMENT PRIMARY KEY, `code` VARCHAR(64) NOT NULL UNIQUE,
            `max_uses` INT NOT NULL DEFAULT 0, `uses` INT NOT NULL DEFAULT 0,
            `enabled` TINYINT(1) NOT NULL DEFAULT 1, `expires_at` DATETIME NULL,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        f"""CREATE TABLE IF NOT EXISTS `{RC}code_items` (
            `id` INT AUTO_INCREMENT PRIMARY KEY, `code_id` INT NOT NULL,
            `item_id` INT UNSIGNED NOT NULL, `amount` INT UNSIGNED NOT NULL DEFAULT 1,
            INDEX `idx_code` (`code_id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
    ]
    with _conn() as c, c.cursor() as cur:
        for s in stmts:
            cur.execute(s)


# ---------- linking ----------

def is_discord_linked(discord_id: int) -> bool:
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT 1 FROM `{SV}links` WHERE discord_id=%s LIMIT 1;", (discord_id,))
        return cur.fetchone() is not None


def create_link_code(discord_id: int) -> str:
    code = _gen_code()
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"DELETE FROM `{SV}link_codes` WHERE discord_id=%s;", (discord_id,))
        cur.execute(f"INSERT INTO `{SV}link_codes` (code, discord_id) VALUES (%s,%s);", (code, discord_id))
    return code


def get_steam_by_discord(discord_id: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT steam_id FROM `{SV}links` WHERE discord_id=%s LIMIT 1;", (discord_id,))
        row = cur.fetchone()
        return int(row["steam_id"]) if row else None


# ---------- coins ----------

def get_coins(steam_id: int) -> int:
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT balance FROM `{SV}coins` WHERE steam_id=%s LIMIT 1;", (steam_id,))
        row = cur.fetchone()
        return int(row["balance"]) if row else 0


def add_coins(steam_id: int, delta: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"INSERT INTO `{SV}coins` (steam_id, balance) VALUES (%s,%s) "
                    f"ON DUPLICATE KEY UPDATE balance = balance + %s;", (steam_id, delta, delta))


def top_coins(limit: int = 10):
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT co.steam_id, co.balance, li.discord_id "
                    f"FROM `{SV}coins` co LEFT JOIN `{SV}links` li ON li.steam_id = co.steam_id "
                    f"ORDER BY co.balance DESC LIMIT %s;", (limit,))
        return cur.fetchall()


# ---------- market (single list: buy single + sell in box) ----------

def list_market(include_hidden: bool = False, exclude_ids=None, only_ids=None):
    """List market items. exclude_ids/only_ids are iterables of item_ids to filter on."""
    clauses = ["enabled=1"] if include_hidden else ["enabled=1", "amount > 0"]
    params = []
    if only_ids:
        ids = tuple(int(i) for i in only_ids)
        if not ids:
            return []
        clauses.append(f"item_id IN ({','.join(['%s'] * len(ids))})")
        params.extend(ids)
    elif exclude_ids:
        ids = tuple(int(i) for i in exclude_ids)
        if ids:
            clauses.append(f"item_id NOT IN ({','.join(['%s'] * len(ids))})")
            params.extend(ids)
    where = "WHERE " + " AND ".join(clauses)
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT item_id, name, price, amount, image_url FROM `{SV}market` {where} "
                    f"ORDER BY price ASC, name ASC;", params)
        return cur.fetchall()


def get_market(item_id: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT item_id, name, price, amount, image_url, enabled "
                    f"FROM `{SV}market` WHERE item_id=%s LIMIT 1;", (item_id,))
        return cur.fetchone()


def upsert_market(item_id: int, name: str, price: float, amount: int, image_url=None):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"INSERT INTO `{SV}market` (item_id, name, price, amount, image_url, enabled) "
            f"VALUES (%s,%s,%s,%s,%s,1) ON DUPLICATE KEY UPDATE "
            f"name=%s, price=%s, amount=%s, image_url=COALESCE(%s,image_url), enabled=1;",
            (item_id, name, price, amount, image_url, name, price, amount, image_url))


def remove_market(item_id: int) -> bool:
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"DELETE FROM `{SV}market` WHERE item_id=%s;", (item_id,))
        return cur.rowcount > 0


def buy_market(discord_id: int, item_id: int):
    """
    Buy ONE unit of a market item with coins; stock -1; create a 1-use redeem code.
    Returns (status, payload): not_linked / no_item / out_of_stock / ('insufficient', balance) / ('ok', code)
    """
    steam_id = get_steam_by_discord(discord_id)
    if steam_id is None:
        return ("not_linked", None)

    conn = _conn()
    conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT name, price, amount FROM `{SV}market` WHERE item_id=%s AND enabled=1 FOR UPDATE;",
                        (item_id,))
            it = cur.fetchone()
            if not it:
                conn.rollback(); return ("no_item", None)
            if int(it["amount"]) <= 0:
                conn.rollback(); return ("out_of_stock", None)

            price = int(round(float(it["price"])))
            cur.execute(f"SELECT balance FROM `{SV}coins` WHERE steam_id=%s FOR UPDATE;", (steam_id,))
            row = cur.fetchone()
            balance = int(row["balance"]) if row else 0
            if balance < price:
                conn.rollback(); return ("insufficient", balance)

            cur.execute(f"UPDATE `{SV}coins` SET balance = balance - %s WHERE steam_id=%s AND balance >= %s;",
                        (price, steam_id, price))
            if cur.rowcount == 0:
                conn.rollback(); return ("insufficient", balance)

            cur.execute(f"UPDATE `{SV}market` SET amount = amount - 1 WHERE item_id=%s AND amount > 0;", (item_id,))
            if cur.rowcount == 0:
                conn.rollback(); return ("out_of_stock", None)

            code = _gen_code()
            cur.execute(f"INSERT INTO `{RC}codes` (code, max_uses) VALUES (%s, 1);", (code,))
            code_id = cur.lastrowid
            cur.execute(f"INSERT INTO `{RC}code_items` (code_id, item_id, amount) VALUES (%s,%s,1);",
                        (code_id, item_id))
            cur.execute(f"INSERT INTO `{SV}market_log` (steam_id,item_id,amount,coins,kind) "
                        f"VALUES (%s,%s,1,%s,'buy');", (steam_id, item_id, price))
        conn.commit()
        return ("ok", code)
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def buy_basket(discord_id: int, items: dict):
    """
    Buy several market items at once. items: {item_id: qty}.
    Atomically checks stock + total coins, deducts, reduces stock, creates ONE redeem code.
    Returns (status, payload):
      not_linked / empty / ('no_item', item_id) / ('out_of_stock', name) /
      ('insufficient', balance) / ('ok', code)
    """
    steam_id = get_steam_by_discord(discord_id)
    if steam_id is None:
        return ("not_linked", None)
    items = {int(k): int(v) for k, v in items.items() if int(v) > 0}
    if not items:
        return ("empty", None)

    conn = _conn(); conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            total = 0
            details = []  # (item_id, qty, cost)
            for item_id, qty in items.items():
                cur.execute(f"SELECT name, price, amount FROM `{SV}market` WHERE item_id=%s AND enabled=1 FOR UPDATE;",
                            (item_id,))
                row = cur.fetchone()
                if not row:
                    conn.rollback(); return ("no_item", item_id)
                if int(row["amount"]) < qty:
                    conn.rollback(); return ("out_of_stock", row["name"])
                cost = int(round(float(row["price"]))) * qty
                total += cost
                details.append((item_id, qty, cost))

            cur.execute(f"SELECT balance FROM `{SV}coins` WHERE steam_id=%s FOR UPDATE;", (steam_id,))
            r = cur.fetchone()
            balance = int(r["balance"]) if r else 0
            if balance < total:
                conn.rollback(); return ("insufficient", balance)

            cur.execute(f"UPDATE `{SV}coins` SET balance = balance - %s WHERE steam_id=%s AND balance >= %s;",
                        (total, steam_id, total))
            if cur.rowcount == 0:
                conn.rollback(); return ("insufficient", balance)

            for item_id, qty, _cost in details:
                cur.execute(f"UPDATE `{SV}market` SET amount = amount - %s WHERE item_id=%s AND amount >= %s;",
                            (qty, item_id, qty))
                if cur.rowcount == 0:
                    conn.rollback(); return ("out_of_stock", item_id)

            code = _gen_code()
            cur.execute(f"INSERT INTO `{RC}codes` (code, max_uses) VALUES (%s, 1);", (code,))
            code_id = cur.lastrowid
            for item_id, qty, cost in details:
                cur.execute(f"INSERT INTO `{RC}code_items` (code_id, item_id, amount) VALUES (%s,%s,%s);",
                            (code_id, item_id, qty))
                cur.execute(f"INSERT INTO `{SV}market_log` (steam_id,item_id,amount,coins,kind) "
                            f"VALUES (%s,%s,%s,%s,'buy');", (steam_id, item_id, qty, cost))
        conn.commit()
        return ("ok", code)
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


# ---------- packages (bundles bought with coins) ----------

def list_packages():
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT id, name, price, image_url FROM `{SV}packages` WHERE enabled=1 ORDER BY price ASC;")
        return cur.fetchall()


def get_package(package_id: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT id, name, price, image_url FROM `{SV}packages` WHERE id=%s AND enabled=1 LIMIT 1;",
                    (package_id,))
        pkg = cur.fetchone()
        if not pkg:
            return None
        cur.execute(f"SELECT item_id, amount FROM `{SV}package_items` WHERE package_id=%s;", (package_id,))
        pkg["items"] = cur.fetchall()
        return pkg


def create_package(name: str, price: int, items, image_url=None) -> int:
    """items: list of (item_id, amount)."""
    conn = _conn(); conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO `{SV}packages` (name, price, image_url) VALUES (%s,%s,%s);",
                        (name, price, image_url))
            pid = cur.lastrowid
            for item_id, amount in items:
                cur.execute(f"INSERT INTO `{SV}package_items` (package_id, item_id, amount) VALUES (%s,%s,%s);",
                            (pid, item_id, amount))
        conn.commit()
        return pid
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def remove_package(package_id: int) -> bool:
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"DELETE FROM `{SV}package_items` WHERE package_id=%s;", (package_id,))
        cur.execute(f"DELETE FROM `{SV}packages` WHERE id=%s;", (package_id,))
        return cur.rowcount > 0


def buy_package(discord_id: int, package_id: int):
    """
    Buy a package with coins; create a 1-use redeem code containing all its items.
    Returns (status, payload): not_linked / no_item / ('insufficient', balance) / ('ok', code)
    """
    steam_id = get_steam_by_discord(discord_id)
    if steam_id is None:
        return ("not_linked", None)

    conn = _conn(); conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT price FROM `{SV}packages` WHERE id=%s AND enabled=1 LIMIT 1;", (package_id,))
            pkg = cur.fetchone()
            if not pkg:
                conn.rollback(); return ("no_item", None)
            price = int(pkg["price"])
            cur.execute(f"SELECT item_id, amount FROM `{SV}package_items` WHERE package_id=%s;", (package_id,))
            items = cur.fetchall()
            if not items:
                conn.rollback(); return ("no_item", None)

            cur.execute(f"SELECT balance FROM `{SV}coins` WHERE steam_id=%s FOR UPDATE;", (steam_id,))
            row = cur.fetchone()
            balance = int(row["balance"]) if row else 0
            if balance < price:
                conn.rollback(); return ("insufficient", balance)

            cur.execute(f"UPDATE `{SV}coins` SET balance = balance - %s WHERE steam_id=%s AND balance >= %s;",
                        (price, steam_id, price))
            if cur.rowcount == 0:
                conn.rollback(); return ("insufficient", balance)

            code = _gen_code()
            cur.execute(f"INSERT INTO `{RC}codes` (code, max_uses) VALUES (%s, 1);", (code,))
            code_id = cur.lastrowid
            for it in items:
                cur.execute(f"INSERT INTO `{RC}code_items` (code_id, item_id, amount) VALUES (%s,%s,%s);",
                            (code_id, int(it["item_id"]), int(it["amount"])))
        conn.commit()
        return ("ok", code)
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


# ---------- redeem codes (admin manual) ----------

def create_redeem_code(max_uses: int, items, code: str = None) -> str:
    """items: list of (item_id, amount). Returns the code."""
    if not code:
        code = _gen_code()
    conn = _conn(); conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO `{RC}codes` (code, max_uses) VALUES (%s,%s);", (code, max_uses))
            code_id = cur.lastrowid
            for item_id, amount in items:
                cur.execute(f"INSERT INTO `{RC}code_items` (code_id, item_id, amount) VALUES (%s,%s,%s);",
                            (code_id, item_id, amount))
        conn.commit()
        return code
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


# ---------- coin transfer ----------

def transfer_coins(from_discord: int, to_discord: int, amount: int):
    if amount <= 0:
        return ("bad_amount", None)
    from_steam = get_steam_by_discord(from_discord)
    if from_steam is None:
        return ("not_linked_self", None)
    to_steam = get_steam_by_discord(to_discord)
    if to_steam is None:
        return ("not_linked_target", None)
    if from_steam == to_steam:
        return ("self", None)

    conn = _conn(); conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT balance FROM `{SV}coins` WHERE steam_id=%s FOR UPDATE;", (from_steam,))
            row = cur.fetchone()
            bal = int(row["balance"]) if row else 0
            if bal < amount:
                conn.rollback(); return ("insufficient", bal)
            cur.execute(f"UPDATE `{SV}coins` SET balance = balance - %s WHERE steam_id=%s AND balance >= %s;",
                        (amount, from_steam, amount))
            if cur.rowcount == 0:
                conn.rollback(); return ("insufficient", bal)
            cur.execute(f"INSERT INTO `{SV}coins` (steam_id, balance) VALUES (%s,%s) "
                        f"ON DUPLICATE KEY UPDATE balance = balance + %s;", (to_steam, amount, amount))
        conn.commit()
        return ("ok", bal - amount)
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
