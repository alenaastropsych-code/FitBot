"""
Фит-бот Алёны: напоминания, вес, еда (КБЖУ по фото через нейросеть), вода, тренировки.

Переменные окружения:
  BOT_TOKEN           — токен от @BotFather (обязательно)
  AI_API_KEY          — ключ ProxyAPI (или OPENROUTER_API_KEY) для подсчёта калорий
  AI_BASE_URL         — адрес API (по умолчанию ProxyAPI)
  AI_MODEL            — модель (по умолчанию anthropic/claude-haiku-4-5)
  OWNER_ID            — твой Telegram ID (необязательно; иначе владельцем станет первый, кто нажмёт /start)
  DB_PATH             — путь к базе (по умолчанию fitbot.db рядом с bot.py)
"""

import asyncio
import base64
import html
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------------------------------------------------------------- настройки

TZ = ZoneInfo("Europe/Moscow")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Ключ нейросети: AI_API_KEY (ProxyAPI) или OPENROUTER_API_KEY
AI_API_KEY = os.getenv("AI_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")
# Адрес API: по умолчанию ProxyAPI (оплата российской картой). Для OpenRouter:
#   https://openrouter.ai/api/v1/chat/completions
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.proxyapi.ru/v1/chat/completions")
AI_MODEL = os.getenv("AI_MODEL", "anthropic/claude-haiku-4-5")
OWNER_ID_ENV = os.getenv("OWNER_ID", "")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fitbot.db"))

# Дневные нормы
NORM = {
    "kcal": (1500, 1600),
    "p": (110, 120),
    "f": (50, 55),
    "c": (140, 160),
    "water": (1500, 2000),
}
GYM_DAYS = {0, 2, 4}  # пн, ср, пт

# План по весу: конец недели -> (база, мечта)
PLAN = [
    ("2026-10-11", 79.4, 79.3),
    ("2026-10-18", 78.1, 77.9),
    ("2026-10-25", 77.1, 76.7),
    ("2026-11-01", 76.2, 75.7),
    ("2026-11-08", 75.5, 74.9),
    ("2026-11-15", 74.9, 74.2),
    ("2026-11-22", 74.3, 73.5),
    ("2026-11-29", 73.7, 72.8),
    ("2026-12-06", 73.1, 72.1),
    ("2026-12-13", 72.5, 71.4),
    ("2026-12-20", 72.0, 70.8),
    ("2026-12-27", 71.5, 70.2),
    ("2026-12-31", 71.2, 69.9),
]
START_WEIGHT = 80.4
GOAL_BASE, GOAL_DREAM = 71.2, 69.9

# Начальные данные (первые дни, которые уже были)
SEED_WEIGHTS = [("2026-10-05", 80.4), ("2026-10-06", 79.3)]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fitbot")

