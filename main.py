import os
import logging
from logging.handlers import RotatingFileHandler
import re
import asyncio
import uuid
from functools import wraps

import paramiko
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

# Загрузка переменных окружения из .env файла
load_dotenv()

# --- Константы из .env ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
SSH_HOST = os.getenv("SSH_HOST")
SSH_PORT = int(os.getenv("SSH_PORT", 22))
SSH_USER = os.getenv("SSH_USER")
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
CPU_THRESHOLD = float(os.getenv("CPU_THRESHOLD", 90.0))
RAM_THRESHOLD = float(os.getenv("RAM_THRESHOLD", 90.0))
DISK_THRESHOLD = float(os.getenv("DISK_THRESHOLD", 95.0))

# --- Настройка логирования в файл и консоль ---
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# Обработчик для записи в файл (bot.log), с ротацией
file_handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)
# Обработчик для вывода в консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
# Получаем корневой логгер и добавляем ему обработчики
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

# --- Состояния для автомониторинга ---
server_unreachable = False
threshold_alerts = {"cpu": False, "ram": False, "disk": False}

# --- Декоратор для проверки прав администратора ---
def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if str(update.effective_user.id) != ADMIN_USER_ID:
            logger.warning(f"Unauthorized access denied for {update.effective_user.id}.")
            await update.message.reply_text("⛔️ Доступ запрещен.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Основная функция для выполнения SSH команд ---
async def execute_ssh_command(command: str) -> str:
    """Подключается к серверу Б по SSH и выполняет команду."""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        private_key = paramiko.RSAKey.from_private_key_file(SSH_KEY_PATH)
        await asyncio.to_thread(
            ssh.connect, SSH_HOST, port=SSH_PORT, username=SSH_USER, pkey=private_key, timeout=15
        )
        stdin, stdout, stderr = ssh.exec_command(command, timeout=30)
        output = stdout.read().decode('utf-8').strip()
        error = stderr.read().decode('utf-8').strip()
        ssh.close()
        if error:
            logger.error(f"SSH command error for '{command}': {error}")
            return f"Ошибка выполнения команды: {error}"
        return output
    except Exception as e:
        logger.error(f"SSH connection or command failed: {e}")
        return f"🚨 Не удалось подключиться к серверу {SSH_HOST} или выполнить команду. Ошибка: {e}"

# --- Вспомогательные функции и команды бота ---
def create_progress_bar(percentage: float, length: int = 10) -> str:
    """Создает текстовый прогресс-бар. Пример: [█████-----] 50.0% """
    if not 0 <= percentage <= 100:
        percentage = 0
    filled_length = int(length * percentage // 100)
    bar = '█' * filled_length + '─' * (length - filled_length)
    return f"[{bar}] {percentage:.1f}%"

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет приветственное сообщение и клавиатуру с командами."""
    keyboard = [
        [KeyboardButton("📊 Ресурсы"), KeyboardButton("💾 Диски")],
        [KeyboardButton("ℹ️ Инфо о сервере"), KeyboardButton("🚀 SpeedTest")],
        [KeyboardButton("🔌 Сеть"), KeyboardButton("📜 Логи (/logs)"), KeyboardButton("⚙️ Рестарт (/restart)")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "👋 Привет! Я ваш бот для мониторинга сервера.\n"
        "Выберите команду на клавиатуре или введите вручную.",
        reply_markup=reply_markup
    )

@admin_only
async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые команды с клавиатуры."""
    text = update.message.text
    action_map = {
        "📊 Ресурсы": get_resources,
        "💾 Диски": get_disk_space,
        "ℹ️ Инфо о сервере": get_server_info,
        "🚀 SpeedTest": run_speedtest,
        "🔌 Сеть": get_network_info,
    }
    if text in action_map:
        await action_map[text](update, context)
    elif text == "📜 Логи (/logs)":
         await update.message.reply_text("Используйте: `/logs [путь_к_логу]`", parse_mode=ParseMode.MARKDOWN)
    elif text == "⚙️ Рестарт (/restart)":
         await update.message.reply_text("Используйте: `/restart [служба]`", parse_mode=ParseMode.MARKDOWN)

@admin_only
async def ping_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная PING-проверка."""
    await update.message.reply_text("🏓 Понг! Бот активен. Проверяю доступность сервера...")
    response = await execute_ssh_command("echo 'OK'")
    if "OK" in response:
        await update.message.reply_text(f"✅ Сервер **{SSH_HOST}** доступен.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)

@admin_only
async def get_resources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает загрузку CPU и RAM с красивыми индикаторами."""
    await update.message.reply_text("⏳ Собираю данные о ресурсах...")
    ram_cmd = "free | awk 'NR==2{printf \"%.1f\", $3/$2*100}'"
    cpu_cmd = "uptime | awk -F'load average: ' '{print $2}'"
    ram_percent_str = await execute_ssh_command(ram_cmd)
    cpu_load_avg = await execute_ssh_command(cpu_cmd)
    try:
        ram_percent = float(ram_percent_str)
        ram_bar = create_progress_bar(ram_percent)
        response = (
            f"📊 **Использование ресурсов**\n\n"
            f"🧠 **RAM:** {ram_bar}\n"
            f"💻 **CPU Load Average:** `{cpu_load_avg.strip()}`"
        )
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
    except (ValueError, TypeError) as e:
        logger.error(f"Failed to parse resources. RAM: '{ram_percent_str}', CPU: '{cpu_load_avg}'. Error: {e}")
        await update.message.reply_text("❌ Не удалось разобрать данные о ресурсах с сервера.")

@admin_only
async def get_disk_space(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает место на дисках."""
    await update.message.reply_text("⏳ Получаю данные о дисках...")
    command = "df -h"
    output = await execute_ssh_command(command)
    response = f"💾 **Место на дисках**\n\n<pre>{output}</pre>"
    await update.message.reply_text(response, parse_mode=ParseMode.HTML)

@admin_only
async def get_server_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит общую информацию о сервере."""
    await update.message.reply_text("⏳ Получаю информацию о сервере...")
    command = "hostname && lsb_release -d -s && uptime -p"
    output = await execute_ssh_command(command)
    try:
        hostname, os_version, uptime = output.split('\n')
        response = (
            f"ℹ️ **Информация о сервере**\n\n"
            f"Сервер: `{hostname}`\n"
            f"ОС: `{os_version}`\n"
            f"Аптайм: `{uptime}`"
        )
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text(f"Не удалось разобрать ответ от сервера:\n<pre>{output}</pre>", parse_mode=ParseMode.HTML)

@admin_only
async def run_speedtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает SpeedTest и выводит результат в красивом виде."""
    await update.message.reply_text("🚀 Запускаю SpeedTest... Это может занять до минуты.")
    command = "speedtest-cli --simple"
    output = await execute_ssh_command(command)
    try:
        ping = re.search(r"Ping: ([\d.]+) ms", output).group(1)
        download = re.search(r"Download: ([\d.]+) Mbit/s", output).group(1)
        upload = re.search(r"Upload: ([\d.]+) Mbit/s", output).group(1)
        response = (
            f"🌐 **Результаты SpeedTest**\n\n"
            f"**Ping:** `{ping} ms`\n"
            f"**Download:** `↓ {download} Mbit/s`\n"
            f"**Upload:** `↑ {upload} Mbit/s`"
        )
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
    except AttributeError:
        logger.warning(f"Could not parse SpeedTest output. Sending raw. Output: {output}")
        response = f"🌐 **Результат SpeedTest (raw)**\n\n<pre>{output}</pre>"
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)

@admin_only
async def restart_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перезапускает указанную службу."""
    service_name = " ".join(context.args)
    if not service_name:
        await update.message.reply_text("⚠️ Укажите имя службы. Пример: `/restart nginx`")
        return
    await update.message.reply_text(f"⚙️ Пытаюсь перезапустить службу `{service_name}`...")
    command = f"sudo systemctl restart {service_name} && echo 'OK'"
    output = await execute_ssh_command(command)
    if "OK" in output:
        response = f"✅ Служба `{service_name}` успешно перезапущена."
    else:
        response = f"❌ Не удалось перезапустить службу `{service_name}`.\n\n<pre>{output}</pre>"
    await update.message.reply_text(response, parse_mode=ParseMode.HTML)

@admin_only
async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Собирает лог, упаковывает в файл и отправляет его."""
    log_path = " ".join(context.args)
    if not log_path:
        await update.message.reply_text("⚠️ Укажите путь к лог-файлу.\n*Пример:* `/logs /var/log/syslog`", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(f"⏳ Собираю лог `{log_path}` и готовлю файл...", parse_mode=ParseMode.MARKDOWN)
    command = f"tail -n 200 {log_path}"
    output = await execute_ssh_command(command)
    if "Ошибка выполнения" in output or not output or "No such file" in output:
        await update.message.reply_text(f"❌ Не удалось получить лог.\nСервер ответил: `{output}`", parse_mode=ParseMode.MARKDOWN)
        return
    temp_filename = ""
    try:
        temp_filename = f"{os.path.basename(log_path)}_{uuid.uuid4()}.log"
        with open(temp_filename, "w", encoding="utf-8") as log_file:
            log_file.write(output)
        with open(temp_filename, "rb") as log_file_to_send:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=log_file_to_send,
                filename=f"{os.path.basename(log_path)}.log",
                caption=f"📋 Вот последние 200 строк из лога `{log_path}`",
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error(f"Failed to send log file: {e}")
        await update.message.reply_text(f"❌ Произошла ошибка при создании или отправке файла лога: {e}")
    finally:
        if temp_filename and os.path.exists(temp_filename):
            os.remove(temp_filename)

@admin_only
async def get_network_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает активные сетевые подключения и порты."""
    await update.message.reply_text("⏳ Получаю сетевую информацию...")
    command = "ss -tulnp"
    output = await execute_ssh_command(command)
    response = f"🔌 **Активные сетевые подключения (TCP/UDP)**\n\n<pre>{output}</pre>"
    await update.message.reply_text(response, parse_mode=ParseMode.HTML)

# --- Фоновые задачи (Автомониторинг) ---
async def check_server_availability(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет доступность сервера по SSH."""
    global server_unreachable
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        private_key = paramiko.RSAKey.from_private_key_file(SSH_KEY_PATH)
        await asyncio.to_thread(
             ssh.connect, SSH_HOST, port=SSH_PORT, username=SSH_USER, pkey=private_key, timeout=10
        )
        ssh.close()
        if server_unreachable:
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"✅ Восстановлено соединение с сервером {SSH_HOST}!")
            server_unreachable = False
        logger.info("Availability check: Server is UP.")
    except Exception as e:
        if not server_unreachable:
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"🚨 ВНИМАНИЕ! Сервер {SSH_HOST} недоступен! Ошибка: {e}")
            server_unreachable = True
        logger.error(f"Availability check: Server is DOWN. Error: {e}")

async def check_thresholds(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет пороговые значения ресурсов."""
    global threshold_alerts
    if server_unreachable:
        return
    # Проверка CPU, RAM, Disk
    cpu_cmd = "top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1}'"
    ram_cmd = "free | grep Mem | awk '{print $3/$2 * 100.0}'"
    disk_cmd = "df / | tail -n 1 | awk '{print $5}' | sed 's/%//'"
    try:
        cpu_usage = float(await execute_ssh_command(cpu_cmd))
        if cpu_usage > CPU_THRESHOLD and not threshold_alerts["cpu"]:
            threshold_alerts["cpu"] = True
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Нагрузка CPU превысила порог: {cpu_usage:.2f}% (Порог: {CPU_THRESHOLD}%)")
        elif cpu_usage < CPU_THRESHOLD and threshold_alerts["cpu"]:
            threshold_alerts["cpu"] = False
        
        ram_usage = float(await execute_ssh_command(ram_cmd))
        if ram_usage > RAM_THRESHOLD and not threshold_alerts["ram"]:
            threshold_alerts["ram"] = True
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Использование RAM превысило порог: {ram_usage:.2f}% (Порог: {RAM_THRESHOLD}%)")
        elif ram_usage < RAM_THRESHOLD and threshold_alerts["ram"]:
            threshold_alerts["ram"] = False
        
        disk_usage = float(await execute_ssh_command(disk_cmd))
        if disk_usage > DISK_THRESHOLD and not threshold_alerts["disk"]:
            threshold_alerts["disk"] = True
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Место на диске превысило порог: {disk_usage:.2f}% (Порог: {DISK_THRESHOLD}%)")
        elif disk_usage < DISK_THRESHOLD and threshold_alerts["disk"]:
            threshold_alerts["disk"] = False
    except (ValueError, TypeError) as e:
        logger.error(f"Could not parse threshold values. Error: {e}")

def main():
    """Основная функция для запуска бота."""
    if not all([BOT_TOKEN, ADMIN_USER_ID, SSH_HOST, SSH_USER, SSH_KEY_PATH]):
        raise ValueError("Одна или несколько критически важных переменных окружения не заданы в .env!")
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Добавляем обработчики команд
    handlers = [
        CommandHandler("start", start),
        CommandHandler("ping", ping_check),
        CommandHandler("resources", get_resources),
        CommandHandler("disk", get_disk_space),
        CommandHandler("info", get_server_info),
        CommandHandler("speedtest", run_speedtest),
        CommandHandler("restart", restart_service),
        CommandHandler("logs", view_logs),
        CommandHandler("netinfo", get_network_info),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands)
    ]
    application.add_handlers(handlers)
    
    # Настройка фоновых задач
    job_queue = application.job_queue
    job_queue.run_repeating(check_server_availability, interval=120, first=10) 
    job_queue.run_repeating(check_thresholds, interval=600, first=20)
    
    logger.info("Bot started...")
    application.run_polling()

if __name__ == "__main__":
    main()
        
