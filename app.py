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

# A vendor's first FREE_ORDER_LIMIT completed (Delivered) orders are free;
# from the next one onward, JuiceFront takes COMMISSION_RATE of the product
# price only -- never the delivery fee, since that's a pass-through cost, not
# the vendor's revenue.
FREE_ORDER_LIMIT = int(os.getenv("FREE_ORDER_LIMIT", "20"))
COMMISSION_RATE = float(os.getenv("COMMISSION_RATE", "0.07"))  # 7%

# An order left untouched flips to Delivered automatically after this many
# hours -- so silence never works in a vendor's favor. Cancelling before then
# is the only way to avoid that, which is why cancellations get verified
# with the customer below.
ORDER_AUTO_DELIVER_HOURS = int(os.getenv("ORDER_AUTO_DELIVER_HOURS", "7"))

# Shared secret for the /cron/auto-deliver endpoint, so an external
# scheduler (cron-job.org, GitHub Actions, Render Cron) can trigger the
# auto-deliver sweep even if nobody happens to open a dashboard that day.
CRON_SECRET = os.getenv("CRON_SECRET", "")

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


def phone_tail(raw_phone, digits=9):
    """Last N digits of a phone number, digits only. Used to match an inbound
    SMS reply to a stored customer_phone without needing both sides to be in
    the exact same format (0700..., 256700..., +256700... all match)."""
    only_digits = "".join(ch for ch in (raw_phone or "") if ch.isdigit())
    return only_digits[-digits:] if len(only_digits) >= digits else only_digits


