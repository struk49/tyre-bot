import os
import re
import sqlite3
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
# ---------------------------
# Config
# ---------------------------
APP_TZ = os.getenv("APP_TIMEZONE", "Europe/London")
DB_PATH = os.getenv("DB_PATH", "bot.db")

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Mobile Tyre Fitter")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "")
BUSINESS_AREAS = os.getenv("BUSINESS_AREAS", "Local area")
BUSINESS_INFO = os.getenv(
    "BUSINESS_INFO",
    "We’re a mobile tyre fitting service. We come to you at home/work. Same-day slots often available."
)
CALL_OUT_FEE = os.getenv("CALL_OUT_FEE", "£0")
PAYMENT_METHODS = os.getenv("PAYMENT_METHODS", "Card / Bank transfer / Cash")

app = Flask(__name__)

# ---------------------------
# DB helpers
# ---------------------------
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        user_id TEXT PRIMARY KEY,
        step TEXT NOT NULL,
        tyre_size TEXT,
        postcode TEXT,
        budget TEXT,
        preferred_date TEXT,
        preferred_time TEXT,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        tyre_size TEXT NOT NULL,
        postcode TEXT NOT NULL,
        budget TEXT NOT NULL,
        preferred_date TEXT NOT NULL,
        preferred_time TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()

init_db()

# ---------------------------
# Text utilities
# ---------------------------
def norm(s: str) -> str:
    return (s or "").strip()

def norm_lower(s: str) -> str:
    return norm(s).lower()

def now_iso():
    try:
        tz = ZoneInfo(APP_TZ)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).isoformat(timespec="seconds")
    return datetime.now(ZoneInfo(APP_TZ)).isoformat(timespec="seconds")

def twiml(text: str) -> str:
    r = MessagingResponse()
    r.message(text)
    return str(r)

# ---------------------------
# Validation / parsing
# ---------------------------
TYRE_PATTERN = re.compile(r"^\s*(\d{3})\s*\/\s*(\d{2})\s*[Rr]?\s*(\d{2})\s*$")
# Accept common UK postcodes loosely (good enough for booking intake)
POSTCODE_PATTERN = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.IGNORECASE)

def parse_tyre_size(text: str) -> str | None:
    """
    Accept: 225/40R18, 225/40 18, 225/40r18, 225/40R 18
    Store as 225/40R18
    """
    t = norm(text).replace(" ", "")
    m = TYRE_PATTERN.match(t.replace("R", "r"))
    if not m:
        return None
    w, a, rim = m.group(1), m.group(2), m.group(3)
    return f"{w}/{a}R{rim}"

def parse_postcode(text: str) -> str | None:
    t = norm(text).upper()
    t = re.sub(r"\s+", " ", t)
    if POSTCODE_PATTERN.match(t):
        return t
    return None

def parse_date_yyyy_mm_dd(text: str) -> str | None:
    """
    Expect YYYY-MM-DD for reliability.
    """
    t = norm(text)
    try:
        d = datetime.strptime(t, "%Y-%m-%d").date()
    except ValueError:
        return None
    if d < date.today():
        return None
    return d.isoformat()

def parse_time_hh_mm(text: str) -> str | None:
    """
    Expect HH:MM 24h.
    """
    t = norm(text)
    try:
        tm = datetime.strptime(t, "%H:%M").time()
    except ValueError:
        return None
    # optional: enforce business hours (e.g. 08:00-20:00)
    return tm.strftime("%H:%M")

# ---------------------------
# Conversation state
# ---------------------------
STEPS = {
    "MENU",
    "ASK_TYRE",
    "ASK_POSTCODE",
    "ASK_BUDGET",
    "ASK_DATE",
    "ASK_TIME",
    "CONFIRM",
}

