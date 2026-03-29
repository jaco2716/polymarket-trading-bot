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
from flask import Flask, jsonify, render_template

DB_FILE     = "paper_trades.db"
BUDGET_FILE = "budget.json"
app         = Flask(__name__)


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
    return jsonify({
        "budget":          get_budget(),
        "total_trades":    row.get("total_trades", 0),
        "open_trades":     row.get("open_trades", 0),
        "resolved_trades": resolved,
        "wins":            wins,
        "losses":          resolved - wins,
        "total_pnl":       row.get("total_pnl") or 0,
        "total_fees":      row.get("total_fees") or 0,
        "win_rate":        round(wins / resolved * 100, 1) if resolved else 0,
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
               ROUND(SUM(COALESCE(pnl, 0)), 2)                AS pnl
        FROM shadow_trades GROUP BY strategy
    """)
    shadow = []
    for r in shadow_rows:
        res = r.get("resolved") or 0
        shadow.append({
            "strategy": r["strategy"],
            "total":    r["total"],
            "resolved": res,
            "wins":     r.get("wins") or 0,
            "pnl":      r.get("pnl") or 0,
            "win_rate": round((r.get("wins") or 0) / res * 100, 1) if res else 0,
        })
    shadow.sort(key=lambda x: -(x["pnl"] or 0))

    return jsonify({"real": real, "shadow": shadow})


@app.route("/api/trades")
def trades():
    return jsonify(db_query("""
        SELECT id, ts, market_name, direction, price, amount, fee,
               order_type, tags, resolved, outcome, pnl, close_ts
        FROM trades ORDER BY id DESC LIMIT 200
    """))


@app.route("/api/shadow-trades")
def shadow_trades():
    return jsonify(db_query("""
        SELECT id, ts, strategy, market_name, direction, price, amount,
               resolved, outcome, pnl, close_ts, confidence
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
        points.append({"ts": (r["close_ts"] or "")[:10], "pnl": round(cumulative, 2)})
    return jsonify(points)


@app.route("/api/scan-log")
def scan_log():
    return jsonify(db_query("""
        SELECT ts, markets_checked, signals_found, trades_placed
        FROM scan_log ORDER BY id DESC LIMIT 30
    """))


if __name__ == "__main__":
    print("━" * 50)
    print("  📊 Dashboard → http://localhost:5000")
    print("━" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False)
