"""
EasyFoodTrack Bot — полная версия
- Онбординг: если Whoop подключён — берём рост/вес оттуда
- Уведомления по местному времени пользователя
- Вода, калории, Whoop интеграция
"""

import asyncio
import os
import sqlite3
import tempfile
import json
import base64
import aiohttp
from datetime import date, datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from openai import AsyncOpenAI

# ── Конфиг ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "ВАШ_ТОКЕН")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "ВАШ_КЛЮЧ")
WHOOP_CLIENT_ID     = os.getenv("WHOOP_CLIENT_ID", "")
WHOOP_CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")

DB_PATH = "bot.db"

bot    = Bot(token=TELEGRAM_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ── FSM ────────────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
    whoop_or_manual = State()
    gender          = State()
    age             = State()
    weight          = State()
    height          = State()
    activity        = State()
    goal            = State()
    notify_hour     = State()

class ChangeNotify(StatesGroup):
    hour = State()

# ── БД ────────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL, time TEXT NOT NULL,
            food TEXT NOT NULL, calories INTEGER NOT NULL,
            protein REAL DEFAULT 0, fat REAL DEFAULT 0, carbs REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS water (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL, time TEXT NOT NULL,
            amount_ml INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whoop_tokens (
            user_id INTEGER PRIMARY KEY,
            access_token TEXT, refresh_token TEXT, expires_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id           INTEGER PRIMARY KEY,
            gender            TEXT DEFAULT '',
            age               INTEGER DEFAULT 0,
            weight_kg         REAL DEFAULT 0,
            height_cm         REAL DEFAULT 0,
            activity          TEXT DEFAULT 'medium',
            goal              TEXT DEFAULT 'maintain',
            calories_goal     INTEGER DEFAULT 2000,
            calories_deficit  INTEGER DEFAULT 1500,
            calories_surplus  INTEGER DEFAULT 2500,
            water_goal_ml     INTEGER DEFAULT 2500,
            timezone_offset   INTEGER DEFAULT 0,
            notify_hour       INTEGER DEFAULT 21,
            summary_sent_date TEXT DEFAULT '',
            setup_done        INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_setting(user_id, key, default=None):
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(f"SELECT {key} FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(user_id, key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
    conn.execute(f"UPDATE user_settings SET {key}=? WHERE user_id=?", (value, user_id))
    conn.commit()
    conn.close()

def save_meal(user_id, food, calories, protein=0, fat=0, carbs=0):
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO meals (user_id,date,time,food,calories,protein,fat,carbs) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), food, calories, protein, fat, carbs)
    )
    conn.commit(); conn.close()

def save_water(user_id, ml):
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO water (user_id,date,time,amount_ml) VALUES (?,?,?,?)",
        (user_id, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), ml)
    )
    conn.commit(); conn.close()

def get_today_meals(user_id):
    today = date.today().strftime("%Y-%m-%d")
    conn  = sqlite3.connect(DB_PATH)
    rows  = conn.execute(
        "SELECT time,food,calories,protein,fat,carbs FROM meals WHERE user_id=? AND date=? ORDER BY time",
        (user_id, today)
    ).fetchall()
    conn.close()
    return [{"time":r[0],"food":r[1],"calories":r[2],"protein":r[3],"fat":r[4],"carbs":r[5]} for r in rows]

def get_today_water(user_id):
    today = date.today().strftime("%Y-%m-%d")
    conn  = sqlite3.connect(DB_PATH)
    row   = conn.execute(
        "SELECT COALESCE(SUM(amount_ml),0) FROM water WHERE user_id=? AND date=?",
        (user_id, today)
    ).fetchone()
    conn.close()
    return row[0] if row else 0

def get_whoop_token(user_id):
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        "SELECT access_token,refresh_token,expires_at FROM whoop_tokens WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row

def save_whoop_token(user_id, access_token, refresh_token, expires_at):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO whoop_tokens VALUES (?,?,?,?)",
        (user_id, access_token, refresh_token, expires_at)
    )
    conn.commit(); conn.close()

