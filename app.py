import csv
import io
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, Response, abort, redirect, render_template_string,
                   request, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

# Reject unexpectedly large form submissions (16 KB is plenty for 4 fields)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

# The secret key signs the login cookie. Always set SECRET_KEY on the server.
# Without it a random key is used, so everyone is logged out on restart.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Set COOKIE_SECURE=1 on HTTPS hosting so the cookie is never sent unencrypted
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE") == "1"

# Keep the database next to app.py, wherever the server is started from.
# The location can be changed without editing code by setting CLOUDCOST_DB.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.environ.get("CLOUDCOST_DB", os.path.join(BASE_DIR, "cloudcost.db"))

# Illustrative cost rates, not live Google Cloud prices
VM_RATE = 5
DB_RATE = 3

# Largest budget the form accepts (stops absurd values)
MAX_BUDGET = 10_000_000

# Simulated resource utilization for demonstration
RESOURCE_METRICS = {
    "cpu": 64,
    "memory": 72,
    "storage": 48
}

# Simulated readings over time, not real cloud measurements
RESOURCE_HISTORY = {
    "labels": ["10:00", "10:05", "10:10", "10:15",
               "10:20", "10:25", "10:30"],
    "cpu": [42, 48, 45, 58, 53, 68, 64],
    "memory": [55, 57, 60, 62, 66, 70, 72]
}


# ---------- Database ----------

@contextmanager
def get_db():
    """Open the database, commit on success, and always close it."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# Indian Standard Time (UTC+5:30, no daylight saving). Servers usually run
# on UTC, so we convert explicitly instead of trusting the server clock.
IST = timezone(timedelta(hours=5, minutes=30))


def now_text():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS simulations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vm_hours REAL NOT NULL,
                db_hours REAL NOT NULL,
                days INTEGER NOT NULL,
                budget REAL NOT NULL,
                total_cost REAL NOT NULL,
                created_at TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id)
            )
        """)
        # Older databases were created before logins existed: add the column.
        columns = [row["name"] for row in
                   conn.execute("PRAGMA table_info(simulations)")]
        if "user_id" not in columns:
            conn.execute("ALTER TABLE simulations ADD COLUMN "
                         "user_id INTEGER REFERENCES users(id)")


# ---------- Users ----------

def create_user(username, password):
    """Save a new user with a hashed password and return their id.
    Raises ValueError if the username is already taken."""
    try:
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (?, ?, ?)",
                (username, generate_password_hash(password), now_text()))
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("That username is already taken.") from None


def check_login(username, password):
    """Return the user (id and username) if the password is right, else None."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?",
                           (username,)).fetchone()
    if row and check_password_hash(row["password_hash"], password):
        return {"id": row["id"], "username": row["username"]}
    return None


# ---------- Estimates (each user only ever sees their own) ----------

def get_history(user_id):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM simulations
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 50
        """, (user_id,)).fetchall()
        return [dict(row) for row in rows]


