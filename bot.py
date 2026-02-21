import os
import logging
import threading
import time as _time
from datetime import datetime, timedelta

import telebot
from telebot import types
from telebot.types import LabeledPrice

from database import (
    # registration state
    save_reg_state, get_reg_state, clear_reg_state,
    # students
    add_student, get_student, get_student_by_id, get_all_students,
    update_lessons_balance, toggle_student_status, update_student_timezone,
    repurchase_tariff,
    # slots / bookings
    get_free_slots, book_slot, get_student_slots,
    add_slot, delete_slot, cancel_booking, cancel_booking_by_student,
    get_all_bookings, get_bookings_by_date, mark_lesson_done,
    # reminders
    get_upcoming_unreminded, mark_reminded,
    # payments
    create_payment, complete_payment,
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

# Stripe provider token from @BotFather → Bot Settings → Payments → Stripe
# Use TEST token for development, LIVE token for production.
STRIPE_PROVIDER_TOKEN = os.environ.get("STRIPE_PROVIDER_TOKEN", "")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

TARIFFS = {
    "🥉 Старт — 8 уроков":    {"lessons": 8,  "price_eur": 80,  "price_cents": 8000},
    "🥈 Стандарт — 16 уроков": {"lessons": 16, "price_eur": 140, "price_cents": 14000},
    "🥇 Премиум — 24 урока":  {"lessons": 24, "price_eur": 190, "price_cents": 19000},
}

TIMEZONES = {
    "🇫🇷 Париж (CET)":       "Europe/Paris",
    "🇬🇧 Лондон (GMT)":      "Europe/London",
    "🇩🇪 Берлин (CET)":      "Europe/Berlin",
    "🇪🇸 Мадрид (CET)":      "Europe/Madrid",
    "🇵🇱 Варшава (CET)":     "Europe/Warsaw",
    "🇺🇦 Киев (EET)":        "Europe/Kyiv",
    "🇷🇺 Москва (MSK)":      "Europe/Moscow",
    "🇹🇷 Стамбул (TRT)":     "Europe/Istanbul",
    "🇺🇸 Нью-Йорк (EST)":   "America/New_York",
}

CANCEL_TEXTS = {"❌ Отмена", "⬅️ Назад"}


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
        mk.row(types.KeyboardButton("📅 Расписание"), types.KeyboardButton("📚 Мои уроки"))
        mk.row(types.KeyboardButton("👤 Личный кабинет"))
        mk.row(types.KeyboardButton("🛒 Купить уроки"))
    else:
        mk.add(types.KeyboardButton("📝 Записаться"))
    return mk


def admin_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.row(types.KeyboardButton("➕ Добавить слот"), types.KeyboardButton("➕ Слоты на дату"))
    mk.row(types.KeyboardButton("🗑 Удалить слот"), types.KeyboardButton("👥 Ученики"))
    mk.row(types.KeyboardButton("📅 Все записи"), types.KeyboardButton("📅 Записи на дату"))
    mk.row(types.KeyboardButton("🔙 Выход"))
    return mk


def cancel_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.add(types.KeyboardButton("❌ Отмена"))
    return mk


def back_cancel_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.row(types.KeyboardButton("⬅️ Назад"), types.KeyboardButton("❌ Отмена"))
    return mk


def _notify_admin_zero_balance(student):
    """Alert admin when a student's balance hits 0."""
    safe_send(ADMIN_ID,
              f"⚠️ У ученика закончились уроки!\n\n"
              f"👤 {student[2]}\n📧 {student[3]}\n"
              f"📚 Тариф: {student[4]}\nБаланс: 0")


def _parse_slot_dt(date_str: str, time_str: str) -> datetime:
    return datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")


def _tz_offset(tz_name: str) -> timedelta:
    """Simple UTC offset for common timezones (no pytz dependency)."""
    offsets = {
        "Europe/London": 0, "Europe/Paris": 1, "Europe/Berlin": 1,
        "Europe/Madrid": 1, "Europe/Warsaw": 1, "Europe/Kyiv": 2,
        "Europe/Moscow": 3, "Europe/Istanbul": 3, "America/New_York": -5,
    }
    hours = offsets.get(tz_name, 1)  # default CET
    return timedelta(hours=hours)


# ---------------------------------------------------------------------------
#  /start
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    clear_reg_state(message.chat.id)
    student = get_student(message.chat.id)
    if student:
        safe_send(message.chat.id,
                  f"С возвращением, {student[2]}! 👋",
                  reply_markup=main_menu(message.chat.id))
    else:
        safe_send(message.chat.id,
                  "Добро пожаловать в школу английского языка! 🎓\n\n"
                  "Здесь вы можете записаться на курс, управлять уроками "
                  "и получать напоминания.",
                  reply_markup=main_menu(message.chat.id))


# ===================================================================
#        REGISTRATION FLOW  (state in DB → survives restart)
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "📝 Записаться")
def reg_start(message):
    if get_student(message.chat.id):
        safe_send(message.chat.id, "Вы уже зарегистрированы!",
                  reply_markup=main_menu(message.chat.id))
        return
    save_reg_state(message.chat.id, "name")
    msg = safe_send(message.chat.id, "Давайте начнём! Как вас зовут?",
                    reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, reg_process_name)