def get_all_active_users():
    conn  = sqlite3.connect(DB_PATH)
    rows  = conn.execute(
        "SELECT user_id, notify_hour, timezone_offset, summary_sent_date FROM user_settings WHERE setup_done=1"
    ).fetchall()
    conn.close()
    return rows

# ── Калории (Миффлин-Сан Жеор) ────────────────────────────────────────────────

ACTIVITY_K = {"low": 1.2, "medium": 1.55, "high": 1.725}

def calculate_calories(gender, age, weight_kg, height_cm, activity, goal):
    if gender == "male":
        bmr = 10*weight_kg + 6.25*height_cm - 5*age + 5
    else:
        bmr = 10*weight_kg + 6.25*height_cm - 5*age - 161
    tdee    = int(bmr * ACTIVITY_K.get(activity, 1.55))
    deficit = tdee - 500
    surplus = tdee + 300
    goal_cal = {"lose": deficit, "maintain": tdee, "gain": surplus}.get(goal, tdee)
    return tdee, deficit, surplus, goal_cal

# ── Whoop API ──────────────────────────────────────────────────────────────────

async def whoop_refresh_token(user_id, refresh_token):
    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.prod.whoop.com/oauth/oauth2/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": WHOOP_CLIENT_ID,
            "client_secret": WHOOP_CLIENT_SECRET,
        }) as r:
            if r.status != 200:
                return None
            d = await r.json()
            new_token = d["access_token"]
            save_whoop_token(user_id, new_token,
                d.get("refresh_token", refresh_token),
                int(datetime.now().timestamp()) + d["expires_in"])
            return new_token

async def get_valid_whoop_token(user_id):
    token_data = get_whoop_token(user_id)
    if not token_data:
        return None
    access_token, refresh_token, expires_at = token_data
    if datetime.now().timestamp() > expires_at - 60:
        access_token = await whoop_refresh_token(user_id, refresh_token)
    return access_token

async def get_whoop_profile(user_id):
    """Получить рост и вес из Whoop"""
    token = await get_valid_whoop_token(user_id)
    if not token:
        return None
    async with aiohttp.ClientSession() as s:
        async with s.get(
            "https://api.prod.whoop.com/developer/v1/user/measurement/body",
            headers={"Authorization": f"Bearer {token}"}
        ) as r:
            if r.status != 200:
                return None
            return await r.json()

async def get_whoop_calories(user_id):
    """Получить расход калорий за сегодня"""
    token = await get_valid_whoop_token(user_id)
    if not token:
        return None
    today = date.today().strftime("%Y-%m-%dT00:00:00.000Z")
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://api.prod.whoop.com/developer/v1/cycle?start={today}&limit=1",
            headers={"Authorization": f"Bearer {token}"}
        ) as r:
            if r.status != 200:
                return None
            d       = await r.json()
            records = d.get("records", [])
            if not records:
                return None
            kj = records[0].get("score", {}).get("kilojoule", 0)
            return int(kj / 4.184) if kj else None

# ── OpenAI ─────────────────────────────────────────────────────────────────────

FOOD_PROMPT = """Ты диетолог-аналитик. Ответь ТОЛЬКО в формате JSON:
{
  "is_food": true,
  "is_water": false,
  "food": "название",
  "weight_g": 100,
  "calories": 200,
  "protein": 10,
  "fat": 5,
  "carbs": 20,
  "comment": ""
}
Если это вода/жидкость без калорий — is_water: true, calories: 0.
Делай разумную оценку порции если вес не указан."""

async def analyze_text(text):
    resp = await client.chat.completions.create(
        model="gpt-4o", response_format={"type": "json_object"},
        messages=[{"role":"system","content":FOOD_PROMPT},{"role":"user","content":text}]
    )
    return json.loads(resp.choices[0].message.content)

async def analyze_image(image_bytes):
    b64  = base64.b64encode(image_bytes).decode()
    resp = await client.chat.completions.create(
        model="gpt-4o", response_format={"type": "json_object"},
        messages=[
            {"role":"system","content":FOOD_PROMPT},
            {"role":"user","content":[
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}},
                {"type":"text","text":"Что это? Определи калории."}
            ]}
        ]
    )
    return json.loads(resp.choices[0].message.content)

