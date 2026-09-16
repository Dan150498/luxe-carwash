from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from datetime import date, datetime, timedelta
import psycopg2
import psycopg2.extras
import hashlib
from datetime import date, datetime
import os
import pandas as pd
from io import BytesIO

app = Flask(__name__)
app.secret_key = "luxe_carwash_secret_key_2026"

# ====================== DATABASE ======================
DATABASE_URL = os.environ.get("DATABASE_URL")

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

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'cashier')),
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS staff (
        staff_id SERIAL PRIMARY KEY,
        full_name TEXT NOT NULL,
        phone TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vehicle_types (
        vehicle_type_id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        description TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS services (
        service_id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        is_package INTEGER DEFAULT 0,
        commission_rule TEXT DEFAULT 'standard',
        is_adjustment INTEGER DEFAULT 0
    );
    """)
    # Backfill for databases created before these columns existed
    cursor.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS commission_rule TEXT DEFAULT 'standard'")
    cursor.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS is_adjustment INTEGER DEFAULT 0")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS prices (
        price_id SERIAL PRIMARY KEY,
        vehicle_type_id INTEGER REFERENCES vehicle_types(vehicle_type_id),
        service_id INTEGER REFERENCES services(service_id),
        amount INTEGER NOT NULL,
        UNIQUE(vehicle_type_id, service_id)
    );
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
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS wash_services (
        wash_service_id SERIAL PRIMARY KEY,
        wash_id INTEGER REFERENCES washes(wash_id) ON DELETE CASCADE,
        service_id INTEGER REFERENCES services(service_id),
        amount INTEGER NOT NULL,
        commission_amount INTEGER DEFAULT 0
    );
    """)
    # Backfill for databases created before this column existed
    cursor.execute("ALTER TABLE wash_services ADD COLUMN IF NOT EXISTS commission_amount INTEGER DEFAULT 0")

    # Insert default admin if not exists
    cursor.execute("SELECT 1 FROM users WHERE username = %s", ("admin",))
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO users (username, password_hash, full_name, role)
            VALUES (%s, %s, %s, %s)
        """, ("admin", hash_password("admin123"), "System Administrator", "admin"))

        cursor.execute("""
            INSERT INTO users (username, password_hash, full_name, role)
            VALUES (%s, %s, %s, %s)
        """, ("cashier1", hash_password("cashier123"), "Cashier One", "cashier"))

    # Default vehicle types
    vehicles = ["Matatu", "5-seater", "7-seater", "31-seater Bus", "51-seater Bus", "Lorry"]
    for v in vehicles:
        cursor.execute("INSERT INTO vehicle_types (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (v,))

    # Default services: (name, is_package, commission_rule, is_adjustment)
    # commission_rule: 'standard' = 30%, 'flat100' = flat KSh100, 'full' = 100% (tips)
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
            INSERT INTO services (name, is_package, commission_rule, is_adjustment)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                commission_rule = EXCLUDED.commission_rule,
                is_adjustment = EXCLUDED.is_adjustment
        """, (name, is_pkg, rule, is_adj))

    conn.commit()
    cursor.close()
    conn.close()
    print("Database initialized successfully!")

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
            SELECT * FROM users 
            WHERE username = %s AND password_hash = %s AND is_active = 1
        """, (username, hashed))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user:
            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            flash(f"Welcome, {user['full_name']}!", "success")
            
            if user["role"] == "admin":
                return redirect(url_for("dashboard"))
            else:
                return redirect(url_for("cashier_home"))
        else:
            flash("Invalid username or password", "danger")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

# ====================== DASHBOARD ======================
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    today = date.today().isoformat()
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT 
            COUNT(*) as total_washes,
            COALESCE(SUM(total_amount), 0) as total_money,
            COALESCE(SUM(CASE WHEN payment_method = 'Cash' THEN total_amount ELSE 0 END), 0) as cash_total,
            COALESCE(SUM(CASE WHEN payment_method = 'M-Pesa' THEN total_amount ELSE 0 END), 0) as mpesa_total
        FROM washes
        WHERE wash_date = %s
    """, (today,))
    stats = cursor.fetchone()
    cursor.close()
    conn.close()

    return render_template("dashboard.html", stats=stats, today=today)

@app.route("/cashier")
def cashier_home():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("cashier_home.html")