def reg_process_name(message):
    if is_cancel(message.text):
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Регистрация отменена.",
                  reply_markup=main_menu(message.chat.id))
        return
    save_reg_state(message.chat.id, "email", name=message.text.strip())
    msg = safe_send(message.chat.id, "Введите ваш email:", reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, reg_process_email)


def reg_process_email(message):
    if is_cancel(message.text):
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Регистрация отменена.",
                  reply_markup=main_menu(message.chat.id))
        return
    if "@" not in message.text:
        msg = safe_send(message.chat.id, "Некорректный email. Попробуйте снова:",
                        reply_markup=cancel_markup())
        if msg:
            bot.register_next_step_handler(msg, reg_process_email)
        return
    save_reg_state(message.chat.id, "timezone", email=message.text.strip())
    _show_timezone_menu(message)


# -- Timezone selection --

def _show_timezone_menu(message):
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keys = list(TIMEZONES.keys())
    for i in range(0, len(keys), 2):
        row = [types.KeyboardButton(keys[i])]
        if i + 1 < len(keys):
            row.append(types.KeyboardButton(keys[i + 1]))
        mk.row(*row)
    mk.row(types.KeyboardButton("⬅️ Назад"), types.KeyboardButton("❌ Отмена"))
    msg = safe_send(message.chat.id, "Выберите ваш часовой пояс:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, reg_process_timezone)


def reg_process_timezone(message):
    if message.text == "❌ Отмена":
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Регистрация отменена.",
                  reply_markup=main_menu(message.chat.id))
        return
    if message.text == "⬅️ Назад":
        save_reg_state(message.chat.id, "email")
        msg = safe_send(message.chat.id, "Введите ваш email:", reply_markup=cancel_markup())
        if msg:
            bot.register_next_step_handler(msg, reg_process_email)
        return
    if message.text not in TIMEZONES:
        msg = safe_send(message.chat.id, "Выберите часовой пояс из списка.")
        if msg:
            bot.register_next_step_handler(msg, reg_process_timezone)
        return
    # store tz in reg_state name field trick — we'll extract it in payment
    # Actually, let's store it separately via a small dict
    _user_tz_cache[message.chat.id] = TIMEZONES[message.text]
    save_reg_state(message.chat.id, "tariff")
    _show_tariff_menu(message)


# temp cache for timezone during registration (only needed between steps)
_user_tz_cache: dict = {}


# -- Tariff selection --

