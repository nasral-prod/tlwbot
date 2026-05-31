"""
=============================================================================
TELEGRAM-БОТ ДЛЯ MINECRAFT СЕРВЕРА "THE LIVING WORLD"
=============================================================================

КАК РАБОТАЮТ ВЕБХУКИ (webhooks):
─────────────────────────────────
Long Polling: бот сам каждые N секунд спрашивает Telegram "есть ли новые
сообщения?" — это требует постоянно работающего процесса в памяти сервера.
На бесплатном Render это ПРОБЛЕМА, т.к. сервис "засыпает" через 15 минут.

Webhook: Telegram САМА отправляет HTTP-запрос на твой сервер при каждом
новом сообщении. Сервер просыпается только когда есть что обработать!
Это идеально для бесплатного хостинга — Render сам "будит" приложение
при входящем запросе.

КАК ЗАДЕПЛОИТЬ НА RENDER:
─────────────────────────
1. Создай аккаунт на render.com
2. New → Web Service → подключи GitHub репозиторий
3. Build Command: pip install -r requirements.txt
4. Start Command: python bot.py
5. Добавь переменные окружения (Environment Variables):
   - TELEGRAM_TOKEN = токен от @BotFather
   - OWNER_ID = твой Telegram ID (узнать у @userinfobot)
   - WEBHOOK_HOST = https://твой-сервис.onrender.com (из Render dashboard)
6. После деплоя вебхук настроится автоматически!

requirements.txt:
─────────────────
aiogram==3.7.0
fastapi==0.110.0
uvicorn==0.29.0
aiohttp==3.9.3

=============================================================================
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# =============================================================================
# НАСТРОЙКИ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# =============================================================================

# Токен бота от @BotFather
TELEGRAM_TOKEN = os.environ.get("8929296662:AAE6cQq-UsMPEMuZjcIxG2ouGWMYpchZyqk", "")

# Telegram ID владельца (получить у @userinfobot)
OWNER_ID = int(os.environ.get("7402154704", "0"))

# Хост для вебхука — должен быть HTTPS URL твоего Render-сервиса
# Пример: https://my-minecraft-bot.onrender.com
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "")

# Порт, на котором запускается сервер (Render автоматически устанавливает PORT)
PORT = int(os.environ.get("PORT", "8000"))

# Путь вебхука включает токен для безопасности — никто случайный не пошлёт запрос
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# Файл базы данных SQLite
DB_FILE = "applications.db"

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================================
# БАЗА ДАННЫХ SQLite
# =============================================================================

def init_db():
    """Инициализация базы данных — создаём таблицы если их нет."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Таблица заявок
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            nickname TEXT,
            answers TEXT,          -- JSON со всеми ответами на вопросы
            status TEXT DEFAULT 'pending',  -- pending / approved / rejected
            created_at TEXT,
            processed_at TEXT,
            rejection_reason TEXT,
            owner_message_id INTEGER  -- ID сообщения у владельца для редактирования
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")


def save_application(user_id: int, username: str, nickname: str, 
                     answers: str, owner_message_id: int) -> int:
    """Сохраняет новую заявку в БД, возвращает её ID."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO applications 
        (user_id, username, nickname, answers, status, created_at, owner_message_id)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
    """, (user_id, username, nickname, answers, 
          datetime.now().strftime("%d.%m.%Y %H:%M"), owner_message_id))
    app_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return app_id


def update_application_status(app_id: int, status: str, reason: str = None):
    """Обновляет статус заявки (approved/rejected) и дату обработки."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE applications 
        SET status = ?, processed_at = ?, rejection_reason = ?
        WHERE id = ?
    """, (status, datetime.now().strftime("%d.%m.%Y %H:%M"), reason, app_id))
    conn.commit()
    conn.close()


