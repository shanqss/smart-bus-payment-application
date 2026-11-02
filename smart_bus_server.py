"""
SMART BUS MONITORING SYSTEM - LAPTOP SERVER  (Flask + SQLite)

What it does
  - stores passengers (name, age, phone, card UID, balance) in smart_bus.db
  - decides every card tap: board / exit, minimum balance, fare, wallet update
  - keeps the current route and stop; the Uno LCD follows it
  - shows a live dashboard: route selector, stop buttons, who is on the bus, rides
  - lets you register new cards and add money from the browser

Run
  1) pip install flask
  2) python smart_bus_server.py
  3) open http://localhost:5000
  4) put the IP address printed below into LAPTOP_IP in nodemcu_smart_bus.ino
Laptop and NodeMCU must be on the same Wi-Fi. If Windows Firewall asks, allow Python
on private networks.

Classroom project: do not use real bank or debit card data.
"""

import csv
import io
import re
import socket
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from flask import (Flask, Response, flash, jsonify, redirect,
                   render_template_string, request, url_for)

# =============== SETTINGS - EDIT THESE ===============
DB_FILE = "smart_bus.db"
HOST = "0.0.0.0"          # 0.0.0.0 lets the NodeMCU reach the laptop
PORT = 5000

MIN_BALANCE = 60          # balance needed to board (Rs)
FARE_PER_STOP = 5         # Rs for every stop travelled
MIN_FARE = 5              # same stop in and out
MAX_FARE = 25             # start-to-end price: the fare never goes above this

# 3 default routes (10 stops each). Keep every stop name 16 letters or fewer
# (the LCD is 16 characters wide). You can add stops or rename them.
ROUTES = {
    "route1": {
        "name": "Route 1 - City Loop",
        "stops": ["Central Station", "Bus Stand", "City Market", "Town Hall",
                  "Govt Hospital", "Main Library", "Old Fort", "Lake View",
                  "Tech Park", "Airport Road"],
    },
    "route2": {
        "name": "Route 2 - College Line",
        "stops": ["School Gate", "Park Avenue", "Temple Street", "Post Office",
                  "Mall Road", "Cinema Hall", "Sports Ground", "University",
                  "Hostel Block", "College Gate"],
    },
    "route3": {
        "name": "Route 3 - Highway Express",
        "stops": ["Bus Depot", "Toll Plaza", "Flyover", "Bridge Cross",
                  "Metro Station", "Hospital Cross", "Stadium", "Business Hub",
                  "IT Corridor", "Airport Gate"],
    },
}
DEFAULT_ROUTE = "route1"
# =====================================================

app = Flask(__name__)
app.secret_key = "smart-bus-classroom"
LOCK = threading.Lock()
UNKNOWN = {}      # cards tapped but not registered yet: uid -> time


