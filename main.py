import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id
import sqlite3
import re
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple, List
from threading import Thread, Lock
import asyncio
from functools import lru_cache

# === КОНФИГУРАЦИЯ ===
VK_TOKEN = "vk1.a.s2vhM_PXqe_0kT-Kx89uKKR2dAYDPz6_Iodx70pwYYsBPRT1R1gvSVRtgFpBgf4QZ3jafIZz8PqSxXepPzR5zg1fgQzGQGAjh7r8zO1i-wHEljtNr126nw9hoHjHKZSlxE_-ZGsWDzH-wFEAp7Qc_m_hhDeTAgPGOQnuBVTJP4j2NohVK12ej9Id0akOY27jXIrbXRdiK2W54QJV8cOtWg"
ADMIN_IDS = [1030658918]  # Замените на ваши ID VK

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === КОНСТАНТЫ ===
MAX_REPORTS_PER_HOUR = 5
DB_PATH = '/app/data/reports.db'

# === БАЗА ДАННЫХ ===
def init_db():
    """Инициализация базы данных"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    joined_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_activity DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_id INTEGER NOT NULL,
                    target_username TEXT NOT NULL,
                    status TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    proof_photo TEXT,
                    is_approved BOOLEAN DEFAULT FALSE,
                    is_rejected BOOLEAN DEFAULT FALSE,
                    moderator_id INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS blocked_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    reason TEXT,
                    blocked_by INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reports_target_username ON reports(target_username)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reports_reporter_id ON reports(reporter_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reports_timestamp ON reports(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_blocked_users_user_id ON blocked_users(user_id)')
            
            conn.commit()
            conn.close()
            logger.info("✅ База данных инициализирована успешно")
            return True
            
        except sqlite3.Error as e:
            logger.error(f"❌ Ошибка инициализации БД (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                logger.critical("❌ Критическая ошибка: не удалось создать таблицы БД")
                return False
    return False

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
class DatabaseConnection:
    """Контекстный менеджер для работы с базой данных"""
    
    @staticmethod
    def get_connection():
        try:
            return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        except sqlite3.Error as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            raise
    
    @staticmethod
    def execute_query(query: str, params: tuple = (), fetch_one: bool = False, 
                     fetch_all: bool = False, return_lastrowid: bool = False, commit: bool = True):
        try:
            with DatabaseConnection.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                
                if fetch_one:
                    result = cursor.fetchone()
                elif fetch_all:
                    result = cursor.fetchall()
                elif return_lastrowid:
                    result = cursor.lastrowid
                else:
                    result = cursor.rowcount
                
                if commit:
                    conn.commit()
                
                return result
        except sqlite3.Error as e:
            logger.error(f"❌ Ошибка выполнения запроса: {e}")
            if "no such table" in str(e):
                logger.warning("🔄 Попытка восстановить таблицы БД...")
                init_db()
            raise

def add_bot_user(user_id, username, first_name, last_name):
    try:
        query = '''
            INSERT OR REPLACE INTO bot_users (user_id, username, first_name, last_name, joined_date, last_activity)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        '''
        DatabaseConnection.execute_query(query, (user_id, username, first_name, last_name))
        logger.info(f"✅ Добавлен/обновлен пользователь: {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка добавления пользователя {user_id}: {e}")
        return False

@lru_cache(maxsize=128)
def is_user_blocked(user_id: int) -> bool:
    try:
        query = 'SELECT id FROM blocked_users WHERE user_id = ?'
        result = DatabaseConnection.execute_query(query, (user_id,), fetch_one=True)
        return result is not None
    except Exception as e:
        logger.error(f"❌ Ошибка проверки блокировки: {e}")
        return False

def get_user_reports(target_username: str) -> List[Tuple]:
    try:
        query = '''
            SELECT status, comment, timestamp FROM reports 
            WHERE target_username = ? AND is_approved = TRUE
            ORDER BY timestamp DESC
            LIMIT 10
        '''
        results = DatabaseConnection.execute_query(query, (target_username.lower(),), fetch_all=True)
        return results if results else []
    except Exception as e:
        logger.error(f"❌ Ошибка получения жалоб: {e}")
        return []

def get_recent_reports_count(reporter_id: int, hours: int = 1) -> int:
    try:
        time_threshold = datetime.now() - timedelta(hours=hours)
        query = 'SELECT COUNT(*) FROM reports WHERE reporter_id = ? AND timestamp > ?'
        result = DatabaseConnection.execute_query(query, (reporter_id, time_threshold.isoformat()), fetch_one=True)
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"❌ Ошибка подсчета жалоб: {e}")
        return 0

def add_report(reporter_id: int, target_username: str, status: str, comment: str, proof_photo: str = None) -> Optional[int]:
    try:
        query = '''
            INSERT INTO reports (reporter_id, target_username, status, comment, proof_photo)
            VALUES (?, ?, ?, ?, ?)
        '''
        report_id = DatabaseConnection.execute_query(
            query, 
            (reporter_id, target_username.lower(), status, comment, proof_photo),
            return_lastrowid=True
        )
        
        if report_id:
            logger.info(f"🆕 Создана жалоба #{report_id} на @{target_username}")
        return report_id
    except Exception as e:
        logger.error(f"❌ Ошибка добавления жалобы: {e}")
        return None

def get_pending_reports() -> List[Tuple]:
    try:
        query = '''
            SELECT id, reporter_id, target_username, status, comment, proof_photo
            FROM reports WHERE is_approved = FALSE AND is_rejected = FALSE
            ORDER BY timestamp ASC LIMIT 20
        '''
        results = DatabaseConnection.execute_query(query, fetch_all=True)
        return results if results else []
    except Exception as e:
        logger.error(f"❌ Ошибка получения жалоб: {e}")
        return []

def approve_report(report_id: int, moderator_id: int) -> Tuple[Optional[int], Optional[str]]:
    try:
        query = '''
            UPDATE reports SET is_approved = TRUE, moderator_id = ?
            WHERE id = ? AND is_approved = FALSE AND is_rejected = FALSE
        '''
        rows_affected = DatabaseConnection.execute_query(query, (moderator_id, report_id))
        
        if rows_affected > 0:
            result = DatabaseConnection.execute_query(
                'SELECT reporter_id, target_username FROM reports WHERE id = ?',
                (report_id,), fetch_one=True
            )
            if result:
                return result[0], result[1]
        return None, None
    except Exception as e:
        logger.error(f"❌ Ошибка одобрения: {e}")
        return None, None

def reject_report(report_id: int, moderator_id: int) -> Optional[int]:
    try:
        query = '''
            UPDATE reports SET is_rejected = TRUE, moderator_id = ?
            WHERE id = ? AND is_approved = FALSE AND is_rejected = FALSE
        '''
        rows_affected = DatabaseConnection.execute_query(query, (moderator_id, report_id))
        
        if rows_affected > 0:
            result = DatabaseConnection.execute_query(
                'SELECT reporter_id FROM reports WHERE id = ?',
                (report_id,), fetch_one=True
            )
            if result:
                return result[0]
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка отклонения: {e}")
        return None

def block_user(user_id: int, username: str, reason: str, blocked_by: int) -> Tuple[bool, str]:
    try:
        if is_user_blocked(user_id):
            return False, "❌ Пользователь уже заблокирован"
        
        query = '''
            INSERT INTO blocked_users (user_id, username, reason, blocked_by)
            VALUES (?, ?, ?, ?)
        '''
        DatabaseConnection.execute_query(query, (user_id, username, reason, blocked_by))
        
        is_user_blocked.cache_clear()
        return True, f"✅ Пользователь @{username} заблокирован"
    except Exception as e:
        logger.error(f"❌ Ошибка блокировки: {e}")
        return False, f"❌ Ошибка блокировки: {e}"

def unblock_user(user_id: int) -> Tuple[bool, str]:
    try:
        query = 'DELETE FROM blocked_users WHERE user_id = ?'
        rows_affected = DatabaseConnection.execute_query(query, (user_id,))
        
        if rows_affected > 0:
            is_user_blocked.cache_clear()
            return True, "✅ Пользователь разблокирован"
        else:
            return False, "❌ Пользователь не найден в заблокированных"
    except Exception as e:
        logger.error(f"❌ Ошибка разблокировки: {e}")
        return False, f"❌ Ошибка разблокировки: {e}"

def delete_user_reports(target_username: str) -> Tuple[bool, str]:
    try:
        query = 'DELETE FROM reports WHERE target_username = ?'
        rows_deleted = DatabaseConnection.execute_query(query, (target_username.lower(),))
        return True, f"✅ Удалено {rows_deleted} жалоб на @{target_username}"
    except Exception as e:
        logger.error(f"❌ Ошибка удаления жалоб: {e}")
        return False, f"❌ Ошибка удаления: {e}"

def get_all_users_for_broadcast():
    try:
        query = 'SELECT DISTINCT user_id FROM bot_users WHERE user_id IS NOT NULL'
        results = DatabaseConnection.execute_query(query, fetch_all=True)
        return [row[0] for row in results] if results else []
    except Exception as e:
        logger.error(f"❌ Ошибка получения пользователей: {e}")
        return []

def get_user_id_by_username(username: str) -> Optional[int]:
    try:
        query = 'SELECT user_id FROM bot_users WHERE username = ?'
        result = DatabaseConnection.execute_query(query, (username,), fetch_one=True)
        return result[0] if result else None
    except Exception as e:
        logger.error(f"❌ Ошибка получения ID по username: {e}")
        return None

def validate_username(username: str) -> Tuple[bool, str]:
    if not username or len(username) < 3:
        return False, "❌ Юзернейм слишком короткий (минимум 3 символа)"
    if len(username) > 32:
        return False, "❌ Юзернейм слишком длинный (максимум 32 символа)"
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        return False, "❌ Юзернейм может содержать только буквы, цифры и подчеркивания"
    return True, "✅ Юзернейм корректен"

# === КЛАСС БОТА ===
class VKUserBot:
    def __init__(self, token):
        self.token = token
        self.vk_session = None
        self.vk = None
        self.longpoll = None
        self.user_states = {}  # Хранение состояний пользователей
        self.db_lock = Lock()
        
    def init_vk(self):
        """Инициализация VK API"""
        try:
            self.vk_session = vk_api.VkApi(token=self.token)
            self.vk = self.vk_session.get_api()
            self.longpoll = VkBotLongPoll(self.vk_session, self.get_group_id())
            logger.info("✅ VK API инициализирован")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации VK API: {e}")
            return False
    
    def get_group_id(self):
        """Получение ID группы"""
        try:
            groups = self.vk.groups.getById()
            return groups[0]['id']
        except Exception as e:
            logger.error(f"❌ Ошибка получения ID группы: {e}")
            return None
    
    def get_user_info(self, user_id):
        """Получение информации о пользователе"""
        try:
            user = self.vk.users.get(user_ids=user_id, fields=['screen_name', 'first_name', 'last_name'])
            if user:
                return user[0]
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка получения информации о пользователе {user_id}: {e}")
            return None
    
    def send_message(self, user_id, message, keyboard=None, attachment=None):
        """Отправка сообщения пользователю"""
        try:
            params = {
                'user_id': user_id,
                'message': message,
                'random_id': get_random_id()
            }
            if keyboard:
                params['keyboard'] = keyboard.get_keyboard()
            if attachment:
                params['attachment'] = attachment
            
            self.vk.messages.send(**params)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка отправки сообщения пользователю {user_id}: {e}")
            return False
    
    def upload_photo(self, photo_path):
        """Загрузка фото на сервер VK"""
        try:
            upload_server = self.vk.photos.getMessagesUploadServer()
            with open(photo_path, 'rb') as f:
                response = self.vk_session.http.post(upload_server['upload_url'], files={'photo': f})
                photo_data = response.json()
            
            photo = self.vk.photos.saveMessagesPhoto(**photo_data)
            return f"photo{photo[0]['owner_id']}_{photo[0]['id']}"
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки фото: {e}")
            return None
    
    def get_user_keyboard(self, user_id):
        """Создание клавиатуры для пользователя"""
        keyboard = VkKeyboard(one_time=False)
        keyboard.add_button('📝 Жалоба', color=VkKeyboardColor.PRIMARY)
        keyboard.add_button('🔍 Проверить', color=VkKeyboardColor.PRIMARY)
        keyboard.add_row()
        keyboard.add_button('➕ Добавить бота', color=VkKeyboardColor.SECONDARY)
        keyboard.add_button('ℹ️ Помощь', color=VkKeyboardColor.SECONDARY)
        
        if user_id in ADMIN_IDS:
            keyboard.add_row()
            keyboard.add_button('🛠 Админ', color=VkKeyboardColor.NEGATIVE)
            keyboard.add_row()
            keyboard.add_button('📊 Статистика', color=VkKeyboardColor.PRIMARY)
            keyboard.add_button('📥 Скачать БД', color=VkKeyboardColor.PRIMARY)
        
        return keyboard
    
    def get_back_keyboard(self):
        """Клавиатура с кнопкой назад"""
        keyboard = VkKeyboard(one_time=False)
        keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
        return keyboard
    
    def get_status_keyboard(self):
        """Клавиатура выбора статуса"""
        keyboard = VkKeyboard(one_time=False)
        keyboard.add_button('тролль', color=VkKeyboardColor.PRIMARY)
        keyboard.add_button('доксинг', color=VkKeyboardColor.PRIMARY)
        keyboard.add_row()
        keyboard.add_button('скам', color=VkKeyboardColor.PRIMARY)
        keyboard.add_button('другое', color=VkKeyboardColor.PRIMARY)
        keyboard.add_row()
        keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
        return keyboard
    
    def get_proof_keyboard(self):
        """Клавиатура для добавления доказательств"""
        keyboard = VkKeyboard(one_time=False)
        keyboard.add_button('📎 Пропустить', color=VkKeyboardColor.SECONDARY)
        keyboard.add_row()
        keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
        return keyboard
    
    def handle_start(self, user_id):
        """Обработка команды /start"""
        if is_user_blocked(user_id):
            self.send_message(user_id, "❌ Вы заблокированы в системе.")
            return
        
        user_info = self.get_user_info(user_id)
        if user_info:
            add_bot_user(
                user_id,
                user_info.get('screen_name', ''),
                user_info.get('first_name', ''),
                user_info.get('last_name', '')
            )
        
        welcome_text = """🎯 Добро пожаловать в бот проверки пользователей!

