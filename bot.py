import os
from flask import Flask, request
from threading import Thread
import urllib.request
import urllib.parse
import json
import time
import sqlite3
from datetime import datetime
import pytz
import logging
import hashlib
import secrets
from functools import wraps
import re

# ========== ОБЯЗАТЕЛЬНО ДОБАВИТЬ В НАЧАЛО ==========
# Эта строка читает порт, который дает Render
PORT = int(os.environ.get('PORT', 10000))

# ========== КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bot_database.db")

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])

@app.route('/')
def home():
    return "🤖 Бот управления статусами работает!", 200
    
app.secret_key = SECRET_KEY

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
bot_start_time = time.time()
bot_enabled = True
bot_disable_reason = ""
user_states = {}
admin_sessions = {}

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            group_id INTEGER,
            thread_id INTEGER,
            message_id INTEGER,
            group_name TEXT,
            timezone TEXT DEFAULT 'Asia/Yekaterinburg',
            server_info TEXT DEFAULT 'Сервер',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS server_statuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id INTEGER,
            target_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

init_db()

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ========== БЕЗОПАСНОСТЬ ==========
def validate_input(text, max_length=1000):
    if not text or len(text) > max_length:
        return False
    return True

def is_admin_authenticated(user_id):
    return admin_sessions.get(user_id, False)

def authenticate_admin(user_id, password):
    if user_id != ADMIN_USER_ID:
        return False
    
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    if password_hash == ADMIN_PASSWORD_HASH:
        admin_sessions[user_id] = True
        logger.info(f"✅ Админ {user_id} авторизовался")
        return True
    
    logger.warning(f"❌ Неудачная попытка входа админа {user_id}")
    return False

def logout_admin(user_id):
    if user_id in admin_sessions:
        del admin_sessions[user_id]

