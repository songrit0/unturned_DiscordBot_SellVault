"""
Unturned economy Discord bot (unified market model).

Shares MySQL with the SellVault + RedeemCode plugins.
  - Welcome Pack button -> link code; player runs /link <code> in-game
  - Market list (one list): every item shown with an "add to basket" button; checkout buys all
    at once (stock -1 each) -> one redeem code. Selling is done in-game in a sell box.
  - /coins /top /pay + admin control panel (popups)
Selling is done in-game (sell box) by the SellVault plugin; the bot only shows the catalog/prices.
"""
import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands
from discord.webhook.async_ import Webhook

import config
import db


# ---- auto-dismiss ephemeral messages after N seconds (no manual "Dismiss") ----
AUTO_DISMISS_SECONDS = 300  # 5 minutes


async def _auto_dismiss(message_coro_or_obj, delay: int):
    try:
        msg = await message_coro_or_obj if asyncio.iscoroutine(message_coro_or_obj) else message_coro_or_obj
        if msg is None:
            return
        await asyncio.sleep(delay)
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception:
        pass


_orig_response_send = discord.InteractionResponse.send_message

async def _patched_response_send(self, *args, **kwargs):
    is_eph = bool(kwargs.get("ephemeral", False))
    result = await _orig_response_send(self, *args, **kwargs)
    if is_eph and AUTO_DISMISS_SECONDS > 0:
        async def grab():
            try:
                msg = await self._parent.original_response()
                asyncio.create_task(_auto_dismiss(msg, AUTO_DISMISS_SECONDS))
            except Exception:
                pass
        asyncio.create_task(grab())
    return result

discord.InteractionResponse.send_message = _patched_response_send


_orig_webhook_send = Webhook.send

async def _patched_webhook_send(self, *args, **kwargs):
    is_eph = bool(kwargs.get("ephemeral", False))
    if is_eph:
        kwargs.setdefault("wait", True)
    msg = await _orig_webhook_send(self, *args, **kwargs)
    if is_eph and msg is not None and AUTO_DISMISS_SECONDS > 0:
        asyncio.create_task(_auto_dismiss(msg, AUTO_DISMISS_SECONDS))
    return msg

Webhook.send = _patched_webhook_send

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("unturned-bot")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory shopping baskets: discord_id -> {item_id: qty}. Transient (cleared on checkout).
_baskets: dict = {}


def basket_add(uid: int, item_id: int, qty: int = 1):
    b = _baskets.setdefault(uid, {})
    b[item_id] = b.get(item_id, 0) + qty


def basket_get(uid: int) -> dict:
    return _baskets.get(uid, {})


def basket_clear(uid: int):
    _baskets.pop(uid, None)