async def transcribe_voice(ogg_bytes):
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(ogg_bytes); tmp = f.name
    with open(tmp, "rb") as f:
        t = await client.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
    os.unlink(tmp)
    return t.text

async def detect_water_ml(text):
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role":"system","content":"Определи количество воды в мл. Стакан=250, кружка=300, бутылка=500. Ответь ТОЛЬКО числом."},
            {"role":"user","content":text}
        ]
    )
    try:
        return int(resp.choices[0].message.content.strip())
    except:
        return 250

# ── Форматирование ─────────────────────────────────────────────────────────────

def water_bar(current, goal=2500):
    pct    = min(current / goal, 1.0)
    filled = int(pct * 10)
    return f"{'💧'*filled}{'⬜'*(10-filled)} {current}/{goal} мл"

def calorie_bar(eaten, goal):
    pct    = min(eaten / goal, 1.0)
    filled = int(pct * 10)
    return f"{'🟩'*filled}{'⬜'*(10-filled)} {eaten}/{goal} ккал"

def format_meal(data):
    lines = [
        f"🍽 *{data['food']}*",
        f"⚖️ ~{data.get('weight_g','?')} г  🔥 {data['calories']} ккал",
        f"🥩 Б: {data.get('protein',0):.0f}г  🧈 Ж: {data.get('fat',0):.0f}г  🍞 У: {data.get('carbs',0):.0f}г",
    ]
    if data.get("comment"):
        lines.append(f"💬 _{data['comment']}_")
    return "\n".join(lines)

async def build_summary(user_id):
    meals      = get_today_meals(user_id)
    water_ml   = get_today_water(user_id)
    cal_goal   = get_setting(user_id, "calories_goal") or 2000
    water_goal = get_setting(user_id, "water_goal_ml") or 2500
    goal_type  = get_setting(user_id, "goal") or "maintain"
    whoop_cal  = await get_whoop_calories(user_id)

    total_cal = sum(m["calories"] for m in meals)
    total_p   = sum(m["protein"] or 0 for m in meals)
    total_f   = sum(m["fat"]     or 0 for m in meals)
    total_c   = sum(m["carbs"]   or 0 for m in meals)

    lines = ["📊 *Итог дня*\n"]

    if meals:
        lines.append("🍽 *Питание:*\n```")
        lines.append(f"{'Время':<6} {'Блюдо':<20} {'Ккал':>5}")
        lines.append("─" * 33)
        for m in meals:
            lines.append(f"{m['time']:<6} {m['food'][:19]:<20} {m['calories']:>5}")
        lines.append("─" * 33)
        lines.append(f"{'ИТОГО':<26} {total_cal:>5}\n```")
        lines.append(f"🥩 Б: *{total_p:.0f}г*  🧈 Ж: *{total_f:.0f}г*  🍞 У: *{total_c:.0f}г*\n")
    else:
        lines.append("🍽 Еда сегодня не записана\n")

    goal_labels = {"lose":"похудение 📉","maintain":"поддержание ⚖️","gain":"набор массы 📈"}
    lines.append(f"⚡ *Калорийный баланс* ({goal_labels.get(goal_type,'')})")
    lines.append(calorie_bar(total_cal, cal_goal))
    if whoop_cal:
        balance = whoop_cal - total_cal
        emoji   = "✅" if balance > 0 else "⚠️"
        lines.append(f"💪 Whoop сожжено: *{whoop_cal} ккал*")
        lines.append(f"{emoji} Баланс: *{'−' if balance<0 else '+'}{abs(balance)} ккал*")
    lines.append("")

    lines.append("💧 *Вода:*")
    lines.append(water_bar(water_ml, water_goal))
    left = max(0, water_goal - water_ml)
    lines.append(f"⚠️ Осталось: *{left} мл*" if left > 0 else "✅ Норма воды выполнена!")

    return "\n".join(lines)

# ── Хелпер: клавиатура выбора часового пояса ──────────────────────────────────