# ====================== RECORD WASH ======================
@app.route("/get-services/<int:vehicle_type_id>")
def get_services(vehicle_type_id):
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT s.service_id, s.name, p.amount
        FROM prices p
        JOIN services s ON p.service_id = s.service_id
        WHERE p.vehicle_type_id = %s
        ORDER BY s.service_id
    """, (vehicle_type_id,))
    services = cursor.fetchall()
    cursor.close()
    conn.close()
    return services

@app.route("/record-wash", methods=["GET", "POST"])
def record_wash():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT staff_id, full_name FROM staff WHERE is_active = 1 ORDER BY full_name")
    staff = cursor.fetchall()

    cursor.execute("SELECT vehicle_type_id, name FROM vehicle_types ORDER BY vehicle_type_id")
    vehicle_types = cursor.fetchall()

    if request.method == "POST":
        reg = request.form.get("registration", "").strip().upper()
        staff_id = request.form.get("staff_id")
        vehicle_type_id = request.form.get("vehicle_type_id")
        payment_method = request.form.get("payment_method", "Cash")
        selected_services = request.form.getlist("services")

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
            """, (vehicle_type_id, sid))
            row = cursor.fetchone()
            if row:
                total += row["amount"]
                service_details.append(row)
            else:
                flash(f"Warning: no price is set for one of the selected services on this vehicle type — it was skipped and NOT charged. Please set it under Change Prices.", "danger")
        try:
            cursor.execute("""
                INSERT INTO washes (registration_number, staff_id, vehicle_type_id, total_amount, payment_method)
                VALUES (%s, %s, %s, %s, %s) RETURNING wash_id
            """, (reg, staff_id, vehicle_type_id, total, payment_method))
            
            wash_id = cursor.fetchone()["wash_id"]

            for s in service_details:
                commission = compute_commission(s["commission_rule"], s["amount"])

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
            flash(f"Error saving wash: {e}", "danger")
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

    today = date.today().isoformat()
    
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT w.wash_id, w.registration_number, s.full_name as staff_name, 
               vt.name as vehicle_name, w.total_amount, w.payment_method, w.wash_time
        FROM washes w
        JOIN staff s ON w.staff_id = s.staff_id
        JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
        WHERE w.wash_date = %s
        ORDER BY w.wash_id DESC
    """, (today,))
    
    washes = cursor.fetchall()
    total = sum(w["total_amount"] for w in washes) if washes else 0
    cursor.close()
    conn.close()

    return render_template("todays_washes.html", washes=washes, total=total, today=today)

# ====================== SEARCH ======================
@app.route("/search")
def search():
    if "user_id" not in session:
        return redirect(url_for("login"))

    query = request.args.get("q", "").strip().upper()
    washes = []

    if query:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT w.wash_id, w.registration_number, s.full_name as staff_name,
                   vt.name as vehicle_name, w.total_amount, w.payment_method, w.wash_date
            FROM washes w
            JOIN staff s ON w.staff_id = s.staff_id
            JOIN vehicle_types vt ON w.vehicle_type_id = vt.vehicle_type_id
            WHERE w.registration_number ILIKE %s
            ORDER BY w.wash_date DESC, w.wash_id DESC
        """, (f"%{query}%",))
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
                GROUP BY s.staff_id, s.full_name
                ORDER BY total_earned DESC
            """, (start_date, end_date))
            
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
        GROUP BY s.staff_id, s.full_name
        ORDER BY SUM(w.total_amount) DESC
    """
    df = pd.read_sql_query(query, conn, params=(start, end))
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
        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=filename)

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
        GROUP BY s.staff_id, s.full_name
        ORDER BY total_earned DESC
    """, (start, end))
    
    results = cursor.fetchall()
    grand_total = sum(r["total_earned"] for r in results) if results else 0
    cursor.close()
    conn.close()

    return render_template("print_report.html",
                           results=results,
                           grand_total=grand_total,
                           start_date=start,
                           end_date=end)

# ====================== MANAGE STAFF ======================
@app.route("/manage-staff")
def manage_staff():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT staff_id, full_name, phone, is_active FROM staff ORDER BY full_name")
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
        cursor.execute("INSERT INTO staff (full_name, phone) VALUES (%s, %s)", (full_name, phone))
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
                    INSERT INTO prices (vehicle_type_id, service_id, amount)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (vehicle_type_id, service_id) 
                    DO UPDATE SET amount = EXCLUDED.amount
                """, (vehicle_type_id, service_id, amount))
                conn.commit()
                flash("Price saved successfully!", "success")
            except Exception as e:
                flash(f"Error: {e}", "danger")
        else:
            flash("All fields are required.", "danger")

    cursor.execute("SELECT vehicle_type_id, name FROM vehicle_types ORDER BY vehicle_type_id")
    vehicle_types = cursor.fetchall()

    cursor.execute("SELECT service_id, name FROM services ORDER BY service_id")
    services = cursor.fetchall()

    cursor.execute("""
        SELECT vt.name as vehicle, s.name as service, p.amount
        FROM prices p
        JOIN vehicle_types vt ON p.vehicle_type_id = vt.vehicle_type_id
        JOIN services s ON p.service_id = s.service_id
        ORDER BY vt.vehicle_type_id, s.service_id
    """)
    prices = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("change_prices.html",
                           vehicle_types=vehicle_types,
                           services=services,
                           prices=prices)