def is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    if config.ADMIN_ROLE_ID:
        return any(r.id == config.ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
    return False


def sell_payout_price(price) -> int:
    """Coins a player gets selling one unit in-game (price minus commission), for display."""
    rate = 1.0 - config.SELL_COMMISSION / 100.0
    if rate < 0:
        rate = 0
    return int(round(float(price) * rate))


def parse_discord_id(text: str):
    m = re.search(r"\d{15,20}", text or "")
    return int(m.group()) if m else None


# ---------------- embeds ----------------

def market_item_embed(it) -> discord.Embed:
    e = discord.Embed(title=it["name"], color=0x3498DB)
    e.add_field(name="ซื้อ / Buy", value=f"{int(round(float(it['price'])))} {config.COIN_NAME}")
    e.add_field(name="ขายได้ / Sell", value=f"~{sell_payout_price(it['price'])} {config.COIN_NAME}")
    e.add_field(name="คงเหลือ / Stock", value=str(int(it["amount"])))
    if it.get("image_url"):
        e.set_image(url=it["image_url"])
    e.set_footer(text=f"item id {it['item_id']}")
    return e


async def build_top_embed() -> discord.Embed:
    rows = await asyncio.to_thread(db.top_coins, 10)
    if not rows:
        return discord.Embed(title=f"🏆 {config.COIN_NAME} Leaderboard",
                             description="ยังไม่มีข้อมูล", color=0xFFD700)
    lines = []
    for i, r in enumerate(rows, 1):
        who = f"<@{r['discord_id']}>" if r.get("discord_id") else f"`{r['steam_id']}`"
        lines.append(f"**{i}.** {who} — {int(r['balance'])} {config.COIN_NAME}")
    return discord.Embed(title=f"🏆 {config.COIN_NAME} Leaderboard", description="\n".join(lines), color=0xFFD700)


def build_commands_embed() -> discord.Embed:
    e = discord.Embed(title="📖 คำสั่งในเกม / In-game commands", description="พิมพ์ในแชทเกม Unturned",
                      color=0x95A5A6)
    e.add_field(name="🌐 เว็บไซต์ / Web shop",
                value=f"ซื้อ-ขาย / ดูยอด / เช็คโค้ด ผ่านเว็บได้เลย: {config.WEB_SHOP_URL}/login",
                inline=False)
    e.add_field(name="/link <code>", value="เชื่อมบัญชี Discord (code จากปุ่ม Welcome Pack) + รับของ", inline=False)
    e.add_field(name="/sell", value="เปิดกล่องขาย (เฉพาะใน Safe Zone) → วางของ → ปิด → ได้ Coin", inline=False)
    e.add_field(name="ขายของ (อีกวิธี)", value="เอาของไปวางใน 'กล่อง sell' ที่แอดมินตั้งไว้ แล้วปิดกล่อง → ได้ Coin", inline=False)
    e.add_field(name="/coins", value="เช็คยอด Coin", inline=False)
    e.add_field(name="/code <code>", value="ใช้โค้ด (จาก /shop หรือ /bills-shop) รับของเข้าเกม", inline=False)
    e.add_field(name="💵 Bills",
                value="ธนบัตร $5/$10/$50/$100/$500 — ซื้อที่ห้อง 💵-bills-shop ใน Discord, ขายที่ /sell ได้เต็มราคา (ไม่หัก commission)",
                inline=False)
    e.add_field(name="🎁 Online + Activity Rewards",
                value="ออนไลน์รับ Coin อัตโนมัติ (ไม่ AFK) + ได้ Coin จากการฆ่าซอมบี้/ผู้เล่น/สัตว์, สร้าง, เก็บทรัพยากร",
                inline=False)
    e.add_field(name="/decay", value="เช็คการป้องกันฐาน (ToolCupboard)", inline=False)
    e.add_field(name="/itemid", value="ดู id ไอเทมที่ถืออยู่", inline=False)
    e.set_footer(text="แอดมิน: /setsellbox /sellreload /tcreload /dmreload")
    return e


# ---------------- player-facing views ----------------

class LinkView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="🌐 เปิดเว็บ Shop", style=discord.ButtonStyle.link,
                                        url=config.WEB_SHOP_URL))

    @discord.ui.button(label="🎁 รับ Welcome Pack / เชื่อมบัญชี", style=discord.ButtonStyle.success,
                       custom_id="welcomepack_link")
    async def link(self, interaction: discord.Interaction, button: discord.ui.Button):
        did = interaction.user.id
        try:
            if await asyncio.to_thread(db.is_discord_linked, did):
                await interaction.response.send_message("บัญชีนี้เชื่อมแล้ว ✅", ephemeral=True)
                return
            code = await asyncio.to_thread(db.create_link_code, did)
        except Exception:
            log.exception("link button failed")
            await interaction.response.send_message("เกิดข้อผิดพลาด ลองใหม่", ephemeral=True)
            return
        await interaction.response.send_message(
            f"เข้าเกมแล้วพิมพ์:\n```/link {code}```\nเชื่อมบัญชี + รับ Welcome Pack 🎁 *(code ใช้ครั้งเดียว)*\n\n"
            f"หรือซื้อ-ขายผ่านเว็บที่ {config.WEB_SHOP_URL}/login (login ด้วย Discord เดียวกัน)",
            ephemeral=True)


class BalanceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 เช็ค Coin ของฉัน / My Coins", style=discord.ButtonStyle.success,
                       custom_id="check_coins")
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        steam = await asyncio.to_thread(db.get_steam_by_discord, interaction.user.id)
        if steam is None:
            await interaction.response.send_message("ยังไม่ได้เชื่อมบัญชี กดปุ่ม Welcome Pack ก่อน", ephemeral=True)
            return
        bal = await asyncio.to_thread(db.get_coins, steam)
        await interaction.response.send_message(f"💰 ยอดของคุณ: **{bal}** {config.COIN_NAME}", ephemeral=True)


class MarketListView(discord.ui.View):
    """Persistent panel: show the whole market list (text + images)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📋 ดูรายการ + ใส่ตะกร้า / View", style=discord.ButtonStyle.primary,
                       custom_id="view_market")
    async def view(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_shop(interaction)


class ShopPanelView(discord.ui.View):
    """Persistent shop panel: buy single items or packages."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="🌐 เปิดเว็บ", style=discord.ButtonStyle.link,
                                        url=config.WEB_SHOP_URL))

    @discord.ui.button(label="🛒 เปิดร้าน / Open shop", style=discord.ButtonStyle.primary, custom_id="open_shop")
    async def open_shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_shop(interaction)

    @discord.ui.button(label="🧺 ตะกร้า / Basket", style=discord.ButtonStyle.secondary, custom_id="open_basket")
    async def open_basket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_basket(interaction)


