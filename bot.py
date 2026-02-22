import os
import logging
import threading
import time as _time
from datetime import datetime, timedelta

import telebot
from telebot import types
from telebot.types import LabeledPrice

from database import (
    save_reg_state, get_reg_state, clear_reg_state,
    add_student, get_student, get_student_by_id, get_all_students,
    update_lessons_balance, toggle_student_status, update_student_timezone,
    repurchase_tariff,
    get_free_slots, book_slot, get_student_slots, get_slot_by_id,
    add_slot, delete_slot, cancel_booking, cancel_booking_by_student,
    get_all_bookings, get_bookings_by_date, mark_lesson_done,
    get_upcoming_unreminded, mark_reminded,
    create_payment, complete_payment,
    add_teacher, get_active_teachers, remove_teacher, get_teacher_by_id,
    get_statistics,
)

# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN env variable is not set")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "7415299809"))
STRIPE_PROVIDER_TOKEN = os.environ.get("STRIPE_PROVIDER_TOKEN", "")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

TARIFFS = {
    "🥉 Start — 8 lessons":     {"lessons": 8,  "price_eur": 80,  "price_cents": 8000},
    "🥈 Standard — 16 lessons":  {"lessons": 16, "price_eur": 140, "price_cents": 14000},
    "🥇 Premium — 24 lessons":   {"lessons": 24, "price_eur": 190, "price_cents": 19000},
}

TIMEZONES = {
    "🇫🇷 Paris (CET)":       "Europe/Paris",
    "🇬🇧 London (GMT)":      "Europe/London",
    "🇩🇪 Berlin (CET)":      "Europe/Berlin",
    "🇪🇸 Madrid (CET)":      "Europe/Madrid",
    "🇵🇱 Warsaw (CET)":      "Europe/Warsaw",
    "🇺🇦 Kyiv (EET)":        "Europe/Kyiv",
    "🇷🇺 Moscow (MSK)":      "Europe/Moscow",
    "🇹🇷 Istanbul (TRT)":    "Europe/Istanbul",
    "🇺🇸 New York (EST)":    "America/New_York",
}

CANCEL_TEXTS = {"❌ Cancel", "⬅️ Back"}


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def is_cancel(text: str) -> bool:
    return text in CANCEL_TEXTS


def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        log.exception("Failed to send message to %s", chat_id)
        return None


def main_menu(telegram_id: int):
    student = get_student(telegram_id)
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if student:
        mk.row(types.KeyboardButton("📅 Schedule"), types.KeyboardButton("📚 My Lessons"))
        mk.row(types.KeyboardButton("👤 My Account"))
        mk.row(types.KeyboardButton("🛒 Buy Lessons"))
    else:
        mk.add(types.KeyboardButton("📝 Sign Up"))
    return mk


def admin_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.row(types.KeyboardButton("➕ Add Slot"), types.KeyboardButton("➕ Bulk Slots"))
    mk.row(types.KeyboardButton("🗑 Delete Slot"), types.KeyboardButton("👥 Students"))
    mk.row(types.KeyboardButton("📅 All Bookings"), types.KeyboardButton("📅 Bookings by Date"))
    mk.row(types.KeyboardButton("👩‍🏫 Teachers"), types.KeyboardButton("📊 Statistics"))
    mk.row(types.KeyboardButton("🔙 Exit Admin"))
    return mk


def cancel_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.add(types.KeyboardButton("❌ Cancel"))
    return mk


def back_cancel_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.row(types.KeyboardButton("⬅️ Back"), types.KeyboardButton("❌ Cancel"))
    return mk


def _notify_admin_zero_balance(student):
    safe_send(ADMIN_ID,
              f"⚠️ Student ran out of lessons!\n\n"
              f"👤 {student[2]}\n📧 {student[3]}\n"
              f"📚 Plan: {student[4]}\nBalance: 0")


def _parse_slot_dt(date_str: str, time_str: str) -> datetime:
    return datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")


# ---------------------------------------------------------------------------
#  /start
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    clear_reg_state(message.chat.id)
    student = get_student(message.chat.id)
    if student:
        safe_send(message.chat.id,
                  f"Welcome back, {student[2]}! 👋",
                  reply_markup=main_menu(message.chat.id))
    else:
        safe_send(message.chat.id,
                  "Welcome to our English Language School! 🎓\n\n"
                  "Here you can sign up for a course, manage your lessons, "
                  "and receive reminders.",
                  reply_markup=main_menu(message.chat.id))


# ===================================================================
#        REGISTRATION FLOW
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "📝 Sign Up")
def reg_start(message):
    if get_student(message.chat.id):
        safe_send(message.chat.id, "You are already registered!",
                  reply_markup=main_menu(message.chat.id))
        return
    save_reg_state(message.chat.id, "name")
    msg = safe_send(message.chat.id, "Let's get started! What is your name?",
                    reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, reg_process_name)