def upload_to_cloudinary(file_storage, folder="juicefront/vendors", prefix="vendor"):
    """Uploads a Werkzeug FileStorage object to Cloudinary, returns secure_url or None.
    Same pipeline for every image type JuiceFront stores -- vendor catalog
    photos and customer "find me in the crowd" photos alike -- just filed
    under a different folder/prefix so they don't collide in the dashboard."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_EXT:
        return None
    try:
        result = cloudinary.uploader.upload(
            file_storage,
            folder=folder,
            public_id=f"{prefix}_{uuid.uuid4().hex[:12]}",
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
    # Each vendor owns their own delivery policy instead of one hardcoded
    # platform-wide fee: 'fixed' (their own flat fee), 'free', or
    # 'calculated' (confirmed by the vendor after they see the order).
    cur.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS "
                "delivery_policy TEXT NOT NULL DEFAULT 'fixed';")
    cur.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS "
                f"delivery_fee INTEGER NOT NULL DEFAULT {SERVICE_FEE};")
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
    # Records which delivery policy applied at order time, so a vendor
    # changing their policy later never rewrites history.
    cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "delivery_policy TEXT NOT NULL DEFAULT 'fixed';")
    # A Cancelled order waits here for the customer to confirm by SMS before
    # it's treated as final -- protects against a vendor mis-marking a real
    # delivery as Cancelled to dodge commission.
    cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "awaiting_customer_confirmation BOOLEAN NOT NULL DEFAULT FALSE;")
    cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "cancel_dispute_note TEXT DEFAULT '';")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS login_attempts (
        ip TEXT PRIMARY KEY,
        fails INTEGER DEFAULT 0,
        locked_until BIGINT DEFAULT 0
    );
    """)

    # ---- Venues ("Grounds" mode) ----
    # A venue is a gathering place with its own schedule (e.g. Phaneroo's two
    # weekly services), not just a vendor field. Vendors get "checked in" to
    # a venue via vendors.venue_id -- the same vendor account can also do
    # regular home delivery when venue_id is NULL. While a venue is inactive
    # (outside service hours), ordering from vendors checked into it pauses.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS venues (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT UNIQUE NOT NULL,
        schedule_note TEXT DEFAULT '',
        active BOOLEAN NOT NULL DEFAULT TRUE
    );
    """)
    # A vendor checked into a venue sells there instead of / in addition to
    # regular delivery; NULL means "regular delivery vendor, no venue".
    cur.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS "
                "venue_id INTEGER REFERENCES venues(id) ON DELETE SET NULL;")
    # Lets a vendor sell more than juice (e.g. snacks) without a schema change
    # to the product model -- category is just a label on top of it.
    cur.execute("ALTER TABLE juices ADD COLUMN IF NOT EXISTS "
                "category TEXT NOT NULL DEFAULT 'Juice';")
    # Venue orders record where they were placed (in case a vendor's venue_id
    # changes later) plus the photo/seat "find me in the crowd" details --
    # customer_location is still populated (with the seat description) so
    # every existing dashboard/SMS/template that reads it keeps working
    # untouched; photo_url is the one genuinely new piece of information.
    cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "venue_id INTEGER REFERENCES venues(id) ON DELETE SET NULL;")
    cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "seat_description TEXT DEFAULT '';")
    cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "photo_url TEXT DEFAULT '';")
    conn.commit()

    # Seed the Phaneroo venue if none exist yet -- operators add more from
    # Manage Vendors, this just gets the first real one in place.
    cur.execute("SELECT COUNT(*) AS count FROM venues")
    if cur.fetchone()["count"] == 0:
        cur.execute(
            "INSERT INTO venues (name, slug, schedule_note, active) "
            "VALUES (%s,%s,%s,TRUE)",
            ("Phaneroo Grounds", "phaneroo",
             "2 services a week -- toggle active before/after each service"))
        conn.commit()

    # Seed vendors + juices only if empty. Kept to just the two real vendors in
    # use today -- everyone else should be added from Operator -> Manage
    # Vendors, not from code.
    cur.execute("SELECT COUNT(*) AS count FROM vendors")
    if cur.fetchone()["count"] == 0:
        seed = [
            ("Mama Pauline", "Fresh tropical blends from Nansana market.", "0700000001",
             "fixed", 200,
             [("500ml Mango Passion", 500), ("5L Jerrycan", 20000),
              ("10L Jerrycan", 40000), ("20L Jerrycan", 80000)]),
            ("Mama Hailey", "100% organic, no added sugar.", "0700000002",
             "free", 0,
             [("Avocado Smoothie", 4000), ("Beetroot Boost", 3500), ("Green Detox", 3500)]),
        ]
        for name, desc, phone, delivery_policy, delivery_fee, juices in seed:
            cur.execute(
                "INSERT INTO vendors (name, description, phone, active, delivery_policy, delivery_fee) "
                "VALUES (%s,%s,%s,TRUE,%s,%s) RETURNING id",
                (name, desc, phone, delivery_policy, delivery_fee))
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


DELIVERY_POLICIES = {
    "fixed": "Flat delivery fee",
    "free": "Free delivery",
    "calculated": "Vendor confirms delivery fee after seeing your location",
}

# Vendors' "juices" are really just name+price items, so a category label is
# all that's needed to let a vendor sell snacks alongside juice -- no new
# table, no schema change beyond the one column.
CATEGORIES = ["Juice", "Snacks", "Other"]


def venue_for_vendor(cur, v):
    """Returns the venue row for a vendor checked into one, else None."""
    if not v.get("venue_id"):
        return None
    cur.execute("SELECT * FROM venues WHERE id=%s", (v["venue_id"],))
    return cur.fetchone()


def delivery_for_vendor(v):
    """Returns (fee_to_charge_now, label_for_customer) for a vendor's delivery policy."""
    policy = v["delivery_policy"] or "fixed"
    if policy == "free":
        return 0, "Free delivery"
    if policy == "calculated":
        return 0, "Delivery fee confirmed by the vendor after they see your order"
    fee = v["delivery_fee"] if v["delivery_fee"] is not None else SERVICE_FEE
    return fee, f"UGX {fee:,} delivery fee"


