"""
Телеграм-бот для подсчёта калорий
Использует Google Gemini (бесплатно)
Поддерживает: фото еды, голосовые сообщения, текст
"""

import asyncio
import os
import sqlite3
import tempfile
import json
import base64
import aiohttp
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

import google.generativeai as genai

# ── Конфиг ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ВАШ_ТОКЕН_СЮДА")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "ВАШ_КЛЮЧ_СЮДА")
DB_PATH = "calories.db"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ── База данных ────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            date      TEXT    NOT NULL,
            time      TEXT    NOT NULL,
            food      TEXT    NOT NULL,
            calories  INTEGER NOT NULL,
            protein   REAL,
            fat       REAL,
            carbs     REAL
        )
    """)
    conn.commit()
    conn.close()

def save_meal(user_id, food, calories, protein=0, fat=0, carbs=0):
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO meals (user_id, date, time, food, calories, protein, fat, carbs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
         food, calories, protein, fat, carbs)
    )
    conn.commit()
    conn.close()

def get_today(user_id):
    today = date.today().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT time, food, calories, protein, fat, carbs FROM meals "
        "WHERE user_id=? AND date=? ORDER BY time",
        (user_id, today)
    ).fetchall()
    conn.close()
    return [{"time": r[0], "food": r[1], "calories": r[2],
             "protein": r[3], "fat": r[4], "carbs": r[5]} for r in rows]

# ── Gemini-утилиты ─────────────────────────────────────────────────────────────

PROMPT = """Ты диетолог-аналитик. Определи калории и БЖУ.
Ответь ТОЛЬКО в формате JSON без лишнего текста, без markdown:
{"food":"название","weight_g":100,"calories":200,"protein":10,"fat":5,"carbs":20,"comment":""}"""

def parse_gemini(text):
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())

async def analyze_text(text):
    resp = model.generate_content(f"{PROMPT}\n\nЕда: {text}")
    return parse_gemini(resp.text)

async def analyze_image(image_bytes):
    img = {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}
    resp = model.generate_content([PROMPT + "\n\nОпредели что на фото и посчитай калории.", img])
    return parse_gemini(resp.text)

async def transcribe_voice(ogg_bytes):
    """Конвертируем через Gemini Audio"""
    audio = {"mime_type": "audio/ogg", "data": base64.b64encode(ogg_bytes).decode()}
    resp = model.generate_content([
        "Транскрибируй это голосовое сообщение на русском языке. Верни только текст без пояснений.",
        audio
    ])
    return resp.text.strip()

# ── Форматирование ─────────────────────────────────────────────────────────────

def format_result(data):
    lines = [
        f"🍽 *{data['food']}*",
        f"⚖️ ~{data.get('weight_g', '?')} г",
        f"🔥 {data['calories']} ккал",
        f"🥩 Б: {data.get('protein', 0):.0f}г  "
        f"🧈 Ж: {data.get('fat', 0):.0f}г  "
        f"🍞 У: {data.get('carbs', 0):.0f}г",
    ]
    if data.get("comment"):
        lines.append(f"💬 _{data['comment']}_")
    return "\n".join(lines)

def format_summary(meals):
    if not meals:
        return "📭 Сегодня записей нет. Отправь фото или опиши что ел!"

    total_cal = sum(m["calories"] for m in meals)
    total_p   = sum(m["protein"] or 0 for m in meals)
    total_f   = sum(m["fat"]     or 0 for m in meals)
    total_c   = sum(m["carbs"]   or 0 for m in meals)

    rows = ["📅 *Дневник за сегодня*\n"]
    rows.append("```")
    rows.append(f"{'Время':<6} {'Блюдо':<22} {'Ккал':>5}")
    rows.append("─" * 35)
    for m in meals:
        name = m["food"][:21]
        rows.append(f"{m['time']:<6} {name:<22} {m['calories']:>5}")
    rows.append("─" * 35)
    rows.append(f"{'ИТОГО':<28} {total_cal:>5}")
    rows.append("```")
    rows.append(
        f"\n🥩 Белки: *{total_p:.0f}г*  "
        f"🧈 Жиры: *{total_f:.0f}г*  "
        f"🍞 Углеводы: *{total_c:.0f}г*"
    )
    return "\n".join(rows)

# ── Хэндлеры ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer(
        "👋 Привет! Я считаю калории.\n\n"
        "Отправь мне:\n"
        "📸 *Фото* еды или напитка\n"
        "🎤 *Голосовое* — расскажи что съел\n"
        "✍️ *Текст* — напиши название блюда\n\n"
        "Команды:\n"
        "/today — дневник за сегодня\n"
        "/clear — очистить дневник сегодня",
        parse_mode="Markdown"
    )

@dp.message(Command("today"))
async def cmd_today(msg: Message):
    meals = get_today(msg.from_user.id)
    await msg.answer(format_summary(meals), parse_mode="Markdown")

@dp.message(Command("clear"))
async def cmd_clear(msg: Message):
    today = date.today().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM meals WHERE user_id=? AND date=?",
                 (msg.from_user.id, today))
    conn.commit()
    conn.close()
    await msg.answer("🗑 Дневник за сегодня очищен.")

@dp.message(F.photo)
async def handle_photo(msg: Message):
    wait = await msg.answer("🔍 Анализирую фото...")
    try:
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                image_bytes = await r.read()

        data = await analyze_image(image_bytes)
        save_meal(msg.from_user.id, data["food"], data["calories"],
                  data.get("protein", 0), data.get("fat", 0), data.get("carbs", 0))

        today_total = sum(m["calories"] for m in get_today(msg.from_user.id))
        text = format_result(data) + f"\n\n📊 Всего сегодня: *{today_total} ккал*"
        await wait.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.voice)
async def handle_voice(msg: Message):
    wait = await msg.answer("🎙 Распознаю голосовое...")
    try:
        file = await bot.get_file(msg.voice.file_id)
        url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                ogg_bytes = await r.read()

        text = await transcribe_voice(ogg_bytes)
        await wait.edit_text(f"📝 Распознано: _{text}_\n\n🔍 Считаю калории...",
                              parse_mode="Markdown")

        data = await analyze_text(text)
        save_meal(msg.from_user.id, data["food"], data["calories"],
                  data.get("protein", 0), data.get("fat", 0), data.get("carbs", 0))

        today_total = sum(m["calories"] for m in get_today(msg.from_user.id))
        result = format_result(data) + f"\n\n📊 Всего сегодня: *{today_total} ккал*"
        await wait.edit_text(result, parse_mode="Markdown")
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    wait = await msg.answer("🔍 Считаю калории...")
    try:
        data = await analyze_text(msg.text)
        save_meal(msg.from_user.id, data["food"], data["calories"],
                  data.get("protein", 0), data.get("fat", 0), data.get("carbs", 0))

        today_total = sum(m["calories"] for m in get_today(msg.from_user.id))
        text = format_result(data) + f"\n\n📊 Всего сегодня: *{today_total} ккал*"
        await wait.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")

# ── Запуск ─────────────────────────────────────────────────────────────────────

async def main():
    init_db()
    print("Бот запущен (Gemini)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