class BillsShopPanelView(discord.ui.View):
    """Persistent bills shop panel: buy cash bills only."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="🌐 เปิดเว็บ", style=discord.ButtonStyle.link,
                                        url=config.WEB_SHOP_URL))

    @discord.ui.button(label="💵 ซื้อ Bills / Buy bills", style=discord.ButtonStyle.primary, custom_id="open_bills_shop")
    async def open_bills(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_bills_shop(interaction)

    @discord.ui.button(label="🧺 ตะกร้า / Basket", style=discord.ButtonStyle.secondary, custom_id="open_bills_basket")
    async def open_basket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_basket(interaction)


class LeaderboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔄 รีเฟรช / Refresh", style=discord.ButtonStyle.secondary, custom_id="refresh_top")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=await build_top_embed(), view=self)


# ----- show all items, each with an Add-to-basket button -----

async def send_shop(interaction: discord.Interaction):
    items = await asyncio.to_thread(db.list_market, False, config.BILL_ITEM_IDS, None)
    await _send_item_cards(interaction, items, empty_msg="ยังไม่มีรายการ | No items.")


async def send_bills_shop(interaction: discord.Interaction):
    items = await asyncio.to_thread(db.list_market, False, None, config.BILL_ITEM_IDS)
    await _send_item_cards(interaction, items, empty_msg="ยังไม่มี Bills ในระบบ | No bills available.")


async def _send_item_cards(interaction: discord.Interaction, items, empty_msg: str):
    if not items:
        await interaction.response.send_message(empty_msg, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    for it in items[:25]:
        await interaction.followup.send(embed=market_item_embed(it),
                                        view=AddOneView(int(it["item_id"]), it["name"]), ephemeral=True)
    note = "" if len(items) <= 25 else "\n*(แสดง 25 รายการแรก)*"
    await interaction.followup.send(content="กด ➕ ใต้แต่ละชิ้น แล้วกด 🧺 เพื่อดู/ซื้อ" + note,
                                    view=CheckoutOnlyView(), ephemeral=True)


class AddOneView(discord.ui.View):
    def __init__(self, item_id: int, name: str):
        super().__init__(timeout=600)
        self.item_id = item_id
        self.name = name

    @discord.ui.button(label="➕ ใส่ตะกร้า / Add", style=discord.ButtonStyle.primary)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        basket_add(interaction.user.id, self.item_id)
        qty = basket_get(interaction.user.id).get(self.item_id, 0)
        await interaction.response.send_message(f"➕ {self.name} (ตะกร้ามี {qty}) | added", ephemeral=True)

    @discord.ui.button(label="🧺 ตะกร้า", style=discord.ButtonStyle.success)
    async def basket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_basket(interaction)


class CheckoutOnlyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.button(label="🧺 ดูตะกร้า / Checkout", style=discord.ButtonStyle.success)
    async def checkout(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_basket(interaction)


async def show_basket(interaction: discord.Interaction):
    uid = interaction.user.id
    basket = basket_get(uid)
    if not basket:
        await interaction.response.send_message("🧺 ตะกร้าว่าง | Basket empty.", ephemeral=True)
        return
    lines, total = [], 0
    for item_id, qty in basket.items():
        it = await asyncio.to_thread(db.get_market, item_id)
        if not it:
            continue
        price = int(round(float(it["price"])))
        sub = price * qty
        total += sub
        lines.append(f"• {it['name']} x{qty} = {sub} {config.COIN_NAME}")
    embed = discord.Embed(title="🧺 ตะกร้าของคุณ / Your basket",
                          description="\n".join(lines) or "ว่าง", color=0x9B59B6)
    embed.add_field(name="รวม / Total", value=f"{total} {config.COIN_NAME}")
    await interaction.response.send_message(embed=embed, view=BasketActionsView(), ephemeral=True)


class BasketActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="✅ ยืนยันซื้อ / Buy", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        basket = dict(basket_get(interaction.user.id))
        if not basket:
            await interaction.response.send_message("ตะกร้าว่าง", ephemeral=True)
            return
        try:
            status, payload = await asyncio.to_thread(db.buy_basket, interaction.user.id, basket)
        except Exception:
            log.exception("buy_basket failed")
            await interaction.response.send_message("เกิดข้อผิดพลาด", ephemeral=True)
            return
        if status == "ok":
            basket_clear(interaction.user.id)
            await interaction.response.send_message(
                f"ซื้อสำเร็จ! เข้าเกมพิมพ์:\n```/code {payload}```\nรับของทั้งตะกร้า", ephemeral=True)
        else:
            await _buy_reply(interaction, status, payload)

    @discord.ui.button(label="🗑️ ล้างตะกร้า / Clear", style=discord.ButtonStyle.danger)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        basket_clear(interaction.user.id)
        await interaction.response.send_message("ล้างตะกร้าแล้ว | Cleared.", ephemeral=True)


async def _buy_reply(interaction, status, payload):
    if status == "not_linked":
        await interaction.response.send_message("ยังไม่ได้เชื่อมบัญชี กดปุ่ม Welcome Pack ก่อน", ephemeral=True)
    elif status == "empty":
        await interaction.response.send_message("ตะกร้าว่าง", ephemeral=True)
    elif status == "no_item":
        await interaction.response.send_message("ไม่พบสินค้า", ephemeral=True)
    elif status == "out_of_stock":
        extra = f" ({payload})" if isinstance(payload, str) else ""
        await interaction.response.send_message(f"ของหมดสต็อก{extra}", ephemeral=True)
    elif status == "insufficient":
        await interaction.response.send_message(
            f"Coin ไม่พอ (มี {payload}) | Not enough (have {payload}).", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"ซื้อสำเร็จ! เข้าเกมพิมพ์:\n```/code {payload}```\nเพื่อรับของ", ephemeral=True)


# ---------------- slash commands (players) ----------------

@bot.tree.command(description="ดูยอด Coin / check balance")
async def coins(interaction: discord.Interaction):
    steam = await asyncio.to_thread(db.get_steam_by_discord, interaction.user.id)
    if steam is None:
        await interaction.response.send_message("ยังไม่ได้เชื่อมบัญชี กดปุ่ม Welcome Pack ก่อน", ephemeral=True)
        return
    bal = await asyncio.to_thread(db.get_coins, steam)
    await interaction.response.send_message(f"💰 ยอดของคุณ: **{bal}** {config.COIN_NAME}", ephemeral=True)


@bot.tree.command(description="อันดับ Coin / leaderboard")
async def top(interaction: discord.Interaction):
    await interaction.response.send_message(embed=await build_top_embed())


@bot.tree.command(description="ดูรายการตลาด + ใส่ตะกร้า / market list")
async def market(interaction: discord.Interaction):
    await send_shop(interaction)


@bot.tree.command(description="โอน Coin ให้ผู้เล่นอื่น / transfer coins")
@app_commands.describe(member="ผู้รับ (เชื่อมบัญชีแล้ว)", amount="จำนวน Coin")
async def pay(interaction: discord.Interaction, member: discord.Member, amount: int):
    status, payload = await asyncio.to_thread(db.transfer_coins, interaction.user.id, member.id, amount)
    msgs = {
        "bad_amount": "จำนวนต้องมากกว่า 0",
        "not_linked_self": "คุณยังไม่เชื่อมบัญชี",
        "not_linked_target": "ผู้รับยังไม่เชื่อมบัญชี",
        "self": "โอนให้ตัวเองไม่ได้",
    }
    if status in msgs:
        await interaction.response.send_message(msgs[status], ephemeral=True)
    elif status == "insufficient":
        await interaction.response.send_message(f"Coin ไม่พอ (มี {payload})", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"✅ โอน {amount} {config.COIN_NAME} ให้ {member.mention} (เหลือ {payload})", ephemeral=True)


# ---------------- admin: modals ----------------

class MarketModal(discord.ui.Modal, title="ตั้ง/แก้ รายการตลาด"):
    m_item = discord.ui.TextInput(label="Item ID", max_length=10)
    m_name = discord.ui.TextInput(label="ชื่อ / Name", max_length=64)
    m_price = discord.ui.TextInput(label="ราคาเริ่มต้น (Coin) / Price", max_length=12)
    m_amount = discord.ui.TextInput(label="สต็อกเริ่มต้น / Stock (0 = ซ่อน)", default="0", max_length=8)
    m_image = discord.ui.TextInput(label="ลิงก์รูป / Image URL (optional)", required=False, max_length=400)

    def __init__(self, prefill=None):
        super().__init__()
        if prefill:
            self.m_item.default = str(prefill["item_id"])
            self.m_name.default = prefill.get("name") or ""
            self.m_price.default = str(int(round(float(prefill["price"]))))
            self.m_amount.default = str(int(prefill["amount"]))
            self.m_image.default = prefill.get("image_url") or ""

    async def on_submit(self, interaction: discord.Interaction):
        try:
            iid = int(str(self.m_item.value)); pr = float(str(self.m_price.value))
            amt = int(str(self.m_amount.value) or "0")
        except ValueError:
            await interaction.response.send_message("ตัวเลขไม่ถูกต้อง (Item ID / Price / Stock)", ephemeral=True)
            return
        img = str(self.m_image.value).strip() or None
        await asyncio.to_thread(db.upsert_market, iid, str(self.m_name.value), pr, amt, img)
        await interaction.response.send_message(
            f"ตั้งรายการ `{iid}` **{self.m_name.value}** ราคา {int(round(pr))} สต็อก {amt} ✅\n*(ในเกม /sellreload)*",
            ephemeral=True)


class RemoveMarketModal(discord.ui.Modal, title="ลบรายการตลาด"):
    m_item = discord.ui.TextInput(label="Item ID", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            iid = int(str(self.m_item.value))
        except ValueError:
            await interaction.response.send_message("Item ID ไม่ถูกต้อง", ephemeral=True)
            return
        ok = await asyncio.to_thread(db.remove_market, iid)
        await interaction.response.send_message("ลบแล้ว ✅ (ในเกม /sellreload)" if ok else "ไม่พบรายการ",
                                                ephemeral=True)


class AdminMarketEditView(discord.ui.View):
    """Admin: pick a market item to edit or delete."""
    def __init__(self, items):
        super().__init__(timeout=180)
        self.add_item(AdminMarketSelect(items))


class AdminMarketSelect(discord.ui.Select):
    def __init__(self, items):
        options = [discord.SelectOption(
            label=it["name"][:100], value=str(it["item_id"]),
            description=f"{int(round(float(it['price'])))} {config.COIN_NAME} · สต็อก {int(it['amount'])}"[:100])
            for it in items[:25]]
        super().__init__(placeholder="เลือกรายการเพื่อแก้ไข/ลบ...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
            return
        it = await asyncio.to_thread(db.get_market, int(self.values[0]))
        if not it:
            await interaction.response.send_message("ไม่พบรายการ", ephemeral=True)
            return
        await interaction.response.send_message(embed=market_item_embed(it),
                                                view=AdminItemActionsView(it), ephemeral=True)


class AdminItemActionsView(discord.ui.View):
    def __init__(self, item):
        super().__init__(timeout=120)
        self.item = item

    @discord.ui.button(label="✏️ แก้ไข / Edit", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
            return
        await interaction.response.send_modal(MarketModal(prefill=self.item))

    @discord.ui.button(label="🗑️ ลบ / Delete", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
            return
        ok = await asyncio.to_thread(db.remove_market, int(self.item["item_id"]))
        await interaction.response.send_message(
            "ลบแล้ว ✅ (ในเกม /sellreload)" if ok else "ไม่พบรายการ", ephemeral=True)


class CreateCodeModal(discord.ui.Modal, title="สร้าง Redeem Code"):
    m_code = discord.ui.TextInput(label="โค้ด (เว้นว่าง = สุ่ม)", required=False, max_length=32)
    m_max = discord.ui.TextInput(label="จำนวนคนใช้ได้ (0 = ไม่จำกัด)", default="1", max_length=6)
    m_items = discord.ui.TextInput(label="ไอเทม เช่น 81:5 78:2", max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            mx = int(str(self.m_max.value) or "1")
        except ValueError:
            await interaction.response.send_message("จำนวนคนใช้ไม่ถูกต้อง", ephemeral=True)
            return
        parsed = []
        try:
            for tok in str(self.m_items.value).split():
                sid, amt = tok.split(":")
                parsed.append((int(sid), int(amt)))
            if not parsed:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("รูปแบบ items ผิด เช่น `81:5 78:2`", ephemeral=True)
            return
        code = str(self.m_code.value).strip() or None
        try:
            code = await asyncio.to_thread(db.create_redeem_code, mx, parsed, code)
        except Exception:
            await interaction.response.send_message("สร้างไม่สำเร็จ (โค้ดซ้ำ?)", ephemeral=True)
            return
        await interaction.response.send_message(f"สร้างโค้ด `{code}` (max {mx}) → /code {code}", ephemeral=True)


class GiveCoinsModal(discord.ui.Modal, title="ให้ / ปรับ Coin"):
    m_target = discord.ui.TextInput(label="Discord ID หรือ @mention", max_length=64)
    m_amount = discord.ui.TextInput(label="จำนวน (ติดลบได้)", max_length=12)

    async def on_submit(self, interaction: discord.Interaction):
        did = parse_discord_id(str(self.m_target.value))
        try:
            amt = int(str(self.m_amount.value))
        except ValueError:
            await interaction.response.send_message("จำนวนไม่ถูกต้อง", ephemeral=True)
            return
        if did is None:
            await interaction.response.send_message("ระบุ Discord ID/mention ไม่ถูกต้อง", ephemeral=True)
            return
        steam = await asyncio.to_thread(db.get_steam_by_discord, did)
        if steam is None:
            await interaction.response.send_message("ผู้เล่นยังไม่เชื่อมบัญชี", ephemeral=True)
            return
        await asyncio.to_thread(db.add_coins, steam, amt)
        bal = await asyncio.to_thread(db.get_coins, steam)
        await interaction.response.send_message(f"ปรับ {amt} {config.COIN_NAME} ให้ <@{did}> (ยอด {bal}) ✅",
                                                ephemeral=True)


# ---------------- admin: control panel ----------------

class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction):
            return True
        await interaction.response.send_message("ต้องเป็นแอดมิน | Admins only.", ephemeral=True)
        return False

    @discord.ui.button(label="➕ ตั้ง/แก้รายการ", style=discord.ButtonStyle.success, custom_id="admin_market_set")
    async def b_market_set(self, interaction, button):
        if await self._guard(interaction):
            await interaction.response.send_modal(MarketModal())

    @discord.ui.button(label="🗑️ ลบรายการ", style=discord.ButtonStyle.danger, custom_id="admin_market_del")
    async def b_market_del(self, interaction, button):
        if await self._guard(interaction):
            await interaction.response.send_modal(RemoveMarketModal())

    @discord.ui.button(label="🎟️ สร้างโค้ด", style=discord.ButtonStyle.secondary, custom_id="admin_code")
    async def b_code(self, interaction, button):
        if await self._guard(interaction):
            await interaction.response.send_modal(CreateCodeModal())

    @discord.ui.button(label="💸 ให้ Coin", style=discord.ButtonStyle.secondary, custom_id="admin_give")
    async def b_give(self, interaction, button):
        if await self._guard(interaction):
            await interaction.response.send_modal(GiveCoinsModal())

    @discord.ui.button(label="📋 จัดการรายการ / Edit list", style=discord.ButtonStyle.secondary,
                       custom_id="admin_list_market")
    async def b_list_market(self, interaction, button):
        if not await self._guard(interaction):
            return
        items = await asyncio.to_thread(db.list_market, True)
        if not items:
            await interaction.response.send_message("ยังไม่มีรายการ — กด ➕ ตั้ง/แก้รายการ เพื่อเพิ่ม", ephemeral=True)
            return
        lines = [f"`{it['item_id']}` · {it['name']} — {int(round(float(it['price'])))} {config.COIN_NAME} "
                 f"(สต็อก {int(it['amount'])})" for it in items]
        await interaction.response.send_message(
            "เลือกจากเมนูเพื่อแก้ไข/ลบ:\n" + "\n".join(lines)[:1800],
            view=AdminMarketEditView(items), ephemeral=True)


# ---------------- admin: slash ----------------

@bot.tree.command(description="[admin] วางแผงควบคุม admin ในห้องนี้")
async def adminpanel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
        return
    await interaction.channel.send(embed=discord.Embed(
        title="🛠️ Admin Panel", description="กดปุ่มเพื่อจัดการ — เด้ง popup", color=0xE74C3C),
        view=AdminPanelView())
    await interaction.response.send_message("วางแผงแล้ว ✅", ephemeral=True)


@bot.tree.command(description="[admin] วางปุ่ม Welcome Pack ในห้องนี้")
async def setuplink(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
        return
    await interaction.channel.send(embed=discord.Embed(
        title="🎁 Welcome Pack",
        description=(
            "กดปุ่มเพื่อรับ code → เข้าเกมพิมพ์ `/link <code>`\n\n"
            f"🌐 เว็บ: {config.WEB_SHOP_URL}/login"
        ),
        color=0x2ECC71), view=LinkView())
    await interaction.response.send_message("วางปุ่มแล้ว ✅", ephemeral=True)


@bot.tree.command(description="[admin] สร้างห้อง + แผงทั้งหมดอัตโนมัติ")
async def setup(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("ใช้ในเซิร์ฟเท่านั้น", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    everyone = guild.default_role
    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
    }

    try:
        category = discord.utils.get(guild.categories, name="🎮 Game Economy") \
            or await guild.create_category("🎮 Game Economy")

        async def ensure_channel(name, ow=None):
            ch = discord.utils.get(guild.text_channels, name=name)
            if ch is None:
                ch = await guild.create_text_channel(name, category=category, overwrites=ow or overwrites)
            else:
                try:
                    await ch.edit(category=category, overwrites=ow or overwrites)
                except Exception:
                    pass
            return ch

        welcome = await ensure_channel("🎁-welcome")
        coins_ch = await ensure_channel("💰-my-coins")
        shop_ch = await ensure_channel("🛒-shop")
        bills_ch = await ensure_channel("💵-bills-shop")
        market_ch = await ensure_channel("🏷️-market")
        lb = await ensure_channel("🏆-leaderboard")
        cmds_ch = await ensure_channel("📖-commands")

        admin_over = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
        }
        if config.ADMIN_ROLE_ID:
            role = guild.get_role(config.ADMIN_ROLE_ID)
            if role:
                admin_over[role] = discord.PermissionOverwrite(view_channel=True)
        admin_ch = await ensure_channel("🛠️-admin", admin_over)

        for ch in (welcome, coins_ch, shop_ch, bills_ch, market_ch, lb, cmds_ch, admin_ch):
            try:
                await ch.purge(limit=20, check=lambda m: m.author == guild.me)
            except Exception:
                pass

        await welcome.send(embed=discord.Embed(
            title="🎁 Welcome Pack",
            description=(
                "กดปุ่มเพื่อรับ code → เข้าเกมพิมพ์ `/link <code>` เชื่อมบัญชี + รับของ\n\n"
                f"**🆕 เข้าใช้ผ่านเว็บได้แล้ว!** ซื้อ-ขาย/ดูยอด/เช็คโค้ดผ่านมือถือก็ได้\n"
                f"👉 {config.WEB_SHOP_URL}/login (login ด้วย Discord เดียวกัน)"
            ),
            color=0x2ECC71),
            view=LinkView())
        await coins_ch.send(embed=discord.Embed(
            title="💰 เช็ค Coin / My Coins", description="กดปุ่มเพื่อดูยอด Coin", color=0xF1C40F), view=BalanceView())
        await shop_ch.send(embed=discord.Embed(
            title="🛒 ร้านค้า / Shop", description="ซื้อชิ้นเดี่ยว หรือแพ็กเกจ ด้วย Coin → ได้โค้ด → /code ในเกม",
            color=0x3498DB), view=ShopPanelView())
        await bills_ch.send(embed=discord.Embed(
            title="💵 ซื้อ Bills / Bills Shop",
            description=("ซื้อธนบัตรในเกมด้วย Coin → ได้โค้ด → /code ในเกม\n"
                         "ขายธนบัตรในเกมที่ /sell ได้เต็มราคา ไม่หัก commission"),
            color=0x27AE60), view=BillsShopPanelView())
        await market_ch.send(embed=discord.Embed(
            title="🏷️ รายการตลาด / Market",
            description="กดดูรายการของที่ซื้อ/ขายได้ + ราคา (ขายจริงในเกมที่กล่อง sell)", color=0xE67E22),
            view=MarketListView())
        await lb.send(embed=await build_top_embed(), view=LeaderboardView())
        await cmds_ch.send(embed=build_commands_embed())
        await admin_ch.send(embed=discord.Embed(
            title="🛠️ Admin Panel", description="กดปุ่มเพื่อจัดการ — เด้ง popup (เฉพาะแอดมิน)", color=0xE74C3C),
            view=AdminPanelView())

    except discord.Forbidden:
        await interaction.followup.send("บอทไม่มีสิทธิ์ Manage Channels/Messages", ephemeral=True)
        return
    except Exception:
        log.exception("setup failed")
        await interaction.followup.send("setup ล้มเหลว ดู log", ephemeral=True)
        return

    await interaction.followup.send(
        "ตั้งค่าเสร็จ ✅ " + " · ".join(
            c.mention for c in (welcome, coins_ch, shop_ch, bills_ch, market_ch, lb, cmds_ch, admin_ch)), ephemeral=True)


# ---------------- lifecycle ----------------

@bot.event
async def setup_hook():
    await asyncio.to_thread(db.ensure_schema)
    for v in (LinkView(), BalanceView(), MarketListView(), ShopPanelView(),
              BillsShopPanelView(), LeaderboardView(), AdminPanelView()):
        bot.add_view(v)
    if config.GUILD_ID:
        guild = discord.Object(id=config.GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    log.info("Commands synced.")


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (copy from .env.example)")
    bot.run(config.DISCORD_TOKEN)
