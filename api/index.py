import os, sys, sqlite3, logging, re, asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "/tmp/reminders.db"
WAITING_TITLE, WAITING_DATETIME, WAITING_REPEAT = 0, 1, 2

# ========== БД ==========
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users 
                        (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, 
                         timezone TEXT DEFAULT 'Europe/Moscow', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        try: conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Europe/Moscow'")
        except: pass
        conn.execute('''CREATE TABLE IF NOT EXISTS reminders 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, 
                         reminder_datetime TEXT, repeat_type TEXT DEFAULT 'none', is_active INTEGER DEFAULT 1, 
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users (user_id))''')
        conn.commit()
init_db()

def register_user(uid, un=None, fn=None):
    with sqlite3.connect(DB_PATH) as c: c.execute("INSERT OR IGNORE INTO users VALUES (?,?,?,?,CURRENT_TIMESTAMP)", (uid, un, fn, 'Europe/Moscow')); c.commit()
def add_reminder(uid, t, dt, rt='none'):
    with sqlite3.connect(DB_PATH) as c: cur=c.cursor(); cur.execute("INSERT INTO reminders (user_id, title, reminder_datetime, repeat_type) VALUES (?,?,?,?)", (uid,t,dt,rt)); c.commit(); return cur.lastrowid
def get_reminders(uid, act=True):
    with sqlite3.connect(DB_PATH) as c: q="SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? AND is_active=1 ORDER BY reminder_datetime" if act else "SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? ORDER BY reminder_datetime"; return c.execute(q, (uid,)).fetchall()
def get_upcoming(uid, lim=10):
    now=datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as c: return c.execute("SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? AND is_active=1 AND reminder_datetime > ? ORDER BY reminder_datetime LIMIT ?", (uid,now,lim)).fetchall()
def get_reminder(rid, uid):
    with sqlite3.connect(DB_PATH) as c: return c.execute("SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE id=? AND user_id=?", (rid,uid)).fetchone()
def del_reminder(rid, uid):
    with sqlite3.connect(DB_PATH) as c: c.execute("UPDATE reminders SET is_active=0 WHERE id=? AND user_id=?", (rid,uid)); c.commit()

# ========== УТИЛИТЫ ==========
def get_user_tz(uid):
    with sqlite3.connect(DB_PATH) as c: r=c.execute("SELECT timezone FROM users WHERE user_id=?", (uid,)).fetchone()
    if r and r[0]:
        try: return pytz.timezone(r[0])
        except: pass
    return pytz.timezone('Europe/Moscow')
def fmt_dt(dt_str, tz):
    try: return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC).astimezone(tz).strftime("%d.%m в %H:%M")
    except: return dt_str
def parse_dt(txt):
    txt=txt.lower().strip(); now=datetime.now()
    m=re.match(r'^([0-1]?[0-9]|2[0-3]):([0-5][0-9])$', txt)
    if m: res=now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0); return res+timedelta(days=1) if res<=now else res
    m=re.match(r'завтра\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])$', txt)
    if m: h,mi=int(m.group(1)),int(m.group(2)); return (now+timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
    wd={'пн':0,'понедельник':0,'вт':1,'вторник':1,'ср':2,'среда':2,'чт':3,'четверг':3,'пт':4,'пятница':4,'сб':5,'суббота':5,'вс':6,'воскресенье':6}
    for n,w in wd.items():
        if txt.startswith(n): tm=re.search(r'([0-1]?[0-9]|2[0-3]):([0-5][0-9])', txt)
        if tm: h,mi=int(tm.group(1)),int(tm.group(2)); d=w-now.weekday(); d=d+7 if d<=0 else d; return (now+timedelta(days=d)).replace(hour=h, minute=mi, second=0, microsecond=0)
    m=re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])', txt)
    if m:
        try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5)))
        except: pass
    m=re.match(r'(\d{1,2})\.(\d{1,2})\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])', txt)
    if m: d,mo,h,mi=int(m.group(1)),int(m.group(2)),int(m.group(3)),int(m.group(4)); res=datetime(now.year, mo, d, h, mi); return res.replace(year=now.year+1) if res<now else res
    return None