def get_calculation(calc_id, user_id):
    """Return one of this user's saved estimates, or None if it does not
    exist or belongs to someone else."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM simulations WHERE id = ? AND user_id = ?",
            (calc_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def save_calculation(user_id, vm_hours, db_hours, days, budget, total_cost):
    """Save an estimate for this user and return its new id."""
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO simulations
            (vm_hours, db_hours, days, budget, total_cost, created_at, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (vm_hours, db_hours, days, budget, total_cost, now_text(), user_id))
        return cursor.lastrowid


# ---------- Reusable logic (easy to test) ----------

def calculate_costs(vm_hours, db_hours, days, budget):
    """Work out costs and budget status from already-validated numbers."""
    vm_cost = vm_hours * days * VM_RATE
    db_cost = db_hours * days * DB_RATE
    total_cost = vm_cost + db_cost
    return {
        "vm_cost": vm_cost,
        "db_cost": db_cost,
        "total_cost": total_cost,
        "budget_percent": (total_cost / budget) * 100,
        "remaining": budget - total_cost,
    }


def summarize_history(history):
    """Spending statistics for a user's saved estimates (newest first).
    Returns None when there is nothing saved yet."""
    if not history:
        return None
    costs = [item["total_cost"] for item in history]
    summary = {
        "count": len(costs),
        "average": sum(costs) / len(costs),
        "highest": max(costs),
        "lowest": min(costs),
        "over_budget": sum(1 for item in history
                           if item["total_cost"] > item["budget"]),
        "change": None,
        "change_percent": None,
    }
    if len(costs) >= 2:
        latest, previous = costs[0], costs[1]
        summary["change"] = latest - previous
        if previous > 0:
            summary["change_percent"] = (latest - previous) / previous * 100
    return summary


@app.template_filter("inr")
def format_inr(amount):
    """Format money the Indian way: 123456.5 -> ₹1,23,456.50"""
    sign = "-" if amount < 0 else ""
    whole, paise = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return f"{sign}₹{whole}.{paise}"


def short_time(timestamp):
    """Turn '2026-10-21 01:04:26' into '21 Oct 01:04' for chart labels."""
    try:
        return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").strftime("%d %b %H:%M")
    except ValueError:
        return timestamp


def _fmt(number):
    """Show 24 as 24, 0.01 as 0.01 and 10000000 as 10,000,000."""
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def parse_number(form, field, label, minimum, maximum, whole=False):
    """Read one form field and return a safe number, or raise ValueError
    with a clear message that can be shown to the user."""
    raw = (form.get(field) or "").strip()

    if raw == "":
        raise ValueError(f"{label} is required.")

    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{label} must be a number.") from None

    if not math.isfinite(value):
        raise ValueError(f"{label} must be a normal number.")

    if whole:
        if not value.is_integer():
            raise ValueError(f"{label} must be a whole number.")
        value = int(value)

    if not minimum <= value <= maximum:
        raise ValueError(
            f"{label} must be between {_fmt(minimum)} and {_fmt(maximum)}."
        )

    return value


# Shared by the dashboard and the login page
STYLES = Markup(r"""
        :root {
            color-scheme: light;
            --bg: #f4f7ff;
            --surface: #ffffff;
            --surface2: #eef2ff;
            --text: #18223b;
            --muted: #68748d;
            --border: #e1e7f2;
            --primary: #4f46e5;
            --primary-hover: #4338ca;
            --green: #15803d;
            --amber: #b45309;
            --red: #b91c1c;
            --shadow: 0 8px 30px rgba(31,41,80,.07);
        }

        body.dark {
            color-scheme: dark;
            --bg: #0b1120;
            --surface: #151e30;
            --surface2: #202c43;
            --text: #edf2ff;
            --muted: #a4b0c7;
            --border: #2b3952;
            --primary: #818cf8;
            --primary-hover: #a5b4fc;
            --green: #86efac;
            --amber: #fcd34d;
            --red: #fca5a5;
            --shadow: 0 8px 30px rgba(0,0,0,.15);
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: Arial, Helvetica, sans-serif;
            transition: background .2s, color .2s;
        }

        .container {
            max-width: 1250px;
            width: 92%;
            margin: 0 auto;
        }

        header {
            background: var(--surface);
            border-bottom: 1px solid var(--border);
            padding: 18px 0;
        }

        .header-inner {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }

        .brand {
            font-size: 23px;
            font-weight: 800;
            letter-spacing: -.5px;
        }

        .brand span {
            color: var(--primary);
        }

        .subtitle {
            color: var(--muted);
            font-size: 13px;
            margin-top: 5px;
        }

        button {
            border: 0;
            border-radius: 10px;
            background: var(--primary);
            color: white;
            padding: 12px 16px;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
        }

        button:hover {
            opacity: .9;
        }

        .secondary-button {
            background: var(--surface2);
            color: var(--text);
        }

        .header-actions {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }

        .header-actions form {
            margin: 0;
        }

        .user-name {
            font-size: 13px;
        }

        .export-button {
            display: inline-block;
            text-decoration: none;
            border-radius: 10px;
            background: var(--primary);
            color: white;
            padding: 10px 14px;
            font-size: 13px;
            font-weight: 700;
        }
        .export-button:hover { opacity: .9; }
        .history-heading {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            flex-wrap: wrap;
        }

        main {
            padding: 28px 0 45px;
        }

        .welcome {
            margin-bottom: 24px;
        }

        .welcome h1 {
            font-size: clamp(25px, 4vw, 34px);
            margin: 0 0 8px;
        }

        .welcome p {
            color: var(--muted);
            margin: 0;
            line-height: 1.6;
        }

        .metrics {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 16px;
            margin-bottom: 22px;
        }

        .metric, .panel {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 17px;
            box-shadow: var(--shadow);
            min-width: 0;
        }

        .metric {
            padding: 21px;
        }

        .metric-label {
            color: var(--muted);
            font-size: 13px;
            margin-bottom: 12px;
        }

        .metric-value {
            font-size: clamp(23px, 3vw, 30px);
            font-weight: 800;
            overflow-wrap: anywhere;
        }

        .metric-note {
            font-size: 12px;
            color: var(--muted);
            margin-top: 8px;
        }

        .layout {
            display: grid;
            grid-template-columns: minmax(260px, .8fr) minmax(0, 1.5fr);
            gap: 20px;
            align-items: start;
        }

        .panel {
            padding: 23px;
            margin-bottom: 20px;
        }

        .panel h2 {
            margin: 0 0 7px;
            font-size: 19px;
        }

        .panel-description {
            margin: 0 0 22px;
            font-size: 13px;
            color: var(--muted);
            line-height: 1.5;
        }

        .field {
            margin-bottom: 17px;
        }

        label {
            display: block;
            font-size: 13px;
            font-weight: 700;
            margin-bottom: 8px;
        }

        input {
            width: 100%;
            padding: 12px;
            background: var(--bg);
            color: var(--text);
            border: 1px solid var(--border);
            border-radius: 9px;
            font-size: 15px;
        }

        input:focus {
            outline: 2px solid var(--primary);
            outline-offset: 1px;
        }

        .full-button {
            width: 100%;
            margin-top: 4px;
        }

        .result {
            padding: 16px;
            border-radius: 12px;
            background: var(--surface2);
            margin-bottom: 18px;
        }

        .result strong {
            display: block;
            font-size: 27px;
            margin-top: 7px;
        }

        .muted {
            color: var(--muted);
        }

        .breakdown-row {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            padding: 13px 0;
            border-bottom: 1px solid var(--border);
            font-size: 14px;
        }

        .breakdown-row:last-child {
            border-bottom: 0;
        }

        .progress-track {
            height: 11px;
            background: var(--surface2);
            border-radius: 20px;
            overflow: hidden;
            margin: 13px 0;
        }

        .progress-fill {
            height: 100%;
            background: var(--primary);
            border-radius: 20px;
            transition: width .3s;
        }

        .progress-fill.good { background: #16a34a; }
        .progress-fill.warning-fill { background: #f59e0b; }
        .progress-fill.danger { background: #dc2626; }

        .status-message {
            margin-top: 14px;
            padding: 13px 15px;
            border-radius: 10px;
            font-size: 13px;
            font-weight: 700;
            line-height: 1.5;
            background: var(--surface2);
        }
        .status-message.good { color: var(--green); }
        .status-message.warning-status { color: var(--amber); }
        .status-message.danger { color: var(--red); }

        .warning {
            color: var(--red);
            font-size: 13px;
            font-weight: 700;
            margin-top: 10px;
        }

        .summary-grid {
            grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
            margin-bottom: 0;
        }

        .summary-grid .metric-value {
            font-size: clamp(20px, 2.4vw, 25px);
        }

        .trend-up { color: var(--red); }
        .trend-down { color: var(--green); }

        .charts-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 20px;
        }

        .chart-wrap {
            height: 290px;
            position: relative;
            width: 100%;
        }

        .history-wrap {
            width: 100%;
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            min-width: 620px;
            font-size: 13px;
        }

        th, td {
            text-align: left;
            padding: 13px 12px;
            border-bottom: 1px solid var(--border);
            white-space: nowrap;
        }

        th {
            color: var(--muted);
            font-weight: 700;
        }

        .empty {
            padding: 28px 10px;
            text-align: center;
            color: var(--muted);
            font-size: 14px;
        }

        .pill {
            display: inline-block;
            border-radius: 20px;
            padding: 6px 10px;
            background: var(--surface2);
            font-size: 12px;
            font-weight: 700;
        }

        footer {
            color: var(--muted);
            text-align: center;
            font-size: 12px;
            padding: 20px 0;
        }

        @media (max-width: 850px) {
            .layout {
                grid-template-columns: 1fr;
            }

            .charts-grid {
                grid-template-columns: 1fr;
            }
        }

        @media (max-width: 600px) {
            .container {
                width: 94%;
            }

            .metrics {
                grid-template-columns: 1fr;
            }

            .metric {
                padding: 17px;
            }

            .panel {
                padding: 17px;
            }

            main {
                padding-top: 20px;
            }

            .chart-wrap {
                height: 260px;
            }
        }
""")


PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CloudCost Watch</title>

    <!-- Pinned version so the charts do not change unexpectedly -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>

    <style>{{ styles }}</style>
</head>

<body>
<header>
    <div class="container header-inner">
        <div>
            <div class="brand">CloudCost <span>Watch</span></div>
            <div class="subtitle">
                Cloud cost planning and analytics dashboard
            </div>
        </div>
        <div class="header-actions">
            <span class="muted user-name">Signed in as <strong>{{ username }}</strong></span>
            <button type="button" id="themeToggle">🌙 Night mode</button>
            <form method="POST" action="{{ url_for('logout') }}">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <button type="submit" class="secondary-button">Sign out</button>
            </form>
        </div>
    </div>
</header>

<main class="container">
    <section class="welcome">
        <h1>Cloud cost dashboard</h1>
        <p>
            Estimate resource costs, track saved calculations,
            and monitor sample resource utilization.
        </p>
    </section>

    <!-- Existing financial summary cards -->
    <section class="metrics">
        <div class="metric">
            <div class="metric-label">Latest estimated cost</div>
            <div class="metric-value">
                {{ (total_cost if calculated else 0)|inr }}
            </div>
            <div class="metric-note">Latest calculation</div>
        </div>

        <div class="metric">
            <div class="metric-label">Budget remaining</div>
            <div class="metric-value">
                {{ (remaining if calculated else 0)|inr }}
            </div>
            <div class="metric-note">Estimated budget balance</div>
        </div>

        <div class="metric">
            <div class="metric-label">Saved estimates</div>
            <div class="metric-value">{{ history|length }}</div>
            <div class="metric-note">Latest 50 records</div>
        </div>
    </section>

    <!-- NEW: resource utilization cards -->
    <section class="panel">
        <h2>Resource Utilization</h2>
        <p class="panel-description">
            Simulated readings for demonstration only.
            These are not live cloud metrics.
        </p>

        <div class="metrics">
            {% for name, value in resource_metrics.items() %}
            <div class="metric">
                <div class="metric-label">
                    {{ name|upper if name == "cpu" else name|capitalize }} Utilization
                </div>

                <div class="metric-value">{{ value }}%</div>

                <div class="progress-track">
                    <div class="progress-fill"
                         style="width: {{ value }}%"></div>
                </div>

                <div class="metric-note">
                    {{ "High usage" if value >= 80 else "Normal usage" }}
                </div>
            </div>
            {% endfor %}
        </div>
    </section>

    <div class="layout">
        <section class="panel">
            <h2>Cost estimator</h2>
            <p class="panel-description">
                Enter resource usage and your budget to estimate costs.
            </p>

            <form method="POST" action="/">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <div class="field">
                    <label for="vm_hours">Virtual machine usage (hours/day)</label>
                    <input id="vm_hours" name="vm_hours" type="number"
                           min="0" max="24" step="0.5"
                           value="{{ vm_hours }}" required>
                </div>

                <div class="field">
                    <label for="db_hours">Database usage (hours/day)</label>
                    <input id="db_hours" name="db_hours" type="number"
                           min="0" max="24" step="0.5"
                           value="{{ db_hours }}" required>
                </div>

                <div class="field">
                    <label for="days">Duration (days)</label>
                    <input id="days" name="days" type="number"
                           min="1" max="31" step="1"
                           value="{{ days }}" required>
                </div>

                <div class="field">
                    <label for="budget">Budget (₹)</label>
                    <input id="budget" name="budget" type="number"
                           min="0.01" step="0.01"
                           value="{{ budget }}" required>
                </div>

                <button class="full-button" type="submit">
                    Calculate Cost
                </button>
            </form>

            {% if error %}
                <p class="warning">{{ error }}</p>
            {% endif %}

            <div style="margin-top:24px">
                <h2>Estimate summary</h2>

                {% if calculated %}
                    <div class="result">
                        <span class="muted">Total estimated cost</span>
                        <strong>{{ total_cost|inr }}</strong>
                    </div>

                    <div class="breakdown-row">
                        <span>Virtual machine</span>
                        <strong>{{ vm_cost|inr }}</strong>
                    </div>

                    <div class="breakdown-row">
                        <span>Database</span>
                        <strong>{{ db_cost|inr }}</strong>
                    </div>

                    <div class="breakdown-row">
                        <span>Budget used</span>
                        <strong>{{ "%.1f"|format(budget_percent) }}%</strong>
                    </div>

                    <div class="progress-track">
                        <div class="progress-fill {% if total_cost > budget %}danger{% elif budget_percent >= 80 %}warning-fill{% else %}good{% endif %}"
                             style="width: {{ [budget_percent, 100]|min }}%">
                        </div>
                    </div>

                    <div class="muted" style="font-size:13px">
                        Budget: {{ budget|inr }}
                    </div>

                    {% if total_cost > budget %}
                        <p class="warning">
                            Warning: your estimate exceeds the budget.
                        </p>
                    {% elif budget_percent >= 80 %}
                        <p class="warning">
                            Warning: you have used at least 80% of your budget.
                        </p>
                    {% endif %}
                {% else %}
                    <p class="empty">
                        Calculate a cost to see your estimate breakdown.
                    </p>
                {% endif %}
            </div>
        </section>

        <section>
            <!-- Existing charts -->
            <div class="charts-grid">
                <div class="panel">
                    <h2>Cost analytics</h2>
                    <p class="panel-description">
                        Visual breakdown of your latest estimate.
                    </p>

                    {% if calculated %}
                        <div class="chart-wrap">
                            <canvas id="costChart"></canvas>
                        </div>
                    {% else %}
                        <div class="empty">
                            Calculate a cost to display the breakdown.
                        </div>
                    {% endif %}
                </div>

                <div class="panel">
                    <h2>Historical cost comparison</h2>
                    <p class="panel-description">
                        Compare saved estimates over time.
                    </p>

                    {% if history_chart.labels %}
                        <div class="chart-wrap">
                            <canvas id="historyChart"></canvas>
                        </div>
                    {% else %}
                        <div class="empty">
                            Saved estimates will appear here after calculation.
                        </div>
                    {% endif %}
                </div>
            </div>

            <!-- NEW: CPU and memory line chart -->
            <div class="panel">
                <h2>CPU and Memory Monitoring</h2>
                <p class="panel-description">
                    Sample utilization readings over time.
                    This chart currently uses simulated data.
                </p>

                <div class="chart-wrap">
                    <canvas id="resourceChart"></canvas>
                </div>

                <p class="metric-note">
                    Demo data only — not connected to live cloud resources.
                </p>
            </div>

            <div class="panel">
                <h2>Budget monitoring</h2>
                <p class="panel-description">
                    Track how much of your planned budget the latest estimate uses.
                </p>

                {% if calculated %}
                    {% set safe_percent = [budget_percent, 100]|min %}
                    {% if total_cost > budget %}
                        {% set budget_class = "danger" %}
                        {% set status_class = "danger" %}
                        {% set status_text = "Over budget" %}
                    {% elif budget_percent >= 80 %}
                        {% set budget_class = "warning-fill" %}
                        {% set status_class = "warning-status" %}
                        {% set status_text = "Near budget limit" %}
                    {% else %}
                        {% set budget_class = "good" %}
                        {% set status_class = "good" %}
                        {% set status_text = "Within budget" %}
                    {% endif %}

                    <div class="breakdown-row">
                        <span>Budget used</span>
                        <strong>{{ "%.1f"|format(budget_percent) }}%</strong>
                    </div>
                    <div class="progress-track" role="progressbar"
                         aria-label="Budget used"
                         aria-valuenow="{{ [budget_percent, 100]|min|round|int }}"
                         aria-valuemin="0" aria-valuemax="100">
                        <div class="progress-fill {{ budget_class }}"
                             style="width: {{ safe_percent }}%"></div>
                    </div>

                    <div class="breakdown-row">
                        <span>Estimated spending</span>
                        <strong>{{ total_cost|inr }}</strong>
                    </div>
                    <div class="breakdown-row">
                        <span>Planned budget</span>
                        <strong>{{ budget|inr }}</strong>
                    </div>
                    <div class="breakdown-row">
                        <span>{% if remaining >= 0 %}Budget remaining{% else %}Amount over budget{% endif %}</span>
                        <strong>{{ (remaining if remaining >= 0 else -remaining)|inr }}</strong>
                    </div>

                    <div class="status-message {{ status_class }}">
                        {% if total_cost > budget %}
                            Alert: this estimate exceeds your budget by {{ (total_cost - budget)|inr }}.
                        {% elif budget_percent >= 80 %}
                            Warning: you have used at least 80% of your planned budget.
                        {% else %}
                            Good: your estimate is currently within the planned budget.
                        {% endif %}
                        <div style="margin-top:5px">Status: {{ status_text }}</div>
                    </div>
                {% else %}
                    <div class="empty">
                        Calculate a cost to see your budget usage, remaining balance, and alerts.
                    </div>
                {% endif %}
            </div>
        </section>
    </div>

    <section class="panel">
        <h2>Spending summary</h2>
        <p class="panel-description">
            Statistics across your last {{ history|length }} saved estimates.
        </p>

        {% if summary %}
            <div class="metrics summary-grid">
                <div class="metric">
                    <div class="metric-label">Average estimate</div>
                    <div class="metric-value">{{ summary.average|inr }}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Highest estimate</div>
                    <div class="metric-value">{{ summary.highest|inr }}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Lowest estimate</div>
                    <div class="metric-value">{{ summary.lowest|inr }}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Over-budget estimates</div>
                    <div class="metric-value">{{ summary.over_budget }} of {{ summary.count }}</div>
                    <div class="metric-note">
                        {{ "Review these plans" if summary.over_budget else "All within budget" }}
                    </div>
                </div>
                <div class="metric">
                    <div class="metric-label">Change vs previous estimate</div>
                    {% if summary.change is none %}
                        <div class="metric-value">—</div>
                        <div class="metric-note">Save another estimate to compare</div>
                    {% else %}
                        <div class="metric-value {{ 'trend-up' if summary.change > 0 else 'trend-down' if summary.change < 0 else '' }}">
                            {{ "+" if summary.change > 0 else "" }}{{ summary.change|inr }}
                        </div>
                        <div class="metric-note">
                            {% if summary.change_percent is not none %}
                                {{ "%+.1f"|format(summary.change_percent) }}% —
                            {% endif %}
                            {{ "costs went up" if summary.change > 0 else "costs went down" if summary.change < 0 else "no change" }}
                        </div>
                    {% endif %}
                </div>
            </div>
        {% else %}
            <div class="empty">Save an estimate to see your spending summary.</div>
        {% endif %}
    </section>

    <section class="panel">
        <div class="history-heading">
            <h2>Calculation history</h2>
            <a class="export-button" href="/export">⬇ Download CSV</a>
        </div>
        <p class="panel-description">
            Saved calculations from the local SQLite database. Export them as a CSV file for reports.
        </p>

        {% if history %}
            <div class="history-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Date and time (IST)</th>
                            <th>VM hours/day</th>
                            <th>DB hours/day</th>
                            <th>Days</th>
                            <th>Budget</th>
                            <th>Total estimate</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for item in history %}
                        <tr>
                            <td>{{ item.created_at }}</td>
                            <td>{{ item.vm_hours }}</td>
                            <td>{{ item.db_hours }}</td>
                            <td>{{ item.days }}</td>
                            <td>{{ item.budget|inr }}</td>
                            <td>
                                <strong>{{ item.total_cost|inr }}</strong>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        {% else %}
            <div class="empty">No calculations saved yet.</div>
        {% endif %}
    </section>

    <footer>
        CloudCost Watch · Student project · Sample rates and metrics
        are illustrative, not live cloud data.
    </footer>
</main>

<script>
    const themeToggle = document.getElementById("themeToggle");

    function updateChartTheme() {
        const textColor = getComputedStyle(document.body)
            .getPropertyValue("--text").trim();

        const gridColor = getComputedStyle(document.body)
            .getPropertyValue("--border").trim();

        [
            window.costChart,
            window.historyChart,
            window.resourceChart
        ].forEach(function(chart) {
            if (!chart || !chart.options) {
                return;
            }

            if (chart.options.plugins &&
                chart.options.plugins.legend &&
                chart.options.plugins.legend.labels) {
                chart.options.plugins.legend.labels.color = textColor;
            }

            if (chart.options.scales) {
                Object.values(chart.options.scales).forEach(function(axis) {
                    if (axis.ticks) axis.ticks.color = textColor;
                    if (axis.grid) axis.grid.color = gridColor;
                    if (axis.title) axis.title.color = textColor;
                });
            }

            chart.update();
        });
    }

    function applyTheme(theme) {
        document.body.classList.toggle("dark", theme === "dark");

        themeToggle.textContent =
            theme === "dark" ? "☀️ Day mode" : "🌙 Night mode";

        updateChartTheme();
    }

    themeToggle.addEventListener("click", function() {
        const nextTheme = document.body.classList.contains("dark")
            ? "light" : "dark";

        applyTheme(nextTheme);

        try {
            localStorage.setItem("cloudcost-theme", nextTheme);
        } catch (error) {
            // Theme switching works without local storage.
        }
    });

    let initialTheme = "light";

    try {
        initialTheme = localStorage.getItem("cloudcost-theme") || "light";
    } catch (error) {
        // Use light mode if storage is unavailable.
    }

    applyTheme(initialTheme);

    // If Chart.js could not load (for example, no internet), say so
    // instead of leaving empty boxes.
    if (!window.Chart) {
        document.querySelectorAll(".chart-wrap").forEach(function(box) {
            box.innerHTML = '<div class="empty">Charts could not load. ' +
                'Check your internet connection and refresh the page.</div>';
        });
    }

    function rupees(value) {
        return "₹" + Number(value).toLocaleString("en-IN", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    // Chart 1: cost breakdown doughnut
    const costCanvas = document.getElementById("costChart");

    if (costCanvas && window.Chart) {
        window.costChart = new Chart(costCanvas, {
            type: "doughnut",
            data: {
                labels: ["Virtual machine", "Database"],
                datasets: [{
                    data: [
                        {{ vm_cost|tojson }},
                        {{ db_cost|tojson }}
                    ],
                    backgroundColor: ["#6366f1", "#14b8a6"],
                    borderWidth: 0,
                    hoverOffset: 8
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "65%",
                plugins: {
                    legend: {
                        position: "bottom",
                        labels: {
                            padding: 18,
                            usePointStyle: true
                        }
                    },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return context.label + ": " + rupees(context.raw);
                            }
                        }
                    }
                }
            }
        });
    }

    // Chart 2: saved cost estimates
    const historyCanvas = document.getElementById("historyChart");

    if (historyCanvas && window.Chart) {
        const historyData = {{ history_chart|tojson }};

        window.historyChart = new Chart(historyCanvas, {
            type: "bar",
            data: {
                labels: historyData.labels,
                datasets: [{
                    label: "Estimated cost (₹)",
                    data: historyData.costs,
                    backgroundColor: "#6366f1",
                    borderRadius: 7,
                    maxBarThickness: 48
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: false
                    },
                    tooltip: {
                        callbacks: {
                            title: function(items) {
                                return historyData.times[items[0].dataIndex];
                            },
                            label: function(context) {
                                return "Estimated cost: " + rupees(context.raw);
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            maxRotation: 45,
                            minRotation: 0
                        },
                        grid: {
                            display: false
                        }
                    },
                    y: {
                        beginAtZero: true,
                        title: {
                            display: true,
                            text: "Estimated cost (₹)"
                        },
                        ticks: {
                            callback: function(value) {
                                return "₹" + Number(value).toLocaleString("en-IN");
                            }
                        }
                    }
                }
            }
        });
    }

    // Chart 3: CPU and memory utilization over time
    const resourceCanvas = document.getElementById("resourceChart");

    if (resourceCanvas && window.Chart) {
        const resourceData = {{ resource_history|tojson }};

        window.resourceChart = new Chart(resourceCanvas, {
            type: "line",
            data: {
                labels: resourceData.labels,
                datasets: [
                    {
                        label: "CPU utilization (%)",
                        data: resourceData.cpu,
                        borderColor: "#6366f1",
                        backgroundColor: "rgba(99, 102, 241, 0.12)",
                        borderWidth: 3,
                        pointRadius: 4,
                        pointHoverRadius: 6,
                        tension: 0.35,
                        fill: true
                    },
                    {
                        label: "Memory utilization (%)",
                        data: resourceData.memory,
                        borderColor: "#14b8a6",
                        backgroundColor: "rgba(20, 184, 166, 0.08)",
                        borderWidth: 3,
                        pointRadius: 4,
                        pointHoverRadius: 6,
                        tension: 0.35,
                        fill: false
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    intersect: false,
                    mode: "index"
                },
                plugins: {
                    legend: {
                        position: "bottom",
                        labels: {
                            padding: 18,
                            usePointStyle: true
                        }
                    },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return context.dataset.label + ": " +
                                    context.raw + "%";
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        title: {
                            display: true,
                            text: "Sample time"
                        },
                        grid: {
                            display: false
                        }
                    },
                    y: {
                        min: 0,
                        max: 100,
                        title: {
                            display: true,
                            text: "Utilization (%)"
                        },
                        ticks: {
                            callback: function(value) {
                                return value + "%";
                            }
                        }
                    }
                }
            }
        });
    }

    // Apply current theme to charts after creating them.
    updateChartTheme();
</script>
</body>
</html>
"""


AUTH_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ "Create account" if mode == "register" else "Sign in" }} · CloudCost Watch</title>
    <style>{{ styles }}
        .auth-box {
            max-width: 400px;
            margin: 60px auto;
        }
        .switch-link {
            margin-top: 18px;
            font-size: 13px;
            color: var(--muted);
            text-align: center;
        }
        .switch-link a { color: var(--primary); font-weight: 700; }
    </style>
