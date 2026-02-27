import os
import sqlite3
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

DB = "bot.db"

# -----------------------------
# DATABASE
# -----------------------------
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            step TEXT,
            tyre TEXT,
            postcode TEXT,
            budget TEXT,
            date TEXT,
            time TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()


def get_user(phone):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    return user


def update_user(phone, **fields):
    conn = sqlite3.connect(DB)
    user = get_user(phone)

    if not user:
        conn.execute("INSERT INTO users (phone, step) VALUES (?, ?)", (phone, "MENU"))
        conn.commit()
        user = get_user(phone)

    for key, value in fields.items():
        conn.execute(f"UPDATE users SET {key} = ? WHERE phone = ?", (value, phone))

    conn.commit()
    conn.close()


def reset_user(phone):
    update_user(phone,
        step="MENU",
        tyre=None,
        postcode=None,
        budget=None,
        date=None,
        time=None
    )

# -----------------------------
# HELPERS
# -----------------------------
def reply(text):
    r = MessagingResponse()
    r.message(text)
    return str(r)


# -----------------------------
# WHATSAPP ROUTE
# -----------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.values.get("From")
    msg = request.values.get("Body", "").strip()

    if not get_user(phone):
        update_user(phone, step="MENU")

    user = get_user(phone)
    step = user["step"]

    # MENU
    if step == "MENU":
        if msg.lower() in ["1", "book"]:
            update_user(phone, step="TYRE")
            return reply("🚗 What is your tyre size? (Example: 225/40R18)")
        return reply("Welcome 👋\nReply 1 to Book a Tyre Fitting")

    # TYRE
    if step == "TYRE":
        update_user(phone, tyre=msg, step="POSTCODE")
        return reply("📍 What is your postcode?")

    # POSTCODE
    if step == "POSTCODE":
        update_user(phone, postcode=msg, step="BUDGET")
        return reply("💰 Choose budget:\n1 = Budget\n2 = Mid-range\n3 = Premium")

    # BUDGET
    if step == "BUDGET":
        if msg == "1":
            budget = "Budget"
        elif msg == "2":
            budget = "Mid-range"
        elif msg == "3":
            budget = "Premium"
        else:
            return reply("Reply 1, 2 or 3")

        update_user(phone, budget=budget, step="DATE")
        return reply("📅 What date do you want? (YYYY-MM-DD)")

    # DATE
    if step == "DATE":
        try:
            datetime.strptime(msg, "%Y-%m-%d")
        except:
            return reply("Please enter date like 2026-03-05")

        update_user(phone, date=msg, step="TIME")
        return reply("⏰ What time? (HH:MM 24h format)")

    # TIME
    if step == "TIME":
        try:
            datetime.strptime(msg, "%H:%M")
        except:
            return reply("Enter time like 14:30")

        update_user(phone, time=msg, step="CONFIRM")

        user = get_user(phone)

        summary = (
            "✅ Confirm booking:\n\n"
            f"Tyre: {user['tyre']}\n"
            f"Postcode: {user['postcode']}\n"
            f"Budget: {user['budget']}\n"
            f"Date: {user['date']}\n"
            f"Time: {user['time']}\n\n"
            "Reply YES to confirm"
        )

        return reply(summary)

    # CONFIRM
    if step == "CONFIRM":
        if msg.lower() == "yes":
            reset_user(phone)
            return reply("🎉 Booking confirmed! We’ll see you then.")
        return reply("Reply YES to confirm booking")

    return reply("Something went wrong. Reply 1 to start again.")


@app.route("/health")
def health():
    return {"ok": True}