def timezone_keyboard():
    zones = [
        ("🇷🇺 Москва (UTC+3)",    3),
        ("🇷🇺 Екатеринбург (UTC+5)", 5),
        ("🇷🇺 Новосибирск (UTC+7)", 7),
        ("🇷🇺 Владивосток (UTC+10)", 10),
        ("🇹🇭 Таиланд (UTC+7)",    7),
        ("🇦🇪 Дубай (UTC+4)",      4),
        ("🇩🇪 Европа (UTC+1/2)",   2),
        ("🇺🇸 Нью-Йорк (UTC-5)",  -5),
    ]
    buttons = []
    for name, offset in zones:
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"tz_{offset}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def notify_hour_keyboard():
    rows = []
    hours = [18, 19, 20, 21, 22, 23]
    row   = []
    for h in hours:
        row.append(InlineKeyboardButton(text=f"{h}:00", callback_data=f"hour_{h}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── ОНБОРДИНГ ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    if get_setting(msg.from_user.id, "setup_done"):
        await msg.answer(
            "👋 Привет!\n\n"
            "📸 Фото / 🎤 Голос / ✍️ Текст → калории\n"
            "💧 'выпил стакан воды' → вода\n\n"
            "/today — еда за сегодня\n"
            "/summary — итог дня\n"
            "/water — вода\n"
            "/goal — изменить цель\n"
            "/notify — время уведомлений\n"
            "/whoop — подключить Whoop\n"
            "/clear — очистить дневник"
        )
        return

    if WHOOP_CLIENT_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подключить Whoop сначала", callback_data="ob_whoop")],
            [InlineKeyboardButton(text="✍️ Ввести данные вручную",    callback_data="ob_manual")],
        ])
        await msg.answer(
            "👋 Привет! Я твой health-трекер.\n\n"
            "Если у тебя есть *Whoop* — подключи его первым. "
            "Я автоматически возьму твой рост и вес оттуда 💪\n\n"
            "Если нет — введём данные вручную.",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await state.set_state(Setup.whoop_or_manual)
    else:
        await start_manual_setup(msg, state)

@dp.callback_query(Setup.whoop_or_manual)
async def ob_whoop_or_manual(call: CallbackQuery, state: FSMContext):
    if call.data == "ob_whoop":
        url = (
            f"https://api.prod.whoop.com/oauth/oauth2/auth"
            f"?client_id={WHOOP_CLIENT_ID}"
            f"&redirect_uri=https://t.me/EasyFoodTrack_bot"
            f"&response_type=code&scope=read:cycles+read:body_measurement"
            f"&state={call.from_user.id}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Авторизоваться в Whoop", url=url)],
            [InlineKeyboardButton(text="✅ Я уже подключил, продолжить", callback_data="ob_whoop_done")],
        ])
        await call.message.edit_text(
            "1️⃣ Нажми кнопку и авторизуйся в Whoop\n"
            "2️⃣ Вернись сюда и нажми *«Я уже подключил»*",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        await call.message.delete()
        await start_manual_setup(call.message, state)

@dp.callback_query(Setup.whoop_or_manual, F.data == "ob_whoop_done")
async def ob_whoop_done(call: CallbackQuery, state: FSMContext):
    uid     = call.from_user.id
    profile = await get_whoop_profile(uid)

    if profile and profile.get("weight_kilogram") and profile.get("height_meter"):
        weight = profile["weight_kilogram"]
        height = profile["height_meter"] * 100
        await state.update_data(
            weight_kg=weight,
            height_cm=height,
            from_whoop=True
        )
        await call.message.edit_text(
            f"✅ Данные из Whoop получены!\n"
            f"⚖️ Вес: *{weight:.1f} кг*\n"
            f"📏 Рост: *{height:.0f} см*\n\n"
            f"Теперь укажи пол:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male"),
                 InlineKeyboardButton(text="👩 Женский",  callback_data="gender_female")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(Setup.gender)
    else:
        await call.message.edit_text(
            "⚠️ Не удалось получить данные из Whoop.\n"
            "Возможно ты ещё не авторизовался. Введём вручную:"
        )
        await start_manual_setup(call.message, state)

async def start_manual_setup(msg, state):
    await state.update_data(from_whoop=False)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male"),
         InlineKeyboardButton(text="👩 Женский",  callback_data="gender_female")]
    ])
    await msg.answer("Укажи пол:", reply_markup=kb)
    await state.set_state(Setup.gender)

