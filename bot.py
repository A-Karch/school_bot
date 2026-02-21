import telebot
import os
import sqlite3
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from telebot import types
from database import add_student, get_student, get_free_slots, book_slot, get_student_slots

TOKEN = os.environ.get("TOKEN")
ADMIN_ID = 7415299809
bot = telebot.TeleBot(TOKEN)

TARIFFS = {
    "🥉 Старт — 8 уроков": {"lessons": 8, "price": 80},
    "🥈 Стандарт — 16 уроков": {"lessons": 16, "price": 140},
    "🥇 Премиум — 24 урока": {"lessons": 24, "price": 190}
}

user_data = {}
def send_reminders():
    now = datetime.now()
    reminder_time = now + timedelta(hours=1)
    target_date = reminder_time.strftime("%d.%m.%Y")
    target_time = reminder_time.strftime("%H:%M")

    conn = sqlite3.connect("school.db")
    c = conn.cursor()
    c.execute('''SELECT sc.date, sc.time, sc.teacher, sc.zoom_link, s.telegram_id, s.name
                 FROM schedule sc
                 JOIN students s ON sc.student_id = s.id
                 WHERE sc.date = ? AND sc.time = ?''', (target_date, target_time))
    lessons = c.fetchall()
    conn.close()

    for lesson in lessons:
        try:
            bot.send_message(lesson[4],
            f"⏰ Напоминание!\n\n"
            f"Через 1 час у вас урок:\n\n"
            f"📅 {lesson[0]} в {lesson[1]}\n"
            f"👩‍🏫 Преподаватель: {lesson[2]}\n"
            f"🔗 Zoom: {lesson[3]}\n\n"
            f"Удачного урока, {lesson[5]}! 🎓")
        except:
            pass

scheduler = BackgroundScheduler()
scheduler.add_job(send_reminders, 'interval', minutes=1)
scheduler.start()

