import sqlite3
import logging
from contextlib import contextmanager
from typing import Optional, List, Tuple, Any

DB_PATH = "school.db"
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _conn():
    """Yield a connection with WAL journal, foreign keys, and auto-close."""
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
#  Schema bootstrap + migration
# ---------------------------------------------------------------------------

def _table_columns(cursor, table: str) -> set:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def init_db():
    """Create tables if missing; add columns introduced in later versions."""
    with _conn() as conn:
        c = conn.cursor()

        # -- students ---------------------------------------------------------
        c.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER UNIQUE NOT NULL,
                name            TEXT    NOT NULL,
                email           TEXT    NOT NULL,
                tariff          TEXT    NOT NULL,
                lessons_balance INTEGER NOT NULL DEFAULT 0,
                status          TEXT    NOT NULL DEFAULT 'active'
            )
        """)

        # -- schedule ---------------------------------------------------------
        c.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher     TEXT    NOT NULL,
                date        TEXT    NOT NULL,
                time        TEXT    NOT NULL,
                zoom_link   TEXT    NOT NULL DEFAULT '',
                student_id  INTEGER DEFAULT NULL
                    REFERENCES students(id) ON DELETE SET NULL
            )
        """)

        # -- registration_state (survives restarts) ---------------------------
        c.execute("""
            CREATE TABLE IF NOT EXISTS registration_state (
                telegram_id INTEGER PRIMARY KEY,
                step        TEXT    NOT NULL DEFAULT 'name',
                name        TEXT,
                email       TEXT,
                tariff      TEXT,
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # -- Migration: add reminder flags ------------------------------------
        sched_cols = _table_columns(c, "schedule")
        if "reminded_24h" not in sched_cols:
            c.execute("ALTER TABLE schedule ADD COLUMN reminded_24h INTEGER NOT NULL DEFAULT 0")
        if "reminded_2h" not in sched_cols:
            c.execute("ALTER TABLE schedule ADD COLUMN reminded_2h INTEGER NOT NULL DEFAULT 0")

        # -- Indexes ----------------------------------------------------------
        c.execute("CREATE INDEX IF NOT EXISTS idx_students_tg   ON students(telegram_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_schedule_free ON schedule(student_id, date, time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_schedule_date ON schedule(date)")

        conn.commit()
        log.info("Database initialised / migrated successfully.")


# ---------------------------------------------------------------------------
#  Registration state helpers (replaces in-memory user_data dict)
# ---------------------------------------------------------------------------

def save_reg_state(telegram_id: int, step: str, *,
                   name: str = None, email: str = None, tariff: str = None):
    with _conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO registration_state (telegram_id, step, name, email, tariff, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                step=excluded.step,
                name=COALESCE(excluded.name, registration_state.name),
                email=COALESCE(excluded.email, registration_state.email),
                tariff=COALESCE(excluded.tariff, registration_state.tariff),
                updated_at=datetime('now')
        """, (telegram_id, step, name, email, tariff))
        conn.commit()


def get_reg_state(telegram_id: int) -> Optional[dict]:
    with _conn() as conn:
        c = conn.cursor()
        c.execute("SELECT step, name, email, tariff FROM registration_state WHERE telegram_id=?",
                  (telegram_id,))
        row = c.fetchone()
    if row is None:
        return None
    return {"step": row[0], "name": row[1], "email": row[2], "tariff": row[3]}


def clear_reg_state(telegram_id: int):
    with _conn() as conn:
        conn.execute("DELETE FROM registration_state WHERE telegram_id=?", (telegram_id,))
        conn.commit()


# ---------------------------------------------------------------------------
#  Student CRUD
# ---------------------------------------------------------------------------