@dp.callback_query(Setup.gender, F.data.startswith("gender_"))
async def setup_gender(call: CallbackQuery, state: FSMContext):
    await state.update_data(gender=call.data.split("_")[1])
    await call.message.edit_text("Сколько тебе лет? Напиши число:")
    await state.set_state(Setup.age)

@dp.message(Setup.age)
async def setup_age(msg: Message, state: FSMContext):
    try:
        age = int(msg.text.strip())
        assert 10 < age < 100
        await state.update_data(age=age)
        d = await state.get_data()
        if d.get("from_whoop"):
            # рост и вес уже есть — спрашиваем активность
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛋 Низкая (сидячая работа)",       callback_data="act_low")],
                [InlineKeyboardButton(text="🚶 Средняя (3-5 тренировок/нед)", callback_data="act_medium")],
                [InlineKeyboardButton(text="🏃 Высокая (6-7 тренировок/нед)", callback_data="act_high")],
            ])
            await msg.answer("Уровень физической активности:", reply_markup=kb)
            await state.set_state(Setup.activity)
        else:
            await msg.answer("Сколько весишь? (кг, например: 75)")
            await state.set_state(Setup.weight)
    except:
        await msg.answer("Введи возраст числом, например: 28")

@dp.message(Setup.weight)
async def setup_weight(msg: Message, state: FSMContext):
    try:
        w = float(msg.text.strip().replace(",", "."))
        assert 30 < w < 300
        await state.update_data(weight_kg=w)
        await msg.answer("Какой рост? (см, например: 178)")
        await state.set_state(Setup.height)
    except:
        await msg.answer("Введи вес числом, например: 75")

@dp.message(Setup.height)
async def setup_height(msg: Message, state: FSMContext):
    try:
        h = float(msg.text.strip().replace(",", "."))
        assert 100 < h < 250
        await state.update_data(height_cm=h)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛋 Низкая (сидячая работа)",       callback_data="act_low")],
            [InlineKeyboardButton(text="🚶 Средняя (3-5 тренировок/нед)", callback_data="act_medium")],
            [InlineKeyboardButton(text="🏃 Высокая (6-7 тренировок/нед)", callback_data="act_high")],
        ])
        await msg.answer("Уровень физической активности:", reply_markup=kb)
        await state.set_state(Setup.activity)
    except:
        await msg.answer("Введи рост числом, например: 178")

@dp.callback_query(Setup.activity, F.data.startswith("act_"))
async def setup_activity(call: CallbackQuery, state: FSMContext):
    await state.update_data(activity=call.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Похудеть",       callback_data="goal_lose")],
        [InlineKeyboardButton(text="⚖️ Поддержать вес", callback_data="goal_maintain")],
        [InlineKeyboardButton(text="📈 Набрать массу",  callback_data="goal_gain")],
    ])
    await call.message.edit_text("Какая цель?", reply_markup=kb)
    await state.set_state(Setup.goal)