# ---------------------------------------------------------------- database
@contextmanager
def conn():
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS passengers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            age INTEGER NOT NULL,
            phone TEXT NOT NULL,
            balance INTEGER NOT NULL DEFAULT 0,
            on_bus INTEGER NOT NULL DEFAULT 0,
            board_route TEXT,
            board_stop INTEGER,
            board_time TEXT,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rides(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            passenger_id INTEGER NOT NULL,
            route TEXT NOT NULL,
            board_stop TEXT NOT NULL,
            exit_stop TEXT NOT NULL,
            fare INTEGER NOT NULL,
            board_time TEXT NOT NULL,
            exit_time TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            passenger_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            time TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL);
        """)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_setting(con, key, default):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))


# ---------------------------------------------------------------- route / stop
def current(con):
    """Returns (route_id, stop_index)."""
    route = get_setting(con, "route", DEFAULT_ROUTE)
    if route not in ROUTES:
        route = DEFAULT_ROUTE
    n = len(ROUTES[route]["stops"])
    try:
        idx = int(get_setting(con, "stop", "0"))
    except ValueError:
        idx = 0
    return route, max(0, min(idx, n - 1))


def stop_name(route_id, idx):
    stops = ROUTES.get(route_id, {}).get("stops", [])
    return stops[idx] if idx is not None and 0 <= idx < len(stops) else "?"


def lcd_text(text, limit=16):
    """Keep only characters the LCD / serial link can handle."""
    clean = re.sub(r"[^A-Za-z0-9 .'\-]", "", text or "").strip()
    return clean[:limit] or "Passenger"


def stop_line(con):
    route, idx = current(con)
    stops = ROUTES[route]["stops"]
    return "STOP,%d,%d,%s" % (idx + 1, len(stops), lcd_text(stops[idx]))


def move_stop(con, step):
    route, idx = current(con)
    n = len(ROUTES[route]["stops"])
    set_setting(con, "stop", (idx + step) % n)      # wraps around


# ---------------------------------------------------------------- fare + taps
def calc_fare(board_route, board_stop, exit_route, exit_stop):
    if board_route != exit_route:                   # route was changed during the ride
        return MAX_FARE
    fare = abs(exit_stop - board_stop) * FARE_PER_STOP
    return max(MIN_FARE, min(MAX_FARE, fare))


def process_tap(uid):
    """Decide a card tap. Returns the one-line reply for the Uno."""
    uid = uid.strip().upper()
    if not re.fullmatch(r"[0-9A-F]{4,20}", uid):
        return "NO,ERROR"

    with LOCK, conn() as con:
        p = con.execute("SELECT * FROM passengers WHERE uid=?", (uid,)).fetchone()
        if p is None:
            UNKNOWN[uid] = now()
            return "NO,UNKNOWN"

        route_id, stop = current(con)
        name = lcd_text(p["name"])

        if not p["on_bus"]:                                         # ---- BOARD
            if p["balance"] < MIN_BALANCE:
                return "NO,LOWBAL,%s,%d,%d" % (name, p["balance"], MIN_BALANCE)
            con.execute("UPDATE passengers SET on_bus=1, board_route=?, "
                        "board_stop=?, board_time=? WHERE id=?",
                        (route_id, stop, now(), p["id"]))
            return "OK,BOARD,%s,%d" % (name, p["balance"])

        # ---------------------------------------------------------- EXIT
        board_route = p["board_route"] or route_id
        board_stop = p["board_stop"] if p["board_stop"] is not None else stop
        fare = calc_fare(board_route, board_stop, route_id, stop)
        fare = min(fare, p["balance"])                              # never below 0
        new_balance = p["balance"] - fare
        con.execute("UPDATE passengers SET balance=?, on_bus=0, board_route=NULL, "
                    "board_stop=NULL, board_time=NULL WHERE id=?",
                    (new_balance, p["id"]))
        con.execute("INSERT INTO rides(passenger_id,route,board_stop,exit_stop,fare,"
                    "board_time,exit_time) VALUES(?,?,?,?,?,?,?)",
                    (p["id"], ROUTES[route_id]["name"],
                     stop_name(board_route, board_stop), stop_name(route_id, stop),
                     fare, p["board_time"] or now(), now()))
        con.execute("INSERT INTO transactions(passenger_id,kind,amount,balance_after,time)"
                    " VALUES(?,?,?,?,?)", (p["id"], "fare", -fare, new_balance, now()))
        return "OK,EXIT,%s,%d,%d" % (name, fare, new_balance)


# ---------------------------------------------------------------- API for the NodeMCU
def text(s):
    return Response(s, mimetype="text/plain")


@app.route("/api/stop")
def api_stop():
    with conn() as con:
        return text(stop_line(con))


@app.route("/api/next")
def api_next():
    with LOCK, conn() as con:
        move_stop(con, +1)
        return text(stop_line(con))


@app.route("/api/prev")
def api_prev():
    with LOCK, conn() as con:
        move_stop(con, -1)
        return text(stop_line(con))


@app.route("/api/tap")
def api_tap():
    return text(process_tap(request.args.get("uid", "")))


@app.route("/api/state")
def api_state():
    with conn() as con:
        route, idx = current(con)
        on_bus = con.execute("SELECT COUNT(*) FROM passengers WHERE on_bus=1").fetchone()[0]
    return jsonify(route=ROUTES[route]["name"], stop=stop_name(route, idx),
                   stop_number=idx + 1, stop_count=len(ROUTES[route]["stops"]),
                   on_bus=on_bus)


# ---------------------------------------------------------------- page layout
BASE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{% if auto_refresh %}<meta http-equiv="refresh" content="3">{% endif %}
<title>Smart Bus Monitoring</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:#eef3fb;color:#1c2733}
header{background:linear-gradient(135deg,#1565c0,#00acc1);color:#fff;padding:14px 18px}
header h1{margin:0 0 8px;font-size:24px}
nav a{color:#fff;text-decoration:none;background:#ffffff33;padding:6px 12px;border-radius:18px;margin-right:6px;font-size:14px}
main{max-width:980px;margin:auto;padding:12px}
.card{background:#fff;border-radius:14px;padding:14px;margin-bottom:12px;box-shadow:0 2px 6px #0002}
.card h2{margin:0 0 10px;font-size:16px;color:#1565c0}
.warn{background:#fff3e0;border-left:5px solid #fb8c00}
.flash{padding:10px 14px;border-radius:10px;margin-bottom:10px;font-weight:600}
.flash.ok{background:#e8f5e9;color:#2e7d32}.flash.err{background:#ffebee;color:#c62828}
.stop{font-size:30px;font-weight:700;color:#e65100;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.stat{background:#fff;border-radius:14px;padding:12px;box-shadow:0 2px 6px #0002}
.stat b{display:block;font-size:26px;color:#1565c0}.stat small{color:#607d8b}
form.inline{display:inline}
.btn,button{background:#1565c0;color:#fff;border:0;border-radius:10px;padding:9px 14px;font-size:14px;cursor:pointer;text-decoration:none;display:inline-block;margin:2px}
.btn.active{background:#e65100}.btn.big{font-size:16px;padding:11px 18px;background:#2e7d32}
.pills{margin-top:10px}
.pill{background:#e3eaf5;color:#1c2733;border-radius:20px;padding:6px 12px;font-size:13px}
.pill.past{background:#cfd8dc;color:#607d8b}.pill.cur{background:#e65100;color:#fff;font-weight:700}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:7px 5px;border-bottom:1px solid #e3e8ef;font-size:14px}
th{color:#607d8b;font-size:12px;text-transform:uppercase}
.on{color:#2e7d32;font-weight:700}.off{color:#90a4ae}
label{display:block;margin-top:10px;font-weight:600;font-size:14px}
input{padding:9px;border:1px solid #b0bec5;border-radius:8px;width:100%;max-width:360px;font-size:15px}
td input{width:80px;padding:5px}
.hint{color:#607d8b;font-size:13px}
.scroll{overflow-x:auto}
</style></head><body>
<header><h1>&#128652; Smart Bus Monitoring System</h1>
<nav><a href="/">Dashboard</a><a href="/passengers">Passengers</a><a href="/register">Register card</a></nav></header>
<main>
{% with msgs = get_flashed_messages(with_categories=true) %}{% for cat, m in msgs %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}{% endwith %}
<!--CONTENT-->
</main></body></html>"""

