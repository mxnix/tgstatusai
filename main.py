import os
import logging
from logging.handlers import RotatingFileHandler
import re
import asyncio
import uuid
from functools import wraps

import paramiko
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# --- Загрузка и настройка ---
load_dotenv()

# --- Константы ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
SSH_HOST = os.getenv("SSH_HOST")
SSH_PORT = int(os.getenv("SSH_PORT", 22))
SSH_USER = os.getenv("SSH_USER")
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
CPU_THRESHOLD = float(os.getenv("CPU_THRESHOLD", 90.0))
RAM_THRESHOLD = float(os.getenv("RAM_THRESHOLD", 90.0))
DISK_THRESHOLD = float(os.getenv("DISK_THRESHOLD", 95.0))


# --- Состояния для ConversationHandler ---
RESTART_SERVICE, KILL_PROCESS_PID = range(2)

# --- Настройка логирования ---
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

# --- Декоратор для проверки прав ---
def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if str(user_id) != ADMIN_USER_ID:
            logger.warning(f"Unauthorized access denied for {user_id}.")
            if update.callback_query:
                await update.callback_query.answer("⛔️ Доступ запрещен.", show_alert=True)
            elif update.message:
                await update.message.reply_text("⛔️ Доступ запрещен.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- SSH и вспомогательные функции ---
async def execute_ssh_command(command: str) -> str:
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
        return f"🚨 Не удалось подключиться к серверу или выполнить команду. Ошибка: {e}"

# --- Клавиатуры ---
def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Дашборд", callback_data='dashboard_start'),
            InlineKeyboardButton("ℹ️ Сводка", callback_data='get_summary'),
        ],
        [InlineKeyboardButton("⚙️ Управление", callback_data='open_management_menu')],
        [
            InlineKeyboardButton("🚀 SpeedTest", callback_data='run_speedtest'),
            InlineKeyboardButton("🔌 Сеть", callback_data='get_network_info'),
        ],
    ])

def get_management_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Рестарт службы", callback_data='restart_service_prompt')],
        [InlineKeyboardButton("📜 Получить лог", callback_data='get_log_info')],
        [InlineKeyboardButton("📈 Топ процессов", callback_data='get_top_processes')],
        [InlineKeyboardButton("🔙 Назад", callback_data='main_menu')],
    ])

def get_top_processes_keyboard(back_target='open_management_menu'):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💀 Завершить процесс", callback_data='kill_process_prompt')],
        [InlineKeyboardButton("🔙 Назад", callback_data=back_target)],
    ])

def get_back_keyboard(target='main_menu'):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=target)]])