# ========== ОСНОВНЫЕ ФУНКЦИИ ==========
def get_user_timezone(user_id):
    conn = get_db_connection()
    user = conn.execute('SELECT timezone FROM users WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    return user['timezone'] if user else 'Asia/Yekaterinburg'

def get_user_server_info(user_id):
    conn = get_db_connection()
    user = conn.execute('SELECT server_info FROM users WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    return user['server_info'] if user else 'Сервер'

def get_current_time(user_id=None):
    timezone_str = get_user_timezone(user_id) if user_id else 'Asia/Yekaterinburg'
    try:
        tz = pytz.timezone(timezone_str)
        return datetime.now(tz).strftime("%H:%M:%S %d.%m.%Y")
    except:
        return datetime.now().strftime("%H:%M:%S %d.%m.%Y")

def safe_request(url, data=None, method="GET", timeout=8):
    try:
        if data and method == "POST":
            data_str = json.dumps(data, ensure_ascii=False)
            data_bytes = data_str.encode('utf-8')
            req = urllib.request.Request(
                url, 
                data=data_bytes,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
        else:
            req = urllib.request.Request(url)
        
        response = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(response.read().decode())
        return result
        
    except Exception as e:
        logger.error(f"Ошибка запроса: {e}")
        return None

def send_message(chat_id, text, buttons=None, parse_mode="HTML", thread_id=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    
    if thread_id:
        payload["message_thread_id"] = thread_id
    
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    
    result = safe_request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        payload,
        "POST"
    )
    return result

def edit_message(chat_id, message_id, text, buttons=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode
    }
    
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    
    result = safe_request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
        payload,
        "POST"
    )
    return result and result.get('ok')

def answer_callback(callback_id):
    safe_request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
        {"callback_query_id": callback_id},
        "POST"
    )

# ========== РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ==========
def setup_user_settings(user_id, group_id, thread_id, message_id, group_name, server_info="Сервер"):
    conn = get_db_connection()
    conn.execute('''
        INSERT OR REPLACE INTO users (user_id, group_id, thread_id, message_id, group_name, server_info)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, group_id, thread_id, message_id, group_name, server_info))
    conn.commit()
    conn.close()

def reset_user_settings(user_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM server_statuses WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM subscriptions WHERE subscriber_id = ? OR target_user_id = ?', (user_id, user_id))
    conn.commit()
    conn.close()
    
    if user_id in user_states:
        del user_states[user_id]
    
    logout_admin(user_id)
    logger.info(f"🔄 Настройки пользователя {user_id} сброшены")

def send_new_status_message(user_id, status_text):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
    
    if not user:
        conn.close()
        return False
    
    result = send_message(
        user['group_id'], 
        status_text,
        thread_id=user['thread_id'] if user['thread_id'] else None
    )
    
    if result and result.get('ok'):
        new_message_id = result["result"]["message_id"]
        conn.execute('UPDATE users SET message_id = ? WHERE user_id = ?', (new_message_id, user_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ Создано новое сообщение: {new_message_id}")
        return True
    
    conn.close()
    return False

def create_and_setup_message(user_id, group_id, group_name=None):
    try:
        status_text = f"🤖 <b>Статус сервера</b>\n\n🔄 Инициализация...\n⏰ {get_current_time()}"
        
        result = send_message(group_id, status_text)
        
        if result and result.get('ok'):
            message_id = result["result"]["message_id"]
            
            setup_user_settings(
                user_id=user_id,
                group_id=group_id,
                thread_id=None,
                message_id=message_id,
                group_name=group_name or f"Группа {group_id}",
                server_info="Сервер"
            )
            
            logger.info(f"✅ Автонастройка: сообщение {message_id} в группе {group_id}")
            return True, message_id
        else:
            error_msg = "Не удалось создать сообщение. Проверьте права бота."
            return False, error_msg
            
    except Exception as e:
        logger.error(f"❌ Ошибка автонастройки: {e}")
        error_msg = f"Ошибка: {str(e)}"
        return False, error_msg

def update_server_status(user_id, status):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
    
    if not user:
        conn.close()
        return False
    
    conn.execute('INSERT INTO server_statuses (user_id, status) VALUES (?, ?)', (user_id, status))
    conn.commit()
    conn.close()
    
    status_text = generate_status_text(user_id, status)
    
    if user['message_id']:
        success = edit_message(user['group_id'], user['message_id'], status_text)
        if success:
            logger.info(f"✅ Сообщение {user['message_id']} отредактировано")
            notify_subscribers(user_id, status)
        else:
            logger.warning(f"❌ Не удалось отредактировать сообщение")
        return success
    else:
        logger.warning("❌ Сообщение для редактирования не найдено")
        return False

def generate_status_text(user_id, status):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
    subscriber_count = get_subscriber_count(user_id)
    conn.close()
    
    status_emojis = {
        "status_on": "🟢",
        "status_pause": "🟡", 
        "status_off": "🔴",
        "status_unknown": "❓"
    }
    
    status_names = {
        "status_on": "ВКЛЮЧЕН",
        "status_pause": "ПРИОСТАНОВЛЕН",
        "status_off": "ВЫКЛЮЧЕН", 
        "status_unknown": "НЕИЗВЕСТНО"
    }
    
    emoji = status_emojis.get(status, "❓")
    name = status_names.get(status, "НЕИЗВЕСТНО")
    server_info = get_user_server_info(user_id)
    
    return f"""{emoji} <b>Статус {server_info}</b>

📊 Статус: <b>{name}</b>
👤 Владелец: {user['group_name'] if user else 'Неизвестно'}
👥 Подписчиков: {subscriber_count}
⏰ Обновлено: {get_current_time(user_id)}

💡 Используйте бота для управления статусом"""

def get_subscriber_count(target_user_id):
    conn = get_db_connection()
    count = conn.execute('SELECT COUNT(*) as count FROM subscriptions WHERE target_user_id = ?', (target_user_id,)).fetchone()
    conn.close()
    return count['count'] if count else 0

def notify_subscribers(user_id, new_status):
    conn = get_db_connection()
    server_info = conn.execute('SELECT group_name, server_info FROM users WHERE user_id = ?', (user_id,)).fetchone()
    if not server_info:
        conn.close()
        return
    
    subscribers = conn.execute('SELECT subscriber_id FROM subscriptions WHERE target_user_id = ?', (user_id,)).fetchall()
    conn.close()
    
    if not subscribers:
        return
    
    status_names = {
        "status_on": "🟢 ВКЛЮЧЕН",
        "status_pause": "🟡 ПРИОСТАНОВЛЕН",
        "status_off": "🔴 ВЫКЛЮЧЕН",
        "status_unknown": "❓ НЕИЗВЕСТНО"
    }
    
    notification_text = (
        f"🔔 <b>Изменение статуса {server_info['server_info']}</b>\n\n"
        f"Владелец: <b>{server_info['group_name']}</b>\n"
        f"Новый статус: {status_names.get(new_status, 'Неизвестно')}\n"
        f"⏰ Время: {get_current_time()}"
    )
    
    for sub in subscribers:
        try:
            send_message(sub['subscriber_id'], notification_text)
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")

# ========== ПОДПИСКИ ==========
def subscribe_to_server(subscriber_id, target_user_id):
    conn = get_db_connection()
    
    existing = conn.execute('''
        SELECT * FROM subscriptions 
        WHERE subscriber_id = ? AND target_user_id = ?
    ''', (subscriber_id, target_user_id)).fetchone()
    
    if not existing:
        conn.execute('''
            INSERT INTO subscriptions (subscriber_id, target_user_id) 
            VALUES (?, ?)
        ''', (subscriber_id, target_user_id))
        conn.commit()
        
        server_owner = conn.execute('SELECT group_name, server_info FROM users WHERE user_id = ?', (target_user_id,)).fetchone()
        conn.close()
        
        if server_owner:
            send_message(target_user_id, 
                        f"🔔 <b>Новый подписчик!</b>\n\n"
                        f"На ваш {server_owner['server_info']} '{server_owner['group_name']}' подписался новый пользователь.")
        return True
    else:
        conn.close()
        return False

def unsubscribe_from_all(subscriber_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM subscriptions WHERE subscriber_id = ?', (subscriber_id,))
    conn.commit()
    conn.close()
    return True

def unsubscribe_from_server(subscriber_id, target_user_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM subscriptions WHERE subscriber_id = ? AND target_user_id = ?', (subscriber_id, target_user_id))
    conn.commit()
    conn.close()
    return True

# ========== АДМИН-ФУНКЦИИ ==========
def get_all_users():
    conn = get_db_connection()
    users = conn.execute('''
        SELECT u.*, 
               (SELECT status FROM server_statuses ss 
                WHERE ss.user_id = u.user_id 
                ORDER BY ss.created_at DESC LIMIT 1) as last_status,
               (SELECT COUNT(*) FROM subscriptions s WHERE s.target_user_id = u.user_id) as subscribers_count
        FROM users u
    ''').fetchall()
    conn.close()
    return users

def broadcast_message(text):
    conn = get_db_connection()
    users = conn.execute('SELECT user_id FROM users').fetchall()
    conn.close()
    
    success_count = 0
    for user in users:
        if send_message(user['user_id'], text):
            success_count += 1
    
    return success_count

def set_bot_status(enabled, reason=""):
    global bot_enabled, bot_disable_reason
    bot_enabled = enabled
    bot_disable_reason = reason

# ========== КНОПКИ ==========
def get_main_menu_buttons():
    return [
        [{"text": "⚡ Управление статусом", "callback_data": "manage_status"}],
        [{"text": "📝 Отправить сообщение", "callback_data": "send_message"}],
        [{"text": "📊 Статистика", "callback_data": "stats"}],
        [{"text": "📈 История", "callback_data": "history"}],
        [{"text": "🔔 Подписки", "callback_data": "subscriptions"}],
        [{"text": "⚙️ Настройки", "callback_data": "settings"}]
    ]

def get_status_buttons():
    return [
        [
            {"text": "🟢 Включен", "callback_data": "status_on"},
            {"text": "🟡 Приостановлен", "callback_data": "status_pause"}
        ],
        [
            {"text": "🔴 Выключен", "callback_data": "status_off"},
            {"text": "❓ Неизвестно", "callback_data": "status_unknown"}
        ],
        [{"text": "🔙 Назад", "callback_data": "back_to_main"}]
    ]

def get_settings_buttons(user_id):
    buttons = [
        [{"text": "🕐 Изменить часовой пояс", "callback_data": "change_timezone"}],
        [{"text": "✏️ Изменить настройки группы", "callback_data": "change_group_settings"}],
        [{"text": "🔗 Изменить название/ссылку", "callback_data": "change_server_info"}],
    ]
    
    if int(user_id) == int(ADMIN_USER_ID):
        if is_admin_authenticated(user_id):
            buttons.insert(0, [{"text": "👑 Админ-панель", "callback_data": "admin_panel"}])
        else:
            buttons.insert(0, [{"text": "🔐 Войти в админку", "callback_data": "admin_login"}])
    
    buttons.append([{"text": "🔙 Назад", "callback_data": "back_to_main"}])
    
    return buttons

def get_admin_buttons():
    return [
        [{"text": "👥 Все пользователи", "callback_data": "admin_users"}],
        [{"text": "📢 Рассылка", "callback_data": "admin_broadcast"}],
        [{"text": "🔧 Управление ботом", "callback_data": "admin_manage_bot"}],
        [{"text": "🚪 Выйти из админки", "callback_data": "admin_logout"}],
        [{"text": "🔙 Назад", "callback_data": "back_to_settings"}]
    ]

def get_welcome_buttons():
    return [
        [{"text": "📋 Начать настройку", "callback_data": "start_setup"}],
        [{"text": "🚀 Быстрая настройка", "callback_data": "quick_setup"}],
        [{"text": "🔍 Как найти thread_id?", "callback_data": "help_thread_id"}],
        [{"text": "🔄 Перезапустить настройку", "callback_data": "restart_setup"}]
    ]

def get_create_message_buttons():
    return [
        [{"text": "📝 Создать сообщение", "callback_data": "create_status_message"}],
        [{"text": "🔙 Назад", "callback_data": "back_to_main"}]
    ]

def get_back_button():
    return [[{"text": "🔙 Назад", "callback_data": "back_to_main"}]]

def get_retry_setup_buttons():
    return [
        [{"text": "🔄 Попробовать снова", "callback_data": "restart_setup"}],
        [{"text": "📋 Ручная настройка", "callback_data": "start_setup"}],
        [{"text": "🚀 Быстрая настройка", "callback_data": "quick_setup"}],
        [{"text": "❌ Отмена", "callback_data": "back_to_main"}]
    ]

# ========== ОБРАБОТЧИКИ СООБЩЕНИЙ ==========
def validate_group_settings_input(text):
    try:
        parts = text.split(',')
        if len(parts) < 4:
            return False, "❌ Неверный формат. Нужно 4 значения через запятую"
        
        group_id = int(parts[0])
        thread_id = int(parts[1]) if parts[1].strip() and parts[1].strip() != 'None' else None
        message_id = int(parts[2])
        group_name = parts[3].strip()
        
        if group_id >= 0:
            return False, "❌ ID группы должен быть отрицательным"
        
        if not group_name:
            return False, "❌ Название группы не может быть пустым"
        
        return True, (group_id, thread_id, message_id, group_name)
        
    except ValueError as e:
        return False, f"❌ Ошибка в числовых значениях: {str(e)}"
    except Exception as e:
        return False, f"❌ Ошибка валидации: {str(e)}"

def process_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    
    if user_id != chat_id:
        return False
    
    if user_id in user_states:
        state = user_states[user_id]
        
        if state == "waiting_group_settings":
            is_valid, validation_result = validate_group_settings_input(text)
            
            if not is_valid:
                send_message(user_id, 
                           f"{validation_result}\n\n"
                           "💡 <b>Пример правильного формата:</b>\n"
                           "<code>-100123456789,,123,Мой Сервер</code>\n\n"
                           "Хотите попробовать снова?",
                           get_retry_setup_buttons())
                user_states[user_id] = None
                return True
            
            group_id, thread_id, message_id, group_name = validation_result
            
            try:
                setup_user_settings(user_id, group_id, thread_id, message_id, group_name)
                user_states[user_id] = "waiting_server_info_initial"
                send_message(user_id, 
                            f"✅ Группа '{group_name}' настроена!\n"
                            f"💬 Бот будет редактировать сообщение: {message_id}\n\n"
                            "🔗 <b>Теперь настройте название или ссылку:</b>\n\n"
                            "Введите название или ссылку для отображения в статусе:\n\n"
                            "💡 <b>Примеры:</b>\n"
                            "• <code>Мой Minecraft Сервер</code>\n"
                            "• <code>https://myserver.com</code>\n"
                            "• <code>Discord сервер</code>\n"
                            "• <code>t.me/mychannel</code>\n\n"
                            "Или отправьте <code>пропустить</code> для значения по умолчанию\n"
                            "Или <code>назад</code> чтобы вернуться к настройке группы",
                            [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}]])
                
            except Exception as e:
                send_message(user_id, 
                           f"❌ <b>Ошибка сохранения настроек!</b>\n\n"
                           f"Причина: {str(e)}\n\n"
                           "Попробуйте снова:",
                           get_retry_setup_buttons())
                user_states[user_id] = None
            
            return True
            
        elif state == "waiting_server_info_initial":
            if text.lower() == "назад":
                user_states[user_id] = "waiting_group_settings"
                send_message(user_id,
                            "🔙 <b>Возврат к настройке группы</b>\n\n"
                            "Введите данные в формате:\n"
                            "<code>group_id,thread_id,message_id,название_группы</code>\n\n"
                            "Пример:\n"
                            "<code>-100123456789,,123,Мой Сервер</code>",
                            [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}]])
                return True
            
            server_info = text if text.lower() != "пропустить" else "Сервер"
            
            try:
                conn = get_db_connection()
                conn.execute('UPDATE users SET server_info = ? WHERE user_id = ?', (server_info, user_id))
                conn.commit()
                conn.close()
                
                send_message(user_id, 
                            f"✅ <b>Настройка завершена!</b>\n\n"
                            f"🏷️ Объект: <b>{server_info}</b>\n"
                            f"📋 Группа: {get_group_name(user_id)}\n"
                            f"💬 Сообщение: {get_message_id(user_id)}\n\n"
                            f"Теперь вы можете управлять статусом {server_info}",
                            buttons=get_main_menu_buttons())
                
                user_states[user_id] = None
                
            except Exception as e:
                send_message(user_id,
                            f"❌ <b>Ошибка сохранения!</b>\n\n"
                            f"Причина: {str(e)}\n\n"
                            "Попробуйте ввести название снова:",
                            [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}]])
            
            return True
            
        elif state == "waiting_broadcast" and int(user_id) == int(ADMIN_USER_ID) and is_admin_authenticated(user_id):
            success_count = broadcast_message(text)
            send_message(user_id, f"✅ Рассылка отправлена {success_count} пользователям!", buttons=get_admin_buttons())
            user_states[user_id] = None
            return True
            
        elif state == "waiting_timezone":
            try:
                pytz.timezone(text)
                conn = get_db_connection()
                conn.execute('UPDATE users SET timezone = ? WHERE user_id = ?', (text, user_id))
                conn.commit()
                conn.close()
                send_message(user_id, f"✅ Часовой пояс изменен на: {text}", buttons=get_settings_buttons(user_id))
            except:
                send_message(user_id, "❌ Неверный часовой пояс. Используйте формат: Europe/Moscow", buttons=get_settings_buttons(user_id))
            
            user_states[user_id] = None
            return True
            
        elif state == "waiting_group_message":
            conn = get_db_connection()
            user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
            conn.close()
            
            if user:
                result = send_message(
                    user['group_id'], 
                    text,
                    thread_id=user['thread_id'] if user['thread_id'] else None
                )
                
                if result and result.get('ok'):
                    send_message(user_id, "✅ Сообщение успешно отправлено в группу!", buttons=get_main_menu_buttons())
                else:
                    send_message(user_id, "❌ Не удалось отправить сообщение. Проверьте права бота.", buttons=get_main_menu_buttons())
            else:
                send_message(user_id, "❌ Ошибка: данные группы не найдены.", buttons=get_main_menu_buttons())
            
            user_states[user_id] = None
            return True
            
        elif state == "waiting_disable_reason" and int(user_id) == int(ADMIN_USER_ID) and is_admin_authenticated(user_id):
            set_bot_status(False, text)
            send_message(user_id, f"🔴 Бот выключен. Причина: {text}", buttons=get_admin_buttons())
            user_states[user_id] = None
            return True
            
        elif state == "waiting_server_info":
            conn = get_db_connection()
            conn.execute('UPDATE users SET server_info = ? WHERE user_id = ?', (text, user_id))
            conn.commit()
            conn.close()
            
            send_message(user_id, 
                        f"✅ Название/ссылка успешно изменена!\n\n"
                        f"Теперь в статусе будет отображаться: <b>{text}</b>",
                        buttons=get_settings_buttons(user_id))
            
            user_states[user_id] = None
            return True
            
        elif state == "waiting_admin_password":
            if authenticate_admin(user_id, text):
                send_message(user_id, "✅ <b>Доступ разрешен!</b>\n\nДобро пожаловать в админ-панель!", buttons=get_admin_buttons())
                show_admin_panel(user_id)
            else:
                send_message(user_id, "❌ <b>Неверный пароль!</b>\n\nПопробуйте еще раз или вернитесь в меню.", 
                           [[{"text": "🔐 Попробовать снова", "callback_data": "admin_login"}],
                            [{"text": "🔙 В главное меню", "callback_data": "back_to_main"}]])
            
            user_states[user_id] = None
            return True
            
        elif state == "waiting_group_id_for_setup":
            try:
                group_id = int(text)
                
                if group_id >= 0:
                    send_message(user_id,
                                "❌ <b>Неверный ID группы!</b>\n\n"
                                "ID группы должен быть отрицательным числом (начинаться с -100).\n\n"
                                "Примеры правильных ID:\n"
                                "• <code>-100123456789</code>\n"
                                "• <code>-100987654321</code>\n\n"
                                "Попробуйте снова:",
                                get_retry_setup_buttons())
                    user_states[user_id] = None
                    return True
                
                success, result = create_and_setup_message(user_id, group_id)
                
                if success:
                    send_message(user_id,
                                f"✅ <b>Автонастройка завершена!</b>\n\n"
                                f"📋 Группа ID: {group_id}\n"
                                f"💬 Создано сообщение: {result}\n\n"
                                f"🤖 Бот автоматически настроен и готов к работе!",
                                buttons=get_main_menu_buttons())
                    user_states[user_id] = None
                else:
                    send_message(user_id,
                                f"❌ <b>Ошибка автонастройки!</b>\n\n"
                                f"{result}\n\n"
                                "Выберите действие:",
                                get_retry_setup_buttons())
                    user_states[user_id] = None
                
            except ValueError:
                send_message(user_id, 
                            "❌ <b>Неверный формат!</b>\n\n"
                            "ID группы должен быть числом.\n"
                            "Пример: <code>-100123456789</code>\n\n"
                            "Попробуйте снова:",
                            get_retry_setup_buttons())
                user_states[user_id] = None
            except Exception as e:
                send_message(user_id, 
                            f"❌ <b>Неизвестная ошибка!</b>\n\n"
                            f"Причина: {str(e)}\n\n"
                            "Попробуйте снова:",
                            get_retry_setup_buttons())
                user_states[user_id] = None
            
            return True
    
    if text == "/start":
        reset_user_settings(user_id)
        
        welcome_text = (
            "🔄 <b>Бот полностью перезагружен!</b>\n\n"
            "🤖 <b>Добро пожаловать в бот управления статусами!</b>\n\n"
            "📋 <b>Выберите способ настройки:</b>\n\n"
            "🚀 <b>Быстрая настройка</b> (рекомендуется):\n"
            "• Бот сам создаст сообщение в группе\n"
            "• Автоматическая настройка\n"
            "• Просто укажите ID группы\n\n"
            "📋 <b>Ручная настройка</b>:\n"
            "• Полный контроль над настройками\n"
            "• Указание всех параметров вручную\n\n"
            "💡 <b>Что можно отслеживать?</b>\n"
            "• Серверы (Minecraft, Discord и др.)\n"
            "• Сайты и приложения\n" 
            "• Telegram каналы и боты\n"
            "• Любые другие объекты!"
        )
        
        send_message(user_id, welcome_text, get_welcome_buttons())
        logger.info(f"🔄 Пользователь {user_id} выполнил /start")
        return True
        
    elif text == "/admin":
        if int(user_id) == int(ADMIN_USER_ID):
            if is_admin_authenticated(user_id):
                show_admin_panel(user_id)
                logger.info(f"👑 Админ {user_id} открыл панель")
            else:
                user_states[user_id] = "waiting_admin_password"
                send_message(user_id, 
                           "🔐 <b>Аутентификация администратора</b>\n\n"
                           "Введите пароль для доступа к админ-панели:",
                           [[{"text": "🔙 Отмена", "callback_data": "back_to_main"}]])
        else:
            send_message(user_id, "❌ <b>Доступ запрещен</b>\n\nЭта команда только для администратора.")
        return True
        
    elif text == "/stats":
        show_stats(user_id)
        return True
        
    elif text == "/settings":
        show_settings(user_id)
        return True
    
    elif text.lower() in ["/restart", "/reset", "перезапустить", "сбросить"]:
        reset_user_settings(user_id)
        send_message(user_id,
                    "🔄 <b>Настройки сброшены!</b>\n\n"
                    "Вы можете начать настройку заново:",
                    get_welcome_buttons())
        return True
    
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    
    if user:
        show_main_menu(user_id)
    else:
        send_message(user_id, 
                    "❌ <b>Бот не настроен</b>\n\n"
                    "Используйте /start для начальной настройки",
                    get_welcome_buttons())
    
    return True

def get_group_name(user_id):
    conn = get_db_connection()
    user = conn.execute('SELECT group_name FROM users WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    return user['group_name'] if user else 'Неизвестно'

def get_message_id(user_id):
    conn = get_db_connection()
    user = conn.execute('SELECT message_id FROM users WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    return user['message_id'] if user else 'Неизвестно'

def process_callback(callback):
    user_id = callback["from"]["id"]
    data = callback["data"]
    message_id = callback["message"]["message_id"]
    
    answer_callback(callback["id"])
    
    if data == "restart_setup":
        if user_id in user_states:
            del user_states[user_id]
        
        edit_message(user_id, message_id,
                    "🔄 <b>Настройка перезапущена!</b>\n\n"
                    "Выберите способ настройки:",
                    get_welcome_buttons())
        return True
    
    elif data == "quick_setup":
        user_states[user_id] = "waiting_group_id_for_setup"
        edit_message(user_id, message_id,
                    "🚀 <b>Быстрая настройка</b>\n\n"
                    "📋 <b>Для автоматической настройки:</b>\n\n"
                    "1. Добавьте бота в вашу группу\n"
                    "2. Дайте боту права администратора\n"
                    "3. Укажите ID группы ниже\n\n"
                    "💡 <b>Как найти ID группы?</b>\n"
                    "• Добавьте @RawDataBot в группу\n"
                    "• Он покажет ID группы (начинается с -100)\n\n"
                    "📝 Введите ID группы:\n\n"
                    "💡 <b>Пример:</b> <code>-100123456789</code>",
                    [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}],
                     [{"text": "🔙 Отмена", "callback_data": "back_to_main"}]])
        return True
    
    elif data == "admin_login":
        if int(user_id) == int(ADMIN_USER_ID):
            user_states[user_id] = "waiting_admin_password"
            edit_message(user_id, message_id,
                        "🔐 <b>Аутентификация администратора</b>\n\n"
                        "Введите пароль для доступа к админ-панели:",
                        [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}],
                         [{"text": "🔙 Отмена", "callback_data": "back_to_settings"}]])
        else:
            send_message(user_id, "❌ Доступ запрещен")
        return True
        
    elif data == "admin_logout":
        if int(user_id) == int(ADMIN_USER_ID):
            logout_admin(user_id)
            edit_message(user_id, message_id,
                        "✅ <b>Выход выполнен</b>\n\n"
                        "Вы вышли из админ-панели.",
                        get_settings_buttons(user_id))
        return True
    
    elif data == "send_message":
        show_send_message_menu(user_id, message_id)
        return True
        
    elif data == "history":
        show_history(user_id, message_id)
        return True
        
    elif data == "subscriptions":
        show_subscriptions_menu(user_id, message_id)
        return True
        
    elif data.startswith("subscribe_"):
        target_user_id = int(data.split("_")[1])
        if subscribe_to_server(user_id, target_user_id):
            send_message(user_id, "✅ Вы успешно подписались на сервер!")
        show_subscriptions_menu(user_id, message_id)
        return True
        
    elif data.startswith("unsubscribe_"):
        target_user_id = int(data.split("_")[1])
        if unsubscribe_from_server(user_id, target_user_id):
            send_message(user_id, "✅ Вы отписались от сервера")
        show_subscriptions_menu(user_id, message_id)
        return True
        
    elif data == "unsubscribe_all":
        if unsubscribe_from_all(user_id):
            send_message(user_id, "✅ Вы отписались от всех серверов")
        show_subscriptions_menu(user_id, message_id)
        return True
        
    elif data == "change_server_info":
        user_states[user_id] = "waiting_server_info"
        current_info = get_user_server_info(user_id)
        edit_message(user_id, message_id,
                    f"🔗 <b>Изменение названия/ссылки</b>\n\n"
                    f"Текущее значение: <b>{current_info}</b>\n\n"
                    "Введите новое название или ссылку:\n\n"
                    "💡 <b>Примеры:</b>\n"
                    "• <code>Мой Minecraft Сервер</code>\n"
                    "• <code>https://myserver.com</code>\n"
                    "• <code>Discord сервер</code>\n"
                    "• <code>t.me/mychannel</code>",
                    [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}],
                     [{"text": "🔙 Отмена", "callback_data": "back_to_settings"}]])
        return True
    
    elif data == "create_status_message":
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
        conn.close()
        
        if user:
            status_text = generate_status_text(user_id, "status_unknown")
            if send_new_status_message(user_id, status_text):
                edit_message(user_id, message_id,
                            "✅ <b>Сообщение создано!</b>\n\n"
                            "Бот создал новое сообщение для статуса в вашей группе.\n"
                            "Теперь вы можете управлять статусом сервера.",
                            get_main_menu_buttons())
            else:
                edit_message(user_id, message_id,
                            "❌ <b>Ошибка создания сообщения</b>\n\n"
                            "Проверьте права бота в группе.",
                            get_main_menu_buttons())
        return True
    
    elif data.startswith("status_"):
        success = update_server_status(user_id, data)
        
        if success:
            status_names = {
                "status_on": "🟢 ВКЛЮЧЕН",
                "status_pause": "🟡 ПРИОСТАНОВЛЕН", 
                "status_off": "🔴 ВЫКЛЮЧЕН",
                "status_unknown": "❓ НЕИЗВЕСТНО"
            }
            edit_message(user_id, message_id,
                        f"✅ <b>Статус обновлен!</b>\n\n"
                        f"Новый статус: {status_names.get(data, 'Неизвестно')}\n"
                        f"⏰ Время: {get_current_time(user_id)}",
                        get_main_menu_buttons())
        else:
            edit_message(user_id, message_id,
                        "❌ <b>Сообщение не найдено!</b>\n\n"
                        "Бот не может найти сообщение для редактирования.\n"
                        "Возможно, сообщение было удалено или не настроено.\n\n"
                        "Создайте новое сообщение для статуса:",
                        get_create_message_buttons())
        return True
    
    elif data == "admin_panel":
        if int(user_id) == int(ADMIN_USER_ID) and is_admin_authenticated(user_id):
            show_admin_panel(user_id, message_id)
        else:
            send_message(user_id, "❌ Доступ запрещен или требуется аутентификация")
        return True
    
    elif data == "admin_users" and int(user_id) == int(ADMIN_USER_ID) and is_admin_authenticated(user_id):
        show_all_users(user_id, message_id)
        return True
        
    elif data == "admin_broadcast" and int(user_id) == int(ADMIN_USER_ID) and is_admin_authenticated(user_id):
        user_states[user_id] = "waiting_broadcast"
        edit_message(user_id, message_id,
                    "📢 <b>Рассылка сообщения</b>\n\n"
                    "Введите текст для рассылки всем пользователям:",
                    [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}],
                     [{"text": "🔙 Отмена", "callback_data": "admin_panel"}]])
        return True
        
    elif data == "admin_manage_bot" and int(user_id) == int(ADMIN_USER_ID) and is_admin_authenticated(user_id):
        show_bot_management(user_id, message_id)
        return True
        
    elif data == "admin_enable_bot" and int(user_id) == int(ADMIN_USER_ID) and is_admin_authenticated(user_id):
        set_bot_status(True, "")
        show_bot_management(user_id, message_id)
        send_message(user_id, "✅ Бот включен!")
        return True
        
    elif data == "admin_disable_bot" and int(user_id) == int(ADMIN_USER_ID) and is_admin_authenticated(user_id):
        user_states[user_id] = "waiting_disable_reason"
        edit_message(user_id, message_id,
                    "🔴 <b>Выключение бота</b>\n\n"
                    "Введите причину выключения:",
                    [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}],
                     [{"text": "🔙 Отмена", "callback_data": "admin_manage_bot"}]])
        return True
    
    elif data == "start_setup":
        user_states[user_id] = "waiting_group_settings"
        edit_message(user_id, message_id,
                    "🤖 <b>Настройка группы</b>\n\n"
                    "Отправьте данные в формате:\n"
                    "<code>group_id,thread_id,message_id,название_группы</code>\n\n"
                    "📝 <b>Пример:</b>\n"
                    "<code>-100123456789,10,123,Мой Сервер</code>\n\n"
                    "ℹ️ <i>Если темы нет, оставьте thread_id пустым:</i>\n"
                    "<code>-100123456789,,123,Мой Сервер</code>",
                    [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}],
                     [{"text": "🔙 Отмена", "callback_data": "back_to_main"}]])
        return True
    
    elif data == "help_thread_id":
        help_text = (
            "🔍 <b>Как найти данные?</b>\n\n"
            "1. <b>group_id</b> - ID группы:\n"
            "   • Добавьте @RawDataBot в группу\n"
            "   • Он покажет ID группы (начинается с -100)\n\n"
            "2. <b>message_id</b> - ID сообщения:\n"
            "   • Перешлите сообщение в @RawDataBot\n"
            "   • Он покажет ID сообщения\n\n"
            "3. <b>thread_id</b> - ID темы:\n"
            "   • Откройте тему в веб-версии\n"
            "   • Посмотрите в URL: t.me/c/.../<b>123</b>\n"
            "   • Или оставьте пустым для основной темы\n\n"
            "💡 <b>Примеры правильных данных:</b>\n"
            "• Без темы: <code>-100123456789,,123,Мой Сервер</code>\n"
            "• С темой: <code>-100123456789,10,123,Мой Сервер</code>"
        )
        edit_message(user_id, message_id, help_text, 
                    [[{"text": "🔄 Начать настройку", "callback_data": "start_setup"}],
                     [{"text": "🔙 Назад", "callback_data": "restart_setup"}]])
        return True
    
    elif data == "back_to_main":
        show_main_menu(user_id, message_id)
        return True
        
    elif data == "back_to_settings":
        show_settings(user_id, message_id)
        return True
        
    elif data == "manage_status":
        show_status_management(user_id, message_id)
        return True
        
    elif data == "stats":
        show_stats(user_id, message_id)
        return True
        
    elif data == "settings":
        show_settings(user_id, message_id)
        return True
        
    elif data == "change_timezone":
        user_states[user_id] = "waiting_timezone"
        edit_message(user_id, message_id,
                    "🕐 <b>Изменение часового пояса</b>\n\n"
                    "Введите ваш часовой пояс (например: Europe/Moscow, Asia/Yekaterinburg):",
                    [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}],
                     [{"text": "🔙 Отмена", "callback_data": "back_to_settings"}]])
        return True
        
    elif data == "change_group_settings":
        user_states[user_id] = "waiting_group_settings"
        edit_message(user_id, message_id,
                    "✏️ <b>Настройки группы</b>\n\n"
                    "Введите данные в формате:\n"
                    "<code>group_id,thread_id,message_id,название_группы</code>\n\n"
                    "Пример:\n"
                    "<code>-100123456,10,123,Мой Сервер</code>\n\n"
                    "Если темы нет, оставьте thread_id пустым:\n"
                    "<code>-100123456,,123,Мой Сервер</code>",
                    [[{"text": "🔄 Начать заново", "callback_data": "restart_setup"}],
                     [{"text": "🔙 Отмена", "callback_data": "back_to_settings"}]])
        return True
    
    return True

def show_main_menu(user_id, message_id=None):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    
    if user:
        server_info = get_user_server_info(user_id)
        text = (
            f"🤖 <b>Управление статусами</b>\n\n"
            f"🏷️ <b>Текущий объект:</b> {server_info}\n"
            f"📋 Группа: {user['group_name']}\n"
            f"💬 Сообщение: {user['message_id'] if user['message_id'] else '❌ Не создано'}\n"
            f"🏷️ Тема: {user['thread_id'] if user['thread_id'] else 'Нет'}\n"
            f"⏰ Часовой пояс: {user['timezone']}\n\n"
            f"<b>Доступные функции:</b>\n"
            "• ⚡ Управление статусом\n"
            "• 📝 Отправка сообщений в группу\n" 
            "• 📊 Просмотр статистики\n"
            "• 📈 История изменений\n"
            "• 🔔 Управление подписками\n"
            "• ⚙️ Настройки\n\n"
            f"⏰ Ваше время: {get_current_time(user_id)}"
        )
    else:
        text = "❌ <b>Бот не настроен</b>\n\nИспользуйте настройки для конфигурации"
    
    if message_id:
        edit_message(user_id, message_id, text, get_main_menu_buttons())
    else:
        send_message(user_id, text, get_main_menu_buttons())

def show_status_management(user_id, message_id):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    
    if not user:
        text = "❌ <b>Сначала настройте группу!</b>\n\nПерейдите в настройки и укажите данные вашей группы."
        edit_message(user_id, message_id, text, [[{"text": "⚙️ Настройки", "callback_data": "settings"}]])
        return
    
    server_info = get_user_server_info(user_id)
    
    if not user['message_id']:
        text = (
            f"⚠️ <b>Сообщение не настроено</b>\n\n"
            f"Для управления статусом {server_info} нужно сообщение в группе.\n\n"
            "Выберите действие:"
        )
        buttons = [
            [{"text": "📝 Создать сообщение", "callback_data": "create_status_message"}],
            [{"text": "⚙️ Настроить сообщение", "callback_data": "change_group_settings"}],
            [{"text": "🔙 Назад", "callback_data": "back_to_main"}]
        ]
    else:
        text = (
            f"⚡ <b>Управление статусом {server_info}</b>\n\n"
            f"Группа: {user['group_name']}\n"
            f"Сообщение: {user['message_id']}\n"
            f"Тема: {user['thread_id'] if user['thread_id'] else 'Нет'}\n"
            f"Подписчиков: {get_subscriber_count(user_id)}\n\n"
            "Выберите новый статус:"
        )
        buttons = get_status_buttons()
    
    edit_message(user_id, message_id, text, buttons)

def show_stats(user_id, message_id=None):
    conn = get_db_connection()
    latest_statuses = conn.execute('''
        SELECT ss.user_id, ss.status, u.group_name, u.server_info
        FROM server_statuses ss
        INNER JOIN (
            SELECT user_id, MAX(created_at) as max_date
            FROM server_statuses
            GROUP BY user_id
        ) latest ON ss.user_id = latest.user_id AND ss.created_at = latest.max_date
        INNER JOIN users u ON ss.user_id = u.user_id
    ''').fetchall()
    conn.close()
    
    stats = {"status_on": 0, "status_pause": 0, "status_off": 0, "status_unknown": 0}
    for status in latest_statuses:
        if status['status'] in stats:
            stats[status['status']] += 1
    
    total = sum(stats.values())
    
    status_emojis = {
        "status_on": "🟢",
        "status_pause": "🟡",
        "status_off": "🔴", 
        "status_unknown": "❓"
    }
    
    status_text = ""
    for status, count in stats.items():
        emoji = status_emojis.get(status, "❓")
        status_text += f"{emoji} {count}\n"
    
    text = (
        "📊 <b>Глобальная статистика</b>\n\n"
        f"Всего объектов: {total}\n\n"
        f"Статусы:\n{status_text}\n"
        f"⏰ Обновлено: {get_current_time(user_id)}"
    )
    
    if message_id:
        edit_message(user_id, message_id, text, [[{"text": "🔙 Назад", "callback_data": "back_to_main"}]])
    else:

        send_message(user_id, text, [[{"text": "🔙 Назад", "callback_data": "back_to_main"}]])

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT, debug=False)