def auto_deliver_stale_orders(cur, db):
    """Any order still sitting in Pending/Preparing/On the Way after
    ORDER_AUTO_DELIVER_HOURS flips to Delivered on its own. This is what
    makes doing nothing the losing move for anyone trying to dodge
    commission -- silence resolves in the customer's (and JuiceFront's)
    favor, not the vendor's. Cancelled orders are untouched here; those are
    handled separately via the customer-confirmation flow."""
    cur.execute(f"""
        UPDATE orders
        SET status = 'Delivered'
        WHERE status IN ('Pending', 'Preparing', 'On the Way')
          AND created_at <= NOW() - INTERVAL '{ORDER_AUTO_DELIVER_HOURS} hours'
    """)
    flipped = cur.rowcount
    db.commit()
    return flipped


def billing_summary(cur, vendor_id=None):
    """Per-vendor completed-order count, free-allowance usage, and commission
    owed on orders past FREE_ORDER_LIMIT. Commission applies to the product
    price only (never the delivery fee), and orders are counted in the order
    they were placed -- so the *first* FREE_ORDER_LIMIT delivered orders are
    always the free ones, regardless of when you run this. Returns a dict
    keyed by vendor_id; vendors with zero completed orders are simply absent.
    """
    where = "WHERE status = 'Delivered'"
    params = []
    if vendor_id is not None:
        where += " AND vendor_id = %s"
        params.append(vendor_id)
    cur.execute(f"""
        WITH completed AS (
            SELECT vendor_id, juice_price,
                   ROW_NUMBER() OVER (PARTITION BY vendor_id ORDER BY id) AS seq
            FROM orders
            {where}
        )
        SELECT vendor_id,
               COUNT(*) AS completed_count,
               COALESCE(SUM(CASE WHEN seq > %s THEN juice_price ELSE 0 END), 0) AS billable_subtotal
        FROM completed
        GROUP BY vendor_id
    """, params + [FREE_ORDER_LIMIT])

    result = {}
    for r in cur.fetchall():
        completed = r["completed_count"]
        result[r["vendor_id"]] = {
            "completed_count": completed,
            "free_used": min(completed, FREE_ORDER_LIMIT),
            "billable_orders": max(0, completed - FREE_ORDER_LIMIT),
            "commission_owed": round(r["billable_subtotal"] * COMMISSION_RATE),
        }
    return result


EMPTY_BILLING = {"completed_count": 0, "free_used": 0, "billable_orders": 0, "commission_owed": 0}


# ---------- Template globals ----------
@app.context_processor
def inject_globals():
    return dict(user=current_user(), SERVICE_FEE=SERVICE_FEE, APP_NAME="JuiceFront",
                DELIVERY_POLICIES=DELIVERY_POLICIES, FREE_ORDER_LIMIT=FREE_ORDER_LIMIT,
                COMMISSION_RATE=COMMISSION_RATE, CATEGORIES=CATEGORIES)


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
    venue = venue_for_vendor(cur, v)
    return render_template("vendor_detail.html", v=v, juices=juices, venue=venue)


