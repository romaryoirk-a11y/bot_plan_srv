# ==========================================
# TELEGRAM REMINDER BOT - RAILWAY OPTIMIZED (FIXED)
# ==========================================

import os, sys, sqlite3, logging, logging.handlers, re, asyncio, signal
from datetime import datetime, timedelta
import pytz
from timezonefinder import TimezoneFinder

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
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
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT,
                         reminder_datetime TEXT, repeat_type TEXT DEFAULT 'none', is_active INTEGER DEFAULT 1,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users (user_id))''')
        conn.commit()
init_db()

def register_user(uid, un=None, fn=None):
    with sqlite3.connect(DB_PATH) as conn: conn.execute("INSERT OR IGNORE INTO users VALUES (?,?,?,?,CURRENT_TIMESTAMP)", (uid, un, fn, 'Europe/Moscow')); conn.commit()
def add_reminder(uid, t, dt, rt='none'):
    with sqlite3.connect(DB_PATH) as conn: c=conn.cursor(); c.execute("INSERT INTO reminders (user_id, title, reminder_datetime, repeat_type) VALUES (?,?,?,?)", (uid,t,dt,rt)); conn.commit(); return c.lastrowid
def get_reminders(uid, act=True):
    with sqlite3.connect(DB_PATH) as conn: q="SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? AND is_active=1 ORDER BY reminder_datetime" if act else "SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? ORDER BY reminder_datetime"; return conn.execute(q, (uid,)).fetchall()
def get_upcoming(uid, lim=10):
    now=datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn: return conn.execute("SELECT id, title, reminder_datetime, repeat_type FROM reminders WHERE user_id=? AND is_active=1 AND reminder_datetime > ? ORDER BY reminder_datetime LIMIT ?", (uid,now,lim)).fetchall()
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
    [InlineKeyboardButton("🇷🇺 Москва (UTC+3)", callback_data="tz_Europe/Moscow"), InlineKeyboardButton("🇷🇺 Екатеринбург (UTC+5)", callback_data="tz_Asia/Yekaterinburg")],
    [InlineKeyboardButton("🇷🇺 Новосибирск (UTC+7)", callback_data="tz_Asia/Novosibirsk"), InlineKeyboardButton("🇷🇺 Владивосток (UTC+10)", callback_data="tz_Asia/Vladivostok")],
    [InlineKeyboardButton("🌍 Лондон (UTC+0)", callback_data="tz_Europe/London"), InlineKeyboardButton("🌐 UTC", callback_data="tz_UTC")],
    [InlineKeyboardButton("📍 Авто (GPS)", callback_data="gps_request")],
    [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
])

def template_kb(): return InlineKeyboardMarkup([
    [InlineKeyboardButton("💊 Лекарства", callback_data="tpl_Лекарства"), InlineKeyboardButton("🏋️ Тренировка", callback_data="tpl_Тренировка")],
    [InlineKeyboardButton("📞 Звонок", callback_data="tpl_Звонок"), InlineKeyboardButton("📝 Отчёт", callback_data="tpl_Отчёт")],
    [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
])

# ========== УНИВЕРСАЛЬНЫЕ ХЕНДЛЕРЫ (РАБОТАЮТ И КАК КОМАНДЫ, И КАК КНОПКИ) ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_user.username, update.effective_user.first_name)
    tz = get_user_tz(update.effective_user.id).zone
    await update.message.reply_text(
        f"👋 *Привет!* Я бот-напоминалка.\n\n"
        f"🌍 Пояс: *{tz}*\n\n"
        f"💡 *Быстрый старт:*\n"
        f"• Нажмите ➕ *Создать*\n"
        f"• Или напишите: `Встреча завтра 15:00`\n\n"
        f"📋 `/myevents` — список\n⚙️ `/settings` — настройки",
        parse_mode="Markdown", reply_markup=main_kb()
    )

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
    txt = "📝 *Новое напоминание*\n\nНапишите в одном сообщении:\n`Название + время`\n\nПримеры:\n• `Встреча завтра 15:00`\n• `Купить молоко сегодня 18:30`\n• `Отчёт 31.12 20:00`"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown")
    else:
        await update.message.reply_text(txt, parse_mode="Markdown")
    context.user_data['awaiting_reminder'] = True

async def handle_reminder_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_reminder'): return
    context.user_data.pop('awaiting_reminder', None)
    text = update.message.text
    title, dt = parse_natural_reminder(text)
    
    if not title or not dt:
        return await update.message.reply_text("❌ Не удалось распознать время.\n\nПопробуйте формат:\n`Название завтра 15:00`\n`Задача сегодня 18:30`", parse_mode="Markdown", reply_markup=main_kb())
    
    uid = update.effective_user.id; tz = get_user_tz(uid)
    utc_dt = tz.localize(dt, is_dst=True).astimezone(pytz.UTC)
    rid = add_reminder(uid, title, utc_dt.strftime("%Y-%m-%d %H:%M:%S"), "none")
    
    sched = context.bot_data.get('scheduler')
    if sched: sched.add_job(send_reminder, trigger=DateTrigger(run_date=utc_dt), args=[uid, title, rid, context.application], id=f"rem_{rid}", replace_existing=True)
    
    await update.message.reply_text(
        f"✅ *Создано!*\n\n📌 {title}\n📅 {fmt_dt(utc_dt.strftime('%Y-%m-%d %H:%M:%S'), tz)}\n🔄 Без повтора\n\n💡 Нажмите на событие в списке «📋 Мои», чтобы изменить повтор или удалить.",
        parse_mode="Markdown", reply_markup=main_kb()
    )

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

async def handle_tz_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; d = q.data; await q.answer()
    if d == "gps_request":
        kb = [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]]
        await q.message.reply_text("📍 Нажмите кнопку ниже:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
        context.user_data['waiting_for_gps_tz'] = True
        return
    tz_name = d[3:] if d.startswith("tz_") else None
    if tz_name:
        try:
            tz = pytz.timezone(tz_name)
            set_user_tz(q.from_user.id, tz.zone)
            await q.edit_message_text(f"✅ *Пояс изменён!*\n🌍 {tz.zone}", parse_mode="Markdown", reply_markup=tz_kb())
        except: await q.edit_message_text("❌ Ошибка", reply_markup=tz_kb())
    elif d == "settings": await settings_menu(update, context)

async def handle_gps_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_gps_tz') or not update.message.location: return
    lat, lon = update.message.location.latitude, update.message.location.longitude
    tz_name = tf.timezone_at(lat=lat, lng=lon)
    await update.message.reply_text("⌨️ Готово", reply_markup=ReplyKeyboardRemove())
    context.user_data.pop('waiting_for_gps_tz', None)
    if tz_name:
        set_user_tz(update.effective_user.id, tz_name)
        await update.message.reply_text(f"✅ Пояс определён: *{tz_name}*", parse_mode="Markdown", reply_markup=main_kb())
    else: await update.message.reply_text("❌ Не удалось определить. Выберите вручную:", reply_markup=tz_kb())

async def view_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = int(q.data.split("_")[1]); uid = update.effective_user.id; tz = get_user_tz(uid)
    rem = get_reminder(rid, uid)
    if not rem: return await q.edit_message_text("❌ Не найдено", reply_markup=main_kb())
    _, title, dt, rt = rem
    txt = {"none":"Без повтора","daily":"Ежедневно","weekly":"Еженедельно","monthly":"Ежемесячно"}.get(rt, "Без повтора")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Изменить повтор", callback_data=f"repeat_{rid}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"del_{rid}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")]
    ])
    await q.edit_message_text(f"📌 *{title}*\n📅 {fmt_dt(dt, tz)}\n🔄 {txt}", parse_mode="Markdown", reply_markup=kb)

async def delete_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = int(q.data.split("_")[1]); uid = update.effective_user.id
    del_reminder(rid, uid)
    sched = context.bot_data.get('scheduler')
    if sched:
        try: sched.remove_job(f"rem_{rid}")
        except: pass
    await q.edit_message_text("✅ Удалено", reply_markup=main_kb())

async def repeat_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = int(q.data.split("_")[1])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔹 Без повтора", callback_data=f"setrep_{rid}_none")],
        [InlineKeyboardButton("🔄 Ежедневно", callback_data=f"setrep_{rid}_daily")],
        [InlineKeyboardButton("📆 Еженедельно", callback_data=f"setrep_{rid}_weekly")],
        [InlineKeyboardButton("📅 Ежемесячно", callback_data=f"setrep_{rid}_monthly")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"view_{rid}")]
    ])
    await q.edit_message_text("🔄 *Выберите повтор:*\n(Изменение применится к следующим срабатываниям)", parse_mode="Markdown", reply_markup=kb)

async def set_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_"); rid = int(parts[1]); rt = parts[2]
    uid = update.effective_user.id
    with sqlite3.connect(DB_PATH) as conn: conn.execute("UPDATE reminders SET repeat_type=? WHERE id=? AND user_id=?", (rt, rid, uid)); conn.commit()
    await q.edit_message_text(f"✅ Повтор изменён на: {rt}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"view_{rid}")]]))

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# ========== ЗАПУСК ==========
async def main():
    logger.info("🔗 Запуск бота...")
    app = Application.builder().token(BOT_TOKEN).build()
    sched = AsyncIOScheduler(); sched.start(); app.bot_data['scheduler'] = sched

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", settings_menu))
    app.add_handler(CommandHandler("myevents", list_reminders))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", create_prompt))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reminder_input))
    app.add_handler(CallbackQueryHandler(create_prompt, pattern="^create_prompt$"))
    app.add_handler(CallbackQueryHandler(list_reminders, pattern="^list_reminders$"))
    app.add_handler(CallbackQueryHandler(settings_menu, pattern="^settings$"))
    app.add_handler(CallbackQueryHandler(tz_menu, pattern="^tz_menu$"))
    app.add_handler(CallbackQueryHandler(templates_menu, pattern="^templates$"))
    app.add_handler(CallbackQueryHandler(apply_template, pattern="^tpl_"))
    app.add_handler(CallbackQueryHandler(handle_tz_select, pattern="^(tz_|gps_request|settings)$"))
    app.add_handler(CallbackQueryHandler(help_cmd, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(back_main, pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(view_reminder, pattern="^view_"))
    app.add_handler(CallbackQueryHandler(delete_reminder, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(repeat_menu, pattern="^repeat_"))
    app.add_handler(CallbackQueryHandler(set_repeat, pattern="^setrep_"))
    app.add_handler(MessageHandler(filters.LOCATION, handle_gps_location))
    app.add_error_handler(error_handler)

    await app.bot.set_my_commands([
        BotCommand("start", "🚀 Старт"), BotCommand("add", "➕ Создать"),
        BotCommand("myevents", "📋 Мои"), BotCommand("settings", "⚙️ Настройки")
    ])

    logger.info("✅ Бот запущен!")
    await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
    
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT): loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()
    await app.updater.stop(); await app.stop(); await app.shutdown(); sched.shutdown()

async def send_reminder(uid, title, rid, app):
    try: await app.bot.send_message(chat_id=uid, text=f"🔔 *Напоминание!*\n\n📝 *{title}*\n\nВремя пришло!", parse_mode="Markdown")
    except Exception as e: logger.error(f"Send fail: {e}")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Stopped by user")
