import asyncio
import logging
import json
import aiosqlite
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from flask import Flask
from threading import Thread

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo, FSInputFile
)
from aiogram.exceptions import TelegramBadRequest

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = "8592304393:AAHOjr7XmPqUGgAfw7eiSNnC2EFCFviU_4w"
ADMIN_IDS = [8351408424, 8429224001]
DB_PATH = "malik_post.db"

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== KEEP-ALIVE ДЛЯ REPLIT ====================
app = Flask('')

@app.route('/')
def home():
    return "MalikPost Bot is alive! 🚀"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ==================== FSM СОСТОЯНИЯ ====================
class PostCreation(StatesGroup):
    select_channel = State()
    add_media = State()
    add_description = State()
    ask_buttons = State()
    buttons_count = State()
    button_names = State()
    button_links = State()
    schedule_time = State()
    preview = State()

class ChannelManagement(StatesGroup):
    add_channel = State()

class AdminPanel(StatesGroup):
    broadcast_message = State()

# ==================== БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_db(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    channel_id INTEGER,
                    channel_name TEXT,
                    is_admin BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    channel_id INTEGER,
                    text TEXT,
                    media TEXT,
                    buttons TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    channel_id INTEGER,
                    text TEXT,
                    media TEXT,
                    buttons TEXT,
                    publish_time TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)

            await db.commit()

    async def add_user(self, user_id: int, username: str = None):
        """Добавить пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            await db.commit()

    async def get_all_users(self) -> List[int]:
        """Получить всех пользователей"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT user_id FROM users") as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    async def add_channel(self, user_id: int, channel_id: int, channel_name: str, is_admin: bool = True):
        """Добавить канал"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO channels (user_id, channel_id, channel_name, is_admin) 
                   VALUES (?, ?, ?, ?)""",
                (user_id, channel_id, channel_name, is_admin)
            )
            await db.commit()

    async def get_user_channels(self, user_id: int) -> List[Dict]:
        """Получить каналы пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, channel_id, channel_name, is_admin FROM channels WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "id": row[0],
                        "channel_id": row[1],
                        "channel_name": row[2],
                        "is_admin": bool(row[3])
                    }
                    for row in rows
                ]

    async def delete_channel(self, channel_db_id: int):
        """Удалить канал"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM channels WHERE id = ?", (channel_db_id,))
            await db.commit()

    async def add_draft(self, user_id: int, channel_id: int, text: str, media: str, buttons: str):
        """Добавить черновик"""
        async with aiosqlite.connect(self.db_path) as db:
            # Проверяем количество черновиков
            async with db.execute(
                "SELECT COUNT(*) FROM drafts WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                count = (await cursor.fetchone())[0]

            # Если больше 5, удаляем самый старый
            if count >= 5:
                await db.execute("""
                    DELETE FROM drafts WHERE id = (
                        SELECT id FROM drafts WHERE user_id = ? 
                        ORDER BY created_at ASC LIMIT 1
                    )
                """, (user_id,))

            # Добавляем новый черновик
            await db.execute(
                """INSERT INTO drafts (user_id, channel_id, text, media, buttons)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, channel_id, text, media, buttons)
            )
            await db.commit()
            return count >= 5  # Возвращаем True если был удален старый черновик

    async def get_user_drafts(self, user_id: int) -> List[Dict]:
        """Получить черновики пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """SELECT d.id, d.channel_id, d.text, d.media, d.buttons, d.created_at, c.channel_name
                   FROM drafts d
                   LEFT JOIN channels c ON d.channel_id = c.channel_id AND d.user_id = c.user_id
                   WHERE d.user_id = ?
                   ORDER BY d.created_at DESC""",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "id": row[0],
                        "channel_id": row[1],
                        "text": row[2],
                        "media": row[3],
                        "buttons": row[4],
                        "created_at": row[5],
                        "channel_name": row[6] or "Неизвестный канал"
                    }
                    for row in rows
                ]

    async def get_draft_by_id(self, draft_id: int) -> Optional[Dict]:
        """Получить черновик по ID"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """SELECT id, channel_id, text, media, buttons, created_at
                   FROM drafts WHERE id = ?""",
                (draft_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "id": row[0],
                        "channel_id": row[1],
                        "text": row[2],
                        "media": row[3],
                        "buttons": row[4],
                        "created_at": row[5]
                    }
                return None

    async def delete_draft(self, draft_id: int):
        """Удалить черновик"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
            await db.commit()

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
db = Database(DB_PATH)