# ========== КЛАВИАТУРЫ ==========
def main_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("➕ Создать", callback_data="create_reminder")],[InlineKeyboardButton("📋 Мои", callback_data="list_reminders")],[InlineKeyboardButton("⏰ Ближайшие", callback_data="upcoming_reminders")],[InlineKeyboardButton("⌚ Часовой пояс", callback_data="set_tz_btn")],[InlineKeyboardButton("❓ Помощь", callback_data="help")]])
def repeat_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("🔹 Без повтора", callback_data="repeat_none")],[InlineKeyboardButton("🔄 Ежедневно", callback_data="repeat_daily")],[InlineKeyboardButton("📆 Еженедельно", callback_data="repeat_weekly")],[InlineKeyboardButton("📅 Ежемесячно", callback_data="repeat_monthly")],[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]])
def tz_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("🇷🇺 Москва (UTC+3)", callback_data="tz_Europe/Moscow"), InlineKeyboardButton("🇷 Екатеринбург (UTC+5)", callback_data="tz_Asia/Yekaterinburg")],[InlineKeyboardButton("🇷🇺 Новосибирск (UTC+7)", callback_data="tz_Asia/Novosibirsk"), InlineKeyboardButton("🇷🇺 Владивосток (UTC+10)", callback_data="tz_Asia/Vladivostok")],[InlineKeyboardButton("🇷🇺 Камчатка (UTC+12)", callback_data="tz_Asia/Kamchatka"), InlineKeyboardButton("🌍 Лондон (UTC+0)", callback_data="tz_Europe/London")],[InlineKeyboardButton("🇦🇪 Дубай (UTC+4)", callback_data="tz_Asia/Dubai"), InlineKeyboardButton("🇺🇸 Нью-Йорк (UTC-5)", callback_data="tz_America/New_York")],[InlineKeyboardButton("🇯🇵 Токио (UTC+9)", callback_data="tz_Asia/Tokyo"), InlineKeyboardButton("🌐 UTC", callback_data="tz_UTC")],[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]])

# ========== PTB ПРИЛОЖЕНИЕ ==========
ptb_app = Application.builder().token(BOT_TOKEN or "DUMMY").build()
sched = AsyncIOScheduler()
sched.start()
ptb_app.bot_data['scheduler'] = sched

# ========== ХЕНДЛЕРЫ ==========
async def start_cmd(update, context):
    register_user(update.effective_user.id, update.effective_user.username, update.effective_user.first_name)
    tz = get_user_tz(update.effective_user.id).zone
    await update.message.reply_text(f"✨ *Привет!* Я бот для напоминаний.\n🌍 Ваш пояс: *{tz}*\n\n➕ `/add` | 📋 `/myevents` | ⌚ `/settz`", parse_mode="Markdown")

async def settz_cmd(update, context):
    await update.message.reply_text("🌍 Выберите часовой пояс:", parse_mode="Markdown", reply_markup=tz_kb())

