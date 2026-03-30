#!/usr/bin/env python3
"""
Polymarket Paper Trading Dashboard
Run: python3 dashboard.py
Open: http://localhost:5000
"""

import json
import os
import sqlite3
from typing import Optional
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, jsonify, render_template

DB_FILE                 = "paper_trades.db"
BUDGET_FILE             = "budget.json"
SHADOW_BUDGET_FILE      = "shadow_budget.json"
LIVE_BUDGET_FILE        = "live_budget.json"
STARTING_BUDGET         = float(os.getenv("STARTING_BUDGET", "500"))
SHADOW_STARTING_BUDGET  = float(os.getenv("SHADOW_STARTING_BUDGET", os.getenv("STARTING_BUDGET", "500")))
SCAN_INTERVAL           = int(os.getenv("SCAN_INTERVAL", "3600"))
STRATEGY_MODE           = os.getenv("STRATEGY_MODE", "compete")
app                     = Flask(__name__)


def db_query(sql: str, params: tuple = ()) -> list:
    if not os.path.exists(DB_FILE):
        return []
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def db_one(sql: str, params: tuple = ()) -> dict:
    rows = db_query(sql, params)
    return rows[0] if rows else {}


def get_budget() -> Optional[float]:
    if not os.path.exists(BUDGET_FILE):
        return None
    with open(BUDGET_FILE) as f:
        return json.load(f).get("budget")


def get_shadow_budget() -> Optional[float]:
    if not os.path.exists(SHADOW_BUDGET_FILE):
        return None
    with open(SHADOW_BUDGET_FILE) as f:
        return json.load(f).get("budget")


def get_live_budget() -> Optional[float]:
    if not os.path.exists(LIVE_BUDGET_FILE):
        return None
    with open(LIVE_BUDGET_FILE) as f:
        return json.load(f).get("budget")


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/overview")
def overview():
    row = db_one("""
        SELECT
            COUNT(*)                                                    AS total_trades,
            COALESCE(SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END), 0)   AS open_trades,
            COALESCE(SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END), 0)   AS resolved_trades,
            COALESCE(SUM(CASE WHEN pnl > 0   THEN 1 ELSE 0 END), 0)   AS wins,
            COALESCE(ROUND(SUM(COALESCE(pnl, 0)), 2), 0)               AS total_pnl,
            COALESCE(ROUND(SUM(COALESCE(fee, 0)), 4), 0)               AS total_fees
        FROM trades
    """)
    resolved = row.get("resolved_trades") or 0
    wins     = row.get("wins") or 0

    shadow_row = db_one("""
        SELECT
            COALESCE(ROUND(SUM(pnl), 2), 0) AS resolved_pnl,
            COALESCE(ROUND(SUM(CASE WHEN resolved=0 THEN amount ELSE 0 END), 2), 0) AS open_amount
        FROM shadow_trades
    """)

    live_row = db_one("""
        SELECT
            COUNT(*)                                                                        AS total_trades,
            COALESCE(SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END), 0)                       AS open_trades,
            COALESCE(SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END), 0)                       AS resolved_trades,
            COALESCE(SUM(CASE WHEN pnl > 0   THEN 1 ELSE 0 END), 0)                       AS wins,
            COALESCE(ROUND(SUM(COALESCE(pnl, 0)), 2), 0)                                   AS total_pnl,
            COALESCE(ROUND(SUM(COALESCE(fee, 0)), 4), 0)                                   AS total_fees,
            COALESCE(ROUND(SUM(CASE WHEN resolved=0 THEN amount ELSE 0 END), 2), 0)        AS open_amount
        FROM trades WHERE mode IN ('live', 'live-dry')
    """)
    live_resolved = live_row.get("resolved_trades") or 0
    live_wins     = live_row.get("wins") or 0

    return jsonify({
        "budget":                  get_budget(),
        "starting_budget":         STARTING_BUDGET,
        "shadow_budget":           get_shadow_budget(),
        "shadow_starting_budget":  SHADOW_STARTING_BUDGET,
        "live_budget":             get_live_budget(),
        "strategy_mode":           STRATEGY_MODE,
        "scan_interval":           SCAN_INTERVAL,
        "total_trades":     row.get("total_trades", 0),
        "open_trades":      row.get("open_trades", 0),
        "resolved_trades":  resolved,
        "wins":             wins,
        "losses":           resolved - wins,
        "total_pnl":        row.get("total_pnl") or 0,
        "total_fees":       row.get("total_fees") or 0,
        "win_rate":         round(wins / resolved * 100, 1) if resolved else 0,
        "resolved_pnl":     row.get("total_pnl") or 0,
        "shadow_resolved_pnl": shadow_row.get("resolved_pnl") or 0,
        "live_total_trades":    live_row.get("total_trades") or 0,
        "live_open_trades":     live_row.get("open_trades") or 0,
        "live_resolved_trades": live_resolved,
        "live_wins":            live_wins,
        "live_losses":          live_resolved - live_wins,
        "live_total_pnl":       live_row.get("total_pnl") or 0,
        "live_total_fees":      live_row.get("total_fees") or 0,
        "live_win_rate":        round(live_wins / live_resolved * 100, 1) if live_resolved else 0,
        "live_open_amount":     live_row.get("open_amount") or 0,
        "shadow_open_amount":   shadow_row.get("open_amount") or 0,
    })


