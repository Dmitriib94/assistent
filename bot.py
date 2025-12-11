"""
Telegram Channel Monitor Bot
Разработано для отслеживания активности канала
"""

import asyncio
import logging
import sqlite3
import json
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from contextlib import asynccontextmanager

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ChatMemberUpdated, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, ChatMemberUpdatedFilter
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# =================== КОНФИГУРАЦИЯ ===================
# ВНИМАНИЕ: Замените эти значения на свои перед деплоем!

# Основной токен бота (получить у @BotFather)
BOT_TOKEN = "8184827957:AAFJIn19PtAn2bB1qqi6U3bFarYfoDcWaoc"

# ID канала в формате @channelname или ID (например: -1001234567890)
CHANNEL_USERNAME = "@dmitriistorik"  # или "CHANNEL_ID"

# Ваш Telegram ID (узнать у @userinfobot)
ADMIN_ID = 5775389281  # Ваш ID здесь

# Дополнительные админы (опционально)
ADDITIONAL_ADMINS = []  # Например: [987654321, 555555555]

# Настройки базы данных
DATABASE_NAME = "channel_monitor.db"

# Настройки логирования
LOG_LEVEL = logging.INFO
LOG_FILE = "bot.log"

# URL для сокращения ссылок (опционально)
URL_SHORTENER_API = ""  # Например: "https://api.short.io/links"

# =================== НАСТРОЙКА ЛОГИРОВАНИЯ ===================
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# =================== ИНИЦИАЛИЗАЦИЯ ===================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Состояния для FSM
class Form(StatesGroup):
    waiting_for_channel = State()
    waiting_for_admin = State()

