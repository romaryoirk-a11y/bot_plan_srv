import os, sys, sqlite3, logging, re, asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

# 🔥 БЕЗОПАСНЫЙ ИМПОРТ (НЕ ЛОМАЕТ СБОРКУ VERCEL)
try:
    from timezonefinder import TimezoneFinder
    tf = TimezoneFinder()
except Exception:
    tf = None  # Фолбэк для серверов без timezonefinder

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "/tmp/reminders.db"  # Vercel позволяет писать только в /tmp
tf = tf or (lambda lat, lng: "UTC")  # Фолбэк-функция если библиотека не загрузилась

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
def tz_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("🇷🇺 Москва (UTC+3)", callback_data="tz_Europe/Moscow"), InlineKeyboardButton("🇷🇺 Екатеринбург (UTC+5)", callback_data="tz_Asia/Yekaterinburg")],[InlineKeyboardButton("🇷🇺 Новосибирск (UTC+7)", callback_data="tz_Asia/Novosibirsk"), InlineKeyboardButton("🇷🇺 Владивосток (UTC+10)", callback_data="tz_Asia/Vladivostok")],[InlineKeyboardButton("🇷🇺 Камчатка (UTC+12)", callback_data="tz_Asia/Kamchatka"), InlineKeyboardButton("🌍 Лондон (UTC+0)", callback_data="tz_Europe/London")],[InlineKeyboardButton("🇦🇪 Дубай (UTC+4)", callback_data="tz_Asia/Dubai"), InlineKeyboardButton("🇺🇸 Нью-Йорк (UTC-5)", callback_data="tz_America/New_York")],[InlineKeyboardButton("🇯🇵 Токио (UTC+9)", callback_data="tz_Asia/Tokyo"), InlineKeyboardButton("🌐 UTC", callback_data="tz_UTC")],[InlineKeyboardButton("📍 Авто (GPS)", callback_data="gps_request")],[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]])

# ========== PTB & HANDLERS ==========
ptb_app = Application.builder().token(BOT_TOKEN or "BUILD_DUMMY").build()
sched = AsyncIOScheduler()
sched.start()
ptb_app.bot_data['scheduler'] = sched