@app.route("/search")
def search():
    """Matches a vendor by name/description, or by any active juice's name
    or category, so 'samosa' finds a vendor who sells samosas even if the
    vendor's own name/description never mentions the word. Kept as two
    passes (find matching vendors, then re-fetch each vendor's own cheapest
    -- or best-matching -- juice) rather than one wide join, so the dict
    rows stay simple vendor rows/juice rows with no duplicate-column
    surprises from aliasing vendor_id twice."""
    q = request.args.get("q", "").strip()[:100]
    db = get_db()
    cur = db.cursor()
    cards = []
    if q:
        like = f"%{q}%"
        cur.execute("""
            SELECT DISTINCT v.*
            FROM vendors v
            LEFT JOIN juices j ON j.vendor_id = v.id AND j.active = TRUE
            WHERE v.active = TRUE
              AND (v.name ILIKE %s OR v.description ILIKE %s
                   OR j.name ILIKE %s OR j.category ILIKE %s)
            ORDER BY v.name
        """, (like, like, like, like))
        vendors = cur.fetchall()
        for v in vendors:
            # Prefer showing the matching item itself (e.g. the samosa, not
            # whatever's cheapest); fall back to the vendor's cheapest item.
            cur.execute(
                "SELECT * FROM juices WHERE vendor_id=%s AND active=TRUE "
                "AND (name ILIKE %s OR category ILIKE %s) ORDER BY price LIMIT 1",
                (v["id"], like, like))
            j = cur.fetchone()
            if not j:
                cur.execute(
                    "SELECT * FROM juices WHERE vendor_id=%s AND active=TRUE "
                    "ORDER BY price LIMIT 1", (v["id"],))
                j = cur.fetchone()
            cards.append({"v": v, "j": j})
    return render_template("search_results.html", q=q, cards=cards)


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
    delivery_fee, delivery_label = delivery_for_vendor(v)
    venue = venue_for_vendor(cur, v)

    if venue and not venue["active"]:
        flash(f"{venue['name']} isn't taking orders right now -- check back "
              f"during service hours.", "error")
        return redirect(url_for("vendor_detail", vid=v["id"]))

    if request.method == "POST":
        name = request.form.get("customer_name", "").strip()[:100]
        phone = request.form.get("customer_phone", "").strip()[:30]
        note = request.form.get("note", "").strip()[:500]
        photo_url = ""

        if venue:
            # Grounds mode: find-me-in-the-crowd instead of a street address.
            # Photo is optional (not everyone wants to submit a selfie for a
            # juice order), the seat/location description is required so
            # there's always something to go on.
            seat = request.form.get("seat_description", "").strip()[:200]
            if not (name and phone and seat and note):
                flash("Please fill in all fields.", "error")
                return render_template("order_form.html", j=j, v=v, venue=venue,
                                        delivery_fee=delivery_fee, delivery_label=delivery_label)
            file = request.files.get("photo")
            if file and file.filename:
                photo_url = upload_to_cloudinary(
                    file, folder="juicefront/customer_photos", prefix="order")
                if not photo_url:
                    flash("Only image files allowed (png/jpg/jpeg/webp/gif), or upload failed.", "error")
                    return render_template("order_form.html", j=j, v=v, venue=venue,
                                            delivery_fee=delivery_fee, delivery_label=delivery_label)
            loc = seat
        else:
            seat = ""
            loc = request.form.get("customer_location", "").strip()[:200]
            if not (name and phone and loc and note):
                flash("Please fill in all fields.", "error")
                return render_template("order_form.html", j=j, v=v, venue=venue,
                                        delivery_fee=delivery_fee, delivery_label=delivery_label)

        total = j["price"] + delivery_fee
        cur.execute("""INSERT INTO orders
            (vendor_id, vendor_name, juice_id, juice_name, juice_price,
             service_fee, total, customer_name, customer_phone, customer_location,
             note, status, delivery_policy, venue_id, seat_description, photo_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id""",
            (v["id"], v["name"], j["id"], j["name"], j["price"],
             delivery_fee, total, name, phone, loc, note, "Pending", v["delivery_policy"],
             venue["id"] if venue else None, seat, photo_url))
        order_id = cur.fetchone()["id"]
        db.commit()

        # Notify by SMS. This runs after the commit above, so a notification
        # failure (bad number, AT outage, no credit) never loses the order --
        # it's already safely in PostgreSQL either way.
        if venue:
            location_line = f"Find them: {loc}" + (" (photo attached in dashboard)" if photo_url else "")
        else:
            location_line = f"Location: {loc}"
        delivery_line = (delivery_label if v["delivery_policy"] == "calculated"
                          else f"Delivery: UGX {delivery_fee:,}")
        order_msg = (
            f"JuiceFront - New Order #{order_id}\n"
            f"Vendor: {v['name']}\n"
            f"Juice: {j['name']}\n"
            f"Qty/details: {note}\n"
            f"Customer: {name} ({phone})\n"
            f"{location_line}\n"
            f"{delivery_line}\n"
            f"Total: UGX {total:,}"
        )
        send_sms(v["phone"], order_msg)
        if OPERATOR_PHONE:
            send_sms(OPERATOR_PHONE, order_msg)

        return redirect(url_for("success", order_id=order_id))
    return render_template("order_form.html", j=j, v=v, venue=venue,
                            delivery_fee=delivery_fee, delivery_label=delivery_label)


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

            delivery_policy = request.form.get("delivery_policy", "fixed")
            if delivery_policy not in DELIVERY_POLICIES:
                delivery_policy = "fixed"
            try:
                delivery_fee = max(0, int(request.form.get("delivery_fee", "0")))
            except ValueError:
                delivery_fee = 0

            file = request.files.get("photo")
            if file and file.filename:
                new_url = upload_to_cloudinary(file)
                if new_url:
                    photo_url = new_url
                else:
                    flash("Only image files allowed (png/jpg/jpeg/webp/gif), or upload failed.", "error")
                    return redirect(url_for("vendor_dashboard"))

            cur.execute(
                "UPDATE vendors SET name=%s, description=%s, phone=%s, photo=%s, "
                "delivery_policy=%s, delivery_fee=%s WHERE id=%s",
                (name, desc, phone, photo_url, delivery_policy, delivery_fee, v["id"]))
            db.commit()
            flash("Profile updated.", "ok")

        elif action == "add_juice":
            n = request.form.get("juice_name", "").strip()[:100]
            category = request.form.get("category", "Juice")
            if category not in CATEGORIES:
                category = "Juice"
            try:
                p = int(request.form.get("juice_price", "0"))
            except ValueError:
                p = 0
            if n and p > 0:
                cur.execute(
                    "INSERT INTO juices (vendor_id, name, price, category) VALUES (%s,%s,%s,%s)",
                    (v["id"], n, p, category))
                db.commit()
                flash("Item added.", "ok")
            else:
                flash("Enter valid name and price.", "error")

        elif action == "edit_juice":
            jid = int(request.form.get("juice_id", "0"))
            n = request.form.get("juice_name", "").strip()[:100]
            category = request.form.get("category", "Juice")
            if category not in CATEGORIES:
                category = "Juice"
            try:
                p = int(request.form.get("juice_price", "0"))
            except ValueError:
                p = 0
            active = request.form.get("active") == "on"
            cur.execute(
                "UPDATE juices SET name=%s, price=%s, active=%s, category=%s "
                "WHERE id=%s AND vendor_id=%s",
                (n, p, active, category, jid, v["id"]))
            db.commit()
            flash("Item updated.", "ok")

        elif action == "delete_juice":
            jid = int(request.form.get("juice_id", "0"))
            cur.execute("DELETE FROM juices WHERE id=%s AND vendor_id=%s", (jid, v["id"]))
            db.commit()
            flash("Juice deleted.", "ok")

        return redirect(url_for("vendor_dashboard"))

    auto_deliver_stale_orders(cur, db)
    cur.execute("SELECT * FROM juices WHERE vendor_id=%s ORDER BY name", (v["id"],))
    juices = cur.fetchall()
    cur.execute("SELECT * FROM orders WHERE vendor_id=%s ORDER BY id DESC", (v["id"],))
    orders = cur.fetchall()
    my_billing = billing_summary(cur, vendor_id=v["id"]).get(v["id"], EMPTY_BILLING)
    venue = venue_for_vendor(cur, v)
    return render_template("vendor_dashboard.html", v=v, juices=juices, orders=orders,
                           billing=my_billing, venue=venue)


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
            if new_status == "Cancelled":
                cur.execute("SELECT * FROM orders WHERE id=%s", (oid,))
                o = cur.fetchone()
                if o:
                    cur.execute(
                        "UPDATE orders SET status='Cancelled', awaiting_customer_confirmation=TRUE "
                        "WHERE id=%s", (oid,))
                    db.commit()
                    send_sms(
                        o["customer_phone"],
                        f"JuiceFront: We understand your order #{o['id']} from {o['vendor_name']} "
                        f"was cancelled. Reply YES if that's correct, or NO if you actually "
                        f"received your order."
                    )
                    flash(f"Order #{oid} marked Cancelled -- SMS sent to the customer to confirm.", "ok")
            else:
                cur.execute(
                    "UPDATE orders SET status=%s, awaiting_customer_confirmation=FALSE WHERE id=%s",
                    (new_status, oid))
                db.commit()
        return redirect(url_for("operator_dashboard"))

    auto_deliver_stale_orders(cur, db)

    cur.execute("SELECT * FROM orders ORDER BY id DESC")
    orders = cur.fetchall()
    today = date.today().isoformat()
    todays = [o for o in orders
              if o["created_at"].isoformat().startswith(today) and o["status"] != "Cancelled"]
    revenue_gross = sum(o["total"] for o in todays)
    revenue_fees = sum(o["service_fee"] for o in todays)

    billing = billing_summary(cur)
    cur.execute("SELECT id, name FROM vendors ORDER BY id")
    vendor_billing = [
        {"id": vv["id"], "name": vv["name"], **billing.get(vv["id"], EMPTY_BILLING)}
        for vv in cur.fetchall()
    ]

    return render_template("operator_dashboard.html", orders=orders, statuses=STATUSES,
                           revenue_gross=revenue_gross, revenue_fees=revenue_fees,
                           todays_count=len(todays), vendor_billing=vendor_billing)


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
            venue_id = request.form.get("venue_id") or None
            if not name:
                flash("Vendor name is required.", "error")
                return redirect(url_for("manage_vendors"))

            cur.execute(
                "INSERT INTO vendors (name, description, phone, active, venue_id) "
                "VALUES (%s,%s,%s,TRUE,%s) RETURNING id",
                (name, desc, phone, venue_id))
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
            delivery_policy = request.form.get("delivery_policy", "fixed")
            if delivery_policy not in DELIVERY_POLICIES:
                delivery_policy = "fixed"
            venue_id = request.form.get("venue_id") or None
            try:
                delivery_fee = max(0, int(request.form.get("delivery_fee", "0")))
            except ValueError:
                delivery_fee = 0
            if not name:
                flash("Vendor name is required.", "error")
                return redirect(url_for("manage_vendors"))
            cur.execute(
                "UPDATE vendors SET name=%s, description=%s, phone=%s, active=%s, "
                "delivery_policy=%s, delivery_fee=%s, venue_id=%s WHERE id=%s",
                (name, desc, phone, active, delivery_policy, delivery_fee, venue_id, vid))
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

        elif action == "add_venue":
            name = request.form.get("venue_name", "").strip()[:100]
            schedule_note = request.form.get("schedule_note", "").strip()[:200]
            if not name:
                flash("Venue name is required.", "error")
                return redirect(url_for("manage_vendors"))
            slug_base = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-") or "venue"
            slug = slug_base
            n = 1
            while True:
                cur.execute("SELECT 1 FROM venues WHERE slug=%s", (slug,))
                if not cur.fetchone():
                    break
                n += 1
                slug = f"{slug_base}-{n}"
            cur.execute(
                "INSERT INTO venues (name, slug, schedule_note, active) VALUES (%s,%s,%s,TRUE)",
                (name, slug, schedule_note))
            db.commit()
            flash(f"Venue '{name}' added.", "ok")

        elif action == "toggle_venue":
            venue_id = int(request.form.get("venue_id", "0"))
            cur.execute("UPDATE venues SET active = NOT active WHERE id=%s", (venue_id,))
            db.commit()
            flash("Venue status updated.", "ok")

        elif action == "edit_venue":
            venue_id = int(request.form.get("venue_id", "0"))
            name = request.form.get("venue_name", "").strip()[:100]
            schedule_note = request.form.get("schedule_note", "").strip()[:200]
            if not name:
                flash("Venue name is required.", "error")
                return redirect(url_for("manage_vendors"))
            cur.execute(
                "UPDATE venues SET name=%s, schedule_note=%s WHERE id=%s",
                (name, schedule_note, venue_id))
            db.commit()
            flash("Venue updated.", "ok")

        return redirect(url_for("manage_vendors"))

    cur.execute("""
        SELECT v.*, u.username AS login_username
        FROM vendors v
        LEFT JOIN users u ON u.vendor_id = v.id AND u.role = 'vendor'
        ORDER BY v.id
    """)
    vendors = cur.fetchall()
    cur.execute("SELECT * FROM venues ORDER BY id")
    venues = cur.fetchall()
    return render_template("manage_vendors.html", vendors=vendors, venues=venues)


