from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from datetime import date, datetime, timedelta
from werkzeug.utils import secure_filename
import uuid
import psycopg2
import psycopg2.extras
import hashlib
from datetime import date, datetime
import os
import pandas as pd
from io import BytesIO
from datetime import date, datetime, timedelta, time
import pytz
import json



def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def save_product_image(file):
    if file and allowed_file(file.filename):
        ext = file.filename.rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        return f"/static/uploads/products/{filename}"
    return None

# Kenyan timezone
EAT = pytz.timezone("Africa/Nairobi")

def get_kenya_now():
    """Current date and time in Kenya"""
    return datetime.now(EAT)

def get_kenya_today():
    """Today's date in Kenya"""
    return datetime.now(EAT).date()

def get_kenya_time():
    """Current time in Kenya"""
    return datetime.now(EAT).time().replace(microsecond=0)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")



from datetime import timedelta

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=6)  # session expires after 6 hours
app.config['SESSION_REFRESH_EACH_REQUEST'] = True


UPLOAD_FOLDER = os.path.join("static", "uploads", "products")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB max

# Create folder if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ====================== DATABASE ======================
DATABASE_URL = os.environ.get("DATABASE_URL")

def current_shop_id():
    return session.get("shop_id") or 1

def shop_has_feature(shop_id, feature_name):
    if not shop_id:
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT is_enabled FROM shop_features
        WHERE shop_id = %s AND feature_name = %s
    """, (shop_id, feature_name))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return bool(row and row[0])

def shop_subscription_ok(shop_id):
    """False if shop is past_due or cancelled (soft lock)."""
    if not shop_id:
        return True  # superadmin has no shop
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT is_active, COALESCE(subscription_status, 'active') as subscription_status
        FROM shops WHERE shop_id = %s
    """, (shop_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return False
    if not row["is_active"]:
        return False
    if row["subscription_status"] in ("past_due", "cancelled"):
        return False
    return True

def get_connection():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    return conn

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def compute_commission(rule, amount):
    """Single source of truth for commission math, driven by services.commission_rule."""
    if rule == "flat100":
        return 100
    elif rule == "full":
        return int(amount)
    elif rule == "none":
        return 0
    else:  # 'standard'
        return int(round(amount * 0.30))

# ====================== CREATE TABLES (first time only) ======================
def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS staff (
                staff_id SERIAL PRIMARY KEY,
                full_name TEXT NOT NULL,
                phone TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_types (
                vehicle_type_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS services (
                service_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                is_package INTEGER DEFAULT 0,
                commission_rule TEXT DEFAULT 'standard',
                is_adjustment INTEGER DEFAULT 0
            )
        """)

        cursor.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS commission_rule TEXT DEFAULT 'standard'")
        cursor.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS is_adjustment INTEGER DEFAULT 0")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                price_id SERIAL PRIMARY KEY,
                vehicle_type_id INTEGER REFERENCES vehicle_types(vehicle_type_id),
                service_id INTEGER REFERENCES services(service_id),
                amount INTEGER NOT NULL,
                UNIQUE(vehicle_type_id, service_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS washes (
                wash_id SERIAL PRIMARY KEY,
                registration_number TEXT NOT NULL,
                staff_id INTEGER REFERENCES staff(staff_id),
                vehicle_type_id INTEGER REFERENCES vehicle_types(vehicle_type_id),
                wash_date DATE DEFAULT CURRENT_DATE,
                wash_time TIME DEFAULT CURRENT_TIME,
                total_amount INTEGER NOT NULL,
                payment_method TEXT DEFAULT 'Cash',
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wash_services (
                wash_service_id SERIAL PRIMARY KEY,
                wash_id INTEGER REFERENCES washes(wash_id) ON DELETE CASCADE,
                service_id INTEGER REFERENCES services(service_id),
                amount INTEGER NOT NULL,
                commission_amount INTEGER DEFAULT 0
            )
        """)

        cursor.execute("ALTER TABLE wash_services ADD COLUMN IF NOT EXISTS commission_amount INTEGER DEFAULT 0")

        for table in ("users", "staff", "vehicle_types", "services", "prices", "washes"):
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS shop_id INTEGER DEFAULT 1")
            except Exception:
                pass

        cursor.execute("SELECT 1 FROM users WHERE username = %s", ("admin",))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO users (username, password_hash, full_name, role, shop_id)
                VALUES (%s, %s, %s, %s, 1)
            """, ("admin", hash_password("admin123"), "System Administrator", "admin"))
            cursor.execute("""
                INSERT INTO users (username, password_hash, full_name, role, shop_id)
                VALUES (%s, %s, %s, %s, 1)
            """, ("cashier1", hash_password("cashier123"), "Cashier One", "cashier"))

        vehicles = ["Matatu", "5-seater", "7-seater", "31-seater Bus", "51-seater Bus", "Lorry"]
        for v in vehicles:
            cursor.execute("""
                SELECT 1 FROM vehicle_types
                WHERE name = %s AND COALESCE(shop_id, 1) = 1
            """, (v,))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO vehicle_types (name, shop_id) VALUES (%s, 1)",
                    (v,)
                )

        services = [
            ("General Wash", 0, "standard", 0),
            ("General + Vacuum", 1, "standard", 0),
            ("Vacuum", 0, "standard", 0),
            ("Outside Wash", 0, "standard", 0),
            ("Underwash", 0, "flat100", 0),
            ("Engine Steaming", 0, "flat100", 0),
            ("Carpet Wash", 0, "standard", 0),
            ("Extra Payment", 0, "standard", 1),
            ("Staff Tip", 0, "full", 1),
        ]
        for name, is_pkg, rule, is_adj in services:
            cursor.execute("""
                SELECT service_id FROM services
                WHERE name = %s AND COALESCE(shop_id, 1) = 1
            """, (name,))
            row = cursor.fetchone()
            if row:
                cursor.execute("""
                    UPDATE services
                    SET commission_rule = %s, is_adjustment = %s, is_package = %s
                    WHERE service_id = %s
                """, (rule, is_adj, is_pkg, row[0]))
            else:
                cursor.execute("""
                    INSERT INTO services (name, is_package, commission_rule, is_adjustment, shop_id)
                    VALUES (%s, %s, %s, %s, 1)
                """, (name, is_pkg, rule, is_adj))

        conn.commit()
        print("Database initialized successfully!")

    except Exception as e:
        conn.rollback()
        print(f"DB init error: {e}")

    finally:
        cursor.close()
        conn.close()
# ====================== LOGIN ======================
@app.route("/")
def home():
    if "user_id" in session:
        if session["role"] == "admin":
            return redirect(url_for("dashboard"))
        else:
            return redirect(url_for("cashier_home"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        hashed = hash_password(password)

        conn = get_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT user_id, username, full_name, role, must_change_password, staff_id, shop_id
            FROM users 
            WHERE username = %s AND password_hash = %s AND is_active = 1
        """, (username, hashed))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user:
            # --- Billing / inactive shop check (NOT for superadmin) ---
            if user["role"] != "superadmin" and user.get("shop_id"):
                conn2 = get_connection()
                cur2 = conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur2.execute("""
                    SELECT is_active,
                           COALESCE(subscription_status, 'active') as subscription_status,
                           name
                    FROM shops
                    WHERE shop_id = %s
                """, (user["shop_id"],))
                shop = cur2.fetchone()
                cur2.close()
                conn2.close()

                if not shop or not shop["is_active"]:
                    flash("This shop is inactive. Contact OshaSmart support.", "danger")
                    return render_template("login.html")

                if shop["subscription_status"] in ("past_due", "cancelled"):
                    flash(
                        f"Billing issue for {shop['name']}: subscription is {shop['subscription_status']}. "
                        "Please contact OshaSmart to restore access.",
                        "danger"
                    )
                    return render_template("login.html")

            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            session["shop_id"] = user.get("shop_id") or 1   # Luxe default
            session.permanent = True

            

            if user and user.get("shop_id"):
                        conn2 = get_connection()
                        cur2 = conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                        cur2.execute("SELECT is_active FROM shops WHERE shop_id = %s", (user["shop_id"],))
                        shop = cur2.fetchone()
                        cur2.close()
                        conn2.close()
                        if shop and not shop["is_active"]:
                            flash("This shop is currently inactive. Contact OshaSmart support.", "danger")
                            return render_template("login.html")

            if user.get("staff_id"):
                session["staff_id"] = user["staff_id"]
###==================================or non-superadmin=====
            if user.get("must_change_password") == 1:
                flash("You must change your password before continuing.", "info")
                return redirect(url_for("change_password"))

            flash(f"Welcome, {user['full_name']}!", "success")


            if user["role"] == "superadmin":
                session["user_id"] = user["user_id"]
                session["username"] = user["username"]
                session["full_name"] = user["full_name"]
                session["role"] = "superadmin"
                session["shop_id"] = None   # platform level — no single shop
                session.permanent = True
                return redirect(url_for("platform_dashboard"))

            elif user["role"] == "admin":
                return redirect(url_for("dashboard"))
            elif user["role"] == "cashier":
                return redirect(url_for("cashier_home"))
            elif user["role"] == "staff":
                return redirect(url_for("staff_dashboard"))
            else:
                return redirect(url_for("login"))
        else:
            flash("Invalid username or password", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

#====================================registration=======================================
@app.route("/check-registration/<reg>")
def check_registration(reg):
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401

    shop_id = current_shop_id()
    reg = reg.strip().upper()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT vt.vehicle_type_id, vt.name
        FROM washes w
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.registration_number = %s AND w.shop_id = %s
        ORDER BY w.wash_id ASC
        LIMIT 1
    """, (reg, shop_id))
    result = cursor.fetchone()
    cursor.close()
    conn.close()

    if result:
        return {
            "exists": True,
            "vehicle_type_id": result["vehicle_type_id"],
            "vehicle_name": result["name"]
        }
    return {"exists": False}


# ====================== DASHBOARD ======================
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()
    show_backup_reminder = today.weekday() == 5

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT
            COUNT(*) as total_washes,
            COALESCE(SUM(total_amount), 0) as total_money,
            COALESCE(SUM(cash_amount), 0) as cash_total,
            COALESCE(SUM(mpesa_amount), 0) as mpesa_total
        FROM washes
        WHERE wash_date = %s
          AND shop_id = %s
          AND (status = 'completed' OR status IS NULL)
    """, (today, shop_id))
    stats = cursor.fetchone()
    cursor.close()
    conn.close()

    return render_template(
        "dashboard.html",
        stats=stats,
        today=today,
        show_backup_reminder=show_backup_reminder
    )

@app.route("/cashier")
def cashier_home():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("cashier_home.html")

# ====================== RECORD WASH ======================

@app.route("/record-wash", methods=["GET", "POST"])
def record_wash():
    if "user_id" not in session:
        return redirect(url_for("login"))

    shop_id = current_shop_id()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Always load these (GET and POST)
    cursor.execute("""
        SELECT staff_id, full_name FROM staff
        WHERE is_active = 1 AND shop_id = %s
        ORDER BY full_name
    """, (shop_id,))
    staff = cursor.fetchall()

    cursor.execute("""
        SELECT vehicle_type_id, name FROM vehicle_types
        WHERE shop_id = %s
        ORDER BY vehicle_type_id
    """, (shop_id,))
    vehicle_types = cursor.fetchall()

    if request.method == "POST":
        reg = request.form.get("registration", "").strip().upper()
        staff_id = request.form.get("staff_id")
        vehicle_type_id = request.form.get("vehicle_type_id") or request.form.get("vehicle_type_id_hidden")
        selected_services = request.form.getlist("services")

        cash_amount_raw = request.form.get("cash_amount", "0").strip()
        mpesa_amount_raw = request.form.get("mpesa_amount", "0").strip()

        try:
            cash_amount = int(cash_amount_raw) if cash_amount_raw else 0
            mpesa_amount = int(mpesa_amount_raw) if mpesa_amount_raw else 0
        except:
            cash_amount = 0
            mpesa_amount = 0

        if not reg or not staff_id or not vehicle_type_id or not selected_services:
            flash("Please fill all required fields and select at least one service.", "danger")
            cursor.close()
            conn.close()
            return render_template("record_wash.html", staff=staff, vehicle_types=vehicle_types)

        total = 0
        service_details = []
        for sid in selected_services:
            cursor.execute("""
                SELECT s.service_id, s.name, s.commission_rule, p.amount
                FROM prices p
                JOIN services s ON p.service_id = s.service_id
                WHERE p.vehicle_type_id = %s AND s.service_id = %s
                  AND s.shop_id = %s
            """, (vehicle_type_id, sid, shop_id))
            row = cursor.fetchone()
            if row:
                total += row["amount"]
                service_details.append(row)

        if not service_details:
            flash("No valid services selected or prices not set.", "danger")
            cursor.close()
            conn.close()
            return render_template("record_wash.html", staff=staff, vehicle_types=vehicle_types)

        if cash_amount + mpesa_amount != total:
            flash(f"Cash + M-Pesa must equal the Total (KSh {total}). You entered KSh {cash_amount + mpesa_amount}.", "danger")
            cursor.close()
            conn.close()
            return render_template("record_wash.html", staff=staff, vehicle_types=vehicle_types)

        if cash_amount > 0 and mpesa_amount > 0:
            payment_method = "Mixed"
        elif mpesa_amount > 0:
            payment_method = "M-Pesa"
        else:
            payment_method = "Cash"

        kenya_today = get_kenya_today()
        kenya_time = get_kenya_time()

        try:
            cursor.execute("""
                INSERT INTO washes 
                (registration_number, staff_id, vehicle_type_id, total_amount,
                 payment_method, cash_amount, mpesa_amount, status, wash_date, wash_time, shop_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING wash_id
            """, (reg, staff_id, vehicle_type_id, total, payment_method,
                  cash_amount, mpesa_amount, "completed", kenya_today, kenya_time, shop_id))

            wash_id = cursor.fetchone()["wash_id"]

            for s in service_details:
                commission = compute_commission(s.get("commission_rule", "standard"), s["amount"])
                cursor.execute("""
                    INSERT INTO wash_services (wash_id, service_id, amount, commission_amount)
                    VALUES (%s, %s, %s, %s)
                """, (wash_id, s["service_id"], s["amount"], commission))

            conn.commit()
            cursor.close()
            conn.close()
            return redirect(url_for("view_receipt", wash_id=wash_id))

        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            flash("Something went wrong while saving the wash. Please try again.", "danger")
            print(f"Error saving wash: {e}")
            return render_template("record_wash.html", staff=staff, vehicle_types=vehicle_types)

    cursor.close()
    conn.close()
    return render_template("record_wash.html", staff=staff, vehicle_types=vehicle_types)

@app.route("/receipt/<int:wash_id>")
def view_receipt(wash_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT w.*, s.full_name as staff_name, vt.name as vehicle_name
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.wash_id = %s
    """, (wash_id,))
    wash = cursor.fetchone()

    if not wash:
        cursor.close()
        conn.close()
        flash("Wash not found.", "danger")
        return redirect(url_for("record_wash"))

    cursor.execute("""
        SELECT s.name, ws.amount
        FROM wash_services ws
        JOIN services s ON ws.service_id = s.service_id
        WHERE ws.wash_id = %s
        ORDER BY ws.wash_service_id
    """, (wash_id,))
    services = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("receipt.html",
                           wash_id=wash["wash_id"],
                           reg=wash["registration_number"],
                           staff_name=wash["staff_name"],
                           vehicle_name=wash["vehicle_name"],
                           services=services,
                           total=wash["total_amount"],
                           payment_method=wash["payment_method"],
                           wash_date=wash["wash_date"],
                           wash_time=wash["wash_time"])

# ====================== TODAY'S WASHES ======================
@app.route("/todays-washes")
def todays_washes():
    if "user_id" not in session:
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT w.wash_id, w.registration_number, w.wash_time, w.total_amount,
               w.payment_method, w.cash_amount, w.mpesa_amount, w.status,
               s.full_name as staff_name, vt.name as vehicle_name
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.wash_date = %s AND w.shop_id = %s
        ORDER BY w.wash_id DESC
    """, (today, shop_id))

    washes = cursor.fetchall()
    cursor.close()
    conn.close()

    total_amount = sum((w["total_amount"] or 0) for w in washes)
    total_cash = sum((w["cash_amount"] or 0) for w in washes)
    total_mpesa = sum((w["mpesa_amount"] or 0) for w in washes)
    total_cars = len(washes)

    return render_template(
        "todays_washes.html",
        washes=washes,
        today=today,
        total_amount=total_amount,
        total_cash=total_cash,
        total_mpesa=total_mpesa,
        total_cars=total_cars
    )

@app.route("/weekly-wash-history")
def weekly_wash_history():
    if "user_id" not in session:
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()

    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    start_param = request.args.get("start")
    if start_param:
        try:
            start_of_week = datetime.strptime(start_param, "%Y-%m-%d").date()
            end_of_week = start_of_week + timedelta(days=6)
        except:
            pass

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if session["role"] == "staff":
        staff_id = session.get("staff_id")
        cursor.execute("""
            SELECT w.wash_id, w.registration_number, w.wash_date, w.wash_time,
                   w.total_amount, w.payment_method, w.cash_amount, w.mpesa_amount,
                   vt.name as vehicle_name, s.full_name as staff_name
            FROM washes w
            JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
            JOIN staff s ON w.staff_id = s.staff_id
            WHERE w.staff_id = %s
              AND w.shop_id = %s
              AND w.wash_date BETWEEN %s AND %s
            ORDER BY w.wash_date DESC, w.wash_id DESC
        """, (staff_id, shop_id, start_of_week, end_of_week))
    else:
        cursor.execute("""
            SELECT w.wash_id, w.registration_number, w.wash_date, w.wash_time,
                   w.total_amount, w.payment_method, w.cash_amount, w.mpesa_amount,
                   vt.name as vehicle_name, s.full_name as staff_name
            FROM washes w
            JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
            JOIN staff s ON w.staff_id = s.staff_id
            WHERE w.shop_id = %s
              AND w.wash_date BETWEEN %s AND %s
            ORDER BY w.wash_date DESC, w.wash_id DESC
        """, (shop_id, start_of_week, end_of_week))

    washes = cursor.fetchall()
    total_amount = sum(w["total_amount"] or 0 for w in washes)
    total_cars = len(washes)
    cursor.close()
    conn.close()

    prev_week = (start_of_week - timedelta(days=7)).isoformat()
    next_week = (start_of_week + timedelta(days=7)).isoformat()

    return render_template(
        "weekly_wash_history.html",
        washes=washes,
        start_of_week=start_of_week,
        end_of_week=end_of_week,
        total_amount=total_amount,
        total_cars=total_cars,
        prev_week=prev_week,
        next_week=next_week,
        is_staff=(session["role"] == "staff"),
        hide_totals=(session["role"] == "cashier")
    )


# ====================== SEARCH ======================
@app.route("/search")
def search():
    if "user_id" not in session:
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    query = request.args.get("q", "").strip().upper()
    washes = []

    if query:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT w.wash_id, w.registration_number, w.wash_date, w.wash_time,
                   w.total_amount, w.payment_method, w.status,
                   s.full_name as staff_name, vt.name as vehicle_name
            FROM washes w
            JOIN staff s ON w.staff_id = s.staff_id
            JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
            WHERE w.shop_id = %s
              AND w.registration_number LIKE %s
            ORDER BY w.wash_id DESC
            LIMIT 50
        """, (shop_id, f"%{query}%"))
        washes = cursor.fetchall()
        cursor.close()
        conn.close()

    return render_template("search.html", washes=washes, query=query)

@app.route("/wash/<int:wash_id>")
def wash_details(wash_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT w.*, s.full_name as staff_name, vt.name as vehicle_name
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.wash_id = %s
    """, (wash_id,))
    wash = cursor.fetchone()

    if not wash:
        cursor.close()
        conn.close()
        flash("Wash not found.", "danger")
        return redirect(url_for("search"))

    cursor.execute("""
        SELECT s.name, ws.amount
        FROM wash_services ws
        JOIN services s ON ws.service_id = s.service_id
        WHERE ws.wash_id = %s
    """, (wash_id,))
    services = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("wash_details.html", wash=wash, services=services)