DASHBOARD = """
{% if pending %}<div class="card warn"><b>New card tapped, not registered yet:</b>
{% for uid, t in pending %}<div style="margin-top:6px">{{uid}} <small>({{t}})</small>
<a class="btn" href="{{ url_for('register', uid=uid) }}">Register passenger</a></div>{% endfor %}</div>{% endif %}

<div class="grid">
<div class="stat"><b>{{on_bus|length}}</b><small>Passengers on bus</small></div>
<div class="stat"><b>{{total_passengers}}</b><small>Registered cards</small></div>
<div class="stat"><b>Rs {{total_collected}}</b><small>Total fare collected</small></div>
<div class="stat"><b>{{total_rides}}</b><small>Completed rides</small></div>
</div>

<div class="card"><h2>Select route</h2>
{% for rid, r in routes.items() %}
<form method="post" action="/route" class="inline"><button name="route" value="{{rid}}" class="btn {{ 'active' if rid == route_id else '' }}">{{ r['name'] }}</button></form>
{% endfor %}
<p class="hint">Changing the route restarts at its first stop. Fare = Rs {{fare_per_stop}} per stop, maximum Rs {{max_fare}}. Minimum balance to board: Rs {{min_balance}}.</p></div>

<div class="card"><h2>Current stop (shown on the bus LCD)</h2>
<div class="stop">{{idx + 1}}/{{stops|length}} &middot; {{stops[idx]}}</div>
<form method="post" action="/stop" class="inline"><button name="action" value="prev" class="btn">&laquo; Previous stop</button></form>
<form method="post" action="/stop" class="inline"><button name="action" value="next" class="btn big">Next stop &raquo;</button></form>
<div class="pills">
{% for s in stops %}
<form method="post" action="/stop" class="inline"><input type="hidden" name="action" value="set"><button name="idx" value="{{loop.index0}}" class="pill {{ 'cur' if loop.index0 == idx else ('past' if loop.index0 < idx else '') }}">{{loop.index}}. {{s}}</button></form>
{% endfor %}</div></div>

<div class="card"><h2>On the bus now</h2><div class="scroll"><table>
<tr><th>Name</th><th>Age</th><th>Phone</th><th>Boarded at</th><th>Since</th><th>Balance</th></tr>
{% for p in on_bus %}<tr><td>{{p.name}}</td><td>{{p.age}}</td><td>{{p.phone}}</td><td>{{p.stop}}</td><td>{{p.since}}</td><td>Rs {{p.balance}}</td></tr>
{% else %}<tr><td colspan="6" class="off">Nobody on the bus</td></tr>{% endfor %}
</table></div></div>

<div class="card"><h2>Recent rides</h2><div class="scroll"><table>
<tr><th>Passenger</th><th>Route</th><th>Boarded</th><th>Exited</th><th>Fare</th><th>Time</th></tr>
{% for r in rides %}<tr><td>{{r['name']}}</td><td>{{r['route']}}</td><td>{{r['board_stop']}}</td><td>{{r['exit_stop']}}</td><td>Rs {{r['fare']}}</td><td>{{r['exit_time']}}</td></tr>
{% else %}<tr><td colspan="6" class="off">No rides yet</td></tr>{% endfor %}
</table></div>
<p><a href="/export/rides.csv">Download rides (CSV)</a></p></div>
"""

