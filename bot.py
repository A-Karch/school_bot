import os
import logging
import threading
import time as _time
from datetime import datetime, timedelta

import telebot
from telebot import types

from database import (
    add_student, get_student, get_free_slots, book_slot, get_student_slots,
    save_reg_state, get_reg_state, clear_reg_state,
    get_all_students, get_student_by_id, update_lessons_balance,
    toggle_student_status, add_slot, delete_slot, cancel_booking,
    get_all_bookings, get_bookings_by_date, get_all_slots,
    get_upcoming_unreminded, mark_reminded, mark_lesson_done,
    get_free_slots_by_date, get_slot_by_id,
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

bot = telebot.TeleBot(TOKEN, parse_mode=None)

TARIFFS = {
    "🥉 Старт — 8 уроков":    {"lessons": 8,  "price": 80},
    "🥈 Стандарт — 16 уроков": {"lessons": 16, "price": 140},
    "🥇 Премиум — 24 урока":  {"lessons": 24, "price": 190},
}

CANCEL_TEXTS = {"❌ Отмена", "⬅️ Назад"}


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def is_cancel(text: str) -> bool:
    return text in CANCEL_TEXTS


def main_menu(telegram_id: int):
    student = get_student(telegram_id)
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if student:
        mk.add(types.KeyboardButton("📅 Расписание"))
        mk.add(types.KeyboardButton("📚 Мои уроки"))
        mk.add(types.KeyboardButton("👤 Личный кабинет"))
    else:
        mk.add(types.KeyboardButton("📝 Записаться"))
    return mk


def admin_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.add(types.KeyboardButton("➕ Добавить слот"))
    mk.add(types.KeyboardButton("➕ Слоты на дату"))
    mk.add(types.KeyboardButton("🗑 Удалить слот"))
    mk.add(types.KeyboardButton("👥 Список учеников"))
    mk.add(types.KeyboardButton("📅 Все записи"))
    mk.add(types.KeyboardButton("📅 Записи на дату"))
    mk.add(types.KeyboardButton("🔙 Выход"))
    return mk


def cancel_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.add(types.KeyboardButton("❌ Отмена"))
    return mk


def back_cancel_markup():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.add(types.KeyboardButton("⬅️ Назад"), types.KeyboardButton("❌ Отмена"))
    return mk


def safe_send(chat_id, text, **kwargs):
    """Send a message, swallowing network errors so the bot doesn't crash."""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        log.exception("Failed to send message to %s", chat_id)
        return None


# ---------------------------------------------------------------------------
#  /start
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    clear_reg_state(message.chat.id)  # reset any pending registration
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


# ---------------------------------------------------------------------------
#  Registration flow  (state in DB → survives restart)
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text == "📝 Записаться")
def reg_start(message):
    if get_student(message.chat.id):
        safe_send(message.chat.id, "Вы уже зарегистрированы!",
                  reply_markup=main_menu(message.chat.id))
        return
    save_reg_state(message.chat.id, "name")
    msg = safe_send(message.chat.id,
                    "Давайте начнём! Как вас зовут?",
                    reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, reg_process_name)


def reg_process_name(message):
    if is_cancel(message.text):
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Регистрация отменена.",
                  reply_markup=main_menu(message.chat.id))
        return
    save_reg_state(message.chat.id, "email", name=message.text)
    msg = safe_send(message.chat.id, "Введите ваш email:",
                    reply_markup=cancel_markup())
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
    save_reg_state(message.chat.id, "tariff", email=message.text)
    _show_tariff_menu(message)