def main_menu(telegram_id):
    student = get_student(telegram_id)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if student:
        markup.add(types.KeyboardButton("📅 Расписание"))
        markup.add(types.KeyboardButton("👤 Личный кабинет"))
        markup.add(types.KeyboardButton("📚 Мои уроки"))
    else:
        markup.add(types.KeyboardButton("📝 Записаться"))
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    student = get_student(message.chat.id)
    if student:
        bot.send_message(message.chat.id,
        f"С возвращением, {student[2]}! 👋",
        reply_markup=main_menu(message.chat.id))
    else:
        bot.send_message(message.chat.id,
        "Добро пожаловать в школу английского языка! 🎓\n\n"
        "Здесь вы можете записаться на курс, управлять уроками и получать напоминания.",
        reply_markup=main_menu(message.chat.id))

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID:
        bot.send_message(message.chat.id, "У вас нет доступа.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("➕ Добавить слот"))
    markup.add(types.KeyboardButton("👥 Список учеников"))
    markup.add(types.KeyboardButton("📅 Все записи"))
    markup.add(types.KeyboardButton("🔙 Выход"))
    bot.send_message(message.chat.id, "Админ панель:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "📝 Записаться")
def register_name(message):
    msg = bot.send_message(message.chat.id,
    "Давайте начнём! Как вас зовут?",
    reply_markup=types.ReplyKeyboardRemove())
    bot.register_next_step_handler(msg, process_name)

def process_name(message):
    user_data[message.chat.id] = {"name": message.text}
    msg = bot.send_message(message.chat.id, "Введите ваш email:")
    bot.register_next_step_handler(msg, process_email)

def process_email(message):
    if "@" not in message.text:
        msg = bot.send_message(message.chat.id, "Некорректный email. Попробуйте снова:")
        bot.register_next_step_handler(msg, process_email)
        return
    user_data[message.chat.id]["email"] = message.text
    choose_tariff(message)

def choose_tariff(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tariff in TARIFFS:
        markup.add(types.KeyboardButton(tariff))
    bot.send_message(message.chat.id, "Выберите тариф:", reply_markup=markup)
    bot.register_next_step_handler(message, process_tariff)

def process_tariff(message):
    if message.text not in TARIFFS:
        bot.send_message(message.chat.id, "Выберите тариф из меню")
        bot.register_next_step_handler(message, process_tariff)
        return
    user_data[message.chat.id]["tariff"] = message.text
    show_payment(message)

def show_payment(message):
    data = user_data[message.chat.id]
    tariff = TARIFFS[data["tariff"]]
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("✅ Я оплатил"))
    markup.add(types.KeyboardButton("🔙 Назад"))
    bot.send_message(message.chat.id,
    f"Отлично! Ваш заказ:\n\n"
    f"👤 Имя: {data['name']}\n"
    f"📧 Email: {data['email']}\n"
    f"📚 Тариф: {data['tariff']}\n"
    f"💰 Стоимость: {tariff['price']}€\n\n"
    f"Для оплаты перейдите по ссылке:\n"
    f"👉 https://buy.stripe.com/test_demo\n\n"
    f"После оплаты нажмите кнопку ниже.",
    reply_markup=markup)
    bot.register_next_step_handler(message, process_payment)

def process_payment(message):
    if message.text == "🔙 Назад":
        choose_tariff(message)
        return
    if message.text != "✅ Я оплатил":
        bot.register_next_step_handler(message, process_payment)
        return

    data = user_data[message.chat.id]
    tariff = TARIFFS[data["tariff"]]
    add_student(message.chat.id, data["name"], data["email"], data["tariff"], tariff["lessons"])

    bot.send_message(ADMIN_ID,
    f"🎉 Новый ученик!\n\n"
    f"👤 {data['name']}\n"
    f"📧 {data['email']}\n"
    f"📚 {data['tariff']}\n"
    f"💰 {tariff['price']}€")

    bot.send_message(message.chat.id,
    f"✅ Добро пожаловать, {data['name']}!\n\n"
    f"Ваш тариф: {data['tariff']}\n"
    f"Уроков на балансе: {tariff['lessons']}\n\n"
    f"Теперь вы можете записаться на урок!",
    reply_markup=main_menu(message.chat.id))

    user_data.pop(message.chat.id, None)

@bot.message_handler(func=lambda m: m.text == "📅 Расписание")
def show_schedule(message):
    slots = get_free_slots()
    if not slots:
        bot.send_message(message.chat.id,
        "Свободных слотов пока нет. Попробуйте позже.",
        reply_markup=main_menu(message.chat.id))
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for slot in slots:
        markup.add(types.KeyboardButton(f"📅 {slot[2]} {slot[3]} — {slot[1]}"))
    markup.add(types.KeyboardButton("🔙 Назад"))
    bot.send_message(message.chat.id, "Выберите удобный слот:", reply_markup=markup)
    bot.register_next_step_handler(message, process_slot_booking)

def process_slot_booking(message):
    if message.text == "🔙 Назад":
        start(message)
        return
    slots = get_free_slots()
    selected = None
    for slot in slots:
        if f"📅 {slot[2]} {slot[3]} — {slot[1]}" == message.text:
            selected = slot
            break
    if not selected:
        bot.send_message(message.chat.id, "Выберите слот из меню")
        bot.register_next_step_handler(message, process_slot_booking)
        return

    student = get_student(message.chat.id)
    book_slot(selected[0], student[0])

    bot.send_message(message.chat.id,
    f"✅ Вы записаны!\n\n"
    f"📅 Дата: {selected[2]}\n"
    f"🕐 Время: {selected[3]}\n"
    f"👩‍🏫 Преподаватель: {selected[1]}\n"
    f"🔗 Zoom: {selected[4]}",
    reply_markup=main_menu(message.chat.id))

@bot.message_handler(func=lambda m: m.text == "👤 Личный кабинет")
def cabinet(message):
    student = get_student(message.chat.id)
    bot.send_message(message.chat.id,
    f"👤 Личный кабинет\n\n"
    f"Имя: {student[2]}\n"
    f"Email: {student[3]}\n"
    f"Тариф: {student[4]}\n"
    f"Уроков на балансе: {student[5]}\n"
    f"Статус: {'✅ Активен' if student[6] == 'active' else '❌ Заблокирован'}",
    reply_markup=main_menu(message.chat.id))

@bot.message_handler(func=lambda m: m.text == "📚 Мои уроки")
def my_lessons(message):
    student = get_student(message.chat.id)
    slots = get_student_slots(student[0])
    if not slots:
        bot.send_message(message.chat.id,
        "У вас пока нет записей на уроки.\n\nНажмите 📅 Расписание чтобы записаться.",
        reply_markup=main_menu(message.chat.id))
        return

    text = "📚 Ваши уроки:\n\n"
    for slot in slots:
        text += f"📅 {slot[2]} в {slot[3]} — {slot[1]}\n🔗 {slot[4]}\n\n"
    bot.send_message(message.chat.id, text, reply_markup=main_menu(message.chat.id))

@bot.message_handler(func=lambda m: m.text == "➕ Добавить слот")
def add_slot_step1(message):
    if message.chat.id != ADMIN_ID:
        return
    msg = bot.send_message(message.chat.id,
    "Введите данные слота в формате:\n\n"
    "Имя преподавателя\n"
    "ДД.ММ.ГГГГ\n"
    "ЧЧ:ММ\n"
    "Ссылка на Zoom\n\n"
    "Например:\nАнна\n28.02.2026\n14:00\nhttps://zoom.us/j/123456",
    reply_markup=types.ReplyKeyboardRemove())
    bot.register_next_step_handler(msg, process_add_slot)

def process_add_slot(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        lines = message.text.strip().split("\n")
        teacher = lines[0]
        date = lines[1]
        time = lines[2]
        zoom = lines[3]

        conn = sqlite3.connect("school.db")
        c = conn.cursor()
        c.execute("INSERT INTO schedule (teacher, date, time, zoom_link) VALUES (?, ?, ?, ?)",
                  (teacher, date, time, zoom))
        conn.commit()
        conn.close()

        bot.send_message(message.chat.id,
        f"✅ Слот добавлен!\n\n"
        f"👩‍🏫 {teacher}\n📅 {date}\n🕐 {time}\n🔗 {zoom}")
        admin_panel(message)
    except:
        bot.send_message(message.chat.id, "❌ Ошибка. Проверьте формат и попробуйте снова.")
        admin_panel(message)

@bot.message_handler(func=lambda m: m.text == "👥 Список учеников")
def list_students(message):
    if message.chat.id != ADMIN_ID:
        return
    conn = sqlite3.connect("school.db")
    c = conn.cursor()
    c.execute("SELECT * FROM students")
    students = c.fetchall()
    conn.close()

    if not students:
        bot.send_message(message.chat.id, "Учеников пока нет.")
        return

    for s in students:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("➕ Урок", callback_data=f"addlesson_{s[0]}"),
            types.InlineKeyboardButton("🚫 Блок", callback_data=f"block_{s[0]}")
        )
        status = "✅ Активен" if s[6] == "active" else "❌ Заблокирован"
        bot.send_message(message.chat.id,
        f"👤 {s[2]}\n"
        f"📧 {s[3]}\n"
        f"📚 {s[4]}\n"
        f"Уроков: {s[5]}\n"
        f"Статус: {status}",
        reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "📅 Все записи")
def all_bookings(message):
    if message.chat.id != ADMIN_ID:
        return
    conn = sqlite3.connect("school.db")
    c = conn.cursor()
    c.execute('''SELECT s.name, sc.date, sc.time, sc.teacher, sc.zoom_link 
                 FROM schedule sc 
                 JOIN students s ON sc.student_id = s.id 
                 WHERE sc.student_id IS NOT NULL''')
    bookings = c.fetchall()
    conn.close()

    if not bookings:
        bot.send_message(message.chat.id, "Записей пока нет.")
        return

    text = "📅 Все записи:\n\n"
    for b in bookings:
        text += f"👤 {b[0]}\n📅 {b[1]} {b[2]}\n👩‍🏫 {b[3]}\n🔗 {b[4]}\n\n"
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "🔙 Выход")
def admin_exit(message):
    if message.chat.id != ADMIN_ID:
        return
    bot.send_message(message.chat.id, "Выход из админ панели.",
    reply_markup=main_menu(message.chat.id))

@bot.callback_query_handler(func=lambda call: call.data.startswith("addlesson_") or call.data.startswith("block_"))
def handle_admin_actions(call):
    if call.message.chat.id != ADMIN_ID:
        return
    action, student_id = call.data.split("_")
    student_id = int(student_id)

    conn = sqlite3.connect("school.db")
    c = conn.cursor()

    if action == "addlesson":
        c.execute("UPDATE students SET lessons_balance = lessons_balance + 1 WHERE id=?", (student_id,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, "✅ Урок начислен")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, "✅ Урок начислен ученику")

    elif action == "block":
        c.execute("SELECT status FROM students WHERE id=?", (student_id,))
        current = c.fetchone()[0]
        new_status = "blocked" if current == "active" else "active"
        c.execute("UPDATE students SET status=? WHERE id=?", (new_status, student_id))
        conn.commit()
        conn.close()
        status_text = "🚫 Заблокирован" if new_status == "blocked" else "✅ Разблокирован"
        bot.answer_callback_query(call.id, status_text)
        bot.send_message(call.message.chat.id, f"Статус ученика изменён: {status_text}")

@bot.message_handler(func=lambda m: True)
def echo(message):
    bot.send_message(message.chat.id, "Нажми кнопку в меню 😊",
    reply_markup=main_menu(message.chat.id))

print("Бот запущен...")
bot.polling()