</head>
<body>
<script>
    // Use the theme chosen on the dashboard.
    try {
        if (localStorage.getItem("cloudcost-theme") === "dark") {
            document.body.classList.add("dark");
        }
    } catch (error) {}
</script>
<header>
    <div class="container header-inner">
        <div>
            <div class="brand">CloudCost <span>Watch</span></div>
            <div class="subtitle">Cloud cost planning and analytics dashboard</div>
        </div>
    </div>
</header>

<main class="container">
    <section class="panel auth-box">
        {% if mode == "register" %}
            <h2>Create an account</h2>
            <p class="panel-description">Your estimates are private to your account.</p>
        {% else %}
            <h2>Sign in</h2>
            <p class="panel-description">Sign in to see your saved cost estimates.</p>
        {% endif %}

        <form method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="field">
                <label for="username">Username</label>
                <input id="username" name="username" value="{{ username }}"
                       autocomplete="username" maxlength="30" required autofocus>
            </div>
            <div class="field">
                <label for="password">Password</label>
                <input id="password" name="password" type="password"
                       autocomplete="{{ 'new-password' if mode == 'register' else 'current-password' }}"
                       {% if mode == "register" %}minlength="8"{% endif %}
                       maxlength="128" required>
            </div>
            {% if mode == "register" %}
            <div class="field">
                <label for="confirm">Confirm password</label>
                <input id="confirm" name="confirm" type="password"
                       autocomplete="new-password" minlength="8" maxlength="128" required>
            </div>
            {% endif %}
            <button class="full-button" type="submit">
                {{ "Create account" if mode == "register" else "Sign in" }}
            </button>
        </form>

        {% if error %}
            <p class="warning">{{ error }}</p>
        {% endif %}

        <p class="switch-link">
            {% if mode == "register" %}
                Already have an account? <a href="{{ url_for('login') }}">Sign in</a>
            {% else %}
                New here? <a href="{{ url_for('register') }}">Create an account</a>
            {% endif %}
        </p>
    </section>