def _show_tariff_menu(message):
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in TARIFFS:
        mk.add(types.KeyboardButton(t))
    mk.row(types.KeyboardButton("⬅️ Назад"), types.KeyboardButton("❌ Отмена"))
    msg = safe_send(message.chat.id, "Выберите тариф:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, reg_process_tariff)


def reg_process_tariff(message):
    if message.text == "❌ Отмена":
        clear_reg_state(message.chat.id)
        _user_tz_cache.pop(message.chat.id, None)
        safe_send(message.chat.id, "Регистрация отменена.",
                  reply_markup=main_menu(message.chat.id))
        return
    if message.text == "⬅️ Назад":
        _show_timezone_menu(message)
        return
    if message.text not in TARIFFS:
        msg = safe_send(message.chat.id, "Выберите тариф из меню.")
        if msg:
            bot.register_next_step_handler(msg, reg_process_tariff)
        return
    save_reg_state(message.chat.id, "payment", tariff=message.text)
    _send_stripe_invoice(message.chat.id, message.text, is_repurchase=False)


# -- Stripe Payment via Telegram Payments API --

def _send_stripe_invoice(chat_id: int, tariff_name: str, is_repurchase: bool = False):
    """Send a Telegram Payments invoice with Stripe provider."""
    tariff = TARIFFS[tariff_name]
    state = get_reg_state(chat_id) if not is_repurchase else None

    if not STRIPE_PROVIDER_TOKEN:
        # Fallback: manual confirmation flow if no Stripe token configured
        _fallback_manual_payment(chat_id, tariff_name, is_repurchase)
        return

    payment_id = create_payment(chat_id, tariff_name, tariff["price_cents"])

    prices = [LabeledPrice(label=tariff_name, amount=tariff["price_cents"])]

    try:
        bot.send_invoice(
            chat_id=chat_id,
            title=tariff_name,
            description=f"{tariff['lessons']} уроков английского языка",
            invoice_payload=f"{payment_id}|{tariff_name}|{'repurchase' if is_repurchase else 'new'}",
            provider_token=STRIPE_PROVIDER_TOKEN,
            currency="EUR",
            prices=prices,
            start_parameter=f"pay_{payment_id}",
            is_flexible=False,
        )
    except Exception:
        log.exception("Failed to send invoice to %s", chat_id)
        safe_send(chat_id, "❌ Ошибка при создании платежа. Попробуйте позже.",
                  reply_markup=main_menu(chat_id))


def _fallback_manual_payment(chat_id: int, tariff_name: str, is_repurchase: bool):
    """If Stripe token is not set — admin-confirm flow."""
    tariff = TARIFFS[tariff_name]
    state = get_reg_state(chat_id)

    # Notify admin for manual confirmation
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(
        "✅ Подтвердить оплату",
        callback_data=f"confirmpay_{chat_id}_{tariff_name}|{'repurchase' if is_repurchase else 'new'}"))

    if is_repurchase:
        student = get_student(chat_id)
        admin_text = (f"💳 Запрос на оплату (продление)\n\n"
                      f"👤 {student[2]}\n📚 {tariff_name}\n💰 {tariff['price_eur']}€")
    else:
        admin_text = (f"💳 Запрос на оплату (новый)\n\n"
                      f"👤 {state['name'] if state else '?'}\n"
                      f"📧 {state['email'] if state else '?'}\n"
                      f"📚 {tariff_name}\n💰 {tariff['price_eur']}€")

    safe_send(ADMIN_ID, admin_text, reply_markup=mk)
    safe_send(chat_id,
              f"📩 Заявка на оплату отправлена!\n\n"
              f"📚 {tariff_name}\n💰 {tariff['price_eur']}€\n\n"
              f"Ожидайте подтверждения от администратора.",
              reply_markup=main_menu(chat_id))


# -- Telegram Payments handlers --

@bot.pre_checkout_query_handler(func=lambda query: True)
def handle_pre_checkout(pre_checkout_query):
    """Telegram calls this before charging. We always approve."""
    try:
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception:
        log.exception("pre_checkout error")


