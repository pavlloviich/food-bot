"""
EasyFoodTrack Bot
- Система доступа по запросу
- Онбординг опциональный (пропустить или заполнить)
- GPT-4o-mini, Supabase
"""

import asyncio
import os
import tempfile
import json
import base64
import aiohttp
from datetime import date, datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from openai import AsyncOpenAI
from supabase import create_client, Client

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
ADMIN_ID             = 6240082239
ADMIN_IDS            = {6240082239}
DAILY_LIMIT          = 50  # запросов в день на пользователя

bot    = Bot(token=TELEGRAM_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ── Лимит запросов ────────────────────────────────────────────────────────────

def get_request_count(user_id):
    today = date.today().strftime("%Y-%m-%d")
    try:
        r = sb.table("request_counts").select("count").eq("user_id", user_id).eq("date", today).execute()
        return r.data[0].get("count", 0) if r.data else 0
    except:
        return 0

def increment_request_count(user_id):
    today = date.today().strftime("%Y-%m-%d")
    try:
        exists = sb.table("request_counts").select("count").eq("user_id", user_id).eq("date", today).execute()
        if exists.data:
            current = exists.data[0].get("count", 0)
            sb.table("request_counts").update({"count": current + 1}).eq("user_id", user_id).eq("date", today).execute()
        else:
            sb.table("request_counts").insert({"user_id": user_id, "date": today, "count": 1}).execute()
    except Exception as e:
        print(f"increment error: {e}")

def check_limit(user_id):
    if user_id in ADMIN_IDS:
        return True
    return get_request_count(user_id) < DAILY_LIMIT

# ── FSM ───────────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
    onboarding_choice = State()
    gender   = State()
    age      = State()
    weight   = State()
    height   = State()
    activity = State()
    goal     = State()
    tz       = State()
    hour     = State()

class ChangeNotify(StatesGroup):
    tz   = State()
    hour = State()

# ── Доступ ────────────────────────────────────────────────────────────────────

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_access_status(user_id):
    if is_admin(user_id):
        return 'granted'
    try:
        r = sb.table("access_requests").select("status").eq("user_id", user_id).single().execute()
        return r.data.get("status", "none") if r.data else "none"
    except:
        return "none"

def request_access(user_id, username, full_name):
    try:
        exists = sb.table("access_requests").select("user_id").eq("user_id", user_id).execute()
        data = {"user_id": user_id, "username": username, "full_name": full_name, "status": "pending"}
        if exists.data:
            sb.table("access_requests").update(data).eq("user_id", user_id).execute()
        else:
            sb.table("access_requests").insert(data).execute()
    except Exception as e:
        print(f"request_access error: {e}")

def set_access_status(user_id, status):
    try:
        sb.table("access_requests").update({"status": status}).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"set_access_status error: {e}")

def get_all_users():
    try:
        r = sb.table("access_requests").select("*").execute()
        return r.data or []
    except:
        return []

# ── Supabase хелперы ──────────────────────────────────────────────────────────

def get_setting(user_id, key, default=None):
    try:
        r = sb.table("user_settings").select(key).eq("user_id", user_id).single().execute()
        return r.data.get(key, default) if r.data else default
    except:
        return default

def set_setting(user_id, key, value):
    try:
        exists = sb.table("user_settings").select("user_id").eq("user_id", user_id).execute()
        if exists.data:
            sb.table("user_settings").update({key: value}).eq("user_id", user_id).execute()
        else:
            sb.table("user_settings").insert({"user_id": user_id, key: value}).execute()
    except Exception as e:
        print(f"set_setting error: {e}")

def set_settings_bulk(user_id, data):
    try:
        exists = sb.table("user_settings").select("user_id").eq("user_id", user_id).execute()
        data["user_id"] = user_id
        if exists.data:
            sb.table("user_settings").update(data).eq("user_id", user_id).execute()
        else:
            sb.table("user_settings").insert(data).execute()
    except Exception as e:
        print(f"set_settings_bulk error: {e}")

# ── ИЗМЕНЕНИЕ: save_meal теперь принимает и сохраняет weight_g ───────────────

def save_meal(user_id, food, calories, protein=0, fat=0, carbs=0, weight_g=None):
    now = datetime.now()
    row = {
        "user_id":  user_id,
        "date":     now.strftime("%Y-%m-%d"),
        "time":     now.strftime("%H:%M"),
        "food":     food,
        "calories": calories,
        "protein":  protein,
        "fat":      fat,
        "carbs":    carbs,
    }
    if weight_g is not None:
        row["weight_g"] = weight_g
    sb.table("meals").insert(row).execute()

def save_water(user_id, ml):
    now = datetime.now()
    sb.table("water").insert({
        "user_id": user_id, "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"), "amount_ml": ml
    }).execute()

def get_today_meals(user_id):
    today = date.today().strftime("%Y-%m-%d")
    r = sb.table("meals").select("*").eq("user_id", user_id).eq("date", today).order("time").execute()
    return r.data or []

def get_today_water(user_id):
    today = date.today().strftime("%Y-%m-%d")
    r = sb.table("water").select("amount_ml").eq("user_id", user_id).eq("date", today).execute()
    return sum(row["amount_ml"] for row in (r.data or []))

