import sqlite3

def init_db():
    conn = sqlite3.connect("school.db")
    c = conn.cursor()

    # Ученики
    c.execute('''CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        name TEXT,
        email TEXT,
        tariff TEXT,
        lessons_balance INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active'
    )''')

    # Расписание
    c.execute('''CREATE TABLE IF NOT EXISTS schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher TEXT,
        date TEXT,
        time TEXT,
        zoom_link TEXT,
        student_id INTEGER DEFAULT NULL
    )''')

    conn.commit()
    conn.close()

def add_student(telegram_id, name, email, tariff, lessons):
    conn = sqlite3.connect("school.db")
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO students 
        (telegram_id, name, email, tariff, lessons_balance)
        VALUES (?, ?, ?, ?, ?)''',
        (telegram_id, name, email, tariff, lessons))
    conn.commit()
    conn.close()

def get_student(telegram_id):
    conn = sqlite3.connect("school.db")
    c = conn.cursor()
    c.execute("SELECT * FROM students WHERE telegram_id=?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_free_slots():
    conn = sqlite3.connect("school.db")
    c = conn.cursor()
    c.execute("SELECT * FROM schedule WHERE student_id IS NULL ORDER BY date, time")
    rows = c.fetchall()
    conn.close()
    return rows

def book_slot(slot_id, student_id):
    conn = sqlite3.connect("school.db")
    c = conn.cursor()
    c.execute("UPDATE schedule SET student_id=? WHERE id=?", (student_id, slot_id))
    conn.commit()
    conn.close()

def get_student_slots(student_id):
    conn = sqlite3.connect("school.db")
    c = conn.cursor()
    c.execute("SELECT * FROM schedule WHERE student_id=? ORDER BY date, time", (student_id,))
    rows = c.fetchall()
    conn.close()
    return rows

init_db()