# ==================== КЛАВИАТУРЫ ====================
def get_main_menu(user_id: int) -> InlineKeyboardMarkup:
    """Главное меню"""
    buttons = [
        [InlineKeyboardButton(text="📝 СОЗДАТЬ ПОСТ", callback_data="create_post")],
        [InlineKeyboardButton(text="📢 МОИ КАНАЛЫ", callback_data="my_channels")],
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ КАНАЛ", callback_data="add_channel")],
        [InlineKeyboardButton(text="📋 ЧЕРНОВИКИ", callback_data="drafts")]
    ]

    # Добавляем кнопку админ-панели только для админов
    if user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="👑 АДМИН ПАНЕЛЬ", callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Кнопка отмены"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])

def get_back_cancel_keyboard() -> InlineKeyboardMarkup:
    """Кнопки назад и отмена"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back"),
         InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])

def get_media_keyboard(count: int) -> InlineKeyboardMarkup:
    """Клавиатура для добавления медиа"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"➡️ Продолжить ({count}/5)", callback_data="continue_media")],
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_media")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])

def get_yes_no_keyboard() -> InlineKeyboardMarkup:
    """Кнопки Да/Нет"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ДА", callback_data="yes"),
         InlineKeyboardButton(text="❌ НЕТ", callback_data="no")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back"),
         InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])

def get_buttons_count_keyboard() -> InlineKeyboardMarkup:
    """Выбор количества кнопок"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1", callback_data="btn_count_1"),
         InlineKeyboardButton(text="2", callback_data="btn_count_2"),
         InlineKeyboardButton(text="3", callback_data="btn_count_3")],
        [InlineKeyboardButton(text="5", callback_data="btn_count_5"),
         InlineKeyboardButton(text="10", callback_data="btn_count_10")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back"),
         InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])

def get_schedule_keyboard() -> InlineKeyboardMarkup:
    """Выбор времени публикации"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Сейчас", callback_data="publish_now")],
        [InlineKeyboardButton(text="⏰ Запланировать", callback_data="schedule")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back"),
         InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])

def get_preview_keyboard() -> InlineKeyboardMarkup:
    """Предпросмотр поста"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ОПУБЛИКОВАТЬ", callback_data="confirm_publish")],
        [InlineKeyboardButton(text="💾 СОХРАНИТЬ ЧЕРНОВИК", callback_data="save_draft")],
        [InlineKeyboardButton(text="◀️ РЕДАКТИРОВАТЬ", callback_data="edit_post")],
        [InlineKeyboardButton(text="❌ ОТМЕНИТЬ", callback_data="cancel")]
    ])