async def start_cmd(u, c): register_user(u.effective_user.id, u.effective_user.username, u.effective_user.first_name); await u.message.reply_text(f"✨ *Привет!* Я бот для напоминаний.\n🌍 Ваш пояс: *{get_user_tz(u.effective_user.id).zone}*\n\n➕ `/add` | 📋 `/myevents` | ⌚ `/settz`", parse_mode="Markdown")
async def settz_cmd(u, c): await u.message.reply_text("🌍 Выберите часовой пояс:", parse_mode="Markdown", reply_markup=tz_kb())
async def create_start(u, c): q=u.callback_query; await q.answer(); await q.edit_message_text("📝 *Новое напоминание*\n\nВведите название:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="cancel")]])) if q else await u.message.reply_text("📝 *Новое напоминание*\n\nВведите название:", parse_mode="Markdown"); return WAITING_TITLE
async def handle_title(u, c): t=u.message.text.strip(); 
    if not t: await u.message.reply_text("❌ Название не может быть пустым."); return WAITING_TITLE
    c.user_data['rem_title'] = t; await u.message.reply_text(f"✅ *{t}*\n\n📅 Введите дату и время:\n• `15:30` — сегодня\n• `завтра 18:00`\n• `31.12 20:00`\n• `пн 10:00`", parse_mode="Markdown"); return WAITING_DATETIME
async def handle_dt(u, c): dt=parse_dt(u.message.text.strip()); 
    if not dt: await u.message.reply_text("❌ Неверный формат. Примеры: `15:30`, `завтра 18:00`", parse_mode="Markdown"); return WAITING_DATETIME
    c.user_data['rem_dt'] = dt; await u.message.reply_text(f"✅ {dt.strftime('%d.%m в %H:%M')}\n\n🔄 Выберите повтор:", parse_mode="Markdown", reply_markup=repeat_kb()); return WAITING_REPEAT
async def schedule_rem(uid, t, rid, utc, rt, ctx): s=ctx.bot_data.get('scheduler'); s.add_job(send_rem, trigger=DateTrigger(run_date=utc), args=[uid, t, rid, ctx.application], id=f"rem_{rid}", replace_existing=True) if s else None
async def send_rem(uid, t, rid, app): 
    try: await app.bot.send_message(chat_id=uid, text=f"🔔 *Напоминание!*\n\n📝 {t}", parse_mode="Markdown")
    except Exception as e: logger.error(f"Send err: {e}")
async def handle_repeat(u, c): q=u.callback_query; await q.answer(); rt=q.data.replace("repeat_",""); rtx={"none":"один раз","daily":"ежедневно","weekly":"еженедельно","monthly":"ежемесячно"}.get(rt,"один раз"); uid=u.effective_user.id; t=c.user_data.get('rem_title'); ldt=c.user_data.get('rem_dt'); tz=get_user_tz(uid); utc=tz.localize(ldt, is_dst=True).astimezone(pytz.UTC); add_reminder(uid, t, utc.strftime("%Y-%m-%d %H:%M:%S"), rt); await schedule_rem(uid, t, utc.strftime("%Y-%m-%d %H:%M:%S"), utc, rt, c); c.user_data.clear(); await q.edit_message_text(f"✅ Готово!\n\n📝 {t}\n📅 {ldt.strftime('%d.%m в %H:%M')} ({tz.zone})\n🔄 {rtx}", parse_mode="Markdown", reply_markup=main_kb()); return ConversationHandler.END
async def list_rem(u, c): q=u.callback_query; mf=q.edit_message_text if q else u.message.reply_text; await q.answer() if q else None; uid=u.effective_user.id; tz=get_user_tz(uid); rems=get_reminders(uid); 
    if not rems: return await mf("📭 Пусто. Создайте напоминание ➕", parse_mode="Markdown", reply_markup=main_kb())
    kb=[]; icons={"none":"","daily":"🔄","weekly":"📆","monthly":"📅"}
    for r in rems: rid,t,dt,rt=r; kb.append([InlineKeyboardButton(f"{t[:20]} • {fmt_dt(dt, tz)} {icons.get(rt,'')}", callback_data=f"view_{rid}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]); await mf("📋 Ваши напоминания:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
async def upcoming_rem(u, c): q=u.callback_query; mf=q.edit_message_text if q else u.message.reply_text; await q.answer() if q else None; uid=u.effective_user.id; tz=get_user_tz(uid); rems=get_upcoming(uid); 
    if not rems: return await mf("⏰ Нет предстоящих", parse_mode="Markdown", reply_markup=main_kb())
    msg="⏰ Ближайшие:\n\n"+"".join(f"• {r[1]} — {fmt_dt(r[2], tz)}\n" for r in rems); await mf(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]))
async def my_events(u, c): uid=u.effective_user.id; tz=get_user_tz(uid); rems=get_reminders(uid); 
    if not rems: return await u.message.reply_text("📭 Пока пусто. Создайте: ➕ или `/add`", parse_mode="Markdown")
    lines=[f"📅 *Ваши мероприятия ({tz.zone}):*"]; 
    for t,dt,rt in [(r[1],r[2],r[3]) for r in rems]: icon={"none":"","daily":" 🔄","weekly":" 📆","monthly":" 📅"}.get(rt,""); lines.append(f"• {t} — {fmt_dt(dt, tz)}{icon}")
    lines.append("\n💡 Удалить: откройте «📋 Мои» и нажмите на напоминание"); await u.message.reply_text("\n".join(lines), parse_mode="Markdown")
async def view_rem(u, c): q=u.callback_query; await q.answer(); rid=int(q.data.split("_")[1]); uid=u.effective_user.id; tz=get_user_tz(uid); rem=get_reminder(rid, uid); 
    if not rem: return await q.edit_message_text("❌ Не найдено", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")]]))
    txt={"none":"один раз","daily":"ежедневно","weekly":"еженедельно","monthly":"ежемесячно"}.get(rem[3],"один раз"); kb=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{rid}")],[InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")],[InlineKeyboardButton("🏠 Меню", callback_data="back_to_main")]]); await q.edit_message_text(f"📌 {rem[1]}\n📅 {fmt_dt(rem[2], tz)}\n🔄 {txt}", parse_mode="Markdown", reply_markup=kb)
async def del_rem(u, c): q=u.callback_query; await q.answer(); rid=int(q.data.split("_")[1]); uid=u.effective_user.id; del_reminder(rid, uid); s=c.bot_data.get('scheduler'); 
    if s: 
        try: s.remove_job(f"rem_{rid}")
        except: pass
    await q.edit_message_text("✅ Удалено", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="list_reminders")]]))
async def help_cmd(u, c): q=u.callback_query; mf=q.edit_message_text if q else u.message.reply_text; await q.answer() if q else None; await mf("*❓ Помощь*\n\n📅 `15:30` / `завтра 18:00` / `31.12 20:00`\n🌍 Пояс: кнопка ⌚ или `/settz`\n📍 GPS: нажмите «Авто (GPS)» в меню поясов\n🔄 Повтор: один раз / ежедневно / еженедельно / ежемесячно\n📋 Команды: `/start` • `/add` • `/myevents` • `/settz` • `/help`", parse_mode="Markdown", reply_markup=main_kb() if q else None)
async def back_main(u, c): q=u.callback_query; await q.answer(); await q.edit_message_text("🏠 Главное меню", parse_mode="Markdown", reply_markup=main_kb())
async def cancel_cmd(u, c): q=u.callback_query; 
    if q: await q.answer(); await q.edit_message_text("❌ Отменено", parse_mode="Markdown", reply_markup=main_kb())
    else: await u.message.reply_text("❌ Отменено", parse_mode="Markdown", reply_markup=main_kb())
    c.user_data.clear(); return ConversationHandler.END
