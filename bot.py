# ==========================================
# TELEGRAM REMINDER BOT - GROUP MENTIONS + TZ FIX
# ==========================================

import os, sys, sqlite3, logging, logging.handlers, re, asyncio, signal
from datetime import datetime, timedelta
import pytz
from timezonefinder import TimezoneFinder

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# ========== НАСТРОЙКА ==========
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout), logging.handlers.RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=3)])
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("❌ BOT_TOKEN not set in environment variables"); sys.exit(1)

DB_PATH = "reminders.db"
tf = TimezoneFinder()

# ========== БАЗА ДАННЫХ ==========
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users
                        (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                         timezone TEXT DEFAULT 'Europe/Moscow', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        try: conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Europe/Moscow'")
        except: pass
        
        conn.execute('''CREATE TABLE IF NOT EXISTS reminders
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, title TEXT,
                         reminder_datetime TEXT, repeat_type TEXT DEFAULT 'none', is_active INTEGER DEFAULT 1,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users (user_id))''')
        try: conn.execute("ALTER TABLE reminders ADD COLUMN chat_id INTEGER")
        except: pass
        
        conn.commit()
init_db()

def register_user(uid, un=None, fn=None):
    with sqlite3.connect(DB_PATH) as conn: conn.execute("INSERT OR IGNORE INTO users VALUES (?,?,?,?,CURRENT_TIMESTAMP)", (uid, un, fn, 'Europe/Moscow')); conn.commit()

def add_reminder(uid, chat_id, t, dt, rt='none'):
    with sqlite3.connect(DB_PATH) as conn: 
        c=conn.cursor()
        c.execute("INSERT INTO reminders (user_id, chat_id, title, reminder_datetime, repeat_type) VALUES (?,?,?,?,?)", (uid, chat_id, t, dt, rt))
        conn.commit()
        return c.lastrowid

def get_reminders(uid, act=True):
    with sqlite3.connect(DB_PATH) as conn: q="SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? AND is_active=1 ORDER BY reminder_datetime" if act else "SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? ORDER BY reminder_datetime"; return conn.execute(q, (uid,)).fetchall()

def get_reminder(rid, uid):
    with sqlite3.connect(DB_PATH) as conn: return conn.execute("SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE id=? AND user_id=?", (rid,uid)).fetchone()

def del_reminder(rid, uid):
    with sqlite3.connect(DB_PATH) as conn: conn.execute("UPDATE reminders SET is_active=0 WHERE id=? AND user_id=?", (rid,uid)); conn.commit()

def set_user_tz(uid, tz_name):
    with sqlite3.connect(DB_PATH) as conn: conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz_name, uid)); conn.commit()

# ========== УТИЛИТЫ ==========
def get_user_tz(uid):
    with sqlite3.connect(DB_PATH) as conn: r=conn.execute("SELECT timezone FROM users WHERE user_id=?", (uid,)).fetchone()
    if r and r[0]:
        try: return pytz.timezone(r[0])
        except: pass
    return pytz.timezone('Europe/Moscow')

def fmt_dt(dt_str, tz):
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC).astimezone(tz)
        now = datetime.now(tz)
        diff = (dt.date() - now.date()).days
        day_str = "Сегодня" if diff == 0 else "Завтра" if diff == 1 else dt.strftime("%d.%m")
        return f"{day_str}, {dt.strftime('%H:%M')}"
    except: return dt_str

