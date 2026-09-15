from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
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
        is_package INTEGER DEFAULT 0
    );
    """)

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
        amount INTEGER NOT NULL
    );
    """)

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

    # Default services
    services = [
        ("General Wash", 0),
        ("General + Vacuum", 1),
        ("Vacuum", 0),
        ("Outside Wash", 0),
        ("Underwash", 0),
        ("Engine Steaming", 0),
        ("Carpet Wash", 0)
    ]
    for name, is_pkg in services:
        cursor.execute("INSERT INTO services (name, is_package) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", (name, is_pkg))

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
                SELECT s.service_id, s.name, p.amount
                FROM prices p
                JOIN services s ON p.service_id = s.service_id
                WHERE p.vehicle_type_id = %s AND s.service_id = %s
            """, (vehicle_type_id, sid))
            row = cursor.fetchone()
            if row:
                total += row["amount"]
                service_details.append(row)

        try:
            cursor.execute("""
                INSERT INTO washes (registration_number, staff_id, vehicle_type_id, total_amount, payment_method)
                VALUES (%s, %s, %s, %s, %s) RETURNING wash_id
            """, (reg, staff_id, vehicle_type_id, total, payment_method))
            
            wash_id = cursor.fetchone()["wash_id"]

            for s in service_details:
                cursor.execute("""
                    INSERT INTO wash_services (wash_id, service_id, amount)
                    VALUES (%s, %s, %s)
                """, (wash_id, s["service_id"], s["amount"]))

            conn.commit()

            cursor.execute("SELECT full_name FROM staff WHERE staff_id = %s", (staff_id,))
            staff_name = cursor.fetchone()["full_name"]

            cursor.execute("SELECT name FROM vehicle_types WHERE vehicle_type_id = %s", (vehicle_type_id,))
            vehicle_name = cursor.fetchone()["name"]

            cursor.close()
            conn.close()

            return render_template("receipt.html",
                                   wash_id=wash_id,
                                   reg=reg,
                                   staff_name=staff_name,
                                   vehicle_name=vehicle_name,
                                   services=service_details,
                                   total=total,
                                   payment_method=payment_method)

        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            flash(f"Error saving wash: {e}", "danger")
            return render_template("record_wash.html", staff=staff, vehicle_types=vehicle_types)

    cursor.close()
    conn.close()
    return render_template("record_wash.html", staff=staff, vehicle_types=vehicle_types)

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

    if request.method == "POST":
        reg = request.form.get("registration", "").strip().upper()
        staff_id = request.form.get("staff_id")
        vehicle_type_id = request.form.get("vehicle_type_id")
        payment_method = request.form.get("payment_method")
        total_amount = request.form.get("total_amount")

        try:
            cursor.execute("""
                UPDATE washes 
                SET registration_number = %s,
                    staff_id = %s,
                    vehicle_type_id = %s,
                    payment_method = %s,
                    total_amount = %s
                WHERE wash_id = %s
            """, (reg, staff_id, vehicle_type_id, payment_method, total_amount, wash_id))
            conn.commit()
            flash("Wash updated successfully!", "success")
            cursor.close()
            conn.close()
            return redirect(url_for("wash_details", wash_id=wash_id))
        except Exception as e:
            flash(f"Error: {e}", "danger")

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

    cursor.execute("SELECT staff_id, full_name FROM staff WHERE is_active = 1 ORDER BY full_name")
    staff = cursor.fetchall()

    cursor.execute("SELECT vehicle_type_id, name FROM vehicle_types ORDER BY vehicle_type_id")
    vehicle_types = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("edit_wash.html", wash=wash, staff=staff, vehicle_types=vehicle_types)


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

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)