# =================== БАЗА ДАННЫХ ===================
class DatabaseManager:
    def __init__(self, db_name: str = DATABASE_NAME):
        self.db_name = db_name
        self.init_database()
    
    def get_connection(self):
        return sqlite3.connect(self.db_name, check_same_thread=False)
    
    def init_database(self):
        """Инициализация базы данных с таблицами"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Таблица подписчиков
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            join_date TIMESTAMP,
            last_seen TIMESTAMP,
            country TEXT DEFAULT 'Unknown',
            city TEXT DEFAULT 'Unknown',
            source TEXT DEFAULT 'direct',
            is_bot BOOLEAN DEFAULT 0,
            language_code TEXT DEFAULT 'ru'
        )
        ''')
        
        # Таблица упоминаний
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            message_id INTEGER,
            chat_id INTEGER,
            chat_title TEXT,
            text TEXT,
            mention_date TIMESTAMP,
            is_forward BOOLEAN DEFAULT 0,
            is_reply BOOLEAN DEFAULT 0
        )
        ''')
        
        # Таблица ежедневной статистики
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date DATE PRIMARY KEY,
            joins INTEGER DEFAULT 0,
            leaves INTEGER DEFAULT 0,
            mentions INTEGER DEFAULT 0,
            forwards INTEGER DEFAULT 0,
            replies INTEGER DEFAULT 0,
            unique_visitors INTEGER DEFAULT 0
        )
        ''')
        
        # Таблица источников трафика
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS traffic_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            source TEXT,
            referrer TEXT,
            landing_url TEXT,
            timestamp TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES subscribers (user_id)
        )
        ''')
        
        # Таблица конфигурации
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        ''')
        
        # Индексы для оптимизации
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_subscribers_join_date ON subscribers (join_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_mentions_date ON mentions (mention_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_mentions_user ON mentions (user_id)')
        
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")
    
    async def add_subscriber(self, user: types.User, source: str = "direct") -> bool:
        """Добавление нового подписчика"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Получаем информацию о местоположении (упрощённо)
            country, city = await self.get_user_location(user)
            
            cursor.execute('''
            INSERT OR REPLACE INTO subscribers 
            (user_id, username, first_name, last_name, join_date, last_seen, 
             country, city, source, is_bot, language_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                user.id,
                user.username or "",
                user.first_name,
                user.last_name or "",
                datetime.now(),
                datetime.now(),
                country,
                city,
                source,
                user.is_bot,
                user.language_code or "ru"
            ))
            
            # Обновляем ежедневную статистику
            today = datetime.now().date().isoformat()
            cursor.execute('''
            INSERT OR IGNORE INTO daily_stats (date) VALUES (?)
            ''', (today,))
            cursor.execute('''
            UPDATE daily_stats SET joins = joins + 1 WHERE date = ?
            ''', (today,))
            
            conn.commit()
            conn.close()
            
            logger.info(f"Подписчик добавлен: {user.username or user.first_name} (ID: {user.id})")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при добавлении подписчика: {e}")
            return False
    
    async def remove_subscriber(self, user_id: int) -> Tuple[bool, Optional[Dict]]:
        """Удаление подписчика"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Получаем информацию о пользователе
            cursor.execute('SELECT username, first_name FROM subscribers WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            
            if result:
                username, first_name = result
                
                # Удаляем пользователя
                cursor.execute('DELETE FROM subscribers WHERE user_id = ?', (user_id,))
                
                # Обновляем статистику
                today = datetime.now().date().isoformat()
                cursor.execute('''
                INSERT OR IGNORE INTO daily_stats (date) VALUES (?)
                ''', (today,))
                cursor.execute('''
                UPDATE daily_stats SET leaves = leaves + 1 WHERE date = ?
                ''', (today,))
                
                conn.commit()
                conn.close()
                
                logger.info(f"Подписчик удалён: {username or first_name} (ID: {user_id})")
                return True, {"username": username, "first_name": first_name}
            else:
                conn.close()
                return False, None
                
        except Exception as e:
            logger.error(f"Ошибка при удалении подписчика: {e}")
            return False, None
    
    async def add_mention(self, user: types.User, message: Message, 
                         chat_title: str = "", is_forward: bool = False, 
                         is_reply: bool = False) -> bool:
        """Добавление упоминания/репоста"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Сохраняем текст сообщения (ограничиваем длину)
            text = message.text or message.caption or ""
            if len(text) > 1000:
                text = text[:1000] + "..."
            
            cursor.execute('''
            INSERT INTO mentions 
            (user_id, username, message_id, chat_id, chat_title, text, 
             mention_date, is_forward, is_reply)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                user.id,
                user.username or f"{user.first_name} {user.last_name or ''}",
                message.message_id,
                message.chat.id,
                chat_title,
                text,
                datetime.now(),
                is_forward,
                is_reply
            ))
            
            # Обновляем статистику
            today = datetime.now().date().isoformat()
            cursor.execute('''
            INSERT OR IGNORE INTO daily_stats (date) VALUES (?)
            ''', (today,))
            
            if is_forward:
                cursor.execute('UPDATE daily_stats SET forwards = forwards + 1 WHERE date = ?', (today,))
            elif is_reply:
                cursor.execute('UPDATE daily_stats SET replies = replies + 1 WHERE date = ?', (today,))
            else:
                cursor.execute('UPDATE daily_stats SET mentions = mentions + 1 WHERE date = ?', (today,))
            
            conn.commit()
            conn.close()
            
            action = "репост" if is_forward else "ответ" if is_reply else "упоминание"
            logger.info(f"Зафиксировано {action} от {user.username or user.first_name}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при добавлении упоминания: {e}")
            return False
    
    async def get_subscribers_count(self) -> int:
        """Получение общего количества подписчиков"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM subscribers')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    async def get_today_stats(self) -> Dict:
        """Получение статистики за сегодня"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        today = datetime.now().date().isoformat()
        cursor.execute('''
        SELECT joins, leaves, mentions, forwards, replies 
        FROM daily_stats WHERE date = ?
        ''', (today,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                "joins": result[0],
                "leaves": result[1],
                "mentions": result[2],
                "forwards": result[3],
                "replies": result[4]
            }
        return {"joins": 0, "leaves": 0, "mentions": 0, "forwards": 0, "replies": 0}
    
    async def get_user_location(self, user: types.User) -> Tuple[str, str]:
        """Определение местоположения пользователя (упрощённо)"""
        # В реальном приложении можно использовать IP-определение
        # Здесь возвращаем заглушку
        return ("Не определено", "Не определено")
    
    async def get_top_sources(self, limit: int = 5) -> List[Tuple]:
        """Получение топ источников трафика"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT source, COUNT(*) as count 
        FROM subscribers 
        WHERE source != 'direct'
        GROUP BY source 
        ORDER BY count DESC 
        LIMIT ?
        ''', (limit,))
        
        result = cursor.fetchall()
        conn.close()
        return result

# Инициализация менеджера БД
db = DatabaseManager()

# =================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===================
def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором"""
    admins = [ADMIN_ID] + ADDITIONAL_ADMINS
    return user_id in admins

async def get_channel_info() -> Optional[Dict]:
    """Получение информации о канале"""
    try:
        chat = await bot.get_chat(CHANNEL_USERNAME)
        return {
            "id": chat.id,
            "title": chat.title,
            "username": chat.username,
            "description": chat.description,
            "members_count": chat.get_members_count() if hasattr(chat, 'get_members_count') else 0
        }
    except Exception as e:
        logger.error(f"Ошибка при получении информации о канале: {e}")
        return None

async def format_user_info(user: types.User) -> str:
    """Форматирование информации о пользователе"""
    info = []
    
    if user.username:
        info.append(f"@{user.username}")
    else:
        info.append(f"{user.first_name} {user.last_name or ''}".strip())
    
    info.append(f"ID: <code>{user.id}</code>")
    
    if user.language_code:
        info.append(f"Язык: {user.language_code.upper()}")
    
    if user.is_bot:
        info.append("🤖 Бот")
    
    return "\n".join(info)

def create_message_link(chat_id: int, message_id: int) -> str:
    """Создание ссылки на сообщение"""
    if str(chat_id).startswith('-100'):
        # Для каналов и супергрупп
        channel_id = str(chat_id)[4:]
        return f"https://t.me/c/{channel_id}/{message_id}"
    else:
        # Для чатов
        return f"https://t.me/c/{chat_id}/{message_id}"

# =================== ОБРАБОТЧИКИ СОБЫТИЙ ===================
@dp.chat_member_updated(
    ChatMemberUpdatedFilter(member_status_changed=(ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT))
)
async def handle_chat_member_update(event: ChatMemberUpdated):
    """Обработчик подписок и отписок"""
    try:
        # Проверяем, что это наш канал
        chat = event.chat
        if chat.username and chat.username != CHANNEL_USERNAME.lstrip('@'):
            if str(chat.id) != CHANNEL_USERNAME.lstrip('-'):
                return
        
        user = event.new_chat_member.user if event.new_chat_member else event.old_chat_member.user
        
        # Игнорируем самого бота
        if user.id == (await bot.get_me()).id:
            return
        
        if event.new_chat_member.status == ChatMemberStatus.MEMBER:
            # Новый подписчик
            source = await detect_source(user.id)
            await db.add_subscriber(user, source)
            
            # Отправляем уведомление админу
            channel_info = await get_channel_info()
            total_subs = await db.get_subscribers_count()
            
            message_text = (
                f"🎉 <b>Новый подписчик в канале \"{channel_info['title'] if channel_info else 'вашем канале'}\"!</b>\n\n"
                f"👤 <b>Информация:</b>\n{await format_user_info(user)}\n\n"
                f"📈 <b>Всего подписчиков:</b> {total_subs}\n"
                f"📍 <b>Источник:</b> {source}\n"
                f"⏰ <b>Время:</b> {datetime.now().strftime('%H:%M:%S')}"
            )
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📊 Открыть статистику", 
                    callback_data="stats_main"
                )],
                [InlineKeyboardButton(
                    text="👁️ Профиль пользователя", 
                    url=f"tg://user?id={user.id}"
                )]
            ])
            
            await bot.send_message(ADMIN_ID, message_text, 
                                 parse_mode="HTML", 
                                 reply_markup=keyboard)
            
        elif event.new_chat_member.status == ChatMemberStatus.LEFT:
            # Пользователь отписался
            success, user_info = await db.remove_subscriber(user.id)
            
            if success and user_info:
                total_subs = await db.get_subscribers_count()
                
                message_text = (
                    "😢 <b>Пользователь отписался</b>\n\n"
                    f"👤 <b>Пользователь:</b> {user_info['username'] or user_info['first_name']}\n"
                    f"🆔 <b>ID:</b> <code>{user.id}</code>\n\n"
                    f"📉 <b>Осталось подписчиков:</b> {total_subs}"
                )
                
                await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
                
    except Exception as e:
        logger.error(f"Ошибка в обработчике подписок: {e}")