def get_application(app_id: int) -> dict:
    """Получает данные заявки по ID."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_stats() -> dict:
    """Возвращает статистику по заявкам."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT status, COUNT(*) FROM applications GROUP BY status")
    rows = cursor.fetchall()
    conn.close()
    stats = {"pending": 0, "approved": 0, "rejected": 0, "total": 0}
    for status, count in rows:
        stats[status] = count
        stats["total"] += count
    return stats


def get_user_pending_application(user_id: int) -> dict:
    """Проверяет, есть ли у пользователя активная (pending) заявка."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM applications WHERE user_id = ? AND status = 'pending'", 
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


# =============================================================================
# FSM (МАШИНА СОСТОЯНИЙ) ДЛЯ АНКЕТЫ
# =============================================================================
# FSM (Finite State Machine) — это способ "запомнить", на каком шаге анкеты
# находится пользователь. Каждый вопрос = отдельное состояние.
# aiogram 3.x хранит состояния в MemoryStorage (в памяти процесса).

class ApplicationForm(StatesGroup):
    """11 состояний = 11 вопросов анкеты."""
    q1_nickname = State()      # Никнейм
    q2_age = State()           # Возраст
    q3_experience = State()    # Опыт в Minecraft
    q4_vanilla = State()       # Опыт на ванильных серверах
    q5_favorite = State()      # Что важно в игре
    q6_frequency = State()     # Частота игры
    q7_rules = State()         # Готов соблюдать правила
    q8_grief = State()         # Что делать при гриферстве
    q9_stolen = State()        # Что делать при краже
    q10_friends = State()      # Друзья на сервере
    q11_why = State()          # Почему выбрал сервер


class WaitingForRejectionReason(StatesGroup):
    """Состояние ожидания причины отклонения от владельца."""
    waiting = State()


# =============================================================================
# ТЕКСТЫ И ВОПРОСЫ АНКЕТЫ
# =============================================================================

RULES_TEXT = """
📜 *ПРАВИЛА СЕРВЕРА THE LIVING WORLD*
━━━━━━━━━━━━━━━━━━━━━━━━

🚫 *ЗАПРЕЩЕНО (бан навсегда):*
• Читы, хаки, x-ray, автокликеры
• Гриферство (разрушение/воровство чужих построек)
• Дюп предметов и использование багов
• Оскорбления по расовому/религиозному признаку
• Реклама других серверов

⚠️ *НАРУШЕНИЯ (предупреждение/мут/темп-бан):*
• Токсичность, флуд, капс
• Намеренная порча ландшафта
• Выдача себя за администрацию

✅ *РАЗРЕШЕНО:*
• Свободное строительство (но не мешать другим)
• PvP только по согласию
• Торговля с игроками

🛡️ *ЗАЩИТА ОТ ГРИФЕРОВ:*
• Все действия логируются
• Администрация восстанавливает постройки