📝 Жалоба - сообщить о ненадежном пользователе
🔍 Проверить - узнайте информацию о пользователе  
➕ Добавить бота - добавить бота в беседу/группу
ℹ️ Помощь - получите справку по работе бота"""
        
        self.send_message(user_id, welcome_text, self.get_user_keyboard(user_id))
    
    def handle_help(self, user_id):
        """Обработка команды помощи"""
        help_text = """📋 Как пользоваться ботом:

📝 Жалоба - нажмите кнопку и следуйте инструкциям
🔍 Проверить - узнайте информацию о пользователе
➕ Добавить бота - добавьте бота в беседу для проверки участников

⚠️ Внимание: 
- Максимум 5 жалоб в час
- Жалобы проходят модерацию
- В беседах используйте команду /check @username"""
        
        self.send_message(user_id, help_text, self.get_user_keyboard(user_id))
    
    def handle_add_bot(self, user_id):
        """Обработка кнопки добавления бота"""
        add_bot_message = """🤖 VK Бот для проверки пользователей

✅ Добавьте меня в вашу беседу или группу для проверки пользователей!

📋 Как добавить:
1. Откройте меню управления беседой
2. Выберите "Добавить участников"
3. Найдите бота по имени

🎯 Функции в группах:
• Проверка пользователей командой /check @username
• Модерация жалоб
• Безопасность сообщества