PASSENGERS = """
<div class="card"><h2>Passengers ({{people|length}})</h2>
<a class="btn big" href="/register">+ Register new card</a>
<div class="scroll"><table>
<tr><th>Name</th><th>Age</th><th>Phone</th><th>Card UID</th><th>Balance</th><th>Status</th><th>Add money</th></tr>
{% for p in people %}<tr>
<td>{{p['name']}}</td><td>{{p['age']}}</td><td>{{p['phone']}}</td><td>{{p['uid']}}</td>
<td>Rs {{p['balance']}}{% if p['balance'] < min_balance %} <small class="off">(below Rs {{min_balance}})</small>{% endif %}</td>
<td class="{{ 'on' if p['on_bus'] else 'off' }}">{{ 'On bus' if p['on_bus'] else 'Off bus' }}</td>
<td><form method="post" action="/topup" class="inline">
<input type="hidden" name="pid" value="{{p['id']}}"><input name="amount" type="number" min="1" max="5000" placeholder="Rs" required>
<button>Add</button></form></td></tr>
{% else %}<tr><td colspan="7" class="off">No passengers yet. Tap a card, then register it.</td></tr>{% endfor %}
</table></div>
<p><a href="/export/passengers.csv">Download passengers (CSV)</a></p></div>
"""

