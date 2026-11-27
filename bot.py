"""Бот графиків відключень ДТЕК для запуска на GitHub Actions.

Один запуск = одна проверка:
1. открывает сайт ДТЕК и берёт график вашей очереди;
2. если график на сегодня/завтра новый или изменился — шлёт сообщение и картинку;
3. если вы писали боту /schedule — отвечает текущим графиком.
Токен и chat_id берутся из секретов GitHub, в коде их нет.
"""
import asyncio
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GROUP = os.environ.get("GROUP") or "GPV4.2"
ADDRESS = os.environ.get("ADDRESS") or ""
# FORCE=true: запуск по кнопке «Оновити» — прислать график, даже если он не менялся
FORCE = os.environ.get("FORCE", "").lower() == "true"
DIAG = ""  # заполняется, если график на сегодня/завтра не найден

URL = "https://www.dtek-dnem.com.ua/ua/shutdowns"
STATE_FILE = "state.json"
TZ = ZoneInfo("Europe/Kyiv")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
S = 2  # масштаб картинки


# ---------- данные с сайта ----------
async def fetch_fact():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        fact = await page.evaluate(
            "() => typeof DisconSchedule !== 'undefined' ? DisconSchedule.fact : null"
        )
        if not fact:
            print("DisconSchedule.fact не найден. Заголовок страницы:", await page.title())
            body = await page.inner_text("body")
            print("Начало страницы:", body[:500].replace("\n", " "))
        await browser.close()
        return fact


def to_slots(hours):
    """48 получасовых слотов: True = света нет."""
    slots = []
    for h in range(1, 25):
        v = hours.get(str(h), "yes")
        slots += [v in ("no", "first"), v in ("no", "second")]
    return slots


def find_group(groups):
    """Ключ вашей очереди в данных дня (ищем и по номеру, на случай смены названия)."""
    if GROUP in groups:
        return GROUP
    num = GROUP.replace("GPV", "").strip()
    for k in groups:
        if str(k).replace("GPV", "").strip() == num:
            return k
    return None


def get_days(fact):
    """[(дата, слоты)] на сегодня и завтра для вашей очереди."""
    global DIAG
    today = datetime.now(TZ).date()
    result = []
    notes = []
    for ts, groups in fact["data"].items():
        day = datetime.fromtimestamp(int(ts), TZ).date()
        if day not in (today, today + timedelta(days=1)):
            continue
        key = find_group(groups)
        if key is None:
            sample = next(iter(groups.values()), None) if groups else None
            notes.append(f"{day}: очереди {GROUP} нет; есть {list(groups)[:30]}; "
                         f"пример данных: {str(sample)[:200]}")
            continue
        result.append((day, to_slots(groups[key])))
    if not result:
        dates = [datetime.fromtimestamp(int(t), TZ).date().isoformat() for t in fact["data"]]
        DIAG = (f"ДИАГНОСТИКА: сегодня {today} | даты в данных сайта: {dates} | "
                f"update: {fact.get('update')} | today: {fact.get('today')} | "
                f"ключи fact: {list(fact)[:15]} | " + " || ".join(notes))
        print(DIAG)
    return sorted(result, key=lambda x: x[0])


def stats(slots):
    outages = sum(1 for i, s in enumerate(slots) if s and (i == 0 or not slots[i - 1]))
    return outages, sum(slots) / 2


def hours_text(x):
    if x != int(x):
        return f"{str(x).replace('.', ',')} години"
    n = int(x)
    if n % 10 == 1 and n % 100 != 11:
        w = "година"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        w = "години"
    else:
        w = "годин"
    return f"{n} {w}"


# ---------- список отключений для напоминаний ----------
def build_outages(days):
    """Непрерывные отключения [(начало, конец)], с учётом перехода через полночь."""
    outages = []
    for day, slots in days:
        base = datetime.combine(day, datetime.min.time(), TZ)
        i = 0
        while i < 48:
            if slots[i]:
                j = i
                while j < 48 and slots[j]:
                    j += 1
                start = base + timedelta(minutes=30 * i)
                end = base + timedelta(minutes=30 * j)
                if outages and outages[-1][1] == start:  # продолжение с прошлого дня
                    outages[-1] = (outages[-1][0], end)
                else:
                    outages.append((start, end))
                i = j
            else:
                i += 1
    return outages


