#!/usr/bin/env python3
"""
Marathon Runners Depository Application
A simple web application to register marathon runners with payment details
and view them on a dashboard.
"""

import http.server
import os
import re
import shutil
import socketserver
import sqlite3
import sys
import urllib.parse
import urllib.request
import urllib.error
import csv
import json
import time
import uuid
from datetime import datetime
from html import escape

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:
    Image = None
    UnidentifiedImageError = Exception

PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_VERCEL = os.environ.get("VERCEL") == "1"
# Vercel Functions can write only to /tmp, and its contents are not durable.
# Persistent deployments must use DATABASE_URL and BLOB_READ_WRITE_TOKEN.
RUNTIME_DIR = os.path.join("/tmp", "marathon-runners-depository") if IS_VERCEL else BASE_DIR
DB_PATH = os.path.join(RUNTIME_DIR, "runners.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
UPLOAD_DIR = os.path.join(RUNTIME_DIR, "uploads")
BACKUP_DIR = os.path.join(RUNTIME_DIR, "backups")
BLOB_READ_WRITE_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
GOOGLE_SHEETS_ENABLED = os.environ.get("GOOGLE_SHEETS_ENABLED", "false").lower() in ("1", "true", "yes")
GOOGLE_SHEETS_SPREADSHEET_ID = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")


def connect_db():
    if USE_POSTGRES:
        import psycopg2
        parsed = urllib.parse.urlparse(DATABASE_URL)
        conn = psycopg2.connect(
            dbname=parsed.path[1:],
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname,
            port=parsed.port or 5432,
        )
        return conn

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def query_db(sql, params=(), fetchone=False, fetchall=False, commit=False):
    conn = connect_db()
    try:
        if USE_POSTGRES:
            from psycopg2.extras import RealDictCursor
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            sql = sql.replace("?", "%s")
        else:
            cursor = conn.cursor()
        cursor.execute(sql, params)
        if commit:
            conn.commit()
        if fetchone:
            result = cursor.fetchone()
            return result
        if fetchall:
            result = cursor.fetchall()
            return result
        if commit:
            return cursor.rowcount
        return cursor
    finally:
        if conn:
            conn.close()


# Initialize database

def init_db():
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    if USE_POSTGRES:
        try:
            conn = connect_db()
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS runners (
                    id SERIAL PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    middle_name TEXT,
                    surname TEXT NOT NULL,
                    payment_mode TEXT NOT NULL,
                    payment_date TEXT NOT NULL,
                    email TEXT NOT NULL,
                    contact_number TEXT NOT NULL,
                    shirt_size TEXT NOT NULL DEFAULT 'M',
                    years_running INTEGER NOT NULL DEFAULT 0,
                    receipt_filename TEXT,
                    receipt_verified TEXT DEFAULT 'Pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT NOT NULL,
                middle_name TEXT,
                surname TEXT NOT NULL,
                payment_mode TEXT NOT NULL,
                payment_date TEXT NOT NULL,
                email TEXT NOT NULL,
                contact_number TEXT NOT NULL,
                shirt_size TEXT NOT NULL DEFAULT 'M',
                years_running INTEGER NOT NULL DEFAULT 0,
                receipt_filename TEXT,
                receipt_verified TEXT DEFAULT 'Pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("PRAGMA table_info(runners)")
        existing_columns = [row[1] for row in cursor.fetchall()]
        if "shirt_size" not in existing_columns:
            cursor.execute("ALTER TABLE runners ADD COLUMN shirt_size TEXT DEFAULT 'M'")
        if "years_running" not in existing_columns:
            cursor.execute("ALTER TABLE runners ADD COLUMN years_running INTEGER DEFAULT 0")
        if "receipt_filename" not in existing_columns:
            cursor.execute("ALTER TABLE runners ADD COLUMN receipt_filename TEXT")
        if "receipt_verified" not in existing_columns:
            cursor.execute("ALTER TABLE runners ADD COLUMN receipt_verified TEXT DEFAULT 'Pending'")
        if "created_at" not in existing_columns:
            cursor.execute("ALTER TABLE runners ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")
        conn.commit()
        conn.close()


def get_all_runners():
    return get_filtered_runners()


def get_filtered_runners(search_query="", payment_method=None, date_from=None, date_to=None):
    filters = []
    params = []

    if search_query:
        like_term = f"%{search_query.strip().lower()}%"
        filters.append("lower(first_name || ' ' || coalesce(middle_name, '') || ' ' || surname) LIKE ?")
        params.append(like_term)

    if payment_method and payment_method != "All":
        filters.append("payment_mode = ?")
        params.append(payment_method)

    if date_from:
        filters.append("payment_date >= ?")
        params.append(date_from)

    if date_to:
        filters.append("payment_date <= ?")
        params.append(date_to)

    query = "SELECT * FROM runners"
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY id DESC"

    rows = query_db(query, tuple(params), fetchall=True)
    return [dict(row) for row in rows]


def get_runner_by_id(runner_id):
    row = query_db("SELECT * FROM runners WHERE id = ?", (runner_id,), fetchone=True)
    return dict(row) if row else None


def ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)


def get_backup_filename():
    return os.path.join(BACKUP_DIR, f"runners_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")


def backup_runners_to_csv(runners):
    ensure_backup_dir()
    backup_path = get_backup_filename()
    fieldnames = [
        "id", "first_name", "middle_name", "surname", "payment_mode", "payment_date",
        "email", "contact_number", "shirt_size", "years_running", "receipt_filename",
        "receipt_verified", "created_at"
    ]
    with open(backup_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for runner in runners:
            writer.writerow({key: runner.get(key, "") for key in fieldnames})
    return backup_path


def load_google_sheets_client():
    if not GOOGLE_SHEETS_ENABLED or not GOOGLE_SHEETS_SPREADSHEET_ID or not GOOGLE_SHEETS_CREDENTIALS_JSON:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return None

    try:
        credentials_info = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file"
        ]
        creds = Credentials.from_service_account_info(credentials_info, scopes=scopes)
        return gspread.authorize(creds)
    except Exception:
        return None


def backup_runners_to_google_sheets(runners):
    client = load_google_sheets_client()
    if not client:
        return False, "Google Sheets backup is not enabled or client could not be initialized."

    try:
        spreadsheet = client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.sheet1
        headers = [
            "ID", "First Name", "Middle Name", "Surname", "Payment Mode", "Payment Date",
            "Email", "Contact Number", "Shirt Size", "Years Running", "Receipt Filename",
            "Receipt Verified", "Created At"
        ]
        rows = [headers]
        for runner in runners:
            rows.append([
                runner.get("id", ""),
                runner.get("first_name", ""),
                runner.get("middle_name", ""),
                runner.get("surname", ""),
                runner.get("payment_mode", ""),
                runner.get("payment_date", ""),
                runner.get("email", ""),
                runner.get("contact_number", ""),
                runner.get("shirt_size", ""),
                runner.get("years_running", ""),
                runner.get("receipt_filename", ""),
                runner.get("receipt_verified", ""),
                runner.get("created_at", "")
            ])
        worksheet.clear()
        worksheet.update(rows)
        return True, "Google Sheets backup completed successfully."
    except Exception as e:
        return False, f"Google Sheets backup failed: {str(e)}"


def backup_all_runners(silent=False):
    runners = get_all_runners()
    backup_path = backup_runners_to_csv(runners)
    message = f"Local backup saved to {os.path.relpath(backup_path, BASE_DIR)}."
    if GOOGLE_SHEETS_ENABLED:
        success, sheet_message = backup_runners_to_google_sheets(runners)
        if success:
            message = f"{message} {sheet_message}"
        else:
            if not silent:
                message = f"{message} {sheet_message}"
            else:
                message = message
    return backup_path, message


def sanitize_filename(filename):
    filename = os.path.basename(filename)
    return re.sub(r"[^a-zA-Z0-9._-]", "_", filename)


def is_valid_email(email):
    return bool(re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
SESSION_COOKIE_NAME = "admin_session"
SESSION_DURATION = 60 * 60 * 2
ADMIN_SESSIONS = {}


def parse_cookies(cookie_header):
    cookies = {}
    if not cookie_header:
        return cookies
    for part in cookie_header.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies


def create_admin_session():
    session_id = str(uuid.uuid4())
    ADMIN_SESSIONS[session_id] = time.time()
    return session_id


def verify_admin_session(session_id):
    if not session_id:
        return False
    expires = ADMIN_SESSIONS.get(session_id)
    if not expires:
        return False
    if time.time() - expires > SESSION_DURATION:
        ADMIN_SESSIONS.pop(session_id, None)
        return False
    ADMIN_SESSIONS[session_id] = time.time()
    return True


def admin_authenticate(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD


def normalize_ph_number(number):
    digits = re.sub(r"\D", "", number)
    if digits.startswith("0") and len(digits) == 11 and digits[1] == "9":
        return "+63" + digits[1:]
    if digits.startswith("63") and len(digits) == 12 and digits[2] == "9":
        return "+63" + digits[2:]
    if digits.startswith("9") and len(digits) == 10:
        return "+63" + digits
    return None


VALID_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif"}


def save_receipt_file(field):
    if not field or not field.get("filename"):
        return None, "No receipt uploaded"
    filename = sanitize_filename(field["filename"])
    file_ext = os.path.splitext(filename)[1].lower()
    if file_ext not in VALID_IMAGE_EXTENSIONS:
        return None, "Receipt must be an image file (PNG, JPG, JPEG, BMP, GIF)"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_name = f"receipt_{timestamp}_{filename}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)
    with open(file_path, "wb") as out_file:
        out_file.write(field["content"])
    if not is_image_file(file_path):
        os.remove(file_path)
        return None, "Uploaded file is not a valid image"
    return safe_name, ""


def persist_receipt_file(filename):
    """Store receipts in Vercel Blob when running as a Vercel Function.

    Local development continues to serve files from the uploads directory. Vercel's
    function filesystem is temporary, so a Blob token is required there instead.
    """
    if not IS_VERCEL:
        return filename, ""
    if not BLOB_READ_WRITE_TOKEN:
        return None, "Receipt storage is not configured. Set BLOB_READ_WRITE_TOKEN in Vercel."

    file_path = os.path.join(UPLOAD_DIR, filename)
    try:
        with open(file_path, "rb") as receipt_file:
            payload = receipt_file.read()
        content_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        request = urllib.request.Request(
            f"https://blob.vercel-storage.com/receipts/{urllib.parse.quote(filename)}",
            data=payload,
            method="PUT",
            headers={
                "Authorization": f"Bearer {BLOB_READ_WRITE_TOKEN}",
                "x-api-version": "7",
                "x-add-random-suffix": "0",
                "Content-Type": content_type,
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            blob = json.loads(response.read().decode("utf-8"))
        return blob["url"], ""
    except (OSError, urllib.error.URLError, KeyError, json.JSONDecodeError) as error:
        return None, f"Could not save receipt to Vercel Blob: {error}"
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass


def is_image_file(file_path):
    if Image:
        try:
            with Image.open(file_path) as img:
                img.verify()
                if img.size[0] < 200 or img.size[1] < 200:
                    return False
            return True
        except (UnidentifiedImageError, OSError):
            return False
    signatures = {
        b"\x89PNG\r\n\x1a\n": ".png",
        b"\xff\xd8\xff": ".jpg",
        b"BM": ".bmp",
        b"GIF87a": ".gif",
        b"GIF89a": ".gif",
    }
    with open(file_path, "rb") as f:
        header = f.read(16)
    for sig in signatures:
        if header.startswith(sig):
            return True
    return False


def parse_multipart(body, boundary):
    parts = body.split(b"--" + boundary)
    fields = {}
    files = {}
    for part in parts:
        if not part or part in (b"--", b"--\r\n"):
            continue
        part = part.strip(b"\r\n")
        if not part:
            continue
        header, _, content = part.partition(b"\r\n\r\n")
        if not content:
            continue
        header_lines = header.split(b"\r\n")
        disposition = None
        for line in header_lines:
            if line.lower().startswith(b"content-disposition:"):
                disposition = line.decode("utf-8", "ignore")
                break
        if not disposition:
            continue
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        field_name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        content = content.rstrip(b"\r\n")
        if filename_match and filename_match.group(1):
            files[field_name] = {
                "filename": filename_match.group(1),
                "content": content
            }
        else:
            fields[field_name] = content.decode("utf-8", "ignore")
    return fields, files


def scan_receipt_image(file_path, receipt_filename, payment_mode):
    if not receipt_filename:
        return "Pending", "No receipt image provided. Please upload a screenshot of your payment receipt."
    if not os.path.exists(file_path) or not is_image_file(file_path):
        return "Invalid", "Invalid attachment. The uploaded photo could not be read or verified as a valid receipt image."

    recognized = False
    lower_name = receipt_filename.lower()
    provider = None
    if "gcash" in lower_name or payment_mode.lower() == "gcash":
        recognized = True
        provider = "GCash"
    elif "maya" in lower_name or payment_mode.lower() == "maya":
        recognized = True
        provider = "Maya"
    elif "bank" in lower_name or payment_mode.lower() == "bank transfer" or "transfer" in lower_name:
        recognized = True
        provider = "bank transfer"

    try:
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
        lower_bytes = raw_bytes.lower()
        if not recognized:
            if b"gcash" in lower_bytes or b"maya" in lower_bytes or b"bank" in lower_bytes or b"transfer" in lower_bytes or b"receipt" in lower_bytes:
                recognized = True
    except OSError:
        return "Invalid", "Invalid attachment. The uploaded photo could not be read or verified as a valid receipt image."

    if recognized:
        if provider:
            return "Ready for Review", f"Receipt image appears to be a {provider} receipt and is ready for review."
        return "Ready for Review", "Receipt image appears to be a valid payment proof and is ready for review."

    return "Ready for Review", "Receipt image is valid and ready for review. Provider could not be automatically recognized."


def update_runner(runner_id, data):
    query_db(
        """
        UPDATE runners
        SET first_name = ?, middle_name = ?, surname = ?, payment_mode = ?, payment_date = ?, email = ?, contact_number = ?, shirt_size = ?, years_running = ?, receipt_filename = ?, receipt_verified = ?
        WHERE id = ?
        """,
        (
            data.get("first_name", "").strip(),
            data.get("middle_name", "").strip(),
            data.get("surname", "").strip(),
            data.get("payment_mode", "Cashless").strip(),
            data.get("payment_date", "").strip(),
            data.get("email", "").strip(),
            data.get("contact_number", "").strip(),
            data.get("shirt_size", "M").strip(),
            int(data.get("years_running", 0)),
            data.get("receipt_filename"),
            data.get("receipt_verified", "Pending"),
            runner_id,
        ),
        commit=True,
    )
    try:
        backup_all_runners(silent=True)
    except Exception:
        pass

def add_runner(data):
    if USE_POSTGRES:
        row = query_db(
            """
            INSERT INTO runners (first_name, middle_name, surname, payment_mode, payment_date, email, contact_number, shirt_size, years_running, receipt_filename, receipt_verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                data.get("first_name", "").strip(),
                data.get("middle_name", "").strip(),
                data.get("surname", "").strip(),
                data.get("payment_mode", "Cashless").strip(),
                data.get("payment_date", "").strip(),
                data.get("email", "").strip(),
                data.get("contact_number", "").strip(),
                data.get("shirt_size", "M").strip(),
                int(data.get("years_running", 0)),
                data.get("receipt_filename"),
                data.get("receipt_verified", "Pending"),
            ),
            fetchone=True,
            commit=True,
        )
        runner_id = row["id"] if row else None
    else:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO runners (first_name, middle_name, surname, payment_mode, payment_date, email, contact_number, shirt_size, years_running, receipt_filename, receipt_verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("first_name", "").strip(),
            data.get("middle_name", "").strip(),
            data.get("surname", "").strip(),
            data.get("payment_mode", "Cashless").strip(),
            data.get("payment_date", "").strip(),
            data.get("email", "").strip(),
            data.get("contact_number", "").strip(),
            data.get("shirt_size", "M").strip(),
            int(data.get("years_running", 0)),
            data.get("receipt_filename"),
            data.get("receipt_verified", "Pending"),
        ))
        conn.commit()
        runner_id = cursor.lastrowid
        conn.close()
    try:
        backup_all_runners(silent=True)
    except Exception:
        pass
    return runner_id

def delete_runner(runner_id):
    if USE_POSTGRES:
        rowcount = query_db(
            "DELETE FROM runners WHERE id = ?",
            (runner_id,),
            commit=True,
        )
        affected = rowcount if isinstance(rowcount, int) else 0
    else:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM runners WHERE id = ?", (runner_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
    try:
        backup_all_runners(silent=True)
    except Exception:
        pass
    return affected > 0

# HTML Templates
def get_base_html(title, content, active="dashboard", show_admin_actions=False):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - Marathon Runners Depository</title>
    <style>
        :root {{
            --primary: #1c1917;
            --primary-soft: #d65a00;
            --accent: #ff9f1c;
            --success: #c2410c;
            --danger: #b91c1c;
            --bg: #fff7ed;
            --surface: #ffffff;
            --text: #1c1917;
            --muted: #57534e;
            --border: #fed7aa;
            --shadow: 0 18px 42px rgba(28, 25, 23, 0.12);
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: Inter, 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: radial-gradient(circle at top left, rgba(255,159,28,0.22), transparent 32%),
                        linear-gradient(180deg, #fff7ed 0%, #ffedd5 100%);
            color: var(--text);
            line-height: 1.65;
            min-height: 100vh;
        }}
        header {{
            background: linear-gradient(135deg, #1c1917 0%, #3f2a1d 62%, #d65a00 150%);
            color: white;
            padding: 2rem 2rem 1.5rem;
            box-shadow: 0 20px 60px rgba(28, 25, 23, 0.22);
        }}
        header h1 {{
            font-size: clamp(1.75rem, 2.5vw, 2.5rem);
            font-weight: 700;
            letter-spacing: 0.02em;
            display: inline-flex;
            align-items: center;
            gap: 0.8rem;
        }}
        header h1 span {{ font-size: 2.2rem; }}
        header p {{
            margin-top: 0.75rem;
            max-width: 760px;
            color: rgba(255,255,255,0.82);
            font-size: 1rem;
        }}
        .page-layout {{
            display: grid;
            grid-template-columns: minmax(240px, 300px) minmax(0, 1fr);
            gap: 1.5rem;
            max-width: 1400px;
            margin: 1.5rem auto 2.5rem;
            padding: 0 1.5rem 2rem;
        }}
        nav {{
            display: flex;
            flex-direction: column;
            gap: 1rem;
            padding: 0;
        }}
        .sidebar {{
            background: rgba(255,255,255,0.92);
            border: 1px solid rgba(28,25,23,0.10);
            border-radius: 28px;
            box-shadow: var(--shadow);
            padding: 1.75rem;
            position: sticky;
            top: 1.5rem;
            min-height: calc(100vh - 6rem);
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            backdrop-filter: blur(12px);
        }}
        .sidebar h2 {{
            font-size: 1.1rem;
            margin: 0;
            color: var(--primary);
        }}
        .sidebar p {{
            color: var(--muted);
            line-height: 1.7;
            font-size: 0.95rem;
        }}
        nav a {{
            display: block;
            padding: 1rem 1.2rem;
            color: var(--text);
            text-decoration: none;
            font-weight: 600;
            background: #fffaf5;
            border-radius: 18px;
            border: 1px solid transparent;
            transition: all 0.2s ease;
        }}
        nav a:hover {{
            transform: translateX(2px);
            background: rgba(214,90,0,0.10);
            color: var(--primary);
        }}
        nav a.active {{
            color: white;
            background: var(--primary);
            border-color: rgba(214,90,0,0.25);
            box-shadow: 0 14px 30px rgba(28,25,23,0.18);
        }}
        .sidebar-actions {{
            display: grid;
            gap: 0.9rem;
            margin-top: 1rem;
        }}
        .sidebar-actions a {{
            display: inline-flex;
            justify-content: center;
            padding: 0.95rem 1rem;
            border-radius: 18px;
            border: 1px solid rgba(16,42,67,0.08);
            text-align: center;
            font-weight: 700;
            transition: transform 0.2s ease, background 0.2s ease;
        }}
        .sidebar-actions a.primary-btn {{
            background: linear-gradient(135deg, var(--primary-soft), var(--accent));
            color: white;
            border-color: transparent;
        }}
        .sidebar-actions a.secondary-btn {{
            background: #fff7ed;
            color: var(--text);
        }}
        .sidebar-actions a:hover {{
            transform: translateY(-1px);
        }}
        main {{
            padding: 0;
        }}
        .content-area {{
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }}
        .page-intro {{
            padding: 0.4rem 0.25rem 0;
        }}
        .page-intro h2 {{
            margin-bottom: 0.35rem;
            font-size: clamp(1.55rem, 2.4vw, 2rem);
            line-height: 1.2;
        }}
        .page-intro p {{ color: var(--muted); max-width: 680px; }}
        .card-title-row {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 1rem;
            margin-bottom: 1.25rem;
        }}
        .card-title-row h2 {{ margin-bottom: 0.25rem; }}
        .filter-form {{
            display: flex;
            gap: 0.75rem;
            flex-wrap: wrap;
            align-items: end;
        }}
        .filter-field {{ flex: 1 1 260px; }}
        .filter-field label {{ display: block; margin-bottom: 0.4rem; }}
        .filter-field input, .filter-field select {{ width: 100%; }}
        .results-meta {{
            color: var(--muted);
            font-size: 0.9rem;
            margin-left: auto;
        }}
        @media (max-width: 960px) {{
            .page-layout {{
                grid-template-columns: 1fr;
            }}
            .sidebar {{
                position: relative;
                min-height: auto;
                top: auto;
            }}
        }}
        .card {{
            background: var(--surface);
            border-radius: 22px;
            box-shadow: var(--shadow);
            padding: 2rem;
            margin-bottom: 1.75rem;
            border: 1px solid rgba(28,25,23,0.08);
        }}
        h2 {{
            font-size: 1.45rem;
            margin-bottom: 1.25rem;
            color: var(--text);
            display: flex;
            align-items: center;
            gap: 0.65rem;
            font-family: Inter, 'Segoe UI', system-ui, -apple-system, sans-serif;
            font-weight: 700;
            font-style: normal;
            letter-spacing: normal;
            text-shadow: none;
            -webkit-text-stroke: 0 transparent;
            font-synthesis: none;
        }}
        .form-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 1.35rem;
        }}
        @media (max-width: 720px) {{
            .form-grid {{ grid-template-columns: 1fr; }}
        }}
        .form-group {{
            display: flex;
            flex-direction: column;
            gap: 0.45rem;
        }}
        .form-group.full {{ grid-column: 1 / -1; }}
        .login-form {{
            width: min(100%, 560px);
        }}
        label {{
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--muted);
        }}
        input, select {{
            padding: 0.95rem 1rem;
            border: 1px solid var(--border);
            border-radius: 14px;
            font-size: 1rem;
            color: var(--text);
            background: #fffdfa;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }}
        input:hover, select:hover {{ border-color: rgba(214,90,0,0.55); }}
        input:focus, select:focus {{
            outline: none;
            border-color: var(--primary-soft);
            box-shadow: 0 0 0 4px rgba(214,90,0,0.16);
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
            padding: 0.95rem 1.75rem;
            border: none;
            border-radius: 14px;
            font-size: 1rem;
            font-weight: 700;
            cursor: pointer;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }}
        .btn:hover {{ transform: translateY(-1px); }}
        .btn-primary {{
            background: linear-gradient(135deg, var(--primary-soft), var(--accent));
            color: white;
            box-shadow: 0 14px 30px rgba(214,90,0,0.22);
        }}
        .btn-secondary {{
            background: #fff7ed;
            color: var(--text);
            border: 1px solid rgba(16,42,67,0.08);
        }}
        .btn-danger {{
            background: var(--danger);
            color: white;
            padding: 0.7rem 1.1rem;
            font-size: 0.92rem;
        }}
        .btn-danger:hover {{ background: darken(var(--danger), 10%); }}
        .receipt-preview {{
            margin-top: 0.9rem;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 0.65rem;
            background: #fffaf5;
            border: 1px dashed rgba(214,90,0,0.38);
            border-radius: 16px;
            padding: 0.95rem 1rem;
        }}
        .preview-label {{
            font-size: 0.92rem;
            color: var(--muted);
        }}
        .badge-status {{
            padding: 0.35rem 0.75rem;
            border-radius: 999px;
            font-size: 0.8rem;
            font-weight: 700;
            background: rgba(231,111,81,0.12);
            color: var(--danger);
        }}
        .field-hint {{
            font-size: 0.88rem;
            color: var(--muted);
            margin-top: 0.4rem;
        }}
        .required-hint {{
            color: var(--danger);
            font-weight: 700;
            margin-top: 0.35rem;
        }}
        .file-drop-zone {{
            position: relative;
            padding: 1.2rem;
            border: 2px dashed rgba(214,90,0,0.55);
            border-radius: 18px;
            background: rgba(255,250,245,0.92);
            cursor: pointer;
            transition: border-color 0.2s ease, background 0.2s ease;
        }}
        .file-drop-zone:hover {{
            border-color: rgba(214,90,0,0.9);
            background: #fff3e5;
        }}
        .file-drop-zone.active {{
            border-color: var(--primary-soft);
            background: rgba(214,90,0,0.14);
        }}
        .file-drop-zone input[type="file"] {{
            position: absolute;
            inset: 0;
            width: 100%;
            height: 100%;
            opacity: 0;
            cursor: pointer;
        }}
        .drop-zone-content {{
            display: flex;
            gap: 1rem;
            align-items: center;
        }}
        .drop-zone-icon {{
            font-size: 1.8rem;
            min-width: 2.2rem;
        }}
        .drop-zone-title {{
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--text);
        }}
        .drop-zone-hint {{
            font-size: 0.85rem;
            color: var(--muted);
            margin-top: 0.2rem;
        }}
        .drop-zone-file {{
            font-size: 0.9rem;
            color: var(--text);
            margin-top: 0.35rem;
        }}
        .alert {{
            padding: 1rem 1.25rem;
            border-radius: 16px;
            margin-bottom: 1.5rem;
            font-weight: 600;
            box-shadow: 0 10px 24px rgba(31,78,121,0.06);
        }}
        .alert-success {{
            background: #ffedd5;
            color: var(--success);
            border: 1px solid rgba(194,65,12,0.24);
        }}
        .alert-error {{
            background: #ffe8e2;
            color: var(--danger);
            border: 1px solid rgba(231,111,81,0.18);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.95rem;
            background: #ffffff;
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 12px 28px rgba(28,25,23,0.08);
        }}
        .table-scroll {{
            max-height: 620px;
            overflow: auto;
            border: 1px solid var(--border);
            border-radius: 16px;
        }}
        .table-scroll table {{ border-radius: 0; box-shadow: none; }}
        .table-scroll th {{ position: sticky; top: 0; z-index: 1; }}
        .pagination {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            flex-wrap: wrap;
            margin-top: 1.25rem;
        }}
        .pagination-links {{ display: flex; gap: 0.6rem; flex-wrap: wrap; }}
        .pagination-links a {{ text-decoration: none; }}
        th, td {{
            padding: 1rem 1.1rem;
            text-align: left;
            border-bottom: 1px solid rgba(28,25,23,0.08);
        }}
        th {{
            background: #fff1df;
            font-weight: 700;
            color: var(--muted);
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }}
        tr:nth-child(even) td {{ background: #fffaf5; }}
        tr:hover td {{ background: #ffedd5; }}
        .badge {{
            display: inline-flex;
            align-items: center;
            padding: 0.35rem 0.8rem;
            border-radius: 999px;
            font-size: 0.8rem;
            font-weight: 600;
            background: rgba(214,90,0,0.12);
            color: var(--primary-soft);
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 1.75rem;
        }}
        .stat-card {{
            background: #fffaf5;
            border-radius: 18px;
            padding: 1.45rem;
            text-align: center;
            border: 1px solid rgba(214,90,0,0.16);
            transition: transform 0.2s ease;
        }}
        .stat-card:hover {{ transform: translateY(-2px); }}
        .stat-card .number {{
            font-size: 2.1rem;
            font-weight: 800;
            color: var(--primary);
        }}
        .stat-card .label {{
            font-size: 0.95rem;
            color: var(--muted);
            margin-top: 0.35rem;
        }}
        .empty-state {{
            text-align: center;
            padding: 3rem 1rem;
            color: var(--muted);
        }}
        .empty-state .icon {{ font-size: 3.2rem; margin-bottom: 0.9rem; }}
        footer {{
            text-align: center;
            padding: 2rem 1rem;
            color: var(--muted);
            font-size: 0.9rem;
        }}
        .name-cell {{ font-weight: 600; color: var(--text); }}
        .actions {{ white-space: nowrap; display: flex; gap: 0.5rem; flex-wrap: wrap; }}
        @media (max-width: 600px) {{
            header {{ padding: 1.5rem 1.25rem 1.25rem; }}
            .page-layout {{ padding: 0 1rem 1.5rem; margin-top: 1rem; }}
            .sidebar, .card {{ border-radius: 18px; padding: 1.25rem; }}
            .results-meta {{ margin-left: 0; width: 100%; }}
            th, td {{ padding: 0.8rem; }}
        }}
    </style>
</head>
<body>
    <header>
        <h1><span>🏃</span> Marathon Runners Depository</h1>
    </header>
    <div class="page-layout">
        <aside class="sidebar">
            <h2>Navigation</h2>
            <nav>
                <a href="/" class="{'active' if active == 'dashboard' else ''}">Public Runners</a>
                <a href="/admin" class="{'active' if active == 'admin' else ''}">Admin Panel</a>
            </nav>
            <div class="sidebar-actions">
                {('<a href="/register" class="primary-btn">Register New Runner</a>' if show_admin_actions else '')}
                {('<a href="/logout" class="secondary-btn">Log Out</a>' if show_admin_actions else '')}
            </div>
        </aside>
        <main>
            <div class="content-area">
                {content}
            </div>
        </main>
    </div>
    <footer>
        Marathon Runners Depository Application &copy; {datetime.now().year}
    </footer>
    <script>
        document.addEventListener('DOMContentLoaded', function () {{
            const dropZone = document.getElementById('receipt-drop-zone');
            const fileInput = document.getElementById('receipt');
            const fileName = document.getElementById('receipt-file-name');

            const updateFileName = () => {{
                if (fileInput.files.length > 0) {{
                    fileName.textContent = fileInput.files[0].name;
                }} else {{
                    fileName.textContent = 'No file selected yet.';
                }}
            }};

            if (dropZone && fileInput && fileName) {{
                fileInput.addEventListener('change', updateFileName);

                dropZone.addEventListener('dragover', function (event) {{
                    event.preventDefault();
                    dropZone.classList.add('active');
                }});
                dropZone.addEventListener('dragleave', function () {{
                    dropZone.classList.remove('active');
                }});
                dropZone.addEventListener('drop', function (event) {{
                    event.preventDefault();
                    dropZone.classList.remove('active');
                    if (event.dataTransfer.files.length > 0) {{
                        const dataTransfer = new DataTransfer();
                        for (const file of event.dataTransfer.files) {{
                            dataTransfer.items.add(file);
                        }}
                        fileInput.files = dataTransfer.files;
                        updateFileName();
                    }}
                }});
                updateFileName();
            }}
        }});
    </script>
</body>
</html>
"""

def render_public_runners(search_query=""):
    runners = get_filtered_runners(search_query)
    search_value = escape(search_query or "")

    search_panel = f"""
    <div class="page-intro">
        <h2>Runner directory</h2>
        <p>Browse the registered runners and celebrate the people in our community.</p>
    </div>
    <div class="card" style="margin-bottom: 0;">
        <div class="card-title-row">
            <div>
                <h2>Find a runner</h2>
                <p class="field-hint">Search by first name, middle name, or surname.</p>
            </div>
        </div>
        <form method="GET" action="/" class="filter-form">
            <div class="filter-field">
                <label for="search">Runner name</label>
                <input id="search" name="search" type="search" placeholder="e.g. Juan Dela Cruz" value="{search_value}">
            </div>
            <button type="submit" class="btn btn-primary">Search</button>
            <a href="/" class="btn btn-secondary">Clear</a>
        </form>
    </div>
    """

    if not runners:
        if search_query:
            content = f"""
            <div class="card">
                <div class="empty-state">
                    <div class="icon">🔎</div>
                    <p>No runners match that search query.</p>
                    <p style="margin-top:0.5rem;">Try a different name or clear the search.</p>
                </div>
            </div>
            """
        else:
            content = """
            <div class="card">
                <div class="empty-state">
                    <div class="icon">🏅</div>
                    <p>No runners are currently listed.</p>
                    <p style="margin-top:0.5rem;">Please check back soon for the latest roster.</p>
                </div>
            </div>
            """
        return get_base_html("Public Runners", search_panel + content, "dashboard")

    rows = ""
    for r in runners:
        full_name = f"{escape(r['first_name'])} {escape(r['middle_name'] or '')} {escape(r['surname'])}".replace("  ", " ").strip()
        years = int(r.get('years_running', 0))
        milestone = "3peat" if years >= 3 and years < 5 else "Hall of Fame" if years >= 5 else ""
        rows += f"""
            <tr>
                <td>{escape(full_name)}</td>
                <td>{escape(r.get('shirt_size', 'M'))}</td>
                <td>{years}</td>
                <td>{milestone}</td>
            </tr>
        """

    content = f"""
    {search_panel}
    <div class="card">
        <h2>🏃‍♂️ Registered Runners</h2>
        <div style="overflow-x:auto;">
            <table>
                <thead>
                    <tr>
                        <th>Full Name</th>
                        <th>Shirt Size</th>
                        <th>Years with Us</th>
                        <th>Milestone</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>
    </div>
    """
    return get_base_html("Public Runners", content, "dashboard")


def render_admin_dashboard(message=None, msg_type="success", search_query="", payment_method="All", date_from="", date_to="", page=1):
    runners = get_filtered_runners(search_query, payment_method, date_from, date_to)
    total = len(get_all_runners())
    filtered_total = len(runners)
    page_size = 25
    total_pages = max(1, (filtered_total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    page_start = (page - 1) * page_size
    page_runners = runners[page_start:page_start + page_size]
    cashless = sum(1 for r in runners if r["payment_mode"].lower() == "cashless")
    pending = sum(1 for r in runners if r.get("receipt_verified", "Pending") == "Pending")

    alert = ""
    if message:
        alert = f'<div class="alert alert-{msg_type}">{escape(message)}</div>'

    stats = f"""
    <div class="stats">
        <div class="stat-card">
            <div class="number">{total}</div>
            <div class="label">Total Runners</div>
        </div>
        <div class="stat-card">
            <div class="number">{cashless}</div>
            <div class="label">Cashless Payments</div>
        </div>
        <div class="stat-card">
            <div class="number">{total - cashless}</div>
            <div class="label">Other Payments</div>
        </div>
        <div class="stat-card">
            <div class="number">{pending}</div>
            <div class="label">Pending Receipts</div>
        </div>
    </div>
    """

    search_value = escape(search_query or "")
    payment_method_value = escape(payment_method or "All")
    date_from_value = escape(date_from or "")
    date_to_value = escape(date_to or "")
    pagination_params = urllib.parse.urlencode({
        "search": search_query,
        "payment_method": payment_method,
        "date_from": date_from,
        "date_to": date_to,
    })
    search_form = f"""
    <div class="card" style="margin-bottom: 1.5rem;">
        <form method="GET" action="/admin" style="display:grid; gap:0.75rem;">
            <div style="display:flex; flex-wrap:wrap; gap:0.75rem; align-items:flex-end;">
                <div style="flex:1; min-width:220px;">
                    <label for="search" style="font-weight:600; display:block; margin-bottom:0.35rem;">Search runner name</label>
                    <input id="search" name="search" type="search" placeholder="Search by first, middle, or surname" value="{search_value}" style="width:100%; padding:0.75rem; border:1px solid #ccc; border-radius:6px;">
                </div>
                <div style="flex:1; min-width:220px;">
                    <label for="payment_method" style="font-weight:600; display:block; margin-bottom:0.35rem;">Payment method</label>
                    <select id="payment_method" name="payment_method" style="width:100%; padding:0.75rem; border:1px solid #ccc; border-radius:6px;">
                        <option value="All" {"selected" if payment_method_value == "All" else ""}>All</option>
                        <option value="Cashless" {"selected" if payment_method_value == "Cashless" else ""}>Cashless</option>
                        <option value="Bank Transfer" {"selected" if payment_method_value == "Bank Transfer" else ""}>Bank Transfer</option>
                        <option value="GCash" {"selected" if payment_method_value == "GCash" else ""}>GCash</option>
                        <option value="Maya" {"selected" if payment_method_value == "Maya" else ""}>Maya</option>
                        <option value="Credit/Debit Card" {"selected" if payment_method_value == "Credit/Debit Card" else ""}>Credit/Debit Card</option>
                        <option value="Other Cashless" {"selected" if payment_method_value == "Other Cashless" else ""}>Other Cashless</option>
                    </select>
                </div>
                <div style="display:grid; gap:0.75rem; min-width:220px;">
                    <div>
                        <label for="date_from" style="font-weight:600; display:block; margin-bottom:0.35rem;">From date</label>
                        <input type="date" id="date_from" name="date_from" value="{date_from_value}" style="width:100%; padding:0.75rem; border:1px solid #ccc; border-radius:6px;">
                    </div>
                    <div>
                        <label for="date_to" style="font-weight:600; display:block; margin-bottom:0.35rem;">To date</label>
                        <input type="date" id="date_to" name="date_to" value="{date_to_value}" style="width:100%; padding:0.75rem; border:1px solid #ccc; border-radius:6px;">
                    </div>
                </div>
            </div>
            <div style="display:flex; gap:0.75rem; flex-wrap:wrap; align-items:center;">
                <button type="submit" class="btn btn-primary">Apply filters</button>
                <a href="/admin" class="btn btn-secondary">Clear</a>
                <a href="/backup" class="btn btn-secondary">Backup Now</a>
                <div style="margin-left:auto; min-width:220px; text-align:right; color:#444;">Showing {filtered_total} of {total} runners</div>
            </div>
        </form>
    </div>
    """
    if message:
        alert = f'<div class="alert alert-{msg_type}">{escape(message)}</div>'

    if not runners:
        table = """
        <div class="card">
            <div class="empty-state">
                <div class="icon">📋</div>
                <p>No runners match that search query.</p>
                <p style="margin-top:0.5rem;"><a href="/admin" style="color:var(--primary);">Clear search and view all runners →</a></p>
            </div>
        </div>
        """
    else:
        rows = ""
        for r in page_runners:
            full_name = f"{escape(r['first_name'])} {escape(r['middle_name'] or '')} {escape(r['surname'])}".replace("  ", " ").strip()
            rows += f"""
            <tr>
                <td>{r['id']}</td>
                <td class="name-cell">{full_name}</td>
                <td>{escape(r.get('shirt_size', 'M'))}</td>
                <td>{escape(str(r.get('years_running', 0)))}</td>
                <td><span class="badge">{escape(r['payment_mode'])}</span></td>
                <td>{escape(r['payment_date'])}</td>
                <td>{escape(r['email'])}</td>
                <td>{escape(r['contact_number'])}</td>
                <td>{escape(r.get('receipt_verified', 'Pending'))}</td>
                <td class="actions">
                    <a href="/edit?id={r['id']}" class="btn btn-secondary">Edit</a>
                    <form method="POST" action="/delete" style="display:inline;" onsubmit="return confirm('Delete this runner?');">
                        <input type="hidden" name="id" value="{r['id']}">
                        <button type="submit" class="btn btn-danger">Delete</button>
                    </form>
                </td>
            </tr>
            """
        range_start = page_start + 1
        range_end = min(page_start + page_size, filtered_total)
        pagination = f"""
            <div class="pagination">
                <span class="field-hint">Showing registrations {range_start}&ndash;{range_end} of {filtered_total} &middot; Page {page} of {total_pages}</span>
                <div class="pagination-links">
                    {f'<a class="btn btn-secondary" href="/admin?{pagination_params}&page={page - 1}">Previous</a>' if page > 1 else ''}
                    {f'<a class="btn btn-secondary" href="/admin?{pagination_params}&page={page + 1}">Next</a>' if page < total_pages else ''}
                </div>
            </div>
        """ if total_pages > 1 else f'<div class="pagination"><span class="field-hint">Showing all {filtered_total} registrations</span></div>'
        table = f"""
        <div class="card" style="margin-bottom: 1.5rem;">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;">
                <div>
                    <h2>📋 Admin Runner Management</h2>
                    <p class="field-hint">Manage runner records, receipts, and milestones from the admin panel.</p>
                </div>
                <a href="/register" class="btn btn-primary">Register New Runner</a>
            </div>
        </div>
        <div class="card">
            <h2>📋 Admin Runner Management</h2>
            <div class="table-scroll">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Full Name</th>
                            <th>Shirt Size</th>
                            <th>Years</th>
                            <th>Payment Mode</th>
                            <th>Payment Date</th>
                            <th>Email</th>
                            <th>Contact</th>
                            <th>Verification</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                    </tbody>
                </table>
            </div>
            {pagination}
        </div>
        """

    intro = """
    <div class="page-intro">
        <h2>Runner management</h2>
        <p>Review registrations, verify payments, and keep the event roster up to date.</p>
    </div>
    """
    content = alert + intro + stats + search_form + table
    return get_base_html("Admin Panel", content, "admin", show_admin_actions=True)

def render_runner_form(title, form_data=None, message=None, msg_type="success", submit_label="Register Runner", runner_id=None, active="register"):
    form_data = form_data or {}
    alert = ""
    if message:
        alert = f'<div class="alert alert-{msg_type}">{escape(message)}</div>'

    today = datetime.now().strftime("%Y-%m-%d")
    receipt_filename = form_data.get("receipt_filename", "")
    receipt_verified = form_data.get("receipt_verified", "Pending")
    receipt_scan_message = form_data.get("receipt_scan_message", "")
    receipt_required = "required" if not receipt_filename else ""
    receipt_preview = ""
    if receipt_filename:
        is_remote_receipt = receipt_filename.startswith(("https://", "http://"))
        safe_name = escape(receipt_filename)
        receipt_url = safe_name if is_remote_receipt else f"/uploads/{safe_name}"
        receipt_label = escape(os.path.basename(urllib.parse.urlparse(receipt_filename).path))
        scan_note = f"<p class=\"field-hint\">{escape(receipt_scan_message)}</p>" if receipt_scan_message else ""
        receipt_preview = f"""
            <div class=\"receipt-preview\">
                <span class=\"preview-label\">Receipt Uploaded:</span>
                <a href=\"{receipt_url}\" target=\"_blank\" rel=\"noopener\">{receipt_label}</a>
                <span class=\"badge badge-status\">{escape(receipt_verified)}</span>
                {scan_note}
            </div>
        """

    contact_pattern = r'^(09[0-9]{9}|\+639[0-9]{9})$'

    content = f"""
    {alert}
    <div class="card">
        <h2>➕ {escape(title)}</h2>
        <form method="POST" action="{ '/edit' if runner_id else '/register' }" enctype="multipart/form-data">
            {f'<input type="hidden" name="id" value="{runner_id}">' if runner_id else ''}
            <div class="form-grid">
                <div class="form-group">
                    <label for="first_name">First Name *</label>
                    <input type="text" id="first_name" name="first_name" required
                           value="{escape(form_data.get('first_name', ''))}"
                           placeholder="e.g. Juan">
                </div>
                <div class="form-group">
                    <label for="middle_name">Middle Name</label>
                    <input type="text" id="middle_name" name="middle_name"
                           value="{escape(form_data.get('middle_name', ''))}"
                           placeholder="e.g. Santos">
                </div>
                <div class="form-group">
                    <label for="surname">Surname *</label>
                    <input type="text" id="surname" name="surname" required
                           value="{escape(form_data.get('surname', ''))}"
                           placeholder="e.g. Dela Cruz">
                </div>
                <div class="form-group">
                    <label for="payment_mode">Payment Mode *</label>
                    <select id="payment_mode" name="payment_mode" required>
                        <option value="Cashless" {"selected" if form_data.get("payment_mode", "Cashless") == "Cashless" else ""}>Cashless</option>
                        <option value="Bank Transfer" {"selected" if form_data.get("payment_mode") == "Bank Transfer" else ""}>Bank Transfer</option>
                        <option value="GCash" {"selected" if form_data.get("payment_mode") == "GCash" else ""}>GCash</option>
                        <option value="Maya" {"selected" if form_data.get("payment_mode") == "Maya" else ""}>Maya</option>
                        <option value="Credit/Debit Card" {"selected" if form_data.get("payment_mode") == "Credit/Debit Card" else ""}>Credit/Debit Card</option>
                        <option value="Other Cashless" {"selected" if form_data.get("payment_mode") == "Other Cashless" else ""}>Other Cashless</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="payment_date">Date of Payment *</label>
                    <input type="date" id="payment_date" name="payment_date" required
                           value="{escape(form_data.get('payment_date', today))}">
                </div>
                <div class="form-group">
                    <label for="shirt_size">Shirt Size *</label>
                    <select id="shirt_size" name="shirt_size" required>
                        <option value="XS" {"selected" if form_data.get("shirt_size", "M") == "XS" else ""}>XS</option>
                        <option value="S" {"selected" if form_data.get("shirt_size", "M") == "S" else ""}>S</option>
                        <option value="M" {"selected" if form_data.get("shirt_size", "M") == "M" else ""}>M</option>
                        <option value="L" {"selected" if form_data.get("shirt_size", "M") == "L" else ""}>L</option>
                        <option value="XL" {"selected" if form_data.get("shirt_size", "M") == "XL" else ""}>XL</option>
                        <option value="XXL" {"selected" if form_data.get("shirt_size", "M") == "XXL" else ""}>XXL</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="years_running">Years with Organization *</label>
                    <input type="number" id="years_running" name="years_running" required min="0" max="50"
                           value="{escape(str(form_data.get('years_running', 0)))}"
                           placeholder="e.g. 3">
                </div>
                <div class="form-group">
                    <label for="email">Email Address *</label>
                    <input type="email" id="email" name="email" required
                           value="{escape(form_data.get('email', ''))}"
                           placeholder="e.g. juan@gmail.com">
                </div>
                    <div class="form-group full">
                    <label for="contact_number">Contact Number *</label>
                    <input type="tel" id="contact_number" name="contact_number" required
                           pattern="{contact_pattern}"
                           inputmode="tel"
                           maxlength="13"
                           value="{escape(form_data.get('contact_number', ''))}"
                           placeholder="09171234567 or +639171234567"
                           title="Enter a Philippine mobile number in 0917... or +63917... format">
                    <p class="field-hint">Enter 11 digits in local format or +63 followed by 10 digits. The form saves it as +63 format.</p>
                </div>
                <div class="form-group full">
                    <label for="receipt">Proof of Payment *</label>
                    <div class="file-drop-zone" id="receipt-drop-zone">
                        <input type="file" id="receipt" name="receipt" accept="image/*" {receipt_required}>
                        <div class="drop-zone-content">
                            <div class="drop-zone-icon">📎</div>
                            <div>
                                <div class="drop-zone-title">Drag & drop your receipt image here, or click to browse</div>
                                <div class="drop-zone-hint">Accepted formats: JPG, PNG, GIF, BMP</div>
                                <div class="drop-zone-file" id="receipt-file-name">{escape(receipt_filename) if receipt_filename else 'No file selected yet.'}</div>
                            </div>
                        </div>
                    </div>
                    <p class="field-hint required-hint">This proof is required for registration and cannot be submitted without a receipt image.</p>
                    <p class="field-hint">Upload a screenshot or receipt image for GCash, Maya, bank transfer, or other online payment.</p>
                    {receipt_preview}
                </div>
            </div>
            <div style="margin-top: 1.5rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center;">
                <button type="submit" class="btn btn-primary">{escape(submit_label)}</button>
                <a href="{'/admin' if active == 'admin' else '/'}" class="btn btn-secondary">Back</a>
            </div>
        </form>
    </div>
    """
    return get_base_html(title, content, active, show_admin_actions=True)


def render_login(message=None, msg_type="success"):
    alert = ''
    if message:
        alert = f'<div class="alert alert-{msg_type}">{escape(message)}</div>'
    content = f"""
    {alert}
    <div class="card">
        <h2>🔐 Admin Login</h2>
        <form method="POST" action="/login" class="login-form">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required placeholder="admin">
            </div>
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required placeholder="••••••••">
            </div>
            <div style="margin-top:1.2rem; display:flex; gap:1rem; flex-wrap:wrap; align-items:center;">
                <button type="submit" class="btn btn-primary">Sign In</button>
            </div>
        </form>
    </div>
    """
    return get_base_html("Admin Login", content, "dashboard", show_admin_actions=False)


def render_register(message=None, msg_type="success", form_data=None):
    return render_runner_form("Register New Runner", form_data, message, msg_type, "Register Runner", None, "admin")


def render_edit(runner, message=None, msg_type="success"):
    if not runner:
        return get_base_html("Runner Not Found", "<div class=\"card\"><p>Runner record not found.</p></div>")
    return render_runner_form(
        "Edit Runner Details",
        runner,
        message,
        msg_type,
        "Save Changes",
        runner.get("id"),
        "admin"
    )

class RequestHandler(http.server.BaseHTTPRequestHandler):
    def get_session_id(self):
        cookie_header = self.headers.get("Cookie")
        cookies = parse_cookies(cookie_header)
        return cookies.get(SESSION_COOKIE_NAME)

    def is_admin_authenticated(self):
        return verify_admin_session(self.get_session_id())

    def redirect_to_login(self):
        self.send_response(303)
        self.send_header("Location", "/login")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/login":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_login().encode("utf-8"))
            return

        if path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", f"{SESSION_COOKIE_NAME}=deleted; Max-Age=0; Path=/")
            self.end_headers()
            return

        if path in ["/admin", "/register", "/edit"] and not self.is_admin_authenticated():
            self.redirect_to_login()
            return

        if path == "/" or path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_public_runners(query.get("search", [""])[0]).encode("utf-8"))
        elif path == "/register":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_register().encode("utf-8"))
        elif path == "/backup":
            if not self.is_admin_authenticated():
                self.redirect_to_login()
                return
            _, backup_message = backup_all_runners(silent=True)
            quote_msg = urllib.parse.quote_plus(backup_message)
            self.send_response(303)
            self.send_header("Location", f"/admin?backup=1&backup_msg={quote_msg}")
            self.end_headers()
            return
        elif path == "/admin":
            message = None
            msg_type = "success"
            if query.get("success") == ["1"]:
                message = "Runner registered successfully."
            elif query.get("updated") == ["1"]:
                message = "Runner details updated successfully."
            elif query.get("backup") == ["1"]:
                message = query.get("backup_msg", ["Backup completed."])[0]
            elif query.get("backup") == ["error"]:
                message = query.get("backup_msg", ["Backup failed."])[0]
                msg_type = "error"
            search_query = query.get("search", [""])[0]
            payment_method = query.get("payment_method", ["All"])[0]
            date_from = query.get("date_from", [""])[0]
            date_to = query.get("date_to", [""])[0]
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
            except ValueError:
                page = 1
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_admin_dashboard(message, msg_type, search_query, payment_method, date_from, date_to, page).encode("utf-8"))
        elif path == "/edit":
            runner_id = query.get("id", [None])[0]
            runner = get_runner_by_id(int(runner_id)) if runner_id and runner_id.isdigit() else None
            self.send_response(200 if runner else 404)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            if runner:
                self.wfile.write(render_edit(runner).encode("utf-8"))
            else:
                self.wfile.write(get_base_html("Runner Not Found", "<div class=\"card\"><p>Runner record not found.</p></div>").encode("utf-8"))
        elif path == "/api/runners":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            runners = get_all_runners()
            self.wfile.write(json.dumps(runners, indent=2).encode("utf-8"))
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif path.startswith("/uploads/"):
            file_name = os.path.basename(path.replace("/uploads/", ""))
            file_path = os.path.join(UPLOAD_DIR, file_name)
            if os.path.isfile(file_path):
                self.send_response(200)
                content_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
                self.send_header("Content-type", content_type)
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "File Not Found")
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        content_type = self.headers.get("Content-Type", "")
        fields = {}
        files = {}
        if content_type.startswith("multipart/form-data"):
            boundary_match = re.search(r'boundary="?([^";]+)"?', content_type)
            if boundary_match:
                boundary = boundary_match.group(1).encode("utf-8")
                fields, files = parse_multipart(body, boundary)
        else:
            params = urllib.parse.parse_qs(body.decode("utf-8", "ignore"))
            fields = {k: v[0] if v else "" for k, v in params.items()}

        data = {}
        for field in ["id", "first_name", "middle_name", "surname", "payment_mode", "payment_date", "email", "contact_number", "shirt_size", "years_running", "receipt_verified", "username", "password"]:
            if field in fields:
                data[field] = fields[field]

        receipt_field = files.get("receipt")

        if self.path in ["/register", "/edit", "/delete"] and not self.is_admin_authenticated():
            self.redirect_to_login()
            return

        if self.path.startswith("/login"):
            username = data.get("username", "")
            password = data.get("password", "")
            if admin_authenticate(username, password):
                session_id = create_admin_session()
                self.send_response(303)
                self.send_header("Location", "/admin")
                self.send_header("Set-Cookie", f"{SESSION_COOKIE_NAME}={session_id}; Path=/; HttpOnly")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(render_login("Invalid username or password.", "error").encode("utf-8"))
            return

        if self.path == "/register" or self.path == "/edit":
            required = ["first_name", "surname", "payment_mode", "payment_date", "email", "contact_number", "shirt_size", "years_running"]
            missing = [f for f in required if not data.get(f, "").strip()]
            if missing:
                html = render_runner_form(
                    "Edit Runner Details" if self.path == "/edit" else "Register New Runner",
                    data,
                    message=f"Missing required fields: {', '.join(missing)}",
                    msg_type="error",
                    submit_label="Save Changes" if self.path == "/edit" else "Register Runner",
                    runner_id=data.get("id") if self.path == "/edit" else None,
                    active="admin"
                )
                self.send_response(400)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            missing_receipt = False
            if self.path == "/register":
                if not receipt_field or not receipt_field.get("filename"):
                    missing_receipt = True
            elif self.path == "/edit" and data.get("id"):
                existing = get_runner_by_id(int(data.get("id")))
                if existing and not existing.get("receipt_filename") and (not receipt_field or not receipt_field.get("filename")):
                    missing_receipt = True

            if missing_receipt:
                html = render_runner_form(
                    "Edit Runner Details" if self.path == "/edit" else "Register New Runner",
                    data,
                    message="Proof of payment is required before submitting the form.",
                    msg_type="error",
                    submit_label="Save Changes" if self.path == "/edit" else "Register Runner",
                    runner_id=data.get("id") if self.path == "/edit" else None,
                    active="admin"
                )
                self.send_response(400)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            if not is_valid_email(data["email"]):
                html = render_runner_form(
                    "Edit Runner Details" if self.path == "/edit" else "Register New Runner",
                    data,
                    message="Please provide a valid email address in the format juan@gmail.com.",
                    msg_type="error",
                    submit_label="Save Changes" if self.path == "/edit" else "Register Runner",
                    runner_id=data.get("id") if self.path == "/edit" else None,
                    active="admin"
                )
                self.send_response(400)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            normalized_phone = normalize_ph_number(data["contact_number"])
            if not normalized_phone:
                html = render_runner_form(
                    "Edit Runner Details" if self.path == "/edit" else "Register New Runner",
                    data,
                    message="Contact number must be a valid Philippine mobile number, for example +639171234567.",
                    msg_type="error",
                    submit_label="Save Changes" if self.path == "/edit" else "Register Runner",
                    runner_id=data.get("id") if self.path == "/edit" else None,
                    active="admin"
                )
                self.send_response(400)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return
            data["contact_number"] = normalized_phone

            if receipt_field and receipt_field.get("filename"):
                receipt_filename, receipt_error = save_receipt_file(receipt_field)
                if receipt_error:
                    html = render_runner_form(
                        "Edit Runner Details" if self.path == "/edit" else "Register New Runner",
                        data,
                        message=receipt_error,
                        msg_type="error",
                        submit_label="Save Changes" if self.path == "/edit" else "Register Runner",
                        runner_id=data.get("id") if self.path == "/edit" else None,
                        active="admin"
                    )
                    self.send_response(400)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return
                receipt_path = os.path.join(UPLOAD_DIR, receipt_filename)
                status, scan_message = scan_receipt_image(receipt_path, receipt_filename, data.get("payment_mode", ""))
                if status == "Invalid":
                    os.remove(receipt_path)
                    html = render_runner_form(
                        "Edit Runner Details" if self.path == "/edit" else "Register New Runner",
                        data,
                        message=scan_message,
                        msg_type="error",
                        submit_label="Save Changes" if self.path == "/edit" else "Register Runner",
                        runner_id=data.get("id") if self.path == "/edit" else None,
                        active="admin"
                    )
                    self.send_response(400)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return
                stored_receipt, storage_error = persist_receipt_file(receipt_filename)
                if storage_error:
                    html = render_runner_form(
                        "Edit Runner Details" if self.path == "/edit" else "Register New Runner",
                        data,
                        message=storage_error,
                        msg_type="error",
                        submit_label="Save Changes" if self.path == "/edit" else "Register Runner",
                        runner_id=data.get("id") if self.path == "/edit" else None,
                        active="admin"
                    )
                    self.send_response(500)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return
                data["receipt_filename"] = stored_receipt
                data["receipt_verified"] = status
                data["receipt_scan_message"] = scan_message
            elif self.path == "/edit" and data.get("id"):
                existing = get_runner_by_id(int(data.get("id")))
                if existing:
                    data["receipt_filename"] = existing.get("receipt_filename")
                    data["receipt_verified"] = existing.get("receipt_verified", "Pending")
            else:
                data.setdefault("receipt_verified", "Pending")

            try:
                if self.path == "/register":
                    runner_id = add_runner(data)
                    self.send_response(303)
                    self.send_header("Location", f"/admin?success=1&id={runner_id}")
                    self.end_headers()
                    return
                if self.path == "/edit":
                    runner_id = data.get("id")
                    update_runner(int(runner_id), data)
                    self.send_response(303)
                    self.send_header("Location", "/admin?updated=1")
                    self.end_headers()
                    return
            except Exception as e:
                html = render_runner_form(
                    "Edit Runner Details" if self.path == "/edit" else "Register New Runner",
                    data,
                    message=f"Error saving runner: {str(e)}",
                    msg_type="error",
                    submit_label="Save Changes" if self.path == "/edit" else "Register Runner",
                    runner_id=data.get("id") if self.path == "/edit" else None,
                    active="admin"
                )
                self.send_response(500)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

        elif self.path == "/delete":
            post_data = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
            params = urllib.parse.parse_qs(post_data)
            runner_id = params.get("id", [None])[0]
            if runner_id:
                try:
                    delete_runner(int(runner_id))
                except Exception:
                    pass
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {args[0]}")

def main():
    init_db()
    server_port = None
    httpd = None
    for candidate in range(PORT, PORT + 10):
        try:
            httpd = socketserver.ThreadingTCPServer(("", candidate), RequestHandler)
            server_port = candidate
            break
        except OSError as e:
            if getattr(e, 'errno', None) in (98, 10048):
                continue
            raise
    if httpd is None:
        raise RuntimeError(f"Unable to bind to any port in range {PORT}-{PORT + 9}")

    # Windows consoles may default to cp1252, which cannot render the banner.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"""
╔══════════════════════════════════════════════════════════╗
║     Marathon Runners Depository Application              ║
║                                                          ║
║  Server running at:  http://localhost:{server_port}               ║
║                                                          ║
║  Endpoints:                                              ║
║    /           → Dashboard                               ║
║    /register   → Register new runner                     ║
║    /api/runners→ JSON API of all runners                 ║
║                                                          ║
║  Press Ctrl+C to stop the server                         ║
╚══════════════════════════════════════════════════════════╝
""")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.shutdown()
        httpd.server_close()

if __name__ == "__main__":
    main()
