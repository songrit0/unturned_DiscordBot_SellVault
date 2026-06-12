"""
MySQL data layer for the Unturned economy Discord bot (unified market model).

Shares the same database as the SellVault + RedeemCode plugins. Each call opens a
short-lived connection. All functions are synchronous — call via asyncio.to_thread(...).

Single market list (sv_market): item_id, name, price, amount (live stock), image_url.
  - buying a single item: stock -1
  - selling in-game (sell box): stock +1   (done by the plugin)
  - amount == 0  -> hidden from shop/sell-list
Packages (sv_packages / sv_package_items): bundles bought with coins (no stock).
Quests (sv_quests / sv_quest_items / sv_quest_progress / sv_quest_completions) +
item types (sv_item_types): DDL owned by shop-api; this module only does CRUD/SELECT.
"""
import datetime
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
            `image_url` VARCHAR(512) NULL, `enabled` TINYINT(1) NOT NULL DEFAULT 1,
            `enabled_isforsell` TINYINT(1) NOT NULL DEFAULT 1)
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
        # Fail fast if the api migration that added discord_username / discord_global_name /
        # discord_username_updated_at has not been deployed. The bot assumes those columns
        # exist when running the backfill loop and CLI; better to halt on boot than to crash
        # mid-tick.
        cur.execute(
            f"SELECT `discord_username`, `discord_global_name`, `discord_username_updated_at` "
            f"FROM `{SV}links` LIMIT 0;"
        )


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