━━━━━━━━━━━━━━━━━━━━━━━━
🌐 *IP:* `thelivingworld.20tps.ru`
🎮 *Версия:* `1.21.1`
"""

# Список вопросов анкеты: (текст вопроса, подсказка для пользователя)
QUESTIONS = [
    (
        "📝 *Вопрос 1 из 11*\n\nНапиши свой игровой никнейм в Minecraft:",
        "Пример: Steve2007"
    ),
    (
        "📝 *Вопрос 2 из 11*\n\nСколько тебе полных лет?",
        "Пример: 16"
    ),
    (
        "📝 *Вопрос 3 из 11*\n\nКак давно ты играешь в Minecraft?",
        "Пример: 3 года, или 6 месяцев"
    ),
    (
        "📝 *Вопрос 4 из 11*\n\nЕсть ли у тебя опыт игры на ванильных серверах?\n_(ответь да/нет и расскажи кратко)_",
        "Пример: Да, играл на сервере XYZ около года"
    ),
    (
        "📝 *Вопрос 5 из 11*\n\nЧто для тебя самое важное в игре?\n_(строительство, PvP, исследование, общение и т.д.)_",
        "Пример: Строительство и общение с другими игроками"
    ),
    (
        "📝 *Вопрос 6 из 11*\n\nКак часто ты планируешь заходить на сервер?",
        "Пример: Каждый день / несколько раз в неделю / по выходным"
    ),
    (
        "📝 *Вопрос 7 из 11*\n\nГотов ли ты соблюдать правила и уважать других игроков? *(да/нет)*",
        "Просто ответь: Да или Нет"
    ),
    (
        "📝 *Вопрос 8 из 11*\n\nЧто ты сделаешь, если увидишь, что кто-то гриферит чужую постройку?",
        "Опиши свои действия"
    ),
    (
        "📝 *Вопрос 9 из 11*\n\nПредставь ситуацию: у тебя украли вещи из сундука. Твои действия?",
        "Опиши свои действия"
    ),
    (
        "📝 *Вопрос 10 из 11*\n\nЕсть ли у тебя друзья, которые тоже хотели бы играть на сервере?\n_Если да — напиши их ники. Если нет — напиши \"нет\"._",
        "Пример: нет / или: CoolPlayer123, GamerBro"
    ),
    (
        "📝 *Вопрос 11 из 11*\n\nПочему ты выбрал именно наш сервер The Living World?",
        "Расскажи своими словами"
    ),
]

# Ключи для хранения ответов в словаре
ANSWER_KEYS = [
    "Никнейм",
    "Возраст",
    "Опыт в Minecraft",
    "Опыт на ванильных серверах",
    "Важное в игре",
    "Частота захода",
    "Готов соблюдать правила",
    "Действия при гриферстве",
    "Действия при краже",
    "Друзья на сервере",
    "Почему наш сервер",
]


# =============================================================================
# КЛАВИАТУРЫ
# =============================================================================

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню с тремя кнопками."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Подать заявку")],
            [KeyboardButton(text="📜 Правила сервера")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def get_back_to_menu_keyboard() -> ReplyKeyboardMarkup:
    """Кнопка возврата в меню."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Назад в меню")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Кнопка отмены анкеты."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить заявку")]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def get_application_keyboard(app_id: int) -> InlineKeyboardMarkup:
    """
    Инлайн-кнопки для заявки у владельца.
    callback_data содержит действие и ID заявки — по ним бот определяет что делать.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять",
                    callback_data=f"approve:{app_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{app_id}"
                ),
            ]
        ]
    )


# =============================================================================
# РОУТЕР И ХЭНДЛЕРЫ
# =============================================================================

router = Router()


# ─── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start — показывает главное меню."""
    await state.clear()  # Сбрасываем любое текущее состояние
    await message.answer(
        f"🌿 Добро пожаловать в систему заявок сервера *The Living World*!\n\n"
        f"Привет, *{message.from_user.first_name}*! 👋\n\n"
        f"Здесь ты можешь подать заявку на вступление, ознакомиться с правилами "
        f"или получить помощь.\n\n"
        f"Выбери действие в меню ниже 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(),
    )


# ─── Правила ──────────────────────────────────────────────────────────────────

@router.message(F.text == "📜 Правила сервера")
async def show_rules(message: Message, state: FSMContext):
    """Показывает правила сервера."""
    await state.clear()
    await message.answer(
        RULES_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_back_to_menu_keyboard(),
    )


# ─── Помощь ───────────────────────────────────────────────────────────────────

@router.message(F.text == "❓ Помощь")
async def show_help(message: Message, state: FSMContext):
    """Показывает справочное сообщение."""
    await state.clear()
    await message.answer(
        "❓ *ПОМОЩЬ*\n\n"
        "Этот бот позволяет подать заявку на сервер *The Living World*.\n\n"
        "📝 *Подать заявку* — заполни анкету из 11 вопросов. После рассмотрения "
        "ты получишь уведомление о решении.\n\n"
        "📜 *Правила* — прочитай правила перед подачей заявки.\n\n"
        "⏱ Рассмотрение заявки обычно занимает до 24 часов.\n\n"
        "🌐 IP сервера: `thelivingworld.20tps.ru`\n"
        "🎮 Версия: `1.21.1`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_back_to_menu_keyboard(),
    )


