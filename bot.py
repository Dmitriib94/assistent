"""
Telegram Channel Monitor Bot
Версия для aiogram 3.3.0
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
from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatMemberUpdated, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, ChatMemberUpdatedFilter, CommandObject
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# =================== КОНФИГУРАЦИЯ ===================
# ЗАМЕНИТЕ ЭТИ ЗНАЧЕНИЯ НА СВОИ!

BOT_TOKEN = "8184827957:AAFJIn19PtAn2bB1qqi6U3bFarYfoDcWaoc"  # Получить у @BotFather
CHANNEL_USERNAME = "@dmitriistorik"  # Имя канала с @
ADMIN_ID = 5775389281  # Ваш ID (узнать у @userinfobot)
ADDITIONAL_ADMINS = []  # Дополнительные админы
DATABASE_NAME = "channel_monitor.db"
LOG_LEVEL = logging.INFO

# =================== НАСТРОЙКА ЛОГИРОВАНИЯ ===================
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# =================== ИНИЦИАЛИЗАЦИЯ ===================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# =================== БАЗА ДАННЫХ ===================
class DatabaseManager:
    def __init__(self, db_name: str = DATABASE_NAME):
        self.db_name = db_name
        self.init_database()
    
    def get_connection(self):
        return sqlite3.connect(self.db_name, check_same_thread=False)
    
    def init_database(self):
        """Инициализация базы данных"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            join_date TIMESTAMP,
            last_seen TIMESTAMP,
            source TEXT DEFAULT 'direct'
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            message_id INTEGER,
            chat_id INTEGER,
            text TEXT,
            mention_date TIMESTAMP,
            type TEXT DEFAULT 'mention'  -- mention, forward, reply
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date DATE PRIMARY KEY,
            joins INTEGER DEFAULT 0,
            leaves INTEGER DEFAULT 0,
            mentions INTEGER DEFAULT 0,
            forwards INTEGER DEFAULT 0,
            replies INTEGER DEFAULT 0
        )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")
    
    async def add_subscriber(self, user: types.User, source: str = "direct"):
        """Добавление нового подписчика"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            INSERT OR REPLACE INTO subscribers 
            (user_id, username, first_name, last_name, join_date, last_seen, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                user.id,
                user.username or "",
                user.first_name,
                user.last_name or "",
                datetime.now(),
                datetime.now(),
                source
            ))
            
            today = datetime.now().date().isoformat()
            cursor.execute('''
            INSERT OR IGNORE INTO daily_stats (date) VALUES (?)
            ''', (today,))
            cursor.execute('''
            UPDATE daily_stats SET joins = joins + 1 WHERE date = ?
            ''', (today,))
            
            conn.commit()
            conn.close()
            logger.info(f"Подписчик добавлен: {user.username or user.first_name}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при добавлении подписчика: {e}")
            return False
    
    async def remove_subscriber(self, user_id: int):
        """Удаление подписчика"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT username, first_name FROM subscribers WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            
            if result:
                username, first_name = result
                
                cursor.execute('DELETE FROM subscribers WHERE user_id = ?', (user_id,))
                
                today = datetime.now().date().isoformat()
                cursor.execute('''
                INSERT OR IGNORE INTO daily_stats (date) VALUES (?)
                ''', (today,))
                cursor.execute('''
                UPDATE daily_stats SET leaves = leaves + 1 WHERE date = ?
                ''', (today,))
                
                conn.commit()
                conn.close()
                logger.info(f"Подписчик удалён: {username or first_name}")
                return {"username": username, "first_name": first_name}
            else:
                conn.close()
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при удалении подписчика: {e}")
            return None
    
    async def add_mention(self, user: types.User, message: Message, mention_type: str = "mention"):
        """Добавление упоминания/репоста"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            text = message.text or message.caption or ""
            if len(text) > 500:
                text = text[:500] + "..."
            
            cursor.execute('''
            INSERT INTO mentions 
            (user_id, username, message_id, chat_id, text, mention_date, type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                user.id,
                user.username or f"{user.first_name} {user.last_name or ''}",
                message.message_id,
                message.chat.id,
                text,
                datetime.now(),
                mention_type
            ))
            
            today = datetime.now().date().isoformat()
            cursor.execute('''
            INSERT OR IGNORE INTO daily_stats (date) VALUES (?)
            ''', (today,))
            
            if mention_type == "forward":
                cursor.execute('UPDATE daily_stats SET forwards = forwards + 1 WHERE date = ?', (today,))
            elif mention_type == "reply":
                cursor.execute('UPDATE daily_stats SET replies = replies + 1 WHERE date = ?', (today,))
            else:
                cursor.execute('UPDATE daily_stats SET mentions = mentions + 1 WHERE date = ?', (today,))
            
            conn.commit()
            conn.close()
            logger.info(f"Добавлено {mention_type} от {user.username or user.first_name}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при добавлении упоминания: {e}")
            return False
    
    async def get_subscribers_count(self):
        """Получение общего количества подписчиков"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM subscribers')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    async def get_today_stats(self):
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

