# main.py — Replit-ready: Flask keep-alive + Slash commands + holiday tracking (start/end) + 🌴 nickname toggle
import os
import json
import re
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Tuple

# --- Keep-alive web server (Flask) ---
from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is alive!", 200

def run_web():
    # Use Replit’s assigned port if provided
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)


# --- Discord setup ---
import discord
from discord.ext import commands
from discord import app_commands

import dateparser  # human-friendly date parsing

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_TZ = ZoneInfo("Europe/London")  # change to your server TZ if you like
DATA_FILE = "holidays.json"
COOLDOWN_SECONDS = 60  # anti-spam for mention notifications

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True  # IMPORTANT: for name lookups
bot = commands.Bot(command_prefix="!", intents=intents)  # prefix unused; we use slash cmds

# --- Storage ---
def load_data() -> Dict[str, Dict]:
    if not os.path.exists(DATA_FILE):
        # user_id -> {"start": iso_utc, "end": iso_utc, "note": str, "prev_nick": str?, "name": str?}
        return {"holidays": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

data = load_data()
_last_notified = {}  # (channel_id, user_id) -> monotonic seconds

# --- Helpers ---
def fmt_local(utc_dt: datetime) -> str:
    return utc_dt.astimezone(GUILD_TZ).strftime("%d %b %Y %H:%M")

_time_pattern = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(am|pm)\b", re.I)
_year_pattern = re.compile(r"\b\d{4}\b")

def _parse_natural(raw: str) -> datetime:
    """Parse with UK (DMY) bias. Returns TZ-aware datetime in local tz if input had tz, else in configured tz."""
    settings = {
        "DATE_ORDER": "DMY",
        "TIMEZONE": str(GUILD_TZ),
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DAY_OF_MONTH": "first",
    }
    dt = dateparser.parse(raw.strip(), settings=settings)
    if not dt:
        raise ValueError("Could not parse date/time")
    return dt

def _has_year(raw: str) -> bool:
    return bool(_year_pattern.search(raw))

def _has_time(raw: str) -> bool:
    return bool(_time_pattern.search(raw))

def parse_start_end(start_text: str, end_text: str) -> Tuple[datetime, datetime]:
    """
    Parse start and end with natural formats.
    - If start has no time -> 00:00 local
    - If end has no time   -> 23:59 local
    - If end has no year   -> inherit start's year
    - If end < start and end had no explicit year -> roll end +1 year
    - If end < start and end DID specify a year -> error
    Returns UTC datetimes.
    """
    start_raw = start_text.strip()
    end_raw = end_text.strip()

    start_dt = _parse_natural(start_raw)
    end_dt = _parse_natural(end_raw)

    # Default times
    if not _has_time(start_raw):
        start_dt = start_dt.astimezone(GUILD_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if not _has_time(end_raw):
        end_dt = end_dt.astimezone(GUILD_TZ).replace(hour=23, minute=59, second=0, microsecond=0)

    # Default year for end if missing -> inherit start's year
    if not _has_year(end_raw):
        end_dt = end_dt.replace(year=start_dt.year)

    # If still end < start, handle roll or error
    if end_dt < start_dt:
        if not _has_year(end_raw):
            # roll end to next year
            try:
                end_dt = end_dt.replace(year=end_dt.year + 1)
            except ValueError:
                end_dt = end_dt + timedelta(days=365)
        else:
            raise ValueError("End date/time must be after start date/time.")

    # Return in UTC
    return start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc)

def set_holiday(user_id: int, start_utc: datetime, end_utc: datetime, note: str = "", name_snapshot: Optional[str] = None):
    entry = data["holidays"].get(str(user_id), {})
    entry.update({"start": start_utc.isoformat(), "end": end_utc.isoformat(), "note": note})
    if name_snapshot:
        entry["name"] = name_snapshot
    data["holidays"][str(user_id)] = entry
    save_data()

def remove_holiday(user_id: int) -> bool:
    key = str(user_id)
    if key in data["holidays"]:
        del data["holidays"][key]
        save_data()
        return True
    return False

def get_holiday(user_id: int) -> Optional[Dict]:
    entry = data["holidays"].get(str(user_id))
    if not entry:
        return None
    end = datetime.fromisoformat(entry["end"])
    # auto-expire once end has passed
    if end <= datetime.now(timezone.utc):
        del data["holidays"][str(user_id)]
        save_data()
        return None
    return entry

def is_active(entry: Dict, when_utc: Optional[datetime] = None) -> bool:
    when = when_utc or datetime.now(timezone.utc)
    start = datetime.fromisoformat(entry["start"])
    end = datetime.fromisoformat(entry["end"])
    return start <= when <= end

def can_notify(channel_id: int, user_id: int) -> bool:
    now = bot.loop.time()
    key = (channel_id, user_id)
    last = _last_notified.get(key, 0)
    if now - last >= COOLDOWN_SECONDS:
        _last_notified[key] = now
        return True
    return False

# Nickname helpers
PREFIX = "🌴 "

def already_prefixed(name: str) -> bool:
    return name.startswith(PREFIX)

async def add_prefix_nick(member: discord.Member):
    try:
        current = member.nick if member.nick is not None else member.name
        if already_prefixed(current):
            return
        entry = data["holidays"].get(str(member.id), {})
        if "prev_nick" not in entry:
            entry["prev_nick"] = member.nick if member.nick is not None else ""
            data["holidays"][str(member.id)] = entry
            save_data()
        new_nick = (PREFIX + current)[:32]
        await member.edit(nick=new_nick, reason="Holiday active: add 🌴 prefix")
    except (discord.Forbidden, discord.HTTPException):
        pass  # Ignore if lacking perms/role position

async def remove_prefix_nick(member: discord.Member):
    try:
        entry = data["holidays"].get(str(member.id), {})
        prev = entry.get("prev_nick", None)
        current = member.nick if member.nick is not None else member.name
        if prev is not None:
            target = prev if prev != "" else None
            await member.edit(nick=target, reason="Holiday cleared/ended: restore nickname")
            if str(member.id) in data["holidays"]:
                data["holidays"][str(member.id)].pop("prev_nick", None)
                save_data()
        else:
            if already_prefixed(current):
                base = current[len(PREFIX):]
                target = base if base else None
                await member.edit(nick=target, reason="Holiday cleared/ended: remove 🌴 prefix")
    except (discord.Forbidden, discord.HTTPException):
        pass

# --- Slash commands (use bot.tree directly) ---
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced")
    except Exception as e:
        print("❌ Failed to sync slash commands:", e)
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

@bot.tree.command(name="ping", description="Check if the bot is online")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🏓")

@bot.tree.command(name="holiday_set", description="Set a user's holiday window (Manage Server only)")
@app_commands.describe(
    member="User to put on holiday",
    start="Start date/time (e.g., 5 Oct, 5 Oct 10:00, 07/10/2025)",
    end="End date/time (e.g., 12 Oct, 12 Oct 18:30, 10/10/2025)",
    note="Optional note (reason/details)"
)
async def holiday_set_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    start: str,
    end: str,
    note: str = ""
):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** to do this.", ephemeral=True)
    try:
        start_utc, end_utc = parse_start_end(start, end)
    except ValueError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    # Save holiday and snapshot a readable name
    set_holiday(member.id, start_utc, end_utc, note, name_snapshot=member.display_name)

    # Add 🌴 only if the holiday is currently active
    entry_now = {"start": start_utc.isoformat(), "end": end_utc.isoformat()}
    if is_active(entry_now):
        await add_prefix_nick(member)

    await interaction.response.send_message(
        f"🌴 Set holiday for **{member.display_name}**\n"
        f"• From: **{fmt_local(start_utc)}**\n"
        f"• To:   **{fmt_local(end_utc)}**" + (f"\n• Note: {note}" if note else "")
    )