@bot.message_handler(content_types=["successful_payment"])
def handle_successful_payment(message):
    """Called after Stripe payment succeeds."""
    payment = message.successful_payment
    payload = payment.invoice_payload  # "payment_id|tariff_name|new_or_repurchase"
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
            safe_send(chat_id, "❌ Ошибка тарифа. Обратитесь к администратору.")
            return

        if flow == "repurchase":
            repurchase_tariff(chat_id, tariff_name, tariff["lessons"])
            student = get_student(chat_id)
            safe_send(chat_id,
                      f"✅ Оплата прошла! Уроки зачислены.\n\n"
                      f"📚 {tariff_name}\n"
                      f"Баланс: {student[5]} уроков",
                      reply_markup=main_menu(chat_id))
            safe_send(ADMIN_ID,
                      f"💰 Продление оплачено!\n👤 {student[2]}\n"
                      f"📚 {tariff_name}\n💳 {charge_id}")
        else:
            # new registration
            state = get_reg_state(chat_id)
            tz = _user_tz_cache.pop(chat_id, "Europe/Paris")
            name = state["name"] if state else "—"
            email = state["email"] if state else "—"
            add_student(chat_id, name, email, tariff_name, tariff["lessons"], tz)
            clear_reg_state(chat_id)

            safe_send(chat_id,
                      f"✅ Добро пожаловать, {name}!\n\n"
                      f"Тариф: {tariff_name}\n"
                      f"Уроков: {tariff['lessons']}\n\n"
                      f"Записывайтесь на урок через 📅 Расписание!",
                      reply_markup=main_menu(chat_id))
            safe_send(ADMIN_ID,
                      f"🎉 Новый ученик (оплата Stripe)!\n"
                      f"👤 {name}\n📧 {email}\n"
                      f"📚 {tariff_name}\n💳 {charge_id}")

    except Exception:
        log.exception("successful_payment processing error for %s", chat_id)
        safe_send(chat_id,
                  "Оплата прошла, но произошла ошибка при обработке. "
                  "Обратитесь к администратору.")


# ===================================================================
#        🛒 REPURCHASE (buy more lessons)
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "🛒 Купить уроки")
def repurchase_start(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Сначала зарегистрируйтесь: 📝 Записаться",
                  reply_markup=main_menu(message.chat.id))
        return
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in TARIFFS:
        mk.add(types.KeyboardButton(t))
    mk.add(types.KeyboardButton("❌ Отмена"))
    msg = safe_send(message.chat.id,
                    f"Баланс: {student[5]} уроков\n\nВыберите тариф для покупки:",
                    reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, repurchase_process_tariff)


def repurchase_process_tariff(message):
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=main_menu(message.chat.id))
        return
    if message.text not in TARIFFS:
        msg = safe_send(message.chat.id, "Выберите тариф из меню.")
        if msg:
            bot.register_next_step_handler(msg, repurchase_process_tariff)
        return
    _send_stripe_invoice(message.chat.id, message.text, is_repurchase=True)


# ===================================================================
#        📅 SCHEDULE — book a slot
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "📅 Расписание")
def show_schedule(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Сначала зарегистрируйтесь: 📝 Записаться",
                  reply_markup=main_menu(message.chat.id))
        return
    if student[6] != "active":
        safe_send(message.chat.id, "Ваш аккаунт заблокирован.",
                  reply_markup=main_menu(message.chat.id))
        return

    slots = get_free_slots()
    if not slots:
        safe_send(message.chat.id, "Свободных слотов пока нет.",
                  reply_markup=main_menu(message.chat.id))
        return

    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for s in slots:
        mk.add(types.KeyboardButton(f"📅 {s[2]} {s[3]} — {s[1]}"))
    mk.add(types.KeyboardButton("❌ Отмена"))
    msg = safe_send(message.chat.id,
                    f"Баланс: {student[5]} уроков\nВыберите слот:",
                    reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, process_slot_booking)


def process_slot_booking(message):
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=main_menu(message.chat.id))
        return

    slots = get_free_slots()
    selected = None
    for s in slots:
        if f"📅 {s[2]} {s[3]} — {s[1]}" == message.text:
            selected = s
            break
    if not selected:
        msg = safe_send(message.chat.id, "Выберите слот из меню.")
        if msg:
            bot.register_next_step_handler(msg, process_slot_booking)
        return

    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Ошибка.", reply_markup=main_menu(message.chat.id))
        return
    if student[5] <= 0:
        safe_send(message.chat.id,
                  "❌ У вас нет уроков на балансе.\nНажмите 🛒 Купить уроки.",
                  reply_markup=main_menu(message.chat.id))
        _notify_admin_zero_balance(student)
        return

    ok = book_slot(selected[0], student[0])
    if not ok:
        safe_send(message.chat.id, "❌ Слот уже занят или нет баланса.",
                  reply_markup=main_menu(message.chat.id))
        return

    safe_send(message.chat.id,
              f"✅ Вы записаны!\n\n"
              f"📅 {selected[2]}\n🕐 {selected[3]}\n"
              f"👩‍🏫 {selected[1]}\n🔗 {selected[4]}",
              reply_markup=main_menu(message.chat.id))

    # check if balance is now 0
    student = get_student(message.chat.id)
    if student and student[5] == 0:
        _notify_admin_zero_balance(student)
        safe_send(message.chat.id,
                  "ℹ️ Это был ваш последний урок на балансе.\n"
                  "Нажмите 🛒 Купить уроки, чтобы продолжить занятия.")