# Инициализация БД
db = DatabaseManager()

# =================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===================
def is_admin(user_id: int):
    """Проверка, является ли пользователь администратором"""
    admins = [ADMIN_ID] + ADDITIONAL_ADMINS
    return user_id in admins

async def get_channel_info():
    """Получение информации о канале"""
    try:
        chat = await bot.get_chat(CHANNEL_USERNAME)
        return {
            "id": chat.id,
            "title": chat.title,
            "username": chat.username
        }
    except Exception as e:
        logger.error(f"Ошибка при получении информации о канале: {e}")
        return None

async def format_user_info(user: types.User):
    """Форматирование информации о пользователе"""
    info = []
    
    if user.username:
        info.append(f"@{user.username}")
    else:
        name = f"{user.first_name} {user.last_name or ''}".strip()
        info.append(name)
    
    info.append(f"ID: <code>{user.id}</code>")
    
    if user.language_code:
        info.append(f"Язык: {user.language_code.upper()}")
    
    if user.is_bot:
        info.append("🤖 Бот")
    
    return "\n".join(info)

def create_message_link(chat_id: int, message_id: int):
    """Создание ссылки на сообщение"""
    if str(chat_id).startswith('-100'):
        channel_id = str(chat_id)[4:]
        return f"https://t.me/c/{channel_id}/{message_id}"
    return f"https://t.me/c/{chat_id}/{message_id}"

# =================== ОБРАБОТЧИКИ СОБЫТИЙ ===================
@dp.chat_member()
async def handle_chat_member_update(event: ChatMemberUpdated):
    """Обработчик подписок и отписок"""
    try:
        # Проверяем, что это наш канал
        chat = event.chat
        if not (chat.username == CHANNEL_USERNAME.lstrip('@') or 
                str(chat.id) == CHANNEL_USERNAME.lstrip('-')):
            return
        
        user = event.new_chat_member.user if event.new_chat_member else event.old_chat_member.user
        
        # Игнорируем самого бота
        if user.id == (await bot.get_me()).id:
            return
        
        # Проверяем статус
        if event.new_chat_member.status == ChatMemberStatus.MEMBER:
            # Новый подписчик
            source = "direct"
            await db.add_subscriber(user, source)
            
            # Отправляем уведомление
            channel_info = await get_channel_info()
            total_subs = await db.get_subscribers_count()
            
            message_text = (
                f"🎉 <b>Новый подписчик!</b>\n\n"
                f"📢 <b>Канал:</b> {channel_info['title'] if channel_info else CHANNEL_USERNAME}\n"
                f"👤 <b>Пользователь:</b>\n{await format_user_info(user)}\n"
                f"📈 <b>Всего подписчиков:</b> {total_subs}\n"
                f"⏰ <b>Время:</b> {datetime.now().strftime('%H:%M:%S')}"
            )
            
            await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
            
        elif event.new_chat_member.status == ChatMemberStatus.LEFT:
            # Пользователь отписался
            user_info = await db.remove_subscriber(user.id)
            
            if user_info:
                total_subs = await db.get_subscribers_count()
                
                message_text = (
                    "😢 <b>Пользователь отписался</b>\n\n"
                    f"👤 <b>Пользователь:</b> {user_info['username'] or user_info['first_name']}\n"
                    f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
                    f"📉 <b>Осталось подписчиков:</b> {total_subs}"
                )
                
                await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
                
    except Exception as e:
        logger.error(f"Ошибка в обработчике подписок: {e}")

