import os
import logging
from logging.handlers import RotatingFileHandler # ### НОВОЕ ###
import re
import asyncio
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


# ### НОВОЕ: Настройка логирования в файл и консоль ###
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Настройка обработчика для записи в файл (bot.log)
# Файл будет иметь максимальный размер 5MB и храниться будет 5 старых копий
file_handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)

# Настройка обработчика для вывода в консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

# Получаем корневой логгер и добавляем ему обработчики
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# Избегаем дублирования, если обработчики уже были добавлены
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
# ### КОНЕЦ НОВОГО БЛОКА ###


# --- Состояния для автомониторинга (чтобы не спамить) ---
server_unreachable = False
threshold_alerts = {
    "cpu": False,
    "ram": False,
    "disk": False,
}

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
        
        # Используем ключ для аутентификации
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
            return f"Ошибка выполнения команды:\n<pre>{error}</pre>"
        return output
    except Exception as e:
        logger.error(f"SSH connection or command failed: {e}")
        return f"🚨 Не удалось подключиться к серверу {SSH_HOST} или выполнить команду. Ошибка: {e}"

# --- Команды бота ---
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет приветственное сообщение и клавиатуру с командами."""
    keyboard = [
        [KeyboardButton("📊 Ресурсы"), KeyboardButton("💾 Диски")],
        [KeyboardButton("ℹ️ Инфо о сервере"), KeyboardButton("🌐 SpeedTest")],
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
    if text == "📊 Ресурсы":
        await get_resources(update, context)
    elif text == "💾 Диски":
        await get_disk_space(update, context)
    elif text == "ℹ️ Инфо о сервере":
        await get_server_info(update, context)
    elif text == "🌐 SpeedTest":
        await run_speedtest(update, context)
    elif text == "🔌 Сеть":
        await get_network_info(update, context)
    elif text == "📜 Логи (/logs)":
         await update.message.reply_text("Используйте: `/logs [путь_к_логу]`, например, `/logs /var/log/syslog`")
    elif text == "⚙️ Рестарт (/restart)":
         await update.message.reply_text("Используйте: `/restart [служба]`, например, `/restart nginx`\n\n**ВНИМАНИЕ:** Требуются права sudo без пароля для пользователя SSH на Сервере Б.")


@admin_only
async def ping_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная PING-проверка."""
    await update.message.reply_text("🏓 Понг! Бот активен. Проверяю доступность сервера...")
    response = await execute_ssh_command("echo 'OK'")
    if "OK" in response:
        await update.message.reply_text(f"✅ Сервер {SSH_HOST} доступен.")
    else:
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)


@admin_only
async def get_resources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает загрузку CPU и RAM."""
    await update.message.reply_text("⏳ Получаю данные о ресурсах...")
    # Команда для RAM: free -h, берем строку 'Mem'
    # Команда для CPU: uptime, берем load average
    command = "free -h | grep 'Mem:' && uptime"
    output = await execute_ssh_command(command)
    
    try:
        # Парсим вывод
        mem_line, uptime_line = output.split('\n')
        
        mem_stats = re.search(r'Mem:\s+([\d,.]+\w)\s+([\d,.]+\w)\s+([\d,.]+\w)', mem_line)
        total_mem, used_mem, free_mem = mem_stats.groups()
        
        load_avg = uptime_line.split('load average:')[1].strip()

        response = (
            f"📊 **Использование ресурсов**\n\n"
            f"💻 **CPU Load Average**\n`{load_avg}`\n\n"
            f"🧠 **Оперативная память (RAM)**\n"
            f"Всего: `{total_mem}`\n"
            f"Использовано: `{used_mem}`\n"
            f"Свободно: `{free_mem}`"
        )
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
    except (AttributeError, IndexError, ValueError):
        await update.message.reply_text(f"Не удалось разобрать ответ от сервера:\n<pre>{output}</pre>", parse_mode=ParseMode.HTML)


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
            f"Имя хоста: `{hostname}`\n"
            f"Версия ОС: `{os_version}`\n"
            f"Аптайм: `{uptime}`"
        )
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text(f"Не удалось разобрать ответ от сервера:\n<pre>{output}</pre>", parse_mode=ParseMode.HTML)


@admin_only
async def run_speedtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает SpeedTest."""
    await update.message.reply_text("🚀 Запускаю SpeedTest... Это может занять до минуты.")
    # Убедитесь, что на сервере Б установлен speedtest-cli: apt install speedtest-cli
    command = "speedtest-cli --simple"
    output = await execute_ssh_command(command)
    response = f"🌐 **Результат SpeedTest**\n\n<pre>{output}</pre>"
    await update.message.reply_text(response, parse_mode=ParseMode.HTML)