# ====================== REPORTS ======================
@app.route("/reports", methods=["GET", "POST"])
def reports():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    results = None
    grand_total = 0
    start_date = None
    end_date = None

    if request.method == "POST":
        start_date = request.form.get("start_date")
        end_date = request.form.get("end_date")

        if start_date and end_date:
            conn = get_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute("""
                SELECT
                    s.full_name,
                    COUNT(w.wash_id) as total_washes,
                    SUM(w.total_amount) as total_earned
                FROM washes w
                JOIN staff s ON w.staff_id = s.staff_id
                WHERE w.wash_date BETWEEN %s AND %s
                  AND w.shop_id = %s
                GROUP BY s.staff_id, s.full_name
                ORDER BY total_earned DESC
            """, (start_date, end_date, current_shop_id()))
            
            results = cursor.fetchall()
            grand_total = sum(r["total_earned"] for r in results) if results else 0
            cursor.close()
            conn.close()

    return render_template("reports.html", 
                           results=results, 
                           grand_total=grand_total,
                           start_date=start_date,
                           end_date=end_date)

@app.route("/export-report")
def export_report():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    start = request.args.get("start")
    end = request.args.get("end")
    fmt = request.args.get("format", "excel")

    if not start or not end:
        flash("Invalid date range", "danger")
        return redirect(url_for("reports"))

    conn = get_connection()
    query = """
        SELECT
            s.full_name as "Staff Name",
            COUNT(w.wash_id) as "Total Washes",
            SUM(w.total_amount) as "Total Earned (KSh)"
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        WHERE w.wash_date BETWEEN %s AND %s
          AND w.shop_id = %s
        GROUP BY s.staff_id, s.full_name
        ORDER BY SUM(w.total_amount) DESC
    """
    df = pd.read_sql_query(query, conn, params=(start, end, current_shop_id()))
    conn.close()

    if fmt == "csv":
        output = BytesIO()
        df.to_csv(output, index=False)
        output.seek(0)
        filename = f"Luxe_Report_{start}_to_{end}.csv"
        return send_file(output, mimetype="text/csv", as_attachment=True, download_name=filename)
    else:
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Staff Earnings")
        output.seek(0)
        filename = f"Luxe_Report_{start}_to_{end}.xlsx"
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename
        )

@app.route("/print-report")
def print_report():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    start = request.args.get("start")
    end = request.args.get("end")

    if not start or not end:
        flash("Invalid date range", "danger")
        return redirect(url_for("reports"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT 
            s.full_name,
            COUNT(w.wash_id) as total_washes,
            SUM(w.total_amount) as total_earned
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        WHERE w.wash_date BETWEEN %s AND %s
            AND w.shop_id = %s
        GROUP BY s.staff_id, s.full_name
        ORDER BY total_earned DESC
    """, (start, end ,current_shop_id()))
    
    results = cursor.fetchall()
    grand_total = sum(r["total_earned"] for r in results) if results else 0
    cursor.close()
    conn.close()

    return render_template("print_report.html",
                           results=results,
                           grand_total=grand_total,
                           start_date=start,
                           end_date=end)

@app.route("/daily-report")
def daily_report():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()
    selected_date = request.args.get("date") or today.isoformat()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT
            COUNT(*) as total_washes,
            COALESCE(SUM(total_amount), 0) as wash_revenue,
            COALESCE(SUM(cash_amount), 0) as wash_cash,
            COALESCE(SUM(mpesa_amount), 0) as wash_mpesa
        FROM washes
        WHERE wash_date = %s AND shop_id = %s
          AND (status = 'completed' OR status IS NULL)
    """, (selected_date, shop_id))
    wash_stats = cursor.fetchone()

    cursor.execute("""
        SELECT
            COUNT(*) as total_sales,
            COALESCE(SUM(selling_price), 0) as product_revenue,
            COALESCE(SUM(cash_amount), 0) as product_cash,
            COALESCE(SUM(mpesa_amount), 0) as product_mpesa
        FROM stock_movements
        WHERE movement_type = 'sale'
          AND created_at::date = %s
          AND shop_id = %s
    """, (selected_date, shop_id))
    product_stats = cursor.fetchone()

    total_revenue = (wash_stats["wash_revenue"] or 0) + (product_stats["product_revenue"] or 0)
    total_cash = (wash_stats["wash_cash"] or 0) + (product_stats["product_cash"] or 0)
    total_mpesa = (wash_stats["wash_mpesa"] or 0) + (product_stats["product_mpesa"] or 0)

    cursor.execute("""
        SELECT w.registration_number, s.full_name as staff_name, vt.name as vehicle_name,
               w.total_amount, w.payment_method, w.wash_time
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.wash_date = %s AND w.shop_id = %s
          AND (w.status = 'completed' OR w.status IS NULL)
        ORDER BY w.wash_id DESC
    """, (selected_date, shop_id))
    washes = cursor.fetchall()

    cursor.execute("""
        SELECT p.name as product_name, m.quantity, m.selling_price as amount,
               m.payment_method, u.full_name as sold_by, m.created_at
        FROM stock_movements m
        JOIN products p ON m.product_id = p.product_id
        LEFT JOIN users u ON m.created_by = u.user_id
        WHERE m.movement_type = 'sale'
          AND m.created_at::date = %s
          AND m.shop_id = %s
        ORDER BY m.created_at DESC
    """, (selected_date, shop_id))
    product_sales = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "daily_report.html",
        selected_date=selected_date,
        wash_stats=wash_stats,
        product_stats=product_stats,
        total_revenue=total_revenue,
        total_cash=total_cash,
        total_mpesa=total_mpesa,
        washes=washes,
        product_sales=product_sales
    )

# ====================== MANAGE STAFF ======================
@app.route("/manage-staff")
def manage_staff():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    shop_id = current_shop_id()
    shop_id = current_shop_id()
    cursor.execute("""
        SELECT * FROM staff WHERE shop_id = %s ORDER BY full_name
    """, (shop_id,))
    staff_list = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("manage_staff.html", staff_list=staff_list)