@dp.callback_query(Setup.goal, F.data.startswith("goal_"))
async def setup_goal_done(call: CallbackQuery, state: FSMContext):
    goal = call.data.split("_")[1]
    d    = await state.get_data()

    tdee, deficit, surplus, goal_cal = calculate_calories(
        d["gender"], d["age"], d["weight_kg"], d["height_cm"], d["activity"], goal
    )
    uid = call.from_user.id
    for k, v in [
        ("gender", d["gender"]), ("age", d["age"]),
        ("weight_kg", d["weight_kg"]), ("height_cm", d["height_cm"]),
        ("activity", d["activity"]), ("goal", goal),
        ("calories_goal", goal_cal), ("calories_deficit", deficit),
        ("calories_surplus", surplus), ("water_goal_ml", 2500),
    ]:
        set_setting(uid, k, v)

    labels = {"lose":"похудение 📉","maintain":"поддержание ⚖️","gain":"набор массы 📈"}
    await call.message.edit_text(
        f"✅ *Норма рассчитана!*\n\n"
        f"📉 Для похудения: *{deficit} ккал/день*\n"
        f"⚖️ Для поддержания: *{tdee} ккал/день*\n"
        f"📈 Для набора массы: *{surplus} ккал/день*\n\n"
        f"🎯 Твоя цель ({labels[goal]}): *{goal_cal} ккал/день*\n\n"
        f"Теперь настроим уведомления. Выбери свой часовой пояс:",
        reply_markup=timezone_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(Setup.notify_hour)

@dp.callback_query(Setup.notify_hour, F.data.startswith("tz_"))
async def setup_tz(call: CallbackQuery, state: FSMContext):
    offset = int(call.data.split("_")[1])
    await state.update_data(tz_offset=offset)
    await call.message.edit_text(
        f"✅ Часовой пояс: UTC{'+' if offset>=0 else ''}{offset}\n\n"
        f"Во сколько присылать вечернюю сводку?",
        reply_markup=notify_hour_keyboard()
    )

@dp.callback_query(Setup.notify_hour, F.data.startswith("hour_"))
async def setup_hour(call: CallbackQuery, state: FSMContext):
    hour = int(call.data.split("_")[1])
    d    = await state.get_data()
    uid  = call.from_user.id

    set_setting(uid, "timezone_offset", d.get("tz_offset", 0))
    set_setting(uid, "notify_hour", hour)
    set_setting(uid, "setup_done", 1)
    await state.clear()

    tz_offset = d.get("tz_offset", 0)
    await call.message.edit_text(
        f"🎉 *Всё готово!*\n\n"
        f"🔔 Буду присылать сводку в *{hour}:00* (UTC{'+' if tz_offset>=0 else ''}{tz_offset})\n\n"
        f"Отправляй фото еды, голосовые или текст — я считаю калории!\n"
        f"💧 'выпил стакан воды' — записываю воду\n\n"
        f"/summary — итог дня\n"
        f"/goal — изменить цель\n"
        f"/notify — изменить время уведомлений",
        parse_mode="Markdown"
    )

# ── /notify — изменить время уведомлений ──────────────────────────────────────

@dp.message(Command("notify"))
async def cmd_notify(msg: Message, state: FSMContext):
    uid     = msg.from_user.id
    cur_h   = get_setting(uid, "notify_hour") or 21
    cur_tz  = get_setting(uid, "timezone_offset") or 0
    await msg.answer(
        f"🔔 Сейчас уведомление в *{cur_h}:00* (UTC{'+' if cur_tz>=0 else ''}{cur_tz})\n\n"
        f"Выбери новый часовой пояс:",
        reply_markup=timezone_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(ChangeNotify.hour)

@dp.callback_query(ChangeNotify.hour, F.data.startswith("tz_"))
async def change_tz(call: CallbackQuery, state: FSMContext):
    offset = int(call.data.split("_")[1])
    await state.update_data(tz_offset=offset)
    await call.message.edit_text(
        f"✅ UTC{'+' if offset>=0 else ''}{offset}\n\nВо сколько присылать сводку?",
        reply_markup=notify_hour_keyboard()
    )

@dp.callback_query(ChangeNotify.hour, F.data.startswith("hour_"))
async def change_hour(call: CallbackQuery, state: FSMContext):
    hour = int(call.data.split("_")[1])
    d    = await state.get_data()
    uid  = call.from_user.id
    tz   = d.get("tz_offset", 0)
    set_setting(uid, "timezone_offset", tz)
    set_setting(uid, "notify_hour", hour)
    await state.clear()
    await call.message.edit_text(
        f"✅ Буду присылать сводку в *{hour}:00* (UTC{'+' if tz>=0 else ''}{tz})",
        parse_mode="Markdown"
    )

# ── /goal ──────────────────────────────────────────────────────────────────────

@dp.message(Command("goal"))
async def cmd_goal(msg: Message):
    uid     = msg.from_user.id
    tdee    = get_setting(uid, "calories_goal")    or 2000
    deficit = get_setting(uid, "calories_deficit") or tdee - 500
    surplus = get_setting(uid, "calories_surplus") or tdee + 300
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📉 Похудеть ({deficit} ккал/день)",      callback_data="setgoal_lose")],
        [InlineKeyboardButton(text=f"⚖️ Поддержать вес ({tdee} ккал/день)",   callback_data="setgoal_maintain")],
        [InlineKeyboardButton(text=f"📈 Набрать массу ({surplus} ккал/день)", callback_data="setgoal_gain")],
        [InlineKeyboardButton(text="🔄 Пересчитать (изменились данные)",       callback_data="setgoal_recalc")],
    ])
    await msg.answer("🎯 Выбери цель:", reply_markup=kb)

