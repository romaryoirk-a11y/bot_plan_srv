import os
import logging
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set!")

async def homepage(request):
    return PlainTextResponse("🤖 Bot is running")

async def webhook_handler(request):
    if request.method != "POST":
        return PlainTextResponse("OK")
    
    try:
        from telegram import Update
        from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
        
        data = await request.json()
        logger.info(f"Received update: {data.get('update_id', 'unknown')}")
        
        # Создаем приложение только когда нужно
        app = Application.builder().token(BOT_TOKEN).build()
        
        # Простые хендлеры
        async def cmd_start(update, context):
            await update.message.reply_text("👋 Привет! Я бот напоминаний.\n\nИспользуй:\n/add - создать напоминание\n/myevents - мои события\n/settz - часовой пояс")
        
        async def cmd_help(update, context):
            await update.message.reply_text("📚 Помощь:\n\n/add - создать напоминание\n/myevents - список событий\n/settz - изменить пояс\n\nФорматы времени:\n15:30 - сегодня\nзавтра 18:00\n31.12 20:00")
        
        async def cmd_myevents(update, context):
            await update.message.reply_text("📋 Ваши мероприятия:\n\nПока пусто. Создайте первое напоминание командой /add")
        
        async def cmd_settz(update, context):
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = [
                [InlineKeyboardButton("🇷🇺 Москва", callback_data="tz_Moscow")],
                [InlineKeyboardButton("🔙 Отмена", callback_data="tz_cancel")]
            ]
            await update.message.reply_text("Выберите пояс:", reply_markup=InlineKeyboardMarkup(keyboard))
        
        async def button_handler(update, context):
            query = update.callback_query
            await query.answer()
            if query.data == "tz_cancel":
                await query.edit_message_text("Отменено")
            elif query.data.startswith("tz_"):
                await query.edit_message_text(f"✅ Пояс установлен: {query.data}")
        
        async def cmd_add(update, context):
            await update.message.reply_text("📝 Введите название напоминания:")
            return 1
        
        async def add_title(update, context):
            context.user_data['title'] = update.message.text
            await update.message.reply_text("📅 Введите время (например: 15:30 или завтра 18:00):")
            return 2
        
        async def add_time(update, context):
            title = context.user_data.get('title', 'Напоминание')
            time = update.message.text
            await update.message.reply_text(f"✅ Создано!\n\n📝 {title}\n⏰ {time}\n\nЯ напомню вам вовремя!")
            return ConversationHandler.END
        
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("add", cmd_add)],
            states={
                1: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
                2: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_time)]
            },
            fallbacks=[]
        )
        
        # Регистрируем хендлеры
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("myevents", cmd_myevents))
        app.add_handler(CommandHandler("settz", cmd_settz))
        app.add_handler(conv_handler)
        app.add_handler(CallbackQueryHandler(button_handler))
        
        # Обрабатываем обновление
        update_obj = Update.de_json(data, app.bot)
        if update_obj:
            await app.process_update(update_obj)
        
        return PlainTextResponse("OK")
        
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

# Простое приложение
app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/api/webhook", webhook_handler, methods=["POST"]),
    ]
)

application = app
handler = app