@app.route("/add-staff", methods=["POST"])
def add_staff():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    full_name = request.form.get("full_name", "").strip()
    phone = request.form.get("phone", "").strip() or None

    if full_name:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO staff (full_name, phone, is_active, shop_id)
        VALUES (%s, %s, 1, %s)
    """, (full_name, phone, current_shop_id()))
        conn.commit()
        cursor.close()
        conn.close()
        flash(f"Staff '{full_name}' added successfully!", "success")
    else:
        flash("Full name is required.", "danger")

    return redirect(url_for("manage_staff"))

@app.route("/deactivate-staff/<int:staff_id>")
def deactivate_staff(staff_id):
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE staff SET is_active = 0 WHERE staff_id = %s", (staff_id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Staff deactivated.", "info")
    return redirect(url_for("manage_staff"))

@app.route("/activate-staff/<int:staff_id>")
def activate_staff(staff_id):
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE staff SET is_active = 1 WHERE staff_id = %s", (staff_id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Staff activated.", "success")
    return redirect(url_for("manage_staff"))

# ====================== CHANGE PRICES ======================
@app.route("/change-prices", methods=["GET", "POST"])
def change_prices():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        vehicle_type_id = request.form.get("vehicle_type_id")
        service_id = request.form.get("service_id")
        amount = request.form.get("amount")

        if vehicle_type_id and service_id and amount:
            try:
                amount = int(amount)
                cursor.execute("""
                    INSERT INTO prices (vehicle_type_id, service_id, amount, shop_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (vehicle_type_id, service_id)
                    DO UPDATE SET amount = EXCLUDED.amount
                """, (vehicle_type_id, service_id, amount, shop_id))
                conn.commit()
                flash("Price saved successfully!", "success")
            except Exception as e:
                flash("Something went wrong. Please try again or contact the administrator.", "danger")
                print(f"Error: {e}")
        else:
            flash("All fields are required.", "danger")

    # Always load lists for the page (GET and after POST)
    cursor.execute("""
        SELECT vehicle_type_id, name FROM vehicle_types
        WHERE shop_id = %s
        ORDER BY vehicle_type_id
    """, (shop_id,))
    vehicle_types = cursor.fetchall()

    cursor.execute("""
        SELECT service_id, name FROM services
        WHERE shop_id = %s
        ORDER BY service_id
    """, (shop_id,))
    services = cursor.fetchall()

    cursor.execute("""
        SELECT vt.name as vehicle, s.name as service, p.amount
        FROM prices p
        JOIN vehicle_types vt ON p.vehicle_type_id = vt.vehicle_type_id
        JOIN services s ON p.service_id = s.service_id
        WHERE vt.shop_id = %s AND s.shop_id = %s
        ORDER BY vt.vehicle_type_id, s.service_id
    """, (shop_id, shop_id))
    prices = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "change_prices.html",
        vehicle_types=vehicle_types,
        services=services,
        prices=prices
    )

# ====================== MANAGE TYPES & SERVICES ======================
@app.route("/manage-types-services", methods=["GET", "POST"])
def manage_types_services():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_type":
            name = request.form.get("type_name", "").strip()
            if name:
                try:
                    cursor.execute("""
                        INSERT INTO vehicle_types (name, shop_id)
                        VALUES (%s, %s)
                    """, (name, current_shop_id()))
                    conn.commit()
                    flash(f"Vehicle type '{name}' added.", "success")
                except Exception as e:
                    flash(f"Error: {e}", "danger")

        elif action == "add_service":
            name = request.form.get("service_name", "").strip()
            rule = request.form.get("commission_rule", "standard")
            if name:
                try:
                    cursor.execute("""
                        INSERT INTO services (name, commission_rule, shop_id)
                        VALUES (%s, %s, %s)
                    """, (name, rule, shop_id))
                    conn.commit()
                    flash(f"Service '{name}' added.", "success")
                except Exception as e:
                    flash(f"Error: {e}", "danger")

    cursor.execute("""
        SELECT vehicle_type_id, name FROM vehicle_types
        WHERE shop_id = %s ORDER BY name
    """, (shop_id,))
    vehicle_types = cursor.fetchall()

    cursor.execute("""
        SELECT service_id, name, commission_rule FROM services
        WHERE shop_id = %s ORDER BY name
    """, (shop_id,))
    services = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "manage_types_services.html",
        vehicle_types=vehicle_types,
        services=services
    )

@app.route("/add-vehicle-type", methods=["POST"])
def add_vehicle_type():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO vehicle_types (name, shop_id) VALUES (%s, %s)", (name, current_shop_id()))
            conn.commit()
            flash(f"Vehicle type '{name}' added successfully!", "success")
        except Exception as e:
            conn.rollback()
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                flash(f"'{name}' already exists for your shop.", "danger")
            else:
                flash(f"Error: {e}", "danger")
        cursor.close()
        conn.close()
    else:
        flash("Name is required.", "danger")

    return redirect(url_for("manage_types_services"))

@app.route("/add-service", methods=["POST"])
def add_service():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO services (name, is_package, shop_id) VALUES (%s, 0, %s)", (name, current_shop_id()))
            conn.commit()
            flash(f"Service '{name}' added successfully!", "success")
        except Exception as e:
            conn.rollback()
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                flash(f"'{name}' already exists for your shop.", "danger")
            else:
                flash(f"Error: {e}", "danger")
        cursor.close()
        conn.close()
    else:
        flash("Name is required.", "danger")

    return redirect(url_for("manage_types_services"))

@app.route("/fix-vehicle-type-unique")
def fix_vehicle_type_unique():
    token = request.args.get("token")
    if token != "oshasmart-setup-2026":
        if "user_id" not in session or session.get("role") != "superadmin":
            return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    messages = []
    try:
        cursor.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'vehicle_types'::regclass AND contype = 'u'
        """)
        for row in cursor.fetchall():
            cursor.execute(f'ALTER TABLE vehicle_types DROP CONSTRAINT IF EXISTS "{row[0]}"')
            messages.append(f"Dropped vehicle_types: {row[0]}")

        cursor.execute("ALTER TABLE vehicle_types ADD COLUMN IF NOT EXISTS shop_id INTEGER DEFAULT 1")
        cursor.execute("""
            ALTER TABLE vehicle_types
            ADD CONSTRAINT vehicle_types_shop_name_unique UNIQUE (shop_id, name)
        """)
        messages.append("vehicle_types UNIQUE(shop_id, name) OK")

        cursor.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'services'::regclass AND contype = 'u'
        """)
        for row in cursor.fetchall():
            cursor.execute(f'ALTER TABLE services DROP CONSTRAINT IF EXISTS "{row[0]}"')
            messages.append(f"Dropped services: {row[0]}")

        cursor.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS shop_id INTEGER DEFAULT 1")
        cursor.execute("""
            ALTER TABLE services
            ADD CONSTRAINT services_shop_name_unique UNIQUE (shop_id, name)
        """)
        messages.append("services UNIQUE(shop_id, name) OK")

        conn.commit()
        result = "SUCCESS<br>" + "<br>".join(messages)
    except Exception as e:
        conn.rollback()
        result = f"Error: {e}"
    cursor.close()
    conn.close()
    return result
   
@app.route("/get-services/<int:vehicle_type_id>")
def get_services(vehicle_type_id):
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401

    shop_id = current_shop_id()
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT s.service_id, s.name, p.amount
        FROM prices p
        JOIN services s ON p.service_id = s.service_id
        JOIN vehicle_types vt ON p.vehicle_type_id = vt.vehicle_type_id
        WHERE p.vehicle_type_id = %s
          AND s.shop_id = %s
          AND vt.shop_id = %s
        ORDER BY s.name
    """, (vehicle_type_id, shop_id, shop_id))
    data = cursor.fetchall()
    cursor.close()
    conn.close()
    return list(data)


# ====================== INITIALIZE DB ON STARTUP ======================
@app.before_request
def before_first_request():
    if not getattr(app, "db_initialized", False):
        try:
            init_db()
            app.db_initialized = True
        except Exception as e:
            print(f"DB init error: {e}")

# ====================== RUN ======================

@app.route("/edit-wash/<int:wash_id>", methods=["GET", "POST"])
def edit_wash(wash_id):
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT * FROM washes WHERE wash_id = %s", (wash_id,))
    existing_wash = cursor.fetchone()
    if not existing_wash:
        cursor.close()
        conn.close()
        flash("Wash not found.", "danger")
        return redirect(url_for("search"))

    if request.method == "POST":
        reg = request.form.get("registration", "").strip().upper()
        staff_id = request.form.get("staff_id")
        vehicle_type_id = request.form.get("vehicle_type_id")
        payment_method = request.form.get("payment_method")
        extra_amount_raw = request.form.get("extra_amount", "").strip()
        extra_type = request.form.get("extra_type", "none")  # 'none' | 'payment' | 'tip'
        extra_note = request.form.get("extra_note", "").strip()

        try:
            extra_amount = int(extra_amount_raw) if extra_amount_raw else 0
        except ValueError:
            extra_amount = 0

        old_vehicle_type_id = str(existing_wash["vehicle_type_id"])
        vehicle_changed = str(vehicle_type_id) != old_vehicle_type_id

        try:
            # 1. Reprice existing (non-adjustment) services if the vehicle type changed
            cursor.execute("""
                SELECT ws.wash_service_id, ws.service_id, ws.amount, s.name, s.commission_rule
                FROM wash_services ws
                JOIN services s ON ws.service_id = s.service_id
                WHERE ws.wash_id = %s AND s.is_adjustment = 0
            """, (wash_id,))
            service_rows = cursor.fetchall()

            if vehicle_changed:
                for row in service_rows:
                    cursor.execute("""
                        SELECT amount FROM prices
                        WHERE vehicle_type_id = %s AND service_id = %s
                    """, (vehicle_type_id, row["service_id"]))
                    price_row = cursor.fetchone()
                    if price_row:
                        new_amount = price_row["amount"]
                        new_commission = compute_commission(row["commission_rule"], new_amount)
                        cursor.execute("""
                            UPDATE wash_services
                            SET amount = %s, commission_amount = %s
                            WHERE wash_service_id = %s
                        """, (new_amount, new_commission, row["wash_service_id"]))
                    else:
                        flash(f"No price set for '{row['name']}' under the new vehicle type — kept its previous price.", "danger")

            # 2. Remove any existing adjustment line (old Extra Payment / Staff Tip) for this wash
            cursor.execute("""
                DELETE FROM wash_services
                WHERE wash_id = %s AND service_id IN (
                    SELECT service_id FROM services WHERE is_adjustment = 1
                )
            """, (wash_id,))

            # 3. Insert the new adjustment line, if any
            if extra_amount > 0 and extra_type in ("payment", "tip"):
                service_name = "Extra Payment" if extra_type == "payment" else "Staff Tip"
                cursor.execute("SELECT service_id, commission_rule FROM services WHERE name = %s", (service_name,))
                adj_service = cursor.fetchone()
                if adj_service:
                    commission = compute_commission(adj_service["commission_rule"], extra_amount)
                    cursor.execute("""
                        INSERT INTO wash_services (wash_id, service_id, amount, commission_amount)
                        VALUES (%s, %s, %s, %s)
                    """, (wash_id, adj_service["service_id"], extra_amount, commission))

            # 4. Recompute total_amount from the actual line items — never typed directly
            cursor.execute("SELECT COALESCE(SUM(amount), 0) as total FROM wash_services WHERE wash_id = %s", (wash_id,))
            new_total = cursor.fetchone()["total"]

            note_parts = []
            if existing_wash.get("notes"):
                note_parts.append(existing_wash["notes"])
            if extra_note:
                note_parts.append(extra_note)
            combined_notes = " | ".join(note_parts) if note_parts else None

            cursor.execute("""
                UPDATE washes
                SET registration_number = %s,
                    staff_id = %s,
                    vehicle_type_id = %s,
                    payment_method = %s,
                    total_amount = %s,
                    notes = %s
                WHERE wash_id = %s
            """, (reg, staff_id, vehicle_type_id, payment_method, new_total, combined_notes, wash_id))

            conn.commit()
            flash("Wash updated successfully! Services, commissions, and total were recalculated.", "success")
            cursor.close()
            conn.close()
            return redirect(url_for("wash_details", wash_id=wash_id))
        except Exception as e:
            conn.rollback()
            flash(f"Error updating wash: {e}", "danger")

    # ---- GET (or POST that hit an error and fell through) ----
    cursor.execute("""
        SELECT w.*, s.full_name as staff_name, vt.name as vehicle_name
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.wash_id = %s
    """, (wash_id,))
    wash = cursor.fetchone()

    cursor.execute("""
        SELECT s.name, ws.amount, s.is_adjustment
        FROM wash_services ws
        JOIN services s ON ws.service_id = s.service_id
        WHERE ws.wash_id = %s
        ORDER BY s.is_adjustment, s.service_id
    """, (wash_id,))
    all_services = cursor.fetchall()
    current_services = [r for r in all_services if not r["is_adjustment"]]
    existing_extra = next((r for r in all_services if r["is_adjustment"]), None)

    cursor.execute("SELECT staff_id, full_name FROM staff WHERE is_active = 1 ORDER BY full_name")
    staff = cursor.fetchall()

    shop_id = current_shop_id()
    cursor.execute("""
        SELECT vehicle_type_id, name FROM vehicle_types
        WHERE shop_id = %s
        ORDER BY vehicle_type_id
    """, (shop_id,))
    vehicle_types = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("edit_wash.html", wash=wash, staff=staff, vehicle_types=vehicle_types,
                           current_services=current_services, existing_extra=existing_extra)


@app.route("/delete-wash/<int:wash_id>")
def delete_wash(wash_id):
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM wash_services WHERE wash_id = %s", (wash_id,))
        cursor.execute("DELETE FROM washes WHERE wash_id = %s", (wash_id,))
        conn.commit()
        flash("Wash deleted successfully!", "success")
    except Exception as e:
       flash("Something went wrong. Please try again or contact the administrator.", "danger")
       print(f"Error: {e}")   # this still logs the real error for you
    cursor.close()
    conn.close()

    return redirect(url_for("search"))

#============================== COMMISION==========================
@app.route("/setup-commission")
def setup_commission():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            ALTER TABLE wash_services 
            ADD COLUMN IF NOT EXISTS commission_amount INTEGER DEFAULT 0
        """)
        conn.commit()
        message = "Commission column added successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/commissions/daily")
def daily_commissions():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()
    selected_date = request.args.get("date") or today.isoformat()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT s.staff_id, s.full_name,
               COUNT(DISTINCT w.wash_id) as total_washes,
               COALESCE(SUM(ws.commission_amount), 0) as total_commission
        FROM staff s
        LEFT JOIN washes w ON s.staff_id = w.staff_id
            AND w.wash_date = %s AND w.shop_id = %s
        LEFT JOIN wash_services ws ON w.wash_id = ws.wash_id
        WHERE s.is_active = 1 AND s.shop_id = %s
        GROUP BY s.staff_id, s.full_name
        ORDER BY total_commission DESC
    """, (selected_date, shop_id, shop_id))

    results = cursor.fetchall()
    grand_total = sum(r["total_commission"] for r in results)
    cursor.close()
    conn.close()

    return render_template(
        "daily_commissions.html",
        results=results,
        selected_date=selected_date,
        grand_total=grand_total
    )