def _show_tariff_menu(message):
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in TARIFFS:
        mk.add(types.KeyboardButton(t))
    mk.add(types.KeyboardButton("⬅️ Назад"), types.KeyboardButton("❌ Отмена"))
    msg = safe_send(message.chat.id, "Выберите тариф:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, reg_process_tariff)


def reg_process_tariff(message):
    if message.text == "❌ Отмена":
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Регистрация отменена.",
                  reply_markup=main_menu(message.chat.id))
        return
    if message.text == "⬅️ Назад":
        save_reg_state(message.chat.id, "email")
        msg = safe_send(message.chat.id, "Введите ваш email:",
                        reply_markup=cancel_markup())
        if msg:
            bot.register_next_step_handler(msg, reg_process_email)
        return
    if message.text not in TARIFFS:
        msg = safe_send(message.chat.id, "Выберите тариф из меню.")
        if msg:
            bot.register_next_step_handler(msg, reg_process_tariff)
        return

    save_reg_state(message.chat.id, "payment", tariff=message.text)
    _show_payment(message)


def _show_payment(message):
    state = get_reg_state(message.chat.id)
    if not state:
        safe_send(message.chat.id, "Сессия устарела. Начните заново: /start")
        return
    tariff_info = TARIFFS[state["tariff"]]
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.add(types.KeyboardButton("✅ Я оплатил"))
    mk.add(types.KeyboardButton("⬅️ Назад"), types.KeyboardButton("❌ Отмена"))
    msg = safe_send(
        message.chat.id,
        f"Ваш заказ:\n\n"
        f"👤 Имя: {state['name']}\n"
        f"📧 Email: {state['email']}\n"
        f"📚 Тариф: {state['tariff']}\n"
        f"💰 Стоимость: {tariff_info['price']}€\n\n"
        f"Для оплаты перейдите по ссылке:\n"
        f"👉 https://buy.stripe.com/test_demo\n\n"
        f"После оплаты нажмите кнопку ниже.",
        reply_markup=mk,
    )
    if msg:
        bot.register_next_step_handler(msg, reg_process_payment)


def reg_process_payment(message):
    if message.text == "❌ Отмена":
        clear_reg_state(message.chat.id)
        safe_send(message.chat.id, "Регистрация отменена.",
                  reply_markup=main_menu(message.chat.id))
        return
    if message.text == "⬅️ Назад":
        _show_tariff_menu(message)
        return
    if message.text != "✅ Я оплатил":
        msg = safe_send(message.chat.id, "Нажмите одну из кнопок.")
        if msg:
            bot.register_next_step_handler(msg, reg_process_payment)
        return

    state = get_reg_state(message.chat.id)
    if not state or not state.get("tariff"):
        safe_send(message.chat.id, "Сессия устарела. Начните заново: /start")
        return

    tariff_info = TARIFFS[state["tariff"]]
    add_student(message.chat.id, state["name"], state["email"],
                state["tariff"], tariff_info["lessons"])
    clear_reg_state(message.chat.id)

    # notify admin
    safe_send(ADMIN_ID,
              f"🎉 Новый ученик!\n\n"
              f"👤 {state['name']}\n"
              f"📧 {state['email']}\n"
              f"📚 {state['tariff']}\n"
              f"💰 {tariff_info['price']}€")

    safe_send(message.chat.id,
              f"✅ Добро пожаловать, {state['name']}!\n\n"
              f"Ваш тариф: {state['tariff']}\n"
              f"Уроков на балансе: {tariff_info['lessons']}\n\n"
              f"Теперь вы можете записаться на урок!",
              reply_markup=main_menu(message.chat.id))


# ---------------------------------------------------------------------------
#  📅 Расписание — book a slot
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text == "📅 Расписание")
def show_schedule(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Сначала зарегистрируйтесь: 📝 Записаться",
                  reply_markup=main_menu(message.chat.id))
        return
    if student[6] != "active":
        safe_send(message.chat.id, "Ваш аккаунт заблокирован. Обратитесь к администратору.",
                  reply_markup=main_menu(message.chat.id))
        return

    slots = get_free_slots()
    if not slots:
        safe_send(message.chat.id, "Свободных слотов пока нет. Попробуйте позже.",
                  reply_markup=main_menu(message.chat.id))
        return

    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for s in slots:
        mk.add(types.KeyboardButton(f"📅 {s[2]} {s[3]} — {s[1]}"))
    mk.add(types.KeyboardButton("❌ Отмена"))
    msg = safe_send(message.chat.id,
                    f"Уроков на балансе: {student[5]}\nВыберите удобный слот:",
                    reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, process_slot_booking)