def get_discord_by_steam(steam_id: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT discord_id FROM `{SV}links` WHERE steam_id=%s LIMIT 1;", (steam_id,))
        row = cur.fetchone()
        return int(row["discord_id"]) if row else None


def all_links() -> list[dict]:
    """Every steam<->discord link as {steam_id, discord_id} (both ints).

    Used by the VIP role-sync loop to map active-VIP steam_ids to Discord members."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT steam_id, discord_id FROM `{SV}links`;")
        return [{"steam_id": int(r["steam_id"]), "discord_id": int(r["discord_id"])}
                for r in cur.fetchall()]


def count_links_missing_username() -> int:
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM `{SV}links` WHERE `discord_username` IS NULL;")
        row = cur.fetchone()
        return int(row["n"]) if row else 0


def select_link_ids_missing_username(limit: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT `discord_id` FROM `{SV}links` "
            f"WHERE `discord_username` IS NULL ORDER BY `discord_id` LIMIT %s;",
            (int(limit),),
        )
        return [int(r["discord_id"]) for r in cur.fetchall()]


def update_link_username(discord_id: int, username, global_name):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"UPDATE `{SV}links` SET `discord_username`=%s, `discord_global_name`=%s, "
            f"`discord_username_updated_at`=NOW() WHERE `discord_id`=%s;",
            (username, global_name, int(discord_id)),
        )


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

def list_market(include_hidden: bool = False, exclude_ids=None, only_ids=None, type_id=None):
    """List market items.
    - include_hidden=True  -> ALL rows (admin manage view; both for-sale and buy-only).
    - include_hidden=False -> shop view: FOR-SALE items only (enabled_isforsell=1) in stock.
    Note: `enabled` = shop buys it (sell-to-shop), `enabled_isforsell` = shop sells it (Shop).
    exclude_ids/only_ids filter item_ids; type_id filters by sv_items.type_id."""
    clauses = []
    params = []
    if not include_hidden:
        clauses.append("m.enabled_isforsell = 1")
        clauses.append("m.amount > 0")
    if only_ids:
        ids = tuple(int(i) for i in only_ids)
        if not ids:
            return []
        clauses.append(f"m.item_id IN ({','.join(['%s'] * len(ids))})")
        params.extend(ids)
    elif exclude_ids:
        ids = tuple(int(i) for i in exclude_ids)
        if ids:
            clauses.append(f"m.item_id NOT IN ({','.join(['%s'] * len(ids))})")
            params.extend(ids)
    if type_id is not None:
        clauses.append("i.type_id = %s")
        params.append(int(type_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as c, c.cursor() as cur:
        # name/image_url/type_id ย้ายไปอยู่ที่ sv_items (master catalog) แล้ว — JOIN เพื่อแสดง
        cur.execute(
            f"SELECT m.item_id, i.name, m.price, m.amount, i.image_url, "
            f"i.type_id, t.name AS type_name, m.enabled, m.enabled_isforsell "
            f"FROM `{SV}market` m "
            f"LEFT JOIN `{SV}items` i ON i.id = m.item_id "
            f"LEFT JOIN `{SV}item_types` t ON t.id = i.type_id "
            f"{where} ORDER BY m.price ASC, i.name ASC;", params)
        return cur.fetchall()


def get_market(item_id: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT m.item_id, i.name, m.price, m.base_price, m.target_stock, m.elasticity, "
            f"m.amount, i.image_url, m.enabled, i.type_id, i.description "
            f"FROM `{SV}market` m LEFT JOIN `{SV}items` i ON i.id = m.item_id "
            f"WHERE m.item_id=%s LIMIT 1;", (item_id,))
        return cur.fetchone()


def item_exists(item_id: int) -> bool:
    """เช็คว่ามีแถวใน sv_items (master catalog) — ใช้ก่อน upsert sv_market"""
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT 1 FROM `{SV}items` WHERE id=%s LIMIT 1;", (int(item_id),))
        return cur.fetchone() is not None


def upsert_market_item(item_id: int, base_price: float, amount: int,
                       target_stock: int = None, elasticity: float = 1.0):
    """
    Upsert ของในตลาดด้วยโมเดลราคา supply/demand:
      - base_price: ราคาฐาน (anchor) ที่ pricing cron ของ API ใช้
      - target_stock: สต็อกเป้าหมาย (ค่าเริ่มต้น = stock ปัจจุบัน หรือ 100)
      - elasticity: ความยืดหยุ่นราคา (default 1.0)
    name/image_url/type_id อยู่ที่ sv_items แล้ว — ต้องเพิ่มที่ master catalog ก่อนเรียกฟังก์ชันนี้
    price ตั้งค่าเริ่มต้น = base_price เพื่อกันไม่ให้เป็น 0 ก่อน cron ของ API tick แรก
    """
    if target_stock is None or target_stock <= 0:
        target_stock = amount if amount > 0 else 100
    cols = "item_id, price, base_price, target_stock, elasticity, amount, enabled"
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"INSERT INTO `{SV}market` ({cols}) "
            f"VALUES (%s,%s,%s,%s,%s,%s,1) ON DUPLICATE KEY UPDATE "
            f"price=%s, base_price=%s, target_stock=%s, elasticity=%s, "
            f"amount=%s, enabled=1;",
            (item_id, base_price, base_price, target_stock, elasticity, amount,
             base_price, base_price, target_stock, elasticity, amount))


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
            # บันทึกเจ้าของโค้ด เพื่อให้ shop-api รู้ว่าใครเป็นคนซื้อ (ตาราง sv_code_owners สร้างโดย API ตอน startup)
            cur.execute(f"INSERT INTO `{SV}code_owners` (code_id, steam_id) VALUES (%s,%s);",
                        (code_id, steam_id))
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
            # บันทึกเจ้าของโค้ด เพื่อให้ shop-api รู้ว่าใครเป็นคนซื้อ (ตาราง sv_code_owners สร้างโดย API ตอน startup)
            cur.execute(f"INSERT INTO `{SV}code_owners` (code_id, steam_id) VALUES (%s,%s);",
                        (code_id, steam_id))
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

# ---------- item types (sv_item_types — DDL owned by shop-api) ----------

def list_item_types():
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT id, name, description FROM `{SV}item_types` ORDER BY id ASC;")
        return cur.fetchall()


def get_item_type(type_id: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT id, name, description FROM `{SV}item_types` WHERE id=%s LIMIT 1;", (type_id,))
        return cur.fetchone()


def upsert_item_type(type_id, name: str, description: str = None):
    """type_id เป็น None = สร้างใหม่, มี = แก้ของเดิม. คืนค่า id"""
    with _conn() as c, c.cursor() as cur:
        if type_id:
            cur.execute(f"UPDATE `{SV}item_types` SET name=%s, description=%s WHERE id=%s;",
                        (name, description, int(type_id)))
            return int(type_id)
        cur.execute(f"INSERT INTO `{SV}item_types` (name, description) VALUES (%s,%s);",
                    (name, description))
        return cur.lastrowid


def delete_item_type(type_id: int) -> bool:
    # type_id ย้ายไปอยู่ที่ sv_items แล้ว — FK ที่ API สร้างเป็น ON DELETE SET NULL
    # แต่ถ้า MySQL ไม่ได้ enforce SET NULL (เช่น engine ไม่ใช่ InnoDB) ก็ปลด manual ด้วย
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"UPDATE `{SV}items` SET type_id=NULL WHERE type_id=%s;", (type_id,))
        cur.execute(f"DELETE FROM `{SV}item_types` WHERE id=%s;", (type_id,))
        return cur.rowcount > 0


# ---------- sv_items master catalog (DDL owned by shop-api) ----------

def list_items(query: str = None, type_id=None):
    """List master catalog. ถ้าให้ query/type_id ใช้กรอง"""
    clauses = []
    params = []
    if query:
        clauses.append("(i.name LIKE %s OR CAST(i.id AS CHAR) LIKE %s)")
        like = f"%{query}%"
        params.extend([like, like])
    if type_id is not None:
        clauses.append("i.type_id = %s")
        params.append(int(type_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT i.id, i.name, i.description, i.image_url, i.type_id, t.name AS type_name "
            f"FROM `{SV}items` i LEFT JOIN `{SV}item_types` t ON t.id = i.type_id "
            f"{where} ORDER BY i.id ASC;", params)
        return cur.fetchall()


def get_item(item_id: int):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT i.id, i.name, i.description, i.image_url, i.type_id, t.name AS type_name "
            f"FROM `{SV}items` i LEFT JOIN `{SV}item_types` t ON t.id = i.type_id "
            f"WHERE i.id=%s LIMIT 1;", (int(item_id),))
        return cur.fetchone()


def upsert_item(item_id: int, name: str, description=None, image_url=None, type_id=None):
    """
    Master catalog upsert. item_id เป็น PK (กำหนดเอง ไม่ auto).
    ถ้ามีอยู่แล้ว update; ไม่มีก็ insert
    """
    iid = int(item_id)
    tid = int(type_id) if type_id else None
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"INSERT INTO `{SV}items` (id, name, description, image_url, type_id) "
            f"VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            f"name=%s, description=%s, image_url=%s, type_id=%s;",
            (iid, name, description, image_url, tid,
             name, description, image_url, tid))
        return iid


def delete_item(item_id: int):
    """
    ลบจาก master catalog. คืน (ok, reason):
      - (True, None) ลบสำเร็จ
      - (False, 'not_found')
      - (False, 'referenced_market') ยังถูกใช้ใน sv_market
      - (False, 'referenced_quest') ยังถูกใช้ใน sv_quest_items
    """
    iid = int(item_id)
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT 1 FROM `{SV}items` WHERE id=%s LIMIT 1;", (iid,))
        if not cur.fetchone():
            return (False, "not_found")
        cur.execute(f"SELECT 1 FROM `{SV}market` WHERE item_id=%s LIMIT 1;", (iid,))
        if cur.fetchone():
            return (False, "referenced_market")
        cur.execute(f"SELECT 1 FROM `{SV}quest_items` WHERE item_id=%s LIMIT 1;", (iid,))
        if cur.fetchone():
            return (False, "referenced_quest")
        cur.execute(f"DELETE FROM `{SV}items` WHERE id=%s;", (iid,))
        return (True, None)


# ---------- quests (sv_quests etc. — DDL owned by shop-api) ----------

def period_key(reset_type: str) -> str:
    """
    คีย์ช่วงเวลาของเควสต์ — ต้องตรงกับที่ shop-api และ SellVault plugin ใช้:
      once   -> 'lifetime'
      daily  -> server local YYYY-MM-DD
      weekly -> ISO week 'YYYY-Www'
    """
    rt = (reset_type or "daily").lower()
    if rt == "once":
        return "lifetime"
    now = datetime.datetime.now()
    if rt == "weekly":
        iso = now.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}"
    return now.strftime("%Y-%m-%d")


def _quest_active_clause():
    """SQL clause for active+in-window quests; no params required."""
    return ("enabled=1 "
            "AND (start_at IS NULL OR start_at <= NOW()) "
            "AND (end_at IS NULL OR end_at > NOW())")


def list_quests_with_progress(steam_id: int, only_active: bool = True):
    """
    คืนรายการเควสต์ พร้อม progress ของผู้เล่นในช่วงเวลาปัจจุบัน
      [{id, name, description, reward_coins, reset_type, period_key, completed,
        items: [{item_id, item_name, qty_required, sold_qty}]}]
    """
    where = _quest_active_clause() if only_active else "1=1"
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT id, name, description, reward_coins, reset_type, start_at, end_at "
            f"FROM `{SV}quests` WHERE {where} ORDER BY id ASC;")
        quests = cur.fetchall()
        if not quests:
            return []
        qids = [int(q["id"]) for q in quests]
        placeholders = ",".join(["%s"] * len(qids))
        # ดึง items + progress ของผู้เล่นในแต่ละ period_key
        cur.execute(
            f"SELECT qi.quest_id, qi.item_id, qi.qty_required, m.name AS item_name "
            f"FROM `{SV}quest_items` qi "
            f"LEFT JOIN `{SV}market` m ON m.item_id = qi.item_id "
            f"WHERE qi.quest_id IN ({placeholders}) ORDER BY qi.quest_id, qi.item_id;",
            qids)
        items_rows = cur.fetchall()

        # progress + completion ต่อ period_key ของแต่ละเควสต์
        progress_map = {}  # (quest_id, item_id) -> sold_qty
        completed_set = set()  # quest_id ที่ completed แล้วใน period นี้
        for q in quests:
            pk = period_key(q["reset_type"])
            q["period_key"] = pk
            cur.execute(
                f"SELECT item_id, sold_qty FROM `{SV}quest_progress` "
                f"WHERE quest_id=%s AND steam_id=%s AND period_key=%s;",
                (int(q["id"]), steam_id, pk))
            for r in cur.fetchall():
                progress_map[(int(q["id"]), int(r["item_id"]))] = int(r["sold_qty"])
            cur.execute(
                f"SELECT 1 FROM `{SV}quest_completions` "
                f"WHERE quest_id=%s AND steam_id=%s AND period_key=%s LIMIT 1;",
                (int(q["id"]), steam_id, pk))
            if cur.fetchone():
                completed_set.add(int(q["id"]))

        by_quest = {}
        for r in items_rows:
            qid = int(r["quest_id"])
            by_quest.setdefault(qid, []).append({
                "item_id": int(r["item_id"]),
                "item_name": r["item_name"] or f"item {r['item_id']}",
                "qty_required": int(r["qty_required"]),
                "sold_qty": int(progress_map.get((qid, int(r["item_id"])), 0)),
            })
        for q in quests:
            q["items"] = by_quest.get(int(q["id"]), [])
            q["completed"] = int(q["id"]) in completed_set
        return quests


def get_quest_detail(quest_id: int, steam_id: int):
    """รายละเอียดเควสต์เดียว + progress ของผู้เล่นใน period ปัจจุบัน. คืน None ถ้าไม่พบ"""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT id, name, description, reward_coins, reset_type, enabled, "
            f"start_at, end_at FROM `{SV}quests` WHERE id=%s LIMIT 1;", (quest_id,))
        q = cur.fetchone()
        if not q:
            return None
        pk = period_key(q["reset_type"])
        q["period_key"] = pk
        cur.execute(
            f"SELECT qi.item_id, qi.qty_required, m.name AS item_name, "
            f"COALESCE(qp.sold_qty, 0) AS sold_qty "
            f"FROM `{SV}quest_items` qi "
            f"LEFT JOIN `{SV}market` m ON m.item_id = qi.item_id "
            f"LEFT JOIN `{SV}quest_progress` qp ON qp.quest_id=qi.quest_id "
            f"  AND qp.item_id=qi.item_id AND qp.steam_id=%s AND qp.period_key=%s "
            f"WHERE qi.quest_id=%s ORDER BY qi.item_id;",
            (steam_id, pk, quest_id))
        q["items"] = [{
            "item_id": int(r["item_id"]),
            "item_name": r["item_name"] or f"item {r['item_id']}",
            "qty_required": int(r["qty_required"]),
            "sold_qty": int(r["sold_qty"]),
        } for r in cur.fetchall()]
        cur.execute(
            f"SELECT completed_at FROM `{SV}quest_completions` "
            f"WHERE quest_id=%s AND steam_id=%s AND period_key=%s LIMIT 1;",
            (quest_id, steam_id, pk))
        row = cur.fetchone()
        q["completed"] = row is not None
        q["completed_at"] = row["completed_at"] if row else None
        return q


def list_completed_quests(steam_id: int, limit: int = 20):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT qc.quest_id, qc.period_key, qc.reward_coins, qc.completed_at, q.name "
            f"FROM `{SV}quest_completions` qc "
            f"JOIN `{SV}quests` q ON q.id = qc.quest_id "
            f"WHERE qc.steam_id=%s ORDER BY qc.completed_at DESC LIMIT %s;",
            (steam_id, int(limit)))
        return cur.fetchall()


def create_quest(name: str, description: str, reward_coins: int, reset_type: str,
                 items, enabled: bool = True, start_at=None, end_at=None) -> int:
    """items: list of (item_id, qty_required). คืน quest_id"""
    rt = (reset_type or "daily").lower()
    if rt not in ("once", "daily", "weekly"):
        rt = "daily"
    conn = _conn(); conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO `{SV}quests` (name, description, reward_coins, reset_type, "
                f"enabled, start_at, end_at) VALUES (%s,%s,%s,%s,%s,%s,%s);",
                (name, description, int(reward_coins), rt, 1 if enabled else 0, start_at, end_at))
            qid = cur.lastrowid
            for item_id, qty in items:
                if int(qty) <= 0:
                    continue
                cur.execute(
                    f"INSERT INTO `{SV}quest_items` (quest_id, item_id, qty_required) "
                    f"VALUES (%s,%s,%s);", (qid, int(item_id), int(qty)))
        conn.commit()
        return qid
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def update_quest(quest_id: int, name: str, description: str, reward_coins: int,
                 reset_type: str, items, enabled: bool = True,
                 start_at=None, end_at=None) -> bool:
    """อัปเดต metadata + แทนที่รายการ items ทั้งหมด. items: list of (item_id, qty_required)"""
    rt = (reset_type or "daily").lower()
    if rt not in ("once", "daily", "weekly"):
        rt = "daily"
    conn = _conn(); conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE `{SV}quests` SET name=%s, description=%s, reward_coins=%s, "
                f"reset_type=%s, enabled=%s, start_at=%s, end_at=%s WHERE id=%s;",
                (name, description, int(reward_coins), rt, 1 if enabled else 0,
                 start_at, end_at, int(quest_id)))
            changed = cur.rowcount
            if items is not None:
                cur.execute(f"DELETE FROM `{SV}quest_items` WHERE quest_id=%s;", (int(quest_id),))
                for item_id, qty in items:
                    if int(qty) <= 0:
                        continue
                    cur.execute(
                        f"INSERT INTO `{SV}quest_items` (quest_id, item_id, qty_required) "
                        f"VALUES (%s,%s,%s);", (int(quest_id), int(item_id), int(qty)))
        conn.commit()
        return changed > 0
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def delete_quest(quest_id: int) -> bool:
    """ลบเควสต์. sv_quest_items มี ON DELETE CASCADE ตาม DDL ของ API; progress/completions ลบเอง"""
    conn = _conn(); conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM `{SV}quest_progress` WHERE quest_id=%s;", (int(quest_id),))
            cur.execute(f"DELETE FROM `{SV}quest_completions` WHERE quest_id=%s;", (int(quest_id),))
            cur.execute(f"DELETE FROM `{SV}quests` WHERE id=%s;", (int(quest_id),))
            ok = cur.rowcount > 0
        conn.commit()
        return ok
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


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


# ---------- notifications outbox (sv_notifications — DDL owned by shop-api) ----------
# The api writes rows (dm_status='pending'); the bot's poll loop reads pending rows,
# DMs the recipient, then marks dm_status. The bot only SELECTs and UPDATEs dm_* —
# it never creates this table (like sv_items / sv_quests, the DDL lives in shop-api).

def fetch_pending_notifications(limit: int):
    """Oldest-first batch of undelivered notifications. payload is raw JSON text;
    the caller parses it. Returns [] when nothing is pending."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT id, discord_id, steam_id, kind, payload, dm_attempts "
            f"FROM `{SV}notifications` WHERE dm_status='pending' "
            f"ORDER BY id ASC LIMIT %s;",
            (int(limit),))
        return cur.fetchall()