REGISTER = """
{% if pending %}<div class="card warn"><b>Cards tapped but not registered:</b>
{% for uid, t in pending %}<a class="btn" href="{{ url_for('register', uid=uid) }}">{{uid}}</a>{% endfor %}</div>{% endif %}
<div class="card"><h2>Register a passenger card</h2>
<form method="post">
<label>Card UID</label>
<input name="uid" value="{{f.uid}}" required placeholder="Tap the card, then pick it above">
<label>Name</label><input name="name" maxlength="30" value="{{f.name}}" required>
<label>Age</label><input name="age" type="number" min="1" max="120" value="{{f.age}}" required>
<label>Phone number</label><input name="phone" value="{{f.phone}}" required placeholder="digits only">
<label>Add money (Rs)</label><input name="money" type="number" min="0" max="5000" value="{{f.money}}">
<p class="hint">A passenger needs at least Rs {{min_balance}} to board.</p>
<button class="btn big">Save passenger</button>
</form></div>
"""


def page(content, **ctx):
    return render_template_string(BASE.replace("<!--CONTENT-->", content), **ctx)


def pending_cards():
    with conn() as con:
        known = {r["uid"] for r in con.execute("SELECT uid FROM passengers")}
    for uid in list(UNKNOWN):
        if uid in known:
            del UNKNOWN[uid]
    return sorted(UNKNOWN.items(), key=lambda kv: kv[1], reverse=True)


# ---------------------------------------------------------------- pages
@app.route("/")
def dashboard():
    with conn() as con:
        route_id, idx = current(con)
        on_bus = []
        for p in con.execute("SELECT * FROM passengers WHERE on_bus=1 ORDER BY board_time"):
            on_bus.append({"name": p["name"], "age": p["age"], "phone": p["phone"],
                           "stop": stop_name(p["board_route"], p["board_stop"]),
                           "since": (p["board_time"] or "")[11:],
                           "balance": p["balance"]})
        rides = con.execute(
            "SELECT p.name AS name, r.* FROM rides r JOIN passengers p "
            "ON p.id=r.passenger_id ORDER BY r.id DESC LIMIT 15").fetchall()
        total_collected = con.execute("SELECT COALESCE(SUM(fare),0) FROM rides").fetchone()[0]
        total_rides = con.execute("SELECT COUNT(*) FROM rides").fetchone()[0]
        total_passengers = con.execute("SELECT COUNT(*) FROM passengers").fetchone()[0]
    return page(DASHBOARD, auto_refresh=True, routes=ROUTES, route_id=route_id,
                stops=ROUTES[route_id]["stops"], idx=idx, on_bus=on_bus, rides=rides,
                total_collected=total_collected, total_rides=total_rides,
                total_passengers=total_passengers, pending=pending_cards(),
                fare_per_stop=FARE_PER_STOP, max_fare=MAX_FARE, min_balance=MIN_BALANCE)


@app.route("/route", methods=["POST"])
def set_route():
    route = request.form.get("route", "")
    if route in ROUTES:
        with LOCK, conn() as con:
            set_setting(con, "route", route)
            set_setting(con, "stop", 0)
    return redirect(url_for("dashboard"))


@app.route("/stop", methods=["POST"])
def set_stop():
    action = request.form.get("action", "")
    with LOCK, conn() as con:
        route, idx = current(con)
        n = len(ROUTES[route]["stops"])
        if action == "next":
            move_stop(con, +1)
        elif action == "prev":
            move_stop(con, -1)
        elif action == "set":
            try:
                new = int(request.form.get("idx", idx))
            except ValueError:
                new = idx
            if 0 <= new < n:
                set_setting(con, "stop", new)
    return redirect(url_for("dashboard"))


@app.route("/passengers")
def passengers():
    with conn() as con:
        people = con.execute("SELECT * FROM passengers ORDER BY name").fetchall()
    return page(PASSENGERS, people=people, min_balance=MIN_BALANCE)