@app.route("/commissions/weekly", methods=["GET", "POST"])
def weekly_commissions():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    start_date = request.args.get("start") or request.form.get("start") or start_of_week.isoformat()
    end_date = request.args.get("end") or request.form.get("end") or end_of_week.isoformat()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Mark as Paid
    if request.method == "POST" and request.form.get("action") == "mark_paid":
        staff_ids = request.form.getlist("staff_ids")
        for sid in staff_ids:
            # Commission
            cursor.execute("""
                SELECT COALESCE(SUM(ws.commission_amount), 0) as total
                FROM washes w
                JOIN wash_services ws ON w.wash_id = ws.wash_id
                WHERE w.staff_id = %s AND w.wash_date BETWEEN %s AND %s
            """, (sid, start_date, end_date))
            commission = cursor.fetchone()["total"]

            # Advances
            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0) as total
                FROM staff_advances
                WHERE staff_id = %s AND advance_date BETWEEN %s AND %s
            """, (sid, start_date, end_date))
            advances = cursor.fetchone()["total"]

            net_pay = commission - advances
            if net_pay < 0:
                net_pay = 0

            cursor.execute("""
                SELECT 1 FROM commission_payments 
                WHERE staff_id = %s AND start_date = %s AND end_date = %s
            """, (sid, start_date, end_date))
            if not cursor.fetchone() and commission > 0:
                cursor.execute("""
                    INSERT INTO commission_payments (staff_id, start_date, end_date, total_amount, paid_by, shop_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (sid, start_date, end_date, net_pay, session.get("full_name"), shop_id))

                # Mark advances as deducted
                cursor.execute("""
                    UPDATE staff_advances
                    SET is_deducted = 1
                    WHERE staff_id = %s AND advance_date BETWEEN %s AND %s
                """, (sid, start_date, end_date))

        conn.commit()
        flash("Selected staff marked as Paid (advances deducted)!", "success")

    # Get data with advances
    shop_id = current_shop_id()

    cursor.execute("""
        SELECT 
            s.staff_id,
            s.full_name,
            COUNT(DISTINCT w.wash_id) as total_washes,
            COALESCE(SUM(ws.commission_amount), 0) as total_commission,
            COALESCE((
                SELECT SUM(a.amount) FROM staff_advances a 
                WHERE a.staff_id = s.staff_id 
                AND a.shop_id = %s
                AND a.advance_date BETWEEN %s AND %s
            ), 0) as total_advances,
            EXISTS (
                SELECT 1 FROM commission_payments cp 
                WHERE cp.staff_id = s.staff_id 
                AND cp.shop_id = %s
                AND cp.start_date = %s AND cp.end_date = %s
            ) as is_paid
        FROM staff s
        LEFT JOIN washes w ON s.staff_id = w.staff_id 
            AND w.shop_id = %s
            AND w.wash_date BETWEEN %s AND %s
        LEFT JOIN wash_services ws ON w.wash_id = ws.wash_id
        WHERE s.is_active = 1 AND s.shop_id = %s
        GROUP BY s.staff_id, s.full_name
        ORDER BY total_commission DESC
    """, (shop_id, start_date, end_date, shop_id, start_date, end_date,
        shop_id, start_date, end_date, shop_id))

    results = cursor.fetchall()

    # Add net_pay to each row
    for r in results:
        r["net_pay"] = max(0, (r["total_commission"] or 0) - (r["total_advances"] or 0))

    unpaid_total = sum(r["net_pay"] for r in results if not r["is_paid"])
    cursor.close()
    conn.close()

    return render_template("weekly_commissions.html",
                           results=results,
                           start_date=start_date,
                           end_date=end_date,
                           grand_total=unpaid_total)

@app.route("/staff-performance")
def staff_performance():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()
    start_date = request.args.get("start") or (today - timedelta(days=30)).isoformat()
    end_date = request.args.get("end") or today.isoformat()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT s.staff_id, s.full_name,
               COUNT(DISTINCT w.wash_id) as total_washes,
               COALESCE(SUM(ws.commission_amount), 0) as total_commission,
               COALESCE(SUM(w.total_amount), 0) as total_revenue
        FROM staff s
        LEFT JOIN washes w ON s.staff_id = w.staff_id
            AND w.shop_id = %s
            AND w.wash_date BETWEEN %s AND %s
        LEFT JOIN wash_services ws ON w.wash_id = ws.wash_id
        WHERE s.is_active = 1 AND s.shop_id = %s
        GROUP BY s.staff_id, s.full_name
        ORDER BY total_commission DESC
    """, (shop_id, start_date, end_date, shop_id))
    results = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template(
        "staff_performance.html",
        results=results,
        start_date=start_date,
        end_date=end_date
    )

@app.route("/setup-payments")
def setup_payments():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS commission_payments (
                payment_id SERIAL PRIMARY KEY,
                staff_id INTEGER REFERENCES staff(staff_id),
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                total_amount INTEGER NOT NULL,
                paid_on DATE DEFAULT CURRENT_DATE,
                paid_by TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        message = "Commission payments table created successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

#========================================= SECURITY ===================================
@app.route("/setup-security")
def setup_security():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS must_change_password INTEGER DEFAULT 0
        """)
        # Force the default admin to change password
        cursor.execute("""
            UPDATE users 
            SET must_change_password = 1 
            WHERE username = 'admin'
        """)
        conn.commit()
        message = "Security setup completed successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        current_password = request.form.get("current_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not current_password or not new_password or not confirm_password:
            flash("All fields are required.", "danger")
            return render_template("change_password.html")

        if new_password != confirm_password:
            flash("New passwords do not match.", "danger")
            return render_template("change_password.html")

        if len(new_password) < 6:
            flash("New password must be at least 6 characters.", "danger")
            return render_template("change_password.html")

        conn = get_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Verify current password
        cursor.execute("SELECT password_hash FROM users WHERE user_id = %s", (session["user_id"],))
        user = cursor.fetchone()

        if not user or user["password_hash"] != hash_password(current_password):
            cursor.close()
            conn.close()
            flash("Current password is incorrect.", "danger")
            return render_template("change_password.html")

        # Update password and clear the force-change flag
        cursor.execute("""
            UPDATE users 
            SET password_hash = %s, must_change_password = 0 
            WHERE user_id = %s
        """, (hash_password(new_password), session["user_id"]))
        conn.commit()
        cursor.close()
        conn.close()

        flash("Password changed successfully!", "success")

        if session["role"] == "admin":
            return redirect(url_for("dashboard"))
        else:
            return redirect(url_for("cashier_home"))

    return render_template("change_password.html")

@app.route("/setup-price-requests")
def setup_price_requests():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS price_change_requests (
                request_id SERIAL PRIMARY KEY,
                vehicle_type_id INTEGER REFERENCES vehicle_types(vehicle_type_id),
                service_id INTEGER REFERENCES services(service_id),
                current_amount INTEGER,
                requested_amount INTEGER NOT NULL,
                requested_by INTEGER REFERENCES users(user_id),
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                reviewed_by INTEGER REFERENCES users(user_id),
                reviewed_at TIMESTAMP,
                admin_note TEXT
            )
        """)
        conn.commit()
        message = "Price change requests table created successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/setup-wash-requests")
def setup_wash_requests():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wash_edit_requests (
                request_id SERIAL PRIMARY KEY,
                wash_id INTEGER REFERENCES washes(wash_id) ON DELETE CASCADE,
                extra_type TEXT NOT NULL CHECK(extra_type IN ('payment', 'tip')),
                extra_amount INTEGER NOT NULL,
                extra_note TEXT,
                requested_by INTEGER REFERENCES users(user_id),
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                reviewed_by INTEGER REFERENCES users(user_id),
                reviewed_at TIMESTAMP,
                admin_note TEXT
            )
        """)
        conn.commit()
        message = "Wash edit requests table created successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/request-extra/<int:wash_id>", methods=["GET", "POST"])
def request_extra(wash_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get wash details
    cursor.execute("""
        SELECT w.*, s.full_name as staff_name, vt.name as vehicle_name
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.wash_id = %s
    """, (wash_id,))
    wash = cursor.fetchone()

    if not wash:
        cursor.close()
        conn.close()
        flash("Wash not found.", "danger")
        return redirect(url_for("search"))

    if request.method == "POST":
        extra_type = request.form.get("extra_type")
        extra_amount = request.form.get("extra_amount", "0").strip()
        extra_note = request.form.get("extra_note", "").strip()

        try:
            extra_amount = int(extra_amount)
        except:
            extra_amount = 0

        if extra_type not in ("payment", "tip") or extra_amount <= 0:
            flash("Please select type and enter a valid amount.", "danger")
        else:
            cursor.execute("""
                INSERT INTO wash_edit_requests 
                (wash_id, extra_type, extra_amount, extra_note, requested_by)
                VALUES (%s, %s, %s, %s, %s)
            """, (wash_id, extra_type, extra_amount, extra_note, session["user_id"]))
            conn.commit()
            flash("Request submitted successfully! Waiting for Admin approval.", "success")
            cursor.close()
            conn.close()
            return redirect(url_for("search"))

    cursor.close()
    conn.close()
    return render_template("request_extra.html", wash=wash)

@app.route("/extra-approvals", methods=["GET", "POST"])
def extra_approvals():

    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        request_id = request.form.get("request_id")
        action = request.form.get("action")
        admin_note = request.form.get("admin_note", "").strip()

        cursor.execute("SELECT * FROM wash_edit_requests WHERE request_id = %s AND status = 'pending'", (request_id,))
        req = cursor.fetchone()

        if req:
            if action == "approve":
                # Apply the extra to the wash
                service_name = "Extra Payment" if req["extra_type"] == "payment" else "Staff Tip"
                cursor.execute("SELECT service_id, commission_rule FROM services WHERE name = %s", (service_name,))
                service = cursor.fetchone()

                if service:
                    commission = compute_commission(service["commission_rule"], req["extra_amount"])

                    cursor.execute("""
                        INSERT INTO wash_services (wash_id, service_id, amount, commission_amount)
                        VALUES (%s, %s, %s, %s)
                    """, (req["wash_id"], service["service_id"], req["extra_amount"], commission))

                    # Update wash total
                    cursor.execute("""
                        UPDATE washes 
                        SET total_amount = total_amount + %s
                        WHERE wash_id = %s
                    """, (req["extra_amount"], req["wash_id"]))

                cursor.execute("""
                    UPDATE wash_edit_requests
                    SET status = 'approved', reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP, admin_note = %s
                    WHERE request_id = %s
                """, (session["user_id"], admin_note, request_id))
                flash("Extra approved and applied to the wash!", "success")

            elif action == "reject":
                cursor.execute("""
                    UPDATE wash_edit_requests
                    SET status = 'rejected', reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP, admin_note = %s
                    WHERE request_id = %s
                """, (session["user_id"], admin_note, request_id))
                flash("Request rejected.", "info")

            conn.commit()

    # Get pending requests
    cursor.execute("""
        SELECT 
            r.*,
            w.registration_number,
            w.total_amount as current_total,
            s.full_name as staff_name,
            u.full_name as requested_by_name
        FROM wash_edit_requests r
        JOIN washes w ON r.wash_id = w.wash_id
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN users u ON r.requested_by = u.user_id
        WHERE r.status = 'pending'
        ORDER BY r.requested_at DESC
    """)
    pending = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("extra_approvals.html", pending=pending)

@app.route("/setup-split-payment")
def setup_split_payment():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            ALTER TABLE washes 
            ADD COLUMN IF NOT EXISTS cash_amount INTEGER DEFAULT 0
        """)
        cursor.execute("""
            ALTER TABLE washes 
            ADD COLUMN IF NOT EXISTS mpesa_amount INTEGER DEFAULT 0
        """)
        conn.commit()
        message = "Split payment columns added successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message



@app.route("/setup-staff-login")
def setup_staff_login():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Allow 'staff' role
        cursor.execute("""
            ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check
        """)
        cursor.execute("""
            ALTER TABLE users ADD CONSTRAINT users_role_check 
            CHECK (role IN ('admin', 'cashier', 'staff'))
        """)

        # Link user to a staff member
        cursor.execute("""
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS staff_id INTEGER REFERENCES staff(staff_id)
        """)

        conn.commit()
        message = "Staff login support added successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/create-staff-login/<int:staff_id>", methods=["GET", "POST"])
def create_staff_login(staff_id):
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT staff_id, full_name FROM staff WHERE staff_id = %s", (staff_id,))
    staff_member = cursor.fetchone()

    if not staff_member:
        cursor.close()
        conn.close()
        flash("Staff member not found.", "danger")
        return redirect(url_for("manage_staff"))

    # Check if login already exists
    cursor.execute("SELECT user_id FROM users WHERE staff_id = %s", (staff_id,))
    existing = cursor.fetchone()

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not username or not password:
            flash("Username and password are required.", "danger")
        elif existing:
            flash("This staff member already has a login account.", "danger")
        else:
            try:
                cursor.execute("""
                    INSERT INTO users (username, password_hash, full_name, role, staff_id)
                    VALUES (%s, %s, %s, 'staff', %s)
                """, (username, hash_password(password), staff_member["full_name"], staff_id))
                conn.commit()
                flash(f"Login created for {staff_member['full_name']} successfully!", "success")
                cursor.close()
                conn.close()
                return redirect(url_for("manage_staff"))
            except Exception as e:
                flash("Username already exists. Please choose another.", "danger")
                print(e)

    cursor.close()
    conn.close()
    return render_template("create_staff_login.html", staff=staff_member, existing=existing)

@app.route("/staff-dashboard")
def staff_dashboard():
    if "user_id" not in session or session["role"] != "staff":
        return redirect(url_for("login"))

    staff_id = session.get("staff_id")
    if not staff_id:
        flash("Your account is not linked to a staff member. Contact Admin.", "danger")
        return redirect(url_for("logout"))

    today = get_kenya_today() if 'get_kenya_today' in globals() else date.today()
    
    # Current week (Monday to Sunday)
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Today's washes
    cursor.execute("""
        SELECT w.wash_id, w.registration_number, vt.name as vehicle_name,
               w.total_amount, w.payment_method, w.wash_time
        FROM washes w
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.staff_id = %s AND w.wash_date = %s
        ORDER BY w.wash_id DESC
    """, (staff_id, today))
    today_washes = cursor.fetchall()

    # Today's commission
    cursor.execute("""
        SELECT COALESCE(SUM(ws.commission_amount), 0) as total
        FROM wash_services ws
        JOIN washes w ON ws.wash_id = w.wash_id
        WHERE w.staff_id = %s AND w.wash_date = %s
    """, (staff_id, today))
    today_commission = cursor.fetchone()["total"]

    # This week's commission
    cursor.execute("""
        SELECT COALESCE(SUM(ws.commission_amount), 0) as total
        FROM wash_services ws
        JOIN washes w ON ws.wash_id = w.wash_id
        WHERE w.staff_id = %s AND w.wash_date BETWEEN %s AND %s
    """, (staff_id, start_of_week, end_of_week))
    week_commission = cursor.fetchone()["total"]

    # Check if this week is already paid
    cursor.execute("""
        SELECT 1 FROM commission_payments 
        WHERE staff_id = %s AND start_date = %s AND end_date = %s
    """, (staff_id, start_of_week, end_of_week))
    is_paid = cursor.fetchone() is not None

    cursor.close()
    conn.close()

    return render_template("staff_dashboard.html",
                           today_washes=today_washes,
                           today_commission=today_commission,
                           week_commission=week_commission,
                           is_paid=is_paid,
                           today=today,
                           start_of_week=start_of_week,
                           end_of_week=end_of_week)

#==================================START WASH=====================
@app.route("/setup-pending-wash")
def setup_pending_wash():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            ALTER TABLE washes 
            ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'completed'
        """)
        # Make sure existing washes are marked as completed
        cursor.execute("""
            UPDATE washes SET status = 'completed' WHERE status IS NULL
        """)
        conn.commit()
        message = "Pending wash support added successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/start-wash", methods=["GET", "POST"])
