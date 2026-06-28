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
import json
import logging
import re

import aiohttp
import discord
from deep_translator import GoogleTranslator
from discord import app_commands
from discord.ext import commands, tasks
from discord.webhook.async_ import Webhook

import backfill_usernames
import config
import db
import vip_db


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
# Privileged Server Members intent — required to enumerate guild members for the VIP role
# reconcile. MUST ALSO be toggled ON in the Discord Developer Portal (Bot > Privileged Gateway
# Intents > Server Members Intent), otherwise guild.members is empty and the sync no-ops.
intents.members = True
# Privileged Message Content intent — required to read the text of the game-chat webhook
# messages we translate below. Toggle ON in Developer Portal (Bot > Message Content Intent) too.
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

GAME_CHAT_LINE_RE = re.compile(r"^💬\s*(.+?):\s*(.+)$")
THAI_RE = re.compile(r"[฀-๿]")


@bot.event
async def on_message(message: discord.Message):
    # Translate in-game chat relayed via webhook ("💬 Name: text"), EN -> TH only. Name is
    # never translated. Skips own bot replies (webhook_id is None for them).
    if not config.GAME_CHAT_CHANNEL_ID or message.channel.id != config.GAME_CHAT_CHANNEL_ID:
        return
    if message.webhook_id is None:
        return
    match = GAME_CHAT_LINE_RE.match(message.content.strip())
    if not match:
        return
    name, text = match.groups()
    if THAI_RE.search(text):
        return
    try:
        translated = await asyncio.to_thread(
            GoogleTranslator(source="en", target="th").translate, text)
    except Exception:
        log.exception("game chat translate failed")
        return
    if translated.strip().lower() != text.strip().lower():
        # Discord has no plain-text color markdown; ANSI codeblock is the only way to get
        # colored text (33 = yellow). Renders plain/monospace on mobile clients, which don't
        # support ANSI codeblocks.
        await message.reply(
            f"```ansi\n💬 {name}: [33m{translated}[0m\n```", mention_author=False)

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


def _group_market_by_type(items):
    """แบ่งไอเทมตาม type_id; คืน list of (type_id, type_name, [items]).
    หมวดที่ไม่มี type_id อยู่ท้ายสุด. หมวดว่างจะถูกข้าม."""
    buckets = {}
    order = []  # รักษาลำดับการเจอ (เรียงตาม price อยู่แล้วใน list_market)
    for it in items:
        tid = it.get("type_id")
        key = int(tid) if tid is not None else None
        if key not in buckets:
            buckets[key] = {"name": it.get("type_name") or "ไม่มีหมวด", "items": []}
            order.append(key)
        buckets[key]["items"].append(it)
    # ดัน None ไปท้ายสุดเสมอ
    order_sorted = [k for k in order if k is not None] + ([None] if None in buckets else [])
    return [(k, buckets[k]["name"], buckets[k]["items"]) for k in order_sorted if buckets[k]["items"]]


def market_group_embed(type_name: str, items) -> discord.Embed:
    e = discord.Embed(title=f"🏷️ {type_name}", color=0xE67E22)
    lines = []
    for it in items:
        price = int(round(float(it["price"])))
        sell = sell_payout_price(it["price"])
        lines.append(
            f"`{it['item_id']}` · **{it['name']}** — ซื้อ {price} / ขาย ~{sell} "
            f"{config.COIN_NAME} · สต็อก {int(it['amount'])}")
    text = "\n".join(lines)
    # embed description รับได้ ~4096 chars; ตัดกันเกินกรณีหมวดยาวมาก
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    e.description = text or "_(ว่าง)_"
    e.set_footer(text=f"{len(items)} รายการ")
    return e


async def _send_market_groups(interaction: discord.Interaction, items):
    """Send for-sale items grouped by type as embeds (Discord caps 10 embeds/message)."""
    groups = _group_market_by_type(items)
    embeds = [market_group_embed(name, its) for _tid, name, its in groups]
    first, rest = embeds[:10], embeds[10:]
    await interaction.response.send_message(embeds=first, ephemeral=True)
    while rest:
        chunk, rest = rest[:10], rest[10:]
        await interaction.followup.send(embeds=chunk, ephemeral=True)


