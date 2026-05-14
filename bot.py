# ==========================================
# TELEGRAM REMINDER BOT - RAILWAY VERSION
# ==========================================

import os, sys, sqlite3, logging, logging.handlers, warnings, re, asyncio, signal
from datetime import datetime, timedelta
import pytz
from timezonefinder import TimezoneFinder

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler = logging.StreamHandler(sys.stdout)
file_handler = logging.handlers.RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=3)
console_handler.setFormatter(log_formatter)
file_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("❌ Не найдена переменная окружения BOT_TOKEN")
    sys.exit(1)

DB_PATH = "reminders.db"
tf = TimezoneFinder()

# Состояния диалога
WAITING_TITLE = 0
WAITING_DATETIME = 1
WAITING_REPEAT = 2

# ========== БАЗА ДАННЫХ ==========
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
        logger.info("✅ База данных инициализирована")

init_db()

def register_user(user_id, username=None, first_name=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (user_id, username, first_name))
        conn.commit()

def add_reminder(user_id, title, reminder_datetime, repeat_type='none'):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO reminders (user_id, title, reminder_datetime, repeat_type) VALUES (?, ?, ?, ?)''', (user_id, title, reminder_datetime, repeat_type))
        conn.commit()
        return cursor.lastrowid

def get_reminders(user_id, only_active=True):
    with sqlite3.connect(DB_PATH) as conn:
        q = "SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? AND is_active=1 ORDER BY reminder_datetime" if only_active else "SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? ORDER BY reminder_datetime"
        return conn.execute(q, (user_id,)).fetchall()

def get_upcoming_reminders(user_id, limit=10):
    now_utc = datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute('''SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? AND is_active=1 AND reminder_datetime > ? ORDER BY reminder_datetime LIMIT ?''', (user_id, now_utc, limit)).fetchall()

def get_reminder_by_id(reminder_id, user_id):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE id=? AND user_id=?", (reminder_id, user_id)).fetchone()

def delete_reminder(reminder_id, user_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE reminders SET is_active=0 WHERE id=? AND user_id=?", (reminder_id, user_id))
        conn.commit()

# ========== ЧАСОВЫЕ ПОЯСА И УТИЛИТЫ ==========
def get_user_tz(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT timezone FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row and row[0]:
            try: return pytz.timezone(row[0])
            except: pass
    return pytz.timezone('Europe/Moscow')

def format_datetime(dt_str, user_tz):
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        return pytz.UTC.localize(dt).astimezone(user_tz).strftime("%d.%m в %H:%M")
    except: return dt_str

def parse_datetime(text):
    text = text.lower().strip(); now = datetime.now()
    m = re.match(r'^([0-1]?[0-9]|2[0-3]):([0-5][0-9])$', text)
    if m:
        res = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return res + timedelta(days=1) if res <= now else res
    m = re.match(r'завтра\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])$', text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return (now + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
    wd = {'пн':0,'понедельник':0,'вт':1,'вторник':1,'ср':2,'среда':2,'чт':3,'четверг':3,'пт':4,'пятница':4,'сб':5,'суббота':5,'вс':6,'воскресенье':6}
    for n, w in wd.items():
        if text.startswith(n):
            tm = re.search(r'([0-1]?[0-9]|2[0-3]):([0-5][0-9])', text)
            if tm:
                h, mi = int(tm.group(1)), int(tm.group(2))
                d = w - now.weekday()
                if d <= 0: d += 7
                return (now + timedelta(days=d)).replace(hour=h, minute=mi, second=0, microsecond=0)
    m = re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])', text)
    if m:
        try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5)))
        except: pass
    m = re.match(r'(\d{1,2})\.(\d{1,2})\s+([0-1]?[0-9]|2[0-3]):([0-5][0-9])', text)
    if m:
        d, mo, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        res = datetime(now.year, mo, d, h, mi)
        return res.replace(year=now.year+1) if res < now else res
    return None

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать", callback_data="create_reminder")],
        [InlineKeyboardButton("📋 Мои", callback_data="list_reminders")],
        [InlineKeyboardButton("⏰ Ближайшие", callback_data="upcoming_reminders")],
        [InlineKeyboardButton("⌚ Часовой пояс", callback_data="set_tz_btn")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")]
    ])

def get_repeat_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔹 Без повтора", callback_data="repeat_none")],
        [InlineKeyboardButton("🔄 Ежедневно", callback_data="repeat_daily")],
        [InlineKeyboardButton("📆 Еженедельно", callback_data="repeat_weekly")],
        [InlineKeyboardButton("📅 Ежемесячно", callback_data="repeat_monthly")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ])

def get_timezone_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇷🇺 Москва (UTC+3)", callback_data="tz_Europe/Moscow"), InlineKeyboardButton("🇷 Екатеринбург (UTC+5)", callback_data="tz_Asia/Yekaterinburg")],
        [InlineKeyboardButton("🇷 Новосибирск (UTC+7)", callback_data="tz_Asia/Novosibirsk"), InlineKeyboardButton("🇷🇺 Владивосток (UTC+10)", callback_data="tz_Asia/Vladivostok")],
        [InlineKeyboardButton("🇷🇺 Камчатка (UTC+12)", callback_data="tz_Asia/Kamchatka"), InlineKeyboardButton("🌍 Лондон (UTC+0)", callback_data="tz_Europe/London")],
        [InlineKeyboardButton("🇦 Дубай (UTC+4)", callback_data="tz_Asia/Dubai"), InlineKeyboardButton("🇺 Нью-Йорк (UTC-5)", callback_data="tz_America/New_York")],
        [InlineKeyboardButton("🇯🇵 Токио (UTC+9)", callback_data="tz_Asia/Tokyo"), InlineKeyboardButton("🌐 UTC", callback_data="tz_UTC")],
        [InlineKeyboardButton("📍 Авто (GPS)", callback_data="gps_request")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]
    ])

# ========== ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name)
    tz = get_user_tz(user.id).zone
    await update.message.reply_text(f"✨ *Привет!* Я бот для напоминаний.\n🌍 Ваш пояс: *{tz}*\n\n➕ `/add` | 📋 `/myevents` | ⌚ `/settz`", parse_mode="Markdown")

async def set_timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌍 Выберите часовой пояс:", parse_mode="Markdown", reply_markup=get_timezone_keyboard())

async def create_reminder_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("📝 *Новое напоминание*\n\nВведите название:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="cancel")]]))
    else: await update.message.reply_text("📝 *Новое напоминание*\n\nВведите название:", parse_mode="Markdown")
    return WAITING_TITLE

async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if not title: await update.message.reply_text("❌ Название не может быть пустым."); return WAITING_TITLE
    context.user_data['reminder_title'] = title
    await update.message.reply_text(f"✅ *{title}*\n\n📅 Введите дату и время:\n• `15:30` — сегодня\n• `завтра 18:00`\n• `31.12 20:00`\n• `пн 10:00`", parse_mode="Markdown")
    return WAITING_DATETIME

async def handle_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = parse_datetime(update.message.text.strip())
    if not dt: await update.message.reply_text("❌ Неверный формат. Примеры: `15:30`, `завтра 18:00`", parse_mode="Markdown"); return WAITING_DATETIME
    context.user_data['reminder_datetime'] = dt
    await update.message.reply_text(f"✅ {dt.strftime('%d.%m в %H:%M')}\n\n🔄 Выберите повтор:", parse_mode="Markdown", reply_markup=get_repeat_keyboard())
    return WAITING_REPEAT

async def schedule_reminder(user_id, title, reminder_id, reminder_utc_dt, repeat_type, context):
    scheduler = context.bot_data.get('scheduler')
    if scheduler: scheduler.add_job(send_reminder, trigger=DateTrigger(run_date=reminder_utc_dt), args=[user_id, title, reminder_id, context.application], id=f"reminder_{reminder_id}", replace_existing=True)

async def send_reminder(user_id, title, reminder_id, application):
    try: await application.bot.send_message(chat_id=user_id, text=f"🔔 *Напоминание!*\n\n📝 {title}", parse_mode="Markdown")
    except Exception as e: logger.error(f"Failed to send: {e}")

async def handle_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    repeat_type = query.data.replace("repeat_", "")
    repeat_text = {"none":"один раз","daily":"ежедневно","weekly":"еженедельно","monthly":"ежемесячно"}.get(repeat_type, "один раз")
    user_id = update.effective_user.id; title = context.user_data.get('reminder_title'); local_dt = context.user_data.get('reminder_datetime')
    user_tz = get_user_tz(user_id)
    utc_dt = user_tz.localize(local_dt, is_dst=True).astimezone(pytz.UTC)
    utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
    add_reminder(user_id, title, utc_str, repeat_type)
    await schedule_reminder(user_id, title, utc_str, utc_dt, repeat_type, context)
    context.user_data.clear()
    await query.edit_message_text(f"✅ Готово!\n\n📝 {title}\n📅 {local_dt.strftime('%d.%m в %H:%M')} ({user_tz.zone})\n🔄 {repeat_text}", parse_mode="Markdown", reply_markup=get_main_keyboard())
    return ConversationHandler.END

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    msg_func = query.edit_message_text if query else update.message.reply_text
    if query: await query.answer()
    user_id = update.effective_user.id; user_tz = get_user_tz(user_id); reminders = get_reminders(user_id)
    if not reminders: await msg_func("📭 Пусто. Создайте напоминание ➕", parse_mode="Markdown", reply_markup=get_main_keyboard()); return
    kb = []; icons = {"none": "", "daily": "🔄", "weekly": "📆", "monthly": "📅"}
    for rem in reminders:
        rid, title, dt, rtype = rem
        time_str = format_datetime(dt, user_tz); icon = icons.get(rtype, "")
        kb.append([InlineKeyboardButton(f"{title[:20]} • {time_str} {icon}", callback_data=f"view_{rid}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    await msg_func("📋 Ваши напоминания:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def upcoming_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; msg_func = query.edit_message_text if query else update.message.reply_text
    if query: await query.answer()
    user_id = update.effective_user.id; user_tz = get_user_tz(user_id); reminders = get_upcoming_reminders(user_id)
    if not reminders: await msg_func("⏰ Нет предстоящих", parse_mode="Markdown", reply_markup=get_main_keyboard()); return
    msg = "⏰ Ближайшие:\n\n" + "".join(f"• {r[1]} — {format_datetime(r[2], user_tz)}\n" for r in reminders)
    await msg_func(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]))

async def my_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; user_tz = get_user_tz(user_id); reminders = get_reminders(user_id, only_active=True)
    if not reminders: await update.message.reply_text("📭 Пока пусто. Создайте: ➕ или `/add`", parse_mode="Markdown"); return
    lines = [f"📅 *Ваши мероприятия ({user_tz.zone}):*"]
    for title, dt, rtype in [(r[1], r[2], r[3]) for r in reminders]:
        icon = {"none":"","daily":" 🔄","weekly":" 📆","monthly":" 📅"}.get(rtype, "")
        lines.append(f"• {title} — {format_datetime(dt, user_tz)}{icon}")
    lines.append("\n💡 Удалить: откройте «📋 Мои» и нажмите на напоминание")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def view_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    rid = int(query.data.split("_")[1]); user_id = update.effective_user.id; user_tz = get_user_tz(user_id)
    rem = get_reminder_by_id(rid, user_id)
    if not rem: await query.edit_message_text("❌ Не найдено", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")]])); return
    txt = {"none":"один раз","daily":"ежедневно","weekly":"еженедельно","monthly":"ежемесячно"}.get(rem[3], "один раз")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{rid}")], [InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")], [InlineKeyboardButton("🏠 Меню", callback_data="back_to_main")]])
    await query.edit_message_text(f"📌 {rem[1]}\n📅 {format_datetime(rem[2], user_tz)}\n🔄 {txt}", parse_mode="Markdown", reply_markup=kb)

async def delete_reminder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    rid = int(query.data.split("_")[1]); user_id = update.effective_user.id; delete_reminder(rid, user_id)
    sched = context.bot_data.get('scheduler')
    if sched:
        try: sched.remove_job(f"reminder_{rid}")
        except: pass
    await query.edit_message_text("✅ Удалено", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")]]))

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; msg_func = query.edit_message_text if query else update.message.reply_text
    if query: await query.answer()
    await msg_func("*❓ Помощь*\n\n📅 `15:30` / `завтра 18:00` / `31.12 20:00`\n🌍 Пояс: кнопка ⌚ или `/settz`\n📍 GPS: нажмите «Авто (GPS)» в меню поясов\n🔄 Повтор: один раз / ежедневно / еженедельно / ежемесячно\n📋 Команды: `/start` • `/add` • `/myevents` • `/settz` • `/help`", parse_mode="Markdown", reply_markup=get_main_keyboard() if query else None)

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("🏠 Главное меню", parse_mode="Markdown", reply_markup=get_main_keyboard())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer(); await query.edit_message_text("❌ Отменено", parse_mode="Markdown", reply_markup=get_main_keyboard())
    else: await update.message.reply_text("❌ Отменено", parse_mode="Markdown", reply_markup=get_main_keyboard())
    context.user_data.clear(); return ConversationHandler.END

async def request_gps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    kb = [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]]
    await query.message.reply_text("📍 Нажмите кнопку ниже, чтобы бот определил пояс автоматически:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    context.user_data['waiting_for_gps_tz'] = True

async def handle_gps_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_gps_tz') or not update.message.location: return
    lat = update.message.location.latitude; lon = update.message.location.longitude
    tz_name = tf.timezone_at(lat=lat, lng=lon)
    await update.message.reply_text("⌨️ Клавиатура скрыта", reply_markup=ReplyKeyboardRemove())
    context.user_data.pop('waiting_for_gps_tz', None)
    user_id = update.effective_user.id
    if tz_name:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz_name, user_id)); conn.commit()
        await update.message.reply_text(f"✅ Часовой пояс определён!\n🌍 *{tz_name}*\n⏰ Напоминания будут приходить по этому времени.", parse_mode="Markdown", reply_markup=get_main_keyboard())
    else: await update.message.reply_text("❌ Не удалось определить пояс. Выберите вручную:", reply_markup=get_timezone_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; data = query.data; await query.answer()
    if data == "list_reminders": await list_reminders(update, context)
    elif data == "upcoming_reminders": await upcoming_reminders(update, context)
    elif data == "set_tz_btn": await query.edit_message_text("🌍 *Выберите часовой пояс:*", parse_mode="Markdown", reply_markup=get_timezone_keyboard())
    elif data == "gps_request": await request_gps(update, context)
    elif data.startswith("tz_"):
        tz_name = data[3:]
        try:
            tz = pytz.timezone(tz_name)
            with sqlite3.connect(DB_PATH) as conn: conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz.zone, query.from_user.id)); conn.commit()
            await query.edit_message_text(f"✅ *Пояс изменён!*\n\n🌍 *{tz.zone}*\n⏰ Все напоминания будут приходить по этому времени.", parse_mode="Markdown", reply_markup=get_main_keyboard())
        except: await query.edit_message_text("❌ Ошибка выбора пояса.", reply_markup=get_main_keyboard())
    elif data == "help": await help_handler(update, context)
    elif data == "back_to_main": await back_to_main(update, context)
    elif data == "cancel": await cancel(update, context)
    elif data.startswith("view_"): await view_reminder(update, context)
    elif data.startswith("delete_"): await delete_reminder_handler(update, context)
    elif data.startswith("repeat_"): await handle_repeat(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"ERROR: {context.error}")

# ========== ЗАПУСК БОТА ==========
app = None

async def main():
    global app
    logger.info("🔗 Инициализация бота...")
    app = Application.builder().token(BOT_TOKEN).build()
    
    sched = AsyncIOScheduler(); sched.start(); app.bot_data['scheduler'] = sched

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", create_reminder_start), CallbackQueryHandler(create_reminder_start, pattern="^create_reminder$")],
        states={
            WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            WAITING_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_datetime)],
            WAITING_REPEAT: [CallbackQueryHandler(handle_repeat, pattern="^repeat_")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^cancel$")]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settz", set_timezone_cmd))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("myevents", my_events))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.LOCATION, handle_gps_location))
    app.add_error_handler(error_handler)

    cmds = [
        BotCommand("start", "🚀 Старт"),
        BotCommand("add", "➕ Создать"),
        BotCommand("myevents", "📋 Мои"),
        BotCommand("settz", "⌚ Часовой пояс"),
        BotCommand("help", "❓ Помощь")
    ]
    await app.bot.set_my_commands(cmds)

    logger.info("✅ Бот запущен и готов к работе!")
    await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)

    # Ждём сигнала остановки
    stop_event = asyncio.Event()
    def shutdown_signal():
        stop_event.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_signal)
        
    await stop_event.wait()
    await app.updater.stop(); await app.stop(); await app.shutdown(); sched.shutdown()
    logger.info("🛑 Бот корректно остановлен.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Завершение работы по Ctrl+C")