# --- Основные обработчики ---
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_jobs = context.job_queue.get_jobs_by_name(str(update.effective_user.id))
    for job in current_jobs: job.schedule_removal()
    
    await update.message.reply_text(
        "👋 **Бот для мониторинга сервера**\n\nВыберите действие:",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

@admin_only
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👋 **Бот для мониторинга сервера**\n\nВыберите действие:",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

@admin_only
async def open_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⚙️ **Меню управления**",
        reply_markup=get_management_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

# --- Дашборд ---
async def update_dashboard_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        ram_cmd = "free -b | awk 'NR==2{printf \"%.1f\", $3/$2*100}'"
        cpu_cmd = "uptime | awk -F'load average: ' '{print $2}'"
        disk_cmd = "df -h / | awk 'NR==2{print $5}'"
        
        ram_percent = float(await execute_ssh_command(ram_cmd))
        cpu_load = (await execute_ssh_command(cpu_cmd)).strip()
        disk_usage = (await execute_ssh_command(disk_cmd)).strip()

        ram_bar = f"[{'█' * int(ram_percent / 10) + '─' * (10 - int(ram_percent / 10))}] {ram_percent:.1f}%"
        
        text = (
            f"📊 **Дашборд (обновляется каждые 10 сек)**\n\n"
            f"🧠 **RAM:** {ram_bar}\n"
            f"💻 **CPU Load:** `{cpu_load}`\n"
            f"💾 **Диск (корень):** `{disk_usage}` занято"
        )
        await context.bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.data['message_id'],
            text=text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрыть", callback_data='dashboard_stop')]]),
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        if "Message is not modified" in str(e): pass
        else:
            logger.error(f"Dashboard update error: {e}")
            job.schedule_removal()
    except Exception as e:
        logger.error(f"Dashboard job failed: {e}")
        job.schedule_removal()

@admin_only
async def dashboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    current_jobs = context.job_queue.get_jobs_by_name(str(query.from_user.id))
    for job in current_jobs: job.schedule_removal()
    message = await query.edit_message_text("⏳ Запускаю дашборд...")
    context.job_queue.run_repeating(
        update_dashboard_job, interval=10, first=0.1,
        chat_id=query.from_user.id,
        data={'message_id': message.message_id},
        name=str(query.from_user.id)
    )

@admin_only
async def dashboard_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    current_jobs = context.job_queue.get_jobs_by_name(str(query.from_user.id))
    for job in current_jobs:
        job.schedule_removal()
    await query.delete_message()

# --- Сводка по серверу (Neofetch) ---
@admin_only
async def get_server_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Собираю сводку по серверу...")

    command = "cat /etc/os-release | grep PRETTY_NAME | cut -d'\"' -f2; " \
              "hostname; " \
              "uptime -p; " \
              "grep 'model name' /proc/cpuinfo | head -1 | cut -d':' -f2 | sed 's/^ *//'; " \
              "free -h | awk '/^Mem:/ {print $3\" / \"$2}'; " \
              "df -h / | awk 'NR==2 {print $3\" / \"$2\" (\"$5\")\"}'"
    output = await execute_ssh_command(command)
    
    try:
        os_name, host, uptime, cpu, ram, disk = output.split('\n')
        art = ["      .--.     ", "     |o_o |    ", "     |:_/ |    ", "    //   \ \   ", "   (|     | )  ", "  /'\_   _/`\  ", "  \___)=(___/  "]
        data = [f"OS:      {os_name}", f"Host:    {host}", f"Uptime:  {uptime}", f"CPU:     {cpu}", f"RAM:     {ram}", f"Disk:    {disk}", ""]
        result = [art[i] + data[i] for i in range(len(art))]
        formatted_output = "\n".join(result)
        
        await query.edit_message_text(f"ℹ️ **Сводка по серверу**\n\n<pre>{formatted_output}</pre>", reply_markup=get_back_keyboard(), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to create summary: {e}. Output: {output}")
        await query.edit_message_text("❌ Не удалось создать сводку. Проверьте логи.", reply_markup=get_back_keyboard())

# --- Другие команды ---
@admin_only
async def get_network_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Получаю сетевую информацию...")
    output = await execute_ssh_command("ss -tulnp")
    await query.edit_message_text(f"🔌 **Активные сетевые подключения**\n\n<pre>{output}</pre>", reply_markup=get_back_keyboard(), parse_mode=ParseMode.HTML)

@admin_only
async def run_speedtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🚀 Запускаю SpeedTest... Это может занять до минуты.")
    output = await execute_ssh_command("speedtest-cli --simple")
    try:
        ping = re.search(r"Ping: ([\d.]+) ms", output).group(1)
        download = re.search(r"Download: ([\d.]+) Mbit/s", output).group(1)
        upload = re.search(r"Upload: ([\d.]+) Mbit/s", output).group(1)
        text = f"🌐 **Результаты SpeedTest**\n\n**Ping:** `{ping} ms`\n**Download:** `↓ {download} Mbit/s`\n**Upload:** `↑ {upload} Mbit/s`"
        await query.edit_message_text(text, reply_markup=get_back_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except AttributeError:
        await query.edit_message_text(f"🌐 **Результат SpeedTest (raw)**\n\n<pre>{output}</pre>", reply_markup=get_back_keyboard(), parse_mode=ParseMode.HTML)

# --- Управление процессами ---
@admin_only
async def get_top_processes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Получаю список процессов...")
    command = "ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -n 11"
    output = await execute_ssh_command(command)
    await query.edit_message_text(f"📈 **Топ процессов по CPU**\n\n<pre>{output}</pre>", reply_markup=get_top_processes_keyboard(), parse_mode=ParseMode.HTML)

async def kill_process_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("💀 **Завершение процесса**\n\nВведите PID процесса, который нужно завершить.", reply_markup=get_back_keyboard('open_management_menu'))
    return KILL_PROCESS_PID

async def kill_process_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = update.message.text
    if not pid.isdigit():
        await update.message.reply_text("Это не похоже на PID. Введите только цифры.", reply_markup=get_back_keyboard('open_management_menu'))
        return KILL_PROCESS_PID
    context.user_data['pid_to_kill'] = pid
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Да, завершить PID {pid}", callback_data=f'kill_process_yes')], [InlineKeyboardButton("❌ Отмена", callback_data='open_management_menu')]])
    await update.message.reply_text(f"Вы уверены, что хотите завершить процесс с PID `{pid}`?", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def kill_process_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pid = context.user_data.get('pid_to_kill')
    await query.answer()
    await query.edit_message_text(f"⏳ Завершаю процесс {pid}...")
    output = await execute_ssh_command(f"kill {pid} && echo 'OK'")
    text = f"✅ Процесс с PID `{pid}` успешно завершен." if "OK" in output else f"❌ Не удалось завершить процесс `{pid}`.\n<pre>{output}</pre>"
    await query.edit_message_text(text, reply_markup=get_back_keyboard('open_management_menu'), parse_mode=ParseMode.HTML)

# --- Логи и Рестарт ---
@admin_only
async def get_log_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📜 **Получение лога**\n\n"
        "Используйте команду `/logs`, чтобы получить файл с логами.\n\n"
        "**Формат:**\n`/logs [кол-во строк] [путь к файлу]`\n\n"
        "**Примеры:**\n`/logs 500 /var/log/nginx/access.log`\n"
        "`/logs /var/log/syslog` (вернет 200 строк по умолчанию)",
        reply_markup=get_back_keyboard('open_management_menu'),
        parse_mode=ParseMode.MARKDOWN
    )

@admin_only
async def view_logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    lines = 200
    log_path = ""

    if len(args) == 1:
        log_path = args[0]
    elif len(args) >= 2 and args[0].isdigit():
        lines = int(args[0])
        log_path = " ".join(args[1:])
    else:
        await update.message.reply_text(
            "⚠️ **Неверный формат команды.**\n\n"
            "**Формат:** `/logs [кол-во строк] [путь]`\n"
            "**Пример:** `/logs 500 /var/log/syslog`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    msg = await update.message.reply_text(f"⏳ Собираю последние {lines} строк из `{log_path}`...", parse_mode=ParseMode.MARKDOWN)
    output = await execute_ssh_command(f"tail -n {lines} {log_path}")

    if "Ошибка" in output or "No such file" in output or not output:
        await msg.edit_text(f"❌ Не удалось получить лог.\n`{output}`", parse_mode=ParseMode.MARKDOWN)
        return
    
    temp_filename = ""
    try:
        temp_filename = f"{os.path.basename(log_path)}_{uuid.uuid4()}.log"
        with open(temp_filename, "w", encoding="utf-8") as f: f.write(output)
        with open(temp_filename, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                caption=f"📋 Вот последние {lines} строк из лога `{log_path}`",
                parse_mode=ParseMode.MARKDOWN
            )
        await msg.delete()
    finally:
        if temp_filename and os.path.exists(temp_filename): os.remove(temp_filename)

async def restart_service_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔄 **Перезапуск службы**\n\nВведите имя службы (например, `nginx`).", reply_markup=get_back_keyboard('open_management_menu'))
    return RESTART_SERVICE

async def restart_service_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service_name = update.message.text.strip()
    context.user_data['service_to_restart'] = service_name
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Да, перезапустить {service_name}", callback_data=f'restart_service_yes')], [InlineKeyboardButton("❌ Отмена", callback_data='open_management_menu')]])
    await update.message.reply_text(f"Вы уверены, что хотите перезапустить службу `{service_name}`?", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def restart_service_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    service_name = context.user_data.get('service_to_restart')
    await query.answer()
    await query.edit_message_text(f"⏳ Перезапускаю службу `{service_name}`...", parse_mode=ParseMode.MARKDOWN)
    output = await execute_ssh_command(f"sudo systemctl restart {service_name} && echo 'OK'")
    text = f"✅ Служба `{service_name}` успешно перезапущена." if "OK" in output else f"❌ Не удалось перезапустить службу `{service_name}`.\n<pre>{output}</pre>"
    await query.edit_message_text(text, reply_markup=get_back_keyboard('open_management_menu'), parse_mode=ParseMode.HTML)

# --- Фоновые задачи (Автомониторинг) ---
# Эти функции не вызываются напрямую, а работают в фоне через job_queue
async def check_server_availability(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет доступность сервера по SSH."""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        private_key = paramiko.RSAKey.from_private_key_file(SSH_KEY_PATH)
        await asyncio.to_thread(
             ssh.connect, SSH_HOST, port=SSH_PORT, username=SSH_USER, pkey=private_key, timeout=10
        )
        ssh.close()
        logger.info("Availability check: Server is UP.")
    except Exception as e:
        logger.error(f"Availability check: Server is DOWN. Error: {e}")
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"🚨 ВНИМАНИЕ! Сервер {SSH_HOST} недоступен! Ошибка: {e}")

async def check_thresholds(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет пороговые значения ресурсов."""
    try:
        cpu_cmd = "top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1}'"
        ram_cmd = "free | grep Mem | awk '{print $3/$2 * 100.0}'"
        disk_cmd = "df / | tail -n 1 | awk '{print $5}' | sed 's/%//'"
        
        cpu_usage = float(await execute_ssh_command(cpu_cmd))
        if cpu_usage > CPU_THRESHOLD:
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Нагрузка CPU превысила порог: {cpu_usage:.2f}% (Порог: {CPU_THRESHOLD}%)")

        ram_usage = float(await execute_ssh_command(ram_cmd))
        if ram_usage > RAM_THRESHOLD:
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Использование RAM превысило порог: {ram_usage:.2f}% (Порог: {RAM_THRESHOLD}%)")

        disk_usage = float(await execute_ssh_command(disk_cmd))
        if disk_usage > DISK_THRESHOLD:
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📈 ВНИМАНИЕ! Место на диске превысило порог: {disk_usage:.2f}% (Порог: {DISK_THRESHOLD}%)")
    except Exception as e:
        logger.error(f"Could not check thresholds. Error: {e}")


# --- Основная функция ---
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Handlers ---
    conv_handlers = {
        "restart": ConversationHandler(entry_points=[CallbackQueryHandler(restart_service_prompt, '^restart_service_prompt$')], states={RESTART_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, restart_service_confirm)]}, fallbacks=[CallbackQueryHandler(open_management_menu, '^open_management_menu$')]),
        "kill": ConversationHandler(entry_points=[CallbackQueryHandler(kill_process_prompt, '^kill_process_prompt$')], states={KILL_PROCESS_PID: [MessageHandler(filters.TEXT & ~filters.COMMAND, kill_process_confirm)]}, fallbacks=[CallbackQueryHandler(open_management_menu, '^open_management_menu$')]),
    }
    callback_handlers = {
        '^main_menu$': main_menu,
        '^open_management_menu$': open_management_menu,
        '^dashboard_start$': dashboard_start,
        '^dashboard_stop$': dashboard_stop,
        '^get_summary$': get_server_summary,
        '^get_network_info$': get_network_info,
        '^run_speedtest$': run_speedtest,
        '^get_top_processes$': get_top_processes,
        '^get_log_info$': get_log_info,
        '^restart_service_yes$': restart_service_execute,
        '^kill_process_yes$': kill_process_execute,
    }
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("logs", view_logs_command))

    for pattern, handler in callback_handlers.items():
        application.add_handler(CallbackQueryHandler(handler, pattern=pattern))
    for handler in conv_handlers.values():
        application.add_handler(handler)

    # --- Настройка фоновых задач ---
    job_queue = application.job_queue
    job_queue.run_repeating(check_server_availability, interval=120, first=15) 
    job_queue.run_repeating(check_thresholds, interval=600, first=30)

    logger.info("Bot started with new interactive UI and background monitoring...")
    application.run_polling()

if __name__ == "__main__":
    main()
