import imaplib
import email
import re
import os
import sqlite3
import random
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

# ─────────────────────────────────────────────
#  CONFIG (Vercel Environment Variables se aayega)
# ─────────────────────────────────────────────
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASS = os.environ.get("EMAIL_APP_PASS", "")
IMAP_SERVER = "imap.gmail.com"
# ⚠️ Vercel par file save nahi hoti, isliye /tmp use kar rahe hain
DB_FILE = "/tmp/fampay_orders.db"

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
#  DATABASE SETUP
# ─────────────────────────────────────────────
def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS orders (
                        order_id TEXT PRIMARY KEY,
                        user_id TEXT,
                        base_amount REAL,
                        unique_amount REAL,
                        status TEXT,
                        utr TEXT,
                        created_at TIMESTAMP
                    )''')
        c.execute('''CREATE TABLE IF NOT EXISTS used_utrs (utr TEXT PRIMARY KEY)''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Init Error: {e}")

init_db()

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# ─────────────────────────────────────────────
#  CORE LOGIC
# ─────────────────────────────────────────────
def clean_body(text: str) -> str:
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def extract_email_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors="replace") + " "
                except: pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors="replace")
        except: pass
    return body

def generate_unique_amount(base_amount):
    conn = get_db()
    while True:
        paise = random.randint(1, 99) / 100.0
        unique_amt = round(base_amount + paise, 2)
        existing = conn.execute(
            "SELECT order_id FROM orders WHERE unique_amount = ? AND status = 'pending'", 
            (unique_amt,)
        ).fetchone()
        if not existing:
            conn.close()
            return unique_amt

def check_gmail_for_amount(expected_amount):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASS:
        return {"found": False}

    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ADDRESS, EMAIL_APP_PASS)
        mail.select("INBOX")

        today = datetime.now().strftime("%d-%b-%Y")
        status, data = mail.search(None, f'(SINCE "{today}" FROM "fampay")')

        if status == "OK" and data[0]:
            email_ids = data[0].split()
            for e_id in reversed(email_ids):
                status, msg_data = mail.fetch(e_id, "(RFC822)")
                if status != "OK": continue

                msg = email.message_from_bytes(msg_data[0][1])
                body_clean = clean_body(extract_email_body(msg))

                amount_match = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)", body_clean, re.I)
                if not amount_match: continue
                
                email_amount = float(amount_match.group(1).replace(",", ""))
                
                if email_amount == float(expected_amount):
                    utr_match = re.search(r"(FMP[A-Z0-9]+|\b\d{10,12}\b)", body_clean, re.I)
                    utr = utr_match.group(1) if utr_match else None
                    
                    if utr:
                        mail.store(e_id, '+FLAGS', '\\Seen')
                        mail.logout()
                        return {"found": True, "utr": utr}
        mail.logout()
        return {"found": False}
    except Exception as e:
        print(f"Gmail Error: {str(e)}")
        return {"found": False}

# ─────────────────────────────────────────────
#  API ROUTES
# ─────────────────────────────────────────────
@app.route("/api/create_order", methods=["POST"])
def create_order():
    data = request.json
    user_id = str(data.get("user_id", ""))
    base_amount = data.get("base_amount")
    
    if not user_id or base_amount is None:
         return jsonify({"error": "user_id and base_amount are required"}), 400

    try:
        base_amount = float(base_amount)
    except ValueError:
        return jsonify({"error": "Invalid base_amount format"}), 400

    unique_amt = generate_unique_amount(base_amount)
    order_id = f"ORD{int(time.time())}{random.randint(10,99)}"

    conn = get_db()
    conn.execute(
        "INSERT INTO orders (order_id, user_id, base_amount, unique_amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, user_id, base_amount, unique_amt, "pending", datetime.now())
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "success", "order_id": order_id, "pay_amount": unique_amt})

@app.route("/api/verify_order", methods=["POST"])
def verify_order():
    data = request.json
    order_id = data.get("order_id")
    
    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()

    if not order:
        conn.close()
        return jsonify({"error": "Order not found"}), 404

    if order["status"] == "success":
        conn.close()
        return jsonify({"status": "success", "utr": order["utr"], "message": "Already verified!"})

    # ⚠️ VERCEL TIMEOUT FIX: No sleep, only 1 check per API call
    result = check_gmail_for_amount(order["unique_amount"])
    
    if result["found"]:
        utr = result["utr"]
        
        is_used = conn.execute("SELECT utr FROM used_utrs WHERE utr = ?", (utr,)).fetchone()
        if is_used:
            conn.close()
            return jsonify({"status": "fraud", "message": "This UTR is already used!"})

        conn.execute("UPDATE orders SET status = 'success', utr = ? WHERE order_id = ?", (utr, order_id))
        conn.execute("INSERT INTO used_utrs (utr) VALUES (?)", (utr,))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "utr": utr, "amount": order["unique_amount"]})

    conn.close()
    return jsonify({"status": "pending", "message": "Payment not received yet."})

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)