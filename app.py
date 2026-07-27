# JuiceFront-main\app.py

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
import time
import uuid
from datetime import date
from functools import wraps

import cloudinary
import cloudinary.uploader
from gas_db import (
    get_vendors,
    get_vendor,
    get_juices,
    place_order,
    vendor_login,
    vendor_orders,
    update_order_status,
    add_vendor,
    add_juice,
    all_orders,
)
from dotenv import load_dotenv
from flask import (Flask, g, render_template, request, redirect, url_for,
                    session, flash, abort)
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

# ---------- Config ----------
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB
SERVICE_FEE = int(os.getenv("SERVICE_FEE", "200"))  # UGX


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

# ---------- Google Apps Script Database ----------

def get_db():
    """
    Temporary compatibility stub.

    This will disappear as we migrate each route to gas_db.py.
    """
    return None

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

    vendors = get_vendors()
    juices = get_all_juices()

    # Group available juices by vendor
    juices_by_vendor = {}

    for juice in juices:

        # Skip unavailable juices
        if str(juice.get("available", "")).lower() not in ("true", "1", "yes"):
            continue

        vendor_id = str(juice["vendorId"])

        juices_by_vendor.setdefault(vendor_id, []).append(juice)

    cards = []

    for vendor in vendors:

        vendor_id = str(vendor["vendorId"])

        vendor_juices = juices_by_vendor.get(vendor_id, [])

        # Show the cheapest juice on the home page
        vendor_juices.sort(key=lambda j: float(j["price"]))

        first_juice = vendor_juices[0] if vendor_juices else None

        cards.append({
            "v": vendor,
            "j": first_juice
        })

    return render_template(
        "index.html",
        cards=cards
    )

@app.route("/vendor/<int:vid>")
def vendor_detail(vid):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM vendors WHERE id=%s", (vid,))
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
        if not (name and phone and loc):
            flash("Please fill in name, phone and location.", "error")
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")),
            debug=os.getenv("DEBUG", "False").lower() == "true")