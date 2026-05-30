"""
VIP shop cog — buy VIP with coins in Discord.

Drop into the existing SellVault economy bot:
    from vip_cog import VipCog
    await bot.add_cog(VipCog(bot))

Reuses the bot's config.py + db.py (coins + Discord↔Steam links) and vip_db.py (sv_vip_* tables,
shared with the VIP Unturned plugin which applies/expires the RocketMod group).

Commands:
  /vipshop  -> list VIP packages with buy buttons (buy with coins)
  /myvip    -> show your active VIP tiers + expiry
"""
import asyncio
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import vip_db

TZ = getattr(config, "VIP_TZ_OFFSET_HOURS", 7)
COIN = getattr(config, "COIN_NAME", "Coin")


def _fmt_expiry(expires_at_utc: datetime) -> str:
    local = expires_at_utc + timedelta(hours=TZ)
    days = max(0, (expires_at_utc - datetime.utcnow()).days)
    return f"{local.strftime('%Y-%m-%d %H:%M')} (เหลือ ~{days} วัน)"


class BuyButton(discord.ui.Button):
    def __init__(self, pkg: dict):
        label = pkg.get("label") or f"{pkg['tier']} {pkg['days']}d"
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"{label} — {int(pkg['price_coins'])} {COIN}",
            custom_id=f"vipbuy:{pkg['id']}",
        )
        self.pkg = pkg

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user

        steam_id = await asyncio.to_thread(db.get_steam_by_discord, user.id)
        if not steam_id:
            await interaction.followup.send(
                "❌ ยังไม่ได้เชื่อมบัญชี — กดปุ่ม Welcome/Link แล้วพิมพ์ `/link <code>` ในเกมก่อน",
                ephemeral=True,
            )
            return

        try:
            res = await asyncio.to_thread(vip_db.buy_vip, int(steam_id), self.pkg["id"], str(user))
        except Exception as e:  # noqa: BLE001
            print(f"[VIP] buy_vip failed: {e}")
            await interaction.followup.send("❌ เกิดข้อผิดพลาด ลองใหม่อีกครั้ง", ephemeral=True)
            return

        if not res["ok"]:
            if res["reason"] == "coins":
                await interaction.followup.send(
                    f"❌ {COIN} ไม่พอ — ต้องใช้ {res['need']} แต่มี {res['balance']}", ephemeral=True
                )
            else:
                await interaction.followup.send("❌ แพ็กเกจนี้ปิดอยู่/ไม่พบ", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ ซื้อ **{res['tier']}** สำเร็จ! (+{res['days']} วัน)\n"
            f"หมดอายุ: **{_fmt_expiry(res['expires_at'])}**\n"
            f"คงเหลือ: {res['balance']} {COIN}\n"
            f"_สิทธิ์จะถูกใส่ในเกมภายใน ~1 นาที (ถ้าออนไลน์อยู่ ลองรีล็อกถ้ายังไม่ขึ้น)_",
            ephemeral=True,
        )


class VipShopView(discord.ui.View):
    def __init__(self, packages: list[dict]):
        super().__init__(timeout=180)
        for pkg in packages[:25]:  # Discord max 25 components
            self.add_item(BuyButton(pkg))


class VipCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="vipshop", description="ซื้อ VIP ด้วย Coin")
    async def vipshop(self, interaction: discord.Interaction):
        packages = await asyncio.to_thread(vip_db.list_packages, True)
        if not packages:
            await interaction.response.send_message("ยังไม่มีแพ็กเกจ VIP เปิดขายตอนนี้", ephemeral=True)
            return

        embed = discord.Embed(
            title="⭐ VIP Shop",
            description="เลือกแพ็กเกจด้านล่าง — ซื้อด้วย Coin (ซื้อซ้ำ = ต่ออายุ)",
            color=discord.Color.gold(),
        )
        for pkg in packages:
            label = pkg.get("label") or f"{pkg['tier']} {pkg['days']} วัน"
            embed.add_field(
                name=label,
                value=f"{int(pkg['price_coins'])} {COIN} · {pkg['days']} วัน · กลุ่ม `{pkg['group_id']}`",
                inline=False,
            )
        await interaction.response.send_message(
            embed=embed, view=VipShopView(packages), ephemeral=True
        )

    @app_commands.command(name="myvip", description="ดูสถานะ VIP ของคุณ")
    async def myvip(self, interaction: discord.Interaction):
        steam_id = await asyncio.to_thread(db.get_steam_by_discord, interaction.user.id)
        if not steam_id:
            await interaction.response.send_message(
                "❌ ยังไม่ได้เชื่อมบัญชี — `/link <code>` ในเกมก่อน", ephemeral=True
            )
            return
        grants = await asyncio.to_thread(vip_db.get_active_grants, int(steam_id))
        if not grants:
            await interaction.response.send_message("คุณยังไม่มี VIP — `/vipshop` เพื่อซื้อ", ephemeral=True)
            return
        lines = [f"• **{g['group_id']}** — หมดอายุ {_fmt_expiry(g['expires_at'])}" for g in grants]
        embed = discord.Embed(title="⭐ VIP ของคุณ", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, ephemeral=True)