def start_wash():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    shop_id = current_shop_id()
    cursor.execute("""
        SELECT staff_id, full_name FROM staff
        WHERE is_active = 1 AND shop_id = %s
        ORDER BY full_name
    """, (shop_id,))
    staff = cursor.fetchall()

    shop_id = current_shop_id()
    cursor.execute("""
        SELECT vehicle_type_id, name FROM vehicle_types
        WHERE shop_id = %s
        ORDER BY vehicle_type_id
    """, (shop_id,))
    vehicle_types = cursor.fetchall()

    if request.method == "POST":
        reg = request.form.get("registration", "").strip().upper()
        staff_id = request.form.get("staff_id")
        vehicle_type_id = request.form.get("vehicle_type_id") or request.form.get("vehicle_type_id_hidden")
        selected_services = request.form.getlist("services")

        if not reg or not staff_id or not vehicle_type_id or not selected_services:
            flash("Please fill all required fields and select at least one service.", "danger")
            cursor.close()
            conn.close()
            return render_template("start_wash.html", staff=staff, vehicle_types=vehicle_types)

        total = 0
        service_details = []
        for sid in selected_services:
            cursor.execute("""
                SELECT s.service_id, s.name, s.commission_rule, p.amount
                FROM prices p
                JOIN services s ON p.service_id = s.service_id
                WHERE p.vehicle_type_id = %s AND s.service_id = %s
            """, (vehicle_type_id, sid))
            row = cursor.fetchone()
            if row:
                total += row["amount"]
                service_details.append(row)

        if not service_details:
            flash("No valid services selected.", "danger")
            cursor.close()
            conn.close()
            return render_template("start_wash.html", staff=staff, vehicle_types=vehicle_types)

        try:
            shop_id = current_shop_id()
            kenya_today = get_kenya_today()
            kenya_time = get_kenya_time()

            cursor.execute("""
                INSERT INTO washes 
                (registration_number, staff_id, vehicle_type_id, total_amount, 
                payment_method, cash_amount, mpesa_amount, status, wash_date, wash_time, shop_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) 
                RETURNING wash_id
            """, (reg, staff_id, vehicle_type_id, total, "Pending", 0, 0,
                "pending", kenya_today, kenya_time, shop_id))
            
            wash_id = cursor.fetchone()["wash_id"]

            for s in service_details:
                commission = compute_commission(s["commission_rule"], s["amount"])
                cursor.execute("""
                    INSERT INTO wash_services (wash_id, service_id, amount, commission_amount)
                    VALUES (%s, %s, %s, %s)
                """, (wash_id, s["service_id"], s["amount"], commission))

            conn.commit()
            flash(f"Wash started successfully! (ID: {wash_id}) - Waiting for payment.", "success")
            cursor.close()
            conn.close()
            return redirect(url_for("pending_washes"))

        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            flash("Something went wrong. Please try again.", "danger")
            print(f"Error: {e}")
            return render_template("start_wash.html", staff=staff, vehicle_types=vehicle_types)

    cursor.close()
    conn.close()
    return render_template("start_wash.html", staff=staff, vehicle_types=vehicle_types)

@app.route("/pending-washes")
def pending_washes():
    if "user_id" not in session:
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT w.wash_id, w.registration_number, s.full_name as staff_name,
               vt.name as vehicle_name, w.total_amount, w.wash_time
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.status = 'pending' AND w.shop_id = %s
        ORDER BY w.wash_id ASC
    """, (shop_id,))
    pending = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("pending_washes.html", pending=pending)

@app.route("/checkout/<int:wash_id>", methods=["GET", "POST"])
def checkout(wash_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT w.*, s.full_name as staff_name, vt.name as vehicle_name
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.wash_id = %s AND w.status = 'pending'
    """, (wash_id,))
    wash = cursor.fetchone()

    if not wash:
        cursor.close()
        conn.close()
        flash("Wash not found or already paid.", "danger")
        return redirect(url_for("pending_washes"))

    if request.method == "POST":
        cash_raw = request.form.get("cash_amount", "0").strip()
        mpesa_raw = request.form.get("mpesa_amount", "0").strip()

        try:
            cash_amount = int(cash_raw) if cash_raw else 0
            mpesa_amount = int(mpesa_raw) if mpesa_raw else 0
        except:
            cash_amount = 0
            mpesa_amount = 0

        if cash_amount + mpesa_amount != wash["total_amount"]:
            flash(f"Cash + M-Pesa must equal KSh {wash['total_amount']}", "danger")
        else:
            if cash_amount > 0 and mpesa_amount > 0:
                payment_method = "Mixed"
            elif mpesa_amount > 0:
                payment_method = "M-Pesa"
            else:
                payment_method = "Cash"

            cursor.execute("""
                UPDATE washes 
                SET cash_amount = %s, mpesa_amount = %s, payment_method = %s, status = 'completed'
                WHERE wash_id = %s
            """, (cash_amount, mpesa_amount, payment_method, wash_id))
            conn.commit()
            cursor.close()
            conn.close()
            flash("Payment completed successfully!", "success")
            return redirect(url_for("view_receipt", wash_id=wash_id))

    cursor.close()
    conn.close()
    return render_template("checkout.html", wash=wash)

#========================== INVENTORY     ====================
@app.route("/setup-inventory")
def setup_inventory():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "inventory_management"):
        flash("Inventory is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Categories
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS product_categories (
                category_id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL
            )
        """)

        # Products
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                category_id INTEGER REFERENCES product_categories(category_id),
                cost_price INTEGER DEFAULT 0,
                selling_price INTEGER NOT NULL,
                stock_qty INTEGER DEFAULT 0,
                low_stock_level INTEGER DEFAULT 5,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Stock movements (in, out, adjustment, sale)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_movements (
                movement_id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES products(product_id),
                movement_type TEXT NOT NULL,  -- 'in', 'out', 'sale', 'adjustment'
                quantity INTEGER NOT NULL,
                unit_cost INTEGER DEFAULT 0,
                total_cost INTEGER DEFAULT 0,
                selling_price INTEGER DEFAULT 0,
                payment_method TEXT,
                cash_amount INTEGER DEFAULT 0,
                mpesa_amount INTEGER DEFAULT 0,
                reference TEXT,
                notes TEXT,
                created_by INTEGER REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Default categories
        categories = ["Beverages", "Car Care Products", "Accessories", "Chemicals"]
        for cat in categories:
            cursor.execute("""
                INSERT INTO product_categories (name) VALUES (%s)
                ON CONFLICT (name) DO NOTHING
            """, (cat,))

        conn.commit()
        message = "Inventory tables created successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/inventory/products", methods=["GET", "POST"])
def inventory_products():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "inventory_management"):
        flash("Inventory is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            category_id = request.form.get("category_id")
            cost_price = int(request.form.get("cost_price", 0) or 0)
            selling_price = int(request.form.get("selling_price", 0) or 0)
            stock_qty = int(request.form.get("stock_qty", 0) or 0)
            low_stock_level = int(request.form.get("low_stock_level", 5) or 5)

            image_url = None
            if "product_image" in request.files:
                f = request.files["product_image"]
                if f and f.filename:
                    image_url = save_product_image(f)

            try:
                cursor.execute("""
                    INSERT INTO products
                    (name, category_id, cost_price, selling_price, stock_qty, low_stock_level, image_url, shop_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (name, category_id, cost_price, selling_price, stock_qty, low_stock_level, image_url, shop_id))
                conn.commit()
                flash(f"Product '{name}' added!", "success")
            except Exception as e:
                flash("Error adding product.", "danger")
                print(e)

    cursor.execute("""
        SELECT category_id, name FROM product_categories
        WHERE shop_id = %s ORDER BY name
    """, (shop_id,))
    categories = cursor.fetchall()

    # If no categories for this shop, fall back to all (for Luxe migration)
    if not categories:
        cursor.execute("SELECT category_id, name FROM product_categories ORDER BY name")
        categories = cursor.fetchall()

    cursor.execute("""
        SELECT p.*, c.name as category_name
        FROM products p
        LEFT JOIN product_categories c ON p.category_id = c.category_id
        WHERE p.is_active = 1 AND p.shop_id = %s
        ORDER BY p.name
    """, (shop_id,))
    products = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template("inventory_products.html", products=products, categories=categories)



@app.route("/inventory/delete-product/<int:product_id>")
def delete_product(product_id):
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))
    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "inventory_management"):
        flash("Inventory is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE products SET is_active = 0 WHERE product_id = %s", (product_id,))
        conn.commit()
        flash("Product deleted successfully.", "success")
    except Exception as e:
        flash("Error deleting product.", "danger")
        print(e)
    cursor.close()
    conn.close()
    return redirect(url_for("inventory_products"))

@app.route("/inventory/stock-in", methods=["GET", "POST"])
def stock_in():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "inventory_management"):
        flash("Inventory is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        product_id = request.form.get("product_id")
        quantity = request.form.get("quantity", "0")
        unit_cost = request.form.get("unit_cost", "0")
        notes = request.form.get("notes", "").strip()

        try:
            quantity = int(quantity)
            unit_cost = int(unit_cost)
            total_cost = quantity * unit_cost

            # Update stock
            cursor.execute("""
                UPDATE products 
                SET stock_qty = stock_qty + %s,
                    cost_price = %s
                WHERE product_id = %s
            """, (quantity, unit_cost, product_id))

            # Record movement
            cursor.execute("""
                INSERT INTO stock_movements 
                (product_id, movement_type, quantity, unit_cost, total_cost, notes, created_by)
                VALUES (%s, 'in', %s, %s, %s, %s, %s)
            """, (product_id, quantity, unit_cost, total_cost, notes, session["user_id"]))

            conn.commit()
            flash("Stock added successfully!", "success")
        except Exception as e:
            flash("Error adding stock.", "danger")
            print(e)

    cursor.execute("""
        SELECT product_id, name, stock_qty 
        FROM products 
        WHERE is_active = 1 
        ORDER BY name
    """)
    products = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("stock_in.html", products=products)

@app.route("/inventory/sell", methods=["GET", "POST"])
def sell_product():
    if "user_id" not in session:
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "inventory_management"):
        flash("Inventory is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        product_id = request.form.get("product_id")
        quantity = request.form.get("quantity", "1")
        agreed_amount_raw = request.form.get("agreed_amount", "").strip()
        cash_amount = request.form.get("cash_amount", "0")
        mpesa_amount = request.form.get("mpesa_amount", "0")

        try:
            quantity = int(quantity)
            cash_amount = int(cash_amount or 0)
            mpesa_amount = int(mpesa_amount or 0)
            agreed_amount = int(agreed_amount_raw) if agreed_amount_raw else 0

            cursor.execute("""
                SELECT * FROM products
                WHERE product_id = %s AND shop_id = %s
            """, (product_id, shop_id))
            product = cursor.fetchone()

            if not product:
                flash("Product not found.", "danger")
            elif product["stock_qty"] < quantity:
                flash(f"Not enough stock. Only {product['stock_qty']} available.", "danger")
            else:
                min_total = product["selling_price"] * quantity
                if agreed_amount < min_total:
                    flash(f"Amount cannot be below KSh {min_total}.", "danger")
                elif cash_amount + mpesa_amount != agreed_amount:
                    flash(f"Cash + M-Pesa must equal KSh {agreed_amount}.", "danger")
                else:
                    if cash_amount > 0 and mpesa_amount > 0:
                        payment_method = "Mixed"
                    elif mpesa_amount > 0:
                        payment_method = "M-Pesa"
                    else:
                        payment_method = "Cash"

                    cursor.execute("""
                        UPDATE products SET stock_qty = stock_qty - %s
                        WHERE product_id = %s AND shop_id = %s
                    """, (quantity, product_id, shop_id))

                    cursor.execute("""
                        INSERT INTO stock_movements
                        (product_id, movement_type, quantity, selling_price, payment_method,
                        cash_amount, mpesa_amount, created_by, shop_id)
                        VALUES (%s, 'sale', %s, %s, %s, %s, %s, %s, %s)
                    """, (product_id, quantity, agreed_amount, payment_method,
                        cash_amount, mpesa_amount, session["user_id"], current_shop_id()))

                    conn.commit()
                    flash(f"Sold {quantity} x {product['name']} for KSh {agreed_amount}", "success")
                    cursor.close()
                    conn.close()
                    return redirect(url_for("sell_product"))
        except Exception as e:
            flash("Error processing sale. Please check the amounts.", "danger")
            print(e)

    cursor.execute("""
        SELECT product_id, name, selling_price, stock_qty
        FROM products
        WHERE is_active = 1 AND stock_qty > 0 AND shop_id = %s
        ORDER BY name
    """, (shop_id,))
    products = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("sell_product.html", products=products)

@app.route("/inventory/report")
def inventory_report():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "inventory_management"):
        flash("Inventory is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT p.name, c.name as category, p.cost_price, p.selling_price,
               p.stock_qty, p.low_stock_level,
               (p.stock_qty * p.cost_price) as stock_value
        FROM products p
        LEFT JOIN product_categories c ON p.category_id = c.category_id
        WHERE p.is_active = 1 AND p.shop_id = %s
        ORDER BY p.name
    """, (shop_id,))
    products = cursor.fetchall()

    total_items = sum(p["stock_qty"] for p in products)
    total_value = sum(p["stock_value"] or 0 for p in products)
    low_stock_count = sum(1 for p in products if p["stock_qty"] <= p["low_stock_level"])

    cursor.execute("""
        SELECT p.name, SUM(m.quantity) as qty_sold,
               SUM(m.selling_price) as revenue
        FROM stock_movements m
        JOIN products p ON m.product_id = p.product_id
        WHERE m.movement_type = 'sale'
          AND m.shop_id = %s
          AND m.created_at >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY p.name
        ORDER BY qty_sold DESC
    """, (shop_id,))
    recent_sales = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "inventory_report.html",
        products=products,
        total_items=total_items,
        total_value=total_value,
        low_stock_count=low_stock_count,
        recent_sales=recent_sales
    )

@app.route("/setup-product-image")
def setup_product_image():

    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "inventory_management"):
        flash("Inventory is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            ALTER TABLE products 
            ADD COLUMN IF NOT EXISTS image_url TEXT
        """)
        conn.commit()
        message = "Product image column added successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/inventory/my-sales")