def process_slot_booking(message):
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.",
                  reply_markup=main_menu(message.chat.id))
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
        safe_send(message.chat.id, "Сначала зарегистрируйтесь.",
                  reply_markup=main_menu(message.chat.id))
        return
    if student[5] <= 0:
        safe_send(message.chat.id,
                  "❌ У вас не осталось уроков на балансе.\n"
                  "Обратитесь к администратору для продления.",
                  reply_markup=main_menu(message.chat.id))
        return

    ok = book_slot(selected[0], student[0])
    if not ok:
        safe_send(message.chat.id,
                  "❌ Не удалось забронировать (слот уже занят или нет баланса). "
                  "Обновите расписание.",
                  reply_markup=main_menu(message.chat.id))
        return

    safe_send(message.chat.id,
              f"✅ Вы записаны!\n\n"
              f"📅 Дата: {selected[2]}\n"
              f"🕐 Время: {selected[3]}\n"
              f"👩‍🏫 Преподаватель: {selected[1]}\n"
              f"🔗 Zoom: {selected[4]}",
              reply_markup=main_menu(message.chat.id))


# ---------------------------------------------------------------------------
#  📚 Мои уроки   (shows balance + upcoming)
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text == "📚 Мои уроки")
def my_lessons(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Сначала зарегистрируйтесь.",
                  reply_markup=main_menu(message.chat.id))
        return
    slots = get_student_slots(student[0])

    text = f"📚 Мои уроки\n\nУроков на балансе: {student[5]}\n\n"
    if slots:
        text += "Предстоящие уроки:\n\n"
        for s in slots:
            text += f"📅 {s[2]} в {s[3]} — {s[1]}\n🔗 {s[4]}\n\n"
    else:
        text += "Записей пока нет.\nНажмите 📅 Расписание, чтобы записаться."

    safe_send(message.chat.id, text, reply_markup=main_menu(message.chat.id))


# ---------------------------------------------------------------------------
#  👤 Личный кабинет
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text == "👤 Личный кабинет")
def cabinet(message):
    student = get_student(message.chat.id)
    if not student:
        safe_send(message.chat.id, "Сначала зарегистрируйтесь.",
                  reply_markup=main_menu(message.chat.id))
        return
    status = "✅ Активен" if student[6] == "active" else "❌ Заблокирован"
    safe_send(
        message.chat.id,
        f"👤 Личный кабинет\n\n"
        f"Имя: {student[2]}\n"
        f"Email: {student[3]}\n"
        f"Тариф: {student[4]}\n"
        f"Уроков на балансе: {student[5]}\n"
        f"Статус: {status}",
        reply_markup=main_menu(message.chat.id),
    )


# ===================================================================
#                         ADMIN PANEL
# ===================================================================

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.chat.id != ADMIN_ID:
        safe_send(message.chat.id, "У вас нет доступа.")
        return
    safe_send(message.chat.id, "Админ-панель:", reply_markup=admin_markup())


# ---- Add single slot ----

@bot.message_handler(func=lambda m: m.text == "➕ Добавить слот")
def admin_add_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    msg = safe_send(
        message.chat.id,
        "Введите данные слота:\n\n"
        "Имя преподавателя\nДД.ММ.ГГГГ\nЧЧ:ММ\nСсылка на Zoom\n\n"
        "Пример:\nАнна\n28.02.2026\n14:00\nhttps://zoom.us/j/123",
        reply_markup=cancel_markup(),
    )
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
        assert len(lines) >= 4, "Нужно 4 строки"
        teacher, date, time_str, zoom = lines[0], lines[1], lines[2], lines[3]
        # basic validation
        datetime.strptime(date, "%d.%m.%Y")
        datetime.strptime(time_str, "%H:%M")
        slot_id = add_slot(teacher, date, time_str, zoom)
        safe_send(message.chat.id,
                  f"✅ Слот #{slot_id} добавлен!\n"
                  f"👩‍🏫 {teacher}\n📅 {date}\n🕐 {time_str}\n🔗 {zoom}",
                  reply_markup=admin_markup())
    except Exception as e:
        log.warning("add_slot parse error: %s", e)
        safe_send(message.chat.id, f"❌ Ошибка: {e}\nПроверьте формат.",
                  reply_markup=admin_markup())


