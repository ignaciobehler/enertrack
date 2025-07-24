import os
import threading
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import asyncio
import mysql.connector

# Configuración de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Obtener el token del bot desde la variable de entorno
TELEGRAM_TOKEN = os.environ.get('enertrackBotToken')
# URL base de la API Flask (ajustar si es necesario)
API_BASE_URL = os.environ.get('ENERTRACK_API_URL', 'https://ignaciobehler.duckdns.org:23405/enertrack')

# Diccionario temporal para mapear códigos de vinculación a usuario_id
# En producción, deberías almacenar esto en la base de datos o en caché persistente
pending_links = {}

# Configuración de la base de datos para verificación directa
MYSQL_HOST = os.environ.get('MYSQL_HOST', 'localhost')
MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', '')
MYSQL_DB = os.environ.get('MYSQL_DB', 'medidoresEnergia')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler para /start. Si el usuario ya está vinculado, responde siempre 'Tu cuenta ya está vinculada con Telegram.'
    Si no está vinculado, permite la vinculación con código.
    """
    args = context.args
    chat_id = update.effective_chat.id
    # Verificar si el chat_id ya está vinculado directamente en la base de datos
    try:
        conn = mysql.connector.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DB
        )
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT usuario_id FROM Usuarios WHERE telegram_chat_id = %s", (chat_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            await update.message.reply_text("Tu cuenta ya está vinculada con Telegram.")
            return
    except Exception as e:
        logging.error(f"Error al verificar vinculación en la base de datos: {e}")
        await update.message.reply_text("Ocurrió un error al verificar el estado de vinculación. Intenta nuevamente más tarde.")
        return
    # Si no está vinculado
    if not args:
        await update.message.reply_text(
            "Para vincular tu cuenta, vuelve a la página de Enertrack y haz clic en el botón de vincular con Telegram."
        )
        return
    # Si el usuario envía /start <codigo>, intentar vincular
    code = args[0]
    try:
        response = requests.post(f"{API_BASE_URL}/api/telegram/vincular", json={"code": code, "chat_id": chat_id})
        try:
            data = response.json()
        except Exception as e:
            logging.error(f"[TELEGRAM] Respuesta no JSON: {response.text}")
            await update.message.reply_text("Ocurrió un error inesperado al vincular tu cuenta. Intenta nuevamente más tarde.")
            return
        if response.status_code == 200:
            await update.message.reply_text("¡Cuenta vinculada con éxito! Ahora recibirás alertas.")
            if code in pending_links:
                del pending_links[code]
        else:
            await update.message.reply_text(data.get("error", "Error al vincular tu cuenta. Intenta nuevamente más tarde."))
    except Exception as e:
        logging.error(f"Error al vincular chat_id: {e}")
        await update.message.reply_text("Ocurrió un error al vincular tu cuenta. Intenta nuevamente más tarde.")

# Función para verificar si el chat_id está autorizado

def check_user_authorized(chat_id):
    try:
        conn = mysql.connector.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DB
        )
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT usuario_id FROM Usuarios WHERE telegram_chat_id = %s", (chat_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row)
    except Exception as e:
        logging.error(f"Error al verificar autorización de usuario: {e}")
        return False

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not check_user_authorized(chat_id):
        await update.message.reply_text("No tienes permiso para usar este bot.")
        return
    texto = (
        "¡Bienvenido a Enertrack!\n\n"
        "Este bot te permite recibir alertas sobre el consumo de tus nodos directamente en Telegram. "
        "Recibirás notificaciones automáticas cuando se detecten eventos importantes relacionados con el consumo energético de tus dispositivos."
    )
    await update.message.reply_text(texto)

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not check_user_authorized(chat_id):
        await update.message.reply_text("No tienes permiso para usar este bot.")
        return
    # Solo manejar mensajes de texto que no sean comandos
    code = update.message.text.strip()
    if not code or code.startswith('/'):
        return  # Ignorar comandos
    try:
        response = requests.post(f"{API_BASE_URL}/api/telegram/vincular", json={"code": code, "chat_id": chat_id})
        try:
            data = response.json()
        except Exception as e:
            logging.error(f"[TELEGRAM] Respuesta no JSON: {response.text}")
            await update.message.reply_text("Ocurrió un error inesperado al vincular tu cuenta. Intenta nuevamente más tarde.")
            return
        if response.status_code == 200:
            await update.message.reply_text("¡Cuenta vinculada con éxito! Ahora recibirás alertas.")
            if code in pending_links:
                del pending_links[code]
        else:
            await update.message.reply_text(data.get("error", "El código de vinculación es inválido o ha expirado. Por favor, vuelve a la web para generar uno nuevo."))
    except Exception as e:
        logging.error(f"Error al vincular chat_id: {e}")
        await update.message.reply_text("Ocurrió un error al vincular tu cuenta. Intenta nuevamente más tarde.")

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_code))
    # Dejar solo /help en el menú de comandos
    from telegram import BotCommand
    async def set_commands():
        commands = [
            BotCommand("help", "Ayuda y descripción del bot"),
        ]
        await application.bot.set_my_commands(commands)
    loop.run_until_complete(set_commands())
    application.run_polling(stop_signals=None)

def start_bot_in_thread():
    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()

# Para pruebas locales, descomentar:
# if __name__ == "__main__":
#     run_bot() 