def my_product_sales():
    if "user_id" not in session:
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "inventory_management"):
        flash("Inventory is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()
    selected_date = request.args.get("date") or today.isoformat()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if session["role"] == "admin":
        cursor.execute("""
            SELECT
                m.movement_id,
                m.created_at,
                p.name as product_name,
                m.quantity,
                m.selling_price as amount,
                m.payment_method,
                m.cash_amount,
                m.mpesa_amount,
                u.full_name as sold_by
            FROM stock_movements m
            JOIN products p ON m.product_id = p.product_id
            LEFT JOIN users u ON m.created_by = u.user_id
            WHERE m.movement_type = 'sale'
              AND m.shop_id = %s
              AND m.created_at::date = %s
            ORDER BY m.created_at DESC
        """, (shop_id, selected_date))
    else:
        cursor.execute("""
            SELECT
                m.movement_id,
                m.created_at,
                p.name as product_name,
                m.quantity,
                m.selling_price as amount,
                m.payment_method,
                m.cash_amount,
                m.mpesa_amount
            FROM stock_movements m
            JOIN products p ON m.product_id = p.product_id
            WHERE m.movement_type = 'sale'
              AND m.shop_id = %s
              AND m.created_by = %s
              AND m.created_at::date = %s
            ORDER BY m.created_at DESC
        """, (shop_id, session["user_id"], selected_date))

    sales = cursor.fetchall()
    total_sales = sum((s["amount"] or 0) for s in sales)
    cursor.close()
    conn.close()

    return render_template(
        "my_product_sales.html",
        sales=sales,
        selected_date=selected_date,
        total_sales=total_sales,
        is_admin=(session["role"] == "admin")
    )

#====================BACKUP DATABASE==========================

@app.route("/backup")
def backup_database():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    backup_data = {
        "backup_date": datetime.now().isoformat(),
        "system": "Luxe Carwash",
        "tables": {}
    }

    # List of important tables to backup
    tables = [
        "users", "staff", "vehicle_types", "services", "prices",
        "washes", "wash_services", "commission_payments",
        "products", "product_categories", "stock_movements",
        "price_change_requests", "wash_edit_requests"
    ]

    for table in tables:
        try:
            cursor.execute(f"SELECT * FROM {table}")
            rows = cursor.fetchall()
            # Convert to normal dicts so it can be JSON serialized
            backup_data["tables"][table] = [dict(row) for row in rows]
        except Exception as e:
            backup_data["tables"][table] = {"error": str(e)}

    cursor.close()
    conn.close()

    # Create downloadable JSON file
    output = BytesIO()
    output.write(json.dumps(backup_data, indent=2, default=str).encode("utf-8"))
    output.seek(0)

    filename = f"luxe_carwash_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    return send_file(
        output,
        mimetype="application/json",
        as_attachment=True,
        download_name=filename
    )


@app.route("/backup-page")
def backup_page():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))
    return render_template("backup.html")

@app.route("/restore-backup", methods=["GET", "POST"])
def restore_backup():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    message = None
    restored_counts = {}

    if request.method == "POST":
        if "backup_file" not in request.files:
            flash("No file selected.", "danger")
            return redirect(url_for("restore_backup"))

        file = request.files["backup_file"]
        if file.filename == "":
            flash("No file selected.", "danger")
            return redirect(url_for("restore_backup"))

        if not file.filename.endswith(".json"):
            flash("Please upload a valid .json backup file.", "danger")
            return redirect(url_for("restore_backup"))

        try:
            backup_data = json.load(file)
            tables_data = backup_data.get("tables", {})

            conn = get_connection()
            cursor = conn.cursor()

            # ===== STAFF =====
            if "staff" in tables_data and isinstance(tables_data["staff"], list):
                count = 0
                for row in tables_data["staff"]:
                    cursor.execute("SELECT 1 FROM staff WHERE staff_id = %s", (row.get("staff_id"),))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO staff (staff_id, full_name, phone, is_active, created_at)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (staff_id) DO NOTHING
                        """, (
                            row.get("staff_id"),
                            row.get("full_name"),
                            row.get("phone"),
                            row.get("is_active", 1),
                            row.get("created_at")
                        ))
                        count += 1
                restored_counts["staff"] = count

            # ===== VEHICLE TYPES =====
            if "vehicle_types" in tables_data and isinstance(tables_data["vehicle_types"], list):
                count = 0
                for row in tables_data["vehicle_types"]:
                    cursor.execute("SELECT 1 FROM vehicle_types WHERE vehicle_type_id = %s", (row.get("vehicle_type_id"),))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO vehicle_types (vehicle_type_id, name, description)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (vehicle_type_id) DO NOTHING
                        """, (
                            row.get("vehicle_type_id"),
                            row.get("name"),
                            row.get("description")
                        ))
                        count += 1
                restored_counts["vehicle_types"] = count

            # ===== SERVICES =====
            if "services" in tables_data and isinstance(tables_data["services"], list):
                count = 0
                for row in tables_data["services"]:
                    cursor.execute("SELECT 1 FROM services WHERE service_id = %s", (row.get("service_id"),))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO services (service_id, name, is_package, commission_rule, is_adjustment)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (service_id) DO NOTHING
                        """, (
                            row.get("service_id"),
                            row.get("name"),
                            row.get("is_package", 0),
                            row.get("commission_rule", "standard"),
                            row.get("is_adjustment", 0)
                        ))
                        count += 1
                restored_counts["services"] = count

            # ===== PRODUCTS =====
            if "products" in tables_data and isinstance(tables_data["products"], list):
                count = 0
                for row in tables_data["products"]:
                    cursor.execute("SELECT 1 FROM products WHERE product_id = %s", (row.get("product_id"),))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO products 
                            (product_id, name, category_id, cost_price, selling_price, stock_qty, low_stock_level, is_active, image_url, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (product_id) DO NOTHING
                        """, (
                            row.get("product_id"),
                            row.get("name"),
                            row.get("category_id"),
                            row.get("cost_price", 0),
                            row.get("selling_price", 0),
                            row.get("stock_qty", 0),
                            row.get("low_stock_level", 5),
                            row.get("is_active", 1),
                            row.get("image_url"),
                            row.get("created_at")
                        ))
                        count += 1
                restored_counts["products"] = count

            # ===== WASHES =====
            if "washes" in tables_data and isinstance(tables_data["washes"], list):
                count = 0
                for row in tables_data["washes"]:
                    cursor.execute("SELECT 1 FROM washes WHERE wash_id = %s", (row.get("wash_id"),))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO washes 
                            (wash_id, registration_number, staff_id, vehicle_type_id, wash_date, wash_time,
                             total_amount, payment_method, cash_amount, mpesa_amount, status, notes, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (wash_id) DO NOTHING
                        """, (
                            row.get("wash_id"),
                            row.get("registration_number"),
                            row.get("staff_id"),
                            row.get("vehicle_type_id"),
                            row.get("wash_date"),
                            row.get("wash_time"),
                            row.get("total_amount"),
                            row.get("payment_method", "Cash"),
                            row.get("cash_amount", 0),
                            row.get("mpesa_amount", 0),
                            row.get("status", "completed"),
                            row.get("notes"),
                            row.get("created_at")
                        ))
                        count += 1
                restored_counts["washes"] = count

            # ===== WASH SERVICES =====
            if "wash_services" in tables_data and isinstance(tables_data["wash_services"], list):
                count = 0
                for row in tables_data["wash_services"]:
                    cursor.execute("SELECT 1 FROM wash_services WHERE wash_service_id = %s", (row.get("wash_service_id"),))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO wash_services 
                            (wash_service_id, wash_id, service_id, amount, commission_amount)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (wash_service_id) DO NOTHING
                        """, (
                            row.get("wash_service_id"),
                            row.get("wash_id"),
                            row.get("service_id"),
                            row.get("amount"),
                            row.get("commission_amount", 0)
                        ))
                        count += 1
                restored_counts["wash_services"] = count

            conn.commit()
            cursor.close()
            conn.close()

            flash("Safe restore completed successfully!", "success")
            message = restored_counts

        except Exception as e:
            flash(f"Error restoring backup: {str(e)}", "danger")
            print(e)

    return render_template("restore_backup.html", restored_counts=message)

