"""
JuiceFront - Fresh Juice Delivered
Airbnb-style marketplace for fresh juice in Nansana, Uganda.

Roles:
  - public:   browse vendors, place orders (no login)
  - vendor:   manage own profile, juices, and view own orders
  - operator: full admin access - all orders, statuses, revenue

Persistence:
  - Structured data (vendors, juices, users, orders, login attempts) -> Render PostgreSQL
  - Vendor photos -> Cloudinary (URLs stored in the vendors.photo column)
"""
import os
import secrets
import string
import time
import uuid
from datetime import date
from functools import wraps

import cloudinary
import cloudinary.uploader
import africastalking
import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from dotenv import load_dotenv
from flask import (Flask, g, render_template, request, redirect, url_for,
                    session, flash, abort)
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

# ---------- Config ----------
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB
SERVICE_FEE = int(os.getenv("SERVICE_FEE", "200"))  # UGX

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set!")

app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# ---------- Cloudinary Config ----------
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

# ---------- Africa's Talking (SMS) Config ----------
AT_USERNAME = os.getenv("AT_USERNAME")
AT_API_KEY = os.getenv("AT_API_KEY")
AT_SENDER_ID = os.getenv("AT_SENDER_ID")  # optional; leave unset to use the AT sandbox/shared shortcode
OPERATOR_PHONE = os.getenv("OPERATOR_PHONE", "")  # e.g. 0700000000 -- gets pinged on every new order

sms_service = None
if AT_USERNAME and AT_API_KEY:
    try:
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        sms_service = africastalking.SMS
    except Exception as e:
        app.logger.error(f"Africa's Talking init failed: {e}")
else:
    app.logger.warning("AT_USERNAME/AT_API_KEY not set — SMS notifications are disabled.")


