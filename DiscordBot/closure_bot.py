"""Closure-mode bot — the game server is permanently closed.

Run this INSTEAD of bot.py. It never imports db.py, so there is NO database
connection at all:

    python closure_bot.py

On startup (idempotent, safe to restart):
  - ensures a #server-closure text channel exists (everyone can read, nobody can type)
  - posts the closure notice with a "Leave server" button (kicks the clicker)
  - locks LOCKED_ROLE_IDS so those roles can see ONLY the closure channel
  - after KICK_AFTER (end of July 30, 2026 UTC+7) an hourly sweep kicks every
    remaining member automatically

Requires: Kick Members + Manage Channels + Manage Roles permissions, and the
privileged Server Members intent (Developer Portal > Bot > Server Members Intent)
so the auto-kick sweep can enumerate members.
"""
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import config  # token/guild id only — db.py is deliberately never imported

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("closure-bot")

LOCKED_ROLE_IDS = (1507325222855114793, 1515091339597975794)
CLOSURE_CHANNEL_NAME = "server-closure"

TZ = timezone(timedelta(hours=7))  # Asia/Bangkok
KICK_AFTER = datetime(2026, 7, 31, 0, 0, tzinfo=TZ)  # "after July 30"

CLOSURE_TEXT = (
    "# Server Closure Notice\n\n"
    "The server has now been permanently closed.\n\n"
    "Thank you to everyone who joined us, played on our server, and supported us "
    "throughout the years. We truly appreciate every moment we shared with this "
    "community.\n\n"
    "Although this chapter has come to an end, we're grateful for all the memories "
    "we've created together.\n\n"
    "Thank you once again for being part of our journey.\n\n"
    "— The Server Team\n\n"
    "> **Notice:** All members will be removed from this Discord automatically "
    "after **July 30, 2026**.\n"
    "> สมาชิกทั้งหมดจะถูกนำออกจาก Discord นี้โดยอัตโนมัติหลังวันที่ **30 กรกฎาคม 2026**"
)

intents = discord.Intents.default()
intents.members = True  # needed to enumerate members for the auto-kick sweep
bot = commands.Bot(command_prefix="!", intents=intents)


class LeaveServerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ออกจากเซิร์ฟเวอร์ / Leave server",
                       style=discord.ButtonStyle.danger, custom_id="closure_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this inside the server.", ephemeral=True)
            return
        # Respond BEFORE kicking — after the kick the interaction can no longer be answered.
        await interaction.response.send_message(
            "ขอบคุณที่อยู่ด้วยกันมาตลอด ลาก่อน / Thank you for everything. Goodbye.",
            ephemeral=True)
        try:
            await member.kick(reason="Server closed — member chose to leave")
        except discord.Forbidden:
            log.warning("cannot kick %s (%s) — missing permission or role hierarchy", member, member.id)
        except discord.HTTPException:
            log.exception("kick failed for %s", member.id)


def _get_guild():
    if config.GUILD_ID:
        return bot.get_guild(config.GUILD_ID)
    return bot.guilds[0] if bot.guilds else None


async def _ensure_closure(guild: discord.Guild):
    everyone = guild.default_role
    ch = discord.utils.get(guild.text_channels, name=CLOSURE_CHANNEL_NAME)
    if ch is None:
        ch = await guild.create_text_channel(CLOSURE_CHANNEL_NAME, overwrites={
            everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        })
        log.info("created #%s", CLOSURE_CHANNEL_NAME)

    posted = False
    async for m in ch.history(limit=10):
        if m.author == guild.me and m.content.startswith("# Server Closure Notice"):
            posted = True
            break
    if not posted:
        await ch.send(CLOSURE_TEXT, view=LeaveServerView())
        log.info("posted closure notice")

    # Locked roles: hide every other channel, show only the closure channel.
    for role_id in LOCKED_ROLE_IDS:
        role = guild.get_role(role_id)
        if role is None:
            log.warning("role %s not found in guild", role_id)
            continue
        if ch.overwrites_for(role).view_channel is not True:
            await ch.set_permissions(role, view_channel=True, send_messages=False,
                                     reason="server closure: only visible channel")
        for other in guild.channels:
            if other.id == ch.id:
                continue
            if other.overwrites_for(role).view_channel is False:
                continue  # already hidden — skip the API call
            try:
                await other.set_permissions(role, view_channel=False,
                                            reason="server closure: hide from locked role")
            except discord.HTTPException:
                log.exception("failed to hide %s from role %s", other, role_id)
        log.info("role %s locked to #%s only", role_id, CLOSURE_CHANNEL_NAME)


@tasks.loop(hours=1)
async def _auto_kick_tick():
    if datetime.now(TZ) < KICK_AFTER:
        return
    guild = _get_guild()
    if guild is None:
        return
    for m in list(guild.members):
        if m.id == guild.me.id:
            continue
        try:
            await m.kick(reason="Server permanently closed")
            log.info("auto-kicked %s (%s)", m, m.id)
        except discord.Forbidden:
            log.warning("cannot auto-kick %s (%s) — permission/hierarchy (owner is never kickable)",
                        m, m.id)
        except discord.HTTPException:
            log.exception("auto-kick failed for %s", m.id)


@_auto_kick_tick.before_loop
async def _auto_kick_wait_ready():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    bot.add_view(LeaveServerView())  # persistent across restarts


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    guild = _get_guild()
    if guild is None:
        log.error("bot is in no guild / GUILD_ID wrong — nothing to do")
        return
    try:
        await _ensure_closure(guild)
    except discord.Forbidden:
        log.error("missing permissions (need Manage Channels + Manage Roles)")
    except Exception:
        log.exception("closure setup failed")
    if not _auto_kick_tick.is_running():
        _auto_kick_tick.start()
        log.info("auto-kick sweep armed for after %s", KICK_AFTER.isoformat())


if __name__ == "__main__":
    assert KICK_AFTER > datetime(2026, 7, 30, 23, 59, tzinfo=TZ)  # deadline is end of July 30
    if not config.DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env")
    bot.run(config.DISCORD_TOKEN)