async def create_start(update, context):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await q.edit_message_text("📝 *Новое напоминание*\n\nВведите название:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="cancel")]]))
    else:
        await update.message.reply_text("📝 *Новое напоминание*\n\nВведите название:", parse_mode="Markdown")
    return WAITING_TITLE

async def handle_title(update, context):
    t = update.message.text.strip()
    if not t:
        await update.message.reply_text("❌ Название не может быть пустым.")
        return WAITING_TITLE
    context.user_data['rem_title'] = t
    await update.message.reply_text(f"✅ *{t}*\n\n📅 Введите дату и время:\n• `15:30` — сегодня\n• `завтра 18:00`\n• `31.12 20:00`\n• `пн 10:00`", parse_mode="Markdown")
    return WAITING_DATETIME

async def handle_dt(update, context):
    dt = parse_dt(update.message.text.strip())
    if not dt:
        await update.message.reply_text("❌ Неверный формат. Примеры: `15:30`, `завтра 18:00`", parse_mode="Markdown")
        return WAITING_DATETIME
    context.user_data['rem_dt'] = dt
    await update.message.reply_text(f"✅ {dt.strftime('%d.%m в %H:%M')}\n\n🔄 Выберите повтор:", parse_mode="Markdown", reply_markup=repeat_kb())
    return WAITING_REPEAT

async def handle_repeat(update, context):
    q = update.callback_query
    await q.answer()
    rt = q.data.replace("repeat_","")
    rtx = {"none":"один раз","daily":"ежедневно","weekly":"еженедельно","monthly":"ежемесячно"}.get(rt,"один раз")
    uid = update.effective_user.id
    t = context.user_data.get('rem_title')
    ldt = context.user_data.get('rem_dt')
    tz = get_user_tz(uid)
    utc = tz.localize(ldt, is_dst=True).astimezone(pytz.UTC)
    add_reminder(uid, t, utc.strftime("%Y-%m-%d %H:%M:%S"), rt)
    context.user_data.clear()
    await q.edit_message_text(f"✅ Готово!\n\n📝 {t}\n📅 {ldt.strftime('%d.%m в %H:%M')} ({tz.zone})\n🔄 {rtx}", parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

async def list_rem(update, context):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        mf = q.edit_message_text
    else:
        mf = update.message.reply_text
    uid = update.effective_user.id
    tz = get_user_tz(uid)
    rems = get_reminders(uid)
    if not rems:
        return await mf("📭 Пусто. Создайте напоминание ➕", parse_mode="Markdown", reply_markup=main_kb())
    kb = []
    icons = {"none":"","daily":"🔄","weekly":"📆","monthly":"📅"}
    for r in rems:
        rid,t,dt,rt = r
        kb.append([InlineKeyboardButton(f"{t[:20]} • {fmt_dt(dt, tz)} {icons.get(rt,'')}", callback_data=f"view_{rid}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    await mf("📋 Ваши напоминания:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def upcoming_rem(update, context):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        mf = q.edit_message_text
    else:
        mf = update.message.reply_text
    uid = update.effective_user.id
    tz = get_user_tz(uid)
    rems = get_upcoming(uid)
    if not rems:
        return await mf("⏰ Нет предстоящих", parse_mode="Markdown", reply_markup=main_kb())
    msg = "⏰ Ближайшие:\n\n" + "".join(f"• {r[1]} — {fmt_dt(r[2], tz)}\n" for r in rems)
    await mf(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]))

async def my_events(update, context):
    uid = update.effective_user.id
    tz = get_user_tz(uid)
    rems = get_reminders(uid)
    if not rems:
        return await update.message.reply_text("📭 Пока пусто. Создайте: ➕ или `/add`", parse_mode="Markdown")
    lines = [f"📅 *Ваши мероприятия ({tz.zone}):*"]
    for t,dt,rt in [(r[1],r[2],r[3]) for r in rems]:
        icon = {"none":"","daily":" 🔄","weekly":" 📆","monthly":" 📅"}.get(rt,"")
        lines.append(f"• {t} — {fmt_dt(dt, tz)}{icon}")
    lines.append("\n💡 Удалить: откройте «📋 Мои» и нажмите на напоминание")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def view_rem(update, context):
    q = update.callback_query
    await q.answer()
    rid = int(q.data.split("_")[1])
    uid = update.effective_user.id
    tz = get_user_tz(uid)
    rem = get_reminder(rid, uid)
    if not rem:
        return await q.edit_message_text("❌ Не найдено", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")]]))
    txt = {"none":"один раз","daily":"ежедневно","weekly":"еженедельно","monthly":"ежемесячно"}.get(rem[3],"один раз")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{rid}")],[InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")],[InlineKeyboardButton("🏠 Меню", callback_data="back_to_main")]])
    await q.edit_message_text(f"📌 {rem[1]}\n📅 {fmt_dt(rem[2], tz)}\n🔄 {txt}", parse_mode="Markdown", reply_markup=kb)