# ===================================================================
#        📚 MY LESSONS  (balance + upcoming + cancel)
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "📚 Мои уроки")
def my_lessons(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Сначала зарегистрируйтесь.",
                  reply_markup=main_menu(message.chat.id))
        return
    slots = get_student_slots(student[0])

    text = f"📚 Мои уроки\n\nБаланс: {student[5]} уроков\n\n"
    if slots:
        text += "Предстоящие:\n\n"
        for s in slots:
            text += f"📅 {s[2]} в {s[3]} — {s[1]}\n🔗 {s[4]}\n\n"

        # inline cancel buttons
        mk = types.InlineKeyboardMarkup()
        for s in slots:
            try:
                lesson_dt = _parse_slot_dt(s[2], s[3])
                diff = lesson_dt - datetime.now()
                if diff > timedelta(hours=24):
                    mk.add(types.InlineKeyboardButton(
                        f"❌ Отменить {s[2]} {s[3]}",
                        callback_data=f"stucancel_{s[0]}"))
            except ValueError:
                pass
        safe_send(message.chat.id, text, reply_markup=mk)
    else:
        text += "Записей нет. Нажмите 📅 Расписание."
        safe_send(message.chat.id, text, reply_markup=main_menu(message.chat.id))


# ===================================================================
#        👤 PERSONAL CABINET + timezone change
# ===================================================================

@bot.message_handler(func=lambda m: m.text == "👤 Личный кабинет")
def cabinet(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Сначала зарегистрируйтесь.",
                  reply_markup=main_menu(message.chat.id))
        return
    status = "✅ Активен" if student[6] == "active" else "❌ Заблокирован"
    tz_label = student[7] if student[7] else "Europe/Paris"

    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("🌍 Сменить часовой пояс", callback_data="changetz"))

    safe_send(message.chat.id,
              f"👤 <b>Личный кабинет</b>\n\n"
              f"Имя: {student[2]}\n"
              f"Email: {student[3]}\n"
              f"Тариф: {student[4]}\n"
              f"Баланс: {student[5]} уроков\n"
              f"Часовой пояс: {tz_label}\n"
              f"Статус: {status}",
              reply_markup=mk)


# ===================================================================
#                         ADMIN PANEL
# ===================================================================

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.chat.id != ADMIN_ID:
        safe_send(message.chat.id, "У вас нет доступа.")
        return
    safe_send(message.chat.id, "Админ-панель:", reply_markup=admin_markup())


@bot.message_handler(func=lambda m: m.text == "➕ Добавить слот")
def admin_add_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    msg = safe_send(message.chat.id,
                    "Формат:\nИмя преподавателя\nДД.ММ.ГГГГ\nЧЧ:ММ\nZoom-ссылка\n\n"
                    "Пример:\nАнна\n28.02.2026\n14:00\nhttps://zoom.us/j/123",
                    reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, _admin_process_add_slot)


def _admin_process_add_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=admin_markup())
        return
    try:
        lines = message.text.strip().split("\n")
        assert len(lines) >= 4
        teacher, date, time_str, zoom = lines[0].strip(), lines[1].strip(), lines[2].strip(), lines[3].strip()
        datetime.strptime(date, "%d.%m.%Y")
        datetime.strptime(time_str, "%H:%M")
        sid = add_slot(teacher, date, time_str, zoom)
        safe_send(message.chat.id,
                  f"✅ Слот #{sid}\n👩‍🏫 {teacher}\n📅 {date} {time_str}\n🔗 {zoom}",
                  reply_markup=admin_markup())
    except Exception as e:
        safe_send(message.chat.id, f"❌ Ошибка: {e}", reply_markup=admin_markup())


