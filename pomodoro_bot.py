"""
Pomodoro Tracker Bot v2
========================
Commands:
  /start   — welcome message
  /go      — start a 1-hour focus cycle
  /cancel  — cancel your current cycle
  /status  — check cycle progress
  /stats   — your achievements & stats
  /stats name — view another user's stats
  /today   — today's leaderboard
  /weekly  — this week's totals
  /alltime — all-time leaderboard
  /help    — show all commands

Admin only:
  /admin        — admin panel overview
  /add name N   — add N tomatoes to a user
  /remove name N— remove N tomatoes from a user
  /ban name     — ban a user
  /unban name   — unban a user
  /reset_today  — wipe today's data
  /broadcast msg— send a message to all users
"""

import json
import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional

from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── CONFIG ────────────────────────────────────────────────────────────────────

BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@your_channel_username")

CYCLE_SECONDS = 3600   # 1 hour
DATA_FILE     = "pomodoro_data.json"

# ── ACHIEVEMENTS ──────────────────────────────────────────────────────────────

ACHIEVEMENTS = {
    "first_tomato":  {"icon": "🌱", "name": "First Steps",   "desc": "Complete your first pomodoro"},
    "five_in_day":   {"icon": "🔥", "name": "On Fire",        "desc": "5 pomodoros in one day"},
    "ten_total":     {"icon": "💪", "name": "Grinder",        "desc": "10 pomodoros total"},
    "fifty_total":   {"icon": "🏆", "name": "Champion",       "desc": "50 pomodoros total"},
    "hundred_total": {"icon": "⚡", "name": "Legend",         "desc": "100 pomodoros total"},
    "week_streak":   {"icon": "📅", "name": "Consistent",     "desc": "7-day streak"},
    "month_streak":  {"icon": "🎯", "name": "Unstoppable",    "desc": "30-day streak"},
    "early_bird":    {"icon": "🌅", "name": "Early Bird",     "desc": "Complete a cycle before 8am"},
    "night_owl":     {"icon": "🌙", "name": "Night Owl",      "desc": "Complete a cycle after 11pm"},
}

# ── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
log = logging.getLogger(__name__)

# ── DATA HELPERS ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "active_cycles": {}, "daily": {}}

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user(data: dict, uid: str) -> dict:
    if uid not in data["users"]:
        data["users"][uid] = {
            "name": f"User {uid}",
            "total_tomatoes": 0,
            "streak": 0,
            "last_active": None,
            "achievements": [],
            "banned": False,
        }
    return data["users"][uid]

def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def week_keys() -> list:
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

def find_user(data: dict, name: str) -> Optional[str]:
    """Find a user ID by display name (case-insensitive, partial match)."""
    needle = name.lstrip("@").lower()
    for uid, u in data["users"].items():
        if u.get("name", "").lower() == needle:
            return uid
    for uid, u in data["users"].items():
        if u.get("name", "").lower().startswith(needle):
            return uid
    return None

def award_tomato(data: dict, uid: str) -> tuple:
    """Award 1 tomato. Returns (today_total, [new_achievement_keys])."""
    user  = get_user(data, uid)
    key   = today_key()
    now   = datetime.now()

    # Daily count
    data["daily"].setdefault(key, {})
    data["daily"][key][uid] = data["daily"][key].get(uid, 0) + 1

    # All-time total
    user["total_tomatoes"] = user.get("total_tomatoes", 0) + 1

    # Streak
    yesterday = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    last = user.get("last_active")
    if last == yesterday:
        user["streak"] = user.get("streak", 0) + 1
    elif last != key:
        user["streak"] = 1
    user["last_active"] = key

    new_ach = _check_achievements(data, uid, key, now.hour)
    return data["daily"][key][uid], new_ach

def _check_achievements(data: dict, uid: str, key: str, hour: int) -> list:
    user  = data["users"][uid]
    earned = set(user.get("achievements", []))
    total  = user.get("total_tomatoes", 0)
    streak = user.get("streak", 0)
    today  = data["daily"].get(key, {}).get(uid, 0)

    candidates = {
        "first_tomato":  total  >= 1,
        "ten_total":     total  >= 10,
        "fifty_total":   total  >= 50,
        "hundred_total": total  >= 100,
        "five_in_day":   today  >= 5,
        "week_streak":   streak >= 7,
        "month_streak":  streak >= 30,
        "early_bird":    hour   < 8,
        "night_owl":     hour   >= 23,
    }

    new = [k for k, v in candidates.items() if v and k not in earned]
    user["achievements"] = list(earned | set(new))
    return new