#==============================================ADVANCE FORM===============================================
@app.route("/setup-advances")
def setup_advances():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403
    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "staff_advances"):
        flash("Advance service is not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS staff_advances (
                advance_id SERIAL PRIMARY KEY,
                staff_id INTEGER REFERENCES staff(staff_id),
                amount INTEGER NOT NULL,
                advance_date DATE NOT NULL,
                notes TEXT,
                created_by INTEGER REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deducted INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        message = "Staff advances table created successfully!"
    except Exception as e:
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/advances", methods=["GET", "POST"])
def staff_advances():
    if "user_id" not in session or session["role"] not in ("admin", "cashier"):
        return redirect(url_for("login"))

    shop_id = current_shop_id()
    if not shop_has_feature(shop_id, "staff_advances"):
        flash("Advances are not enabled for this shop.", "danger")
        return redirect(url_for("dashboard"))

    
    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        staff_id = request.form.get("staff_id")
        amount = request.form.get("amount", "0").strip()
        notes = request.form.get("notes", "").strip()
        advance_date = request.form.get("advance_date") or today.isoformat()
        try:
            amount = int(amount)
            if amount > 0:
                cursor.execute("""
                    INSERT INTO staff_advances
                    (staff_id, amount, advance_date, notes, created_by, shop_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (staff_id, amount, advance_date, notes, session["user_id"], shop_id))
                conn.commit()
                flash("Advance recorded!", "success")
        except Exception as e:
            flash("Error recording advance.", "danger")
            print(e)

    cursor.execute("""
        SELECT staff_id, full_name FROM staff
        WHERE is_active = 1 AND shop_id = %s
        ORDER BY full_name
    """, (shop_id,))
    staff_list = cursor.fetchall()

    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    cursor.execute("""
        SELECT a.*, s.full_name as staff_name, u.full_name as created_by_name
        FROM staff_advances a
        JOIN staff s ON a.staff_id = s.staff_id
        LEFT JOIN users u ON a.created_by = u.user_id
        WHERE a.shop_id = %s AND a.advance_date BETWEEN %s AND %s
        ORDER BY a.advance_date DESC, a.advance_id DESC
    """, (shop_id, start_of_week, end_of_week))
    advances = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "staff_advances.html",
        staff_list=staff_list,
        advances=advances,
        today=today.isoformat(),
        start_of_week=start_of_week,
        end_of_week=end_of_week
    )

@app.route("/advances/edit/<int:advance_id>", methods=["GET", "POST"])
def edit_advance(advance_id):
    if "user_id" not in session or session["role"] not in ("admin", "cashier"):
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT a.*, s.full_name as staff_name
        FROM staff_advances a
        JOIN staff s ON a.staff_id = s.staff_id
        WHERE a.advance_id = %s
    """, (advance_id,))
    advance = cursor.fetchone()

    if not advance:
        cursor.close()
        conn.close()
        flash("Advance not found.", "danger")
        return redirect(url_for("staff_advances"))

    if request.method == "POST":
        amount = request.form.get("amount", "0").strip()
        notes = request.form.get("notes", "").strip()
        advance_date = request.form.get("advance_date")

        try:
            amount = int(amount)
            if amount <= 0:
                flash("Amount must be greater than zero.", "danger")
            else:
                cursor.execute("""
                    UPDATE staff_advances
                    SET amount = %s, notes = %s, advance_date = %s
                    WHERE advance_id = %s
                """, (amount, notes, advance_date, advance_id))
                conn.commit()
                flash("Advance updated successfully!", "success")
                cursor.close()
                conn.close()
                return redirect(url_for("staff_advances"))
        except Exception as e:
            flash("Error updating advance.", "danger")
            print(e)

    cursor.execute("SELECT staff_id, full_name FROM staff WHERE is_active = 1 ORDER BY full_name")
    staff_list = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("edit_advance.html", advance=advance, staff_list=staff_list)


@app.route("/advances/delete/<int:advance_id>")
def delete_advance(advance_id):
    if "user_id" not in session or session["role"] not in ("admin", "cashier"):
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM staff_advances WHERE advance_id = %s", (advance_id,))
        conn.commit()
        flash("Advance deleted successfully.", "success")
    except Exception as e:
        flash("Error deleting advance.", "danger")
        print(e)
    cursor.close()
    conn.close()
    return redirect(url_for("staff_advances"))

@app.route("/check-time")
def check_time():
    return {
        "kenya_now": str(get_kenya_now()),
        "kenya_today": str(get_kenya_today()),
        "kenya_time": str(get_kenya_time()),
        "utc_now": str(datetime.utcnow())
    }



#=================================================MULTISHOP==================================================================
@app.route("/setup-multishop")
def setup_multishop():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        # 1. Shops table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                shop_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                phone TEXT,
                location TEXT,
                is_active INTEGER DEFAULT 1,
                plan TEXT DEFAULT 'standard',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Seed Luxe as shop 1 (OshaSmart platform, Luxe is the first tenant)
        cursor.execute("""
            INSERT INTO shops (shop_id, name, slug, location, plan)
            VALUES (1, 'Luxe Carwash', 'luxe', 'Kenya', 'pro')
            ON CONFLICT (shop_id) DO NOTHING
        """)

        # Fix sequence if needed (PostgreSQL)
        cursor.execute("""
            SELECT setval(pg_get_serial_sequence('shops', 'shop_id'), 
                          COALESCE((SELECT MAX(shop_id) FROM shops), 1))
        """)

        conn.commit()
        message = "OshaSmart multi-shop foundation created. Luxe Carwash = Shop ID 1"
    except Exception as e:
        conn.rollback()
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/setup-shop-ids")
def setup_shop_ids():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    messages = []

    tables = [
        "users",
        "staff",
        "vehicle_types",
        "services",
        "prices",
        "washes",
        "products",
        "product_categories",
        "stock_movements",
        "staff_advances",
        "commission_payments",
        "price_change_requests",
        "wash_edit_requests",
    ]

    try:
        for table in tables:
            try:
                cursor.execute(f"""
                    ALTER TABLE {table}
                    ADD COLUMN IF NOT EXISTS shop_id INTEGER REFERENCES shops(shop_id) DEFAULT 1
                """)
                # Attach all existing rows to Luxe (shop 1)
                cursor.execute(f"""
                    UPDATE {table} SET shop_id = 1 WHERE shop_id IS NULL
                """)
                messages.append(f"OK: {table}")
            except Exception as e:
                messages.append(f"Skip/Error {table}: {e}")

        conn.commit()
        result = "shop_id added and existing data linked to Luxe (shop_id=1)<br>" + "<br>".join(messages)
    except Exception as e:
        conn.rollback()
        result = f"Error: {e}"
    cursor.close()
    conn.close()
    return result

@app.route("/setup-shop-id-wash-edit")
def setup_shop_id_wash_edit():
    if "user_id" not in session or session["role"] != "admin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            ALTER TABLE wash_edit_requests
            ADD COLUMN IF NOT EXISTS shop_id INTEGER REFERENCES shops(shop_id) DEFAULT 1
        """)
        cursor.execute("""
            UPDATE wash_edit_requests SET shop_id = 1 WHERE shop_id IS NULL
        """)
        conn.commit()
        message = "wash_edit_requests shop_id OK"
    except Exception as e:
        conn.rollback()
        message = f"Skip or error (table may not exist): {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/setup-shop-ids-fix")
def setup_shop_ids_fix():
    if "user_id" not in session or session["role"] != "admin":
        # Allow running even if login is broken: use a temporary secret key in URL
        # Or log in won't work — so we use a simple token check for recovery
        token = request.args.get("token")
        if token != "oshasmart-setup-2026":
            return "Unauthorized. Add ?token=oshasmart-setup-2026 to the URL", 403

    conn = get_connection()
    messages = []

    tables = [
        "users", "staff", "vehicle_types", "services", "prices", "washes",
        "products", "product_categories", "stock_movements", "staff_advances",
        "commission_payments", "wash_edit_requests"
    ]

    for table in tables:
        cursor = conn.cursor()
        try:
            cursor.execute(f"""
                ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS shop_id INTEGER DEFAULT 1
            """)
            cursor.execute(f"""
                UPDATE {table} SET shop_id = 1 WHERE shop_id IS NULL
            """)
            conn.commit()
            messages.append(f"OK: {table}")
        except Exception as e:
            conn.rollback()
            messages.append(f"Skip {table}: {e}")
        cursor.close()

    conn.close()
    return "<br>".join(messages)

@app.route("/fix-sales-shop-id")
def fix_sales_shop_id():
    if "user_id" not in session or session.get("role") != "superadmin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE stock_movements
            SET shop_id = 1
            WHERE shop_id IS NULL AND movement_type = 'sale'
        """)
        conn.commit()
        msg = f"Updated {cursor.rowcount} sales to Luxe (shop_id=1)"
    except Exception as e:
        conn.rollback()
        msg = str(e)
    cursor.close()
    conn.close()
    return msg

@app.route("/check-shop")
def check_shop():

    if "user_id" not in session:
        return "Not logged in"
    return {
        "user_id": session.get("user_id"),
        "username": session.get("username"),
        "role": session.get("role"),
        "shop_id": session.get("shop_id"),
        "full_name": session.get("full_name")
    }

@app.context_processor
def inject_shop():
    shop_name = "Luxe Carwash"
    subscription_status = "active"
    shop_id = session.get("shop_id")
    if shop_id:
        try:
            conn = get_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT name, COALESCE(subscription_status, 'active') as subscription_status FROM shops WHERE shop_id = %s",
                (shop_id,)
            )
            row = cur.fetchone()
            if row:
                shop_name = row["name"]
                subscription_status = row["subscription_status"]
            cur.close()
            conn.close()
        except Exception:
            pass
    return {
        "current_shop_name": shop_name,
        "platform_name": "OshaSmart",
        "shop_has_feature": shop_has_feature,
        "subscription_status": subscription_status,
    }

@app.route("/create-shop", methods=["GET", "POST"])
def create_shop():
    if "user_id" not in session or session.get("role") != "superadmin":
        flash("Only OshaSmart Super Admin can create shops.", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        slug = request.form.get("slug", "").strip().lower().replace(" ", "-")
        phone = request.form.get("phone", "").strip()
        location = request.form.get("location", "").strip()
        plan = request.form.get("plan", "standard")

        admin_username = request.form.get("admin_username", "").strip().lower()
        admin_password = request.form.get("admin_password", "").strip()
        admin_fullname = request.form.get("admin_fullname", "").strip()

        if not name or not slug or not admin_username or not admin_password or not admin_fullname:
            flash("Shop name, slug, and admin details are required.", "danger")
        else:
            conn = get_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cursor.execute("""
                    INSERT INTO shops (name, slug, phone, location, plan, is_active)
                    VALUES (%s, %s, %s, %s, %s, 1)
                    RETURNING shop_id
                """, (name, slug, phone, location, plan))
                new_shop_id = cursor.fetchone()["shop_id"]

                cursor.execute("""
                    INSERT INTO users (username, password_hash, full_name, role, is_active, shop_id)
                    VALUES (%s, %s, %s, 'admin', 1, %s)
                """, (admin_username, hash_password(admin_password), admin_fullname, new_shop_id))

                                # Seed default feature flags for the new shop
                default_features = [
                    "inventory_management",
                    "staff_commissions",
                    "staff_advances",
                    "product_sales",
                    "pending_wash",
                ]
                for feature in default_features:
                    cursor.execute("""
                        INSERT INTO shop_features (shop_id, feature_name, is_enabled)
                        VALUES (%s, %s, 1)
                        ON CONFLICT (shop_id, feature_name) DO NOTHING
                    """, (new_shop_id, feature))

                # NO copy of types/services/prices — shop starts empty
                conn.commit()
                flash(f"Shop '{name}' created (ID {new_shop_id}). Admin: {admin_username}. Shop starts empty.", "success")
                cursor.close()
                conn.close()
                return redirect(url_for("platform_dashboard"))
            except Exception as e:
                conn.rollback()
                cursor.close()
                conn.close()
                flash(f"Error creating shop: {e}", "danger")
                print(e)

    return render_template("create_shop.html")

@app.route("/shops", methods=["GET", "POST"])
def list_shops():
    if "user_id" not in session or session.get("role") != "superadmin":
        flash("Only OshaSmart Super Admin can can view all shops.", "danger")
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT s.*, 
               (SELECT COUNT(*) FROM users u WHERE u.shop_id = s.shop_id) as user_count
        FROM shops s
        ORDER BY s.shop_id
    """)
    shops = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("list_shops.html", shops=shops)

@app.route("/setup-shop-unique-names")
def setup_shop_unique_names():
    token = request.args.get("token")
    if token != "oshasmart-setup-2026":
        if "user_id" not in session or session.get("role") != "superadmin":
            return "Unauthorized. Use ?token=oshasmart-setup-2026", 403

    conn = get_connection()
    cursor = conn.cursor()
    messages = []

    try:
        # vehicle_types: drop global unique on name, add per-shop unique
        cursor.execute("""
            ALTER TABLE vehicle_types DROP CONSTRAINT IF EXISTS vehicle_types_name_key
        """)
        messages.append("Dropped vehicle_types_name_key")

        cursor.execute("""
            ALTER TABLE vehicle_types
            DROP CONSTRAINT IF EXISTS vehicle_types_shop_name_unique
        """)
        cursor.execute("""
            ALTER TABLE vehicle_types
            ADD CONSTRAINT vehicle_types_shop_name_unique UNIQUE (shop_id, name)
        """)
        messages.append("Added UNIQUE (shop_id, name) on vehicle_types")

        # services: same pattern
        cursor.execute("""
            ALTER TABLE services DROP CONSTRAINT IF EXISTS services_name_key
        """)
        messages.append("Dropped services_name_key (if existed)")

        cursor.execute("""
            ALTER TABLE services
            DROP CONSTRAINT IF EXISTS services_shop_name_unique
        """)
        cursor.execute("""
            ALTER TABLE services
            ADD CONSTRAINT services_shop_name_unique UNIQUE (shop_id, name)
        """)
        messages.append("Added UNIQUE (shop_id, name) on services")

        conn.commit()
        result = "OK<br>" + "<br>".join(messages)
    except Exception as e:
        conn.rollback()
        result = f"Error: {e}"
    cursor.close()
    conn.close()
    return result

@app.route("/cleanup-shop-2")
def cleanup_shop_2():
    if "user_id" not in session or session.get("role") != "superadmin":
        return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM wash_services WHERE wash_id IN (SELECT wash_id FROM washes WHERE shop_id = 2)")
        cursor.execute("DELETE FROM washes WHERE shop_id = 2")
        cursor.execute("DELETE FROM stock_movements WHERE shop_id = 2")
        cursor.execute("DELETE FROM products WHERE shop_id = 2")
        cursor.execute("DELETE FROM prices WHERE shop_id = 2")
        cursor.execute("DELETE FROM services WHERE shop_id = 2")
        cursor.execute("DELETE FROM vehicle_types WHERE shop_id = 2")
        cursor.execute("DELETE FROM staff_advances WHERE shop_id = 2")
        cursor.execute("DELETE FROM commission_payments WHERE shop_id = 2")
        cursor.execute("DELETE FROM staff WHERE shop_id = 2")
        conn.commit()
        msg = "Shop 2 cleared. Extreme admin can set up everything from zero."
    except Exception as e:
        conn.rollback()
        msg = f"Error: {e}"
    cursor.close()
    conn.close()
    return msg

@app.route("/platform/deactivate-shop/<int:shop_id>")
def deactivate_shop(shop_id):
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))
    if shop_id == 1:
        flash("Cannot deactivate primary shop (Luxe).", "danger")
        return redirect(url_for("platform_dashboard"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE shops SET is_active = 0 WHERE shop_id = %s", (shop_id,))
        cursor.execute("UPDATE users SET is_active = 0 WHERE shop_id = %s", (shop_id,))
        conn.commit()
        flash("Shop deactivated.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")
    cursor.close()
    conn.close()
    return redirect(url_for("platform_dashboard"))


@app.route("/platform/activate-shop/<int:shop_id>")
def activate_shop(shop_id):
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE shops SET is_active = 1 WHERE shop_id = %s", (shop_id,))
        cursor.execute("UPDATE users SET is_active = 1 WHERE shop_id = %s", (shop_id,))
        conn.commit()
        flash("Shop activated.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")
    cursor.close()
    conn.close()
    return redirect(url_for("platform_dashboard"))


@app.route("/platform/delete-shop/<int:shop_id>")
def delete_shop(shop_id):
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))
    if shop_id == 1:
        flash("Cannot delete primary shop (Luxe).", "danger")
        return redirect(url_for("platform_dashboard"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM wash_services WHERE wash_id IN (SELECT wash_id FROM washes WHERE shop_id = %s)", (shop_id,))
        cursor.execute("DELETE FROM washes WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM stock_movements WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM products WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM prices WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM services WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM vehicle_types WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM staff_advances WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM commission_payments WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM shop_features WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM staff WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM users WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM product_categories WHERE shop_id = %s", (shop_id,))
        cursor.execute("DELETE FROM shops WHERE shop_id = %s", (shop_id,))
        conn.commit()
        flash("Shop deleted permanently.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")
    cursor.close()
    conn.close()
    return redirect(url_for("platform_dashboard"))

@app.route("/platform/shop/<int:shop_id>")
def platform_shop_detail(shop_id):
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT * FROM shops WHERE shop_id = %s", (shop_id,))
    shop = cursor.fetchone()
    if not shop:
        cursor.close()
        conn.close()
        flash("Shop not found.", "danger")
        return redirect(url_for("platform_dashboard"))

    cursor.execute("""
        SELECT user_id, username, full_name, role, is_active, created_at
        FROM users WHERE shop_id = %s
        ORDER BY role, full_name
    """, (shop_id,))
    users = cursor.fetchall()

    cursor.execute("""
        SELECT feature_name, is_enabled FROM shop_features
        WHERE shop_id = %s ORDER BY feature_name
    """, (shop_id,))
    features = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template(
        "platform_shop_detail.html",
        shop=shop,
        users=users,
        features=features
    )


@app.route("/platform/shop/<int:shop_id>/edit", methods=["GET", "POST"])
def edit_shop(shop_id):
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM shops WHERE shop_id = %s", (shop_id,))
    shop = cursor.fetchone()
    if not shop:
        cursor.close()
        conn.close()
        flash("Shop not found.", "danger")
        return redirect(url_for("platform_dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        location = request.form.get("location", "").strip()
        plan = request.form.get("plan", "standard")
        subscription_status = request.form.get("subscription_status", "active")
        notes = request.form.get("notes", "").strip()

        cursor.execute("""
            UPDATE shops
            SET name = %s, phone = %s, location = %s, plan = %s,
                subscription_status = %s, notes = %s
            WHERE shop_id = %s
        """, (name, phone, location, plan, subscription_status, notes, shop_id))
        conn.commit()
        flash("Shop updated.", "success")
        cursor.close()
        conn.close()
        return redirect(url_for("platform_shop_detail", shop_id=shop_id))

    cursor.close()
    conn.close()
    return render_template("edit_shop.html", shop=shop)


@app.route("/platform/shop/<int:shop_id>/features", methods=["POST"])
def update_shop_features(shop_id):
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT feature_name FROM shop_features WHERE shop_id = %s", (shop_id,))
    all_features = [r[0] for r in cursor.fetchall()]

    for fname in all_features:
        enabled = 1 if request.form.get(f"feature_{fname}") else 0
        cursor.execute("""
            UPDATE shop_features SET is_enabled = %s
            WHERE shop_id = %s AND feature_name = %s
        """, (enabled, shop_id, fname))

    conn.commit()
    cursor.close()
    conn.close()
    flash("Features updated.", "success")
    return redirect(url_for("platform_shop_detail", shop_id=shop_id))

@app.route("/platform/enter-shop/<int:shop_id>")
def enter_shop(shop_id):
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM shops WHERE shop_id = %s AND is_active = 1", (shop_id,))
    shop = cursor.fetchone()
    if not shop:
        cursor.close()
        conn.close()
        flash("Shop not found or inactive.", "danger")
        return redirect(url_for("platform_dashboard"))

    cursor.execute("""
        SELECT user_id, username, full_name FROM users
        WHERE shop_id = %s AND role = 'admin' AND is_active = 1
        ORDER BY user_id LIMIT 1
    """, (shop_id,))
    admin = cursor.fetchone()
    cursor.close()
    conn.close()

    if not admin:
        flash("This shop has no active admin user.", "danger")
        return redirect(url_for("platform_dashboard"))

    # Save superadmin identity so we can return
    session["impersonator_id"] = session["user_id"]
    session["impersonator_name"] = session.get("full_name")
    session["user_id"] = admin["user_id"]
    session["username"] = admin["username"]
    session["full_name"] = admin["full_name"] + " (Support)"
    session["role"] = "admin"
    session["shop_id"] = shop_id

    flash(f"Entered {shop['name']} as support. Use Exit Shop to return.", "info")
    return redirect(url_for("dashboard"))


@app.route("/platform/exit-shop")
def exit_shop():
    if not session.get("impersonator_id"):
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT user_id, username, full_name, role
        FROM users WHERE user_id = %s
    """, (session["impersonator_id"],))
    sa = cursor.fetchone()
    cursor.close()
    conn.close()

    if not sa:
        session.clear()
        return redirect(url_for("login"))

    session.clear()
    session["user_id"] = sa["user_id"]
    session["username"] = sa["username"]
    session["full_name"] = sa["full_name"]
    session["role"] = "superadmin"
    session["shop_id"] = None
    session.permanent = True

    flash("Returned to OshaSmart platform.", "success")
    return redirect(url_for("platform_dashboard"))