def get_conv(user_id: str):
    conn = db_conn()
    row = conn.execute("SELECT * FROM conversations WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row

def upsert_conv(user_id: str, **fields):
    fields["updated_at"] = now_iso()
    conn = db_conn()
    existing = conn.execute("SELECT user_id FROM conversations WHERE user_id = ?", (user_id,)).fetchone()
    if existing:
        sets = ", ".join([f"{k} = ?" for k in fields.keys()])
        vals = list(fields.values()) + [user_id]
        conn.execute(f"UPDATE conversations SET {sets} WHERE user_id = ?", vals)
    else:
        # defaults
        base = {
            "user_id": user_id,
            "step": "MENU",
            "tyre_size": None,
            "postcode": None,
            "budget": None,
            "preferred_date": None,
            "preferred_time": None,
            "updated_at": now_iso()
        }
        base.update(fields)
        cols = ", ".join(base.keys())
        qs = ", ".join(["?"] * len(base))
        conn.execute(f"INSERT INTO conversations ({cols}) VALUES ({qs})", list(base.values()))
    conn.commit()
    conn.close()

def reset_conv(user_id: str):
    upsert_conv(
        user_id,
        step="MENU",
        tyre_size=None,
        postcode=None,
        budget=None,
        preferred_date=None,
        preferred_time=None
    )

def latest_booking_for_user(user_id: str):
    conn = db_conn()
    row = conn.execute(
        "SELECT * FROM bookings WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()
    return row

def create_booking_from_conv(user_id: str):
    conv = get_conv(user_id)
    if not conv:
        return None
    needed = ["tyre_size", "postcode", "budget", "preferred_date", "preferred_time"]
    if any(not conv[k] for k in needed):
        return None

    conn = db_conn()
    conn.execute("""
        INSERT INTO bookings (user_id, tyre_size, postcode, budget, preferred_date, preferred_time, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        conv["tyre_size"],
        conv["postcode"],
        conv["budget"],
        conv["preferred_date"],
        conv["preferred_time"],
        "CONFIRMED",
        now_iso()
    ))
    conn.commit()
    conn.close()
    return latest_booking_for_user(user_id)

# ---------------------------
# Copy blocks
# ---------------------------
def menu_text():
    return (
        f"👋 Welcome to {BUSINESS_NAME}\n\n"
        "Reply with:\n"
        "1️⃣ BOOK a fitting\n"
        "2️⃣ INFO (prices / service)\n"
        "3️⃣ STATUS (check your last booking)\n\n"
        "You can also type: MENU, CANCEL"
    )

def info_text():
    return (
        f"ℹ️ {BUSINESS_NAME} Info\n\n"
        f"• Areas: {BUSINESS_AREAS}\n"
        f"• Call-out fee: {CALL_OUT_FEE}\n"
        f"• Payment: {PAYMENT_METHODS}\n\n"
        f"{BUSINESS_INFO}\n\n"
        "To book, reply: 1"
        + (f"\n\nCall/Text: {BUSINESS_PHONE}" if BUSINESS_PHONE else "")
    )

def ask_tyre_text():
    return (
        "Great — let’s book you in ✅\n\n"
        "What’s your tyre size?\n"
        "Example: 225/40R18\n\n"
        "Tip: it’s printed on the tyre sidewall."
    )

def ask_postcode_text():
    return "What’s your postcode? (Example: B74 2AA)"

def ask_budget_text():
    return (
        "What budget level do you want?\n"
        "Reply with:\n"
        "1 = Budget\n"
        "2 = Mid-range\n"
        "3 = Premium"
    )

def ask_date_text():
    return (
        "What date do you want?\n"
        "Reply in this format: YYYY-MM-DD\n"
        "Example: 2026-03-05"
    )

def ask_time_text():
    return (
        "What time suits you?\n"
        "Reply in 24h format HH:MM\n"
        "Example: 10:30"
    )

def confirm_text(conv_row):
    return (
        "✅ Please confirm your booking:\n\n"
        f"Tyre size: {conv_row['tyre_size']}\n"
        f"Postcode: {conv_row['postcode']}\n"
        f"Budget: {conv_row['budget']}\n"
        f"Date: {conv_row['preferred_date']}\n"
        f"Time: {conv_row['preferred_time']}\n\n"
        "Reply YES to confirm, or CANCEL to start over."
    )

# ---------------------------
# WhatsApp webhook
# ---------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    # Twilio sends From like: "whatsapp:+447..."
    from_number = request.values.get("From", "")
    body = request.values.get("Body", "")
    text = norm_lower(body)

    user_id = from_number or "unknown"

    # Ensure conversation exists
    if not get_conv(user_id):
        upsert_conv(user_id, step="MENU")

    # Global commands
    if text in {"menu", "start"}:
        upsert_conv(user_id, step="MENU")
        return twiml(menu_text())

    if text in {"cancel", "reset"}:
        reset_conv(user_id)
        return twiml("✅ Cancelled. " + menu_text())

    if text in {"2", "info", "help"}:
        upsert_conv(user_id, step="MENU")
        return twiml(info_text())

    if text in {"3", "status"}:
        b = latest_booking_for_user(user_id)
        if not b:
            return twiml("You don’t have any bookings yet. Reply 1 to book.")
        return twiml(
            "📌 Your latest booking:\n\n"
            f"Tyre size: {b['tyre_size']}\n"
            f"Postcode: {b['postcode']}\n"
            f"Budget: {b['budget']}\n"
            f"Date: {b['preferred_date']}\n"
            f"Time: {b['preferred_time']}\n"
            f"Status: {b['status']}\n\n"
            "Reply MENU to start again."
        )

    # Start booking
    if text in {"1", "book", "booking"}:
        upsert_conv(user_id, step="ASK_TYRE",
                    tyre_size=None, postcode=None, budget=None, preferred_date=None, preferred_time=None)
        return twiml(ask_tyre_text())

    conv = get_conv(user_id)
    step = conv["step"]

    # If user says hi without choosing, show menu
    if step == "MENU":
        return twiml(menu_text())

    # Booking steps
    if step == "ASK_TYRE":
        tyre = parse_tyre_size(body)
        if not tyre:
            return twiml("Sorry — I didn’t catch that. Please enter like 225/40R18.")
        upsert_conv(user_id, step="ASK_POSTCODE", tyre_size=tyre)
        return twiml(ask_postcode_text())

    if step == "ASK_POSTCODE":
        pc = parse_postcode(body)
        if not pc:
            return twiml("Please enter a valid UK postcode (example: B74 2AA).")
        upsert_conv(user_id, step="ASK_BUDGET", postcode=pc)
        return twiml(ask_budget_text())

    if step == "ASK_BUDGET":
        budget_map = {"1": "Budget", "2": "Mid-range", "3": "Premium",
                      "budget": "Budget", "mid": "Mid-range", "mid-range": "Mid-range", "premium": "Premium"}
        bud = budget_map.get(text)
        if not bud:
            return twiml("Reply 1 (Budget), 2 (Mid-range), or 3 (Premium).")
        upsert_conv(user_id, step="ASK_DATE", budget=bud)
        return twiml(ask_date_text())

    if step == "ASK_DATE":
        d = parse_date_yyyy_mm_dd(body)
        if not d:
            return twiml("Please reply with a valid future date in YYYY-MM-DD format. Example: 2026-03-05")
        upsert_conv(user_id, step="ASK_TIME", preferred_date=d)
        return twiml(ask_time_text())

    if step == "ASK_TIME":
        tm = parse_time_hh_mm(body)
        if not tm:
            return twiml("Please reply with time as HH:MM (24h). Example: 10:30")
        upsert_conv(user_id, step="CONFIRM", preferred_time=tm)
        conv = get_conv(user_id)
        return twiml(confirm_text(conv))

    if step == "CONFIRM":
        if text in {"yes", "y"}:
            booking = create_booking_from_conv(user_id)
            reset_conv(user_id)
            if not booking:
                return twiml("Something went wrong saving your booking. Please reply 1 to start again.")
            return twiml(
                "✅ Booking confirmed!\n\n"
                f"Tyre size: {booking['tyre_size']}\n"
                f"Postcode: {booking['postcode']}\n"
                f"Budget: {booking['budget']}\n"
                f"Date: {booking['preferred_date']}\n"
                f"Time: {booking['preferred_time']}\n\n"
                "If anything changes, reply MENU."
            )
        if text in {"no", "n"}:
            return twiml("No problem. Reply CANCEL to restart or YES to confirm.")
        return twiml("Reply YES to confirm, or CANCEL to start over.")

    # Fallback
    upsert_conv(user_id, step="MENU")
    return twiml(menu_text())

@app.get("/health")
def health():
    return {"ok": True, "time": now_iso()}

if __name__ == "__main__":
    # For local dev only
    app.run(host="127.0.0.1", port=5000, debug=True)