@bot.message_handler(func=lambda m: m.text == "➕ Слоты на дату")
def admin_bulk_slots(message):
    if message.chat.id != ADMIN_ID:
        return
    msg = safe_send(message.chat.id,
                    "Формат:\nИмя\nДД.ММ.ГГГГ\nЧЧ:ММ, ЧЧ:ММ, ЧЧ:ММ\nZoom\n\n"
                    "Пример:\nАнна\n01.03.2026\n09:00, 10:00, 11:00\nhttps://zoom.us/j/123",
                    reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, _admin_process_bulk)


def _admin_process_bulk(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=admin_markup())
        return
    try:
        lines = message.text.strip().split("\n")
        assert len(lines) >= 4
        teacher = lines[0].strip()
        date = lines[1].strip()
        times = [t.strip() for t in lines[2].split(",")]
        zoom = lines[3].strip()
        datetime.strptime(date, "%d.%m.%Y")
        added = []
        for t in times:
            datetime.strptime(t, "%H:%M")
            sid = add_slot(teacher, date, t, zoom)
            added.append(f"  #{sid} {t}")
        safe_send(message.chat.id,
                  f"✅ {len(added)} слотов на {date}:\n" + "\n".join(added),
                  reply_markup=admin_markup())
    except Exception as e:
        safe_send(message.chat.id, f"❌ Ошибка: {e}", reply_markup=admin_markup())


