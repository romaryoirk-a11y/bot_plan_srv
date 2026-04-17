import os
import logging
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def homepage(request):
    return PlainTextResponse("🤖 Telegram Reminder Bot is running")

async def webhook_handler(request):
    if request.method != "POST":
        return PlainTextResponse("Method Not Allowed", status_code=405)
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set")
        return JSONResponse({"error": "Server configuration error"}, status_code=500)
    
    try:
        data = await request.json()
        logger.info(f"Received update: {data.get('update_id', 'unknown')}")
        
        # 🔥 ЛЕНИВЫЙ ИМПОРТ — только когда реально нужно
        from telegram import Update
        from telegram.ext import Application
        
        app_ptb = Application.builder().token(BOT_TOKEN).build()
        update = Update.de_json(data, app_ptb.bot)
        
        if update and update.message:
            logger.info(f"Message from {update.effective_user.id}: {update.message.text}")
            # Здесь можно добавить вашу логику обработки
        
        return PlainTextResponse("OK")
        
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse({"error": "Internal server error"}, status_code=500)

# 🔥 ТОЧКА ВХОДА — на верхнем уровне модуля
app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/api/webhook", webhook_handler, methods=["POST"]),
        Route("/api/webhook", homepage, methods=["GET"]),  # Для проверки в браузере
    ]
)

# Алиасы для совместимости с разными платформами
application = app
handler = app