def reg_process_name(message):
    if is_cancel(message.text):
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Registration cancelled.",
                  reply_markup=main_menu(message.chat.id))
        return
    save_reg_state(message.chat.id, "email", name=message.text.strip())
    msg = safe_send(message.chat.id, "Enter your email:", reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, reg_process_email)


def reg_process_email(message):
    if is_cancel(message.text):
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Registration cancelled.",
                  reply_markup=main_menu(message.chat.id))
        return
    if "@" not in message.text:
        msg = safe_send(message.chat.id, "Invalid email. Please try again:",
                        reply_markup=cancel_markup())
        if msg:
            bot.register_next_step_handler(msg, reg_process_email)
        return
    save_reg_state(message.chat.id, "timezone", email=message.text.strip())
    _show_timezone_menu(message)


def _show_timezone_menu(message):
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keys = list(TIMEZONES.keys())
    for i in range(0, len(keys), 2):
        row = [types.KeyboardButton(keys[i])]
        if i + 1 < len(keys):
            row.append(types.KeyboardButton(keys[i + 1]))
        mk.row(*row)
    mk.row(types.KeyboardButton("⬅️ Back"), types.KeyboardButton("❌ Cancel"))
    msg = safe_send(message.chat.id, "Select your timezone:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, reg_process_timezone)


_user_tz_cache: dict = {}


def reg_process_timezone(message):
    if message.text == "❌ Cancel":
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Registration cancelled.",
                  reply_markup=main_menu(message.chat.id))
        return
    if message.text == "⬅️ Back":
        save_reg_state(message.chat.id, "email")
        msg = safe_send(message.chat.id, "Enter your email:", reply_markup=cancel_markup())
        if msg:
            bot.register_next_step_handler(msg, reg_process_email)
        return
    if message.text not in TIMEZONES:
        msg = safe_send(message.chat.id, "Please select a timezone from the list.")
        if msg:
            bot.register_next_step_handler(msg, reg_process_timezone)
        return
    _user_tz_cache[message.chat.id] = TIMEZONES[message.text]
    save_reg_state(message.chat.id, "tariff")
    _show_tariff_menu(message)


def _show_tariff_menu(message):
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in TARIFFS:
        mk.add(types.KeyboardButton(t))
    mk.row(types.KeyboardButton("⬅️ Back"), types.KeyboardButton("❌ Cancel"))
    msg = safe_send(message.chat.id, "Choose a plan:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, reg_process_tariff)


def reg_process_tariff(message):
    if message.text == "❌ Cancel":
        clear_reg_state(message.chat.id)
        _user_tz_cache.pop(message.chat.id, None)
        safe_send(message.chat.id, "Registration cancelled.",
                  reply_markup=main_menu(message.chat.id))
        return
    if message.text == "⬅️ Back":
        _show_timezone_menu(message)
        return
    if message.text not in TARIFFS:
        msg = safe_send(message.chat.id, "Please choose a plan from the menu.")
        if msg:
            bot.register_next_step_handler(msg, reg_process_tariff)
        return
    save_reg_state(message.chat.id, "payment", tariff=message.text)
    _send_invoice(message.chat.id, message.text, is_repurchase=False)


# ---------------------------------------------------------------------------
#  Payment (Telegram Payments API / manual fallback)
# ---------------------------------------------------------------------------

def _send_invoice(chat_id: int, tariff_name: str, is_repurchase: bool = False):
    tariff = TARIFFS[tariff_name]

    if not STRIPE_PROVIDER_TOKEN:
        _fallback_manual_payment(chat_id, tariff_name, is_repurchase)
        return

    payment_id = create_payment(chat_id, tariff_name, tariff["price_cents"])
    prices = [LabeledPrice(label=tariff_name, amount=tariff["price_cents"])]

    try:
        bot.send_invoice(
            chat_id=chat_id,
            title=tariff_name,
            description=f"{tariff['lessons']} English lessons",
            invoice_payload=f"{payment_id}|{tariff_name}|{'repurchase' if is_repurchase else 'new'}",
            provider_token=STRIPE_PROVIDER_TOKEN,
            currency="EUR",
            prices=prices,
            start_parameter=f"pay_{payment_id}",
            is_flexible=False,
        )
    except Exception:
        log.exception("Failed to send invoice to %s", chat_id)
        safe_send(chat_id, "❌ Payment error. Please try again later.",
                  reply_markup=main_menu(chat_id))


def _fallback_manual_payment(chat_id: int, tariff_name: str, is_repurchase: bool):
    tariff = TARIFFS[tariff_name]
    state = get_reg_state(chat_id)

    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(
        "✅ Confirm Payment",
        callback_data=f"confirmpay_{chat_id}_{tariff_name}|{'repurchase' if is_repurchase else 'new'}"))

    if is_repurchase:
        student = get_student(chat_id)
        admin_text = (f"💳 Payment request (renewal)\n\n"
                      f"👤 {student[2]}\n📚 {tariff_name}\n💰 {tariff['price_eur']}€")
    else:
        admin_text = (f"💳 Payment request (new student)\n\n"
                      f"👤 {state['name'] if state else '?'}\n"
                      f"📧 {state['email'] if state else '?'}\n"
                      f"📚 {tariff_name}\n💰 {tariff['price_eur']}€")

    safe_send(ADMIN_ID, admin_text, reply_markup=mk)
    safe_send(chat_id,
              f"📩 Payment request sent!\n\n"
              f"📚 {tariff_name}\n💰 {tariff['price_eur']}€\n\n"
              f"Please wait for admin confirmation.",
              reply_markup=main_menu(chat_id))