@dp.message(F.text | F.caption)
async def handle_mentions(message: Message):
    """Обработчик упоминаний и репостов"""
    try:
        user = message.from_user
        
        # Игнорируем сообщения от самого бота
        if user.id == (await bot.get_me()).id:
            return
        
        # Проверяем упоминание нашего канала
        channel_mention = False
        text = message.text or message.caption or ""
        
        if CHANNEL_USERNAME.startswith('@'):
            if CHANNEL_USERNAME.lower() in text.lower():
                channel_mention = True
        else:
            # Если это ID канала, ищем по ID в пересланных сообщениях
            pass
        
        # Проверяем репосты из нашего канала
        is_forward_from_channel = False
        if message.forward_from_chat:
            if message.forward_from_chat.username == CHANNEL_USERNAME.lstrip('@'):
                is_forward_from_channel = True
            elif str(message.forward_from_chat.id) == CHANNEL_USERNAME.lstrip('-'):
                is_forward_from_channel = True
        
        # Проверяем ответы на сообщения из нашего канала
        is_reply_to_channel = False
        if message.reply_to_message and message.reply_to_message.forward_from_chat:
            if message.reply_to_message.forward_from_chat.username == CHANNEL_USERNAME.lstrip('@'):
                is_reply_to_channel = True
        
        # Обрабатываем события
        if channel_mention:
            # Упоминание канала в тексте
            await db.add_mention(user, message, message.chat.title or "", False, False)
            
            message_text = (
                "🔔 <b>Новое упоминание канала!</b>\n\n"
                f"👤 <b>От:</b> {await format_user_info(user)}\n"
                f"💬 <b>Чат:</b> {message.chat.title or 'Без названия'}\n"
                f"📝 <b>Текст:</b>\n<code>{text[:200]}...</code>\n\n"
                f"🔗 <a href='{create_message_link(message.chat.id, message.message_id)}'>Перейти к сообщению</a>"
            )
            
            await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
            
        elif is_forward_from_channel:
            # Репост из канала
            await db.add_mention(user, message, message.chat.title or "", True, False)
            
            message_text = (
                "🔄 <b>Репост вашего поста!</b>\n\n"
                f"👤 <b>От:</b> {await format_user_info(user)}\n"
                f"📢 <b>В:</b> {message.chat.title or 'Без названия'}\n\n"
                f"🔗 <a href='{create_message_link(message.chat.id, message.message_id)}'>Посмотреть репост</a>"
            )
            
            await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
            
        elif is_reply_to_channel:
            # Ответ на сообщение из канала
            await db.add_mention(user, message, message.chat.title or "", False, True)
            
            message_text = (
                "💬 <b>Ответ на ваш пост!</b>\n\n"
                f"👤 <b>От:</b> {await format_user_info(user)}\n"
                f"💭 <b>Текст ответа:</b>\n<code>{text[:200]}...</code>\n\n"
                f"🔗 <a href='{create_message_link(message.chat.id, message.message_id)}'>Перейти к ответу</a>"
            )
            
            await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
            
    except Exception as e:
        logger.error(f"Ошибка в обработчике упоминаний: {e}")