@admin_only
async def restart_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перезапускает указанную службу."""
    service_name = " ".join(context.args)
    if not service_name:
        await update.message.reply_text("⚠️ Укажите имя службы. Пример: `/restart nginx`")
        return

    # ВАЖНО: для этой команды у SSH пользователя должны быть права sudo без пароля
    await update.message.reply_text(f"⚙️ Пытаюсь перезапустить службу `{service_name}`...")
    command = f"sudo systemctl restart {service_name} && echo 'OK'"
    output = await execute_ssh_command(command)

    if "OK" in output:
        response = f"✅ Служба `{service_name}` успешно перезапущена."
    else:
        response = f"❌ Не удалось перезапустить службу `{service_name}`.\n\n<pre>{output}</pre>"
    
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает последние 30 строк лог-файла."""
    log_path = " ".join(context.args)
    if not log_path:
        await update.message.reply_text("⚠️ Укажите путь к лог-файлу. Пример: `/logs /var/log/syslog`")
        return

    await update.message.reply_text(f"📜 Получаю последние 30 строк из `{log_path}`...")
    command = f"tail -n 30 {log_path}"
    output = await execute_ssh_command(command)
    if not output:
        output = "(файл пуст или не существует)"
    
    response = f"📜 **Лог: `{log_path}`**\n\n<pre>{output}</pre>"
    # Разбиваем сообщение, если оно слишком длинное для Telegram
    if len(response) > 4096:
        for x in range(0, len(response), 4096):
            await update.message.reply_text(response[x:x+4096], parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)

@admin_only
async def get_network_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает активные сетевые подключения и порты."""
    await update.message.reply_text("⏳ Получаю сетевую информацию...")
    # ss более современная утилита, чем netstat
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
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"✅ Восстановлено соединение с сервером {SSH_HOST}!"
            )
            server_unreachable = False
        logger.info("Availability check: Server is UP.")
    
    except Exception as e:
        if not server_unreachable:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"🚨 ВНИМАНИЕ! Сервер {SSH_HOST} недоступен! Ошибка: {e}"
            )
            server_unreachable = True
        logger.error(f"Availability check: Server is DOWN. Error: {e}")

async def check_thresholds(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет пороговые значения ресурсов."""
    global threshold_alerts
    if server_unreachable:
        return # Не проверяем, если сервер и так недоступен

    # 1. Проверка CPU
    cpu_cmd = "top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1}'"
    cpu_usage_str = await execute_ssh_command(cpu_cmd)
    
    # 2. Проверка RAM
    ram_cmd = "free | grep Mem | awk '{print $3/$2 * 100.0}'"
    ram_usage_str = await execute_ssh_command(ram_cmd)
    
    # 3. Проверка диска (корневой раздел)
    disk_cmd = "df / | tail -n 1 | awk '{print $5}' | sed 's/%//'"
    disk_usage_str = await execute_ssh_command(disk_cmd)

    try:
        # Проверяем CPU
        cpu_usage = float(cpu_usage_str)
        if cpu_usage > CPU_THRESHOLD and not threshold_alerts["cpu"]:
            threshold_alerts["cpu"] = True
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Нагрузка CPU превысила порог: {cpu_usage:.2f}% (Порог: {CPU_THRESHOLD}%)")
        elif cpu_usage < CPU_THRESHOLD and threshold_alerts["cpu"]:
            threshold_alerts["cpu"] = False
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📉 Нагрузка CPU вернулась в норму: {cpu_usage:.2f}%")

        # Проверяем RAM
        ram_usage = float(ram_usage_str)
        if ram_usage > RAM_THRESHOLD and not threshold_alerts["ram"]:
            threshold_alerts["ram"] = True
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Использование RAM превысило порог: {ram_usage:.2f}% (Порог: {RAM_THRESHOLD}%)")
        elif ram_usage < RAM_THRESHOLD and threshold_alerts["ram"]:
            threshold_alerts["ram"] = False
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📉 Использование RAM вернулось в норму: {ram_usage:.2f}%")

        # Проверяем Диск
        disk_usage = float(disk_usage_str)
        if disk_usage > DISK_THRESHOLD and not threshold_alerts["disk"]:
            threshold_alerts["disk"] = True
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Место на диске превысило порог: {disk_usage:.2f}% (Порог: {DISK_THRESHOLD}%)")
        elif disk_usage < DISK_THRESHOLD and threshold_alerts["disk"]:
            threshold_alerts["disk"] = False
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📉 Место на диске вернулось в норму: {disk_usage:.2f}%")
            
    except (ValueError, TypeError) as e:
        logger.error(f"Could not parse threshold values. CPU: '{cpu_usage_str}', RAM: '{ram_usage_str}', Disk: '{disk_usage_str}'. Error: {e}")

def main():
    """Основная функция для запуска бота."""
    if not all([BOT_TOKEN, ADMIN_USER_ID, SSH_HOST, SSH_USER, SSH_KEY_PATH]):
        raise ValueError("Одна или несколько критически важных переменных окружения не заданы в .env!")

    application = Application.builder().token(BOT_TOKEN).build()
    
    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ping", ping_check))
    application.add_handler(CommandHandler("resources", get_resources))
    application.add_handler(CommandHandler("disk", get_disk_space))
    application.add_handler(CommandHandler("info", get_server_info))
    application.add_handler(CommandHandler("speedtest", run_speedtest))
    application.add_handler(CommandHandler("restart", restart_service))
    application.add_handler(CommandHandler("logs", view_logs))
    application.add_handler(CommandHandler("netinfo", get_network_info))
    
    # Обработчик текстовых команд с клавиатуры
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands))
    
    # --- Настройка фоновых задач ---
    job_queue = application.job_queue
    # Проверка доступности каждые 2 минуты
    job_queue.run_repeating(check_server_availability, interval=120, first=10) 
    # Проверка порогов каждые 10 минут
    job_queue.run_repeating(check_thresholds, interval=600, first=20)
    
    logger.info("Bot started...")
    application.run_polling()


if __name__ == "__main__":
    main()
        