📌 Требования:
• Боту нужны права на чтение сообщений
• Для полного функционала дайте права администратора"""
        
        self.send_message(user_id, add_bot_message, self.get_user_keyboard(user_id))
    
    def handle_check_command(self, user_id, message_text):
        """Обработка команды /check"""
        if is_user_blocked(user_id):
            self.send_message(user_id, "❌ Вы заблокированы в системе.")
            return
        
        args = message_text.split()
        if len(args) < 2:
            self.send_message(user_id, "❌ Используйте: /check @username")
            return
        
        username = args[1].strip()
        if username.startswith('@'):
            username = username[1:]
        
        is_valid, validation_msg = validate_username(username)
        if not is_valid:
            self.send_message(user_id, validation_msg)
            return
        
        reports = get_user_reports(username)
        
        if not reports:
            self.send_message(user_id, f"ℹ️ Информация о @{username} не найдена в базе данных.")
        else:
            statuses = set()
            comments = []
            
            for status, comment, timestamp in reports:
                statuses.add(status)
                comments.append(f"• {comment} ({timestamp[:10]})")
            
            response = [
                f"🔍 Информация о @{username}:",
                f"🏷 Статусы: {', '.join(sorted(statuses))}",
                f"📝 Комментарии:",
                *comments[:3],
                f"📊 Всего жалоб: {len(reports)}"
            ]
            
            self.send_message(user_id, "\n".join(response), self.get_user_keyboard(user_id))
    
    def handle_complaint_start(self, user_id):
        """Начало процесса подачи жалобы"""
        if is_user_blocked(user_id):
            self.send_message(user_id, "❌ Вы заблокированы в системе.")
            return
        
        if get_recent_reports_count(user_id) >= MAX_REPORTS_PER_HOUR:
            self.send_message(user_id, f"❌ Максимум {MAX_REPORTS_PER_HOUR} жалоб в час!")
            return
        
        self.user_states[user_id] = {'state': 'waiting_username'}
        self.send_message(user_id, "👤 Введите юзернейм:\n(например: username)", self.get_back_keyboard())
    
    def process_complaint_username(self, user_id, username):
        """Обработка ввода юзернейма для жалобы"""
        if username.startswith('@'):
            username = username[1:]
        
        is_valid, validation_msg = validate_username(username)
        if not is_valid:
            self.send_message(user_id, f"{validation_msg}\nПопробуйте снова:", self.get_back_keyboard())
            return False
        
        user_info = self.get_user_info(user_id)
        current_username = user_info.get('screen_name', '') if user_info else ''
        
        if current_username and current_username.lower() == username.lower():
            self.send_message(user_id, "❌ Нельзя подать жалобу на самого себя!", self.get_user_keyboard(user_id))
            del self.user_states[user_id]
            return False
        
        self.user_states[user_id]['target_username'] = username
        self.user_states[user_id]['state'] = 'waiting_comment'
        self.send_message(user_id, "📝 Введите комментарий:\n(например: «не отправил товар»)", self.get_back_keyboard())
        return True
    
    def process_complaint_comment(self, user_id, comment):
        """Обработка ввода комментария для жалобы"""
        if len(comment) < 5:
            self.send_message(user_id, "❌ Комментарий слишком короткий (минимум 5 символов). Попробуйте снова:", self.get_back_keyboard())
            return False
        
        if len(comment) > 500:
            self.send_message(user_id, "❌ Комментарий слишком длинный (максимум 500 символов). Попробуйте снова:", self.get_back_keyboard())
            return False
        
        self.user_states[user_id]['comment'] = comment
        self.user_states[user_id]['state'] = 'waiting_proof'
        self.send_message(user_id, "📎 Пришлите скриншот или нажмите 'Пропустить':", self.get_proof_keyboard())
        return True
    
    def process_complaint_proof(self, user_id, attachment=None):
        """Обработка добавления доказательств"""
        if attachment:
            self.user_states[user_id]['proof_photo'] = attachment
            self.send_message(user_id, "📸 Доказательство сохранено! Выберите статус:", self.get_status_keyboard())
        else:
            self.user_states[user_id]['proof_photo'] = None
            self.send_message(user_id, "Выберите статус:", self.get_status_keyboard())
        
        self.user_states[user_id]['state'] = 'waiting_status'
    
    def process_complaint_status(self, user_id, status):
        """Обработка выбора статуса жалобы"""
        if status == "🔙 Назад":
            self.user_states[user_id]['state'] = 'waiting_proof'
            self.send_message(user_id, "📎 Пришлите скриншот или нажмите 'Пропустить':", self.get_proof_keyboard())
            return False
        
        if status == "другое":
            self.user_states[user_id]['state'] = 'waiting_custom_status'
            self.send_message(user_id, "✏️ Введите свой вариант статуса:", self.get_back_keyboard())
            return False
        
        self.save_report(user_id, status)
        return True
    
    def process_custom_status(self, user_id, custom_status):
        """Обработка пользовательского статуса"""
        if custom_status == "🔙 Назад":
            self.user_states[user_id]['state'] = 'waiting_status'
            self.send_message(user_id, "Выберите статус:", self.get_status_keyboard())
            return False
        
        if len(custom_status) < 2:
            self.send_message(user_id, "❌ Статус слишком короткий. Введите еще раз:", self.get_back_keyboard())
            return False
        
        self.save_report(user_id, custom_status)
        return True
    
    def save_report(self, user_id, status):
        """Сохранение жалобы в базу данных"""
        data = self.user_states.get(user_id, {})
        
        report_id = add_report(
            user_id,
            data.get('target_username', ''),
            status,
            data.get('comment', ''),
            data.get('proof_photo')
        )
        
        if report_id:
            user_info = self.get_user_info(user_id)
            reporter_name = f"@{user_info.get('screen_name')}" if user_info and user_info.get('screen_name') else f"Пользователь (ID: {user_id})"
            
            admin_text = (f"🆕 Новая жалоба #{report_id}\n\n"
                         f"👤 От кого: {reporter_name}\n"
                         f"🚨 На кого: @{data['target_username']}\n"
                         f"📝 Комментарий: {data['comment']}\n"
                         f"🏷 Статус: {status}")
            
            for admin_id in ADMIN_IDS:
                try:
                    self.send_message(admin_id, admin_text)
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
            
            self.send_message(user_id, "✅ Жалоба отправлена на модерацию!", self.get_user_keyboard(user_id))
        else:
            self.send_message(user_id, "❌ Ошибка сохранения жалобы", self.get_user_keyboard(user_id))
        
        del self.user_states[user_id]
    
    def handle_check_button(self, user_id):
        """Обработка кнопки проверки"""
        if is_user_blocked(user_id):
            self.send_message(user_id, "❌ Вы заблокированы в системе.")
            return
        
        self.user_states[user_id] = {'state': 'waiting_check_username'}
        self.send_message(user_id, "🔍 Введите юзернейм для проверки:", self.get_back_keyboard())
    
    def process_check_username(self, user_id, username):
        """Обработка ввода юзернейма для проверки"""
        if username.startswith('@'):
            username = username[1:]
        
        is_valid, validation_msg = validate_username(username)
        if not is_valid:
            self.send_message(user_id, f"{validation_msg}\nПопробуйте снова:", self.get_back_keyboard())
            return
        
        reports = get_user_reports(username)
        
        if not reports:
            self.send_message(user_id, f"ℹ️ По пользователю @{username} информации нет", self.get_user_keyboard(user_id))
        else:
            statuses = set()
            comments = []
            
            for status, comment, timestamp in reports:
                statuses.add(status)
                comments.append(f"• {comment} ({timestamp[:10]})")
            
            response = [
                f"🔍 Информация о @{username}:",
                f"🏷 Статусы: {', '.join(sorted(statuses))}",
                f"📝 Комментарии:",
                *comments[:3],
                f"📊 Всего жалоб: {len(reports)}"
            ]
            
            self.send_message(user_id, "\n".join(response), self.get_user_keyboard(user_id))
        
        del self.user_states[user_id]
    
    def handle_admin_panel(self, user_id):
        """Открытие панели администратора"""
        if user_id not in ADMIN_IDS:
            self.send_message(user_id, "❌ У вас нет доступа к админ-панели.")
            return
        
        keyboard = VkKeyboard(one_time=False)
        keyboard.add_button('📋 Показать жалобы', color=VkKeyboardColor.PRIMARY)
        keyboard.add_row()
        keyboard.add_button('🚫 Заблокировать', color=VkKeyboardColor.NEGATIVE)
        keyboard.add_button('✅ Разблокировать', color=VkKeyboardColor.POSITIVE)
        keyboard.add_row()
        keyboard.add_button('📢 Сделать объявление', color=VkKeyboardColor.PRIMARY)
        keyboard.add_row()
        keyboard.add_button('🗑️ Удалить информацию', color=VkKeyboardColor.SECONDARY)
        keyboard.add_row()
        keyboard.add_button('📊 Статистика', color=VkKeyboardColor.PRIMARY)
        keyboard.add_row()
        keyboard.add_button('🔄 Уведомление об обновлении', color=VkKeyboardColor.SECONDARY)
        keyboard.add_row()
        keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
        
        self.send_message(user_id, "🛠 Панель администратора\nВыберите действие:", keyboard)
    
    def handle_show_reports(self, user_id):
        """Показать жалобы на модерации"""
        if user_id not in ADMIN_IDS:
            return
        
        pending_reports = get_pending_reports()
        
        if not pending_reports:
            self.send_message(user_id, "📭 Нет жалоб на модерации", self.get_user_keyboard(user_id))
            return
        
        self.send_message(user_id, f"📋 Найдено {len(pending_reports)} жалоб на модерации:")
        
        for report in pending_reports:
            report_id, reporter_id, target_username, status, comment, proof_photo = report
            
            report_text = (f"🆕 Жалоба #{report_id}\n\n"
                          f"👤 От кого: ID {reporter_id}\n"
                          f"🚨 На кого: @{target_username}\n"
                          f"📝 Комментарий: {comment}\n"
                          f"🏷 Статус: {status}")
            
            keyboard = VkKeyboard(one_time=True)
            keyboard.add_button(f'✅ Принять #{report_id}', color=VkKeyboardColor.POSITIVE)
            keyboard.add_button(f'❌ Отклонить #{report_id}', color=VkKeyboardColor.NEGATIVE)
            
            self.send_message(user_id, report_text, keyboard)
    
    def handle_block_user_start(self, user_id):
        """Начало процесса блокировки"""
        if user_id not in ADMIN_IDS:
            return
        
        self.user_states[user_id] = {'state': 'admin_waiting_block_username'}
        self.send_message(user_id, "🚫 Введите @username для блокировки:\n(например: username)", self.get_back_keyboard())
    
    def process_admin_block_username(self, user_id, username):
        """Обработка ввода юзернейма для блокировки"""
        if username.startswith('@'):
            username = username[1:]
        
        target_user_id = get_user_id_by_username(username)
        if not target_user_id:
            self.send_message(user_id, "❌ Пользователь не найден в базе бота.", self.get_user_keyboard(user_id))
            del self.user_states[user_id]
            return
        
        self.user_states[user_id]['target_user_id'] = target_user_id
        self.user_states[user_id]['target_username'] = username
        self.user_states[user_id]['state'] = 'admin_waiting_block_reason'
        self.send_message(user_id, "📝 Введите причину блокировки:", self.get_back_keyboard())
    
    def process_admin_block_reason(self, user_id, reason):
        """Обработка ввода причины блокировки"""
        data = self.user_states.get(user_id, {})
        
        success, result_msg = block_user(data['target_user_id'], data['target_username'], reason, user_id)
        self.send_message(user_id, result_msg, self.get_user_keyboard(user_id))
        
        try:
            self.send_message(data['target_user_id'], f"🚫 Вы заблокированы!\nПричина: {reason}")
        except:
            pass
        
        del self.user_states[user_id]
    
    def handle_unblock_user_start(self, user_id):
        """Начало процесса разблокировки"""
        if user_id not in ADMIN_IDS:
            return
        
        self.user_states[user_id] = {'state': 'admin_waiting_unblock_username'}
        self.send_message(user_id, "✅ Введите @username для разблокировки:\n(например: username)", self.get_back_keyboard())
    
    def process_admin_unblock_username(self, user_id, username):
        """Обработка ввода юзернейма для разблокировки"""
        if username.startswith('@'):
            username = username[1:]
        
        target_user_id = get_user_id_by_username(username)
        if not target_user_id:
            self.send_message(user_id, "❌ Пользователь не найден в базе бота.", self.get_user_keyboard(user_id))
            del self.user_states[user_id]
            return
        
        success, result_msg = unblock_user(target_user_id)
        self.send_message(user_id, result_msg, self.get_user_keyboard(user_id))
        
        try:
            self.send_message(target_user_id, "✅ Вы разблокированы!")
        except:
            pass
        
        del self.user_states[user_id]
    
    def handle_announcement_start(self, user_id):
        """Начало процесса создания объявления"""
        if user_id not in ADMIN_IDS:
            return
        
        self.user_states[user_id] = {'state': 'admin_waiting_announcement'}
        self.send_message(user_id, "📢 Введите текст объявления:", self.get_back_keyboard())
    
    def process_admin_announcement(self, user_id, text):
        """Обработка отправки объявления"""
        users = get_all_users_for_broadcast()
        success_count = 0
        
        self.send_message(user_id, f"📢 Начинаю рассылку для {len(users)} пользователей...")
        
        for target_id in users:
            try:
                if not is_user_blocked(target_id):
                    self.send_message(target_id, f"📢 Объявление:\n\n{text}")
                    success_count += 1
                    time.sleep(0.1)
            except Exception as e:
                logger.error(f"Не удалось отправить пользователю {target_id}: {e}")
        
        self.send_message(user_id, f"📢 Объявление отправлено {success_count} пользователям", self.get_user_keyboard(user_id))
        del self.user_states[user_id]
    
    def handle_delete_user_start(self, user_id):
        """Начало процесса удаления информации о пользователе"""
        if user_id not in ADMIN_IDS:
            return
        
        self.user_states[user_id] = {'state': 'admin_waiting_delete_user'}
        self.send_message(user_id, "🗑️ Введите @username для удаления информации:\n(например: username)", self.get_back_keyboard())
    
    def process_admin_delete_user(self, user_id, username):
        """Обработка удаления информации о пользователе"""
        if username.startswith('@'):
            username = username[1:]
        
        success, result_msg = delete_user_reports(username)
        self.send_message(user_id, result_msg, self.get_user_keyboard(user_id))
        del self.user_states[user_id]
    
    def handle_stats(self, user_id):
        """Показать статистику"""
        if user_id not in ADMIN_IDS:
            return
        
        try:
            query = '''
                SELECT COUNT(*) as total_users,
                       (SELECT COUNT(*) FROM blocked_users) as blocked_users,
                       (SELECT COUNT(*) FROM reports) as total_reports,
                       (SELECT COUNT(*) FROM reports WHERE is_approved = TRUE) as approved_reports,
                       (SELECT COUNT(*) FROM reports WHERE is_approved = FALSE AND is_rejected = FALSE) as pending_reports
                FROM bot_users
            '''
            result = DatabaseConnection.execute_query(query, fetch_one=True)
            
            if result:
                total_users, blocked_users, total_reports, approved_reports, pending_reports = result
                
                stats_text = f"""