async def get_channels_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с каналами пользователя"""
    channels = await db.get_user_channels(user_id)

    if not channels:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
        ])

    buttons = []
    for ch in channels:
        status = "✅" if ch["is_admin"] else "⚠️"
        text = f"{status} {ch['channel_name']}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"select_ch_{ch['id']}")])

    buttons.append([InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_manage_channels_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Управление каналами"""
    channels = await db.get_user_channels(user_id)

    if not channels:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")],
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
        ])

    buttons = []
    for ch in channels:
        status = "✅ Активен" if ch["is_admin"] else "⚠️ Бот не админ"
        buttons.append([
            InlineKeyboardButton(text=f"{ch['channel_name']}", callback_data=f"info_{ch['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"del_ch_{ch['id']}")
        ])

    buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")])
    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    """Админ-панель"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
    ])

# ==================== ХЕНДЛЕРЫ ====================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start"""
    await state.clear()
    await db.add_user(message.from_user.id, message.from_user.username)

    welcome_text = (
        "👋 <b>Добро пожаловать в MalikPost!</b>\n\n"
        "🤖 <b>Я помогу вам:</b>\n"
        "• Создавать красивые посты для Telegram-каналов\n"
        "• Добавлять медиа (фото, видео, GIF)\n"
        "• Планировать публикации\n"
        "• Управлять несколькими каналами\n"
        "• Сохранять черновики\n\n"
        "📝 <b>Выберите действие:</b>"
    )

    await message.answer(welcome_text, reply_markup=get_main_menu(message.from_user.id), parse_mode="HTML")

@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel")
async def cancel_handler(event, state: FSMContext):
    """Отмена любого действия"""
    await state.clear()

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(
            "❌ Действие отменено.\n\n📝 Выберите действие:",
            reply_markup=get_main_menu(event.from_user.id)
        )
        await event.answer()
    else:
        await event.answer(
            "❌ Действие отменено.\n\n📝 Выберите действие:",
            reply_markup=get_main_menu(event.from_user.id)
        )

@router.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    await callback.message.edit_text(
        "📝 <b>Главное меню</b>\n\nВыберите действие:",
        reply_markup=get_main_menu(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()

# ==================== СОЗДАНИЕ ПОСТА ====================

@router.callback_query(F.data == "create_post")
async def create_post_start(callback: CallbackQuery, state: FSMContext):
    """Начало создания поста"""
    channels = await db.get_user_channels(callback.from_user.id)

    if not channels:
        await callback.message.edit_text(
            "⚠️ <b>У вас нет добавленных каналов</b>\n\n"
            "Сначала добавьте канал для публикации постов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")],
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    await state.set_state(PostCreation.select_channel)
    await callback.message.edit_text(
        "📢 <b>Шаг 1/6: Выбор канала</b>\n\n"
        "Выберите канал для публикации поста:",
        reply_markup=await get_channels_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(PostCreation.select_channel, F.data.startswith("select_ch_"))
async def select_channel(callback: CallbackQuery, state: FSMContext):
    """Выбор канала"""
    channel_db_id = int(callback.data.split("_")[2])
    channels = await db.get_user_channels(callback.from_user.id)
    selected_channel = next((ch for ch in channels if ch["id"] == channel_db_id), None)

    if not selected_channel:
        await callback.answer("❌ Канал не найден", show_alert=True)
        return

    await state.update_data(channel=selected_channel, media=[], media_count=0)
    await state.set_state(PostCreation.add_media)

    await callback.message.edit_text(
        f"📢 <b>Канал:</b> {selected_channel['channel_name']}\n\n"
        "📸 <b>Шаг 2/6: Медиафайлы</b>\n\n"
        "Отправьте фото, видео или GIF (до 5 файлов)\n"
        "Или нажмите кнопку для продолжения.",
        reply_markup=get_media_keyboard(0),
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(PostCreation.add_media, F.photo | F.video | F.animation)
async def add_media(message: Message, state: FSMContext):
    """Добавление медиафайлов"""
    data = await state.get_data()
    media = data.get("media", [])

    if len(media) >= 5:
        await message.answer("⚠️ Максимум 5 медиафайлов!")
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        media_type = "video"
    elif message.animation:
        file_id = message.animation.file_id
        media_type = "animation"
    else:
        return

    media.append({"type": media_type, "file_id": file_id})
    await state.update_data(media=media, media_count=len(media))

    await message.answer(
        f"✅ Медиафайл добавлен ({len(media)}/5)\n\n"
        "Отправьте ещё или нажмите 'Продолжить'",
        reply_markup=get_media_keyboard(len(media))
    )

@router.callback_query(PostCreation.add_media, F.data.in_(["continue_media", "skip_media"]))
async def continue_or_skip_media(callback: CallbackQuery, state: FSMContext):
    """Продолжить или пропустить медиа"""
    await state.set_state(PostCreation.add_description)

    await callback.message.edit_text(
        "✍️ <b>Шаг 3/6: Описание</b>\n\n"
        "Введите текст описания для поста (до 4096 символов):",
        reply_markup=get_back_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(PostCreation.add_description, F.text)
async def add_description(message: Message, state: FSMContext):
    """Добавление описания"""
    if len(message.text) > 4096:
        await message.answer("⚠️ Текст слишком длинный! Максимум 4096 символов.")
        return

    await state.update_data(text=message.text)
    await state.set_state(PostCreation.ask_buttons)

    await message.answer(
        "🔘 <b>Шаг 4/6: Кнопки</b>\n\n"
        "Добавить кнопки под постом?",
        reply_markup=get_yes_no_keyboard(),
        parse_mode="HTML"
    )

@router.callback_query(PostCreation.ask_buttons, F.data == "no")
async def skip_buttons(callback: CallbackQuery, state: FSMContext):
    """Пропустить кнопки"""
    await state.update_data(buttons=[])
    await state.set_state(PostCreation.schedule_time)

    await callback.message.edit_text(
        "⏰ <b>Шаг 5/6: Время публикации</b>\n\n"
        "Когда опубликовать пост?",
        reply_markup=get_schedule_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(PostCreation.ask_buttons, F.data == "yes")
async def ask_buttons_count(callback: CallbackQuery, state: FSMContext):
    """Запрос количества кнопок"""
    await state.set_state(PostCreation.buttons_count)

    await callback.message.edit_text(
        "🔢 <b>Количество кнопок</b>\n\n"
        "Сколько кнопок нужно? (максимум 10)",
        reply_markup=get_buttons_count_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(PostCreation.buttons_count, F.data.startswith("btn_count_"))
async def set_buttons_count(callback: CallbackQuery, state: FSMContext):
    """Установка количества кнопок"""
    count = int(callback.data.split("_")[2])
    await state.update_data(buttons_total=count, buttons=[], current_button=1)
    await state.set_state(PostCreation.button_names)

    await callback.message.edit_text(
        f"📝 <b>Название кнопки №1</b>\n\n"
        f"Введите название для кнопки (всего кнопок: {count}):",
        reply_markup=get_back_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(PostCreation.button_names, F.text)
async def add_button_name(message: Message, state: FSMContext):
    """Добавление названия кнопки"""
    data = await state.get_data()
    current = data.get("current_button", 1)
    total = data.get("buttons_total", 1)
    buttons = data.get("buttons", [])

    # Сохраняем название кнопки
    if len(buttons) < current:
        buttons.append({"text": message.text})
    else:
        buttons[current - 1]["text"] = message.text

    await state.update_data(buttons=buttons, current_button_name=message.text)
    await state.set_state(PostCreation.button_links)

    await message.answer(
        f"🔗 <b>Ссылка для кнопки №{current}</b>\n\n"
        f"Введите URL-ссылку для кнопки '{message.text}':",
        reply_markup=get_back_cancel_keyboard(),
        parse_mode="HTML"
    )

@router.message(PostCreation.button_links, F.text)
async def add_button_link(message: Message, state: FSMContext):
    """Добавление ссылки кнопки"""
    url = message.text

    # Простая валидация URL
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer("⚠️ Неверный формат ссылки! Ссылка должна начинаться с http:// или https://")
        return

    data = await state.get_data()
    current = data.get("current_button", 1)
    total = data.get("buttons_total", 1)
    buttons = data.get("buttons", [])

    # Сохраняем ссылку
    buttons[current - 1]["url"] = url
    await state.update_data(buttons=buttons)

    # Если это не последняя кнопка, запрашиваем следующую
    if current < total:
        next_button = current + 1
        await state.update_data(current_button=next_button)
        await state.set_state(PostCreation.button_names)

        await message.answer(
            f"📝 <b>Название кнопки №{next_button}</b>\n\n"
            f"Введите название для кнопки (всего кнопок: {total}):",
            reply_markup=get_back_cancel_keyboard(),
            parse_mode="HTML"
        )
    else:
        # Все кнопки добавлены, переходим к времени публикации
        await state.set_state(PostCreation.schedule_time)
        await message.answer(
            "⏰ <b>Шаг 5/6: Время публикации</b>\n\n"
            "Когда опубликовать пост?",
            reply_markup=get_schedule_keyboard(),
            parse_mode="HTML"
        )

@router.callback_query(PostCreation.schedule_time, F.data == "publish_now")
async def publish_now(callback: CallbackQuery, state: FSMContext):
    """Опубликовать сейчас"""
    await state.update_data(schedule=None)
    await show_preview(callback, state)

async def show_preview(callback: CallbackQuery, state: FSMContext):
    """Показать предпросмотр поста"""
    data = await state.get_data()
    text = data.get("text", "")
    media = data.get("media", [])
    buttons = data.get("buttons", [])
    channel = data.get("channel", {})

    await state.set_state(PostCreation.preview)

    # Формируем клавиатуру с кнопками поста
    post_buttons = []
    if buttons:
        for btn in buttons:
            post_buttons.append([InlineKeyboardButton(text=btn["text"], url=btn["url"])])

    post_keyboard = InlineKeyboardMarkup(inline_keyboard=post_buttons) if post_buttons else None

    preview_text = (
        f"👁 <b>Предпросмотр поста</b>\n\n"
        f"📢 <b>Канал:</b> {channel.get('channel_name', 'Неизвестно')}\n"
        f"📸 <b>Медиа:</b> {len(media)} файл(ов)\n"
        f"🔘 <b>Кнопок:</b> {len(buttons)}\n\n"
        f"<b>Текст:</b>\n{text[:200]}{'...' if len(text) > 200 else ''}"
    )

    # Отправляем предпросмотр
    if media:
        # Если есть медиа, отправляем его с текстом
        if len(media) == 1:
            m = media[0]
            if m["type"] == "photo":
                await callback.message.answer_photo(
                    photo=m["file_id"],
                    caption=text,
                    reply_markup=post_keyboard
                )
            elif m["type"] == "video":
                await callback.message.answer_video(
                    video=m["file_id"],
                    caption=text,
                    reply_markup=post_keyboard
                )
            elif m["type"] == "animation":
                await callback.message.answer_animation(
                    animation=m["file_id"],
                    caption=text,
                    reply_markup=post_keyboard
                )
        else:
            # Несколько медиа - используем media group
            media_group = []
            for i, m in enumerate(media):
                if m["type"] == "photo":
                    media_group.append(InputMediaPhoto(media=m["file_id"], caption=text if i == 0 else None))
                elif m["type"] == "video":
                    media_group.append(InputMediaVideo(media=m["file_id"], caption=text if i == 0 else None))

            await callback.message.answer_media_group(media=media_group)
            if post_buttons:
                await callback.message.answer("Кнопки:", reply_markup=post_keyboard)

    # Отправляем кнопки управления
    await callback.message.answer(
        preview_text,
        reply_markup=get_preview_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(PostCreation.preview, F.data == "confirm_publish")
async def confirm_publish(callback: CallbackQuery, state: FSMContext):
    """Подтверждение публикации"""
    data = await state.get_data()
    text = data.get("text", "")
    media = data.get("media", [])
    buttons = data.get("buttons", [])
    channel = data.get("channel", {})

    # Формируем клавиатуру
    post_buttons = []
    if buttons:
        for btn in buttons:
            post_buttons.append([InlineKeyboardButton(text=btn["text"], url=btn["url"])])

    post_keyboard = InlineKeyboardMarkup(inline_keyboard=post_buttons) if post_buttons else None

    try:
        channel_id = channel["channel_id"]

        # Публикуем пост
        if media:
            if len(media) == 1:
                m = media[0]
                if m["type"] == "photo":
                    await bot.send_photo(
                        chat_id=channel_id,
                        photo=m["file_id"],
                        caption=text,
                        reply_markup=post_keyboard
                    )
                elif m["type"] == "video":
                    await bot.send_video(
                        chat_id=channel_id,
                        video=m["file_id"],
                        caption=text,
                        reply_markup=post_keyboard
                    )
                elif m["type"] == "animation":
                    await bot.send_animation(
                        chat_id=channel_id,
                        animation=m["file_id"],
                        caption=text,
                        reply_markup=post_keyboard
                    )
            else:
                # Несколько медиа
                media_group = []
                for i, m in enumerate(media):
                    if m["type"] == "photo":
                        media_group.append(InputMediaPhoto(media=m["file_id"], caption=text if i == 0 else None))
                    elif m["type"] == "video":
                        media_group.append(InputMediaVideo(media=m["file_id"], caption=text if i == 0 else None))

                await bot.send_media_group(chat_id=channel_id, media=media_group)
                if post_buttons:
                    await bot.send_message(chat_id=channel_id, text="👆 Кнопки к посту:", reply_markup=post_keyboard)
        else:
            # Только текст
            await bot.send_message(
                chat_id=channel_id,
                text=text,
                reply_markup=post_keyboard
            )

        await state.clear()
        await callback.message.answer(
            "✅ <b>Пост успешно опубликован!</b>",
            reply_markup=get_main_menu(callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("✅ Успешно опубликовано!")

    except Exception as e:
        logger.error(f"Error publishing post: {e}")
        await callback.message.answer(
            f"❌ <b>Ошибка публикации:</b>\n{str(e)}\n\n"
            "Проверьте, что бот является администратором канала.",
            reply_markup=get_main_menu(callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("❌ Ошибка публикации", show_alert=True)

@router.callback_query(PostCreation.preview, F.data == "save_draft")
async def save_draft(callback: CallbackQuery, state: FSMContext):
    """Сохранение черновика"""
    data = await state.get_data()
    text = data.get("text", "")
    media = data.get("media", [])
    buttons = data.get("buttons", [])
    channel = data.get("channel", {})

    # Сохраняем в БД
    media_json = json.dumps([{"type": m["type"], "file_id": m["file_id"]} for m in media])
    buttons_json = json.dumps(buttons)

    was_deleted = await db.add_draft(
        callback.from_user.id,
        channel["channel_id"],
        text,
        media_json,
        buttons_json
    )

    await state.clear()

    msg = "💾 <b>Черновик сохранён!</b>"
    if was_deleted:
        msg += "\n\n⚠️ Достигнут лимит черновиков (5). Самый старый черновик был удалён."

    await callback.message.answer(
        msg,
        reply_markup=get_main_menu(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer("💾 Черновик сохранён!")

# ==================== УПРАВЛЕНИЕ КАНАЛАМИ ====================

@router.callback_query(F.data == "my_channels")
async def my_channels(callback: CallbackQuery):
    """Показать мои каналы"""
    channels = await db.get_user_channels(callback.from_user.id)

    if not channels:
        text = "⚠️ <b>У вас нет добавленных каналов</b>\n\nДобавьте канал для начала работы."
    else:
        text = f"📢 <b>Мои каналы ({len(channels)})</b>\n\nУправление каналами:"

    await callback.message.edit_text(
        text,
        reply_markup=await get_manage_channels_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("del_ch_"))
async def delete_channel(callback: CallbackQuery):
    """Удаление канала"""
    channel_db_id = int(callback.data.split("_")[2])
    await db.delete_channel(channel_db_id)

    await callback.answer("🗑 Канал удалён", show_alert=True)
    await my_channels(callback)

@router.callback_query(F.data == "add_channel")
async def add_channel_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления канала"""
    await state.set_state(ChannelManagement.add_channel)

    await callback.message.edit_text(
        "➕ <b>Добавление канала</b>\n\n"
        "Перешлите любое сообщение из канала или отправьте @username канала\n\n"
        "⚠️ Убедитесь, что бот добавлен в канал как администратор!",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(ChannelManagement.add_channel)
async def add_channel_process(message: Message, state: FSMContext):
    """Обработка добавления канала"""
    channel_id = None
    channel_name = None

    # Если переслано сообщение из канала
    if message.forward_from_chat:
        if message.forward_from_chat.type == "channel":
            channel_id = message.forward_from_chat.id
            channel_name = message.forward_from_chat.title

    # Если отправлен username
    elif message.text and message.text.startswith("@"):
        try:
            chat = await bot.get_chat(message.text)
            if chat.type == "channel":
                channel_id = chat.id
                channel_name = chat.title
        except Exception as e:
            await message.answer(f"❌ Ошибка: {str(e)}\n\nПроверьте username канала.")
            return

    if not channel_id:
        await message.answer(
            "⚠️ Не удалось определить канал.\n\n"
            "Перешлите сообщение из канала или отправьте @username канала."
        )
        return

    # Проверяем, является ли бот администратором
    try:
        bot_member = await bot.get_chat_member(channel_id, bot.id)
        is_admin = bot_member.status in ["administrator", "creator"]

        if not is_admin:
            await message.answer(
                "⚠️ <b>Бот не является администратором канала!</b>\n\n"
                f"Добавьте бота @{(await bot.get_me()).username} в канал '{channel_name}' "
                "как администратора с правами на публикацию сообщений.",
                parse_mode="HTML"
            )
            return

        # Добавляем канал в БД
        await db.add_channel(message.from_user.id, channel_id, channel_name, is_admin)
        await state.clear()

        await message.answer(
            f"✅ <b>Канал добавлен!</b>\n\n"
            f"📢 {channel_name}\n"
            f"🆔 {channel_id}",
            reply_markup=get_main_menu(message.from_user.id),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Error adding channel: {e}")
        await message.answer(
            f"❌ <b>Ошибка при добавлении канала:</b>\n{str(e)}\n\n"
            "Убедитесь, что бот добавлен в канал как администратор.",
            parse_mode="HTML"
        )

# ==================== ЧЕРНОВИКИ ====================

@router.callback_query(F.data == "drafts")
async def show_drafts(callback: CallbackQuery):
    """Показать черновики"""
    drafts = await db.get_user_drafts(callback.from_user.id)

    if not drafts:
        await callback.message.edit_text(
            "📋 <b>Черновики</b>\n\n"
            "У вас пока нет сохранённых черновиков.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Формируем список черновиков
    buttons = []
    for draft in drafts:
        date = datetime.fromisoformat(draft["created_at"]).strftime("%d.%m.%Y %H:%M")
        preview = draft["text"][:30] + "..." if len(draft["text"]) > 30 else draft["text"]
        media_count = len(json.loads(draft["media"])) if draft["media"] else 0

        text = f"📅 {date} | {draft['channel_name']}\n{preview}"
        if media_count:
            text += f" | 📸 {media_count}"

        buttons.append([InlineKeyboardButton(text=text, callback_data=f"draft_{draft['id']}")])

    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")])

    await callback.message.edit_text(
        f"📋 <b>Черновики ({len(drafts)}/5)</b>\n\n"
        "Выберите черновик:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("draft_"))
async def show_draft(callback: CallbackQuery, state: FSMContext):
    """Показать черновик"""
    draft_id = int(callback.data.split("_")[1])
    draft = await db.get_draft_by_id(draft_id)

    if not draft:
        await callback.answer("❌ Черновик не найден", show_alert=True)
        return

    # Сохраняем данные черновика в state для публикации
    media = json.loads(draft["media"]) if draft["media"] else []
    buttons = json.loads(draft["buttons"]) if draft["buttons"] else []

    await state.update_data(
        draft_id=draft_id,
        channel_id=draft["channel_id"],
        text=draft["text"],
        media=media,
        buttons=buttons
    )

    # Формируем клавиатуру с кнопками поста
    post_buttons = []
    if buttons:
        for btn in buttons:
            post_buttons.append([InlineKeyboardButton(text=btn["text"], url=btn["url"])])

    post_keyboard = InlineKeyboardMarkup(inline_keyboard=post_buttons) if post_buttons else None

    # Показываем предпросмотр
    preview_text = (
        f"📋 <b>Черновик</b>\n\n"
        f"📅 {datetime.fromisoformat(draft['created_at']).strftime('%d.%m.%Y %H:%M')}\n"
        f"📸 Медиа: {len(media)} файл(ов)\n"
        f"🔘 Кнопок: {len(buttons)}\n\n"
        f"<b>Текст:</b>\n{draft['text'][:200]}{'...' if len(draft['text']) > 200 else ''}"
    )

    # Отправляем предпросмотр с медиа
    if media:
        if len(media) == 1:
            m = media[0]
            if m["type"] == "photo":
                await callback.message.answer_photo(
                    photo=m["file_id"],
                    caption=draft["text"],
                    reply_markup=post_keyboard
                )
            elif m["type"] == "video":
                await callback.message.answer_video(
                    video=m["file_id"],
                    caption=draft["text"],
                    reply_markup=post_keyboard
                )
            elif m["type"] == "animation":
                await callback.message.answer_animation(
                    animation=m["file_id"],
                    caption=draft["text"],
                    reply_markup=post_keyboard
                )
        else:
            media_group = []
            for i, m in enumerate(media):
                if m["type"] == "photo":
                    media_group.append(InputMediaPhoto(media=m["file_id"], caption=draft["text"] if i == 0 else None))
                elif m["type"] == "video":
                    media_group.append(InputMediaVideo(media=m["file_id"], caption=draft["text"] if i == 0 else None))

            await callback.message.answer_media_group(media=media_group)
            if post_buttons:
                await callback.message.answer("Кнопки:", reply_markup=post_keyboard)

    # Кнопки управления черновиком
    manage_buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ОПУБЛИКОВАТЬ", callback_data=f"publish_draft_{draft_id}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ", callback_data=f"delete_draft_{draft_id}")],
        [InlineKeyboardButton(text="◀️ К черновикам", callback_data="drafts")]
    ])

    await callback.message.answer(
        preview_text,
        reply_markup=manage_buttons,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("publish_draft_"))
async def publish_draft(callback: CallbackQuery, state: FSMContext):
    """Публикация черновика"""
    draft_id = int(callback.data.split("_")[2])
    draft = await db.get_draft_by_id(draft_id)

    if not draft:
        await callback.answer("❌ Черновик не найден", show_alert=True)
        return

    media = json.loads(draft["media"]) if draft["media"] else []
    buttons = json.loads(draft["buttons"]) if draft["buttons"] else []

    # Формируем клавиатуру
    post_buttons = []
    if buttons:
        for btn in buttons:
            post_buttons.append([InlineKeyboardButton(text=btn["text"], url=btn["url"])])

    post_keyboard = InlineKeyboardMarkup(inline_keyboard=post_buttons) if post_buttons else None

    try:
        # Публикуем
        if media:
            if len(media) == 1:
                m = media[0]
                if m["type"] == "photo":
                    await bot.send_photo(
                        chat_id=draft["channel_id"],
                        photo=m["file_id"],
                        caption=draft["text"],
                        reply_markup=post_keyboard
                    )
                elif m["type"] == "video":
                    await bot.send_video(
                        chat_id=draft["channel_id"],
                        video=m["file_id"],
                        caption=draft["text"],
                        reply_markup=post_keyboard
                    )
                elif m["type"] == "animation":
                    await bot.send_animation(
                        chat_id=draft["channel_id"],
                        animation=m["file_id"],
                        caption=draft["text"],
                        reply_markup=post_keyboard
                    )
            else:
                media_group = []
                for i, m in enumerate(media):
                    if m["type"] == "photo":
                        media_group.append(InputMediaPhoto(media=m["file_id"], caption=draft["text"] if i == 0 else None))
                    elif m["type"] == "video":
                        media_group.append(InputMediaVideo(media=m["file_id"], caption=draft["text"] if i == 0 else None))

                await bot.send_media_group(chat_id=draft["channel_id"], media=media_group)
                if post_buttons:
                    await bot.send_message(chat_id=draft["channel_id"], text="👆 Кнопки к посту:", reply_markup=post_keyboard)
        else:
            await bot.send_message(
                chat_id=draft["channel_id"],
                text=draft["text"],
                reply_markup=post_keyboard
            )

        # Удаляем черновик после публикации
        await db.delete_draft(draft_id)

        await callback.message.answer(
            "✅ <b>Черновик успешно опубликован!</b>",
            reply_markup=get_main_menu(callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("✅ Опубликовано!")

    except Exception as e:
        logger.error(f"Error publishing draft: {e}")
        await callback.message.answer(
            f"❌ <b>Ошибка публикации:</b>\n{str(e)}",
            reply_markup=get_main_menu(callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("❌ Ошибка публикации", show_alert=True)

@router.callback_query(F.data.startswith("delete_draft_"))
async def delete_draft(callback: CallbackQuery):
    """Удаление черновика"""
    draft_id = int(callback.data.split("_")[2])
    await db.delete_draft(draft_id)

    await callback.answer("🗑 Черновик удалён", show_alert=True)
    await show_drafts(callback)

# ==================== АДМИН ПАНЕЛЬ ====================

@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    """Админ панель"""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔️ Доступ запрещён", show_alert=True)
        return

    await callback.message.edit_text(
        "👑 <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=get_admin_panel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    """Показать статистику"""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔️ Доступ запрещён", show_alert=True)
        return

    users = await db.get_all_users()

    stats_text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: {len(users)}\n"
    )

    await callback.message.edit_text(
        stats_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Админ панель", callback_data="admin_panel")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext):
    """Начало рассылки"""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔️ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminPanel.broadcast_message)
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Отправьте сообщение для рассылки всем пользователям:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(AdminPanel.broadcast_message)
async def broadcast_process(message: Message, state: FSMContext):
    """Обработка рассылки"""
    users = await db.get_all_users()

    success = 0
    failed = 0

    status_msg = await message.answer(
        f"📤 Рассылка началась...\n\n"
        f"✅ Успешно: {success}\n"
        f"❌ Ошибок: {failed}\n"
        f"📊 Всего: {len(users)}"
    )

    for user_id in users:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            success += 1
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast error for user {user_id}: {e}")

        # Обновляем статус каждые 10 пользователей
        if (success + failed) % 10 == 0:
            try:
                await status_msg.edit_text(
                    f"📤 Рассылка в процессе...\n\n"
                    f"✅ Успешно: {success}\n"
                    f"❌ Ошибок: {failed}\n"
                    f"📊 Всего: {len(users)}"
                )
            except:
                pass

        await asyncio.sleep(0.05)  # Небольшая задержка

    await state.clear()
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"✅ Успешно: {success}\n"
        f"❌ Ошибок: {failed}\n"
        f"📊 Всего пользователей: {len(users)}",
        reply_markup=get_admin_panel_keyboard(),
        parse_mode="HTML"
    )

# ==================== ЗАПУСК ====================

async def main():
    """Главная функция"""
    # Инициализация БД
    await db.init_db()
    logger.info("Database initialized")

    # Запуск keep-alive
    keep_alive()
    logger.info("Keep-alive started")

    # Регистрация роутера
    dp.include_router(router)

    # Удаление вебхука
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started polling")

    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:

        logger.info("Bot stopped")