def get_all_active_users():
    r = sb.table("user_settings").select(
        "user_id,notify_hour,timezone_offset,summary_sent_date"
    ).eq("setup_done", 1).execute()
    return r.data or []

# ── Расчёт калорий (Миффлин-Сан Жеор) ───────────────────────────────────────

ACTIVITY_K = {
    "sedentary":  1.2,
    "light":      1.375,
    "moderate":   1.55,
    "active":     1.725,
    "very_active": 1.9,
}

def calculate_calories(gender, age, weight_kg, height_cm, activity, goal):
    if gender == "male":
        bmr = 10*weight_kg + 6.25*height_cm - 5*age + 5
    else:
        bmr = 10*weight_kg + 6.25*height_cm - 5*age - 161
    tdee     = int(bmr * ACTIVITY_K.get(activity, 1.55))
    deficit  = tdee - 500
    surplus  = tdee + 300
    goal_cal = {"lose": deficit, "maintain": tdee, "gain": surplus}.get(goal, tdee)
    return tdee, deficit, surplus, goal_cal

# ── OpenAI ────────────────────────────────────────────────────────────────────

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
  "water_ml": 0,
  "comment": ""
}
Если это вода/жидкость без калорий — is_water: true, water_ml: количество мл.
Делай разумную оценку порции если вес не указан."""

async def analyze_text(text):
    resp = await client.chat.completions.create(
        model="gpt-4o-mini", response_format={"type": "json_object"},
        messages=[{"role":"system","content":FOOD_PROMPT},{"role":"user","content":text}]
    )
    return json.loads(resp.choices[0].message.content)

async def analyze_image(image_bytes):
    b64 = base64.b64encode(image_bytes).decode()
    resp = await client.chat.completions.create(
        model="gpt-4o-mini", response_format={"type": "json_object"},
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

# ── Форматирование ────────────────────────────────────────────────────────────

def water_bar(current, goal=2500):
    pct = min(current/goal, 1.0)
    return f"{'💧'*int(pct*10)}{'⬜'*(10-int(pct*10))} {current}/{goal} мл"

def calorie_bar(eaten, goal):
    pct = min(eaten/goal, 1.0)
    return f"{'🟩'*int(pct*10)}{'⬜'*(10-int(pct*10))} {eaten}/{goal} ккал"

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

    total_cal = sum(m["calories"] for m in meals)
    total_p   = sum(m.get("protein") or 0 for m in meals)
    total_f   = sum(m.get("fat")     or 0 for m in meals)
    total_c   = sum(m.get("carbs")   or 0 for m in meals)

    lines = ["📊 *Итог дня*\n"]
    if meals:
        lines.append("🍽 *Питание:*\n```")
        lines.append(f"{'Время':<6} {'Блюдо':<20} {'Ккал':>5}")
        lines.append("─"*33)
        for m in meals:
            lines.append(f"{m['time']:<6} {m['food'][:19]:<20} {m['calories']:>5}")
        lines.append("─"*33)
        lines.append(f"{'ИТОГО':<26} {total_cal:>5}\n```")
        lines.append(f"🥩 Б: *{total_p:.0f}г*  🧈 Ж: *{total_f:.0f}г*  🍞 У: *{total_c:.0f}г*\n")
    else:
        lines.append("🍽 Еда сегодня не записана\n")

    goal_labels = {"lose":"похудение 📉","maintain":"поддержание ⚖️","gain":"набор массы 📈"}
    lines.append(f"⚡ *Калорийный баланс* ({goal_labels.get(goal_type,'')})")
    lines.append(calorie_bar(total_cal, cal_goal))
    lines.append("")
    lines.append("💧 *Вода:*")
    lines.append(water_bar(water_ml, water_goal))
    left = max(0, water_goal - water_ml)
    lines.append(f"⚠️ Осталось: *{left} мл*" if left > 0 else "✅ Норма воды выполнена!")
    return "\n".join(lines)

# ── Клавиатуры ────────────────────────────────────────────────────────────────

def tz_keyboard():
    zones = [
        ("🇷🇺 Москва (UTC+3)", 3), ("🇷🇺 Екатеринбург (UTC+5)", 5),
        ("🇷🇺 Новосибирск (UTC+7)", 7), ("🇷🇺 Владивосток (UTC+10)", 10),
        ("🇹🇭 Таиланд (UTC+7)", 7), ("🇦🇪 Дубай (UTC+4)", 4),
        ("🇩🇪 Европа (UTC+2)", 2), ("🇺🇸 Нью-Йорк (UTC-5)", -5),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"tz_{offset}")]
        for name, offset in zones
    ])

def hour_keyboard():
    hours = [18, 19, 20, 21, 22, 23]
    rows, row = [], []
    for h in hours:
        row.append(InlineKeyboardButton(text=f"{h}:00", callback_data=f"hour_{h}"))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── /start ────────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    uid    = msg.from_user.id
    status = get_access_status(uid)

    if status == 'granted':
        if get_setting(uid, "setup_done"):
            await msg.answer(
                "👋 Привет!\n\n"
                "📸 Фото / 🎤 Голос / ✍️ Текст → калории\n"
                "💧 *'выпил стакан воды'* → вода\n"
                "\n"
                "/today — еда за сегодня\n"
                "/diary — дневник с удалением\n"
                "/month — сводка за период\n"
                "/summary — итог дня\n"
                "/water — вода\n"
                "/goal — норма калорий\n"
                "/notify — время уведомлений\n"
                "/clear — очистить дневник\n"
                "/reminders — настройки напоминаний\n"
                + ("\n/users — управление пользователями" if is_admin(uid) else ""),
                parse_mode="Markdown"
            )
        else:
            await start_onboarding(msg, state)
        return

    if status == 'pending':
        await msg.answer("⏳ Твой запрос на доступ уже отправлен. Ожидай подтверждения!")
        return

    if status == 'rejected':
        await msg.answer("❌ Твой запрос на доступ был отклонён.")
        return

    # Новый пользователь
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Запросить доступ", callback_data="request_access")]
    ])
    await msg.answer(
        "👋 Привет! Это закрытый бот для подсчёта калорий.\n\n"
        "Нажми кнопку чтобы запросить доступ:",
        reply_markup=kb
    )

@dp.callback_query(F.data == "request_access")
async def cb_request_access(call: CallbackQuery):
    uid       = call.from_user.id
    username  = call.from_user.username or ""
    full_name = call.from_user.full_name or ""

    request_access(uid, username, full_name)
    await call.message.edit_text("✅ Запрос отправлен! Ожидай подтверждения от администратора.")

    display = f"@{username}" if username else full_name or str(uid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{uid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{uid}"),
        ]
    ])
    await bot.send_message(
        ADMIN_ID,
        f"🔔 *Новый запрос доступа*\n\n"
        f"👤 {display}\n"
        f"🆔 `{uid}`",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split("_")[1])
    set_access_status(uid, "granted")
    await call.message.edit_text(f"✅ Пользователь `{uid}` одобрен!", parse_mode="Markdown")
    try:
        await bot.send_message(
            uid,
            "🎉 *Доступ открыт!*\n\nДобро пожаловать! Напиши /start чтобы начать.",
            parse_mode="Markdown"
        )
    except:
        pass

@dp.callback_query(F.data.startswith("reject_"))
async def cb_reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split("_")[1])
    set_access_status(uid, "rejected")
    await call.message.edit_text(f"❌ Пользователь `{uid}` отклонён.", parse_mode="Markdown")
    try:
        await bot.send_message(uid, "❌ Твой запрос на доступ был отклонён.")
    except:
        pass

# ── /users ────────────────────────────────────────────────────────────────────

@dp.message(Command("users"))
async def cmd_users(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    users = get_all_users()
    if not users:
        await msg.answer("📭 Нет запросов доступа.")
        return

    status_emoji = {"granted": "✅", "pending": "⏳", "rejected": "❌"}
    lines = ["👥 *Пользователи:*\n"]
    for u in users:
        display = f"@{u['username']}" if u.get('username') else u.get('full_name') or str(u['user_id'])
        emoji   = status_emoji.get(u['status'], "❓")
        lines.append(f"{emoji} {display} `{u['user_id']}`")

    buttons = []
    for u in users:
        display = f"@{u['username']}" if u.get('username') else str(u['user_id'])
        if u['status'] == 'granted':
            buttons.append([InlineKeyboardButton(text=f"🚫 Заблокировать {display}", callback_data=f"reject_{u['user_id']}")])
        elif u['status'] in ('pending', 'rejected'):
            buttons.append([
                InlineKeyboardButton(text=f"✅ {display}", callback_data=f"approve_{u['user_id']}"),
                InlineKeyboardButton(text=f"❌ {display}", callback_data=f"reject_{u['user_id']}"),
            ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await msg.answer("\n".join(lines), reply_markup=kb, parse_mode="Markdown")

# ── Онбординг ─────────────────────────────────────────────────────────────────

async def start_onboarding(msg, state):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заполнить профиль (точнее)", callback_data="ob_full")],
        [InlineKeyboardButton(text="⚡ Пропустить (настрою вручную)", callback_data="ob_skip")],
    ])
    await msg.answer(
        "🎉 *Добро пожаловать!*\n\n"
        "Хочешь заполнить профиль чтобы я рассчитал твою норму калорий?\n\n"
        "Или можешь пропустить и настроить норму вручную через /goal",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(Setup.onboarding_choice)

@dp.callback_query(Setup.onboarding_choice, F.data == "ob_skip")
async def ob_skip(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    set_settings_bulk(uid, {
        "calories_goal": 2000, "calories_deficit": 1500,
        "calories_surplus": 2300, "water_goal_ml": 2500,
        "timezone_offset": 0, "notify_hour": 21, "setup_done": 1,
        "goal": "maintain",
    })
    await state.clear()
    await call.message.edit_text(
        "✅ *Готово!* Норма по умолчанию: *2000 ккал/день*\n\n"
        "Измени через /goal в любой момент.\n\n"
        "Отправляй фото еды, голосовые или текст — считаю калории! 🚀",
        parse_mode="Markdown"
    )

@dp.callback_query(Setup.onboarding_choice, F.data == "ob_full")
async def ob_full(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male"),
         InlineKeyboardButton(text="👩 Женский",  callback_data="gender_female")]
    ])
    await call.message.edit_text("Укажи пол:", reply_markup=kb)
    await state.set_state(Setup.gender)

@dp.callback_query(Setup.gender, F.data.startswith("gender_"))
async def setup_gender(call: CallbackQuery, state: FSMContext):
    await state.update_data(gender=call.data.split("_")[1])
    await call.message.edit_text("Сколько тебе лет?")
    await state.set_state(Setup.age)

@dp.message(Setup.age)
async def setup_age(msg: Message, state: FSMContext):
    try:
        age = int(msg.text.strip())
        assert 10 < age < 100
        await state.update_data(age=age)
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
            [InlineKeyboardButton(text="🛋 Сидячая работа, почти нет спорта",      callback_data="act_sedentary")],
            [InlineKeyboardButton(text="🚶 Лёгкая активность (1-2 тренировки/нед)", callback_data="act_light")],
            [InlineKeyboardButton(text="🏃 Умеренная (3-5 тренировок/нед)",         callback_data="act_moderate")],
            [InlineKeyboardButton(text="💪 Высокая (6-7 тренировок/нед)",            callback_data="act_active")],
            [InlineKeyboardButton(text="🔥 Очень высокая (спорт + физ. работа)",    callback_data="act_very_active")],
        ])
        await msg.answer("Уровень физической активности:", reply_markup=kb)
        await state.set_state(Setup.activity)
    except:
        await msg.answer("Введи рост числом, например: 178")

@dp.callback_query(Setup.activity, F.data.startswith("act_"))
async def setup_activity(call: CallbackQuery, state: FSMContext):
    await state.update_data(activity=call.data[4:])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Похудеть",       callback_data="goal_lose")],
        [InlineKeyboardButton(text="⚖️ Поддержать вес", callback_data="goal_maintain")],
        [InlineKeyboardButton(text="📈 Набрать массу",  callback_data="goal_gain")],
    ])
    await call.message.edit_text("Какая цель?", reply_markup=kb)
    await state.set_state(Setup.goal)

@dp.callback_query(Setup.goal, F.data.startswith("goal_"))
async def setup_goal(call: CallbackQuery, state: FSMContext):
    goal = call.data.split("_")[1]
    d    = await state.get_data()
    tdee, deficit, surplus, goal_cal = calculate_calories(
        d["gender"], d["age"], d["weight_kg"], d["height_cm"], d["activity"], goal
    )
    await state.update_data(goal=goal, tdee=tdee, deficit=deficit, surplus=surplus, goal_cal=goal_cal)
    labels = {"lose":"похудение 📉","maintain":"поддержание ⚖️","gain":"набор массы 📈"}
    await call.message.edit_text(
        f"✅ *Норма рассчитана!*\n\n"
        f"📉 Для похудения: *{deficit} ккал/день*\n"
        f"⚖️ Для поддержания: *{tdee} ккал/день*\n"
        f"📈 Для набора массы: *{surplus} ккал/день*\n\n"
        f"🎯 Твоя цель ({labels[goal]}): *{goal_cal} ккал/день*\n\n"
        f"Выбери свой часовой пояс:",
        reply_markup=tz_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(Setup.tz)

@dp.callback_query(Setup.tz, F.data.startswith("tz_"))
async def setup_tz(call: CallbackQuery, state: FSMContext):
    offset = int(call.data.split("_")[1])
    await state.update_data(tz_offset=offset)
    await call.message.edit_text(
        f"✅ UTC{'+' if offset>=0 else ''}{offset}\n\nВо сколько присылать вечернюю сводку?",
        reply_markup=hour_keyboard()
    )
    await state.set_state(Setup.hour)

@dp.callback_query(Setup.hour, F.data.startswith("hour_"))
async def setup_hour(call: CallbackQuery, state: FSMContext):
    hour = int(call.data.split("_")[1])
    d    = await state.get_data()
    uid  = call.from_user.id
    tz   = d.get("tz_offset", 0)
    set_settings_bulk(uid, {
        "gender": d["gender"], "age": d["age"],
        "weight_kg": d["weight_kg"], "height_cm": d["height_cm"],
        "activity": d["activity"], "goal": d["goal"],
        "calories_goal": d["goal_cal"], "calories_deficit": d["deficit"],
        "calories_surplus": d["surplus"], "water_goal_ml": 2500,
        "timezone_offset": tz, "notify_hour": hour, "setup_done": 1,
    })
    await state.clear()
    await call.message.edit_text(
        f"🎉 *Всё готово!*\n\n"
        f"🔔 Сводка каждый день в *{hour}:00* (UTC{'+' if tz>=0 else ''}{tz})\n\n"
        f"Отправляй фото еды, голосовые или текст!\n"
        f"💧 'выпил стакан воды' — вода",
        parse_mode="Markdown"
    )

# ── /notify ───────────────────────────────────────────────────────────────────

@dp.message(Command("notify"))
async def cmd_notify(msg: Message, state: FSMContext):
    if get_access_status(msg.from_user.id) != 'granted': return
    cur_h  = get_setting(msg.from_user.id, "notify_hour") or 21
    cur_tz = get_setting(msg.from_user.id, "timezone_offset") or 0
    await msg.answer(
        f"🔔 Сейчас: *{cur_h}:00* (UTC{'+' if cur_tz>=0 else ''}{cur_tz})\n\nВыбери часовой пояс:",
        reply_markup=tz_keyboard(), parse_mode="Markdown"
    )
    await state.set_state(ChangeNotify.tz)

@dp.callback_query(ChangeNotify.tz, F.data.startswith("tz_"))
async def change_tz(call: CallbackQuery, state: FSMContext):
    offset = int(call.data.split("_")[1])
    await state.update_data(tz_offset=offset)
    await call.message.edit_text("Во сколько присылать сводку?", reply_markup=hour_keyboard())
    await state.set_state(ChangeNotify.hour)

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

# ── /goal ─────────────────────────────────────────────────────────────────────

@dp.message(Command("goal"))
async def cmd_goal(msg: Message, state: FSMContext):
    if get_access_status(msg.from_user.id) != 'granted': return
    uid     = msg.from_user.id
    tdee    = get_setting(uid, "calories_goal")    or 2000
    deficit = get_setting(uid, "calories_deficit") or tdee - 500
    surplus = get_setting(uid, "calories_surplus") or tdee + 300
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📉 Похудеть ({deficit} ккал/день)",      callback_data="setgoal_lose")],
        [InlineKeyboardButton(text=f"⚖️ Поддержать вес ({tdee} ккал/день)",   callback_data="setgoal_maintain")],
        [InlineKeyboardButton(text=f"📈 Набрать массу ({surplus} ккал/день)", callback_data="setgoal_gain")],
        [InlineKeyboardButton(text="✏️ Ввести свою норму",                     callback_data="setgoal_custom")],
        [InlineKeyboardButton(text="🔄 Пересчитать профиль",                   callback_data="setgoal_recalc")],
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
        await state.set_state(Setup.gender)
        return
    if action == "custom":
        await call.message.edit_text("Введи свою норму калорий числом, например: 1800")
        await state.set_state(None)
        await state.update_data(awaiting_custom_goal=True)
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

# ── Основные команды ──────────────────────────────────────────────────────────

@dp.message(Command("today"))
async def cmd_today(msg: Message):
    if get_access_status(msg.from_user.id) != 'granted': return
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
    if get_access_status(msg.from_user.id) != 'granted': return
    wait = await msg.answer("📊 Собираю итог...")
    await wait.edit_text(await build_summary(msg.from_user.id), parse_mode="Markdown")

@dp.message(Command("water"))
async def cmd_water(msg: Message):
    if get_access_status(msg.from_user.id) != 'granted': return
    water = get_today_water(msg.from_user.id)
    goal  = get_setting(msg.from_user.id, "water_goal_ml") or 2500
    await msg.answer(f"💧 *Вода за сегодня*\n\n{water_bar(water, goal)}", parse_mode="Markdown")

@dp.message(Command("diary"))
async def cmd_diary(msg: Message):
    if get_access_status(msg.from_user.id) != 'granted': return
    meals = get_today_meals(msg.from_user.id)
    if not meals:
        await msg.answer("📭 Сегодня записей нет.")
        return
    await msg.answer("📋 *Дневник за сегодня*\nНажми ❌ чтобы удалить:", parse_mode="Markdown")
    for m in meals:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{m['id']}")]
        ])
        await msg.answer(
            f"🕐 {m['time']} — *{m['food']}*\n🔥 {m['calories']} ккал",
            reply_markup=kb, parse_mode="Markdown"
        )

@dp.callback_query(F.data.startswith("del_"))
async def cb_delete_meal(call: CallbackQuery):
    meal_id = int(call.data.split("_")[1])
    sb.table("meals").delete().eq("id", meal_id).execute()
    await call.message.edit_text("🗑 Запись удалена.")

@dp.message(Command("month"))
async def cmd_month(msg: Message):
    if get_access_status(msg.from_user.id) != 'granted': return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="period_today")],
        [InlineKeyboardButton(text="📅 7 дней",  callback_data="period_7")],
        [InlineKeyboardButton(text="📅 30 дней", callback_data="period_30")],
    ])
    await msg.answer("За какой период показать статистику?", reply_markup=kb)

@dp.callback_query(F.data.startswith("period_"))
async def cb_period(call: CallbackQuery):
    from datetime import timedelta
    period = call.data.split("_")[1]
    t      = date.today()
    start  = t if period=="today" else t-timedelta(days=6 if period=="7" else 29)
    r = sb.table("meals").select("*").eq("user_id", call.from_user.id)\
        .gte("date", start.strftime("%Y-%m-%d")).lte("date", t.strftime("%Y-%m-%d"))\
        .order("date").execute()
    meals = r.data or []
    if not meals:
        await call.message.edit_text("📭 Нет данных за этот период.")
        return
    by_day = {}
    for m in meals:
        by_day.setdefault(m["date"], []).append(m)
    lines = [f"📊 *Статистика за {len(by_day)} дней*\n```"]
    lines.append(f"{'Дата':<8} {'Ккал':>6} {'Б':>5} {'Ж':>5} {'У':>5}")
    lines.append("─"*33)
    total_all = 0
    for day, dm in sorted(by_day.items()):
        cal = sum(m["calories"] for m in dm)
        p   = sum(m.get("protein") or 0 for m in dm)
        f   = sum(m.get("fat") or 0 for m in dm)
        c   = sum(m.get("carbs") or 0 for m in dm)
        total_all += cal
        lines.append(f"{day[5:]:<8} {cal:>6} {p:>5.0f} {f:>5.0f} {c:>5.0f}")
    lines += ["─"*33, f"{'Среднее':<8} {total_all//len(by_day):>6}", "```"]
    await call.message.edit_text("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("clear"))
async def cmd_clear(msg: Message):
    if get_access_status(msg.from_user.id) != 'granted': return
    today = date.today().strftime("%Y-%m-%d")
    sb.table("meals").delete().eq("user_id", msg.from_user.id).eq("date", today).execute()
    sb.table("water").delete().eq("user_id", msg.from_user.id).eq("date", today).execute()
    await msg.answer("🗑 Дневник за сегодня очищен.")

# ── Обработка сообщений ───────────────────────────────────────────────────────

@dp.message(F.photo)
async def handle_photo(msg: Message):
    if get_access_status(msg.from_user.id) != 'granted': return
    if not check_limit(msg.from_user.id):
        await msg.answer(f"⚠️ Дневной лимит {DAILY_LIMIT} запросов исчерпан. Приходи завтра!")
        return
    increment_request_count(msg.from_user.id)
    wait = await msg.answer("🔍 Анализирую фото...")
    try:
        file = await bot.get_file(msg.photo[-1].file_id)
        url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                img = await r.read()
        data  = await analyze_image(img)
        # ИЗМЕНЕНИЕ: передаём weight_g в save_meal
        save_meal(msg.from_user.id, data["food"], data["calories"],
                  data.get("protein", 0), data.get("fat", 0), data.get("carbs", 0),
                  weight_g=data.get("weight_g"))
        total = sum(m["calories"] for m in get_today_meals(msg.from_user.id))
        goal  = get_setting(msg.from_user.id, "calories_goal") or 2000
        await wait.edit_text(format_meal(data)+f"\n\n{calorie_bar(total,goal)}", parse_mode="Markdown")
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.voice)
async def handle_voice(msg: Message):
    if get_access_status(msg.from_user.id) != 'granted': return
    if not check_limit(msg.from_user.id):
        await msg.answer(f"⚠️ Дневной лимит {DAILY_LIMIT} запросов исчерпан. Приходи завтра!")
        return
    increment_request_count(msg.from_user.id)
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
    if get_access_status(msg.from_user.id) != 'granted': return
    if not check_limit(msg.from_user.id):
        await msg.answer(f"⚠️ Дневной лимит {DAILY_LIMIT} запросов исчерпан. Приходи завтра!")
        return
    increment_request_count(msg.from_user.id)
    wait = await msg.answer("🔍 Анализирую...")
    await process_input(msg.from_user.id, msg.text, wait)

async def process_input(user_id, text, wait_msg):
    try:
        data = await analyze_text(text)
        if data.get("is_water"):
            ml    = data.get("water_ml") or 250
            save_water(user_id, ml)
            total = get_today_water(user_id)
            goal  = get_setting(user_id, "water_goal_ml") or 2500
            await wait_msg.edit_text(f"💧 +*{ml} мл* воды!\n\n{water_bar(total,goal)}", parse_mode="Markdown")
        else:
            # ИЗМЕНЕНИЕ: передаём weight_g в save_meal
            save_meal(user_id, data["food"], data["calories"],
                      data.get("protein", 0), data.get("fat", 0), data.get("carbs", 0),
                      weight_g=data.get("weight_g"))
            total = sum(m["calories"] for m in get_today_meals(user_id))
            goal  = get_setting(user_id, "calories_goal") or 2000
            await wait_msg.edit_text(format_meal(data)+f"\n\n{calorie_bar(total,goal)}", parse_mode="Markdown")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка: {e}")

# ── /reminders ────────────────────────────────────────────────────────────────

@dp.message(Command("reminders"))
async def cmd_reminders(msg: Message):
    if get_access_status(msg.from_user.id) != 'granted': return
    uid = msg.from_user.id
    s = {
        'rb': get_setting(uid, 'remind_breakfast_enabled') or 1,
        'rt': get_setting(uid, 'remind_breakfast_time') or '09:00',
        'lb': get_setting(uid, 'remind_lunch_enabled') or 1,
        'lt': get_setting(uid, 'remind_lunch_time') or '13:00',
        'db': get_setting(uid, 'remind_dinner_enabled') or 1,
        'dt': get_setting(uid, 'remind_dinner_time') or '19:00',
        'wb': get_setting(uid, 'remind_water_enabled') or 0,
        'wi': get_setting(uid, 'remind_water_interval') or 2,
    }
    def on_off(v): return "✅" if v else "⬜"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{on_off(s['rb'])} 🌅 Завтрак ({s['rt']})", callback_data="rem_breakfast")],
        [InlineKeyboardButton(text=f"{on_off(s['lb'])} ☀️ Обед ({s['lt']})", callback_data="rem_lunch")],
        [InlineKeyboardButton(text=f"{on_off(s['db'])} 🌆 Ужин ({s['dt']})", callback_data="rem_dinner")],
        [InlineKeyboardButton(text=f"{on_off(s['wb'])} 💧 Вода (каждые {s['wi']}ч)", callback_data="rem_water")],
    ])
    await msg.answer(
        "🔔 *Настройки напоминаний*\n\n"
        "Бот напомнит через 1.5 часа после указанного времени приёма пищи — если записей ещё нет.\n\n"
        "Нажми на пункт чтобы включить/выключить или изменить время:",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("rem_"))
async def cb_reminder(call: CallbackQuery):
    uid  = call.from_user.id
    kind = call.data.split("_")[1]

    if kind == "breakfast":
        enabled = get_setting(uid, 'remind_breakfast_enabled') or 1
        if enabled:
            await call.message.edit_text(
                "🌅 *Напоминание о завтраке*\n\nВведи примерное время завтрака (например: 08:30)\nИли нажми Выключить:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬜ Выключить", callback_data="rem_off_breakfast")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="rem_back")],
                ]),
                parse_mode="Markdown"
            )
        else:
            set_setting(uid, 'remind_breakfast_enabled', 1)
            await call.answer("✅ Напоминание о завтраке включено!")
            await cmd_reminders_edit(call.message, uid)

    elif kind == "lunch":
        enabled = get_setting(uid, 'remind_lunch_enabled') or 1
        if enabled:
            await call.message.edit_text(
                "☀️ *Напоминание об обеде*\n\nВведи примерное время обеда (например: 13:00)\nИли нажми Выключить:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬜ Выключить", callback_data="rem_off_lunch")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="rem_back")],
                ]),
                parse_mode="Markdown"
            )
        else:
            set_setting(uid, 'remind_lunch_enabled', 1)
            await call.answer("✅ Напоминание об обеде включено!")
            await cmd_reminders_edit(call.message, uid)

    elif kind == "dinner":
        enabled = get_setting(uid, 'remind_dinner_enabled') or 1
        if enabled:
            await call.message.edit_text(
                "🌆 *Напоминание об ужине*\n\nВведи примерное время ужина (например: 19:00)\nИли нажми Выключить:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬜ Выключить", callback_data="rem_off_dinner")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="rem_back")],
                ]),
                parse_mode="Markdown"
            )
        else:
            set_setting(uid, 'remind_dinner_enabled', 1)
            await call.answer("✅ Напоминание об ужине включено!")
            await cmd_reminders_edit(call.message, uid)

    elif kind == "water":
        enabled = get_setting(uid, 'remind_water_enabled') or 0
        if enabled:
            set_setting(uid, 'remind_water_enabled', 0)
            await call.answer("⬜ Напоминания о воде выключены")
            await cmd_reminders_edit(call.message, uid)
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Каждые 2 часа", callback_data="rem_water_2"),
                 InlineKeyboardButton(text="Каждые 3 часа", callback_data="rem_water_3")],
                [InlineKeyboardButton(text="Каждые 4 часа", callback_data="rem_water_4")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="rem_back")],
            ])
            await call.message.edit_text(
                "💧 *Напоминания о воде*\n\nКак часто напоминать?",
                reply_markup=kb, parse_mode="Markdown"
            )

    elif kind.startswith("water_"):
        interval = int(kind.split("_")[1])
        set_setting(uid, 'remind_water_enabled', 1)
        set_setting(uid, 'remind_water_interval', interval)
        await call.answer(f"✅ Напоминание о воде каждые {interval}ч")
        await cmd_reminders_edit(call.message, uid)

    elif kind.startswith("off_"):
        meal = kind.split("_")[1]
        set_setting(uid, f'remind_{meal}_enabled', 0)
        await call.answer("⬜ Выключено")
        await cmd_reminders_edit(call.message, uid)

    elif kind == "back":
        await cmd_reminders_edit(call.message, uid)

async def cmd_reminders_edit(message, uid):
    s = {
        'rb': get_setting(uid, 'remind_breakfast_enabled') or 1,
        'rt': get_setting(uid, 'remind_breakfast_time') or '09:00',
        'lb': get_setting(uid, 'remind_lunch_enabled') or 1,
        'lt': get_setting(uid, 'remind_lunch_time') or '13:00',
        'db': get_setting(uid, 'remind_dinner_enabled') or 1,
        'dt': get_setting(uid, 'remind_dinner_time') or '19:00',
        'wb': get_setting(uid, 'remind_water_enabled') or 0,
        'wi': get_setting(uid, 'remind_water_interval') or 2,
    }
    def on_off(v): return "✅" if v else "⬜"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{on_off(s['rb'])} 🌅 Завтрак ({s['rt']})", callback_data="rem_breakfast")],
        [InlineKeyboardButton(text=f"{on_off(s['lb'])} ☀️ Обед ({s['lt']})", callback_data="rem_lunch")],
        [InlineKeyboardButton(text=f"{on_off(s['db'])} 🌆 Ужин ({s['dt']})", callback_data="rem_dinner")],
        [InlineKeyboardButton(text=f"{on_off(s['wb'])} 💧 Вода (каждые {s['wi']}ч)", callback_data="rem_water")],
    ])
    await message.edit_text(
        "🔔 *Настройки напоминаний*\n\nНажми на пункт чтобы включить/выключить:",
        reply_markup=kb, parse_mode="Markdown"
    )

# ── Умные напоминания ─────────────────────────────────────────────────────────

def has_meals_in_window(user_id, from_hour, to_hour):
    """Есть ли записи еды в диапазоне часов (время в базе — локальное время пользователя)"""
    today = date.today().strftime("%Y-%m-%d")
    r = sb.table("meals").select("time").eq("user_id", user_id).eq("date", today).execute()
    for m in (r.data or []):
        h = int(m["time"].split(":")[0])
        if from_hour <= h <= to_hour:
            return True
    return False

def get_local_hour(tz_offset):
    now_utc = datetime.utcnow()
    return (now_utc.hour + (tz_offset or 0)) % 24

def get_local_minute():
    return datetime.utcnow().minute

REMINDER_SENT = {}

async def smart_reminders_task():
    while True:
        await asyncio.sleep(60)
        try:
            users = get_all_active_users()
            today = date.today().strftime("%Y-%m-%d")

            for user in users:
                uid     = user["user_id"]
                tz      = user.get("timezone_offset") or 0
                local_h = get_local_hour(tz)
                local_m = get_local_minute()

                if local_m > 5:
                    continue

                # Не беспокоим ночью (с 23 до 7 по местному времени)
                if local_h >= 23 or local_h < 7:
                    continue

                # Завтрак
                if get_setting(uid, 'remind_breakfast_enabled'):
                    bt = get_setting(uid, 'remind_breakfast_time') or '09:00'
                    bh = int(bt.split(':')[0])
                    remind_h = (bh + 1) % 24
                    key = (uid, 'breakfast', today)
                    if local_h == remind_h and key not in REMINDER_SENT:
                        if not has_meals_in_window(uid, max(bh - 1, 0), bh + 1):
                            try:
                                await bot.send_message(uid, "🌅 Как прошёл завтрак? Не забудь записать! 🍳")
                                REMINDER_SENT[key] = today
                            except: pass

                # Обед
                if get_setting(uid, 'remind_lunch_enabled'):
                    lt = get_setting(uid, 'remind_lunch_time') or '13:00'
                    lh = int(lt.split(':')[0])
                    remind_h = (lh + 1) % 24
                    key = (uid, 'lunch', today)
                    if local_h == remind_h and key not in REMINDER_SENT:
                        if not has_meals_in_window(uid, max(lh - 1, 0), lh + 1):
                            try:
                                await bot.send_message(uid, "☀️ Как прошёл обед? Запиши что ел 🍱")
                                REMINDER_SENT[key] = today
                            except: pass

                # Ужин
                if get_setting(uid, 'remind_dinner_enabled'):
                    dt = get_setting(uid, 'remind_dinner_time') or '19:00'
                    dh = int(dt.split(':')[0])
                    remind_h = (dh + 1) % 24
                    key = (uid, 'dinner', today)
                    if local_h == remind_h and key not in REMINDER_SENT:
                        if not has_meals_in_window(uid, max(dh - 1, 0), dh + 1):
                            try:
                                await bot.send_message(uid, "🌆 Как прошёл ужин? Запиши что ел 🍽")
                                REMINDER_SENT[key] = today
                            except: pass

                # Вода — только с 8 до 21 по местному времени
                if get_setting(uid, 'remind_water_enabled') and 8 <= local_h <= 21:
                    interval = get_setting(uid, 'remind_water_interval') or 2
                    if local_h % interval == 0:
                        key = (uid, f'water_{local_h}', today)
                        if key not in REMINDER_SENT:
                            water = get_today_water(uid)
                            goal  = get_setting(uid, 'water_goal_ml') or 2500
                            if water < goal:
                                try:
                                    await bot.send_message(uid, f"💧 Время выпить воды! Сегодня выпито {water} из {goal} мл")
                                    REMINDER_SENT[key] = today
                                except: pass
        except Exception as e:
            print(f"smart_reminders_task error: {e}")

async def auto_summary_task():
    while True:
        await asyncio.sleep(60)
        now_utc = datetime.utcnow()
        today   = date.today().strftime("%Y-%m-%d")
        for user in get_all_active_users():
            local_hour = (now_utc.hour + (user.get("timezone_offset") or 0)) % 24
            if local_hour == (user.get("notify_hour") or 21) and now_utc.minute < 2:
                if user.get("summary_sent_date") != today:
                    try:
                        text = await build_summary(user["user_id"])
                        await bot.send_message(user["user_id"], f"🌙 *Вечерняя сводка*\n\n{text}", parse_mode="Markdown")
                        set_setting(user["user_id"], "summary_sent_date", today)
                    except:
                        pass

# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    print("Бот запущен!")
    asyncio.create_task(auto_summary_task())
    asyncio.create_task(smart_reminders_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