@dp.message()
async def handle_all_messages(message: Message):
    """Обработчик всех сообщений для поиска упоминаний"""
    try:
        user = message.from_user
        
        # Игнорируем сообщения от бота
        if not user or user.id == (await bot.get_me()).id:
            return
        
        text = message.text or message.caption or ""
        
        # Проверяем упоминание канала
        if CHANNEL_USERNAME.lower() in text.lower():
            await db.add_mention(user, message, "mention")
            
            message_text = (
                "🔔 <b>Новое упоминание канала!</b>\n\n"
                f"👤 <b>От:</b> {await format_user_info(user)}\n"
                f"💬 <b>Чат:</b> {message.chat.title or 'Без названия'}\n"
                f"📝 <b>Текст:</b>\n<code>{text[:200]}...</code>\n\n"
                f"🔗 <a href='{create_message_link(message.chat.id, message.message_id)}'>Перейти к сообщению</a>"
            )
            
            await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
        
        # Проверяем репосты
        if message.forward_from_chat:
            if (message.forward_from_chat.username == CHANNEL_USERNAME.lstrip('@') or 
                str(message.forward_from_chat.id) == CHANNEL_USERNAME.lstrip('-')):
                await db.add_mention(user, message, "forward")
                
                message_text = (
                    "🔄 <b>Репост вашего поста!</b>\n\n"
                    f"👤 <b>От:</b> {await format_user_info(user)}\n"
                    f"📢 <b>В:</b> {message.chat.title or 'Без названия'}\n\n"
                    f"🔗 <a href='{create_message_link(message.chat.id, message.message_id)}'>Посмотреть репост</a>"
                )
                
                await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
        
        # Проверяем ответы
        if message.reply_to_message:
            reply_msg = message.reply_to_message
            if reply_msg.forward_from_chat:
                if (reply_msg.forward_from_chat.username == CHANNEL_USERNAME.lstrip('@') or 
                    str(reply_msg.forward_from_chat.id) == CHANNEL_USERNAME.lstrip('-')):
                    await db.add_mention(user, message, "reply")
                    
                    message_text = (
                        "💬 <b>Ответ на ваш пост!</b>\n\n"
                        f"👤 <b>От:</b> {await format_user_info(user)}\n"
                        f"💭 <b>Текст ответа:</b>\n<code>{text[:200]}...</code>\n\n"
                        f"🔗 <a href='{create_message_link(message.chat.id, message.message_id)}'>Перейти к ответу</a>"
                    )
                    
                    await bot.send_message(ADMIN_ID, message_text, parse_mode="HTML")
            
    except Exception as e:
        logger.error(f"Ошибка в обработчике сообщений: {e}")