def build_leaderboard(totals: dict, users: dict, title: str, top_n: int = 0) -> str:
    if not totals:
        return f"{title}\n\nNo pomodoros recorded yet."

    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    if top_n:
        ranked = ranked[:top_n]

    medals = ["🥇", "🥈", "🥉"]
    lines  = [title, ""]
    for i, (uid, count) in enumerate(ranked):
        name  = users.get(uid, {}).get("name", f"User {uid}")
        badge = medals[i] if i < 3 else f"{i+1}."
        bar   = "🍅" * min(count, 10) + (f" +{count-10}" if count > 10 else "")
        lines.append(f"{badge} {name} — {count} 🍅\n    {bar}")

    grand = sum(t for _, t in ranked)
    lines += ["", "──────────────────",
              f"Total: {grand} 🍅 = {grand * 25} min of focus 🔥"]
    return "\n".join(lines)

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(
            update.effective_chat.id, update.effective_user.id
        )
        return m.status in ("administrator", "creator")
    except Exception:
        return False

# ── USER COMMANDS ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    await update.message.reply_text(
        "🍅 *Welcome to Pomodoro Tracker!*\n\n"
        "Stay focused and earn tomatoes 🍅 for every hour you complete.\n\n"
        "*How to use:*\n"
        "1. Send /go to start a 1-hour focus cycle\n"
        "2. Stay focused — don't cancel!\n"
        "3. When the hour ends you earn 🍅\n"
        "4. Check /today to see the leaderboard\n\n"
        "Send /help for all commands.",
        parse_mode="Markdown"
    )