class MarketTypeView(discord.ui.View):
    """Pick an item type from the dropdown, then press Show to list its for-sale items.
    Only items the shop SELLS (enabled_isforsell=1, in stock) are listed — buy-only items
    are excluded here (they live on the website's 'Sell to shop' board)."""

    def __init__(self, types):
        super().__init__(timeout=180)
        self.selected_type = None  # None = all types
        opts = [discord.SelectOption(label="ทั้งหมด / All types", value="all", default=True)]
        for t in (types or [])[:24]:  # Discord caps a select at 25 options
            label = str(t.get("name") or t["id"])[:100]
            opts.append(discord.SelectOption(label=label, value=str(t["id"])))
        self.select = discord.ui.Select(
            placeholder="เลือกหมวดสินค้า / Select item type",
            min_values=1, max_values=1, options=opts, row=0)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        v = self.select.values[0]
        self.selected_type = None if v == "all" else int(v)
        await interaction.response.defer()  # ack; keep the picker message

    @discord.ui.button(label="📋 แสดง / Show", style=discord.ButtonStyle.primary, row=1)
    async def show(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = await asyncio.to_thread(
            db.list_market, False, config.BILL_ITEM_IDS, None, self.selected_type)
        if not items:
            await interaction.response.send_message(
                "ไม่พบรายการขายในหมวดนี้ | No items for sale here.", ephemeral=True)
            return
        await _send_market_groups(interaction, items)


@bot.tree.command(description="ดูรายการตลาดตามหมวด / browse market by type")
async def market(interaction: discord.Interaction):
    types = await asyncio.to_thread(db.list_item_types)
    await interaction.response.send_message(
        "เลือกหมวดสินค้าแล้วกด **แสดง** / pick a type, then press **Show**:",
        view=MarketTypeView(types), ephemeral=True)


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


def _quest_status_emoji(q) -> str:
    return "✅" if q.get("completed") else "🟡"


def _quest_progress_line(it) -> str:
    done = int(it["sold_qty"]) >= int(it["qty_required"])
    mark = "✅" if done else "❌"
    return f"{mark} {it['sold_qty']}/{it['qty_required']} {it['item_name']}"


def quest_summary_embed(quests) -> discord.Embed:
    e = discord.Embed(title="📜 เควสต์ปัจจุบัน / Active Quests", color=0xF1C40F)
    if not quests:
        e.description = "ยังไม่มีเควสต์ที่เปิดอยู่ในช่วงนี้"
        return e
    for q in quests:
        parts = [_quest_progress_line(it) for it in q["items"]]
        status = _quest_status_emoji(q)
        body = " + ".join(parts) if parts else "_(ไม่มี items)_"
        e.add_field(
            name=f"{status} #{q['id']} {q['name']} — รางวัล {int(q['reward_coins'])} {config.COIN_NAME}",
            value=f"{body}\n_reset: {q['reset_type']} · period: `{q['period_key']}`_",
            inline=False)
    e.set_footer(text="ขายไอเทมที่กำหนดในเกมเพื่อสะสม progress · /quest <id> ดูรายละเอียด")
    return e


def quest_detail_embed(q) -> discord.Embed:
    color = 0x2ECC71 if q.get("completed") else 0x3498DB
    e = discord.Embed(title=f"📜 #{q['id']} {q['name']}",
                      description=q.get("description") or "_(ไม่มีคำอธิบาย)_",
                      color=color)
    e.add_field(name="รางวัล / Reward",
                value=f"{int(q['reward_coins'])} {config.COIN_NAME}", inline=True)
    e.add_field(name="Reset", value=str(q["reset_type"]), inline=True)
    e.add_field(name="Period", value=f"`{q['period_key']}`", inline=True)
    if q["items"]:
        lines = [_quest_progress_line(it) for it in q["items"]]
        e.add_field(name="ความคืบหน้า / Progress", value="\n".join(lines), inline=False)
    else:
        e.add_field(name="ความคืบหน้า / Progress", value="_(ไม่มี items)_", inline=False)
    if q.get("completed"):
        when = q.get("completed_at")
        e.set_footer(text=f"เสร็จสมบูรณ์แล้ว ✅ {when or ''}".strip())
    return e


@bot.tree.command(description="ดูเควสต์ที่เปิดอยู่ + ความคืบหน้า / list quests")
async def quests(interaction: discord.Interaction):
    steam = await asyncio.to_thread(db.get_steam_by_discord, interaction.user.id)
    if steam is None:
        await interaction.response.send_message("ยังไม่ได้เชื่อมบัญชี กดปุ่ม Welcome Pack ก่อน", ephemeral=True)
        return
    qs = await asyncio.to_thread(db.list_quests_with_progress, steam, True)
    await interaction.response.send_message(embed=quest_summary_embed(qs), ephemeral=True)


@bot.tree.command(description="ดูรายละเอียดเควสต์ตาม id / quest detail")
@app_commands.describe(quest_id="หมายเลขเควสต์ (ดูจาก /quests)")
async def quest(interaction: discord.Interaction, quest_id: int):
    steam = await asyncio.to_thread(db.get_steam_by_discord, interaction.user.id)
    if steam is None:
        await interaction.response.send_message("ยังไม่ได้เชื่อมบัญชี กดปุ่ม Welcome Pack ก่อน", ephemeral=True)
        return
    q = await asyncio.to_thread(db.get_quest_detail, quest_id, steam)
    if not q:
        await interaction.response.send_message(f"ไม่พบเควสต์ #{quest_id}", ephemeral=True)
        return
    await interaction.response.send_message(embed=quest_detail_embed(q), ephemeral=True)


# ---------------- admin: modals ----------------

class MarketModal(discord.ui.Modal, title="ตั้ง/แก้ รายการตลาด"):
    # โมเดลราคา supply/demand: base_price = ราคาฐาน, target_stock = สต็อกเป้าหมาย, elasticity = ความยืดหยุ่น
    # name/image/type_id ย้ายไปอยู่ที่ sv_items (master catalog) — แก้ผ่านปุ่ม "📦 จัดการ Items"
    m_item = discord.ui.TextInput(label="Item ID (ต้องมีอยู่ใน catalog แล้ว)", max_length=10)
    m_base_price = discord.ui.TextInput(label="Base price (Coin) — ราคาฐาน", max_length=12)
    m_stock = discord.ui.TextInput(
        label="Stock / target / elas",
        placeholder="เช่น  10 / 100 / 1.0  (เว้น target = stock, elas = 1.0)",
        max_length=64)

    def __init__(self, prefill=None):
        super().__init__()
        if prefill:
            self.m_item.default = str(prefill["item_id"])
            bp = prefill.get("base_price")
            if bp is None or float(bp) == 0:
                bp = prefill.get("price") or 0
            self.m_base_price.default = str(int(round(float(bp))))
            stock = int(prefill["amount"])
            tgt = int(prefill.get("target_stock") or 0) or stock or 100
            elas = float(prefill.get("elasticity") or 1.0) or 1.0
            self.m_stock.default = f"{stock} / {tgt} / {elas}"

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.m_stock.value).strip()
        parts = [p.strip() for p in raw.split("/")]
        try:
            iid = int(str(self.m_item.value))
            bp = float(str(self.m_base_price.value))
            amt = int(parts[0]) if parts and parts[0] else 0
            tgt_raw = parts[1] if len(parts) > 1 and parts[1] else ""
            elas_raw = parts[2] if len(parts) > 2 and parts[2] else ""
            target_stock = int(tgt_raw) if tgt_raw else (amt if amt > 0 else 100)
            elasticity = float(elas_raw) if elas_raw else 1.0
        except ValueError:
            await interaction.response.send_message(
                "ตัวเลขไม่ถูกต้อง (Item ID / Base price / Stock-Target-Elas)", ephemeral=True)
            return
        # ต้องมีใน master catalog ก่อน — กันสร้าง market row ที่ JOIN แล้วได้ name=NULL
        exists = await asyncio.to_thread(db.item_exists, iid)
        if not exists:
            await interaction.response.send_message(
                f"Item ID `{iid}` ยังไม่อยู่ใน catalog — เพิ่มผ่าน '📦 จัดการ Items' ก่อน",
                ephemeral=True)
            return
        await asyncio.to_thread(db.upsert_market_item, iid, bp, amt, target_stock, elasticity)
        item = await asyncio.to_thread(db.get_item, iid)
        nm = (item or {}).get("name") or f"item {iid}"
        await interaction.response.send_message(
            f"ตั้งรายการ `{iid}` **{nm}** base {int(round(bp))} "
            f"stock {amt} target {target_stock} elas {elasticity} ✅\n*(ในเกม /sellreload)*",
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


# ---------------- admin: quests + item types ----------------

def _parse_quest_items(raw: str):
    """แปลง "121:10 / 122:5 / 130:3" -> [(121,10),(122,5),(130,3)]"""
    out = []
    for tok in str(raw or "").split("/"):
        tok = tok.strip()
        if not tok:
            continue
        if ":" not in tok:
            raise ValueError(f"ไม่มี ':' ใน '{tok}'")
        sid, amt = tok.split(":", 1)
        out.append((int(sid.strip()), int(amt.strip())))
    if not out:
        raise ValueError("ต้องมีอย่างน้อย 1 ไอเทม")
    return out


class QuestCreateModal(discord.ui.Modal, title="📜 สร้างเควสต์"):
    # Discord modal limit = 5 fields. items field ใช้รูปแบบคั่นด้วย ' / ' เพื่อใส่หลายไอเทม
    m_name = discord.ui.TextInput(label="ชื่อ / Name", max_length=128)
    m_desc = discord.ui.TextInput(label="คำอธิบาย / Description",
                                  style=discord.TextStyle.paragraph,
                                  required=False, max_length=500)
    m_reward = discord.ui.TextInput(label="รางวัล (Coin)", max_length=12)
    m_reset = discord.ui.TextInput(label="Reset type (once / daily / weekly)",
                                   default="daily", max_length=10)
    m_items = discord.ui.TextInput(label="Items (id:qty / id:qty / ...)",
                                   placeholder="121:10 / 122:5 / 130:3", max_length=300)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            reward = int(str(self.m_reward.value))
            items = _parse_quest_items(str(self.m_items.value))
        except ValueError as e:
            await interaction.response.send_message(f"รูปแบบไม่ถูกต้อง: {e}", ephemeral=True)
            return
        reset = str(self.m_reset.value).strip().lower() or "daily"
        if reset not in ("once", "daily", "weekly"):
            await interaction.response.send_message("Reset type ต้องเป็น once / daily / weekly", ephemeral=True)
            return
        try:
            qid = await asyncio.to_thread(
                db.create_quest, str(self.m_name.value),
                str(self.m_desc.value) or None, reward, reset, items)
        except Exception:
            log.exception("create_quest failed")
            await interaction.response.send_message("สร้างเควสต์ล้มเหลว ดู log", ephemeral=True)
            return
        item_lines = "\n".join(f"  • `{i}` x{q}" for i, q in items)
        await interaction.response.send_message(
            f"สร้างเควสต์ `#{qid}` **{self.m_name.value}** ({reset}) รางวัล {reward} "
            f"{config.COIN_NAME} ✅\n{item_lines}", ephemeral=True)


class QuestEditModal(discord.ui.Modal, title="✏️ แก้เควสต์"):
    m_id = discord.ui.TextInput(label="Quest ID", max_length=10)
    m_name = discord.ui.TextInput(label="ชื่อ (เว้น = ไม่เปลี่ยน)", required=False, max_length=128)
    m_reward = discord.ui.TextInput(label="รางวัล / reset (เช่น  500 / daily)",
                                    placeholder="เว้นว่าง = คงเดิม",
                                    required=False, max_length=40)
    m_desc = discord.ui.TextInput(label="คำอธิบาย (เว้น = ไม่เปลี่ยน)",
                                  style=discord.TextStyle.paragraph,
                                  required=False, max_length=500)
    m_items = discord.ui.TextInput(label="Items (เว้น = ไม่เปลี่ยน)",
                                   placeholder="121:10 / 122:5",
                                   required=False, max_length=300)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qid = int(str(self.m_id.value))
        except ValueError:
            await interaction.response.send_message("Quest ID ไม่ถูกต้อง", ephemeral=True)
            return
        cur = await asyncio.to_thread(db.get_quest_detail, qid, 0)
        if not cur:
            await interaction.response.send_message(f"ไม่พบเควสต์ #{qid}", ephemeral=True)
            return

        name = str(self.m_name.value).strip() or cur["name"]
        desc_raw = str(self.m_desc.value)
        desc = desc_raw if desc_raw.strip() else (cur.get("description") or "")

        reward = int(cur["reward_coins"])
        reset = cur["reset_type"]
        rr_raw = str(self.m_reward.value).strip()
        if rr_raw:
            parts = [p.strip() for p in rr_raw.split("/")]
            try:
                if parts[0]:
                    reward = int(parts[0])
                if len(parts) > 1 and parts[1]:
                    cand = parts[1].lower()
                    if cand in ("once", "daily", "weekly"):
                        reset = cand
            except ValueError:
                await interaction.response.send_message("รางวัล/reset ไม่ถูกต้อง", ephemeral=True)
                return

        items_raw = str(self.m_items.value).strip()
        if items_raw:
            try:
                items = _parse_quest_items(items_raw)
            except ValueError as e:
                await interaction.response.send_message(f"รูปแบบ items ผิด: {e}", ephemeral=True)
                return
        else:
            items = [(it["item_id"], it["qty_required"]) for it in cur["items"]]

        try:
            await asyncio.to_thread(db.update_quest, qid, name, desc, reward, reset, items, True)
        except Exception:
            log.exception("update_quest failed")
            await interaction.response.send_message("แก้เควสต์ล้มเหลว ดู log", ephemeral=True)
            return
        await interaction.response.send_message(
            f"แก้เควสต์ `#{qid}` **{name}** ({reset}) รางวัล {reward} ✅", ephemeral=True)


class QuestDeleteModal(discord.ui.Modal, title="🗑️ ลบเควสต์"):
    m_id = discord.ui.TextInput(label="Quest ID", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qid = int(str(self.m_id.value))
        except ValueError:
            await interaction.response.send_message("Quest ID ไม่ถูกต้อง", ephemeral=True)
            return
        ok = await asyncio.to_thread(db.delete_quest, qid)
        await interaction.response.send_message(
            f"ลบเควสต์ #{qid} ✅" if ok else f"ไม่พบเควสต์ #{qid}", ephemeral=True)


class ItemTypeModal(discord.ui.Modal, title="🏷️ จัดการ Item Types"):
    # ใช้ฟอร์มเดียวสำหรับ add/edit/delete: เว้น id = สร้างใหม่; เว้น name+desc แต่ใส่ id+'del' = ลบ
    m_id = discord.ui.TextInput(label="Type ID (เว้น = สร้างใหม่)", required=False, max_length=10)
    m_name = discord.ui.TextInput(label="ชื่อ / Name (เว้น+มี ID = ลบ)", required=False, max_length=64)
    m_desc = discord.ui.TextInput(label="คำอธิบาย / Description",
                                  required=False, style=discord.TextStyle.paragraph, max_length=250)

    async def on_submit(self, interaction: discord.Interaction):
        tid_raw = str(self.m_id.value).strip()
        name = str(self.m_name.value).strip()
        desc = str(self.m_desc.value).strip() or None
        tid = None
        if tid_raw:
            try:
                tid = int(tid_raw)
            except ValueError:
                await interaction.response.send_message("Type ID ไม่ถูกต้อง", ephemeral=True)
                return
        # ลบ: มี ID แต่ไม่มี name
        if tid is not None and not name:
            ok = await asyncio.to_thread(db.delete_item_type, tid)
            await interaction.response.send_message(
                f"ลบ Type #{tid} ✅" if ok else f"ไม่พบ Type #{tid}", ephemeral=True)
            return
        if not name:
            await interaction.response.send_message("ต้องระบุชื่อสำหรับสร้าง/แก้", ephemeral=True)
            return
        new_id = await asyncio.to_thread(db.upsert_item_type, tid, name, desc)
        verb = "แก้" if tid else "สร้าง"
        await interaction.response.send_message(
            f"{verb} Type `#{new_id}` **{name}** ✅", ephemeral=True)


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

    @discord.ui.button(label="📜 สร้างเควสต์", style=discord.ButtonStyle.success, custom_id="admin_quest_create",
                       row=2)
    async def b_quest_create(self, interaction, button):
        if await self._guard(interaction):
            await interaction.response.send_modal(QuestCreateModal())

    @discord.ui.button(label="✏️ แก้เควสต์", style=discord.ButtonStyle.primary, custom_id="admin_quest_edit",
                       row=2)
    async def b_quest_edit(self, interaction, button):
        if await self._guard(interaction):
            await interaction.response.send_modal(QuestEditModal())

    @discord.ui.button(label="🗑️ ลบเควสต์", style=discord.ButtonStyle.danger, custom_id="admin_quest_delete",
                       row=2)
    async def b_quest_delete(self, interaction, button):
        if await self._guard(interaction):
            await interaction.response.send_modal(QuestDeleteModal())

    @discord.ui.button(label="🏷️ จัดการ Item Types", style=discord.ButtonStyle.secondary,
                       custom_id="admin_item_types", row=2)
    async def b_item_types(self, interaction, button):
        if not await self._guard(interaction):
            return
        types = await asyncio.to_thread(db.list_item_types)
        if types:
            preview = "\n".join(f"`{t['id']}` · {t['name']}" +
                                (f" — {t['description']}" if t.get('description') else '')
                                for t in types[:20])
        else:
            preview = "_(ยังไม่มี type — ใส่ชื่อในฟอร์มเพื่อสร้าง)_"
        await interaction.response.send_message(
            f"**Item Types ปัจจุบัน:**\n{preview}\n\nกดเปิดฟอร์มเพื่อเพิ่ม/แก้/ลบ:",
            view=ItemTypeFormLauncher(), ephemeral=True)

    @discord.ui.button(label="📦 จัดการ Items", style=discord.ButtonStyle.primary,
                       custom_id="admin_items", row=3)
    async def b_items(self, interaction, button):
        if not await self._guard(interaction):
            return
        items = await asyncio.to_thread(db.list_items)
        if items:
            head = items[:20]
            preview = "\n".join(
                f"`{i['id']}` · **{i['name']}**" +
                (f" · type `{i['type_id']}`" if i.get('type_id') else '') +
                (f" · 🖼️" if i.get('image_url') else '')
                for i in head)
            tail = f"\n_(แสดง 20/{len(items)})_" if len(items) > 20 else ""
            preview += tail
        else:
            preview = "_(ยังไม่มี item — ใส่ ID + name ในฟอร์มเพื่อสร้าง)_"
        await interaction.response.send_message(
            f"**Master Catalog (sv_items):**\n{preview}\n\nกดเปิดฟอร์มเพื่อเพิ่ม/แก้/ลบ:",
            view=ItemFormLauncher(), ephemeral=True)


class ItemTypeFormLauncher(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="เปิดฟอร์ม Item Type", style=discord.ButtonStyle.primary)
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
            return
        await interaction.response.send_modal(ItemTypeModal())


class ItemFormLauncher(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="เปิดฟอร์ม Item", style=discord.ButtonStyle.primary)
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
            return
        await interaction.response.send_modal(ItemModal())


class ItemModal(discord.ui.Modal, title="📦 จัดการ Items (catalog)"):
    # โมเดล CRUD เดียวกับ ItemTypeModal: blank ID = สร้างใหม่; ID + blank name = ลบ
    m_id = discord.ui.TextInput(label="Item ID (ต้องระบุเสมอ)", max_length=10)
    m_name = discord.ui.TextInput(label="ชื่อ / Name (เว้น+มี ID = ลบ)",
                                  required=False, max_length=64)
    m_desc = discord.ui.TextInput(label="คำอธิบาย / Description (optional)",
                                  required=False, style=discord.TextStyle.paragraph,
                                  max_length=500)
    m_image = discord.ui.TextInput(label="ลิงก์รูป / Image URL (optional)",
                                   required=False, max_length=400)
    m_type = discord.ui.TextInput(label="Type ID (optional, ดูจาก 'จัดการ Item Types')",
                                  required=False, max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            iid = int(str(self.m_id.value))
        except ValueError:
            await interaction.response.send_message("Item ID ไม่ถูกต้อง", ephemeral=True)
            return
        name = str(self.m_name.value).strip()
        # ลบ: มี ID แต่ไม่มี name
        if not name:
            ok, reason = await asyncio.to_thread(db.delete_item, iid)
            if ok:
                await interaction.response.send_message(f"ลบ Item `{iid}` ✅", ephemeral=True)
            elif reason == "not_found":
                await interaction.response.send_message(f"ไม่พบ Item `{iid}`", ephemeral=True)
            elif reason == "referenced_market":
                await interaction.response.send_message(
                    f"ลบไม่ได้ — `{iid}` ยังอยู่ใน sv_market (ลบจากตลาดก่อน)", ephemeral=True)
            elif reason == "referenced_quest":
                await interaction.response.send_message(
                    f"ลบไม่ได้ — `{iid}` ยังถูกใช้ใน sv_quest_items (ลบจากเควสต์ก่อน)", ephemeral=True)
            else:
                await interaction.response.send_message(f"ลบไม่ได้: {reason}", ephemeral=True)
            return

        desc = str(self.m_desc.value).strip() or None
        image = str(self.m_image.value).strip() or None
        tid_raw = str(self.m_type.value).strip()
        try:
            tid = int(tid_raw) if tid_raw else None
        except ValueError:
            await interaction.response.send_message("Type ID ไม่ถูกต้อง", ephemeral=True)
            return
        try:
            await asyncio.to_thread(db.upsert_item, iid, name, desc, image, tid)
        except Exception:
            log.exception("upsert_item failed")
            await interaction.response.send_message("upsert ล้มเหลว ดู log", ephemeral=True)
            return
        tid_msg = f" type {tid}" if tid is not None else ""
        await interaction.response.send_message(
            f"บันทึก Item `{iid}` **{name}**{tid_msg} ✅", ephemeral=True)


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


@bot.tree.command(description="[admin] ซิงก์ Role VIP กับสถานะ VIP ตอนนี้ทันที")
async def syncvip(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("ต้องเป็นแอดมิน", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        res = await _reconcile_vip_roles()
    except Exception:
        log.exception("/syncvip failed")
        await interaction.followup.send("❌ เกิดข้อผิดพลาดระหว่างซิงก์ — ดู log", ephemeral=True)
        return

    if not res["ok"]:
        reason = res.get("reason", "unknown")
        msg = {
            "no_guild_id": "❌ ยังไม่ได้ตั้ง GUILD_ID ใน .env",
            "guild_not_found": "❌ บอทไม่อยู่ในกิลด์ตาม GUILD_ID",
            "role_not_found": f"❌ ไม่พบ Role id `{config.VIP_ROLE_ID}` ในกิลด์",
            "no_members": ("❌ มองไม่เห็นสมาชิก — ต้องเปิด **Server Members Intent** ใน "
                           "Developer Portal ก่อน (ข้ามการซิงก์เพื่อกันลบ role ผิดพลาด)"),
        }.get(reason, f"❌ ซิงก์ไม่สำเร็จ ({reason})")
        await interaction.followup.send(msg, ephemeral=True)
        return

    tag = " _(dry-run — ไม่ได้แก้จริง)_" if res["dryrun"] else ""
    await interaction.followup.send(
        f"✅ ซิงก์ VIP role เสร็จ{tag}\n"
        f"• เพิ่ม: **{res['added']}**\n"
        f"• ลบ: **{res['removed']}**\n"
        f"• VIP ที่ยังไม่ผูก Discord (ข้าม): **{res['skipped_unlinked_vips']}**",
        ephemeral=True,
    )


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
        p2p_ch = await ensure_channel("🤝-p2p-market")
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

        # Note: 🤝-p2p-market is the auto-feed — don't purge it (would delete live listing
        # posts and their Buy buttons; announced_at already marks them as posted).
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
        await p2p_ch.send(embed=discord.Embed(
            title="🤝 ตลาดผู้เล่น / Player Market",
            description=("ประกาศขายของผู้เล่นจะโผล่ที่นี่อัตโนมัติ — กด **ซื้อ / Buy** ใต้รายการเพื่อซื้อด้วย Coin → ได้โค้ด → /code ในเกม\n"
                         "New player listings post here automatically — tap **Buy** under an item to purchase with Coin.\n\n"
                         f"🌐 ลงขาย/ดูทั้งหมดบนเว็บ · List & browse on the web: {config.WEB_SHOP_URL}"),
            color=0x1ABC9C))
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
            c.mention for c in (welcome, coins_ch, shop_ch, bills_ch, market_ch, p2p_ch, lb, cmds_ch, admin_ch)),
        ephemeral=True)


# ---------------- notifications outbox (DM the seller) ----------------
# The shop-api writes rows into sv_notifications (dm_status='pending') — e.g. when a
# P2P listing expires/cancels and a redeem code is minted. Only the bot holds the
# Discord token, so it polls the table, DMs the recipient, and marks the outcome.
# Mirrors the username-backfill loop pattern (DB-poll + asyncio.to_thread + status as
# the idempotency marker). The bot never creates sv_notifications; the api owns its DDL.

NOTIFY_POLL_SECONDS = 20
NOTIFY_BATCH_LIMIT = 25       # rows handled per tick
NOTIFY_MAX_ATTEMPTS = 5       # transient-error retries before giving up (-> 'failed')


async def dm_user(discord_id: int, embed: discord.Embed) -> str:
    """
    Best-effort DM to a user resolved by id. Returns:
      'sent'   — delivered
      'failed' — user unreachable (DMs closed, blocked, or no such user) — don't retry
      'error'  — transient failure (network/Discord 5xx) — safe to retry later
    """
    try:
        user = await bot.fetch_user(discord_id)
    except discord.NotFound:
        log.warning("notify: discord user %s not found", discord_id)
        return "failed"
    except discord.HTTPException:
        log.exception("notify: fetch_user failed for %s", discord_id)
        return "error"
    try:
        await user.send(embed=embed)
        return "sent"
    except discord.Forbidden:
        # DMs disabled / bot blocked / no shared guild — permanent for this recipient.
        log.info("notify: cannot DM %s (Forbidden)", discord_id)
        return "failed"
    except discord.HTTPException:
        log.exception("notify: user.send failed for %s", discord_id)
        return "error"


# Per-kind bilingual copy (Thai primary + English second line), keyed by sv_notifications.kind.
# {item_name} {qty} are substituted; %s in the desc holds the second-line break. Approved copy.
_NOTIFY_COPY = {
    "p2p_expired": {
        "title": "⌛ ประกาศขายหมดเวลา · Listing expired",
        "th": "ประกาศขาย **{item_name}** ×{qty} ของคุณหมดเวลาแล้ว — ระบบเปลี่ยนไอเทมเป็นโค้ดรับของให้",
        "en": "Your listing for **{item_name}** ×{qty} has expired — your item is now a redeem code.",
    },
    "p2p_cancelled": {
        "title": "🚫 ยกเลิกประกาศขาย · Listing cancelled",
        "th": "คุณยกเลิกประกาศขาย **{item_name}** ×{qty} — รับไอเทมคืนผ่านโค้ดนี้",
        "en": "You cancelled your listing for **{item_name}** ×{qty} — claim your item with this code.",
    },
    "p2p_force_closed": {
        "title": "🛠️ ประกาศถูกปิดโดยแอดมิน · Listing closed by admin",
        "th": "ประกาศขาย **{item_name}** ×{qty} ของคุณถูกปิดโดยแอดมิน — รับไอเทมคืนผ่านโค้ดนี้",
        "en": "Your listing for **{item_name}** ×{qty} was closed by an admin — claim your item with this code.",
    },
    "raid_alert": {
        "title": "⚔️ บ้านถูกโจมตี · Base under attack",
        "th": "**{item_name}** กำลัง Raid บ้านของคุณ!",
        "en": "**{item_name}** is raiding your base!",
    },
    "raid_alert_offhours": {
        "title": "🚨 Raid นอกเวลา · Off-hours raid blocked",
        "th": "**{item_name}** พยายาม Raid บ้านคุณนอกช่วงเวลา — ผู้โจมตีถูกกักกันแล้ว",
        "en": "**{item_name}** tried to raid your base outside hours — attacker exiled.",
    },
}


def _expires_value(payload: dict):
    """Discord relative+full timestamp from code_expires_at_unix (epoch seconds);
    fall back to the raw ISO code_expires_at string if the unix field is missing/bad."""
    unix = payload.get("code_expires_at_unix")
    try:
        if unix is not None:
            u = int(unix)
            return f"<t:{u}:R> · <t:{u}:f>"
    except (TypeError, ValueError):
        pass
    iso = payload.get("code_expires_at")
    return str(iso) if iso else None


def notification_embed(kind: str, payload: dict) -> discord.Embed:
    """
    Render an sv_notifications outbox row into a bilingual DM embed, keyed by kind.
    payload keys (api contract, all optional so a sparse row still renders):
      item_name, amount, image_url, code, code_expires_at (ISO),
      code_expires_at_unix (epoch seconds).
    """
    item_name = payload.get("item_name") or "ไอเทม / item"
    qty = payload.get("amount") or 1
    code = payload.get("code")

    copy = _NOTIFY_COPY.get(kind)
    if copy:
        title = copy["title"]
        th = copy["th"].format(item_name=item_name, qty=qty)
        en = copy["en"].format(item_name=item_name, qty=qty)
        desc = f"{th}\n{en}"
    else:
        # Unknown kind — still deliver something useful rather than dropping the DM.
        title = "🔔 แจ้งเตือน · Notification"
        desc = (f"อัปเดตเกี่ยวกับ **{item_name}** ×{qty}\n"
                f"Update on **{item_name}** ×{qty}.")

    is_raid = kind.startswith("raid_")
    color = 0xE74C3C if is_raid else 0xE67E22
    e = discord.Embed(title=title, description=desc, color=color)
    image_url = payload.get("image_url")
    if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
        e.set_thumbnail(url=image_url)
    if code:
        e.add_field(name="🎟️ โค้ด · Code", value=f"`{code}`", inline=False)
        e.add_field(name="🎮 รับในเกม · Claim", value=f"`/code {code}`", inline=False)
    expires = _expires_value(payload)
    if expires:
        e.add_field(name="⏳ หมดอายุ · Expires", value=expires, inline=False)
    if not is_raid:
        e.set_footer(text="⚠️ ใช้ก่อนหมดอายุ ไม่งั้นไอเทมจะหายถาวร · Redeem before it expires or the item is lost.")
    return e


async def _process_notification(row: dict):
    nid = int(row["id"])
    discord_id = row.get("discord_id")
    if not discord_id:
        steam_id = row.get("steam_id")
        if steam_id is not None:
            discord_id = await asyncio.to_thread(db.get_discord_by_steam, int(steam_id))
    if not discord_id:
        # No way to reach a Discord user — don't keep retrying forever.
        log.warning("notify: row %s has no resolvable discord_id; marking failed", nid)
        await asyncio.to_thread(db.mark_notification, nid, "failed")
        return

    raw = row.get("payload")
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else (raw or {})
        if not isinstance(payload, dict):
            payload = {}
    except (ValueError, TypeError):
        log.warning("notify: row %s has unparseable payload", nid)
        payload = {}

    embed = notification_embed(row.get("kind") or "", payload)
    result = await dm_user(int(discord_id), embed)
    if result == "sent":
        await asyncio.to_thread(db.mark_notification, nid, "sent")
    elif result == "failed":
        await asyncio.to_thread(db.mark_notification, nid, "failed")
    else:  # 'error' — transient. Retry until the attempt cap, then give up.
        next_attempts = int(row.get("dm_attempts") or 0) + 1
        status = "failed" if next_attempts >= NOTIFY_MAX_ATTEMPTS else "pending"
        await asyncio.to_thread(db.mark_notification, nid, status)


# ---------------- p2p market feed (auto-announce + Buy button) ----------------
# The bot polls sv_p2p_listings for new active listings (announced_at IS NULL), posts an
# embed + Buy button to the P2P feed channel, then stamps announced_at so it posts once.
# The Buy button calls the shop-api (logic stays server-side); the bot only resolves the
# buyer's steam_id and relays the API result. Buy buttons are persistent via DynamicItem
# (the listing id lives in the custom_id), so they survive bot restarts.

P2P_FEED_POLL_SECONDS = 25
P2P_FEED_BATCH_LIMIT = 10     # listings posted per tick (rate-friendly)
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


def p2p_listing_embed(row: dict) -> discord.Embed:
    name = row.get("name") or f"item {row.get('item_id')}"
    amount = int(row.get("amount") or 1)
    price = int(round(float(row.get("price") or 0)))
    quality = row.get("quality")
    seller_discord = row.get("seller_discord")
    seller = f"<@{int(seller_discord)}>" if seller_discord else "ผู้เล่น / a player"

    e = discord.Embed(title=f"🤝 {name}", color=0x1ABC9C)
    e.add_field(name="จำนวน · Amount", value=f"×{amount}", inline=True)
    e.add_field(name="ราคา · Price", value=f"{price} {config.COIN_NAME}", inline=True)
    if quality is not None:
        e.add_field(name="สภาพ · Quality", value=f"{int(quality)}%", inline=True)
    e.add_field(name="ผู้ขาย · Seller", value=seller, inline=False)
    image_url = row.get("image_url")
    if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
        e.set_thumbnail(url=image_url)
    e.set_footer(text=f"listing #{row.get('id')} · กดซื้อด้านล่าง / tap Buy below")
    return e


async def _execute_p2p_buy(listing_id: int, discord_id: int):
    """
    POST the buy to the shop-api (which owns the purchase + buy+claim code-mint).
    Returns (kind, payload):
      ('ok', data)            — 200/201; data = {redeem_code, item_name, amount, price}
      ('unconfigured', None)  — API_BASE_URL / BOT_API_SECRET missing
      ('http', (status, msg)) — error; msg is the Nest `message` token (or "")
      ('error', None)         — network/transport failure
    """
    if not config.API_BASE_URL or not config.BOT_API_SECRET:
        return ("unconfigured", None)
    url = f"{config.API_BASE_URL}/bot/p2p/listings/{int(listing_id)}/buy"
    headers = {"X-Bot-Secret": config.BOT_API_SECRET, "Content-Type": "application/json"}
    body = {"discord_id": str(discord_id)}
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status in (200, 201):
                    return ("ok", await resp.json())
                # Nest error body: { statusCode, message, error }. message may be a
                # string token or a list (validation); normalise to a single token.
                token = ""
                try:
                    data = await resp.json()
                    msg = data.get("message")
                    token = msg[0] if isinstance(msg, list) and msg else (msg or "")
                except Exception:
                    pass
                return ("http", (resp.status, str(token)))
    except Exception:
        log.exception("p2p buy call failed for listing %s", listing_id)
        return ("error", None)


# Bilingual messages keyed by the api's documented Nest `message` token.
_P2P_BUY_TOKEN_MSG = {
    "discord_not_linked": ("ยังไม่ได้เชื่อมบัญชี กดปุ่ม Welcome Pack ก่อน · "
                           "Link your account first (tap the Welcome Pack button)."),
    "cannot_buy_own_listing": "ซื้อ listing ของตัวเองไม่ได้ · You can't buy your own listing.",
    "insufficient_funds": "เหรียญไม่พอ · Not enough coins.",
    "Listing not found": "ไม่พบรายการนี้ · Listing not found.",
    "listing_no_longer_active": "ขายไปแล้ว/ปิดแล้ว · Already sold or closed.",
}
_P2P_BUY_AUTH_TOKENS = ("bot_auth_failed", "bot_auth_unavailable")
_P2P_BUY_GENERIC = "ซื้อไม่สำเร็จ ลองใหม่ · Purchase failed, try again."
_P2P_BUY_UNAVAILABLE = "ระบบซื้อยังไม่พร้อม · Purchase service unavailable."


async def _handle_p2p_buy_click(interaction: discord.Interaction, listing_id: int):
    await interaction.response.defer(ephemeral=True)
    # Pre-check links to short-circuit before the round-trip; the api's
    # discord_not_linked is the backstop.
    steam = await asyncio.to_thread(db.get_steam_by_discord, interaction.user.id)
    if steam is None:
        await interaction.followup.send(_P2P_BUY_TOKEN_MSG["discord_not_linked"], ephemeral=True)
        return

    kind, payload = await _execute_p2p_buy(listing_id, interaction.user.id)
    if kind == "ok":
        data = payload or {}
        name = data.get("item_name") or "item"
        amount = data.get("amount") or 1
        code = data.get("redeem_code")
        if code:
            await interaction.followup.send(
                f"ซื้อสำเร็จ! · Purchased **{name}** ×{amount}\n"
                f"เข้าเกมพิมพ์ · In-game type:\n```/code {code}```",
                ephemeral=True)
        else:
            # Rare: coins moved but the code wasn't returned. Don't imply failure —
            # point them to the web /codes to claim.
            await interaction.followup.send(
                f"ซื้อสำเร็จ **{name}** ×{amount} · Purchase went through.\n"
                f"แต่โค้ดยังไม่ขึ้น — เช็ค/รับโค้ดที่เว็บ · No code shown yet; claim it at "
                f"{config.WEB_SHOP_URL}/codes",
                ephemeral=True)
        return

    if kind == "unconfigured":
        log.error("p2p buy: API_BASE_URL/BOT_API_SECRET not configured")
        await interaction.followup.send(_P2P_BUY_UNAVAILABLE, ephemeral=True)
        return
    if kind == "error":
        await interaction.followup.send(
            "เกิดข้อผิดพลาดในการเชื่อมต่อ ลองใหม่ · Connection error, try again.", ephemeral=True)
        return

    # kind == "http": switch on the message token, fall back to status/generic.
    status, token = payload
    if token in _P2P_BUY_AUTH_TOKENS:
        log.error("p2p buy: bot auth problem (status=%s token=%s)", status, token)
        await interaction.followup.send(_P2P_BUY_UNAVAILABLE, ephemeral=True)
        return
    await interaction.followup.send(_P2P_BUY_TOKEN_MSG.get(token, _P2P_BUY_GENERIC), ephemeral=True)


class P2PBuyButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"p2p_buy:(?P<id>[0-9]+)"):
    """Persistent Buy button whose listing id is encoded in the custom_id, so it keeps
    working after a restart without re-registering per-message views."""
    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(
            discord.ui.Button(
                label="🛒 ซื้อ / Buy",
                style=discord.ButtonStyle.success,
                custom_id=f"p2p_buy:{listing_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await _handle_p2p_buy_click(interaction, self.listing_id)


def p2p_buy_view(listing_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(P2PBuyButton(listing_id))
    return view


async def _announce_listing(channel, row: dict) -> bool:
    """Post one listing to the feed; stamp announced_at only on success. Returns True if
    posted (so the loop can pace itself). Leaves announced_at NULL on failure to retry."""
    lid = int(row["id"])
    try:
        await channel.send(embed=p2p_listing_embed(row), view=p2p_buy_view(lid))
    except Exception:
        log.exception("p2p feed: failed to post listing %s", lid)
        return False
    await asyncio.to_thread(db.mark_listing_announced, lid)
    return True


async def _resolve_feed_channel():
    cid = config.P2P_FEED_CHANNEL_ID
    if not cid:
        return None
    ch = bot.get_channel(cid)
    if ch is None:
        try:
            ch = await bot.fetch_channel(cid)
        except Exception:
            log.warning("p2p feed: channel %s not reachable", cid)
            return None
    return ch


# ---------------- VIP role sync ----------------
# Keep one Discord role (config.VIP_ROLE_ID) in lockstep with VIP status in sv_vip_grants:
# any active grant -> member holds the role; otherwise -> role removed. Only ever touches this
# one role; only acts on actual deltas (no API call when a member is already correct).

VIP_SYNC_CHURN_WARN = 50  # log a warning if a single reconcile would change more members than this


async def _compute_vip_target_discord_ids() -> set:
    """discord_ids that SHOULD hold the VIP role = linked members whose steam has an active grant.
    Runs the two DB reads off-thread (fresh conn per query is fine at this cadence)."""
    vip_steam_ids = set(await asyncio.to_thread(vip_db.list_active_vip_steam_ids))
    if not vip_steam_ids:
        return set()
    links = await asyncio.to_thread(db.all_links)
    return {row["discord_id"] for row in links if row["steam_id"] in vip_steam_ids}


async def _reconcile_vip_roles() -> dict:
    """Full reconcile of the VIP role across the configured guild.

    Returns a summary dict: {ok, reason?, added, removed, skipped_unlinked_vips,
    no_members, dryrun}. Never raises for per-member permission issues — logs and continues.
    """
    summary = {"ok": False, "added": 0, "removed": 0, "skipped_unlinked_vips": 0,
               "no_members": False, "dryrun": bool(config.VIP_ROLE_SYNC_DRYRUN)}

    if not config.GUILD_ID:
        summary["reason"] = "no_guild_id"
        return summary
    guild = bot.get_guild(config.GUILD_ID)
    if guild is None:
        summary["reason"] = "guild_not_found"
        return summary
    role = guild.get_role(config.VIP_ROLE_ID)
    if role is None:
        summary["reason"] = "role_not_found"
        return summary

    target = await _compute_vip_target_discord_ids()

    members = guild.members
    # Members intent off (or not yet populated) -> we'd see (almost) nobody and wrongly strip
    # roles. Guard: if the guild claims more members than we can see, bail without touching roles.
    if len(members) <= 1 and (guild.member_count or 0) > 1:
        log.warning(
            "vip-sync: guild.members is empty (%d visible / %d total) — Server Members intent "
            "likely OFF in the Dev Portal; skipping reconcile to avoid stripping roles.",
            len(members), guild.member_count or 0,
        )
        summary["no_members"] = True
        summary["reason"] = "no_members"
        return summary

    # VIPs with no Discord link can't receive the role — count them for the report.
    visible_ids = {m.id for m in members}
    summary["skipped_unlinked_vips"] = len(target - visible_ids)

    to_add, to_remove = [], []
    for m in members:
        has_role = role in m.roles
        should = m.id in target
        if should and not has_role:
            to_add.append(m)
        elif has_role and not should:
            to_remove.append(m)
        # else: already correct -> no-op, no API call

    churn = len(to_add) + len(to_remove)
    if churn > VIP_SYNC_CHURN_WARN:
        log.warning("vip-sync: large change this tick (%d adds, %d removes > %d) — proceeding",
                    len(to_add), len(to_remove), VIP_SYNC_CHURN_WARN)

    if config.VIP_ROLE_SYNC_DRYRUN:
        log.info("vip-sync [DRYRUN]: would +%d add, -%d remove, %d unlinked-vips skipped",
                 len(to_add), len(to_remove), summary["skipped_unlinked_vips"])
        summary["added"], summary["removed"], summary["ok"] = len(to_add), len(to_remove), True
        return summary

    for m in to_add:
        try:
            await m.add_roles(role, reason="VIP role sync: active grant")
            summary["added"] += 1
        except discord.Forbidden:
            log.warning("vip-sync: Forbidden adding role to %s (%s) — check Manage Roles + "
                        "role hierarchy (bot role must be above VIP role)", m, m.id)
        except discord.HTTPException:
            log.exception("vip-sync: HTTP error adding role to %s", m.id)

    for m in to_remove:
        try:
            await m.remove_roles(role, reason="VIP role sync: no active grant")
            summary["removed"] += 1
        except discord.Forbidden:
            log.warning("vip-sync: Forbidden removing role from %s (%s) — check Manage Roles + "
                        "role hierarchy (bot role must be above VIP role)", m, m.id)
        except discord.HTTPException:
            log.exception("vip-sync: HTTP error removing role from %s", m.id)

    summary["ok"] = True
    log.info("vip-sync: +%d added, -%d removed, %d unlinked-vips skipped",
             summary["added"], summary["removed"], summary["skipped_unlinked_vips"])
    return summary


async def sync_vip_role_for_member(discord_id: int) -> bool:
    """Add the VIP role to ONE member immediately (used right after a VIP purchase so the role
    lands without waiting for the loop). Best-effort; returns True if the role is now present.
    Never raises."""
    if config.VIP_ROLE_SYNC_DRYRUN or not config.VIP_ROLE_SYNC_ENABLED or not config.GUILD_ID:
        return False
    guild = bot.get_guild(config.GUILD_ID)
    if guild is None:
        return False
    role = guild.get_role(config.VIP_ROLE_ID)
    if role is None:
        return False
    member = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except discord.HTTPException:
            return False
    if role in member.roles:
        return True
    try:
        await member.add_roles(role, reason="VIP role sync: VIP purchased")
        return True
    except discord.Forbidden:
        log.warning("vip-sync: Forbidden adding role to %s on purchase — check Manage Roles + "
                    "hierarchy", discord_id)
    except discord.HTTPException:
        log.exception("vip-sync: HTTP error adding role to %s on purchase", discord_id)
    return False


# ---------------- lifecycle ----------------

@tasks.loop(seconds=P2P_FEED_POLL_SECONDS)
async def _p2p_feed_tick():
    if not config.P2P_FEED_CHANNEL_ID:
        return
    try:
        rows = await asyncio.to_thread(db.fetch_unannounced_listings, P2P_FEED_BATCH_LIMIT)
        if not rows:
            return
        channel = await _resolve_feed_channel()
        if channel is None:
            return
        for row in rows:
            await _announce_listing(channel, row)
    except Exception:
        log.exception("p2p feed tick failed")


@_p2p_feed_tick.before_loop
async def _p2p_feed_wait_ready():
    await bot.wait_until_ready()




@tasks.loop(seconds=NOTIFY_POLL_SECONDS)
async def _notification_tick():
    try:
        rows = await asyncio.to_thread(db.fetch_pending_notifications, NOTIFY_BATCH_LIMIT)
        for row in rows:
            await _process_notification(row)
    except Exception:
        log.exception("notification tick failed")


@_notification_tick.before_loop
async def _notification_wait_ready():
    await bot.wait_until_ready()


@tasks.loop(minutes=max(config.USERNAME_BACKFILL_INTERVAL_MIN, 1))
async def _username_backfill_tick():
    try:
        await backfill_usernames.run_once(
            token=config.DISCORD_TOKEN,
            limit=backfill_usernames.PER_TICK_LIMIT,
        )
    except Exception:
        log.exception("username backfill tick failed")


@_username_backfill_tick.before_loop
async def _username_backfill_wait_ready():
    await bot.wait_until_ready()


@tasks.loop(minutes=max(config.VIP_ROLE_SYNC_INTERVAL_MIN, 1))
async def _vip_role_sync_tick():
    try:
        await _reconcile_vip_roles()
    except Exception:
        log.exception("vip role sync tick failed")


@_vip_role_sync_tick.before_loop
async def _vip_role_sync_wait_ready():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    await asyncio.to_thread(db.ensure_schema)
    for v in (LinkView(), BalanceView(), MarketListView(), ShopPanelView(),
              BillsShopPanelView(), LeaderboardView(), AdminPanelView()):
        bot.add_view(v)
    # Persistent P2P Buy buttons: the listing id is carried in the custom_id, so a single
    # dynamic-item registration keeps every posted Buy button working across restarts.
    bot.add_dynamic_items(P2PBuyButton)
    # VIP shop (buy VIP with coins; sv_vip_* tables shared with the VIP plugin)
    from vip_cog import VipCog
    await bot.add_cog(VipCog(bot))
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
    if config.USERNAME_BACKFILL_INTERVAL_MIN > 0 and not _username_backfill_tick.is_running():
        _username_backfill_tick.start()
        log.info("username backfill loop started (every %d min)",
                 config.USERNAME_BACKFILL_INTERVAL_MIN)
    if not _notification_tick.is_running():
        _notification_tick.start()
        log.info("notification loop started (every %d s)", NOTIFY_POLL_SECONDS)
    if config.P2P_FEED_CHANNEL_ID and not _p2p_feed_tick.is_running():
        _p2p_feed_tick.start()
        log.info("p2p feed loop started (every %d s -> channel %d)",
                 P2P_FEED_POLL_SECONDS, config.P2P_FEED_CHANNEL_ID)
    if config.VIP_ROLE_SYNC_ENABLED and config.GUILD_ID and not _vip_role_sync_tick.is_running():
        _vip_role_sync_tick.start()
        log.info("vip role-sync loop started (every %d min -> role %d%s)",
                 max(config.VIP_ROLE_SYNC_INTERVAL_MIN, 1), config.VIP_ROLE_ID,
                 " [DRYRUN]" if config.VIP_ROLE_SYNC_DRYRUN else "")
    elif config.VIP_ROLE_SYNC_ENABLED and not config.GUILD_ID:
        log.warning("vip role-sync enabled but GUILD_ID is unset — loop not started")


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (copy from .env.example)")
    bot.run(config.DISCORD_TOKEN)