@bot.tree.command(name="holiday_clear", description="Clear a user’s holiday (Manage Server only)")
@app_commands.describe(member="User to clear")
async def holiday_clear_cmd(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** to do this.", ephemeral=True)

    # Remove prefix if present
    await remove_prefix_nick(member)
    ok = remove_holiday(member.id)
    if ok:
        await interaction.response.send_message(f"✅ Cleared holiday for **{member.display_name}**")
    else:
        await interaction.response.send_message(f"No active holiday record found for **{member.display_name}**.")

@bot.tree.command(name="holiday_list", description="List all current and future holidays")
async def holiday_list_cmd(interaction: discord.Interaction):
    now = datetime.now(timezone.utc)
    guild = interaction.guild
    lines = []

    for uid_str, entry in list(data["holidays"].items()):
        start = datetime.fromisoformat(entry["start"])
        end = datetime.fromisoformat(entry["end"])

        # Auto-remove if ended
        if end <= now:
            del data["holidays"][uid_str]
            continue

        uid = int(uid_str)
        member = guild.get_member(uid)
        if member is None:
            # Try API fetch when not cached
            try:
                member = await guild.fetch_member(uid)
            except (discord.NotFound, discord.HTTPException):
                member = None

        # Fallbacks for display name
        snap_name = entry.get("name")
        name = (member.display_name if member else None) or snap_name or f"<@{uid}>"

        note = entry.get("note") or ""
        active_flag = " (active)" if start <= now <= end else ""
        lines.append(
            f"• **{name}**{active_flag}\n"
            f"  — From **{fmt_local(start)}** to **{fmt_local(end)}**"
            + (f" — {note}" if note else "")
        )

        # If active and no prefix yet, try to add it
        if start <= now <= end and member:
            # add prefix if missing
            current = member.nick if member.nick is not None else member.name
            if not already_prefixed(current):
                await add_prefix_nick(member)

    save_data()
    await interaction.response.send_message("\n".join(lines) if lines else "Nobody is currently on holiday.")

# --- Mention auto-reply (only during active window) ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    now = datetime.now(timezone.utc)

    for user in message.mentions:
        entry = get_holiday(user.id)
        if not entry:
            continue
        start = datetime.fromisoformat(entry["start"])
        end = datetime.fromisoformat(entry["end"])
        if not (start <= now <= end):
            continue  # not currently active

        if can_notify(message.channel.id, user.id):
            note = entry.get("note") or ""
            await message.channel.send(
                f"🌴 **{user.display_name}** is on holiday "
                f"from **{fmt_local(start)}** to **{fmt_local(end)}**"
                + (f" — {note}" if note else "")
            )

    await bot.process_commands(message)

# --- Run both Flask + Bot ---
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ No DISCORD_BOT_TOKEN set (add it in Replit Secrets).")
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)
