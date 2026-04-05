"""
EasyFoodTrack Bot — версия с Supabase
- Данные хранятся в Supabase (PostgreSQL) — не теряются при перезапуске
- GPT-4o-mini везде
- Без Whoop
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

# ── Конфиг ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

bot    = Bot(token=TELEGRAM_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ── FSM ────────────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
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

def set_settings_bulk(user_id, data: dict):
    try:
        exists = sb.table("user_settings").select("user_id").eq("user_id", user_id).execute()
        data["user_id"] = user_id
        if exists.data:
            sb.table("user_settings").update(data).eq("user_id", user_id).execute()
        else:
            sb.table("user_settings").insert(data).execute()
    except Exception as e:
        print(f"set_settings_bulk error: {e}")

def save_meal(user_id, food, calories, protein=0, fat=0, carbs=0):
    now = datetime.now()
    sb.table("meals").insert({
        "user_id": user_id,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "food": food, "calories": calories,
        "protein": protein, "fat": fat, "carbs": carbs
    }).execute()

def save_water(user_id, ml):
    now = datetime.now()
    sb.table("water").insert({
        "user_id": user_id,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "amount_ml": ml
    }).execute()

def save_burned(user_id, calories):
    now = datetime.now()
    sb.table("burned").insert({
        "user_id": user_id,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "calories": calories
    }).execute()

def get_today_meals(user_id):
    today = date.today().strftime("%Y-%m-%d")
    r = sb.table("meals").select("*").eq("user_id", user_id).eq("date", today).order("time").execute()
    return r.data or []

def get_period_meals(user_id, start_date, end_date):
    r = sb.table("meals").select("*").eq("user_id", user_id)\
        .gte("date", start_date).lte("date", end_date).order("date").order("time").execute()
    return r.data or []

def get_today_water(user_id):
    today = date.today().strftime("%Y-%m-%d")
    r = sb.table("water").select("amount_ml").eq("user_id", user_id).eq("date", today).execute()
    return sum(row["amount_ml"] for row in (r.data or []))

def get_today_burned(user_id):
    today = date.today().strftime("%Y-%m-%d")
    r = sb.table("burned").select("calories").eq("user_id", user_id).eq("date", today).execute()
    return sum(row["calories"] for row in (r.data or []))

def delete_meal(meal_id):
    sb.table("meals").delete().eq("id", meal_id).execute()

def get_all_active_users():
    r = sb.table("user_settings").select(
        "user_id,notify_hour,timezone_offset,summary_sent_date"
    ).eq("setup_done", 1).execute()
    return r.data or []

# ── Расчёт калорий ────────────────────────────────────────────────────────────

ACTIVITY_K = {"low": 1.2, "medium": 1.55, "high": 1.725}

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

# ── OpenAI ─────────────────────────────────────────────────────────────────────

FOOD_PROMPT = """Ты диетолог-аналитик. Ответь ТОЛЬКО в формате JSON:
{
  "is_food": true,
  "is_water": false,
  "is_burned": false,
  "food": "название",
  "weight_g": 100,
  "calories": 200,
  "protein": 10,
  "fat": 5,
  "carbs": 20,
  "burned_calories": 0,
  "water_ml": 0,
  "comment": ""
}
Если это вода/жидкость без калорий — is_water: true, water_ml: количество мл.
Если это сожжённые калории (тренировка) — is_burned: true, burned_calories: количество.
Делай разумную оценку порции если вес не указан."""

async def analyze_text(text):
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[{"role":"system","content":FOOD_PROMPT},{"role":"user","content":text}]
    )
    return json.loads(resp.choices[0].message.content)

async def analyze_image(image_bytes):
    b64  = base64.b64encode(image_bytes).decode()
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
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
    burned_cal = get_today_burned(user_id)
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
    if burned_cal:
        balance = burned_cal - total_cal
        emoji   = "✅" if balance > 0 else "⚠️"
        lines.append(f"💪 Сожжено: *{burned_cal} ккал*")
        lines.append(f"{emoji} Баланс: *{'−' if balance<0 else '+'}{abs(balance)} ккал*")
    lines.append("")

    lines.append("💧 *Вода:*")
    lines.append(water_bar(water_ml, water_goal))
    left = max(0, water_goal - water_ml)
    lines.append(f"⚠️ Осталось: *{left} мл*" if left > 0 else "✅ Норма воды выполнена!")

    return "\n".join(lines)

# ── Клавиатуры ─────────────────────────────────────────────────────────────────

def tz_keyboard():
    zones = [
        ("🇷🇺 Москва (UTC+3)", 3),
        ("🇷🇺 Екатеринбург (UTC+5)", 5),
        ("🇷🇺 Новосибирск (UTC+7)", 7),
        ("🇷🇺 Владивосток (UTC+10)", 10),
        ("🇹🇭 Таиланд (UTC+7)", 7),
        ("🇦🇪 Дубай (UTC+4)", 4),
        ("🇩🇪 Европа (UTC+2)", 2),
        ("🇺🇸 Нью-Йорк (UTC-5)", -5),
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
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── Онбординг ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    if get_setting(msg.from_user.id, "setup_done"):
        await msg.answer(
            "👋 Привет!\n\n"
            "📸 Фото / 🎤 Голос / ✍️ Текст → калории\n"
            "💧 *'выпил стакан воды'* → вода\n"
            "💪 *'пробежал 5км'* или */burned 300* → сожжённые калории\n\n"
            "/today — еда за сегодня\n"
            "/diary — дневник с удалением\n"
            "/month — сводка за период\n"
            "/summary — итог дня\n"
            "/water — вода\n"
            "/goal — изменить цель\n"
            "/notify — время уведомлений\n"
            "/clear — очистить дневник",
            parse_mode="Markdown"
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male"),
         InlineKeyboardButton(text="👩 Женский",  callback_data="gender_female")]
    ])
    await msg.answer(
        "👋 Привет! Я твой health-трекер.\n\n"
        "Давай рассчитаем твою норму калорий. Укажи пол:",
        reply_markup=kb
    )
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
            [InlineKeyboardButton(text="🛋 Низкая (сидячая работа)",        callback_data="act_low")],
            [InlineKeyboardButton(text="🚶 Средняя (3-5 тренировок/нед)",  callback_data="act_medium")],
            [InlineKeyboardButton(text="🏃 Высокая (6-7 тренировок/нед)",  callback_data="act_high")],
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
        f"💧 'выпил стакан воды' — вода\n"
        f"💪 'пробежал 5км' — сожжённые калории\n\n"
        f"/summary — итог дня",
        parse_mode="Markdown"
    )

# ── /notify ────────────────────────────────────────────────────────────────────

@dp.message(Command("notify"))
async def cmd_notify(msg: Message, state: FSMContext):
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

# ── /goal ──────────────────────────────────────────────────────────────────────

@dp.message(Command("goal"))
async def cmd_goal(msg: Message, state: FSMContext):
    uid     = msg.from_user.id
    tdee    = get_setting(uid, "calories_goal")    or 2000
    deficit = get_setting(uid, "calories_deficit") or tdee - 500
    surplus = get_setting(uid, "calories_surplus") or tdee + 300
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📉 Похудеть ({deficit} ккал/день)",      callback_data="setgoal_lose")],
        [InlineKeyboardButton(text=f"⚖️ Поддержать вес ({tdee} ккал/день)",   callback_data="setgoal_maintain")],
        [InlineKeyboardButton(text=f"📈 Набрать массу ({surplus} ккал/день)", callback_data="setgoal_gain")],
        [InlineKeyboardButton(text="🔄 Пересчитать",                           callback_data="setgoal_recalc")],
    ])
    await msg.answer("🎯 Выбери цель:", reply_markup=kb)

@dp.callback_query(F.data.startswith("setgoal_"))
async def cb_setgoal(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[1]
    uid    = call.from_user.id
    if action == "recalc":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male"),
             InlineKeyboardButton(text="👩 Женский",  callback_data="gender_female")]
        ])
        await call.message.edit_text("Пересчитаем! Укажи пол:", reply_markup=kb)
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

# ── /diary — дневник с удалением ──────────────────────────────────────────────

@dp.message(Command("diary"))
async def cmd_diary(msg: Message):
    meals = get_today_meals(msg.from_user.id)
    if not meals:
        await msg.answer("📭 Сегодня записей нет.")
        return
    await msg.answer("📋 *Дневник за сегодня*\nНажми ❌ чтобы удалить запись:", parse_mode="Markdown")
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
    delete_meal(meal_id)
    await call.message.edit_text("🗑 Запись удалена.")

# ── /month — сводка за период ─────────────────────────────────────────────────

@dp.message(Command("month"))
async def cmd_month(msg: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня",    callback_data="period_today")],
        [InlineKeyboardButton(text="📅 7 дней",     callback_data="period_7")],
        [InlineKeyboardButton(text="📅 30 дней",    callback_data="period_30")],
    ])
    await msg.answer("За какой период показать статистику?", reply_markup=kb)

@dp.callback_query(F.data.startswith("period_"))
async def cb_period(call: CallbackQuery):
    period = call.data.split("_")[1]
    today  = date.today()

    if period == "today":
        start = today
    elif period == "7":
        from datetime import timedelta
        start = today - timedelta(days=6)
    else:
        from datetime import timedelta
        start = today - timedelta(days=29)

    meals = get_period_meals(
        call.from_user.id,
        start.strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d")
    )

    if not meals:
        await call.message.edit_text("📭 Нет данных за этот период.")
        return

    # Группируем по дням
    by_day = {}
    for m in meals:
        d = m["date"]
        if d not in by_day:
            by_day[d] = []
        by_day[d].append(m)

    lines = [f"📊 *Статистика за {len(by_day)} дней*\n```"]
    lines.append(f"{'Дата':<10} {'Ккал':>6} {'Б':>5} {'Ж':>5} {'У':>5}")
    lines.append("─" * 35)

    total_cal_all = 0
    for day, day_meals in sorted(by_day.items()):
        cal = sum(m["calories"] for m in day_meals)
        p   = sum(m.get("protein") or 0 for m in day_meals)
        f   = sum(m.get("fat")     or 0 for m in day_meals)
        c   = sum(m.get("carbs")   or 0 for m in day_meals)
        total_cal_all += cal
        # Форматируем дату покороче
        d_short = day[5:]  # MM-DD
        lines.append(f"{d_short:<10} {cal:>6} {p:>5.0f} {f:>5.0f} {c:>5.0f}")

    lines.append("─" * 35)
    avg = total_cal_all // len(by_day)
    lines.append(f"{'Среднее':<10} {avg:>6}")
    lines.append("```")

    await call.message.edit_text("\n".join(lines), parse_mode="Markdown")

# ── /burned ────────────────────────────────────────────────────────────────────

@dp.message(Command("burned"))
async def cmd_burned(msg: Message):
    try:
        cal = int(msg.text.split()[1])
        save_burned(msg.from_user.id, cal)
        total = get_today_burned(msg.from_user.id)
        await msg.answer(
            f"💪 +*{cal} ккал* сожжено!\nВсего сегодня: *{total} ккал*",
            parse_mode="Markdown"
        )
    except:
        await msg.answer("Используй так: /burned 300")

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

@dp.message(Command("clear"))
async def cmd_clear(msg: Message):
    today = date.today().strftime("%Y-%m-%d")
    sb.table("meals").delete().eq("user_id", msg.from_user.id).eq("date", today).execute()
    sb.table("water").delete().eq("user_id", msg.from_user.id).eq("date", today).execute()
    sb.table("burned").delete().eq("user_id", msg.from_user.id).eq("date", today).execute()
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
        data = await analyze_text(text)
        if data.get("is_water"):
            ml    = data.get("water_ml") or 250
            save_water(user_id, ml)
            total = get_today_water(user_id)
            goal  = get_setting(user_id, "water_goal_ml") or 2500
            await wait_msg.edit_text(
                f"💧 +*{ml} мл* воды!\n\n{water_bar(total, goal)}",
                parse_mode="Markdown"
            )
        elif data.get("is_burned"):
            cal   = data.get("burned_calories") or 0
            save_burned(user_id, cal)
            total = get_today_burned(user_id)
            await wait_msg.edit_text(
                f"💪 +*{cal} ккал* сожжено!\nВсего сегодня: *{total} ккал*",
                parse_mode="Markdown"
            )
        else:
            save_meal(user_id, data["food"], data["calories"],
                      data.get("protein",0), data.get("fat",0), data.get("carbs",0))
            total = sum(m["calories"] for m in get_today_meals(user_id))
            goal  = get_setting(user_id, "calories_goal") or 2000
            await wait_msg.edit_text(
                format_meal(data)+f"\n\n{calorie_bar(total, goal)}",
                parse_mode="Markdown"
            )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка: {e}")

# ── Автосводка ─────────────────────────────────────────────────────────────────

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

# ── Запуск ─────────────────────────────────────────────────────────────────────

async def main():
    print("Бот запущен с Supabase!")
    asyncio.create_task(auto_summary_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