</main>
</body>
</html>
"""


# ---------- Login and form protection ----------

def log_in(user):
    """Start a fresh session for this user (a new session also stops
    session-fixation attacks)."""
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]


def login_required(view):
    """Send visitors to the sign-in page unless they are logged in."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.template_global()
def csrf_token():
    """A secret per-session value placed in every form, so other websites
    cannot submit forms on a signed-in user's behalf (CSRF)."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        if not sent or not secrets.compare_digest(sent, expected):
            abort(400, "This form has expired. Please go back, refresh "
                       "the page and try again.")


@app.after_request
def add_security_headers(response):
    """Basic browser protections against sniffing and clickjacking."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.errorhandler(sqlite3.Error)
def database_error(exc):
    """Show a friendly message instead of a crash page if the database fails."""
    app.logger.error("Database error: %s", exc)
    return ("<h1>Database unavailable</h1>"
            "<p>CloudCost Watch could not read its database. "
            "Please try again in a moment.</p>"), 500


@app.route("/register", methods=["GET", "POST"])
def register():
    error = ""
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        try:
            if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", username):
                raise ValueError("Username must be 3-30 letters, numbers "
                                 "or underscores.")
            if not 8 <= len(password) <= 128:
                raise ValueError("Password must be at least 8 characters.")
            if password != request.form.get("confirm"):
                raise ValueError("The two passwords do not match.")
            user_id = create_user(username, password)
            log_in({"id": user_id, "username": username})
            return redirect(url_for("dashboard"))
        except ValueError as exc:
            error = str(exc)
    return render_template_string(AUTH_PAGE, styles=STYLES, mode="register",
                                  error=error, username=username)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        user = check_login(username, request.form.get("password") or "")
        if user:
            log_in(user)
            return redirect(url_for("dashboard"))
        # Same message either way, so attackers cannot discover usernames.
        error = "Incorrect username or password."
    return render_template_string(AUTH_PAGE, styles=STYLES, mode="login",
                                  error=error, username=username)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/export")