@dp.callback_query(F.data.startswith("setgoal_"))
async def cb_setgoal(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[1]
    uid    = call.from_user.id
    if action == "recalc":
        await call.message.edit_text(
            "Пересчитаем! Выбери пол:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male"),
                 InlineKeyboardButton(text="👩 Женский",  callback_data="gender_female")]
            ])
        )
        await state.update_data(from_whoop=False)
        await state.set_state(Setup.gender)
        return
    goal_map = {
        "lose":     get_setting(uid, "calories_deficit") or 1500,
        "maintain": get_setting(uid, "calories_goal")    or 2000,
        "gain":     get_setting(uid, "calories_surplus") or 2500,
    }
    set_setting(uid, "goal", action)
    set_setting(uid, "calories_goal", goal_map[action])
    labels = {"lose":"похудение 📉","maintain":"поддержание ⚖️","gain":"набор массы 📈"}
    await call.message.edit_text(
        f"✅ Цель: *{labels[action]}*\n🎯 Норма: *{goal_map[action]} ккал/день*",
        parse_mode="Markdown"
    )

# ── Основные команды ───────────────────────────────────────────────────────────

@dp.message(Command("today"))
async def cmd_today(msg: Message):
    meals = get_today_meals(msg.from_user.id)
    if not meals:
        await msg.answer("📭 Сегодня записей нет.")
        return
    total = sum(m["calories"] for m in meals)
    rows  = ["📅 *Еда за сегодня*\n```",
             f"{'Время':<6} {'Блюдо':<20} {'Ккал':>5}", "─"*33]
    for m in meals:
        rows.append(f"{m['time']:<6} {m['food'][:19]:<20} {m['calories']:>5}")
    rows += ["─"*33, f"{'ИТОГО':<26} {total:>5}", "```"]
    await msg.answer("\n".join(rows), parse_mode="Markdown")

@dp.message(Command("summary"))
async def cmd_summary(msg: Message):
    wait = await msg.answer("📊 Собираю итог...")
    await wait.edit_text(await build_summary(msg.from_user.id), parse_mode="Markdown")

@dp.message(Command("water"))
async def cmd_water(msg: Message):
    water = get_today_water(msg.from_user.id)
    goal  = get_setting(msg.from_user.id, "water_goal_ml") or 2500
    await msg.answer(f"💧 *Вода за сегодня*\n\n{water_bar(water, goal)}", parse_mode="Markdown")

@dp.message(Command("whoop"))
async def cmd_whoop(msg: Message):
    if not WHOOP_CLIENT_ID:
        await msg.answer("⚠️ Whoop не настроен.\nДобавь в Railway:\n`WHOOP_CLIENT_ID`\n`WHOOP_CLIENT_SECRET`", parse_mode="Markdown")
        return
    if get_whoop_token(msg.from_user.id):
        await msg.answer("✅ Whoop уже подключён!")
        return
    url = (
        f"https://api.prod.whoop.com/oauth/oauth2/auth?client_id={WHOOP_CLIENT_ID}"
        f"&redirect_uri=https://t.me/EasyFoodTrack_bot&response_type=code"
        f"&scope=read:cycles+read:body_measurement&state={msg.from_user.id}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Подключить Whoop", url=url)]])
    await msg.answer("Нажми и авторизуйся в Whoop:", reply_markup=kb)