@bot.pre_checkout_query_handler(func=lambda query: True)
def handle_pre_checkout(pre_checkout_query):
    try:
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception:
        log.exception("pre_checkout error")


@bot.message_handler(content_types=["successful_payment"])
def handle_successful_payment(message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    charge_id = payment.provider_payment_charge_id
    chat_id = message.chat.id

    try:
        parts = payload.split("|")
        payment_id = int(parts[0])
        tariff_name = parts[1]
        flow = parts[2] if len(parts) > 2 else "new"

        complete_payment(payment_id, charge_id)
        tariff = TARIFFS.get(tariff_name)
        if not tariff:
            safe_send(chat_id, "❌ Plan error. Please contact the administrator.")
            return

        if flow == "repurchase":
            repurchase_tariff(chat_id, tariff_name, tariff["lessons"])
            student = get_student(chat_id)
            safe_send(chat_id,
                      f"✅ Payment successful! Lessons added.\n\n"
                      f"📚 {tariff_name}\nBalance: {student[5]} lessons",
                      reply_markup=main_menu(chat_id))
            safe_send(ADMIN_ID,
                      f"💰 Renewal paid!\n👤 {student[2]}\n"
                      f"📚 {tariff_name}\n💳 {charge_id}")
        else:
            state = get_reg_state(chat_id)
            tz = _user_tz_cache.pop(chat_id, "Europe/Paris")
            name = state["name"] if state else "—"
            email = state["email"] if state else "—"
            add_student(chat_id, name, email, tariff_name, tariff["lessons"], tz)
            clear_reg_state(chat_id)

            safe_send(chat_id,
                      f"✅ Welcome, {name}!\n\n"
                      f"Plan: {tariff_name}\nLessons: {tariff['lessons']}\n\n"
                      f"Book your first lesson via 📅 Schedule!",
                      reply_markup=main_menu(chat_id))
            safe_send(ADMIN_ID,
                      f"🎉 New student (paid)!\n👤 {name}\n📧 {email}\n"
                      f"📚 {tariff_name}\n💳 {charge_id}")

    except Exception:
        log.exception("successful_payment error for %s", chat_id)
        safe_send(chat_id,
                  "Payment received but processing failed. Please contact admin.")


# ===================================================================
#        🛒 REPURCHASE
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "🛒 Buy Lessons")
def repurchase_start(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Please sign up first: 📝 Sign Up",
                  reply_markup=main_menu(message.chat.id))
        return
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in TARIFFS:
        mk.add(types.KeyboardButton(t))
    mk.add(types.KeyboardButton("❌ Cancel"))
    msg = safe_send(message.chat.id,
                    f"Balance: {student[5]} lessons\n\nChoose a plan to purchase:",
                    reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, repurchase_process_tariff)


def repurchase_process_tariff(message):
    if is_cancel(message.text):
        safe_send(message.chat.id, "Cancelled.", reply_markup=main_menu(message.chat.id))
        return
    if message.text not in TARIFFS:
        msg = safe_send(message.chat.id, "Please choose a plan from the menu.")
        if msg:
            bot.register_next_step_handler(msg, repurchase_process_tariff)
        return
    _send_invoice(message.chat.id, message.text, is_repurchase=True)


# ===================================================================
#        📅 SCHEDULE — book a slot
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "📅 Schedule")
def show_schedule(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Please sign up first.",
                  reply_markup=main_menu(message.chat.id))
        return
    if student[6] != "active":
        safe_send(message.chat.id, "Your account is blocked.",
                  reply_markup=main_menu(message.chat.id))
        return

    slots = get_free_slots()
    if not slots:
        safe_send(message.chat.id, "No available slots at the moment.",
                  reply_markup=main_menu(message.chat.id))
        return

    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for s in slots:
        mk.add(types.KeyboardButton(f"📅 {s[2]} {s[3]} — {s[1]}"))
    mk.add(types.KeyboardButton("❌ Cancel"))
    msg = safe_send(message.chat.id,
                    f"Balance: {student[5]} lessons\nSelect a slot:",
                    reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, process_slot_booking)