def save_outages(days):
    data = {
        "group": GROUP,
        "outages": [{"start": s.isoformat(), "end": e.isoformat()}
                    for s, e in build_outages(days)],
    }
    with open("schedule.json", "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ---------- сводка по всем очередям (чтобы найти свою) ----------
def _gkey(name):
    try:
        return tuple(int(x) for x in str(name).replace("GPV", "").split("."))
    except ValueError:
        return (999, str(name))


def groups_overview(fact):
    """Текст: у каждой очереди из данных сайта — когда нет света сегодня."""
    today = datetime.now(TZ).date()
    lines = []
    for ts, groups in fact["data"].items():
        day = datetime.fromtimestamp(int(ts), TZ).date()
        if day != today:
            continue
        for name in sorted(groups, key=_gkey):
            outs = build_outages([(day, to_slots(groups[name]))])
            parts = []
            for s, e in outs:
                et = e.strftime("%H:%M")
                if et == "00:00" and e.date() != day:
                    et = "24:00"
                parts.append(f"{s.strftime('%H:%M')}–{et}")
            lines.append(f"{str(name).replace('GPV', '')}: " + (", ".join(parts) or "без відключень"))
    return "\n".join(lines)


# ---------- текст ----------
def build_text(day, label, slots, kind):
    n, total = stats(slots)
    date = f"{day.day} {MONTHS[day.month - 1]}"
    if kind == "new":
        title = f"📅 З'явився графік на {label}, {date}"
    elif kind == "changed":
        title = f"🔄 Графік на {label}, {date} змінився"
    else:
        title = f"📅 Графік на {label}, {date}"
    lines = [title]
    if ADDRESS:
        lines.append(ADDRESS)
    lines.append(f"Дніпропетровська обл. · черга {GROUP.replace('GPV', '')}")
    lines.append("")
    if n == 0:
        lines.append("✅ Відключень не заплановано.")
    else:
        lines.append(f"🕯 Відключень: {n}, разом без світла {hours_text(total)}.")
        lines.append("Точний час — на картинці нижче 👇")
    return "\n".join(lines)


# ---------- картинка ----------
def font(size, bold=False):
    names = ("DejaVuSans-Bold.ttf", "arialbd.ttf") if bold else ("DejaVuSans.ttf", "arial.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size * S)
        except OSError:
            pass
    return ImageFont.load_default()


def bolt(d, cx, cy, k=1.0):
    pts = [(2, -14), (-8, 2), (-1, 2), (-4, 14), (8, -3), (1, -3)]
    d.polygon([(cx + x * k * S, cy + y * k * S) for x, y in pts], fill="#444")
    d.line([(cx - 10 * k * S, cy - 12 * k * S), (cx + 10 * k * S, cy + 12 * k * S)],
           fill="#444", width=2 * S)


def render(day, label, slots, updated):
    img = Image.new("RGB", (390 * S, 790 * S), "white")
    d = ImageDraw.Draw(img)
    d.text((16 * S, 16 * S), "Графік відключень:", font=font(26, True), fill="#1b2a5c")
    d.rounded_rectangle((16 * S, 64 * S, 374 * S, 104 * S), 10 * S, fill="#FFE600")
    d.text((195 * S, 84 * S), f"на {label}  {day:%d.%m.%y}", anchor="mm",
           font=font(16, True), fill="black")
    if updated:
        d.text((16 * S, 116 * S), f"Останнє оновлення: {updated}", font=font(12), fill="#555")

    rh, tw, sw, top = 40 * S, 64 * S, 56 * S, 150 * S
    for col in range(2):
        x = 40 * S + col * (tw + sw + 50 * S)
        sx = x + tw
        d.text((x + tw // 2, top + rh // 2), "Час", anchor="mm", font=font(13), fill="black")
        for r in range(12):
            h = col * 12 + r
            y = top + rh * (r + 1)
            first, second = slots[2 * h], slots[2 * h + 1]
            if first:
                d.rectangle((sx, y, sx + sw, y + rh // 2), fill="#d3d3d3")
            if second:
                d.rectangle((sx, y + rh // 2, sx + sw, y + rh), fill="#d3d3d3")
            if first or second:
                bolt(d, sx + sw // 2, y + rh // 2, 1 if first and second else 0.7)
            d.line((x, y, x + tw + sw, y), fill="#bbb", width=S)
            d.text((x + tw // 2, y + rh // 2), f"{h:02d}-{h + 1:02d}",
                   anchor="mm", font=font(13), fill="black")
        d.rounded_rectangle((x, top, x + tw + sw, top + rh * 13), 12 * S, outline="#888", width=S)
        d.line((sx, top, sx, top + rh * 13), fill="#888", width=S)

    # легенда
    ly = 690 * S
    items = [("Світло є", 0), ("Світла немає", 2), ("Світла не буде 30 хв.", 1)]
    for i, (text, mode) in enumerate(items):
        y = ly + i * 32 * S
        box = (16 * S, y, 40 * S, y + 24 * S)
        d.rectangle(box, fill="white", outline="#888", width=S)
        if mode == 2:
            d.rectangle(box, fill="#d3d3d3", outline="#888", width=S)
        if mode == 1:
            d.rectangle((16 * S, y, 40 * S, y + 12 * S), fill="#d3d3d3")
            d.rectangle(box, outline="#888", width=S)
        if mode:
            bolt(d, 28 * S, y + 12 * S, 0.5)
        d.text((52 * S, y + 12 * S), text, anchor="lm", font=font(14), fill="black")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- Telegram ----------
def send_message(text):
    kb = {"inline_keyboard": [[{"text": "Подивитися графік", "url": URL}]]}
    r = requests.post(f"{API}/sendMessage", timeout=20,
                      data={"chat_id": CHAT_ID, "text": text, "reply_markup": json.dumps(kb)})
    if not r.ok:
        print("Ошибка sendMessage:", r.text)
    return r.ok


def send_photo(png):
    r = requests.post(f"{API}/sendPhoto", timeout=40,
                      data={"chat_id": CHAT_ID}, files={"photo": ("schedule.png", png)})
    if not r.ok:
        print("Ошибка sendPhoto:", r.text)
    return r.ok


def check_commands(state):
    """True, если вы писали боту /schedule (или /start) с прошлого запуска."""
    params = {"timeout": 0}
    if "offset" in state:
        params["offset"] = state["offset"]
    r = requests.get(f"{API}/getUpdates", params=params, timeout=30)
    if r.status_code == 409:  # включён webhook (кнопка «Оновити»): команды приходят через него
        return False
    if not r.ok:
        print("Ошибка getUpdates:", r.text)
        return False
    wanted = False
    for u in r.json().get("result", []):
        state["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(CHAT_ID):
            continue  # чужие сообщения игнорируем
        text = (m.get("text") or "").strip().lower()
        if text.startswith(("/schedule", "/start", "/grafik")):
            wanted = True
    return wanted


# ---------- состояние ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    today = str(datetime.now(TZ).date())
    for k in list(state):
        if k != "offset" and k < today:  # убираем старые даты
            del state[k]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# ---------- главная логика ----------
async def main():
    fact = await fetch_fact()
    if not fact:
        sys.exit(1)
    try:
        days = get_days(fact)
    except Exception as e:
        print("Неожиданная структура данных:", e)
        print("Ключи fact:", list(fact)[:20] if isinstance(fact, dict) else type(fact))
        sys.exit(1)

    save_outages(days)  # schedule.json: по нему Worker шлёт напоминания за 30 минут
    state = load_state()
    wanted = check_commands(state) or FORCE
    today = datetime.now(TZ).date()
    updated = fact.get("update", "")
    sent = set()
    failed = False

    for day, slots in days:
        key = str(day)
        h = hashlib.sha256(json.dumps(slots).encode()).hexdigest()
        if state.get(key) == h:
            continue
        kind = "new" if key not in state else "changed"
        label = "сьогодні" if day == today else "завтра"
        ok = send_message(build_text(day, label, slots, kind))
        if ok:
            send_photo(render(day, label, slots, updated))
            state[key] = h  # запоминаем, только если сообщение дошло
            sent.add(key)
        else:
            failed = True

    if wanted:
        for day, slots in days:
            if str(day) in sent:
                continue
            label = "сьогодні" if day == today else "завтра"
            send_message(build_text(day, label, slots, "request"))
            send_photo(render(day, label, slots, updated))
        if not days:
            overview = groups_overview(fact)
            send_message(f"Черги {GROUP.replace('GPV', '')} у даних сайту немає.\n\n"
                         "Коли немає світла сьогодні, за чергами:\n" + overview +
                         "\n\nЗнайдіть рядок, що збігається з графіком вашої адреси на сайті ДТЕК.")

    save_state(state)
    print("Готово. Дней в графике:", len(days), "| команда:", wanted)
    if failed:
        print("Не удалось отправить сообщение в Telegram: проверьте BOT_TOKEN и CHAT_ID.")
        sys.exit(1)


asyncio.run(main())