# ---- Add multiple slots on a date ----

@bot.message_handler(func=lambda m: m.text == "➕ Слоты на дату")
def admin_add_bulk_slots(message):
    if message.chat.id != ADMIN_ID:
        return
    msg = safe_send(
        message.chat.id,
        "Добавление нескольких слотов.\n\n"
        "Формат:\n"
        "Имя преподавателя\nДД.ММ.ГГГГ\nЧЧ:ММ, ЧЧ:ММ, ЧЧ:ММ\nZoom-ссылка\n\n"
        "Пример:\nАнна\n01.03.2026\n09:00, 10:00, 11:00\nhttps://zoom.us/j/123",
        reply_markup=cancel_markup(),
    )
    if msg:
        bot.register_next_step_handler(msg, _admin_process_bulk_slots)


def _admin_process_bulk_slots(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=admin_markup())
        return
    try:
        lines = message.text.strip().split("\n")
        assert len(lines) >= 4, "Нужно 4 строки"
        teacher = lines[0].strip()
        date = lines[1].strip()
        times = [t.strip() for t in lines[2].split(",")]
        zoom = lines[3].strip()
        datetime.strptime(date, "%d.%m.%Y")
        added = []
        for t in times:
            datetime.strptime(t, "%H:%M")
            sid = add_slot(teacher, date, t, zoom)
            added.append(f"  #{sid}  {t}")
        safe_send(message.chat.id,
                  f"✅ Добавлено {len(added)} слотов на {date}:\n" + "\n".join(added),
                  reply_markup=admin_markup())
    except Exception as e:
        log.warning("bulk add_slot error: %s", e)
        safe_send(message.chat.id, f"❌ Ошибка: {e}", reply_markup=admin_markup())


# ---- Delete slot ----

@bot.message_handler(func=lambda m: m.text == "🗑 Удалить слот")
def admin_delete_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    slots = get_free_slots()
    if not slots:
        safe_send(message.chat.id, "Нет свободных слотов для удаления.",
                  reply_markup=admin_markup())
        return
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for s in slots:
        mk.add(types.KeyboardButton(f"DEL#{s[0]} {s[2]} {s[3]} {s[1]}"))
    mk.add(types.KeyboardButton("❌ Отмена"))
    msg = safe_send(message.chat.id, "Выберите слот для удаления:", reply_markup=mk)
    if msg:
        bot.register_next_step_handler(msg, _admin_process_delete_slot)


def _admin_process_delete_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=admin_markup())
        return
    try:
        slot_id = int(message.text.split("#")[1].split(" ")[0])
        ok = delete_slot(slot_id)
        if ok:
            safe_send(message.chat.id, f"✅ Слот #{slot_id} удалён.", reply_markup=admin_markup())
        else:
            safe_send(message.chat.id, "❌ Не удалось удалить (возможно, уже забронирован).",
                      reply_markup=admin_markup())
    except Exception as e:
        safe_send(message.chat.id, f"❌ Ошибка: {e}", reply_markup=admin_markup())


# ---- List students ----

@bot.message_handler(func=lambda m: m.text == "👥 Список учеников")
def admin_list_students(message):
    if message.chat.id != ADMIN_ID:
        return
    students = get_all_students()
    if not students:
        safe_send(message.chat.id, "Учеников пока нет.", reply_markup=admin_markup())
        return
    for s in students:
        mk = types.InlineKeyboardMarkup()
        mk.add(
            types.InlineKeyboardButton("➕ Урок", callback_data=f"addlesson_{s[0]}"),
            types.InlineKeyboardButton("➖ Урок", callback_data=f"rmlesson_{s[0]}"),
        )
        mk.add(
            types.InlineKeyboardButton(
                "🚫 Блок" if s[6] == "active" else "✅ Разблок",
                callback_data=f"block_{s[0]}"),
        )
        status = "✅ Активен" if s[6] == "active" else "❌ Заблокирован"
        safe_send(
            message.chat.id,
            f"👤 {s[2]}  (id {s[0]})\n📧 {s[3]}\n📚 {s[4]}\n"
            f"Баланс: {s[5]}   Статус: {status}",
            reply_markup=mk,
        )


