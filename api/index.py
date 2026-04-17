import os
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

# 🔥 ПРОСТОЕ ОПРЕДЕЛЕНИЕ ПРИЛОЖЕНИЯ (БЕЗ СЛОЖНЫХ ИМПОРТОВ)
async def homepage(request):
    return PlainTextResponse("Telegram Bot is running")

async def webhook_handler(request):
    if request.method == "POST":
        try:
            data = await request.json()
            # Импортируем PTB только когда реально нужно
            from telegram import Update
            from telegram.ext import Application
            
            BOT_TOKEN = os.getenv("BOT_TOKEN")
            if not BOT_TOKEN:
                return JSONResponse({"error": "No token"}, status_code=500)
            
            app_ptb = Application.builder().token(BOT_TOKEN).build()
            update = Update.de_json(data, app_ptb.bot)
            
            if update:
                # Здесь можно добавить обработку, но для простоты пока лог
                print(f"Received update: {update}")
                
            return PlainTextResponse("OK")
        except Exception as e:
            print(f"Error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
    return PlainTextResponse("Method Not Allowed", status_code=405)

# 🔥 ГЛАВНАЯ ТОЧКА ВХОДА - ПРОСТАЯ И ПОНЯТНАЯ ДЛЯ VERCEL
app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/api/webhook", webhook_handler, methods=["POST"]),
        Route("/api/webhook", homepage, methods=["GET"]),
    ]
)

# Алиасы для совместимости
application = app
handler = app