@bot.message_handler(func=lambda m: m.text == "🗑 Удалить слот")
def admin_delete_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    slots = get_free_slots()
    if not slots:
        safe_send(message.chat.id, "Нет свободных слотов.", reply_markup=admin_markup())
        return
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for s in slots:
        mk.add(types.KeyboardButton(f"DEL#{s[0]} {s[2]} {s[3]} {s[1]}"))
    mk.add(types.KeyboardButton("❌ Отмена"))
    msg = safe_send(message.chat.id, "Выберите слот:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, _admin_do_delete)


def _admin_do_delete(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=admin_markup())
        return
    try:
        slot_id = int(message.text.split("#")[1].split(" ")[0])
        ok = delete_slot(slot_id)
        msg = f"✅ Слот #{slot_id} удалён." if ok else "❌ Не удалось удалить."
        safe_send(message.chat.id, msg, reply_markup=admin_markup())
    except Exception as e:
        safe_send(message.chat.id, f"❌ {e}", reply_markup=admin_markup())


@bot.message_handler(func=lambda m: m.text == "👥 Ученики")
def admin_students(message):
    if message.chat.id != ADMIN_ID:
        return
    students = get_all_students()
    if not students:
        safe_send(message.chat.id, "Учеников нет.", reply_markup=admin_markup())
        return
    for s in students:
        mk = types.InlineKeyboardMarkup()
        mk.row(
            types.InlineKeyboardButton("➕ Урок", callback_data=f"addlesson_{s[0]}"),
            types.InlineKeyboardButton("➖ Урок (проведён)", callback_data=f"rmlesson_{s[0]}"),
        )
        mk.add(types.InlineKeyboardButton(
            "🚫 Блок" if s[6] == "active" else "✅ Разблок",
            callback_data=f"block_{s[0]}"))
        status = "✅" if s[6] == "active" else "❌"
        safe_send(message.chat.id,
                  f"👤 {s[2]} (id:{s[0]})\n📧 {s[3]}\n"
                  f"📚 {s[4]}  Баланс: {s[5]}  {status}",
                  reply_markup=mk)


@bot.message_handler(func=lambda m: m.text == "📅 Все записи")
def admin_all_bookings(message):
    if message.chat.id != ADMIN_ID:
        return
    bookings = get_all_bookings()
    if not bookings:
        safe_send(message.chat.id, "Записей нет.", reply_markup=admin_markup())
        return
    text = "📅 Все записи:\n\n"
    mk = types.InlineKeyboardMarkup()
    for b in bookings:
        text += f"[#{b[0]}] {b[1]} — {b[2]} | {b[3]} {b[4]}\n"
        mk.row(
            types.InlineKeyboardButton(f"❌ Отмена #{b[0]}", callback_data=f"cancelbook_{b[0]}"),
            types.InlineKeyboardButton(f"✅ Проведён #{b[0]}", callback_data=f"done_{b[0]}"),
        )
    safe_send(message.chat.id, text, reply_markup=mk)


@bot.message_handler(func=lambda m: m.text == "📅 Записи на дату")
def admin_bookings_date(message):
    if message.chat.id != ADMIN_ID:
        return
    msg = safe_send(message.chat.id, "Дата (ДД.ММ.ГГГГ):", reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, _admin_do_bookings_date)


def _admin_do_bookings_date(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=admin_markup())
        return
    date = message.text.strip()
    try:
        datetime.strptime(date, "%d.%m.%Y")
    except ValueError:
        safe_send(message.chat.id, "❌ Формат ДД.ММ.ГГГГ", reply_markup=admin_markup())
        return
    bookings = get_bookings_by_date(date)
    if not bookings:
        safe_send(message.chat.id, f"На {date} записей нет.", reply_markup=admin_markup())
        return
    text = f"📅 {date}:\n\n"
    mk = types.InlineKeyboardMarkup()
    for b in bookings:
        text += f"[#{b[0]}] {b[1]} — {b[2]} в {b[4]}\n"
        mk.row(
            types.InlineKeyboardButton(f"❌ #{b[0]}", callback_data=f"cancelbook_{b[0]}"),
            types.InlineKeyboardButton(f"✅ #{b[0]}", callback_data=f"done_{b[0]}"),
        )
    safe_send(message.chat.id, text, reply_markup=mk)


@bot.message_handler(func=lambda m: m.text == "🔙 Выход")
def admin_exit(message):
    if message.chat.id != ADMIN_ID:
        return
    safe_send(message.chat.id, "Выход.", reply_markup=main_menu(message.chat.id))


# ===================================================================
#        INLINE CALLBACKS (admin + student)
# ===================================================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    data = call.data
    chat_id = call.message.chat.id

    try:
        # ---------- Student: cancel own lesson ----------
        if data.startswith("stucancel_"):
            slot_id = int(data.split("_")[1])
            student = get_student(call.from_user.id)
            if not student:
                bot.answer_callback_query(call.id, "Ошибка")
                return
            # check 24h rule
            slot = get_slot_by_id(slot_id)
            if slot:
                try:
                    lesson_dt = _parse_slot_dt(slot[2], slot[3])
                    if lesson_dt - datetime.now() < timedelta(hours=24):
                        bot.answer_callback_query(call.id,
                                                  "❌ Отмена возможна минимум за 24 часа", show_alert=True)
                        return
                except ValueError:
                    pass
            ok = cancel_booking_by_student(slot_id, student[0])
            if ok:
                bot.answer_callback_query(call.id, "✅ Урок отменён, баланс возвращён")
                safe_send(call.from_user.id, "✅ Урок отменён. Урок возвращён на баланс.",
                          reply_markup=main_menu(call.from_user.id))
                safe_send(ADMIN_ID, f"ℹ️ Ученик {student[2]} отменил урок (слот #{slot_id})")
            else:
                bot.answer_callback_query(call.id, "❌ Не удалось отменить")
            return

        # ---------- Student: change timezone ----------
        if data == "changetz":
            mk = types.InlineKeyboardMarkup()
            for label, tz_val in TIMEZONES.items():
                mk.add(types.InlineKeyboardButton(label, callback_data=f"setzt_{tz_val}"))
            safe_send(call.from_user.id, "Выберите часовой пояс:", reply_markup=mk)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("setzt_"):
            tz = data[6:]
            update_student_timezone(call.from_user.id, tz)
            bot.answer_callback_query(call.id, f"✅ Часовой пояс: {tz}")
            safe_send(call.from_user.id, f"✅ Часовой пояс изменён на {tz}",
                      reply_markup=main_menu(call.from_user.id))
            return

        # ---------- Admin-only callbacks below ----------
        if chat_id != ADMIN_ID:
            return

        if data.startswith("addlesson_"):
            sid = int(data.split("_")[1])
            update_lessons_balance(sid, +1)
            bot.answer_callback_query(call.id, "✅ Урок начислен")
            safe_send(chat_id, f"✅ +1 урок ученику #{sid}")

        elif data.startswith("rmlesson_"):
            sid = int(data.split("_")[1])
            ok = update_lessons_balance(sid, -1)
            if ok:
                bot.answer_callback_query(call.id, "➖ Урок списан")
                safe_send(chat_id, f"➖ Урок списан у #{sid}")
                st = get_student_by_id(sid)
                if st and st[5] == 0:
                    safe_send(chat_id, f"⚠️ У ученика {st[2]} баланс = 0!")
            else:
                bot.answer_callback_query(call.id, "❌ Баланс уже 0")

        elif data.startswith("block_"):
            sid = int(data.split("_")[1])
            new = toggle_student_status(sid)
            label = "🚫 Заблокирован" if new == "blocked" else "✅ Разблокирован"
            bot.answer_callback_query(call.id, label)
            safe_send(chat_id, f"#{sid}: {label}")

        elif data.startswith("cancelbook_"):
            slot_id = int(data.split("_")[1])
            ok = cancel_booking(slot_id)
            if ok:
                bot.answer_callback_query(call.id, "✅ Отменено")
                safe_send(chat_id, f"✅ Запись #{slot_id} отменена, урок возвращён.")
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка")

        elif data.startswith("done_"):
            slot_id = int(data.split("_")[1])
            ok = mark_lesson_done(slot_id)
            if ok:
                bot.answer_callback_query(call.id, "✅ Проведён")
                safe_send(chat_id, f"✅ Урок #{slot_id} проведён.")
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка")

        elif data.startswith("confirmpay_"):
            # manual payment confirmation (fallback without Stripe token)
            rest = data[len("confirmpay_"):]  # "chat_id_tariff|flow"
            parts = rest.split("|")
            target_chat = int(parts[0].rsplit("_", 1)[0])
            tariff_and_flow = parts[0].rsplit("_", 1)[1] if "_" in parts[0] else ""
            flow = parts[1] if len(parts) > 1 else "new"

            # parse tariff from the button — find it in the message text
            tariff_name = None
            for t in TARIFFS:
                if t in call.message.text:
                    tariff_name = t
                    break
            if not tariff_name:
                bot.answer_callback_query(call.id, "❌ Тариф не найден")
                return

            tariff = TARIFFS[tariff_name]
            if flow == "repurchase":
                repurchase_tariff(target_chat, tariff_name, tariff["lessons"])
                student = get_student(target_chat)
                safe_send(target_chat,
                          f"✅ Оплата подтверждена!\n📚 {tariff_name}\n"
                          f"Баланс: {student[5]} уроков",
                          reply_markup=main_menu(target_chat))
            else:
                state = get_reg_state(target_chat)
                tz = _user_tz_cache.pop(target_chat, "Europe/Paris")
                name = state["name"] if state else "Ученик"
                email = state["email"] if state else "—"
                add_student(target_chat, name, email, tariff_name, tariff["lessons"], tz)
                clear_reg_state(target_chat)
                safe_send(target_chat,
                          f"✅ Оплата подтверждена, {name}!\n"
                          f"Тариф: {tariff_name}\nУроков: {tariff['lessons']}",
                          reply_markup=main_menu(target_chat))

            bot.answer_callback_query(call.id, "✅ Подтверждено")
            safe_send(chat_id, f"✅ Оплата подтверждена для пользователя.")

    except Exception:
        log.exception("Callback error: %s", data)
        try:
            bot.answer_callback_query(call.id, "Ошибка")
        except Exception:
            pass


# ===================================================================
#        CATCH-ALL
# ===================================================================

@bot.message_handler(func=lambda m: True)
def echo(message):
    safe_send(message.chat.id, "Нажмите кнопку в меню 😊",
              reply_markup=main_menu(message.chat.id))


# ===================================================================
#        REMINDERS (background thread)
# ===================================================================

def _reminder_loop():
    """Check every 5 min for upcoming lessons, send reminders."""
    while True:
        try:
            now = datetime.now()

            for flag, hours, label in [
                ("reminded_24h", 24, "Завтра"),
                ("reminded_2h", 2, "Через ~2 часа"),
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
                                  f"⏰ Напоминание! {label} у вас урок:\n\n"
                                  f"📅 {date} в {time_str}\n"
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

    log.info("Bot polling started. STRIPE_PROVIDER_TOKEN=%s",
             "SET" if STRIPE_PROVIDER_TOKEN else "NOT SET (manual mode)")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


if __name__ == "__main__":
    main()