@app.route("/api/strategy-stats")
def strategy_stats():
    # Real trades — tag breakdown
    rows = db_query("SELECT tags, pnl, resolved FROM trades")
    real_map: dict = {}
    for r in rows:
        for tag in json.loads(r["tags"]):
            s = real_map.setdefault(tag, {"open": 0, "resolved": 0, "wins": 0, "pnl": 0.0})
            if r["resolved"]:
                s["resolved"] += 1
                s["pnl"] += r["pnl"] or 0
                if (r["pnl"] or 0) > 0:
                    s["wins"] += 1
            else:
                s["open"] += 1

    real = []
    for tag, s in real_map.items():
        real.append({
            "strategy": tag,
            "open":     s["open"],
            "resolved": s["resolved"],
            "wins":     s["wins"],
            "pnl":      round(s["pnl"], 2),
            "win_rate": round(s["wins"] / s["resolved"] * 100, 1) if s["resolved"] else 0,
        })
    real.sort(key=lambda x: -x["pnl"])

    # Shadow trades
    shadow_rows = db_query("""
        SELECT strategy,
               COUNT(*)                                       AS total,
               SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END)   AS resolved,
               SUM(CASE WHEN pnl > 0   THEN 1 ELSE 0 END)    AS wins,
               ROUND(SUM(COALESCE(pnl, 0)), 2)                AS pnl,
               ROUND(SUM(COALESCE(fee, 0)), 4)                AS total_fees
        FROM shadow_trades GROUP BY strategy
    """)
    shadow = []
    for r in shadow_rows:
        res = r.get("resolved") or 0
        shadow.append({
            "strategy":    r["strategy"],
            "total":       r["total"],
            "resolved":    res,
            "wins":        r.get("wins") or 0,
            "pnl":         r.get("pnl") or 0,
            "total_fees":  r.get("total_fees") or 0,
            "win_rate":    round((r.get("wins") or 0) / res * 100, 1) if res else 0,
        })
    shadow.sort(key=lambda x: -(x["pnl"] or 0))

    return jsonify({"real": real, "shadow": shadow})


@app.route("/api/trades")
def trades():
    return jsonify(db_query("""
        SELECT id, ts, market_name, direction, price, amount, fee,
               order_type, tags, resolved, outcome, pnl, close_ts, mode, order_id, whale_size
        FROM trades ORDER BY id DESC LIMIT 200
    """))


@app.route("/api/live-trades")
def live_trades():
    return jsonify(db_query("""
        SELECT id, ts, market_name, direction, price, amount, fee,
               order_type, tags, resolved, outcome, pnl, close_ts, mode, order_id, whale_size
        FROM trades WHERE mode IN ('live', 'live-dry')
        ORDER BY id DESC LIMIT 200
    """))


@app.route("/api/shadow-trades")
def shadow_trades():
    return jsonify(db_query("""
        SELECT id, ts, strategy, market_name, direction, price, amount, fee,
               resolved, outcome, pnl, close_ts, confidence, notes
        FROM shadow_trades ORDER BY id DESC LIMIT 200
    """))


@app.route("/api/pnl-history")
def pnl_history():
    rows = db_query("""
        SELECT close_ts, pnl FROM trades
        WHERE resolved=1 AND pnl IS NOT NULL
        ORDER BY close_ts ASC
    """)
    cumulative = 0.0
    points = []
    for r in rows:
        cumulative += r["pnl"]
        points.append({"ts": (r["close_ts"] or "")[:16], "pnl": round(cumulative, 2)})
    return jsonify(points)


@app.route("/api/shadow-pnl-history")
def shadow_pnl_history():
    rows = db_query("""
        SELECT close_ts, pnl FROM shadow_trades
        WHERE resolved=1 AND pnl IS NOT NULL
        ORDER BY close_ts ASC
    """)
    balance = SHADOW_STARTING_BUDGET
    points = [{"ts": None, "pnl": round(balance, 2)}]
    for r in rows:
        balance += r["pnl"]
        points.append({"ts": (r["close_ts"] or "")[:16], "pnl": round(balance, 2)})
    return jsonify({"starting_budget": SHADOW_STARTING_BUDGET, "points": points})


@app.route("/api/live-pnl-history")
def live_pnl_history():
    rows = db_query("""
        SELECT close_ts, pnl FROM trades
        WHERE resolved=1 AND pnl IS NOT NULL AND mode IN ('live', 'live-dry')
        ORDER BY close_ts ASC
    """)
    cumulative = 0.0
    points = []
    for r in rows:
        cumulative += r["pnl"]
        points.append({"ts": (r["close_ts"] or "")[:16], "pnl": round(cumulative, 2)})
    return jsonify(points)


@app.route("/api/scan-log")
def scan_log():
    return jsonify(db_query("""
        SELECT ts, markets_checked, signals_found, trades_placed
        FROM scan_log ORDER BY id DESC LIMIT 30
    """))


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    print("━" * 50)
    print(f"  📊 Dashboard → http://localhost:{port}")
    print("━" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