def parse_natural_reminder(text):
    text = text.strip()
    patterns = [
        r'^(.+?)\s+завтра\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])$',
        r'^(.+?)\s+сегодня\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])$',
        r'^(.+?)\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])$',
        r'^(.+?)\s+(\d{1,2})\.(\d{1,2})\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])$',
        r'^(.+?)\s+(\d{1,2})\.(\d{1,2})\.(\d{4})\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])$'
    ]
    now = datetime.now()
    for p in patterns:
        m = re.match(p, text, re.IGNORECASE)
        if m:
            groups = m.groups()
            title = groups[0].strip().rstrip('.,!?:;')
            try:
                if 'завтра' in p or 'сегодня' in p:
                    days = 1 if 'завтра' in p else 0
                    h, mi = int(groups[1]), int(groups[2])
                    dt = (now + timedelta(days=days)).replace(hour=h, minute=mi, second=0, microsecond=0)
                elif len(groups) == 4:
                    d, mo, h, mi = int(groups[1]), int(groups[2]), int(groups[3]), int(groups[4])
                    dt = datetime(now.year, mo, d, h, mi)
                    if dt < now: dt = dt.replace(year=now.year+1)
                elif len(groups) == 6:
                    d, mo, y, h, mi = int(groups[1]), int(groups[2]), int(groups[3]), int(groups[4]), int(groups[5])
                    dt = datetime(y, mo, d, h, mi)
                else:
                    h, mi = int(groups[1]), int(groups[2])
                    dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
                    if dt <= now: dt += timedelta(days=1)
                return title, dt
            except: pass
    return None, None

# ========== КЛАВИАТУРЫ ==========
def main_kb(): return InlineKeyboardMarkup([
    [InlineKeyboardButton("➕ Создать", callback_data="create_prompt")],
    [InlineKeyboardButton("📋 Мои напоминания", callback_data="list_reminders")],
    [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
])

def settings_kb(): return InlineKeyboardMarkup([
    [InlineKeyboardButton("⌚ Часовой пояс", callback_data="tz_menu")],
    [InlineKeyboardButton("📝 Шаблоны", callback_data="templates")],
    [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    [InlineKeyboardButton("🔙 Главное меню", callback_data="back_main")]
])

def tz_kb(): return InlineKeyboardMarkup([
    [InlineKeyboardButton("🇷🇺 Москва (UTC+3)", callback_data="tz_Europe/Moscow"), InlineKeyboardButton("🇷 Екатеринбург (UTC+5)", callback_data="tz_Asia/Yekaterinburg")],
    [InlineKeyboardButton("🇷 Новосибирск (UTC+7)", callback_data="tz_Asia/Novosibirsk"), InlineKeyboardButton("🇷 Владивосток (UTC+10)", callback_data="tz_Asia/Vladivostok")],
    [InlineKeyboardButton("🌍 Лондон (UTC+0)", callback_data="tz_Europe/London"), InlineKeyboardButton("🌐 UTC", callback_data="tz_UTC")],
    [InlineKeyboardButton("📍 Авто (GPS)", callback_data="gps_request")],
    [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
])

def template_kb(): return InlineKeyboardMarkup([
    [InlineKeyboardButton("💊 Лекарства", callback_data="tpl_Лекарства"), InlineKeyboardButton("🏋️ Тренировка", callback_data="tpl_Тренировка")],
    [InlineKeyboardButton("📞 Звонок", callback_data="tpl_Звонок"), InlineKeyboardButton("📝 Отчёт", callback_data="tpl_Отчёт")],
    [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
])

# ========== ХЕНДЛЕРЫ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_user.username, update.effective_user.first_name)
    tz = get_user_tz(update.effective_user.id).zone
    chat_type = update.effective_chat.type
    
    if chat_type in ['group', 'supergroup']:
        txt = (f"👋 *Привет, группа!* Я бот-напоминалка.\n\n"
               f"🌍 Мой пояс: *{tz}*\n\n"
               f"💡 *Как использовать в группе:*\n"
               f"• `/add Встреча завтра 15:00`\n"
               f"• `/myevents` — мои напоминания\n"
               f"• `/settings` — настройки\n\n"
               f"📢 Напоминания придут в этот чат с упоминанием вас.")
    else:
        txt = (f"👋 *Привет!* Я бот-напоминалка.\n\n"
               f"🌍 Пояс: *{tz}*\n\n"
               f"💡 *Быстрый старт:*\n"
               f"• Напишите: `Встреча завтра 15:00`\n"
               f"• Или нажмите ➕ *Создать*\n\n"
               f"📋 `/myevents` — список\n⚙️ `/settings` — настройки")
    
    await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb())

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "⚙️ *Настройки*"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown", reply_markup=settings_kb())
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=settings_kb())

async def tz_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "🌍 *Часовой пояс*"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown", reply_markup=tz_kb())
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=tz_kb())

async def templates_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "📝 *Быстрые шаблоны*\n\nВыберите, чтобы создать мгновенно:"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown", reply_markup=template_kb())
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=template_kb())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "*❓ Помощь*\n\n📅 *Форматы:*\n`Название завтра 15:00`\n`Задача сегодня 18:30`\n`Отчёт 31.12 20:00`\n\n⚙️ *Настройки:* смена пояса, шаблоны\n📋 *Список:* просмотр и управление"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown", reply_markup=settings_kb())
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=settings_kb())