def process_slot_booking(message):
    if is_cancel(message.text):
        safe_send(message.chat.id, "Cancelled.", reply_markup=main_menu(message.chat.id))
        return

    slots = get_free_slots()
    selected = None
    for s in slots:
        if f"📅 {s[2]} {s[3]} — {s[1]}" == message.text:
            selected = s
            break
    if not selected:
        msg = safe_send(message.chat.id, "Please select a slot from the menu.")
        if msg:
            bot.register_next_step_handler(msg, process_slot_booking)
        return

    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Error.", reply_markup=main_menu(message.chat.id))
        return
    if student[5] <= 0:
        safe_send(message.chat.id,
                  "❌ No lessons left.\nTap 🛒 Buy Lessons to continue.",
                  reply_markup=main_menu(message.chat.id))
        _notify_admin_zero_balance(student)
        return

    ok = book_slot(selected[0], student[0])
    if not ok:
        safe_send(message.chat.id, "❌ Slot already taken or insufficient balance.",
                  reply_markup=main_menu(message.chat.id))
        return

    safe_send(message.chat.id,
              f"✅ Booked!\n\n"
              f"📅 {selected[2]}\n🕐 {selected[3]}\n"
              f"👩‍🏫 {selected[1]}\n🔗 {selected[4]}",
              reply_markup=main_menu(message.chat.id))

    student = get_student(message.chat.id)
    if student and student[5] == 0:
        _notify_admin_zero_balance(student)
        safe_send(message.chat.id,
                  "ℹ️ That was your last lesson.\n"
                  "Tap 🛒 Buy Lessons to keep learning!")


# ===================================================================
#        📚 MY LESSONS
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "📚 My Lessons")
def my_lessons(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Please sign up first.",
                  reply_markup=main_menu(message.chat.id))
        return
    slots = get_student_slots(student[0])

    text = f"📚 My Lessons\n\nBalance: {student[5]} lessons\n\n"
    if slots:
        text += "Upcoming:\n\n"
        for s in slots:
            text += f"📅 {s[2]} at {s[3]} — {s[1]}\n🔗 {s[4]}\n\n"

        mk = types.InlineKeyboardMarkup()
        for s in slots:
            try:
                lesson_dt = _parse_slot_dt(s[2], s[3])
                if lesson_dt - datetime.now() > timedelta(hours=24):
                    mk.add(types.InlineKeyboardButton(
                        f"❌ Cancel {s[2]} {s[3]}",
                        callback_data=f"stucancel_{s[0]}"))
            except ValueError:
                pass
        safe_send(message.chat.id, text, reply_markup=mk)
    else:
        text += "No bookings yet. Tap 📅 Schedule to book."
        safe_send(message.chat.id, text, reply_markup=main_menu(message.chat.id))


# ===================================================================
#        👤 MY ACCOUNT
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "👤 My Account")
def cabinet(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Please sign up first.",
                  reply_markup=main_menu(message.chat.id))
        return
    status = "✅ Active" if student[6] == "active" else "❌ Blocked"
    tz_label = student[7] or "Europe/Paris"

    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("🌍 Change Timezone", callback_data="changetz"))

    safe_send(message.chat.id,
              f"👤 <b>My Account</b>\n\n"
              f"Name: {student[2]}\n"
              f"Email: {student[3]}\n"
              f"Plan: {student[4]}\n"
              f"Balance: {student[5]} lessons\n"
              f"Timezone: {tz_label}\n"
              f"Status: {status}",
              reply_markup=mk)


# ===================================================================
#                         ADMIN PANEL
# ===================================================================

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.chat.id != ADMIN_ID:
        safe_send(message.chat.id, "Access denied.")
        return
    safe_send(message.chat.id, "Admin Panel:", reply_markup=admin_markup())


# ---- Add Slot (picks teacher from DB) ----