async def request_gps(u, c): q=u.callback_query; await q.answer(); kb=[[KeyboardButton("📍 Отправить геолокацию", request_location=True)]]; await q.message.reply_text("📍 Нажмите кнопку ниже, чтобы бот определил пояс автоматически:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)); c.user_data['waiting_for_gps_tz']=True
async def handle_gps(u, c): 
    if not c.user_data.get('waiting_for_gps_tz') or not u.message.location: return
    lat=u.message.location.latitude; lon=u.message.location.longitude
    tz_name = tf(lat, lng) if callable(tf) else "UTC"
    await u.message.reply_text("⌨️ Клавиатура скрыта", reply_markup=ReplyKeyboardRemove()); c.user_data.pop('waiting_for_gps_tz', None)
    uid=u.effective_user.id
    if tz_name and tz_name != "UTC":
        with sqlite3.connect(DB_PATH) as conn: conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz_name, uid)); conn.commit()
        await u.message.reply_text(f"✅ Часовой пояс определён!\n🌍 *{tz_name}*\n⏰ Напоминания будут приходить по этому времени.", parse_mode="Markdown", reply_markup=main_kb())
    else: await u.message.reply_text("❌ Не удалось определить пояс. Выберите вручную:", reply_markup=tz_kb())

async def button_dispatch(u, c):
    q=u.callback_query; d=q.data; await q.answer()
    if d=="list_reminders": await list_rem(u,c)
    elif d=="upcoming_reminders": await upcoming_rem(u,c)
    elif d=="set_tz_btn": await q.edit_message_text("🌍 *Выберите часовой пояс:*", parse_mode="Markdown", reply_markup=tz_kb())
    elif d=="gps_request": await request_gps(u,c)
    elif d.startswith("tz_"):
        try: tz=pytz.timezone(d[3:]); 
            with sqlite3.connect(DB_PATH) as conn: conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz.zone, q.from_user.id)); conn.commit()
            await q.edit_message_text(f"✅ *Пояс изменён!*\n\n🌍 *{tz.zone}*\n⏰ Все напоминания будут приходить по этому времени.", parse_mode="Markdown", reply_markup=main_kb())
        except: await q.edit_message_text("❌ Ошибка выбора пояса.", reply_markup=main_kb())
    elif d=="help": await help_cmd(u,c)
    elif d=="back_to_main": await back_main(u,c)
    elif d=="cancel": await cancel_cmd(u,c)
    elif d.startswith("view_"): await view_rem(u,c)
    elif d.startswith("delete_"): await del_rem(u,c)
    elif d.startswith("repeat_"): await handle_repeat(u,c)

conv = ConversationHandler(entry_points=[CommandHandler("add", create_start), CallbackQueryHandler(create_start, pattern="^create_reminder$")], states={WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)], WAITING_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_dt)], WAITING_REPEAT: [CallbackQueryHandler(handle_repeat, pattern="^repeat_")]}, fallbacks=[CommandHandler("cancel", cancel_cmd), CallbackQueryHandler(cancel_cmd, pattern="^cancel$")])
ptb_app.add_handler(CommandHandler("start", start_cmd))
ptb_app.add_handler(CommandHandler("settz", settz_cmd))
ptb_app.add_handler(CommandHandler("help", help_cmd))
ptb_app.add_handler(CommandHandler("myevents", my_events))
ptb_app.add_handler(conv)
ptb_app.add_handler(CallbackQueryHandler(button_dispatch))
ptb_app.add_handler(MessageHandler(filters.LOCATION, handle_gps))

# ========== ASGI ENTRY POINT (ОБЯЗАТЕЛЬНО НА УРОВНЕ МОДУЛЯ) ==========
@asynccontextmanager
async def lifespan(context):
    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.bot.set_my_commands([BotCommand("start","🚀 Старт"), BotCommand("add","➕ Создать"), BotCommand("myevents","📋 Мои"), BotCommand("settz","⌚ Часовой пояс"), BotCommand("help","❓ Помощь")])
    logger.info("✅ Бот инициализирован и готов принимать вебхуки")
    yield
    await ptb_app.stop()
    await ptb_app.shutdown()
    sched.shutdown()

async def webhook_handler(request):
    if request.method == "POST":
        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        if update: await ptb_app.process_update(update)
        return PlainTextResponse("OK")
    return PlainTextResponse("Method Not Allowed", status_code=405)

# 🔥 ГЛАВНАЯ ТОЧКА ВХОДА (ИЩЕТ VERCEL)
app = Starlette(lifespan=lifespan, routes=[Route("/", webhook_handler)])
application = app
handler = app
