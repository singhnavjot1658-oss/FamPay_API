import imaplib
import email
import re
import os
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASS = os.environ.get("EMAIL_APP_PASS", "")
IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
#  CORE LOGIC
# ─────────────────────────────────────────────

def clean_body(text: str) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)      # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_email_body(msg) -> str:
    """Recursively extract plain text + HTML from email parts."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in ("text/plain", "text/html"):
                charset = part.get_content_charset() or "utf-8"
                try:
                    body += part.get_payload(decode=True).decode(charset, errors="replace") + " "
                except Exception:
                    pass
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            body = msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            pass
    return body


def parse_transaction(body: str, date_obj: datetime) -> dict:
    """Extract transaction fields from email body text."""
    body_clean = clean_body(body)

    # Amount
    amount = (
        re.search(r"₹\s*([\d,]+(?:\.\d+)?)", body_clean) or
        re.search(r"Rs\.?\s*([\d,]+(?:\.\d+)?)", body_clean, re.I) or
        re.search(r"INR\s*([\d,]+(?:\.\d+)?)", body_clean, re.I)
    )
    amount = amount.group(1) if amount else "0.0"

    # UTR / Ref
    utr = (
        re.search(r"UTR\s*[:#]?\s*(\d{10,})", body_clean, re.I) or
        re.search(r"UPI\s*Ref(?:\s*No)?\s*[:#]?\s*(\d{10,})", body_clean, re.I) or
        re.search(r"Ref(?:\s*No(?:umber)?)?\s*[:#]?\s*(\d{10,})", body_clean, re.I) or
        re.search(r"\b(\d{12})\b", body_clean)
    )
    utr = utr.group(1) if utr else ""

    # Transaction ID
    txn = (
        re.search(r"Transaction\s*ID\s*[:#]?\s*([A-Z0-9]{6,})", body_clean, re.I) or
        re.search(r"Txn\s*ID\s*[:#]?\s*([A-Z0-9]{6,})", body_clean, re.I) or
        re.search(r"Fampay\s*Txn\s*[:#]?\s*([A-Z0-9]{6,})", body_clean, re.I)
    )
    txn = txn.group(1) if txn else ""

    # Sender / Payer name
    sender = (
        re.search(r"from\s+([A-Z][A-Z\s]{1,30})(?=\s+(?:to|on|via|at|for|UTR|Ref)\b)", body_clean, re.I) or
        re.search(r"Paid\s+by\s+([A-Z][A-Z\s]{1,30})(?=\s+(?:to|on|via|at)\b)", body_clean, re.I)
    )
    sender = sender.group(1).strip().title() if sender else "Unknown"

    # Status
    is_failed = bool(re.search(r"fail|decline|unsuccessful|reversed", body_clean, re.I))
    status = "Failed" if is_failed else "Success"

    # Format date/time
    date_str = date_obj.strftime("%d %b %Y %H:%M:%S") if date_obj else ""
    time_str = date_obj.strftime("%I:%M %p") if date_obj else ""

    return {
        "date":    date_str,
        "time":    time_str,
        "money":   amount,
        "name":    sender,
        "txn_id":  txn,
        "utr":     utr,
        "payment": status,
    }


def search_gmail(query_id: str) -> dict:
    """Connect via IMAP and search for email containing query_id."""
    if not EMAIL_ADDRESS or not EMAIL_APP_PASS:
        return {
            "error": "Server improperly configured: EMAIL_ADDRESS / EMAIL_APP_PASS env vars missing.",
            "found": False,
        }

    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_ADDRESS, EMAIL_APP_PASS)
    except imaplib.IMAP4.error as e:
        return {"error": f"Login failed: {str(e)}. Check your email and App Password.", "found": False}
    except Exception as e:
        return {"error": f"Connection error: {str(e)}", "found": False}

    try:
        mail.select("INBOX")

        # Search for emails containing the query string
        status, data = mail.search(None, f'TEXT "{query_id}"')
        if status != "OK" or not data[0]:
            mail.logout()
            return {"query": query_id, "found": False, "results": []}

        # Get the most recent match
        email_ids = data[0].split()
        latest_id = email_ids[-1]

        status, msg_data = mail.fetch(latest_id, "(RFC822)")
        if status != "OK":
            mail.logout()
            return {"query": query_id, "found": False, "results": []}

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        # Parse date
        date_str = msg.get("Date", "")
        try:
            date_obj = email.utils.parsedate_to_datetime(date_str)
        except Exception:
            date_obj = datetime.now()

        body = extract_email_body(msg)
        result = parse_transaction(body, date_obj)

        mail.logout()
        return {
            "query":   query_id,
            "found":   True,
            "results": [result]
        }

    except Exception as e:
        try:
            mail.logout()
        except Exception:
            pass
        return {"error": str(e), "found": False}


# ─────────────────────────────────────────────
#  FLASK ROUTES (API only, no web UI)
# ─────────────────────────────────────────────
@app.route("/api/fampay")
def api_fampay():
    query_id = request.args.get("id") or request.args.get("utr") or request.args.get("transaction", "")

    if not query_id:
        return jsonify({
            "error": "Provide UTR or Transaction ID via ?id=, ?utr=, or ?transaction="
        }), 400

    result = search_gmail(query_id.strip())

    # API Credit
    result["credit"] = "API by @mars24x7 | Dev @marshelp01"

    return jsonify(result)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})