# ---------------------------------------------------------------- база


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS weights (day TEXT PRIMARY KEY, kg REAL);
            CREATE TABLE IF NOT EXISTS food (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, day TEXT, description TEXT,
                kcal REAL, p REAL, f REAL, c REAL
            );
            CREATE TABLE IF NOT EXISTS water (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, ml INTEGER
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, kind TEXT, value REAL
            );
            """
        )
        for d, kg in SEED_WEIGHTS:
            con.execute("INSERT OR IGNORE INTO weights(day, kg) VALUES (?, ?)", (d, kg))


def get_setting(key: str) -> str | None:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def owner_id() -> int | None:
    if OWNER_ID_ENV.strip():
        return int(OWNER_ID_ENV.strip())
    v = get_setting("owner_id")
    return int(v) if v else None


# ---------------------------------------------------------------- данные


def save_weight(d: date, kg: float) -> None:
    with db() as con:
        con.execute("INSERT OR REPLACE INTO weights(day, kg) VALUES (?, ?)", (d.isoformat(), kg))


def weight_on(d: date) -> float | None:
    with db() as con:
        row = con.execute("SELECT kg FROM weights WHERE day=?", (d.isoformat(),)).fetchone()
    return row["kg"] if row else None


def last_weight_before(d: date) -> tuple[str, float] | None:
    with db() as con:
        row = con.execute(
            "SELECT day, kg FROM weights WHERE day<? ORDER BY day DESC LIMIT 1", (d.isoformat(),)
        ).fetchone()
    return (row["day"], row["kg"]) if row else None


def avg_weight(end: date, days: int = 7) -> tuple[float | None, int]:
    start = end - timedelta(days=days - 1)
    with db() as con:
        rows = con.execute(
            "SELECT kg FROM weights WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
        ).fetchall()
    if not rows:
        return None, 0
    return round(sum(r["kg"] for r in rows) / len(rows), 2), len(rows)


def add_food(d: date, description: str, kcal: float, p: float, f: float, c: float) -> int:
    with db() as con:
        cur = con.execute(
            "INSERT INTO food(ts, day, description, kcal, p, f, c) VALUES (?,?,?,?,?,?,?)",
            (now().isoformat(), d.isoformat(), description, kcal, p, f, c),
        )
        return cur.lastrowid


def undo_last_food(d: date) -> sqlite3.Row | None:
    with db() as con:
        row = con.execute(
            "SELECT * FROM food WHERE day=? ORDER BY id DESC LIMIT 1", (d.isoformat(),)
        ).fetchone()
        if row:
            con.execute("DELETE FROM food WHERE id=?", (row["id"],))
    return row


def food_totals(d: date) -> dict:
    with db() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(kcal),0) kcal, COALESCE(SUM(p),0) p, COALESCE(SUM(f),0) f, "
            "COALESCE(SUM(c),0) c, COUNT(*) n FROM food WHERE day=?",
            (d.isoformat(),),
        ).fetchone()
    return dict(row)


def add_water(d: date, ml: int) -> None:
    if ml <= 0:
        return
    with db() as con:
        con.execute(
            "INSERT INTO water(ts, day, ml) VALUES (?,?,?)", (now().isoformat(), d.isoformat(), ml)
        )


def water_total(d: date) -> int:
    with db() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(ml),0) s FROM water WHERE day=?", (d.isoformat(),)
        ).fetchone()
    return int(row["s"])


def add_activity(d: date, kind: str, value: float = 1) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO activity(ts, day, kind, value) VALUES (?,?,?,?)",
            (now().isoformat(), d.isoformat(), kind, value),
        )


def activity_on(d: date) -> dict:
    with db() as con:
        rows = con.execute(
            "SELECT kind, SUM(value) v, COUNT(*) n FROM activity WHERE day=? GROUP BY kind",
            (d.isoformat(),),
        ).fetchall()
    return {r["kind"]: {"sum": r["v"], "count": r["n"]} for r in rows}


def plan_target(d: date) -> tuple[str, float, float]:
    for end, base, dream in PLAN:
        if date.fromisoformat(end) >= d:
            return end, base, dream
    return PLAN[-1]


# ---------------------------------------------------------------- форматирование


def fmt(x: float, nd: int = 1) -> str:
    s = f"{x:.{nd}f}".rstrip("0").rstrip(".") if nd else f"{x:.0f}"
    return s.replace(".", ",")


def mark(value: float, lo: float, hi: float) -> str:
    if value < lo:
        return "⬇️"
    if value > hi:
        return "⬆️"
    return "✅"


def ru_date(d: date) -> str:
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    return f"{d.day} {months[d.month - 1]}"


def day_summary(d: date) -> str:
    t = food_totals(d)
    w = water_total(d)
    a = activity_on(d)
    kg = weight_on(d)
    lines = [f"<b>📊 {ru_date(d)}</b>"]
    lines.append(f"⚖️ Вес: {fmt(kg) + ' кг' if kg else 'не записан'}")
    if t["n"]:
        lo, hi = NORM["kcal"]
        lines.append(f"🔥 Калории: {fmt(t['kcal'], 0)} из {lo}–{hi} {mark(t['kcal'], lo, hi)}")
        lines.append(f"🥩 Белок: {fmt(t['p'], 0)} г из {NORM['p'][0]}–{NORM['p'][1]} {mark(t['p'], *NORM['p'])}")
        lines.append(f"🧈 Жиры: {fmt(t['f'], 0)} г из {NORM['f'][0]}–{NORM['f'][1]} {mark(t['f'], *NORM['f'])}")
        lines.append(f"🍞 Углеводы: {fmt(t['c'], 0)} г из {NORM['c'][0]}–{NORM['c'][1]} {mark(t['c'], *NORM['c'])}")
        left = NORM["kcal"][1] - t["kcal"]
        if left > 0:
            lines.append(f"Осталось до нормы: ~{fmt(left, 0)} ккал")
    else:
        lines.append("🍽 Еда: пока ничего не записано")
    lines.append(f"💧 Вода: {fmt(w / 1000, 2)} л из 1,5–2 {mark(w, *NORM['water'])}")
    acts = []
    if "zaryadka" in a:
        acts.append("зарядка ✅")
    if "gym" in a:
        acts.append("зал ✅")
    if "steps" in a:
        acts.append(f"прогулка {int(a['steps']['sum'])} шагов ✅")
    lines.append("🏃 Движение: " + (", ".join(acts) if acts else "пока нет"))
    return "\n".join(lines)


def weight_reply(d: date, kg: float) -> str:
    prev = last_weight_before(d)
    end, base, dream = plan_target(d)
    avg, n = avg_weight(d)
    lines = [f"⚖️ Записала: <b>{fmt(kg)} кг</b> ({ru_date(d)})"]
    if prev:
        diff = kg - prev[1]
        sign = "+" if diff > 0 else ""
        lines.append(f"К прошлому взвешиванию: {sign}{fmt(diff)} кг")
    lines.append(f"С начала (80,4): {fmt(kg - START_WEIGHT)} кг")
    if avg is not None:
        lines.append(f"Среднее за 7 дней: {fmt(avg, 2)} кг (взвешиваний: {n})")
    lines.append(f"🎯 Цель к {ru_date(date.fromisoformat(end))}: {fmt(base)} (мечта {fmt(dream)})")
    lines.append(f"До финиша: {fmt(kg - GOAL_BASE)} кг до 71,2 · {fmt(kg - GOAL_DREAM)} кг до 69,9")
    if prev and kg - prev[1] >= 0.5:
        lines.append("\nСкачок вверх за день — почти всегда вода (соль, цикл, тренировка). Смотрим на среднее за неделю 🙂")
    return "\n".join(lines)


def week_summary(end: date) -> str:
    start = end - timedelta(days=6)
    days = [start + timedelta(days=i) for i in range(7)]
    avg, n = avg_weight(end)
    prev_avg, _ = avg_weight(start - timedelta(days=1))
    pend, base, dream = plan_target(end)
    kcal_days = [food_totals(d) for d in days]
    logged = [t for t in kcal_days if t["n"]]
    gym = sum(1 for d in days if "gym" in activity_on(d))
    zar = sum(1 for d in days if "zaryadka" in activity_on(d))
    steps = [activity_on(d).get("steps", {}).get("sum", 0) for d in days]
    water = [water_total(d) for d in days]
    lines = [f"<b>📈 Неделя {ru_date(start)} – {ru_date(end)}</b>"]
    if avg is not None:
        lines.append(f"⚖️ Средний вес: <b>{fmt(avg, 2)} кг</b> ({n} взвеш.)")
        if prev_avg is not None:
            lines.append(f"К прошлой неделе: {fmt(avg - prev_avg, 2)} кг")
        lines.append(f"🎯 План к {ru_date(date.fromisoformat(pend))}: {fmt(base)} (мечта {fmt(dream)})")
        gap = avg - base
        if gap <= 0:
            lines.append("Идёшь по плану или быстрее 💪")
        elif gap <= 1:
            lines.append(f"Отставание {fmt(gap)} кг — в пределах нормы")
        else:
            lines.append(f"Отставание {fmt(gap)} кг — давай найдём утечку, а не урезать калории")
    else:
        lines.append("⚖️ Взвешиваний за неделю нет")
    if logged:
        k = sum(t["kcal"] for t in logged) / len(logged)
        p = sum(t["p"] for t in logged) / len(logged)
        lines.append(f"🔥 Калории в среднем: {fmt(k, 0)} в день (записано дней: {len(logged)})")
        lines.append(f"🥩 Белок в среднем: {fmt(p, 0)} г")
    lines.append(f"🏋️ Зал: {gym} из 3 · 🤸 Зарядка: {zar} из 7")
    walk_days = sum(1 for s in steps if s)
    if walk_days:
        lines.append(f"🚶 Прогулки: {walk_days} дн., в среднем {fmt(sum(steps) / walk_days, 0)} шагов")
    water_days = [w for w in water if w]
    if water_days:
        lines.append(f"💧 Вода в среднем: {fmt(sum(water_days) / len(water_days) / 1000, 2)} л (дней с записью: {len(water_days)})")
    return "\n".join(lines)


# ---------------------------------------------------------------- нейросеть

AI_SYSTEM = (
    "Ты нутрициолог-ассистент. Пользователь — женщина 36 лет, худеет, норма 1500–1600 ккал, "
    "белок 110–120 г, жиры 50–55 г, углеводы 140–160 г. По фото и/или описанию еды оцени состав "
    "и КБЖУ. Порции оценивай реалистично по размеру тарелки и приборов. Если в описании указаны "
    "граммы или объём — используй их. Напитки без калорий (вода, чай, кофе без сахара) считай как 0 ккал, "
    "но укажи их объём в water_ml (чай, кофе, вода, морс, суп — тоже вода; лечебные минеральные и "
    "алкоголь не считай). Если на фото не еда — верни пустой items.\n"
    "Ответь ТОЛЬКО JSON без пояснений вокруг, формат:\n"
    '{"items":[{"name":"...","portion":"~150 г","kcal":0,"p":0,"f":0,"c":0}],'
    '"water_ml":0,"comment":"1–2 коротких дружелюбных совета по-русски"}'
)


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("нет JSON в ответе")
    return json.loads(text[start : end + 1])


async def ai_estimate(description: str, image_bytes: bytes | None, totals: dict) -> dict:
    if not AI_API_KEY:
        raise RuntimeError("Не задан ключ нейросети AI_API_KEY")
    context = (
        f"Уже съедено сегодня: {fmt(totals['kcal'], 0)} ккал, белок {fmt(totals['p'], 0)} г, "
        f"жиры {fmt(totals['f'], 0)} г, углеводы {fmt(totals['c'], 0)} г."
    )
    content: list = [{"type": "text", "text": f"{context}\nОписание: {description or '(только фото)'}"}]
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": content},
        ],
        "max_tokens": 1200,
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            AI_BASE_URL,
            headers={"Authorization": f"Bearer {AI_API_KEY}"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    text = data["choices"][0]["message"]["content"]
    if isinstance(text, list):  # некоторые модели отдают список блоков
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    return extract_json(text)


def food_reply(result: dict, d: date) -> str:
    items = result.get("items") or []
    lines = []
    tk = tp = tf = tc = 0.0
    for it in items:
        k, p, f, c = (float(it.get(x, 0) or 0) for x in ("kcal", "p", "f", "c"))
        tk, tp, tf, tc = tk + k, tp + p, tf + f, tc + c
        name = html.escape(str(it.get("name", "")))
        portion = html.escape(str(it.get("portion", "")))
        lines.append(f"• {name} {portion} — {fmt(k, 0)} ккал (Б {fmt(p, 0)} / Ж {fmt(f, 0)} / У {fmt(c, 0)})")
    head = f"🍽 <b>~{fmt(tk, 0)} ккал</b> · Б {fmt(tp, 0)} · Ж {fmt(tf, 0)} · У {fmt(tc, 0)}"
    out = [head] + lines
    water = int(result.get("water_ml") or 0)
    if water:
        out.append(f"💧 +{water} мл к воде")
    if result.get("comment"):
        out.append(f"\n💬 {html.escape(str(result['comment']))}")
    t = food_totals(d)
    left = NORM["kcal"][1] - t["kcal"]
    out.append(
        f"\n<b>Итог дня:</b> {fmt(t['kcal'], 0)} ккал · белок {fmt(t['p'], 0)} г · "
        f"жиры {fmt(t['f'], 0)} г · углеводы {fmt(t['c'], 0)} г"
    )
    out.append(f"Осталось: ~{fmt(left, 0)} ккал" if left > 0 else f"Норма превышена на {fmt(-left, 0)} ккал")
    out.append("Ошиблась с оценкой? «↩️ Отменить еду» и пришли заново с уточнением.")
    return "\n".join(out)


# ---------------------------------------------------------------- бот

BTN_FOOD = "🍽 Еда"
BTN_W250 = "💧 +250 мл"
BTN_W500 = "💧 +500 мл"
BTN_ZAR = "🤸 Зарядка"
BTN_GYM = "🏋️ Зал"
BTN_WALK = "🚶 Прогулка"
BTN_TODAY = "📊 Сегодня"
BTN_WEEK = "📈 Неделя"
BTN_UNDO = "↩️ Отменить еду"

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_FOOD), KeyboardButton(text=BTN_W250), KeyboardButton(text=BTN_W500)],
        [KeyboardButton(text=BTN_ZAR), KeyboardButton(text=BTN_GYM), KeyboardButton(text=BTN_WALK)],
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_UNDO)],
    ],
    resize_keyboard=True,
)

dp = Dispatcher()
pending: dict[int, str] = {}  # chat_id -> чего ждём ("steps")


def allowed(message: Message) -> bool:
    oid = owner_id()
    return oid is None or message.chat.id == oid


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    oid = owner_id()
    if oid is None:
        set_setting("owner_id", str(message.chat.id))
    elif message.chat.id != oid:
        await message.answer("Это личный бот, извини 🙂")
        return
    await message.answer(
        "Привет, Алёна! Я твой фит-бот 💪\n\n"
        "• Пришли <b>число</b> (например 79,3) — запишу вес\n"
        "• Пришли <b>фото еды</b> или опиши её текстом — посчитаю КБЖУ\n"
        "• Кнопки — вода, зарядка, зал, прогулка, сводки\n\n"
        "Напоминания: 7:43 — вес и зарядка, 15:00 — вода, 20:52 — итог дня, "
        "воскресенье 19:47 — разбор недели.\n\n"
        f"Твой Telegram ID: <code>{message.chat.id}</code>",
        reply_markup=KEYBOARD,
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


@dp.message(F.text == BTN_TODAY)
@dp.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not allowed(message):
        return
    await message.answer(day_summary(today()), reply_markup=KEYBOARD)


@dp.message(F.text == BTN_WEEK)
@dp.message(Command("week"))
async def cmd_week(message: Message) -> None:
    if not allowed(message):
        return
    await message.answer(week_summary(today()), reply_markup=KEYBOARD)


@dp.message(F.text == BTN_UNDO)
@dp.message(Command("undo"))
async def cmd_undo(message: Message) -> None:
    if not allowed(message):
        return
    row = undo_last_food(today())
    if row:
        await message.answer(
            f"Удалила: {html.escape(row['description'][:80])} ({fmt(row['kcal'], 0)} ккал)\n\n"
            + day_summary(today())
        )
    else:
        await message.answer("Сегодня ещё нечего отменять.")


@dp.message(F.text.in_({BTN_W250, BTN_W500}))
async def btn_water(message: Message) -> None:
    if not allowed(message):
        return
    ml = 250 if message.text == BTN_W250 else 500
    add_water(today(), ml)
    w = water_total(today())
    tail = " Норма выполнена ✅" if w >= NORM["water"][0] else ""
    await message.answer(f"💧 +{ml} мл. Сегодня: {fmt(w / 1000, 2)} л из 1,5–2.{tail}")


@dp.message(F.text == BTN_ZAR)
async def btn_zar(message: Message) -> None:
    if not allowed(message):
        return
    add_activity(today(), "zaryadka")
    await message.answer("🤸 Зарядка засчитана! Отличное начало дня.")


@dp.message(F.text == BTN_GYM)
async def btn_gym(message: Message) -> None:
    if not allowed(message):
        return
    add_activity(today(), "gym")
    start = today() - timedelta(days=today().weekday())
    n = sum(1 for i in range(7) if "gym" in activity_on(start + timedelta(days=i)))
    await message.answer(f"🏋️ Зал засчитан! На этой неделе: {n} из 3.")


@dp.message(F.text == BTN_WALK)
async def btn_walk(message: Message) -> None:
    if not allowed(message):
        return
    pending[message.chat.id] = "steps"
    await message.answer("🚶 Сколько шагов? Пришли число (например 7500) или км (например «5 км»).")


@dp.message(F.text == BTN_FOOD)
async def btn_food(message: Message) -> None:
    if not allowed(message):
        return
    await message.answer("Пришли фото еды (можно с подписью) или опиши текстом, что и сколько съела.")


NUM_RE = re.compile(r"^\s*(?:вес\s*)?(\d{1,5}(?:[.,]\d{1,2})?)\s*(кг|км|шаг\w*)?\s*$", re.IGNORECASE)


async def handle_food(message: Message, description: str, image: bytes | None) -> None:
    d = today()
    wait = await message.answer("Считаю… 🧮")
    try:
        result = await ai_estimate(description, image, food_totals(d))
    except Exception as e:  # noqa: BLE001
        log.exception("AI error")
        await wait.edit_text(f"Не получилось посчитать: {html.escape(str(e))[:200]}. Попробуй ещё раз чуть позже.")
        return
    items = result.get("items") or []
    water = int(result.get("water_ml") or 0)
    if not items and not water:
        await wait.edit_text("Не увидела тут еды 🤔 Пришли фото тарелки или опиши, что съела.")
        return
    if items:
        names = ", ".join(str(i.get("name", "")) for i in items)
        add_food(
            d,
            (description + " | " if description else "") + names,
            sum(float(i.get("kcal", 0) or 0) for i in items),
            sum(float(i.get("p", 0) or 0) for i in items),
            sum(float(i.get("f", 0) or 0) for i in items),
            sum(float(i.get("c", 0) or 0) for i in items),
        )
    if water:
        add_water(d, water)
    await wait.edit_text(food_reply(result, d))


@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    if not allowed(message):
        return
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = await bot.download_file(file.file_path)
    await handle_food(message, message.caption or "", buf.read())


@dp.message(F.text)
async def on_text(message: Message) -> None:
    if not allowed(message):
        return
    text = message.text.strip()
    m = NUM_RE.match(text)
    if m:
        value = float(m.group(1).replace(",", "."))
        unit = (m.group(2) or "").lower()
        if (pending.get(message.chat.id) == "steps" or unit == "км" or unit.startswith("шаг")
                or (not unit and value >= 1000)):
            pending.pop(message.chat.id, None)
            steps = int(value * 1300) if (unit == "км" or value < 50) else int(value)
            add_activity(today(), "steps", steps)
            km = "" if unit == "км" or value < 50 else f" (~{fmt(steps / 1300)} км)"
            await message.answer(f"🚶 Записала прогулку: {steps} шагов{km}. Отлично!")
            return
        if 35 <= value <= 200:
            save_weight(today(), value)
            await message.answer(weight_reply(today(), value))
            return
    pending.pop(message.chat.id, None)
    await handle_food(message, text, None)


# ---------------------------------------------------------------- напоминания


async def send_owner(bot: Bot, text: str) -> None:
    oid = owner_id()
    if oid is None:
        log.info("Нет владельца — напоминание пропущено")
        return
    try:
        await bot.send_message(oid, text, reply_markup=KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить напоминание")


async def remind_morning(bot: Bot) -> None:
    d = today()
    end, base, dream = plan_target(d)
    gym = "\n🏋️ Сегодня день зала!" if d.weekday() in GYM_DAYS else ""
    await send_owner(
        bot,
        "☀️ Доброе утро, Алёна!\n"
        "⚖️ Взвесься натощак и пришли вес числом.\n"
        f"🤸 Зарядка 15–20 минут.{gym}\n"
        "🥣 В завтраке — белок + углеводы (каша/хлеб/фрукт).\n"
        f"🎯 Цель недели: {fmt(base)} кг (мечта {fmt(dream)})",
    )


async def remind_water(bot: Bot) -> None:
    w = water_total(today())
    if w < 800:
        await send_owner(bot, f"💧 Сейчас воды всего {fmt(w / 1000, 2)} л. Выпей стакан-другой 🙂")


async def remind_evening(bot: Bot) -> None:
    d = today()
    missing = []
    if weight_on(d) is None:
        missing.append("вес")
    if not food_totals(d)["n"]:
        missing.append("еду")
    a = activity_on(d)
    if "zaryadka" not in a:
        missing.append("зарядку (если была)")
    if d.weekday() in GYM_DAYS and "gym" not in a:
        missing.append("зал (если был)")
    if "steps" not in a:
        missing.append("прогулку")
    ask = ("\n\n✍️ Не записано: " + ", ".join(missing)) if missing else "\n\nВсё записано — ты молодец! 🌙"
    await send_owner(bot, "🌙 Итог дня\n\n" + day_summary(d) + ask)


async def remind_week(bot: Bot) -> None:
    await send_owner(
        bot,
        week_summary(today())
        + "\n\n📝 Разбор недели: замерь талию и подумай — что получилось, что мешало, "
        "и какое одно правило возьмёшь на следующую неделю. Спланируй тренировки в календаре.",
    )


# ---------------------------------------------------------------- запуск


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(remind_morning, CronTrigger(hour=7, minute=43, timezone=TZ), args=[bot])
    scheduler.add_job(remind_water, CronTrigger(hour=15, minute=0, timezone=TZ), args=[bot])
    scheduler.add_job(remind_evening, CronTrigger(hour=20, minute=52, timezone=TZ), args=[bot])
    scheduler.add_job(remind_week, CronTrigger(day_of_week="sun", hour=19, minute=47, timezone=TZ), args=[bot])
    scheduler.start()
    log.info("Бот запущен, модель: %s", AI_MODEL)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