def normalize_ug_phone(raw_phone):
    """Best-effort conversion of a Ugandan number to the +256E.164 format AT requires."""
    digits = "".join(ch for ch in raw_phone if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        return digits
    if digits.startswith("256"):
        return "+" + digits
    if digits.startswith("0"):
        return "+256" + digits[1:]
    if digits:
        return "+256" + digits
    return ""


def send_sms(raw_phone, message):
    """Fire-and-forget SMS. Never raises -- a notification failure must never
    break order placement, since the order is already safely in PostgreSQL."""
    if not sms_service:
        return False
    recipient = normalize_ug_phone(raw_phone or "")
    if not recipient:
        return False
    try:
        kwargs = {}
        if AT_SENDER_ID:
            kwargs["sender_id"] = AT_SENDER_ID
        sms_service.send(message, [recipient], **kwargs)
        return True
    except Exception as e:
        app.logger.error(f"SMS to {recipient} failed: {e}")
        return False


def upload_to_cloudinary(file_storage):
    """Uploads a Werkzeug FileStorage object to Cloudinary, returns secure_url or None."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_EXT:
        return None
    try:
        result = cloudinary.uploader.upload(
            file_storage,
            folder="juicefront/vendors",
            public_id=f"vendor_{uuid.uuid4().hex[:12]}",
            overwrite=True,
        )
        return result.get("secure_url")
    except Exception as e:
        app.logger.error(f"Cloudinary upload failed: {e}")
        return None


def generate_password(length=10):
    """Random, readable-enough password for newly-created vendor logins."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def slugify_username(name):
    base = "".join(ch.lower() if ch.isalnum() else "" for ch in name) or "vendor"
    return base[:20]


# ---------- Database ----------
def get_db():
    if "db" not in g:
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS vendors (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        photo TEXT DEFAULT '',
        phone TEXT DEFAULT ''
    );
    """)
    # Migration for existing DBs that predate the active/inactive toggle.
    cur.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE;")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS juices (
        id SERIAL PRIMARY KEY,
        vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        active BOOLEAN DEFAULT TRUE
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('operator', 'vendor')),
        vendor_id INTEGER REFERENCES vendors(id) ON DELETE SET NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        vendor_id INTEGER NOT NULL REFERENCES vendors(id),
        vendor_name TEXT NOT NULL,
        juice_id INTEGER REFERENCES juices(id),
        juice_name TEXT NOT NULL,
        juice_price INTEGER NOT NULL,
        service_fee INTEGER NOT NULL,
        total INTEGER NOT NULL,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        customer_location TEXT NOT NULL,
        note TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'Pending',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS login_attempts (
        ip TEXT PRIMARY KEY,
        fails INTEGER DEFAULT 0,
        locked_until BIGINT DEFAULT 0
    );
    """)
    conn.commit()

    # Seed vendors + juices only if empty. Kept to just the two real vendors in
    # use today -- everyone else should be added from Operator -> Manage
    # Vendors, not from code.
    cur.execute("SELECT COUNT(*) AS count FROM vendors")
    if cur.fetchone()["count"] == 0:
        seed = [
            ("Mama Pauline", "Fresh tropical blends from Nansana market.", "0700000001",
             [("Mango Passion", 2000), ("Pineapple Ginger", 2500), ("Watermelon Mint", 2000)]),
            ("Mama Hailey", "100% organic, no added sugar.", "0700000002",
             [("Avocado Smoothie", 4000), ("Beetroot Boost", 3500), ("Green Detox", 3500)]),
        ]
        for name, desc, phone, juices in seed:
            cur.execute(
                "INSERT INTO vendors (name, description, phone, active) VALUES (%s,%s,%s,TRUE) RETURNING id",
                (name, desc, phone))
            vid = cur.fetchone()["id"]
            for jn, jp in juices:
                cur.execute(
                    "INSERT INTO juices (vendor_id, name, price) VALUES (%s,%s,%s)",
                    (vid, jn, jp))

    # Seed users
    cur.execute("SELECT COUNT(*) AS count FROM users")
    if cur.fetchone()["count"] == 0:
        op_pw = os.getenv("OPERATOR_PASSWORD", "operator123")
        v_pw = os.getenv("VENDOR_DEFAULT_PASSWORD", "vendor123")
        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s)",
            ("operator", generate_password_hash(op_pw), "operator"))
        cur.execute("SELECT id FROM vendors ORDER BY id")
        for row in cur.fetchall():
            cur.execute(
                "INSERT INTO users (username, password_hash, role, vendor_id) VALUES (%s,%s,%s,%s)",
                (f"vendor{row['id']}", generate_password_hash(v_pw), "vendor", row["id"]))

    conn.commit()
    cur.close()
    conn.close()


# ---------- Auth helpers ----------
LOCKOUT_SECONDS = 15 * 60
MAX_FAILS = 3


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    cur = get_db().cursor()
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    return cur.fetchone()


def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("login", next=request.path))
            if role and u["role"] != role:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def check_lockout(ip):
    cur = get_db().cursor()
    cur.execute("SELECT * FROM login_attempts WHERE ip=%s", (ip,))
    row = cur.fetchone()
    if row and row["locked_until"] > time.time():
        return int(row["locked_until"] - time.time())
    return 0


def register_fail(ip):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM login_attempts WHERE ip=%s", (ip,))
    row = cur.fetchone()
    fails = (row["fails"] if row else 0) + 1
    locked = int(time.time() + LOCKOUT_SECONDS) if fails >= MAX_FAILS else 0
    if row:
        cur.execute("UPDATE login_attempts SET fails=%s, locked_until=%s WHERE ip=%s",
                    (fails, locked, ip))
    else:
        cur.execute("INSERT INTO login_attempts (ip, fails, locked_until) VALUES (%s,%s,%s)",
                    (ip, fails, locked))
    db.commit()
    return fails


def clear_fails(ip):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM login_attempts WHERE ip=%s", (ip,))
    db.commit()


# ---------- Template globals ----------
@app.context_processor
def inject_globals():
    return dict(user=current_user(), SERVICE_FEE=SERVICE_FEE, APP_NAME="JuiceFront")