def add_student(telegram_id: int, name: str, email: str, tariff: str, lessons: int):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO students (telegram_id, name, email, tariff, lessons_balance)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                name=excluded.name,
                email=excluded.email,
                tariff=excluded.tariff,
                lessons_balance=excluded.lessons_balance,
                status='active'
        """, (telegram_id, name, email, tariff, lessons))
        conn.commit()


def get_student(telegram_id: int) -> Optional[Tuple]:
    """Return full row or None.  Columns:
       0:id  1:telegram_id  2:name  3:email  4:tariff  5:lessons_balance  6:status
    """
    with _conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM students WHERE telegram_id=?", (telegram_id,))
        return c.fetchone()


def get_student_by_id(student_id: int) -> Optional[Tuple]:
    with _conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM students WHERE id=?", (student_id,))
        return c.fetchone()


def get_all_students() -> List[Tuple]:
    with _conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM students ORDER BY id")
        return c.fetchall()


def update_lessons_balance(student_id: int, delta: int) -> bool:
    """Add *delta* lessons (can be negative). Returns False if would go below 0."""
    with _conn() as conn:
        c = conn.cursor()
        c.execute("SELECT lessons_balance FROM students WHERE id=?", (student_id,))
        row = c.fetchone()
        if row is None:
            return False
        new_balance = row[0] + delta
        if new_balance < 0:
            return False
        c.execute("UPDATE students SET lessons_balance=? WHERE id=?", (new_balance, student_id))
        conn.commit()
        return True


def toggle_student_status(student_id: int) -> str:
    """Toggle active/blocked.  Returns new status string."""
    with _conn() as conn:
        c = conn.cursor()
        c.execute("SELECT status FROM students WHERE id=?", (student_id,))
        row = c.fetchone()
        new_status = "blocked" if row and row[0] == "active" else "active"
        c.execute("UPDATE students SET status=? WHERE id=?", (new_status, student_id))
        conn.commit()
        return new_status


# ---------------------------------------------------------------------------
#  Schedule / slots
# ---------------------------------------------------------------------------

def add_slot(teacher: str, date: str, time: str, zoom_link: str) -> int:
    """Insert a free slot. Returns new row id."""
    with _conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO schedule (teacher, date, time, zoom_link) VALUES (?, ?, ?, ?)",
            (teacher, date, time, zoom_link))
        conn.commit()
        return c.lastrowid


def delete_slot(slot_id: int) -> bool:
    """Delete a slot (only if not booked). Returns True on success."""
    with _conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM schedule WHERE id=? AND student_id IS NULL", (slot_id,))
        conn.commit()
        return c.rowcount == 1


def get_free_slots() -> List[Tuple]:
    """Columns: 0:id 1:teacher 2:date 3:time 4:zoom_link"""
    with _conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, teacher, date, time, zoom_link FROM schedule "
            "WHERE student_id IS NULL ORDER BY date, time")
        return c.fetchall()


def get_free_slots_by_date(date: str) -> List[Tuple]:
    with _conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, teacher, date, time, zoom_link FROM schedule "
            "WHERE student_id IS NULL AND date=? ORDER BY time", (date,))
        return c.fetchall()


def book_slot(slot_id: int, student_id: int) -> bool:
    """Atomically book a slot.  Decrements lessons_balance.
       Returns True on success, False if slot taken or no balance.
    """
    with _conn() as conn:
        c = conn.cursor()
        try:
            c.execute("BEGIN IMMEDIATE")

            # check balance
            c.execute("SELECT lessons_balance FROM students WHERE id=?", (student_id,))
            row = c.fetchone()
            if not row or row[0] <= 0:
                conn.rollback()
                return False

            # try to claim the slot
            c.execute(
                "UPDATE schedule SET student_id=? WHERE id=? AND student_id IS NULL",
                (student_id, slot_id))
            if c.rowcount != 1:
                conn.rollback()
                return False

            # decrement balance
            c.execute(
                "UPDATE students SET lessons_balance = lessons_balance - 1 WHERE id=?",
                (student_id,))

            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def cancel_booking(slot_id: int) -> bool:
    """Admin cancels a booking → frees slot AND returns lesson to student."""
    with _conn() as conn:
        c = conn.cursor()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("SELECT student_id FROM schedule WHERE id=?", (slot_id,))
            row = c.fetchone()
            if not row or row[0] is None:
                conn.rollback()
                return False
            student_id = row[0]
            c.execute(
                "UPDATE schedule SET student_id=NULL, reminded_24h=0, reminded_2h=0 WHERE id=?",
                (slot_id,))
            c.execute(
                "UPDATE students SET lessons_balance = lessons_balance + 1 WHERE id=?",
                (student_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def get_student_slots(student_id: int) -> List[Tuple]:
    """Columns: 0:id 1:teacher 2:date 3:time 4:zoom_link"""
    with _conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, teacher, date, time, zoom_link FROM schedule "
            "WHERE student_id=? ORDER BY date, time", (student_id,))
        return c.fetchall()


def get_slot_by_id(slot_id: int) -> Optional[Tuple]:
    with _conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM schedule WHERE id=?", (slot_id,))
        return c.fetchone()


def get_bookings_by_date(date: str) -> List[Tuple]:
    """Return booked slots with student name for a given date."""
    with _conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT sc.id, s.name, sc.teacher, sc.date, sc.time, sc.zoom_link
            FROM schedule sc
            JOIN students s ON sc.student_id = s.id
            WHERE sc.date = ?
            ORDER BY sc.time
        """, (date,))
        return c.fetchall()


def get_all_bookings() -> List[Tuple]:
    with _conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT sc.id, s.name, sc.teacher, sc.date, sc.time, sc.zoom_link
            FROM schedule sc
            JOIN students s ON sc.student_id = s.id
            ORDER BY sc.date, sc.time
        """)
        return c.fetchall()


def get_all_slots() -> List[Tuple]:
    """All slots (free + booked) for admin view."""
    with _conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT sc.id, sc.teacher, sc.date, sc.time, sc.zoom_link,
                   sc.student_id, COALESCE(s.name, '—')
            FROM schedule sc
            LEFT JOIN students s ON sc.student_id = s.id
            ORDER BY sc.date, sc.time
        """)
        return c.fetchall()


# ---------------------------------------------------------------------------
#  Reminder helpers
# ---------------------------------------------------------------------------

def get_upcoming_unreminded(hours: int, flag_col: str) -> List[Tuple]:
    """Return booked slots within *hours* that haven't been reminded yet.
       flag_col must be 'reminded_24h' or 'reminded_2h'.
    """
    assert flag_col in ("reminded_24h", "reminded_2h")
    with _conn() as conn:
        c = conn.cursor()
        # We store date as DD.MM.YYYY and time as HH:MM — build an ISO
        # string for comparison.
        c.execute(f"""
            SELECT sc.id, sc.teacher, sc.date, sc.time, sc.zoom_link,
                   s.telegram_id, s.name
            FROM schedule sc
            JOIN students s ON sc.student_id = s.id
            WHERE sc.{flag_col} = 0
              AND sc.student_id IS NOT NULL
        """)
        return c.fetchall()


def mark_reminded(slot_id: int, flag_col: str):
    assert flag_col in ("reminded_24h", "reminded_2h")
    with _conn() as conn:
        conn.execute(f"UPDATE schedule SET {flag_col}=1 WHERE id=?", (slot_id,))
        conn.commit()


def mark_lesson_done(slot_id: int) -> bool:
    """Admin marks a lesson as done — removes booking (frees slot row is deleted)."""
    with _conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM schedule WHERE id=? AND student_id IS NOT NULL", (slot_id,))
        conn.commit()
        return c.rowcount == 1


# ---------------------------------------------------------------------------
#  Run on import
# ---------------------------------------------------------------------------
init_db()