async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "🏠 *Главное меню*"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown", reply_markup=main_kb())
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb())

async def create_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        text = " ".join(context.args)
        await process_reminder_text(update, context, text)
        return
    
    txt = "📝 *Новое напоминание*\n\nНапишите в одном сообщении:\n`Название + время`\n\nПримеры:\n• `Встреча завтра 15:00`\n• `Купить молоко сегодня 18:30`"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown")
    else:
        await update.message.reply_text(txt, parse_mode="Markdown")
    context.user_data['awaiting_reminder'] = True

async def process_reminder_text(update, context, text):
    title, dt = parse_natural_reminder(text)
    if not title or not dt:
        return await update.message.reply_text("❌ Не удалось распознать время.\n\nФормат: `Название завтра 15:00`", parse_mode="Markdown")
    
    uid = update.effective_user.id
    chat_id = update.effective_chat.id 
    tz = get_user_tz(uid)
    utc_dt = tz.localize(dt, is_dst=True).astimezone(pytz.UTC)
    
    rid = add_reminder(uid, chat_id, title, utc_dt.strftime("%Y-%m-%d %H:%M:%S"), "none")
    
    sched = context.bot_data.get('scheduler')
    if sched: 
        sched.add_job(send_reminder, trigger=DateTrigger(run_date=utc_dt), 
                      args=[uid, title, chat_id, rid, context.application], 
                      id=f"rem_{rid}", replace_existing=True)
    
    await update.message.reply_text(f"✅ *Создано!*\n📌 {title}\n📅 {fmt_dt(utc_dt.strftime('%Y-%m-%d %H:%M:%S'), tz)}\n📢 Напомню здесь.", parse_mode="Markdown")

async def handle_reminder_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_reminder'): return
    context.user_data.pop('awaiting_reminder', None)
    await process_reminder_text(update, context, update.message.text)

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; tz = get_user_tz(uid)
    rems = get_reminders(uid)
    if not rems:
        txt = "📭 Пока пусто. Создайте первое напоминание ➕"
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(txt, parse_mode="Markdown", reply_markup=main_kb())
        else:
            await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb())
        return
    
    grouped = {}
    for r in rems:
        day_key = fmt_dt(r[2], tz).split(',')[0]
        grouped.setdefault(day_key, []).append(r)
    
    msg = "📅 *Ваши напоминания:*\n\n"
    for day, items in sorted(grouped.items(), key=lambda x: datetime.strptime(x[0], "%d.%m") if "Сегодня" not in x[0] and "Завтра" not in x[0] else datetime.now()):
        msg += f" *{day}*\n"
        for rid, title, dt, rt in items:
            icon = "🔁" if rt != "none" else ""
            msg += f"• {title} {icon}\n"
        msg += "\n"
    msg += "💡 Нажмите на событие для управления"
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown", reply_markup=main_kb())
    else:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_kb())

async def apply_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    title = q.data.replace("tpl_", "")
    context.user_data['tpl_title'] = title
    await q.edit_message_text(f"📝 *{title}*\n\nУкажите время (например: `завтра 10:00`):", parse_mode="Markdown")
    context.user_data['awaiting_reminder'] = True

# 🔥 ИСПРАВЛЕННЫЙ ХЕНДЛЕР ЧАСОВЫХ ПОЯСОВ
async def handle_tz_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; d = q; d = q