# ---------- SMS inbound (customer confirms/disputes a cancellation) ----------
YES_WORDS = {"yes", "y", "correct", "confirm", "confirmed", "true", "ok", "okay"}
NO_WORDS = {"no", "n", "wrong", "incorrect", "received", "false"}


@app.route("/sms/inbound/<secret>", methods=["POST"])
def sms_inbound(secret):
    """Africa's Talking posts here when a customer replies to a cancellation
    confirmation SMS. Configure this URL (with your CRON_SECRET as <secret>)
    as the callback in the AT dashboard. Not behind @login_required since
    AT itself is the caller -- the secret in the URL is the auth."""
    if not CRON_SECRET or secret != CRON_SECRET:
        abort(404)

    from_number = request.form.get("from", "") or request.values.get("from", "")
    text = (request.form.get("text", "") or request.values.get("text", "")).strip().lower()
    if not from_number or not text:
        return ("", 204)

    tail = phone_tail(from_number)
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT * FROM orders
        WHERE awaiting_customer_confirmation = TRUE
        ORDER BY id DESC
    """)
    candidates = [o for o in cur.fetchall() if phone_tail(o["customer_phone"]) == tail]
    if not candidates:
        return ("", 204)
    order = candidates[0]  # most recent match

    first_word_raw = text.split()[0] if text.split() else ""
    first_word = "".join(ch for ch in first_word_raw if ch.isalnum())
    if first_word in YES_WORDS:
        cur.execute(
            "UPDATE orders SET awaiting_customer_confirmation=FALSE, "
            "cancel_dispute_note='Customer confirmed the cancellation by SMS.' WHERE id=%s",
            (order["id"],))
        db.commit()
    elif first_word in NO_WORDS:
        cur.execute(
            "UPDATE orders SET status='Delivered', awaiting_customer_confirmation=FALSE, "
            "cancel_dispute_note=%s WHERE id=%s",
            (f"Customer disputed the cancellation (replied \"{text}\"). "
             f"Auto-reverted to Delivered for review.", order["id"]))
        db.commit()
        if OPERATOR_PHONE:
            send_sms(OPERATOR_PHONE,
                     f"JuiceFront: Order #{order['id']} ({order['vendor_name']}) -- customer "
                     f"disputed the cancellation and it's been reverted to Delivered. Please review.")
    else:
        # Ambiguous reply -- leave it pending and log it for manual follow-up
        # rather than guessing.
        cur.execute(
            "UPDATE orders SET cancel_dispute_note=%s WHERE id=%s",
            (f"Customer replied \"{text}\" (unclear yes/no) -- still awaiting confirmation.",
             order["id"]))
        db.commit()

    return ("", 204)


# ---------- Cron: catch orders auto-deliver would otherwise miss ----------
@app.route("/cron/auto-deliver")
def cron_auto_deliver():
    """Hit by an external scheduler (cron-job.org, GitHub Actions, Render Cron)
    so the auto-deliver sweep runs on a real clock, not just whenever someone
    happens to open a dashboard. ?secret=<CRON_SECRET> is the auth."""
    if not CRON_SECRET or request.args.get("secret") != CRON_SECRET:
        abort(404)
    db = get_db()
    cur = db.cursor()
    flipped = auto_deliver_stale_orders(cur, db)
    return {"auto_delivered": flipped}


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