# ====================== MANAGE TYPES & SERVICES ======================
@app.route("/manage-types-services")
def manage_types_services():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT vehicle_type_id, name FROM vehicle_types ORDER BY vehicle_type_id")
    vehicle_types = cursor.fetchall()

    cursor.execute("SELECT service_id, name FROM services ORDER BY service_id")
    services = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("manage_types_services.html",
                           vehicle_types=vehicle_types,
                           services=services)

@app.route("/add-vehicle-type", methods=["POST"])
def add_vehicle_type():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO vehicle_types (name) VALUES (%s)", (name,))
            conn.commit()
            flash(f"Vehicle type '{name}' added successfully!", "success")
        except Exception as e:
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
            cursor.execute("INSERT INTO services (name, is_package) VALUES (%s, 0)", (name,))
            conn.commit()
            flash(f"Service '{name}' added successfully!", "success")
        except Exception as e:
            flash(f"Error: {e}", "danger")
        cursor.close()
        conn.close()
    else:
        flash("Name is required.", "danger")

    return redirect(url_for("manage_types_services"))

   

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

    cursor.execute("SELECT vehicle_type_id, name FROM vehicle_types ORDER BY vehicle_type_id")
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
        flash(f"Error deleting wash: {e}", "danger")
    cursor.close()
    conn.close()

    return redirect(url_for("search"))


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

    selected_date = request.args.get("date") or date.today().isoformat()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT 
            s.staff_id,
            s.full_name,
            COUNT(DISTINCT w.wash_id) as total_washes,
            COALESCE(SUM(ws.commission_amount), 0) as total_commission
        FROM staff s
        LEFT JOIN washes w ON s.staff_id = w.staff_id AND w.wash_date = %s
        LEFT JOIN wash_services ws ON w.wash_id = ws.wash_id
        WHERE s.is_active = 1
        GROUP BY s.staff_id, s.full_name
        ORDER BY total_commission DESC
    """, (selected_date,))
    
    results = cursor.fetchall()
    grand_total = sum(r["total_commission"] for r in results)
    cursor.close()
    conn.close()

    return render_template("daily_commissions.html",
                           results=results,
                           selected_date=selected_date,
                           grand_total=grand_total)


@app.route("/commissions/weekly", methods=["GET", "POST"])
def weekly_commissions():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    start_date = request.args.get("start") or request.form.get("start") or start_of_week.isoformat()
    end_date = request.args.get("end") or request.form.get("end") or end_of_week.isoformat()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Handle Mark as Paid
    if request.method == "POST" and request.form.get("action") == "mark_paid":
        staff_ids = request.form.getlist("staff_ids")
        for sid in staff_ids:
            cursor.execute("""
                SELECT COALESCE(SUM(ws.commission_amount), 0) as total
                FROM washes w
                JOIN wash_services ws ON w.wash_id = ws.wash_id
                WHERE w.staff_id = %s AND w.wash_date BETWEEN %s AND %s
            """, (sid, start_date, end_date))
            total = cursor.fetchone()["total"]

            if total > 0:
                # Avoid duplicate payment for same period
                cursor.execute("""
                    SELECT 1 FROM commission_payments 
                    WHERE staff_id = %s AND start_date = %s AND end_date = %s
                """, (sid, start_date, end_date))
                if not cursor.fetchone():
                    cursor.execute("""
                        INSERT INTO commission_payments (staff_id, start_date, end_date, total_amount, paid_by)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (sid, start_date, end_date, total, session.get("full_name")))
        conn.commit()
        flash("Selected staff have been marked as Paid!", "success")

    # Get data + payment status
    cursor.execute("""
        SELECT 
            s.staff_id,
            s.full_name,
            COUNT(DISTINCT w.wash_id) as total_washes,
            COALESCE(SUM(ws.commission_amount), 0) as total_commission,
            EXISTS (
                SELECT 1 FROM commission_payments cp 
                WHERE cp.staff_id = s.staff_id 
                  AND cp.start_date = %s AND cp.end_date = %s
            ) as is_paid
        FROM staff s
        LEFT JOIN washes w ON s.staff_id = w.staff_id 
            AND w.wash_date BETWEEN %s AND %s
        LEFT JOIN wash_services ws ON w.wash_id = ws.wash_id
        WHERE s.is_active = 1
        GROUP BY s.staff_id, s.full_name
        ORDER BY total_commission DESC
    """, (start_date, end_date, start_date, end_date))
    
    results = cursor.fetchall()
    unpaid_total = sum(r["total_commission"] for r in results if not r["is_paid"])
    cursor.close()
    conn.close()

    return render_template("weekly_commissions.html",
                           results=results,
                           start_date=start_date,
                           end_date=end_date,
                           grand_total=unpaid_total)

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