@app.route("/register", methods=["GET", "POST"])
def register():
    form = {"uid": request.args.get("uid", "").upper(), "name": "", "age": "",
            "phone": "", "money": "100"}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("uid", "name", "age", "phone", "money")}
        form["uid"] = form["uid"].upper()
        errors = []
        if not re.fullmatch(r"[0-9A-F]{4,20}", form["uid"]):
            errors.append("Card UID must be hex letters/numbers (example A1B2C3D4).")
        if not 2 <= len(form["name"]) <= 30:
            errors.append("Name must be 2 to 30 characters.")
        age = 0
        try:
            age = int(form["age"])
            if not 1 <= age <= 120:
                raise ValueError
        except ValueError:
            errors.append("Age must be a number from 1 to 120.")
        phone = form["phone"].replace(" ", "")
        if not re.fullmatch(r"\+?\d{7,15}", phone):
            errors.append("Phone number must be 7 to 15 digits.")
        money = 0
        try:
            money = int(form["money"] or 0)
            if not 0 <= money <= 5000:
                raise ValueError
        except ValueError:
            errors.append("Money must be between 0 and 5000.")
        if not errors:
            with LOCK, conn() as con:
                if con.execute("SELECT 1 FROM passengers WHERE uid=?", (form["uid"],)).fetchone():
                    errors.append("This card is already registered.")
                else:
                    cur = con.execute(
                        "INSERT INTO passengers(uid,name,age,phone,balance,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (form["uid"], form["name"], age, phone, money, now()))
                    if money > 0:
                        con.execute("INSERT INTO transactions(passenger_id,kind,amount,"
                                    "balance_after,time) VALUES(?,?,?,?,?)",
                                    (cur.lastrowid, "initial", money, money, now()))
            if not errors:
                UNKNOWN.pop(form["uid"], None)
                flash("Saved %s with Rs %d." % (form["name"], money), "ok")
                return redirect(url_for("passengers"))
        for e in errors:
            flash(e, "err")
    return page(REGISTER, f=form, pending=pending_cards(), min_balance=MIN_BALANCE)


@app.route("/topup", methods=["POST"])
def topup():
    try:
        pid = int(request.form.get("pid", 0))
        amount = int(request.form.get("amount", 0))
    except ValueError:
        pid, amount = 0, 0
    if not 1 <= amount <= 5000:
        flash("Amount must be between 1 and 5000.", "err")
        return redirect(url_for("passengers"))
    with LOCK, conn() as con:
        p = con.execute("SELECT * FROM passengers WHERE id=?", (pid,)).fetchone()
        if p is None:
            flash("Passenger not found.", "err")
        else:
            new_balance = p["balance"] + amount
            con.execute("UPDATE passengers SET balance=? WHERE id=?", (new_balance, pid))
            con.execute("INSERT INTO transactions(passenger_id,kind,amount,balance_after,time)"
                        " VALUES(?,?,?,?,?)", (pid, "topup", amount, new_balance, now()))
            flash("Added Rs %d to %s. New balance Rs %d." % (amount, p["name"], new_balance), "ok")
    return redirect(url_for("passengers"))


def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=" + filename})


@app.route("/export/passengers.csv")
def export_passengers():
    with conn() as con:
        rows = con.execute("SELECT name,age,phone,uid,balance,created_at FROM passengers "
                           "ORDER BY name").fetchall()
    return csv_response("passengers.csv",
                        ["name", "age", "phone", "card_uid", "balance", "registered"],
                        [tuple(r) for r in rows])


@app.route("/export/rides.csv")
def export_rides():
    with conn() as con:
        rows = con.execute(
            "SELECT p.name, p.phone, r.route, r.board_stop, r.exit_stop, r.fare, "
            "r.board_time, r.exit_time FROM rides r JOIN passengers p "
            "ON p.id=r.passenger_id ORDER BY r.id").fetchall()
    return csv_response("rides.csv",
                        ["name", "phone", "route", "boarded_at", "exited_at", "fare",
                         "board_time", "exit_time"],
                        [tuple(r) for r in rows])


# ---------------------------------------------------------------- start
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))          # no data is sent, it only picks the Wi-Fi adapter
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    init_db()
    ip = local_ip()
    print("=" * 60)
    print(" Smart Bus server is running")
    print(" Dashboard on this laptop :  http://localhost:%d" % PORT)
    print(" Put this in the NodeMCU  :  LAPTOP_IP = \"%s\"" % ip)
    print("=" * 60)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