@dp.message(Command("clear"))
async def cmd_clear(msg: Message):
    today = date.today().strftime("%Y-%m-%d")
    conn  = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM meals WHERE user_id=? AND date=?", (msg.from_user.id, today))
    conn.execute("DELETE FROM water WHERE user_id=? AND date=?", (msg.from_user.id, today))
    conn.commit(); conn.close()
    await msg.answer("🗑 Дневник за сегодня очищен.")

# ── Обработка сообщений ────────────────────────────────────────────────────────

@dp.message(F.photo)
async def handle_photo(msg: Message):
    wait = await msg.answer("🔍 Анализирую фото...")
    try:
        file = await bot.get_file(msg.photo[-1].file_id)
        url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                img = await r.read()
        data  = await analyze_image(img)
        save_meal(msg.from_user.id, data["food"], data["calories"],
                  data.get("protein",0), data.get("fat",0), data.get("carbs",0))
        total = sum(m["calories"] for m in get_today_meals(msg.from_user.id))
        goal  = get_setting(msg.from_user.id, "calories_goal") or 2000
        await wait.edit_text(format_meal(data)+f"\n\n{calorie_bar(total,goal)}", parse_mode="Markdown")
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.voice)
async def handle_voice(msg: Message):
    wait = await msg.answer("🎙 Распознаю...")
    try:
        file = await bot.get_file(msg.voice.file_id)
        url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                ogg = await r.read()
        text = await transcribe_voice(ogg)
        await wait.edit_text(f"📝 _{text}_\n\n🔍 Анализирую...", parse_mode="Markdown")
        await process_input(msg.from_user.id, text, wait)
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    wait = await msg.answer("🔍 Анализирую...")
    await process_input(msg.from_user.id, msg.text, wait)

async def process_input(user_id, text, wait_msg):
    try:
        water_kw = ["воды","воду","вода","выпил","выпила","стакан","кружку","бутылку"]
        if any(w in text.lower() for w in water_kw):
            ml    = await detect_water_ml(text)
            save_water(user_id, ml)
            total = get_today_water(user_id)
            goal  = get_setting(user_id, "water_goal_ml") or 2500
            await wait_msg.edit_text(f"💧 +*{ml} мл* воды!\n\n{water_bar(total,goal)}", parse_mode="Markdown")
            return
        data = await analyze_text(text)
        if data.get("is_water"):
            ml    = await detect_water_ml(text)
            save_water(user_id, ml)
            total = get_today_water(user_id)
            goal  = get_setting(user_id, "water_goal_ml") or 2500
            await wait_msg.edit_text(f"💧 +*{ml} мл* воды!\n\n{water_bar(total,goal)}", parse_mode="Markdown")
        else:
            save_meal(user_id, data["food"], data["calories"],
                      data.get("protein",0), data.get("fat",0), data.get("carbs",0))
            total = sum(m["calories"] for m in get_today_meals(user_id))
            goal  = get_setting(user_id, "calories_goal") or 2000
            await wait_msg.edit_text(format_meal(data)+f"\n\n{calorie_bar(total,goal)}", parse_mode="Markdown")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка: {e}")

# ── Автосводка по местному времени ────────────────────────────────────────────

async def auto_summary_task():
    while True:
        await asyncio.sleep(60)
        now_utc = datetime.utcnow()
        today   = date.today().strftime("%Y-%m-%d")
        users   = get_all_active_users()

        for user_id, notify_hour, tz_offset, sent_date in users:
            # Вычисляем местное время пользователя
            local_hour = (now_utc.hour + (tz_offset or 0)) % 24
            if local_hour == (notify_hour or 21) and now_utc.minute < 2:
                if sent_date != today:
                    try:
                        text = await build_summary(user_id)
                        await bot.send_message(
                            user_id,
                            f"🌙 *Вечерняя сводка*\n\n{text}",
                            parse_mode="Markdown"
                        )
                        set_setting(user_id, "summary_sent_date", today)
                    except:
                        pass

# ── Запуск ─────────────────────────────────────────────────────────────────────

async def main():
    init_db()
    print("Бот запущен!")
    asyncio.create_task(auto_summary_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