def mark_notification(notification_id: int, status: str):
    """Record a delivery attempt outcome. Always bumps dm_attempts; sets dm_sent_at
    only on success. status is one of 'pending' (retry later) / 'sent' / 'failed'."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"UPDATE `{SV}notifications` SET dm_status=%s, dm_attempts=dm_attempts+1, "
            f"dm_sent_at=CASE WHEN %s='sent' THEN NOW() ELSE dm_sent_at END "
            f"WHERE id=%s;",
            (status, status, int(notification_id)))


# ---------- p2p listings feed (sv_p2p_listings — DDL owned by shop-api) ----------
# The bot auto-announces new player listings to a Discord channel. It only SELECTs
# listing rows and UPDATEs announced_at (the watermark added by api migration 007);
# it never creates sv_p2p_listings nor mutates listing state (buy goes through the api).
# Column names below follow the agreed contract — confirm against the api's 007 DDL.

def fetch_unannounced_listings(limit: int):
    """Active listings not yet posted to the feed, oldest first. JOINs sv_items for the
    item name/image and sv_links for the seller's discord handle. Returns [] when none."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT p.id, p.item_id, p.amount, p.price, p.quality, p.seller_steam, "
            f"i.name, i.image_url, li.discord_id AS seller_discord "
            f"FROM `{SV}p2p_listings` p "
            f"LEFT JOIN `{SV}items` i ON i.id = p.item_id "
            f"LEFT JOIN `{SV}links` li ON li.steam_id = p.seller_steam "
            f"WHERE p.status='active' AND p.announced_at IS NULL "
            f"ORDER BY p.id ASC LIMIT %s;",
            (int(limit),))
        return cur.fetchall()


def mark_listing_announced(listing_id: int):
    """Stamp announced_at so the feed loop never re-posts this listing (the watermark).
    Called only after a successful channel post."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"UPDATE `{SV}p2p_listings` SET announced_at=NOW() WHERE id=%s;",
            (int(listing_id),))