@login_required
def export_history():
    """Download the signed-in user's estimate history as a CSV spreadsheet."""
    history = list(reversed(get_history(session["user_id"])))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date and time (IST)",
        "VM hours per day",
        "Database hours per day",
        "Duration (days)",
        "Budget (INR)",
        "Total estimated cost (INR)"
    ])

    for item in history:
        writer.writerow([
            item["created_at"],
            item["vm_hours"],
            item["db_hours"],
            item["days"],
            f'{item["budget"]:.2f}',
            f'{item["total_cost"]:.2f}'
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cloudcost_history.csv"}
    )


@app.route("/", methods=["GET", "POST"])
@login_required
def dashboard():
    user_id = session["user_id"]
    vm_hours = 6.0
    db_hours = 4.0
    days = 26
    budget = 2000.0

    calculated = False
    error = ""
    results = {"vm_cost": 0.0, "db_cost": 0.0, "total_cost": 0.0,
               "budget_percent": 0.0, "remaining": budget}

    if request.method == "POST":
        try:
            vm_hours = parse_number(request.form, "vm_hours",
                                    "VM hours", 0, 24)
            db_hours = parse_number(request.form, "db_hours",
                                    "Database hours", 0, 24)
            days = parse_number(request.form, "days",
                                "Duration (days)", 1, 31, whole=True)
            budget = parse_number(request.form, "budget",
                                  "Budget", 0.01, MAX_BUDGET)

            results = calculate_costs(vm_hours, db_hours, days, budget)
            new_id = save_calculation(user_id, vm_hours, db_hours, days,
                                      budget, results["total_cost"])

            # Redirect so refreshing the page does not save a duplicate.
            return redirect(url_for("dashboard", latest=new_id))

        except ValueError as exc:
            error = str(exc)
            # Keep what the user typed so they can fix it.
            vm_hours = request.form.get("vm_hours", "")
            db_hours = request.form.get("db_hours", "")
            days = request.form.get("days", "")
            budget = request.form.get("budget", "")
            results["remaining"] = 0.0

        except sqlite3.Error:
            error = "Could not save the estimate. Please try again."
            results["remaining"] = 0.0

    else:
        # After a successful calculation we land here with ?latest=<id>.
        latest_id = request.args.get("latest", type=int)
        saved = get_calculation(latest_id, user_id) if latest_id else None

        if saved:
            vm_hours = saved["vm_hours"]
            db_hours = saved["db_hours"]
            days = saved["days"]
            budget = saved["budget"]
            results = calculate_costs(vm_hours, db_hours, days, budget)
            calculated = True

    history = get_history(user_id)

    # Display up to 10 saved estimates, oldest to newest.
    chart_records = list(reversed(history[:10]))

    history_chart = {
        "labels": [short_time(item["created_at"]) for item in chart_records],
        "times": [item["created_at"] for item in chart_records],
        "costs": [item["total_cost"] for item in chart_records]
    }

    return render_template_string(
        PAGE,
        styles=STYLES,
        username=session["username"],
        vm_hours=vm_hours,
        db_hours=db_hours,
        days=days,
        budget=budget,
        calculated=calculated,
        error=error,
        history=history,
        summary=summarize_history(history),
        history_chart=history_chart,
        resource_metrics=RESOURCE_METRICS,
        resource_history=RESOURCE_HISTORY,
        **results
    )


init_db()

if __name__ == "__main__":
    # Debug mode is off unless you ask for it: set FLASK_DEBUG=1 while developing.
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