@app.route("/performance")
def staff_performance():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    today = date.today()
    default_start = (today - timedelta(days=30)).isoformat()
    default_end = today.isoformat()

    start_date = request.args.get("start") or default_start
    end_date = request.args.get("end") or default_end

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ===== 1. Staff Performance =====
    cursor.execute("""
        SELECT 
            s.staff_id,
            s.full_name,
            COUNT(DISTINCT w.wash_id) AS total_washes,
            COALESCE(SUM(w.total_amount), 0) AS total_revenue,
            COALESCE(SUM(ws.commission_amount), 0) AS total_commission,
            CASE 
                WHEN COUNT(DISTINCT w.wash_id) > 0 
                THEN ROUND(COALESCE(SUM(w.total_amount), 0)::numeric / COUNT(DISTINCT w.wash_id), 0)
                ELSE 0 
            END AS avg_per_wash
        FROM staff s
        LEFT JOIN washes w ON s.staff_id = w.staff_id 
            AND w.wash_date BETWEEN %s AND %s
        LEFT JOIN wash_services ws ON w.wash_id = ws.wash_id
        WHERE s.is_active = 1
        GROUP BY s.staff_id, s.full_name
        ORDER BY total_revenue DESC
    """, (start_date, end_date))
    staff_results = cursor.fetchall()

    total_washes = sum(r["total_washes"] for r in staff_results)
    total_revenue = sum(r["total_revenue"] for r in staff_results)
    total_commission = sum(r["total_commission"] for r in staff_results)

    # ===== 2. Top Services =====
    cursor.execute("""
        SELECT 
            s.name AS service_name,
            COUNT(*) AS times_done,
            COALESCE(SUM(ws.amount), 0) AS total_revenue
        FROM wash_services ws
        JOIN services s ON ws.service_id = s.service_id
        JOIN washes w ON ws.wash_id = w.wash_id
        WHERE w.wash_date BETWEEN %s AND %s
        GROUP BY s.name
        ORDER BY times_done DESC
        LIMIT 10
    """, (start_date, end_date))
    top_services = cursor.fetchall()

    # ===== 3. Busiest Days =====
    cursor.execute("""
        SELECT 
            w.wash_date,
            TO_CHAR(w.wash_date, 'Day') AS day_name,
            COUNT(*) AS total_washes,
            COALESCE(SUM(w.total_amount), 0) AS total_revenue
        FROM washes w
        WHERE w.wash_date BETWEEN %s AND %s
        GROUP BY w.wash_date
        ORDER BY total_washes DESC
        LIMIT 10
    """, (start_date, end_date))
    busiest_days = cursor.fetchall()

    # ===== 4. Weekly Comparison (last 8 weeks) =====
    cursor.execute("""
        SELECT 
            DATE_TRUNC('week', w.wash_date)::date AS week_start,
            COUNT(*) AS total_washes,
            COALESCE(SUM(w.total_amount), 0) AS total_revenue
        FROM washes w
        WHERE w.wash_date >= %s - INTERVAL '56 days'
        GROUP BY week_start
        ORDER BY week_start DESC
        LIMIT 8
    """, (today,))
    weekly_comparison = cursor.fetchall()

    # ===== 5. Monthly Comparison (last 6 months) =====
    cursor.execute("""
        SELECT 
            TO_CHAR(w.wash_date, 'YYYY-MM') AS month,
            COUNT(*) AS total_washes,
            COALESCE(SUM(w.total_amount), 0) AS total_revenue
        FROM washes w
        WHERE w.wash_date >= %s - INTERVAL '180 days'
        GROUP BY month
        ORDER BY month DESC
        LIMIT 6
    """, (today,))
    monthly_comparison = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("staff_performance.html",
                           staff_results=staff_results,
                           top_services=top_services,
                           busiest_days=busiest_days,
                           weekly_comparison=weekly_comparison,
                           monthly_comparison=monthly_comparison,
                           start_date=start_date,
                           end_date=end_date,
                           total_washes=total_washes,
                           total_revenue=total_revenue,
                           total_commission=total_commission)
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)