async def del_rem(update, context):
    q = update.callback_query
    await q.answer()
    rid = int(q.data.split("_")[1])
    uid = update.effective_user.id
    del_reminder(rid, uid)
    await q.edit_message_text("✅ Удалено", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")]]))

async def help_cmd(update, context):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        mf = q.edit_message_text
    else:
        mf = update.message.reply_text
    await mf("*❓ Помощь*\n\n📅 `15:30` / `завтра 18:00` / `31.12 20:00`\n🌍 Пояс: кнопка ⌚ или `/settz`\n🔄 Повтор: один раз / ежедневно / еженедельно / ежемесячно\n📋 Команды: `/start` • `/add` • `/myevents` • `/settz` • `/help`", parse_mode="Markdown", reply_markup=main_kb() if update.callback_query else None)

async def back_main(update, context):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🏠 Главное меню", parse_mode="Markdown", reply_markup=main_kb())

async def cancel_cmd(update, context):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await q.edit_message_text("❌ Отменено", parse_mode="Markdown", reply_markup=main_kb())
    else:
        await update.message.reply_text("❌ Отменено", parse_mode="Markdown", reply_markup=main_kb())
    context.user_data.clear()
    return ConversationHandler.END

async def button_dispatch(update, context):
    if not update.callback_query:
        return
    q = update.callback_query
    d = q.data
    await q.answer()
    
    if d == "list_reminders":
        await list_rem(update, context)
    elif d == "upcoming_reminders":
        await upcoming_rem(update, context)
    elif d == "set_tz_btn":
        await q.edit_message_text("🌍 *Выберите часовой пояс:*", parse_mode="Markdown", reply_markup=tz_kb())
    elif d.startswith("tz_"):
        try:
            tz_name = d[3:]
            tz = pytz.timezone(tz_name)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz.zone, q.from_user.id))
                conn.commit()
            await q.edit_message_text(f"✅ *Пояс изменён!*\n\n🌍 *{tz.zone}*\n⏰ Все напоминания будут приходить по этому времени.", parse_mode="Markdown", reply_markup=main_kb())
        except Exception as e:
            logger.error(f"TZ error: {e}")
            await q.edit_message_text("❌ Ошибка выбора пояса.", reply_markup=main_kb())
    elif d == "help":
        await help_cmd(update, context)
    elif d == "back_to_main":
        await back_main(update, context)
    elif d == "cancel":
        await cancel_cmd(update, context)
    elif d.startswith("view_"):
        await view_rem(update, context)
    elif d.startswith("delete_"):
        await del_rem(update, context)
    elif d.startswith("repeat_"):
        await handle_repeat(update, context)

# Регистрация хендлеров
conv = ConversationHandler(
    entry_points=[CommandHandler("add", create_start), CallbackQueryHandler(create_start, pattern="^create_reminder$")],
    states={
        WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
        WAITING_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_dt)],
        WAITING_REPEAT: [CallbackQueryHandler(handle_repeat, pattern="^repeat_")]
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd), CallbackQueryHandler(cancel_cmd, pattern="^cancel$")]
)

ptb_app.add_handler(CommandHandler("start", start_cmd))
ptb_app.add_handler(CommandHandler("settz", settz_cmd))
ptb_app.add_handler(CommandHandler("help", help_cmd))
ptb_app.add_handler(CommandHandler("myevents", my_events))
ptb_app.add_handler(conv)
ptb_app.add_handler(CallbackQueryHandler(button_dispatch))

# ========== ASGI ПРИЛОЖЕНИЕ ДЛЯ VERCEL ==========
async def homepage(request):
    return PlainTextResponse("🤖 Telegram Reminder Bot is running")

async def webhook_handler(request):
    if request.method != "POST":
        return PlainTextResponse("Method Not Allowed", status_code=405)
    
    if not BOT_TOKEN:
        return JSONResponse({"error": "BOT_TOKEN not set"}, status_code=500)
    
    try:
        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        
        if update:
            await ptb_app.process_update(update)
        
        return PlainTextResponse("OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

# 🔥 ТОЧКА ВХОДА
app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/api/webhook", webhook_handler, methods=["POST"]),
        Route("/api/webhook", homepage, methods=["GET"]),
    ]
)

application = app
handler = app