# ---------- Public routes ----------
@app.route("/")
def index():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM vendors WHERE active=TRUE ORDER BY id")
    vendors = cur.fetchall()

    cards = []
    for v in vendors:
        cur.execute(
            "SELECT * FROM juices WHERE vendor_id=%s AND active=TRUE ORDER BY price LIMIT 1",
            (v["id"],))
        j = cur.fetchone()
        cards.append({"v": v, "j": j})
    return render_template("index.html", cards=cards)


@app.route("/vendor/<int:vid>")
def vendor_detail(vid):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM vendors WHERE id=%s AND active=TRUE", (vid,))
    v = cur.fetchone()
    if not v:
        abort(404)
    cur.execute(
        "SELECT * FROM juices WHERE vendor_id=%s AND active=TRUE ORDER BY name", (vid,))
    juices = cur.fetchall()
    return render_template("vendor_detail.html", v=v, juices=juices)


@app.route("/order/<int:juice_id>", methods=["GET", "POST"])
def order_form(juice_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM juices WHERE id=%s", (juice_id,))
    j = cur.fetchone()
    if not j:
        abort(404)
    cur.execute("SELECT * FROM vendors WHERE id=%s", (j["vendor_id"],))
    v = cur.fetchone()

    if request.method == "POST":
        name = request.form.get("customer_name", "").strip()[:100]
        phone = request.form.get("customer_phone", "").strip()[:30]
        loc = request.form.get("customer_location", "").strip()[:200]
        note = request.form.get("note", "").strip()[:500]
        if not (name and phone and loc and note):
            flash("Please fill in all fields.", "error")
            return render_template("order_form.html", j=j, v=v)
        total = j["price"] + SERVICE_FEE
        cur.execute("""INSERT INTO orders
            (vendor_id, vendor_name, juice_id, juice_name, juice_price,
             service_fee, total, customer_name, customer_phone, customer_location,
             note, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id""",
            (v["id"], v["name"], j["id"], j["name"], j["price"],
             SERVICE_FEE, total, name, phone, loc, note, "Pending"))
        order_id = cur.fetchone()["id"]
        db.commit()

        # Notify by SMS. This runs after the commit above, so a notification
        # failure (bad number, AT outage, no credit) never loses the order --
        # it's already safely in PostgreSQL either way.
        order_msg = (
            f"JuiceFront - New Order #{order_id}\n"
            f"Vendor: {v['name']}\n"
            f"Juice: {j['name']}\n"
            f"Qty/details: {note}\n"
            f"Customer: {name} ({phone})\n"
            f"Location: {loc}\n"
            f"Total: UGX {total:,}"
        )
        send_sms(v["phone"], order_msg)
        if OPERATOR_PHONE:
            send_sms(OPERATOR_PHONE, order_msg)

        return redirect(url_for("success", order_id=order_id))
    return render_template("order_form.html", j=j, v=v)


@app.route("/success/<int:order_id>")
def success(order_id):
    cur = get_db().cursor()
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    o = cur.fetchone()
    if not o:
        abort(404)
    return render_template("success.html", o=o)


# ---------- Auth routes ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "unknown"
    wait = check_lockout(ip)
    if request.method == "POST":
        if wait > 0:
            flash(f"Too many attempts. Try again in {wait // 60}m {wait % 60}s.", "error")
            return render_template("login.html", locked=wait)
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        cur = get_db().cursor()
        cur.execute("SELECT * FROM users WHERE username=%s", (u,))
        row = cur.fetchone()
        if row and check_password_hash(row["password_hash"], p):
            session["user_id"] = row["id"]
            clear_fails(ip)
            if row["role"] == "operator":
                return redirect(url_for("operator_dashboard"))
            return redirect(url_for("vendor_dashboard"))
        fails = register_fail(ip)
        remaining = MAX_FAILS - fails
        if remaining > 0:
            flash(f"Invalid credentials. {remaining} attempt(s) left.", "error")
        else:
            flash("Locked out for 15 minutes.", "error")
    return render_template("login.html", locked=wait)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- Vendor dashboard ----------
@app.route("/vendor", methods=["GET", "POST"])
@login_required(role="vendor")
def vendor_dashboard():
    db = get_db()
    cur = db.cursor()
    u = current_user()
    cur.execute("SELECT * FROM vendors WHERE id=%s", (u["vendor_id"],))
    v = cur.fetchone()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            name = request.form.get("name", "").strip()[:100] or v["name"]
            desc = request.form.get("description", "").strip()[:500]
            phone = request.form.get("phone", "").strip()[:30]
            photo_url = v["photo"] or ""

            file = request.files.get("photo")
            if file and file.filename:
                new_url = upload_to_cloudinary(file)
                if new_url:
                    photo_url = new_url
                else:
                    flash("Only image files allowed (png/jpg/jpeg/webp/gif), or upload failed.", "error")
                    return redirect(url_for("vendor_dashboard"))

            cur.execute(
                "UPDATE vendors SET name=%s, description=%s, phone=%s, photo=%s WHERE id=%s",
                (name, desc, phone, photo_url, v["id"]))
            db.commit()
            flash("Profile updated.", "ok")

        elif action == "add_juice":
            n = request.form.get("juice_name", "").strip()[:100]
            try:
                p = int(request.form.get("juice_price", "0"))
            except ValueError:
                p = 0
            if n and p > 0:
                cur.execute("INSERT INTO juices (vendor_id, name, price) VALUES (%s,%s,%s)",
                            (v["id"], n, p))
                db.commit()
                flash("Juice added.", "ok")
            else:
                flash("Enter valid name and price.", "error")

        elif action == "edit_juice":
            jid = int(request.form.get("juice_id", "0"))
            n = request.form.get("juice_name", "").strip()[:100]
            try:
                p = int(request.form.get("juice_price", "0"))
            except ValueError:
                p = 0
            active = request.form.get("active") == "on"
            cur.execute(
                "UPDATE juices SET name=%s, price=%s, active=%s WHERE id=%s AND vendor_id=%s",
                (n, p, active, jid, v["id"]))
            db.commit()
            flash("Juice updated.", "ok")

        elif action == "delete_juice":
            jid = int(request.form.get("juice_id", "0"))
            cur.execute("DELETE FROM juices WHERE id=%s AND vendor_id=%s", (jid, v["id"]))
            db.commit()
            flash("Juice deleted.", "ok")

        return redirect(url_for("vendor_dashboard"))

    cur.execute("SELECT * FROM juices WHERE vendor_id=%s ORDER BY name", (v["id"],))
    juices = cur.fetchall()
    cur.execute("SELECT * FROM orders WHERE vendor_id=%s ORDER BY id DESC", (v["id"],))
    orders = cur.fetchall()
    return render_template("vendor_dashboard.html", v=v, juices=juices, orders=orders)


# ---------- Operator dashboard ----------
STATUSES = ["Pending", "Preparing", "On the Way", "Delivered", "Cancelled"]


@app.route("/operator", methods=["GET", "POST"])
@login_required(role="operator")
def operator_dashboard():
    db = get_db()
    cur = db.cursor()
    if request.method == "POST":
        oid = int(request.form.get("order_id", "0"))
        new_status = request.form.get("status", "")
        if new_status in STATUSES:
            cur.execute("UPDATE orders SET status=%s WHERE id=%s", (new_status, oid))
            db.commit()
        return redirect(url_for("operator_dashboard"))

    cur.execute("SELECT * FROM orders ORDER BY id DESC")
    orders = cur.fetchall()
    today = date.today().isoformat()
    todays = [o for o in orders
              if o["created_at"].isoformat().startswith(today) and o["status"] != "Cancelled"]
    revenue_gross = sum(o["total"] for o in todays)
    revenue_fees = sum(o["service_fee"] for o in todays)
    return render_template("operator_dashboard.html", orders=orders, statuses=STATUSES,
                           revenue_gross=revenue_gross, revenue_fees=revenue_fees,
                           todays_count=len(todays))


@app.route("/orders")
@login_required(role="operator")
def orders_all():
    cur = get_db().cursor()
    cur.execute("SELECT * FROM orders ORDER BY id DESC")
    orders = cur.fetchall()
    return render_template("orders.html", orders=orders)


# ---------- Operator: manage vendors (no-code onboarding) ----------
@app.route("/operator/vendors", methods=["GET", "POST"])
@login_required(role="operator")
def manage_vendors():
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_vendor":
            name = request.form.get("name", "").strip()[:100]
            desc = request.form.get("description", "").strip()[:500]
            phone = request.form.get("phone", "").strip()[:30]
            if not name:
                flash("Vendor name is required.", "error")
                return redirect(url_for("manage_vendors"))

            cur.execute(
                "INSERT INTO vendors (name, description, phone, active) VALUES (%s,%s,%s,TRUE) RETURNING id",
                (name, desc, phone))
            vid = cur.fetchone()["id"]

            # Create a login for the new vendor, username unique-ified with the vendor id.
            username = f"{slugify_username(name)}{vid}"
            password = generate_password()
            cur.execute(
                "INSERT INTO users (username, password_hash, role, vendor_id) VALUES (%s,%s,'vendor',%s)",
                (username, generate_password_hash(password), vid))
            db.commit()

            flash(f"Vendor '{name}' added. Login — username: {username}  password: {password} "
                  f"(share this with the vendor now; it won't be shown again).", "ok")

        elif action == "edit_vendor":
            vid = int(request.form.get("vendor_id", "0"))
            name = request.form.get("name", "").strip()[:100]
            desc = request.form.get("description", "").strip()[:500]
            phone = request.form.get("phone", "").strip()[:30]
            active = request.form.get("active") == "on"
            if not name:
                flash("Vendor name is required.", "error")
                return redirect(url_for("manage_vendors"))
            cur.execute(
                "UPDATE vendors SET name=%s, description=%s, phone=%s, active=%s WHERE id=%s",
                (name, desc, phone, active, vid))
            db.commit()
            flash("Vendor updated.", "ok")

        elif action == "reset_password":
            vid = int(request.form.get("vendor_id", "0"))
            new_password = generate_password()
            cur.execute(
                "UPDATE users SET password_hash=%s WHERE vendor_id=%s AND role='vendor'",
                (generate_password_hash(new_password), vid))
            db.commit()
            if cur.rowcount == 0:
                flash("That vendor has no login yet — nothing to reset.", "error")
            else:
                flash(f"New password for this vendor: {new_password} "
                      f"(share it now; it won't be shown again).", "ok")

        elif action == "delete_vendor":
            vid = int(request.form.get("vendor_id", "0"))
            try:
                cur.execute("DELETE FROM vendors WHERE id=%s", (vid,))
                db.commit()
                flash("Vendor deleted.", "ok")
            except pg_errors.ForeignKeyViolation:
                db.rollback()
                flash("Can't delete a vendor with existing orders — mark them inactive instead.", "error")

        return redirect(url_for("manage_vendors"))

    cur.execute("""
        SELECT v.*, u.username AS login_username
        FROM vendors v
        LEFT JOIN users u ON u.vendor_id = v.id AND u.role = 'vendor'
        ORDER BY v.id
    """)
    vendors = cur.fetchall()
    return render_template("manage_vendors.html", vendors=vendors)


# ---------- Errors ----------
@app.errorhandler(403)
def e403(e):
    return render_template("error.html", code=403, msg="Forbidden"), 403


@app.errorhandler(404)
def e404(e):
    return render_template("error.html", code=404, msg="Not found"), 404


@app.errorhandler(413)
def e413(e):
    flash("File too large. Max 2MB.", "error")
    return redirect(request.referrer or url_for("index"))


# ---------- Bootstrap ----------
with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")),
            debug=os.getenv("DEBUG", "False").lower() == "true")