# ─── Назад в меню ─────────────────────────────────────────────────────────────

@router.message(F.text == "🔙 Назад в меню")
async def back_to_menu(message: Message, state: FSMContext):
    """Возврат в главное меню."""
    await state.clear()
    await message.answer(
        "🏠 Главное меню:",
        reply_markup=get_main_menu_keyboard(),
    )


# ─── Начало анкеты ────────────────────────────────────────────────────────────

@router.message(F.text == "📝 Подать заявку")
async def start_application(message: Message, state: FSMContext):
    """Начинает процесс заполнения анкеты."""
    # Проверяем, нет ли уже активной заявки
    existing = get_user_pending_application(message.from_user.id)
    if existing:
        await message.answer(
            "⏳ У тебя уже есть заявка на рассмотрении!\n"
            "Дождись решения администрации.",
            reply_markup=get_main_menu_keyboard(),
        )
        return
    
    # Инициализируем словарь для хранения ответов в FSM-контексте
    await state.update_data(answers={}, current_question=0)
    
    await message.answer(
        "📋 *Анкета для вступления на сервер The Living World*\n\n"
        "Сейчас тебе будут заданы 11 вопросов. Отвечай честно и развёрнуто — "
        "это поможет нам лучше познакомиться с тобой!\n\n"
        "Для отмены нажми кнопку ниже.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_cancel_keyboard(),
    )
    
    # Задаём первый вопрос и устанавливаем состояние
    await message.answer(
        QUESTIONS[0][0] + f"\n\n_💡 {QUESTIONS[0][1]}_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(ApplicationForm.q1_nickname)


# ─── Отмена анкеты ────────────────────────────────────────────────────────────

@router.message(F.text == "❌ Отменить заявку")
async def cancel_application(message: Message, state: FSMContext):
    """Отмена заполнения анкеты."""
    await state.clear()
    await message.answer(
        "❌ Заявка отменена. Ты всегда можешь подать её позже!",
        reply_markup=get_main_menu_keyboard(),
    )


# =============================================================================
# ОБРАБОТЧИКИ ВОПРОСОВ АНКЕТЫ
# =============================================================================
# Вместо 11 одинаковых функций используем универсальный подход:
# каждый обработчик сохраняет ответ и переходит к следующему вопросу.

async def process_answer(message: Message, state: FSMContext, 
                          question_index: int, next_state):
    """
    Универсальная функция обработки ответа на вопрос анкеты.
    
    question_index — индекс текущего вопроса (0-10)
    next_state — следующее состояние FSM (или None если это последний вопрос)
    """
    answer = message.text.strip()
    
    # Минимальная валидация — ответ не должен быть пустым
    if len(answer) < 1:
        await message.answer("⚠️ Пожалуйста, напиши ответ!")
        return
    
    # Сохраняем ответ в FSM-контексте
    data = await state.get_data()
    answers = data.get("answers", {})
    answers[ANSWER_KEYS[question_index]] = answer
    await state.update_data(answers=answers)
    
    if next_state is not None:
        # Переходим к следующему вопросу
        next_index = question_index + 1
        await state.set_state(next_state)
        await message.answer(
            QUESTIONS[next_index][0] + f"\n\n_💡 {QUESTIONS[next_index][1]}_",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        # Это был последний вопрос — отправляем заявку
        await finish_application(message, state, answers)


# Обработчики для каждого из 11 вопросов:

@router.message(ApplicationForm.q1_nickname)
async def process_q1(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 0, ApplicationForm.q2_age)


@router.message(ApplicationForm.q2_age)
async def process_q2(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 1, ApplicationForm.q3_experience)


@router.message(ApplicationForm.q3_experience)
async def process_q3(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 2, ApplicationForm.q4_vanilla)


@router.message(ApplicationForm.q4_vanilla)
async def process_q4(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 3, ApplicationForm.q5_favorite)


@router.message(ApplicationForm.q5_favorite)
async def process_q5(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 4, ApplicationForm.q6_frequency)


@router.message(ApplicationForm.q6_frequency)
async def process_q6(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 5, ApplicationForm.q7_rules)


@router.message(ApplicationForm.q7_rules)
async def process_q7(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 6, ApplicationForm.q8_grief)


@router.message(ApplicationForm.q8_grief)
async def process_q8(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 7, ApplicationForm.q9_stolen)


@router.message(ApplicationForm.q9_stolen)
async def process_q9(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 8, ApplicationForm.q10_friends)


@router.message(ApplicationForm.q10_friends)
async def process_q10(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    await process_answer(message, state, 9, ApplicationForm.q11_why)


@router.message(ApplicationForm.q11_why)
async def process_q11(message: Message, state: FSMContext):
    if message.text == "❌ Отменить заявку":
        await cancel_application(message, state)
        return
    # None означает — это последний вопрос, отправляем заявку
    await process_answer(message, state, 10, None)


# =============================================================================
# ФИНАЛИЗАЦИЯ ЗАЯВКИ
# =============================================================================

async def finish_application(message: Message, state: FSMContext, answers: dict):
    """
    Завершает анкету: формирует текст заявки, отправляет владельцу,
    сохраняет в БД, уведомляет игрока.
    """
    user = message.from_user
    username = f"@{user.username}" if user.username else "без username"
    
    # Формируем красивый текст заявки для владельца
    answers_text = ""
    for i, (key, value) in enumerate(answers.items(), 1):
        answers_text += f"\n*{i}. {key}:*\n{value}\n"
    
    application_text = (
        f"🆕 *НОВАЯ ЗАЯВКА на The Living World*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *Telegram ID:* `{user.id}`\n"
        f"📱 *Username:* {username}\n"
        f"📅 *Дата:* {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{answers_text}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    
    # Отправляем заявку владельцу в ЛС
    bot = message.bot
    try:
        owner_msg = await bot.send_message(
            chat_id=OWNER_ID,
            text=application_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_application_keyboard(0),  # временный ID, обновим ниже
        )
        
        # Сохраняем заявку в БД (нужен owner_message_id для редактирования)
        import json
        app_id = save_application(
            user_id=user.id,
            username=username,
            nickname=answers.get("Никнейм", ""),
            answers=json.dumps(answers, ensure_ascii=False),
            owner_message_id=owner_msg.message_id,
        )
        
        # Обновляем кнопки с реальным ID заявки
        await bot.edit_message_reply_markup(
            chat_id=OWNER_ID,
            message_id=owner_msg.message_id,
            reply_markup=get_application_keyboard(app_id),
        )
        
        logger.info(f"Новая заявка #{app_id} от пользователя {user.id}")
        
    except Exception as e:
        logger.error(f"Ошибка при отправке заявки владельцу: {e}")
        await message.answer(
            "⚠️ Произошла ошибка при отправке заявки. Попробуй ещё раз позже.",
            reply_markup=get_main_menu_keyboard(),
        )
        await state.clear()
        return
    
    # Сбрасываем состояние и уведомляем игрока
    await state.clear()
    await message.answer(
        "✅ *Заявка успешно отправлена!*\n\n"
        "🕐 Администрация рассмотрит её в ближайшее время.\n"
        "Ты получишь уведомление о решении прямо здесь!\n\n"
        "_Обычно рассмотрение занимает до 24 часов._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(),
    )


# =============================================================================
# ОБРАБОТКА ИНЛАЙН-КНОПОК (ПРИНЯТИЕ / ОТКЛОНЕНИЕ)
# =============================================================================
# Инлайн-кнопки работают через callback_query — когда владелец нажимает кнопку,
# Telegram отправляет боту CallbackQuery с данными из callback_data.

@router.callback_query(F.data.startswith("approve:"))
async def approve_application(callback: CallbackQuery, state: FSMContext):
    """Обработка нажатия кнопки '✅ Принять'."""
    # Проверяем, что нажимает именно владелец
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Только владелец может принимать заявки!", show_alert=True)
        return
    
    app_id = int(callback.data.split(":")[1])
    app = get_application(app_id)
    
    if not app:
        await callback.answer("❌ Заявка не найдена!", show_alert=True)
        return
    
    if app["status"] != "pending":
        await callback.answer("⚠️ Заявка уже обработана!", show_alert=True)
        return
    
    # Обновляем статус в БД
    update_application_status(app_id, "approved")
    
    processed_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    
    # Редактируем сообщение у владельца — убираем кнопки и добавляем пометку
    new_text = (
        callback.message.text + 
        f"\n\n✅ *ПРИНЯТО* | {processed_at}"
    )
    await callback.message.edit_text(
        new_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=None,  # Убираем кнопки
    )
    
    # Уведомляем игрока о принятии
    try:
        await callback.bot.send_message(
            chat_id=app["user_id"],
            text=(
                "🎉 *Поздравляем! Твоя заявка принята!*\n\n"
                "Добро пожаловать на сервер *The Living World*! 🌿\n\n"
                f"🌐 *IP:* `thelivingworld.20tps.ru`\n"
                f"🎮 *Версия:* `1.21.1`\n\n"
                "Удачной игры! Соблюдай правила и наслаждайся! ⚔️🏗️"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить игрока {app['user_id']}: {e}")
    
    await callback.answer("✅ Заявка принята, игрок уведомлён!")
    logger.info(f"Заявка #{app_id} принята")


@router.callback_query(F.data.startswith("reject:"))
async def reject_application_start(callback: CallbackQuery, state: FSMContext):
    """
    Обработка нажатия кнопки '❌ Отклонить'.
    Запрашиваем причину у владельца.
    """
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Только владелец может отклонять заявки!", show_alert=True)
        return
    
    app_id = int(callback.data.split(":")[1])
    app = get_application(app_id)
    
    if not app:
        await callback.answer("❌ Заявка не найдена!", show_alert=True)
        return
    
    if app["status"] != "pending":
        await callback.answer("⚠️ Заявка уже обработана!", show_alert=True)
        return
    
    # Сохраняем ID заявки в FSM-контексте владельца для следующего шага
    await state.update_data(rejecting_app_id=app_id, rejecting_message_id=callback.message.message_id)
    await state.set_state(WaitingForRejectionReason.waiting)
    
    await callback.answer()
    await callback.message.answer(
        f"📝 Укажи причину отклонения заявки #{app_id}:\n"
        f"_(ответь на это сообщение следующим сообщением)_",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(WaitingForRejectionReason.waiting)
async def process_rejection_reason(message: Message, state: FSMContext):
    """Получаем причину отклонения от владельца и завершаем процесс."""
    # Убеждаемся, что это владелец
    if message.from_user.id != OWNER_ID:
        return
    
    reason = message.text.strip()
    data = await state.get_data()
    app_id = data.get("rejecting_app_id")
    owner_message_id = data.get("rejecting_message_id")
    
    await state.clear()
    
    if not app_id:
        await message.answer("⚠️ Ошибка: не найден ID заявки.")
        return
    
    app = get_application(app_id)
    if not app:
        await message.answer("⚠️ Заявка не найдена!")
        return
    
    # Обновляем статус в БД
    update_application_status(app_id, "rejected", reason)
    
    processed_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    
    # Редактируем оригинальное сообщение с заявкой у владельца
    try:
        original_msg = await message.bot.edit_message_text(
            chat_id=OWNER_ID,
            message_id=owner_message_id,
            text=f"[Заявка #{app_id} — см. ниже]\n\n❌ *ОТКЛОНЕНО:* {reason}\n_{processed_at}_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
        )
    except Exception as e:
        logger.error(f"Ошибка редактирования сообщения: {e}")
    
    # Уведомляем игрока об отклонении
    try:
        await message.bot.send_message(
            chat_id=app["user_id"],
            text=(
                "❌ *К сожалению, твоя заявка была отклонена.*\n\n"
                f"📋 *Причина:* {reason}\n\n"
                "Ты можешь подать заявку повторно позже, учтя замечания.\n"
                "Если у тебя есть вопросы, обратись к администрации сервера."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить игрока {app['user_id']}: {e}")
    
    await message.answer(
        f"✅ Заявка #{app_id} отклонена. Игрок уведомлён.\n*Причина:* {reason}",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info(f"Заявка #{app_id} отклонена. Причина: {reason}")


# =============================================================================
# СТАТИСТИКА (только для владельца)
# =============================================================================

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Показывает статистику заявок — только для владельца."""
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Эта команда только для администратора.")
        return
    
    stats = get_stats()
    await message.answer(
        f"📊 *СТАТИСТИКА ЗАЯВОК*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Всего заявок: *{stats['total']}*\n"
        f"✅ Принято: *{stats['approved']}*\n"
        f"❌ Отклонено: *{stats['rejected']}*\n"
        f"⏳ На рассмотрении: *{stats['pending']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
    )


# =============================================================================
# ЗАПУСК БОТА С ВЕБХУКАМИ (FastAPI + uvicorn)
# =============================================================================
# FastAPI — современный Python веб-фреймворк. Мы используем его как HTTP-сервер,
# который будет принимать POST-запросы от Telegram (вебхуки).

# Создаём объекты бота и диспетчера
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan-контекст FastAPI: код до yield выполняется при старте,
    код после yield — при завершении.
    """
    # ── Запуск ──
    logger.info("Запуск бота...")
    init_db()
    
    # Устанавливаем команды меню бота
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="stats", description="Статистика заявок (для владельца)"),
    ])
    
    # Устанавливаем вебхук — говорим Telegram куда присылать обновления
    await bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,  # Игнорируем накопившиеся обновления при рестарте
    )
    logger.info(f"Вебхук установлен: {WEBHOOK_URL}")
    
    yield  # Приложение работает
    
    # ── Завершение ──
    logger.info("Остановка бота...")
    await bot.delete_webhook()
    await bot.session.close()


# Создаём FastAPI-приложение
app = FastAPI(lifespan=lifespan, title="The Living World Bot")


@app.get("/health")
async def health_check():
    """
    Эндпоинт проверки работоспособности.
    Render и другие платформы периодически опрашивают его чтобы убедиться,
    что приложение живо. Ответ 200 OK = всё хорошо.
    """
    return JSONResponse({"status": "ok", "bot": "The Living World Bot"})


@app.post(WEBHOOK_PATH)
async def webhook_handler(request: Request):
    """
    Основной эндпоинт вебхука — сюда Telegram присылает все обновления.
    aiogram сам разберёт JSON и передаст нужному хэндлеру.
    """
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return JSONResponse({"ok": True})


# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================

if __name__ == "__main__":
    # Проверяем обязательные переменные окружения
    if not TELEGRAM_TOKEN:
        logger.error("❌ Не установлена переменная окружения TELEGRAM_TOKEN!")
        exit(1)
    if not OWNER_ID:
        logger.error("❌ Не установлена переменная окружения OWNER_ID!")
        exit(1)
    if not WEBHOOK_HOST:
        logger.error("❌ Не установлена переменная окружения WEBHOOK_HOST!")
        logger.error("   Пример: https://my-minecraft-bot.onrender.com")
        exit(1)
    
    logger.info(f"🚀 Запуск сервера на порту {PORT}")
    logger.info(f"🔗 Вебхук: {WEBHOOK_URL}")
    logger.info(f"👤 ID владельца: {OWNER_ID}")
    
    # Запускаем uvicorn — ASGI-сервер для FastAPI
    uvicorn.run(
        app,           # наше FastAPI-приложение
        host="0.0.0.0",  # слушаем все интерфейсы (обязательно для Render)
        port=PORT,     # порт из переменной окружения
    )