# =================== КОМАНДЫ АДМИНИСТРАТОРА ===================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start"""
    if is_admin(message.from_user.id):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="stats_main")],
            [InlineKeyboardButton(text="👥 Подписчики", callback_data="subscribers_list")],
            [InlineKeyboardButton(text="🔔 Упоминания", callback_data="mentions_list")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_main")]
        ])
        
        await message.answer(
            f"👋 Привет, администратор!\n\n"
            f"Бот отслеживает канал: <b>{CHANNEL_USERNAME}</b>\n\n"
            f"<b>Доступные команды:</b>\n"
            f"/stats - Полная статистика\n"
            f"/subscribers - Список подписчиков\n"
            f"/mentions - Последние упоминания\n"
            f"/export - Экспорт данных\n"
            f"/help - Помощь",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        await message.answer("⚠️ У вас нет доступа к этому боту.")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Команда /stats - отображение статистики"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    
    try:
        # Получаем данные
        total_subs = await db.get_subscribers_count()
        today_stats = await db.get_today_stats()
        top_sources = await db.get_top_sources(3)
        
        # Получаем информацию о канале
        channel_info = await get_channel_info()
        
        # Формируем сообщение
        stats_text = (
            f"📊 <b>СТАТИСТИКА КАНАЛА</b>\n\n"
            f"📢 <b>Канал:</b> {channel_info['title'] if channel_info else CHANNEL_USERNAME}\n"
            f"👥 <b>Всего подписчиков:</b> {total_subs}\n\n"
            f"<b>Сегодня ({datetime.now().strftime('%d.%m.%Y')}):</b>\n"
            f"  ➕ Новые: {today_stats['joins']}\n"
            f"  ➖ Отписались: {today_stats['leaves']}\n"
            f"  🔔 Упоминания: {today_stats['mentions']}\n"
            f"  🔄 Репосты: {today_stats['forwards']}\n"
            f"  💬 Ответы: {today_stats['replies']}\n"
        )
        
        if top_sources:
            stats_text += "\n<b>Топ источников трафика:</b>\n"
            for source, count in top_sources:
                stats_text += f"  • {source}: {count}\n"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="stats_refresh"),
             InlineKeyboardButton(text="📈 Подробнее", callback_data="stats_detailed")]
        ])
        
        await message.answer(stats_text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await message.answer("❌ Ошибка при получении статистики.")

@dp.message(Command("subscribers"))
async def cmd_subscribers(message: Message):
    """Команда /subscribers - список подписчиков"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    
    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        
        # Получаем последних 10 подписчиков
        cursor.execute('''
        SELECT user_id, username, first_name, join_date, source 
        FROM subscribers 
        ORDER BY join_date DESC 
        LIMIT 10
        ''')
        
        subscribers = cursor.fetchall()
        conn.close()
        
        if not subscribers:
            await message.answer("📭 Пока нет подписчиков.")
            return
        
        subs_text = "👥 <b>Последние подписчики:</b>\n\n"
        
        for user_id, username, first_name, join_date, source in subscribers:
            time_ago = datetime.now() - datetime.fromisoformat(join_date)
            hours = int(time_ago.total_seconds() / 3600)
            
            subs_text += (
                f"<b>{first_name}</b> "
                f"(@{username if username else 'нет'})\n"
                f"🆔: <code>{user_id}</code>\n"
                f"⏰ {hours}ч назад | 📍 {source}\n"
                f"{'-'*20}\n"
            )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Полный список", callback_data="subscribers_full")],
            [InlineKeyboardButton(text="📊 Аналитика", callback_data="subscribers_analytics")]
        ])
        
        await message.answer(subs_text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при получении списка подписчиков: {e}")
        await message.answer("❌ Ошибка при получении списка подписчиков.")

@dp.message(Command("mentions"))
async def cmd_mentions(message: Message):
    """Команда /mentions - список упоминаний"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    
    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT username, text, mention_date, is_forward, is_reply 
        FROM mentions 
        ORDER BY mention_date DESC 
        LIMIT 10
        ''')
        
        mentions = cursor.fetchall()
        conn.close()
        
        if not mentions:
            await message.answer("🔕 Пока нет упоминаний.")
            return
        
        mentions_text = "🔔 <b>Последние упоминания:</b>\n\n"
        
        for username, text, mention_date, is_forward, is_reply in mentions:
            time_ago = datetime.now() - datetime.fromisoformat(mention_date)
            hours = int(time_ago.total_seconds() / 3600)
            
            if is_forward:
                type_icon = "🔄"
                type_text = "Репост"
            elif is_reply:
                type_icon = "💬"
                type_text = "Ответ"
            else:
                type_icon = "🔔"
                type_text = "Упоминание"
            
            mentions_text += (
                f"{type_icon} <b>{type_text}</b> от @{username if username else 'скрыт'}\n"
                f"📝 {text[:50]}...\n"
                f"⏰ {hours}ч назад\n"
                f"{'-'*20}\n"
            )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Все упоминания", callback_data="mentions_all")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="mentions_stats")]
        ])
        
        await message.answer(mentions_text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при получении упоминаний: {e}")
        await message.answer("❌ Ошибка при получении упоминаний.")

@dp.message(Command("export"))
async def cmd_export(message: Message):
    """Команда /export - экспорт данных"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    
    try:
        # Создаём файл с данными
        export_data = {
            "export_date": datetime.now().isoformat(),
            "channel": CHANNEL_USERNAME,
            "subscribers": [],
            "mentions": [],
            "stats": []
        }
        
        conn = db.get_connection()
        cursor = conn.cursor()
        
        # Экспорт подписчиков
        cursor.execute('SELECT * FROM subscribers')
        subscribers = cursor.fetchall()
        export_data["subscribers_count"] = len(subscribers)
        
        # Экспорт упоминаний
        cursor.execute('SELECT * FROM mentions')
        mentions = cursor.fetchall()
        export_data["mentions_count"] = len(mentions)
        
        # Экспорт статистики
        cursor.execute('SELECT * FROM daily_stats ORDER BY date DESC LIMIT 30')
        stats = cursor.fetchall()
        export_data["stats"] = stats
        
        conn.close()
        
        # Сохраняем в файл
        filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2, default=str)
        
        # Отправляем файл
        with open(filename, 'rb') as f:
            await message.answer_document(
                types.BufferedInputFile(
                    f.read(),
                    filename=filename
                ),
                caption=f"📁 Экспорт данных канала {CHANNEL_USERNAME}\n"
                       f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
                       f"Подписчиков: {export_data['subscribers_count']}\n"
                       f"Упоминаний: {export_data['mentions_count']}"
            )
        
        # Удаляем временный файл
        import os
        os.remove(filename)
        
    except Exception as e:
        logger.error(f"Ошибка при экспорте данных: {e}")
        await message.answer("❌ Ошибка при экспорте данных.")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help - помощь"""
    if is_admin(message.from_user.id):
        help_text = (
            "📚 <b>Помощь по боту</b>\n\n"
            "<b>Основные команды:</b>\n"
            "/start - Запуск бота\n"
            "/stats - Статистика канала\n"
            "/subscribers - Список подписчиков\n"
            "/mentions - Последние упоминания\n"
            "/export - Экспорт данных в JSON\n"
            "/help - Эта справка\n\n"
            "<b>Что отслеживает бот:</b>\n"
            "✅ Новые подписчики\n"
            "✅ Отписавшиеся\n"
            "✅ Упоминания канала\n"
            "✅ Репосты ваших постов\n"
            "✅ Ответы на ваши посты\n\n"
            "<b>Настройки в коде:</b>\n"
            "BOT_TOKEN - Токен бота\n"
            "CHANNEL_USERNAME - Имя канала\n"
            "ADMIN_ID - Ваш ID Telegram\n"
            "ADDITIONAL_ADMINS - Доп. админы"
        )
        await message.answer(help_text, parse_mode="HTML")
    else:
        await message.answer("❌ У вас нет доступа к этому боту.")

@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    """Команда /ping - проверка работы бота"""
    if is_admin(message.from_user.id):
        start_time = datetime.now()
        
        # Проверяем соединение с каналом
        channel_info = await get_channel_info()
        channel_status = "✅" if channel_info else "❌"
        
        # Проверяем базу данных
        try:
            total_subs = await db.get_subscribers_count()
            db_status = "✅"
        except:
            db_status = "❌"
            total_subs = "Ошибка"
        
        end_time = datetime.now()
        response_time = (end_time - start_time).total_seconds() * 1000
        
        ping_text = (
            f"🏓 <b>PONG!</b>\n\n"
            f"⏱ <b>Время ответа:</b> {response_time:.0f} мс\n"
            f"📅 <b>Дата сервера:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            f"<b>Статус систем:</b>\n"
            f"{channel_status} Канал: {CHANNEL_USERNAME}\n"
            f"{db_status} База данных: {total_subs} подписчиков\n"
            f"✅ Бот активен\n\n"
            f"<i>Версия: 2.0 | Разработано для BotHost</i>"
        )
        
        await message.answer(ping_text, parse_mode="HTML")

# =================== ОБРАБОТЧИКИ CALLBACK-ЗАПРОСОВ ===================
@dp.callback_query(F.data == "stats_main")
async def callback_stats_main(callback: types.CallbackQuery):
    """Обработчик кнопки статистики"""
    if is_admin(callback.from_user.id):
        await cmd_stats(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "stats_refresh")
async def callback_stats_refresh(callback: types.CallbackQuery):
    """Обновление статистики"""
    if is_admin(callback.from_user.id):
        await callback.message.delete()
        await cmd_stats(callback.message)
    await callback.answer("Статистика обновлена")

@dp.callback_query(F.data.startswith("subscribers_"))
async def callback_subscribers(callback: types.CallbackQuery):
    """Обработчик кнопок подписчиков"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён")
        return
    
    action = callback.data.split("_")[1]
    
    if action == "list":
        await cmd_subscribers(callback.message)
    
    await callback.answer()

# =================== ДЕТЕКТОР ИСТОЧНИКОВ ===================
async def detect_source(user_id: int) -> str:
    """
    Определение источника подписки
    В реальном приложении здесь можно добавить логику отслеживания
    по реферальным ссылкам, UTM-меткам и т.д.
    """
    sources = [
        "direct",
        "search",
        "recommendation",
        "mention",
        "repost",
        "advertisement"
    ]
    
    # Простая логика для демонстрации
    import random
    return random.choice(sources)

# =================== ФУНКЦИЯ ЗАПУСКА ===================
async def main():
    """Главная функция запуска бота"""
    logger.info("=" * 50)
    logger.info("Запуск Telegram Channel Monitor Bot")
    logger.info(f"Канал: {CHANNEL_USERNAME}")
    logger.info(f"Админ: {ADMIN_ID}")
    logger.info("=" * 50)
    
    # Проверка конфигурации
    if BOT_TOKEN == "ВАШ_ТОКЕН_БОТА_ЗДЕСЬ":
        logger.error("❌ Токен бота не настроен! Замените BOT_TOKEN в коде.")
        return
    
    if ADMIN_ID == 123456789:
        logger.error("❌ ID администратора не настроен! Замените ADMIN_ID в коде.")
        return
    
    # Проверка подключения к каналу
    try:
        channel_info = await get_channel_info()
        if channel_info:
            logger.info(f"✅ Подключено к каналу: {channel_info['title']}")
        else:
            logger.error(f"❌ Не удалось подключиться к каналу: {CHANNEL_USERNAME}")
            logger.error("Проверьте username/ID канала и права бота")
            return
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке канала: {e}")
        return
    
    # Отправка уведомления о запуске
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✅ <b>Бот запущен!</b>\n\n"
            f"📢 <b>Канал:</b> {CHANNEL_USERNAME}\n"
            f"🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"📊 <b>Статус:</b> Активен\n\n"
            f"<i>Бот начал отслеживание активности канала.</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление о запуске: {e}")
    
    # Запуск поллинга
    logger.info("Бот запущен и готов к работе")
    await dp.start_polling(bot)

# =================== ЗАПУСК ПРИЛОЖЕНИЯ ===================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        # Попытка отправить уведомление об ошибке
        try:
            asyncio.run(bot.send_message(
                ADMIN_ID,
                f"❌ <b>Бот остановлен из-за ошибки!</b>\n\n"
                f"🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
                f"⚠️ <b>Ошибка:</b> {str(e)[:200]}\n\n"
                f"<i>Проверьте логи на сервере.</i>",
                parse_mode="HTML"
            ))
        except:
            pass