📊 Статистика системы:

👥 Пользователей: {total_users}
📨 Всего жалоб: {total_reports}
✅ Одобрено: {approved_reports}
⏳ На модерации: {pending_reports}
🚫 Заблокировано: {blocked_users}
                """
                self.send_message(user_id, stats_text, self.get_user_keyboard(user_id))
        except Exception as e:
            logger.error(f"❌ Ошибка получения статистики: {e}")
            self.send_message(user_id, "❌ Ошибка получения статистики", self.get_user_keyboard(user_id))
    
    def handle_update_notify(self, user_id):
        """Отправка уведомления об обновлении"""
        if user_id not in ADMIN_IDS:
            return
        
        users = get_all_users_for_broadcast()
        success_count = 0
        failed_count = 0
        
        self.send_message(user_id, f"🔄 Начинаю рассылку уведомлений об обновлении для {len(users)} пользователей...")
        
        update_message = """🔄 ОБНОВЛЕНИЕ БОТА ЗАВЕРШЕНО!

✅ Были добавлены новые функции и улучшена стабильность работы

📲 Пожалуйста, перезапустите бота командой /start
чтобы получить доступ ко всем новым возможностям!"""
        
        for target_id in users:
            try:
                if not is_user_blocked(target_id):
                    self.send_message(target_id, update_message)
                    success_count += 1
                    time.sleep(0.05)
            except Exception as e:
                logger.error(f"Не удалось отправить пользователю {target_id}: {e}")
                failed_count += 1
        
        result_message = f"📢 Рассылка завершена!\n\n✅ Успешно: {success_count}\n❌ Ошибок: {failed_count}"
        self.send_message(user_id, result_message, self.get_user_keyboard(user_id))
    
    def handle_approve_report(self, user_id, report_id):
        """Одобрение жалобы"""
        if user_id not in ADMIN_IDS:
            return
        
        reporter_id, target_username = approve_report(report_id, user_id)
        
        if reporter_id:
            try:
                self.send_message(reporter_id, f"✅ Ваша жалоба на @{target_username} одобрена!")
            except:
                pass
            
            self.send_message(user_id, f"✅ Жалоба #{report_id} одобрена")
        else:
            self.send_message(user_id, f"❌ Жалоба #{report_id} уже обработана")
    
    def handle_reject_report(self, user_id, report_id):
        """Отклонение жалобы"""
        if user_id not in ADMIN_IDS:
            return
        
        reporter_id = reject_report(report_id, user_id)
        
        if reporter_id:
            try:
                self.send_message(reporter_id, "❌ Ваша жалоба отклонена.")
            except:
                pass
            
            self.send_message(user_id, f"❌ Жалоба #{report_id} отклонена")
        else:
            self.send_message(user_id, f"❌ Жалоба #{report_id} уже обработана")
    
    def handle_message(self, message):
        """Обработка входящих сообщений"""
        user_id = message['from_id']
        text = message.get('text', '').lower()
        attachments = message.get('attachments', [])
        
        # Обработка команд
        if text == '/start' or text == 'начать':
            self.handle_start(user_id)
            return
        
        # Обработка кнопок админа
        if text == '🛠 админ':
            self.handle_admin_panel(user_id)
            return
        
        if text == '📋 показать жалобы':
            self.handle_show_reports(user_id)
            return
        
        if text == '🚫 заблокировать':
            self.handle_block_user_start(user_id)
            return
        
        if text == '✅ разблокировать':
            self.handle_unblock_user_start(user_id)
            return
        
        if text == '📢 сделать объявление':
            self.handle_announcement_start(user_id)
            return
        
        if text == '🗑️ удалить информацию':
            self.handle_delete_user_start(user_id)
            return
        
        if text == '📊 статистика':
            self.handle_stats(user_id)
            return
        
        if text == '🔄 уведомление об обновлении':
            self.handle_update_notify(user_id)
            return
        
        # Обработка одобрения/отклонения жалоб
        if text.startswith('✅ принять #'):
            try:
                report_id = int(text.split('#')[1])
                self.handle_approve_report(user_id, report_id)
            except:
                pass
            return
        
        if text.startswith('❌ отклонить #'):
            try:
                report_id = int(text.split('#')[1])
                self.handle_reject_report(user_id, report_id)
            except:
                pass
            return
        
        # Обработка команд в группах
        if text.startswith('/check'):
            self.handle_check_command(user_id, text)
            return
        
        # Обработка основных кнопок
        if text == '📝 жалоба':
            self.handle_complaint_start(user_id)
            return
        
        if text == '🔍 проверить':
            self.handle_check_button(user_id)
            return
        
        if text == '➕ добавить бота':
            self.handle_add_bot(user_id)
            return
        
        if text == 'ℹ️ помощь':
            self.handle_help(user_id)
            return
        
        if text == '🔙 назад':
            if user_id in self.user_states:
                del self.user_states[user_id]
            self.send_message(user_id, "🎯 Выберите действие:", self.get_user_keyboard(user_id))
            return
        
        # Обработка состояний
        if user_id in self.user_states:
            state = self.user_states[user_id]['state']
            
            if state == 'waiting_username':
                self.process_complaint_username(user_id, text)
            elif state == 'waiting_comment':
                self.process_complaint_comment(user_id, text)
            elif state == 'waiting_proof':
                if attachments:
                    # Сохраняем вложение как доказательство
                    attachment = f"{attachments[0]['type']}{attachments[0][attachments[0]['type']]['owner_id']}_{attachments[0][attachments[0]['type']]['id']}"
                    self.process_complaint_proof(user_id, attachment)
                elif text == '📎 пропустить':
                    self.process_complaint_proof(user_id)
                else:
                    self.send_message(user_id, "❌ Отправьте фото или нажмите 'Пропустить'", self.get_proof_keyboard())
            elif state == 'waiting_status':
                self.process_complaint_status(user_id, text)
            elif state == 'waiting_custom_status':
                self.process_custom_status(user_id, text)
            elif state == 'waiting_check_username':
                self.process_check_username(user_id, text)
            elif state == 'admin_waiting_block_username':
                self.process_admin_block_username(user_id, text)
            elif state == 'admin_waiting_block_reason':
                self.process_admin_block_reason(user_id, text)
            elif state == 'admin_waiting_unblock_username':
                self.process_admin_unblock_username(user_id, text)
            elif state == 'admin_waiting_announcement':
                self.process_admin_announcement(user_id, text)
            elif state == 'admin_waiting_delete_user':
                self.process_admin_delete_user(user_id, text)
    
    def run(self):
        """Запуск бота"""
        if not self.init_vk():
            logger.critical("❌ Не удалось инициализировать VK API")
            return False
        
        logger.info("🤖 Бот запущен и ожидает сообщения...")
        
        try:
            for event in self.longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    if event.object.message:
                        self.handle_message(event.object.message)
        except Exception as e:
            logger.error(f"❌ Ошибка при работе бота: {e}")
            return False
        
        return True

# === ЗАПУСК БОТА ===
def main():
    """Основная функция запуска"""
    if not init_db():
        logger.critical("❌ Не удалось инициализировать базу данных")
        sys.exit(1)
    
    bot = VKUserBot(VK_TOKEN)
    
    max_restarts = 10
    restart_count = 0
    
    while restart_count < max_restarts:
        try:
            if not bot.run():
                restart_count += 1
                if restart_count < max_restarts:
                    logger.info(f"🔄 Перезапуск через 10 секунд... (попытка {restart_count}/{max_restarts})")
                    time.sleep(10)
                else:
                    logger.critical("❌ Достигнут лимит перезапусков")
                    break
        except KeyboardInterrupt:
            logger.info("⏹️ Бот остановлен пользователем")
            break
        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            restart_count += 1
            if restart_count < max_restarts:
                time.sleep(10)
            else:
                break

if __name__ == "__main__":
    main()