@app.route("/setup-commission-config")
def setup_commission_config():
    token = request.args.get("token")
    if token != "oshasmart-setup-2026":
        if "user_id" not in session or session.get("role") not in ("admin", "superadmin"):
            return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    messages = []
    try:
        cursor.execute("""
            ALTER TABLE services
            ADD COLUMN IF NOT EXISTS commission_type TEXT DEFAULT 'percentage'
        """)
        cursor.execute("""
            ALTER TABLE services
            ADD COLUMN IF NOT EXISTS commission_value INTEGER DEFAULT 30
        """)
        messages.append("Columns added")

        # Backfill from old commission_rule
        cursor.execute("""
            UPDATE services
            SET commission_type = 'percentage', commission_value = 30
            WHERE COALESCE(commission_rule, 'standard') = 'standard'
               OR commission_rule IS NULL
        """)
        cursor.execute("""
            UPDATE services
            SET commission_type = 'flat', commission_value = 100
            WHERE commission_rule = 'flat100'
        """)
        cursor.execute("""
            UPDATE services
            SET commission_type = 'full', commission_value = 100
            WHERE commission_rule = 'full'
        """)
        cursor.execute("""
            UPDATE services
            SET commission_type = 'none', commission_value = 0
            WHERE commission_rule = 'none'
        """)
        messages.append("Backfill from commission_rule done")

        conn.commit()
        result = "SUCCESS<br>" + "<br>".join(messages)
    except Exception as e:
        conn.rollback()
        result = f"Error: {e}"
    cursor.close()
    conn.close()
    return result


#==============================================superadmin=====================================================
@app.route("/setup-superadmin")
def setup_superadmin():
    # Temporary token so this works even before superadmin exists
    token = request.args.get("token")
    if token != "oshasmart-setup-2026":
        if "user_id" not in session or session.get("role") not in ("admin", "superadmin"):
            return "Unauthorized. Use ?token=oshasmart-setup-2026", 403

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
        cursor.execute("""
            ALTER TABLE users ADD CONSTRAINT users_role_check
            CHECK (role IN ('superadmin', 'admin', 'cashier', 'staff'))
        """)

        # Create platform superadmin (not tied to a single shop)
        # shop_id can be NULL for superadmin
        cursor.execute("""
            ALTER TABLE users ALTER COLUMN shop_id DROP NOT NULL
        """)

        cursor.execute("""
            INSERT INTO users (username, password_hash, full_name, role, is_active, shop_id)
            VALUES (%s, %s, %s, 'superadmin', 1, NULL)
            ON CONFLICT (username) DO UPDATE
            SET role = 'superadmin', shop_id = NULL, password_hash = EXCLUDED.password_hash
        """, ("superadmin", hash_password("OshaSmart2026"), "OshaSmart Super Admin"))

        conn.commit()
        message = "Superadmin ready. Login: superadmin / OshaSmart2026 — CHANGE THIS PASSWORD after first login."
    except Exception as e:
        conn.rollback()
        message = f"Error: {e}"
    cursor.close()
    conn.close()
    return message

@app.route("/platform")
def platform_dashboard():
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))

    today = get_kenya_today() if "get_kenya_today" in globals() else date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT
            s.shop_id, s.name, s.slug, s.plan, s.location, s.is_active,
            COALESCE(s.subscription_status, 'active') as subscription_status,
            (SELECT COUNT(*) FROM users u WHERE u.shop_id = s.shop_id) as user_count,
            (SELECT COUNT(*) FROM staff st WHERE st.shop_id = s.shop_id AND st.is_active = 1) as staff_count,
            COALESCE((
                SELECT COUNT(*) FROM washes w
                WHERE w.shop_id = s.shop_id AND w.wash_date = %s
                  AND (w.status = 'completed' OR w.status IS NULL)
            ), 0) as today_washes,
            COALESCE((
                SELECT SUM(w.total_amount) FROM washes w
                WHERE w.shop_id = s.shop_id AND w.wash_date = %s
                  AND (w.status = 'completed' OR w.status IS NULL)
            ), 0) as today_wash_revenue,
            COALESCE((
                SELECT SUM(m.selling_price) FROM stock_movements m
                WHERE m.shop_id = s.shop_id AND m.movement_type = 'sale'
                  AND m.created_at::date = %s
            ), 0) as today_product_sales,
            COALESCE((
                SELECT SUM(w.total_amount) FROM washes w
                WHERE w.shop_id = s.shop_id
                  AND w.wash_date BETWEEN %s AND %s
                  AND (w.status = 'completed' OR w.status IS NULL)
            ), 0) as week_wash_revenue,
            COALESCE((
                SELECT SUM(m.selling_price) FROM stock_movements m
                WHERE m.shop_id = s.shop_id AND m.movement_type = 'sale'
                  AND m.created_at::date BETWEEN %s AND %s
            ), 0) as week_product_sales
        FROM shops s
        ORDER BY s.shop_id
    """, (today, today, today, week_start, week_end, week_start, week_end))

    shops = cursor.fetchall()
    for s in shops:
        s["today_total"] = (s["today_wash_revenue"] or 0) + (s["today_product_sales"] or 0)
        s["week_total"] = (s["week_wash_revenue"] or 0) + (s["week_product_sales"] or 0)

    platform_today = sum(s["today_total"] for s in shops)
    platform_week = sum(s["week_total"] for s in shops)
    platform_washes = sum(s["today_washes"] for s in shops)
    active_shops = sum(1 for s in shops if s["is_active"])

    cursor.close()
    conn.close()

    return render_template(
        "platform_dashboard.html",
        shops=shops,
        today=today,
        week_start=week_start,
        week_end=week_end,
        platform_today=platform_today,
        platform_week=platform_week,
        platform_washes=platform_washes,
        active_shops=active_shops
    )

@app.route("/setup-platform-tools")
def setup_platform_tools():
    token = request.args.get("token")
    if token != "oshasmart-setup-2026":
        if "user_id" not in session or session.get("role") != "superadmin":
            return "Unauthorized", 403

    conn = get_connection()
    cursor = conn.cursor()
    messages = []
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shop_features (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER REFERENCES shops(shop_id) ON DELETE CASCADE,
                feature_name TEXT NOT NULL,
                is_enabled INTEGER DEFAULT 1,
                UNIQUE(shop_id, feature_name)
            )
        """)
        messages.append("shop_features OK")

        cursor.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'standard'")
        cursor.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT 'active'")
        cursor.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS notes TEXT")
        messages.append("shops extra columns OK")

        # Default features for every existing shop
        features = [
            "inventory_management",
            "staff_commissions",
            "staff_advances",
            "product_sales",
            "pending_wash"
        ]
        cursor.execute("SELECT shop_id FROM shops")
        for (sid,) in cursor.fetchall():
            for f in features:
                cursor.execute("""
                    INSERT INTO shop_features (shop_id, feature_name, is_enabled)
                    VALUES (%s, %s, 1)
                    ON CONFLICT (shop_id, feature_name) DO NOTHING
                """, (sid, f))
        messages.append("default features seeded")

        conn.commit()
        result = "SUCCESS<br>" + "<br>".join(messages)
    except Exception as e:
        conn.rollback()
        result = f"Error: {e}"
    cursor.close()
    conn.close()
    return result

@app.route("/platform/reset-password/<int:shop_id>", methods=["GET", "POST"])
def reset_shop_admin_password(shop_id):
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT shop_id, name FROM shops WHERE shop_id = %s", (shop_id,))
    shop = cursor.fetchone()
    if not shop:
        cursor.close()
        conn.close()
        flash("Shop not found.", "danger")
        return redirect(url_for("platform_dashboard"))

    cursor.execute("""
        SELECT user_id, username, full_name
        FROM users
        WHERE shop_id = %s AND role = 'admin'
        ORDER BY user_id
    """, (shop_id,))
    admins = cursor.fetchall()

    if request.method == "POST":
        user_id = request.form.get("user_id")
        new_password = request.form.get("new_password", "").strip()
        if not user_id or len(new_password) < 6:
            flash("Select an admin and use a password of at least 6 characters.", "danger")
        else:
            cursor.execute("""
                UPDATE users
                SET password_hash = %s, must_change_password = 1
                WHERE user_id = %s AND shop_id = %s
            """, (hash_password(new_password), user_id, shop_id))
            conn.commit()
            flash("Password reset. Admin must change it on next login.", "success")
            cursor.close()
            conn.close()
            return redirect(url_for("platform_dashboard"))

    cursor.close()
    conn.close()
    return render_template("reset_shop_password.html", shop=shop, admins=admins)

@app.route("/platform/backup")
def platform_backup():
    if "user_id" not in session or session.get("role") != "superadmin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    backup_data = {
        "backup_date": datetime.now().isoformat(),
        "system": "OshaSmart Platform",
        "tables": {}
    }

    tables = [
        "shops", "shop_features", "users", "staff", "vehicle_types", "services",
        "prices", "washes", "wash_services", "products", "product_categories",
        "stock_movements", "staff_advances", "commission_payments"
    ]

    for table in tables:
        try:
            cursor.execute(f"SELECT * FROM {table}")
            rows = cursor.fetchall()
            backup_data["tables"][table] = [dict(row) for row in rows]
        except Exception as e:
            backup_data["tables"][table] = {"error": str(e)}

    cursor.close()
    conn.close()

    output = BytesIO()
    output.write(json.dumps(backup_data, indent=2, default=str).encode("utf-8"))
    output.seek(0)
    filename = f"oshasmart_platform_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    return send_file(
        output,
        mimetype="application/json",
        as_attachment=True,
        download_name=filename
    )

@app.context_processor
def inject_shop():
    shop_name = "Luxe Carwash"
    if session.get("shop_id"):
        try:
            conn = get_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT name FROM shops WHERE shop_id = %s", (session["shop_id"],))
            row = cur.fetchone()
            if row:
                shop_name = row["name"]
            cur.close()
            conn.close()
        except Exception:
            pass
    return {
        "current_shop_name": shop_name,
        "platform_name": "OshaSmart",
        "shop_has_feature": shop_has_feature,  # make helper available in templates
    }

@app.before_request
def check_subscription_lock():
    # Skip static, login, logout, platform, setup
    if request.endpoint in (
        None, "login", "logout", "static",
        "platform_dashboard", "create_shop", "list_shops",
        "platform_backup", "exit_shop", "change_password"
    ):
        return
    if request.endpoint and request.endpoint.startswith("platform"):
        return
    if request.endpoint and request.endpoint.startswith("setup"):
        return

    if session.get("role") == "superadmin":
        return
    if session.get("impersonator_id"):
        return  # support mode still works

    shop_id = session.get("shop_id")
    if shop_id and not shop_subscription_ok(shop_id):
        session.clear()
        flash("Your shop subscription needs attention. Contact OshaSmart.", "danger")
        return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)