@bot.message_handler(func=lambda m: m.text == "➕ Add Slot")
def admin_add_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    teachers = get_active_teachers()
    if not teachers:
        safe_send(message.chat.id,
                  "No teachers found. Add a teacher first via 👩‍🏫 Teachers.",
                  reply_markup=admin_markup())
        return
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in teachers:
        mk.add(types.KeyboardButton(f"🎓 {t[1]} (#{t[0]})"))
    mk.add(types.KeyboardButton("❌ Cancel"))
    msg = safe_send(message.chat.id, "Select a teacher:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, _admin_slot_pick_teacher)


def _admin_slot_pick_teacher(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Cancelled.", reply_markup=admin_markup())
        return
    try:
        tid = int(message.text.split("#")[1].split(")")[0])
        teacher = get_teacher_by_id(tid)
        if not teacher:
            raise ValueError("not found")
    except Exception:
        safe_send(message.chat.id, "Invalid selection.", reply_markup=admin_markup())
        return
    _admin_slot_teacher_cache[message.chat.id] = teacher
    msg = safe_send(message.chat.id,
                    f"Teacher: {teacher[1]}\n\n"
                    f"Enter slot details:\nDD.MM.YYYY\nHH:MM\nZoom link\n\n"
                    f"Example:\n28.02.2026\n14:00\nhttps://zoom.us/j/123",
                    reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, _admin_process_add_slot)


_admin_slot_teacher_cache: dict = {}


def _admin_process_add_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        _admin_slot_teacher_cache.pop(message.chat.id, None)
        safe_send(message.chat.id, "Cancelled.", reply_markup=admin_markup())
        return
    teacher = _admin_slot_teacher_cache.pop(message.chat.id, None)
    if not teacher:
        safe_send(message.chat.id, "Session expired. Try again.", reply_markup=admin_markup())
        return
    try:
        lines = message.text.strip().split("\n")
        assert len(lines) >= 3
        date, time_str, zoom = lines[0].strip(), lines[1].strip(), lines[2].strip()
        datetime.strptime(date, "%d.%m.%Y")
        datetime.strptime(time_str, "%H:%M")
        zoom_link = zoom if zoom else teacher[2]  # fallback to teacher's default zoom
        sid = add_slot(teacher[1], date, time_str, zoom_link)
        safe_send(message.chat.id,
                  f"✅ Slot #{sid}\n👩‍🏫 {teacher[1]}\n📅 {date} {time_str}\n🔗 {zoom_link}",
                  reply_markup=admin_markup())
    except Exception as e:
        safe_send(message.chat.id, f"❌ Error: {e}", reply_markup=admin_markup())


# ---- Bulk Slots (picks teacher from DB) ----

@bot.message_handler(func=lambda m: m.text == "➕ Bulk Slots")
def admin_bulk_slots(message):
    if message.chat.id != ADMIN_ID:
        return
    teachers = get_active_teachers()
    if not teachers:
        safe_send(message.chat.id, "No teachers. Add one first.", reply_markup=admin_markup())
        return
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in teachers:
        mk.add(types.KeyboardButton(f"🎓 {t[1]} (#{t[0]})"))
    mk.add(types.KeyboardButton("❌ Cancel"))
    msg = safe_send(message.chat.id, "Select a teacher:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, _admin_bulk_pick_teacher)


def _admin_bulk_pick_teacher(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Cancelled.", reply_markup=admin_markup())
        return
    try:
        tid = int(message.text.split("#")[1].split(")")[0])
        teacher = get_teacher_by_id(tid)
        if not teacher:
            raise ValueError("not found")
    except Exception:
        safe_send(message.chat.id, "Invalid selection.", reply_markup=admin_markup())
        return
    _admin_slot_teacher_cache[message.chat.id] = teacher
    msg = safe_send(message.chat.id,
                    f"Teacher: {teacher[1]}\n\n"
                    f"Format:\nDD.MM.YYYY\nHH:MM, HH:MM, HH:MM\nZoom link\n\n"
                    f"Example:\n01.03.2026\n09:00, 10:00, 11:00\nhttps://zoom.us/j/123",
                    reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, _admin_process_bulk)


def _admin_process_bulk(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        _admin_slot_teacher_cache.pop(message.chat.id, None)
        safe_send(message.chat.id, "Cancelled.", reply_markup=admin_markup())
        return
    teacher = _admin_slot_teacher_cache.pop(message.chat.id, None)
    if not teacher:
        safe_send(message.chat.id, "Session expired.", reply_markup=admin_markup())
        return
    try:
        lines = message.text.strip().split("\n")
        assert len(lines) >= 3
        date = lines[0].strip()
        times = [t.strip() for t in lines[1].split(",")]
        zoom = lines[2].strip() if len(lines) > 2 else teacher[2]
        datetime.strptime(date, "%d.%m.%Y")
        added = []
        for t in times:
            datetime.strptime(t, "%H:%M")
            sid = add_slot(teacher[1], date, t, zoom)
            added.append(f"  #{sid} {t}")
        safe_send(message.chat.id,
                  f"✅ {len(added)} slots on {date} ({teacher[1]}):\n" + "\n".join(added),
                  reply_markup=admin_markup())
    except Exception as e:
        safe_send(message.chat.id, f"❌ Error: {e}", reply_markup=admin_markup())


# ---- Delete Slot ----

@bot.message_handler(func=lambda m: m.text == "🗑 Delete Slot")
def admin_delete_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    slots = get_free_slots()
    if not slots:
        safe_send(message.chat.id, "No free slots to delete.", reply_markup=admin_markup())
        return
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for s in slots:
        mk.add(types.KeyboardButton(f"DEL#{s[0]} {s[2]} {s[3]} {s[1]}"))
    mk.add(types.KeyboardButton("❌ Cancel"))
    msg = safe_send(message.chat.id, "Select a slot to delete:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, _admin_do_delete)


def _admin_do_delete(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Cancelled.", reply_markup=admin_markup())
        return
    try:
        slot_id = int(message.text.split("#")[1].split(" ")[0])
        ok = delete_slot(slot_id)
        txt = f"✅ Slot #{slot_id} deleted." if ok else "❌ Could not delete."
        safe_send(message.chat.id, txt, reply_markup=admin_markup())
    except Exception as e:
        safe_send(message.chat.id, f"❌ {e}", reply_markup=admin_markup())


# ---- Students ----

@bot.message_handler(func=lambda m: m.text == "👥 Students")
def admin_students(message):
    if message.chat.id != ADMIN_ID:
        return
    students = get_all_students()
    if not students:
        safe_send(message.chat.id, "No students yet.", reply_markup=admin_markup())
        return
    for s in students:
        mk = types.InlineKeyboardMarkup()
        mk.row(
            types.InlineKeyboardButton("➕ Lesson", callback_data=f"addlesson_{s[0]}"),
            types.InlineKeyboardButton("➖ Done", callback_data=f"rmlesson_{s[0]}"),
        )
        mk.add(types.InlineKeyboardButton(
            "🚫 Block" if s[6] == "active" else "✅ Unblock",
            callback_data=f"block_{s[0]}"))
        status = "✅" if s[6] == "active" else "❌"
        safe_send(message.chat.id,
                  f"👤 {s[2]} (id:{s[0]})\n📧 {s[3]}\n"
                  f"📚 {s[4]}  Balance: {s[5]}  {status}",
                  reply_markup=mk)


# ---- All Bookings ----

@bot.message_handler(func=lambda m: m.text == "📅 All Bookings")
def admin_all_bookings(message):
    if message.chat.id != ADMIN_ID:
        return
    bookings = get_all_bookings()
    if not bookings:
        safe_send(message.chat.id, "No bookings.", reply_markup=admin_markup())
        return
    text = "📅 All Bookings:\n\n"
    mk = types.InlineKeyboardMarkup()
    for b in bookings:
        text += f"[#{b[0]}] {b[1]} — {b[2]} | {b[3]} {b[4]}\n"
        mk.row(
            types.InlineKeyboardButton(f"❌ Cancel #{b[0]}", callback_data=f"cancelbook_{b[0]}"),
            types.InlineKeyboardButton(f"✅ Done #{b[0]}", callback_data=f"done_{b[0]}"),
        )
    safe_send(message.chat.id, text, reply_markup=mk)


# ---- Bookings by Date ----

@bot.message_handler(func=lambda m: m.text == "📅 Bookings by Date")
def admin_bookings_date(message):
    if message.chat.id != ADMIN_ID:
        return
    msg = safe_send(message.chat.id, "Enter date (DD.MM.YYYY):", reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, _admin_do_bookings_date)


def _admin_do_bookings_date(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Cancelled.", reply_markup=admin_markup())
        return
    date = message.text.strip()
    try:
        datetime.strptime(date, "%d.%m.%Y")
    except ValueError:
        safe_send(message.chat.id, "❌ Use DD.MM.YYYY format.", reply_markup=admin_markup())
        return
    bookings = get_bookings_by_date(date)
    if not bookings:
        safe_send(message.chat.id, f"No bookings on {date}.", reply_markup=admin_markup())
        return
    text = f"📅 {date}:\n\n"
    mk = types.InlineKeyboardMarkup()
    for b in bookings:
        text += f"[#{b[0]}] {b[1]} — {b[2]} at {b[4]}\n"
        mk.row(
            types.InlineKeyboardButton(f"❌ #{b[0]}", callback_data=f"cancelbook_{b[0]}"),
            types.InlineKeyboardButton(f"✅ #{b[0]}", callback_data=f"done_{b[0]}"),
        )
    safe_send(message.chat.id, text, reply_markup=mk)


# ---- Teachers Management ----

@bot.message_handler(func=lambda m: m.text == "👩‍🏫 Teachers")
def admin_teachers(message):
    if message.chat.id != ADMIN_ID:
        return
    teachers = get_active_teachers()
    text = "👩‍🏫 <b>Teachers</b>\n\n"
    if teachers:
        for t in teachers:
            text += f"#{t[0]} — {t[1]}  🔗 {t[2] or '—'}\n"
    else:
        text += "No teachers yet.\n"

    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("➕ Add Teacher", callback_data="addteacher"))
    if teachers:
        for t in teachers:
            mk.add(types.InlineKeyboardButton(
                f"🗑 Remove {t[1]}", callback_data=f"rmteacher_{t[0]}"))
    safe_send(message.chat.id, text, reply_markup=mk)


# ---- Statistics ----

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def admin_statistics(message):
    if message.chat.id != ADMIN_ID:
        return
    s = get_statistics()
    safe_send(message.chat.id,
              f"📊 <b>Statistics</b>\n\n"
              f"👥 Total students: {s['total_students']}\n"
              f"✅ Active students: {s['active_students']}\n"
              f"💰 Total revenue: {s['total_revenue_eur']:.2f} €\n"
              f"💰 This month: {s['month_revenue_eur']:.2f} €\n"
              f"📚 Lessons conducted: {s['total_lessons_done']}\n"
              f"💳 Paid students: {s['paid_students']}\n"
              f"📈 Conversion: {s['conversion']}%",
              reply_markup=admin_markup())


# ---- Exit Admin ----

@bot.message_handler(func=lambda m: m.text == "🔙 Exit Admin")
def admin_exit(message):
    if message.chat.id != ADMIN_ID:
        return
    safe_send(message.chat.id, "Exited admin panel.",
              reply_markup=main_menu(message.chat.id))


# ===================================================================
#        INLINE CALLBACKS
# ===================================================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    data = call.data
    chat_id = call.message.chat.id

    try:
        # -- Student: cancel own lesson --
        if data.startswith("stucancel_"):
            slot_id = int(data.split("_")[1])
            student = get_student(call.from_user.id)
            if not student:
                bot.answer_callback_query(call.id, "Error")
                return
            slot = get_slot_by_id(slot_id)
            if slot:
                try:
                    lesson_dt = _parse_slot_dt(slot[2], slot[3])
                    if lesson_dt - datetime.now() < timedelta(hours=24):
                        bot.answer_callback_query(
                            call.id,
                            "❌ Cancellation is only allowed 24+ hours before the lesson",
                            show_alert=True)
                        return
                except ValueError:
                    pass
            ok = cancel_booking_by_student(slot_id, student[0])
            if ok:
                bot.answer_callback_query(call.id, "✅ Lesson cancelled, balance restored")
                safe_send(call.from_user.id, "✅ Lesson cancelled. Credit returned.",
                          reply_markup=main_menu(call.from_user.id))
                safe_send(ADMIN_ID, f"ℹ️ {student[2]} cancelled lesson (slot #{slot_id})")
            else:
                bot.answer_callback_query(call.id, "❌ Could not cancel")
            return

        # -- Student: change timezone --
        if data == "changetz":
            mk = types.InlineKeyboardMarkup()
            for label, tz_val in TIMEZONES.items():
                mk.add(types.InlineKeyboardButton(label, callback_data=f"setzt_{tz_val}"))
            safe_send(call.from_user.id, "Select your timezone:", reply_markup=mk)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("setzt_"):
            tz = data[6:]
            update_student_timezone(call.from_user.id, tz)
            bot.answer_callback_query(call.id, f"✅ Timezone: {tz}")
            safe_send(call.from_user.id, f"✅ Timezone changed to {tz}",
                      reply_markup=main_menu(call.from_user.id))
            return

        # -- Admin: add teacher (via next_step) --
        if data == "addteacher":
            if chat_id != ADMIN_ID:
                return
            msg = safe_send(chat_id,
                            "Enter teacher info:\nName\nZoom link (optional)\n\n"
                            "Example:\nAnna\nhttps://zoom.us/j/123",
                            reply_markup=cancel_markup())
            bot.answer_callback_query(call.id)
            if msg:
                bot.register_next_step_handler(msg, _admin_process_add_teacher)
            return

        if data.startswith("rmteacher_"):
            if chat_id != ADMIN_ID:
                return
            tid = int(data.split("_")[1])
            ok = remove_teacher(tid)
            if ok:
                bot.answer_callback_query(call.id, "✅ Teacher removed")
                safe_send(chat_id, f"✅ Teacher #{tid} removed.")
            else:
                bot.answer_callback_query(call.id, "❌ Error")
            return

        # -- Admin-only callbacks --
        if chat_id != ADMIN_ID:
            return

        if data.startswith("addlesson_"):
            sid = int(data.split("_")[1])
            update_lessons_balance(sid, +1)
            bot.answer_callback_query(call.id, "✅ Lesson added")
            safe_send(chat_id, f"✅ +1 lesson for student #{sid}")

        elif data.startswith("rmlesson_"):
            sid = int(data.split("_")[1])
            ok = update_lessons_balance(sid, -1)
            if ok:
                bot.answer_callback_query(call.id, "➖ Lesson deducted")
                safe_send(chat_id, f"➖ Lesson deducted from #{sid}")
                st = get_student_by_id(sid)
                if st and st[5] == 0:
                    safe_send(chat_id, f"⚠️ Student {st[2]} balance is now 0!")
            else:
                bot.answer_callback_query(call.id, "❌ Balance already 0")

        elif data.startswith("block_"):
            sid = int(data.split("_")[1])
            new = toggle_student_status(sid)
            label = "🚫 Blocked" if new == "blocked" else "✅ Unblocked"
            bot.answer_callback_query(call.id, label)
            safe_send(chat_id, f"#{sid}: {label}")

        elif data.startswith("cancelbook_"):
            slot_id = int(data.split("_")[1])
            ok = cancel_booking(slot_id)
            if ok:
                bot.answer_callback_query(call.id, "✅ Cancelled")
                safe_send(chat_id, f"✅ Booking #{slot_id} cancelled, lesson returned.")
            else:
                bot.answer_callback_query(call.id, "❌ Error")

        elif data.startswith("done_"):
            slot_id = int(data.split("_")[1])
            ok = mark_lesson_done(slot_id)
            if ok:
                bot.answer_callback_query(call.id, "✅ Done")
                safe_send(chat_id, f"✅ Lesson #{slot_id} marked as done.")
            else:
                bot.answer_callback_query(call.id, "❌ Error")

        elif data.startswith("confirmpay_"):
            rest = data[len("confirmpay_"):]
            parts = rest.split("|")
            target_chat = int(parts[0].rsplit("_", 1)[0])
            flow = parts[1] if len(parts) > 1 else "new"

            tariff_name = None
            for t in TARIFFS:
                if t in call.message.text:
                    tariff_name = t
                    break
            if not tariff_name:
                bot.answer_callback_query(call.id, "❌ Plan not found")
                return

            tariff = TARIFFS[tariff_name]
            if flow == "repurchase":
                repurchase_tariff(target_chat, tariff_name, tariff["lessons"])
                student = get_student(target_chat)
                safe_send(target_chat,
                          f"✅ Payment confirmed!\n📚 {tariff_name}\n"
                          f"Balance: {student[5]} lessons",
                          reply_markup=main_menu(target_chat))
            else:
                state = get_reg_state(target_chat)
                tz = _user_tz_cache.pop(target_chat, "Europe/Paris")
                name = state["name"] if state else "Student"
                email = state["email"] if state else "—"
                add_student(target_chat, name, email, tariff_name, tariff["lessons"], tz)
                clear_reg_state(target_chat)
                safe_send(target_chat,
                          f"✅ Payment confirmed, {name}!\n"
                          f"Plan: {tariff_name}\nLessons: {tariff['lessons']}",
                          reply_markup=main_menu(target_chat))

            bot.answer_callback_query(call.id, "✅ Confirmed")
            safe_send(chat_id, "✅ Payment confirmed.")

    except Exception:
        log.exception("Callback error: %s", data)
        try:
            bot.answer_callback_query(call.id, "Error")
        except Exception:
            pass


# ---- Admin: process add teacher (next_step) ----

def _admin_process_add_teacher(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Cancelled.", reply_markup=admin_markup())
        return
    lines = message.text.strip().split("\n")
    name = lines[0].strip()
    zoom = lines[1].strip() if len(lines) > 1 else ""
    tid = add_teacher(name, zoom)
    safe_send(message.chat.id,
              f"✅ Teacher #{tid} added: {name}" + (f"\n🔗 {zoom}" if zoom else ""),
              reply_markup=admin_markup())


# ===================================================================
#        CATCH-ALL
# ===================================================================

@bot.message_handler(func=lambda m: True)
def echo(message):
    safe_send(message.chat.id, "Tap a button in the menu 😊",
              reply_markup=main_menu(message.chat.id))


# ===================================================================
#        REMINDERS
# ===================================================================

def _reminder_loop():
    while True:
        try:
            now = datetime.now()
            for flag, hours, label in [
                ("reminded_24h", 24, "Tomorrow"),
                ("reminded_2h", 2, "In ~2 hours"),
            ]:
                for row in get_upcoming_unreminded(flag):
                    slot_id, teacher, date, time_str, zoom, tg_id, name, tz = row
                    try:
                        lesson_dt = _parse_slot_dt(date, time_str)
                    except ValueError:
                        continue
                    diff = lesson_dt - now
                    if timedelta(0) < diff <= timedelta(hours=hours):
                        safe_send(tg_id,
                                  f"⏰ Reminder! {label} you have a lesson:\n\n"
                                  f"📅 {date} at {time_str}\n"
                                  f"👩‍🏫 {teacher}\n🔗 {zoom}")
                        mark_reminded(slot_id, flag)
        except Exception:
            log.exception("Reminder loop error")
        _time.sleep(300)


# ===================================================================
#        ENTRY POINT
# ===================================================================

def main():
    log.info("Starting reminder thread…")
    threading.Thread(target=_reminder_loop, daemon=True).start()
    log.info("Bot started. PROVIDER_TOKEN=%s",
             "SET" if STRIPE_PROVIDER_TOKEN else "NOT SET (manual mode)")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


if __name__ == "__main__":
    main()