# ---- All bookings / bookings by date ----

@bot.message_handler(func=lambda m: m.text == "📅 Все записи")
def admin_all_bookings(message):
    if message.chat.id != ADMIN_ID:
        return
    bookings = get_all_bookings()
    if not bookings:
        safe_send(message.chat.id, "Записей пока нет.", reply_markup=admin_markup())
        return
    text = "📅 Все записи:\n\n"
    for b in bookings:
        text += (f"[#{b[0]}] 👤 {b[1]} — 👩‍🏫 {b[2]}\n"
                 f"📅 {b[3]} {b[4]}  🔗 {b[5]}\n\n")
    # inline buttons for each booking to cancel or mark done
    mk = types.InlineKeyboardMarkup()
    for b in bookings:
        mk.add(
            types.InlineKeyboardButton(f"❌ Отменить #{b[0]}", callback_data=f"cancelbook_{b[0]}"),
            types.InlineKeyboardButton(f"✅ Проведён #{b[0]}", callback_data=f"done_{b[0]}"),
        )
    safe_send(message.chat.id, text, reply_markup=mk)


@bot.message_handler(func=lambda m: m.text == "📅 Записи на дату")
def admin_bookings_date(message):
    if message.chat.id != ADMIN_ID:
        return
    msg = safe_send(message.chat.id,
                    "Введите дату (ДД.ММ.ГГГГ):", reply_markup=cancel_markup())
    if msg:
        bot.register_next_step_handler(msg, _admin_process_bookings_date)


def _admin_process_bookings_date(message):
    if message.chat.id != ADMIN_ID:
        return
    if is_cancel(message.text):
        safe_send(message.chat.id, "Отменено.", reply_markup=admin_markup())
        return
    date = message.text.strip()
    try:
        datetime.strptime(date, "%d.%m.%Y")
    except ValueError:
        safe_send(message.chat.id, "❌ Неверный формат. Используйте ДД.ММ.ГГГГ",
                  reply_markup=admin_markup())
        return
    bookings = get_bookings_by_date(date)
    if not bookings:
        safe_send(message.chat.id, f"На {date} записей нет.", reply_markup=admin_markup())
        return
    text = f"📅 Записи на {date}:\n\n"
    mk = types.InlineKeyboardMarkup()
    for b in bookings:
        text += f"[#{b[0]}] 👤 {b[1]} — 👩‍🏫 {b[2]} в {b[4]}\n"
        mk.add(
            types.InlineKeyboardButton(f"❌ Отменить #{b[0]}", callback_data=f"cancelbook_{b[0]}"),
            types.InlineKeyboardButton(f"✅ Проведён #{b[0]}", callback_data=f"done_{b[0]}"),
        )
    safe_send(message.chat.id, text, reply_markup=mk)


# ---- Admin exit ----

@bot.message_handler(func=lambda m: m.text == "🔙 Выход")
def admin_exit(message):
    if message.chat.id != ADMIN_ID:
        return
    safe_send(message.chat.id, "Выход из админ-панели.",
              reply_markup=main_menu(message.chat.id))