async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start a 1-hour pomodoro cycle."""
    uid  = str(update.effective_user.id)
    data = load_data()
    user = get_user(data, uid)

    if user.get("banned"):
        await update.message.reply_text("❌ You are banned from using this bot.")
        return

    user["name"] = (
        update.effective_user.full_name
        or update.effective_user.username
        or f"User {uid}"
    )

    # Already in a cycle?
    if uid in data.get("active_cycles", {}):
        cycle    = data["active_cycles"][uid]
        start    = datetime.fromisoformat(cycle["start_time"])
        elapsed  = int((datetime.now() - start).total_seconds())
        remaining = max(0, CYCLE_SECONDS - elapsed)
        m, s     = remaining // 60, remaining % 60
        await update.message.reply_text(
            f"⏳ You already have an active cycle!\n"
            f"Time remaining: *{m}m {s}s*\n\n"
            f"Use /status to check progress or /cancel to stop.",
            parse_mode="Markdown"
        )
        save_data(data)
        return

    # Start cycle
    now = datetime.now()
    data.setdefault("active_cycles", {})[uid] = {
        "start_time": now.isoformat(),
        "chat_id":    update.effective_chat.id,
    }
    save_data(data)

    context.job_queue.run_once(
        _cycle_done,
        when=CYCLE_SECONDS,
        data={"uid": uid, "chat_id": update.effective_chat.id},
        name=f"cycle_{uid}",
    )

    end_str = (now + timedelta(seconds=CYCLE_SECONDS)).strftime("%H:%M")
    await update.message.reply_text(
        f"🍅 *Cycle started!*\n\n"
        f"⏱ Duration: 1 hour\n"
        f"🏁 Ends at: *{end_str}*\n\n"
        f"Stay focused 💪 — you'll be notified when it's done!\n"
        f"Use /cancel to stop anytime.",
        parse_mode="Markdown"
    )

async def _cycle_done(context: ContextTypes.DEFAULT_TYPE):
    """Background job — fires when cycle finishes."""
    job_data = context.job.data
    uid      = job_data["uid"]
    chat_id  = job_data["chat_id"]

    data = load_data()
    if uid not in data.get("active_cycles", {}):
        return   # was cancelled

    del data["active_cycles"][uid]
    today_total, new_ach = award_tomato(data, uid)
    save_data(data)

    user      = data["users"][uid]
    name      = user.get("name", f"User {uid}")
    all_time  = user.get("total_tomatoes", 0)

    msg = (
        f"🎉 *Cycle complete, {name}!*\n\n"
        f"🍅 +1 pomodoro earned!\n"
        f"📅 Today: {today_total} 🍅\n"
        f"🏆 All time: {all_time} 🍅\n"
        f"🔥 Streak: {user.get('streak', 0)} days\n"
    )

    if new_ach:
        msg += "\n🎖 *New achievements unlocked!*\n"
        for k in new_ach:
            a = ACHIEVEMENTS[k]
            msg += f"  {a['icon']} {a['name']} — {a['desc']}\n"

    msg += "\nSend /go to start another cycle!"
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    data = load_data()

    if uid not in data.get("active_cycles", {}):
        await update.message.reply_text("❌ You don't have an active cycle.")
        return

    del data["active_cycles"][uid]
    save_data(data)

    for job in context.job_queue.get_jobs_by_name(f"cycle_{uid}"):
        job.schedule_removal()

    await update.message.reply_text(
        "🛑 Cycle cancelled. No tomato earned.\n\nSend /go when you're ready to focus again."
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    data = load_data()

    if uid not in data.get("active_cycles", {}):
        await update.message.reply_text("💤 No active cycle.\n\nSend /go to start one!")
        return

    cycle     = data["active_cycles"][uid]
    start     = datetime.fromisoformat(cycle["start_time"])
    elapsed   = int((datetime.now() - start).total_seconds())
    remaining = max(0, CYCLE_SECONDS - elapsed)
    m, s      = remaining // 60, remaining % 60
    progress  = min(int((elapsed / CYCLE_SECONDS) * 12), 12)
    bar       = "█" * progress + "░" * (12 - progress)
    pct       = min(int((elapsed / CYCLE_SECONDS) * 100), 100)

    await update.message.reply_text(
        f"⏱ *Cycle in progress*\n\n"
        f"`[{bar}]` {pct}%\n\n"
        f"Time remaining: *{m}m {s}s*\n"
        f"Keep going! 💪",
        parse_mode="Markdown"
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()

    # Target: self or named user
    if context.args:
        name   = " ".join(context.args)
        target = find_user(data, name)
        if not target:
            await update.message.reply_text(f"❌ User '{name}' not found.")
            return
        uid = target
    else:
        uid = str(update.effective_user.id)

    user  = get_user(data, uid)
    key   = today_key()
    today = data.get("daily", {}).get(key, {}).get(uid, 0)
    week  = sum(data.get("daily", {}).get(k, {}).get(uid, 0) for k in week_keys())

    earned    = user.get("achievements", [])
    ach_lines = []
    for k, a in ACHIEVEMENTS.items():
        mark = "✅" if k in earned else "🔒"
        ach_lines.append(f"  {mark} {a['icon']} {a['name']} — {a['desc']}")

    active_msg = ""
    if uid in data.get("active_cycles", {}):
        cycle     = data["active_cycles"][uid]
        start     = datetime.fromisoformat(cycle["start_time"])
        elapsed   = int((datetime.now() - start).total_seconds())
        remaining = max(0, CYCLE_SECONDS - elapsed)
        active_msg = f"\n⏱ Active cycle: {remaining // 60}m {remaining % 60}s left\n"

    await update.message.reply_text(
        f"📊 *Stats — {user.get('name', f'User {uid}')}*\n"
        f"{active_msg}\n"
        f"🍅 Today: {today}\n"
        f"📅 This week: {week}\n"
        f"🏆 All time: {user.get('total_tomatoes', 0)}\n"
        f"🔥 Streak: {user.get('streak', 0)} days\n\n"
        f"🎖 *Achievements ({len(earned)}/{len(ACHIEVEMENTS)})*\n"
        + "\n".join(ach_lines),
        parse_mode="Markdown"
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    key  = today_key()
    text = build_leaderboard(
        data.get("daily", {}).get(key, {}),
        data["users"],
        f"🍅 Today's Leaderboard — {key}"
    )
    await update.message.reply_text(text)

async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data   = load_data()
    totals = defaultdict(int)
    for k in week_keys():
        for uid, n in data.get("daily", {}).get(k, {}).items():
            totals[uid] += n
    text = build_leaderboard(dict(totals), data["users"], f"📊 Weekly Leaderboard — w/o {week_keys()[0]}")
    await update.message.reply_text(text)

async def cmd_alltime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data   = load_data()
    totals = {uid: u.get("total_tomatoes", 0)
              for uid, u in data["users"].items()
              if u.get("total_tomatoes", 0) > 0}
    text = build_leaderboard(totals, data["users"], "🏆 All-Time Leaderboard")
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await is_admin(update, context)
    text = (
        "🍅 *Pomodoro Tracker — Commands*\n\n"
        "*Focus:*\n"
        "/go — start a 1-hour cycle\n"
        "/cancel — cancel your current cycle\n"
        "/status — check cycle progress\n\n"
        "*Stats:*\n"
        "/stats — your achievements & stats\n"
        "/stats name — view another user's stats\n"
        "/today — today's leaderboard\n"
        "/weekly — this week's totals\n"
        "/alltime — all-time leaderboard\n"
    )
    if admin:
        text += (
            "\n*Admin:*\n"
            "/admin — overview panel\n"
            "/add name N — add tomatoes\n"
            "/remove name N — remove tomatoes\n"
            "/ban name — ban user\n"
            "/unban name — unban user\n"
            "/reset\\_today — wipe today's data\n"
            "/broadcast msg — message all users\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")

# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admin only.")
        return

    data         = load_data()
    key          = today_key()
    total_users  = len(data["users"])
    active       = len(data.get("active_cycles", {}))
    today_total  = sum(data.get("daily", {}).get(key, {}).values())
    banned       = sum(1 for u in data["users"].values() if u.get("banned"))
    all_time     = sum(u.get("total_tomatoes", 0) for u in data["users"].values())

    await update.message.reply_text(
        f"⚙️ *Admin Panel*\n\n"
        f"👥 Total users: {total_users}\n"
        f"⏱ Active cycles: {active}\n"
        f"🍅 Today's pomodoros: {today_total}\n"
        f"🏆 All-time total: {all_time}\n"
        f"🚫 Banned users: {banned}\n\n"
        f"*Available commands:*\n"
        f"/add name N · /remove name N\n"
        f"/ban name · /unban name\n"
        f"/reset\\_today · /broadcast msg",
        parse_mode="Markdown"
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add name N")
        return

    data   = load_data()
    target = find_user(data, context.args[0])
    if not target:
        await update.message.reply_text("❌ User not found.")
        return

    try:
        n = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ N must be a number.")
        return

    key = today_key()
    data["daily"].setdefault(key, {})
    data["daily"][key][target] = data["daily"][key].get(target, 0) + n
    data["users"][target]["total_tomatoes"] = data["users"][target].get("total_tomatoes", 0) + n
    save_data(data)

    name = data["users"][target].get("name", target)
    await update.message.reply_text(f"✅ Added {n} 🍅 to {name}.")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /remove name N")
        return

    data   = load_data()
    target = find_user(data, context.args[0])
    if not target:
        await update.message.reply_text("❌ User not found.")
        return

    try:
        n = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ N must be a number.")
        return

    key = today_key()
    if key in data["daily"] and target in data["daily"][key]:
        data["daily"][key][target] = max(0, data["daily"][key][target] - n)
    data["users"][target]["total_tomatoes"] = max(
        0, data["users"][target].get("total_tomatoes", 0) - n
    )
    save_data(data)

    name = data["users"][target].get("name", target)
    await update.message.reply_text(f"✅ Removed {n} 🍅 from {name}.")

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban name")
        return

    data   = load_data()
    target = find_user(data, context.args[0])
    if not target:
        await update.message.reply_text("❌ User not found.")
        return

    data["users"][target]["banned"] = True
    save_data(data)
    name = data["users"][target].get("name", target)
    await update.message.reply_text(f"🚫 {name} has been banned.")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban name")
        return

    data   = load_data()
    target = find_user(data, context.args[0])
    if not target:
        await update.message.reply_text("❌ User not found.")
        return

    data["users"][target]["banned"] = False
    save_data(data)
    name = data["users"][target].get("name", target)
    await update.message.reply_text(f"✅ {name} has been unbanned.")

async def cmd_reset_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admin only.")
        return

    data = load_data()
    key  = today_key()
    data["daily"].pop(key, None)
    save_data(data)
    await update.message.reply_text(f"✅ Today's data ({key}) has been reset.")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast your message here")
        return

    text = " ".join(context.args)
    data = load_data()
    sent = failed = 0

    for uid, user in data["users"].items():
        if user.get("banned"):
            continue
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"📢 *Announcement*\n\n{text}",
                parse_mode="Markdown"
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast complete\n✅ Delivered: {sent}\n❌ Failed: {failed}"
    )

# ── SCHEDULED: midnight top-3 ─────────────────────────────────────────────────

async def post_daily_top3(bot: Bot):
    data     = load_data()
    key      = today_key()
    day_data = data.get("daily", {}).get(key, {})
    if not day_data:
        return

    ranked = sorted(day_data.items(), key=lambda x: x[1], reverse=True)[:3]
    medals = ["🥇", "🥈", "🥉"]
    lines  = [f"🌙 *Top 3 — {key}*\n"]

    for i, (uid, count) in enumerate(ranked):
        name = data["users"].get(uid, {}).get("name", f"User {uid}")
        lines.append(f"{medals[i]} {name} — {count} 🍅 ({count * 25} min)")

    grand = sum(day_data.values())
    lines.append(f"\nGroup total: {grand} 🍅")

    msg = "\n".join(lines)
    if CHANNEL_ID != "@your_channel_username":
        await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode="Markdown")
    log.info(f"Daily top-3 posted for {key}")

# ── RESTORE CYCLES ON RESTART ─────────────────────────────────────────────────

async def restore_cycles(app):
    """Re-queue any cycles that were active when the bot last stopped."""
    data = load_data()
    restored = completed = 0

    for uid, cycle in list(data.get("active_cycles", {}).items()):
        start     = datetime.fromisoformat(cycle["start_time"])
        elapsed   = (datetime.now() - start).total_seconds()
        remaining = CYCLE_SECONDS - elapsed

        if remaining <= 0:
            # Cycle ended while offline — award the tomato now
            del data["active_cycles"][uid]
            today_total, new_ach = award_tomato(data, uid)
            try:
                await app.bot.send_message(
                    chat_id=cycle["chat_id"],
                    text=(
                        f"🎉 Your cycle ended while the bot was offline!\n\n"
                        f"🍅 +1 pomodoro awarded!\n"
                        f"📅 Today: {today_total} 🍅"
                    )
                )
            except Exception:
                pass
            completed += 1
        else:
            app.job_queue.run_once(
                _cycle_done,
                when=int(remaining),
                data={"uid": uid, "chat_id": cycle["chat_id"]},
                name=f"cycle_{uid}",
            )
            restored += 1

    save_data(data)
    if restored or completed:
        log.info(f"Restored {restored} active cycles, completed {completed} offline cycles")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.error("❌ BOT_TOKEN not set!")
        return

    if CHANNEL_ID == "@your_channel_username":
        log.warning("⚠️  CHANNEL_ID not set — daily top-3 won't post to a channel.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(restore_cycles)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("go",      cmd_go))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("alltime", cmd_alltime))
    app.add_handler(CommandHandler("help",    cmd_help))

    # Admin commands
    app.add_handler(CommandHandler("admin",       cmd_admin))
    app.add_handler(CommandHandler("add",         cmd_add))
    app.add_handler(CommandHandler("remove",      cmd_remove))
    app.add_handler(CommandHandler("ban",         cmd_ban))
    app.add_handler(CommandHandler("unban",       cmd_unban))
    app.add_handler(CommandHandler("reset_today", cmd_reset_today))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))

    # Scheduler — midnight top-3
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        post_daily_top3,
        trigger="cron",
        hour=0, minute=0,
        args=[app.bot],
    )
    scheduler.start()

    log.info("🍅 Pomodoro bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