# =================== КОМАНДЫ ===================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start"""
    if is_admin(message.from_user.id):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton(text="👥 Подписчики", callback_data="subscribers")],
            [InlineKeyboardButton(text="🔔 Упоминания", callback_data="mentions")]
        ])
        
        await message.answer(
            f"👋 Привет, администратор!\n\n"
            f"Бот отслеживает канал: <b>{CHANNEL_USERNAME}</b>\n\n"
            f"<b>Доступные команды:</b>\n"
            f"/stats - Статистика\n"
            f"/subscribers - Подписчики\n"
            f"/mentions - Упоминания\n"
            f"/help - Помощь",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        await message.answer("⚠️ У вас нет доступа к этому боту.")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Команда /stats"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    
    try:
        total_subs = await db.get_subscribers_count()
        today_stats = await db.get_today_stats()
        channel_info = await get_channel_info()
        
        stats_text = (
            f"📊 <b>СТАТИСТИКА КАНАЛА</b>\n\n"
            f"📢 <b>Канал:</b> {channel_info['title'] if channel_info else CHANNEL_USERNAME}\n"
            f"👥 <b>Всего подписчиков:</b> {total_subs}\n\n"
            f"<b>Сегодня ({datetime.now().strftime('%d.%m.%Y')}):</b>\n"
            f"  ➕ Новые: {today_stats['joins']}\n"
            f"  ➖ Отписались: {today_stats['leaves']}\n"
            f"  🔔 Упоминания: {today_stats['mentions']}\n"
            f"  🔄 Репосты: {today_stats['forwards']}\n"
            f"  💬 Ответы: {today_stats['replies']}"
        )
        
        await message.answer(stats_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await message.answer("❌ Ошибка при получении статистики.")

@dp.message(Command("subscribers"))
async def cmd_subscribers(message: Message):
    """Команда /subscribers"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    
    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT user_id, username, first_name, join_date 
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
        
        for user_id, username, first_name, join_date in subscribers:
            time_ago = datetime.now() - datetime.fromisoformat(join_date)
            hours = int(time_ago.total_seconds() / 3600)
            
            subs_text += (
                f"<b>{first_name}</b> "
                f"(@{username if username else 'нет'})\n"
                f"🆔: <code>{user_id}</code>\n"
                f"⏰ {hours}ч назад\n"
                f"{'-'*20}\n"
            )
        
        await message.answer(subs_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка при получении списка подписчиков: {e}")
        await message.answer("❌ Ошибка при получении списка подписчиков.")

@dp.message(Command("mentions"))
async def cmd_mentions(message: Message):
    """Команда /mentions"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    
    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT username, text, mention_date, type 
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
        
        for username, text, mention_date, mtype in mentions:
            time_ago = datetime.now() - datetime.fromisoformat(mention_date)
            hours = int(time_ago.total_seconds() / 3600)
            
            if mtype == "forward":
                icon = "🔄"
            elif mtype == "reply":
                icon = "💬"
            else:
                icon = "🔔"
            
            mentions_text += (
                f"{icon} <b>{mtype}</b> от @{username if username else 'скрыт'}\n"
                f"📝 {text[:50]}...\n"
                f"⏰ {hours}ч назад\n"
                f"{'-'*20}\n"
            )
        
        await message.answer(mentions_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка при получении упоминаний: {e}")
        await message.answer("❌ Ошибка при получении упоминаний.")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help"""
    if is_admin(message.from_user.id):
        help_text = (
            "📚 <b>Помощь по боту</b>\n\n"
            "<b>Основные команды:</b>\n"
            "/start - Запуск бота\n"
            "/stats - Статистика канала\n"
            "/subscribers - Список подписчиков\n"
            "/mentions - Последние упоминания\n"
            "/help - Эта справка\n\n"
            "<b>Что отслеживает бот:</b>\n"
            "✅ Новые подписчики\n"
            "✅ Отписавшиеся\n"
            "✅ Упоминания канала\n"
            "✅ Репосты ваших постов\n"
            "✅ Ответы на ваши посты"
        )
        await message.answer(help_text, parse_mode="HTML")
    else:
        await message.answer("❌ У вас нет доступа к этому боту.")

@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    """Команда /ping"""
    if is_admin(message.from_user.id):
        start_time = datetime.now()
        
        channel_info = await get_channel_info()
        channel_status = "✅" if channel_info else "❌"
        
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
            f"✅ Бот активен"
        )
        
        await message.answer(ping_text, parse_mode="HTML")

# =================== ЗАПУСК БОТА ===================
async def main():
    """Главная функция"""
    logger.info("=" * 50)
    logger.info("Запуск Telegram Channel Monitor Bot")
    logger.info(f"Канал: {CHANNEL_USERNAME}")
    logger.info("=" * 50)
    
    # Проверка конфигурации
    if BOT_TOKEN == "ВАШ_ТОКЕН_БОТА_ЗДЕСЬ":
        logger.error("❌ Токен бота не настроен!")
        return
    
    if ADMIN_ID == 123456789:
        logger.error("❌ ID администратора не настроен!")
        return
    
    # Проверка канала
    try:
        channel_info = await get_channel_info()
        if channel_info:
            logger.info(f"✅ Подключено к каналу: {channel_info['title']}")
            
            # Отправляем уведомление о запуске
            await bot.send_message(
                ADMIN_ID,
                f"✅ <b>Бот запущен!</b>\n\n"
                f"📢 <b>Канал:</b> {channel_info['title']}\n"
                f"🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
                f"📊 <b>Статус:</b> Активен",
                parse_mode="HTML"
            )
        else:
            logger.error(f"❌ Не удалось подключиться к каналу")
            return
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке канала: {e}")
        return
    
    # Запуск
    logger.info("Бот запущен и готов к работе")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")