# ---------------------------------------------------------------------------
#  Inline callback handler (admin actions)
# ---------------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if call.message.chat.id != ADMIN_ID:
        return
    data = call.data
    try:
        if data.startswith("addlesson_"):
            sid = int(data.split("_")[1])
            update_lessons_balance(sid, +1)
            bot.answer_callback_query(call.id, "✅ Урок начислен")
            safe_send(call.message.chat.id, f"✅ Урок начислен ученику #{sid}")

        elif data.startswith("rmlesson_"):
            sid = int(data.split("_")[1])
            ok = update_lessons_balance(sid, -1)
            if ok:
                bot.answer_callback_query(call.id, "➖ Урок списан")
                safe_send(call.message.chat.id, f"➖ Урок списан у ученика #{sid}")
            else:
                bot.answer_callback_query(call.id, "❌ Баланс уже 0")

        elif data.startswith("block_"):
            sid = int(data.split("_")[1])
            new_status = toggle_student_status(sid)
            label = "🚫 Заблокирован" if new_status == "blocked" else "✅ Разблокирован"
            bot.answer_callback_query(call.id, label)
            safe_send(call.message.chat.id, f"Статус ученика #{sid}: {label}")

        elif data.startswith("cancelbook_"):
            slot_id = int(data.split("_")[1])
            ok = cancel_booking(slot_id)
            if ok:
                bot.answer_callback_query(call.id, "✅ Запись отменена, урок возвращён")
                safe_send(call.message.chat.id,
                          f"✅ Запись #{slot_id} отменена. Урок возвращён ученику.")
            else:
                bot.answer_callback_query(call.id, "❌ Не удалось отменить")

        elif data.startswith("done_"):
            slot_id = int(data.split("_")[1])
            ok = mark_lesson_done(slot_id)
            if ok:
                bot.answer_callback_query(call.id, "✅ Урок проведён")
                safe_send(call.message.chat.id, f"✅ Урок #{slot_id} отмечен как проведённый.")
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка")

    except Exception:
        log.exception("Callback error: %s", data)
        bot.answer_callback_query(call.id, "Ошибка")


# ---------------------------------------------------------------------------
#  Catch-all
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: True)
def echo(message):
    safe_send(message.chat.id, "Нажмите кнопку в меню 😊",
              reply_markup=main_menu(message.chat.id))


# ---------------------------------------------------------------------------
#  Reminders (runs in a background thread inside the polling process)
# ---------------------------------------------------------------------------

def _parse_slot_dt(date_str: str, time_str: str) -> datetime:
    """Parse DD.MM.YYYY + HH:MM → datetime."""
    return datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")


def _reminder_loop():
    """Periodically check for upcoming lessons and send reminders."""
    while True:
        try:
            now = datetime.now()

            # --- 24h reminder ---
            for row in get_upcoming_unreminded(24, "reminded_24h"):
                slot_id, teacher, date, time_str, zoom, tg_id, name = row
                try:
                    lesson_dt = _parse_slot_dt(date, time_str)
                except ValueError:
                    continue
                diff = lesson_dt - now
                if timedelta(0) < diff <= timedelta(hours=24):
                    safe_send(tg_id,
                              f"⏰ Напоминание!\n\n"
                              f"Завтра у вас урок:\n"
                              f"📅 {date} в {time_str}\n"
                              f"👩‍🏫 {teacher}\n🔗 {zoom}")
                    mark_reminded(slot_id, "reminded_24h")

            # --- 2h reminder ---
            for row in get_upcoming_unreminded(2, "reminded_2h"):
                slot_id, teacher, date, time_str, zoom, tg_id, name = row
                try:
                    lesson_dt = _parse_slot_dt(date, time_str)
                except ValueError:
                    continue
                diff = lesson_dt - now
                if timedelta(0) < diff <= timedelta(hours=2):
                    safe_send(tg_id,
                              f"⏰ Скоро урок!\n\n"
                              f"Через ~2 часа:\n"
                              f"📅 {date} в {time_str}\n"
                              f"👩‍🏫 {teacher}\n🔗 {zoom}")
                    mark_reminded(slot_id, "reminded_2h")

        except Exception:
            log.exception("Reminder loop error")

        _time.sleep(300)  # check every 5 minutes


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

def main():
    log.info("Starting reminder thread…")
    t = threading.Thread(target=_reminder_loop, daemon=True)
    t.start()

    log.info("Bot polling started.")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


if __name__ == "__main__":
    main()