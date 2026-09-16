import csv
import io
import re
import sqlite3
from pathlib import Path
from flask import Flask, jsonify, render_template, request, Response

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "sheet.db"

app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sheet_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            rows INTEGER NOT NULL,
            cols INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cells (
            addr TEXT PRIMARY KEY,
            raw TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cur.execute("INSERT OR IGNORE INTO sheet_meta (id, rows, cols) VALUES (1, 20, 10)")
    conn.commit()
    conn.close()


def col_to_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord('A') + 1)
    return n - 1


def index_to_col(i: int) -> str:
    i += 1
    out = ""
    while i > 0:
        i, rem = divmod(i - 1, 26)
        out = chr(ord('A') + rem) + out
    return out


def parse_addr(addr: str):
    m = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", addr.strip().upper())
    if not m:
        raise ValueError(f"Invalid address: {addr}")
    col = col_to_index(m.group(1))
    row = int(m.group(2)) - 1
    return row, col


def make_addr(row: int, col: int) -> str:
    return f"{index_to_col(col)}{row + 1}"


def iter_range(a: str, b: str):
    r1, c1 = parse_addr(a)
    r2, c2 = parse_addr(b)
    rmin, rmax = sorted((r1, r2))
    cmin, cmax = sorted((c1, c2))
    for r in range(rmin, rmax + 1):
        for c in range(cmin, cmax + 1):
            yield make_addr(r, c)


def as_number(v):
    if v is None:
        return 0.0
    s = str(v).strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def eval_formula(expr: str, raw_map: dict, memo: dict, stack: set):
    text = expr.strip()

    def value_of(addr: str):
        addr = addr.upper()
        if addr in memo:
            return memo[addr]
        if addr in stack:
            return "#CYCLE!"
        stack.add(addr)
        v = evaluate_cell(addr, raw_map, memo, stack)
        stack.remove(addr)
        memo[addr] = v
        return v

    # SUM(A1:B2)
    sum_match = re.fullmatch(r"SUM\(([A-Z]+[0-9]+):([A-Z]+[0-9]+)\)", text, flags=re.IGNORECASE)
    if sum_match:
        total = 0.0
        for a in iter_range(sum_match.group(1), sum_match.group(2)):
            v = value_of(a)
            if isinstance(v, str) and v.startswith("#"):
                continue
            total += as_number(v)
        return total

    # Replace cell references for arithmetic
    def repl(m):
        a = m.group(0).upper()
        v = value_of(a)
        if isinstance(v, str) and v.startswith("#"):
            return "0"
        return str(as_number(v))

    expr_with_vals = re.sub(r"\b[A-Z]+[1-9][0-9]*\b", repl, text.upper())

    if not re.fullmatch(r"[0-9\s\.+\-\*/\(\)]+", expr_with_vals):
        return "#ERR!"

    try:
        return eval(expr_with_vals, {"__builtins__": {}}, {})
    except Exception:
        return "#ERR!"


def evaluate_cell(addr: str, raw_map: dict, memo: dict, stack: set):
    raw = raw_map.get(addr, "")
    if isinstance(raw, str) and raw.startswith("="):
        return eval_formula(raw[1:], raw_map, memo, stack)
    return raw


def current_sheet_model():
    conn = get_db()
    meta = conn.execute("SELECT rows, cols FROM sheet_meta WHERE id = 1").fetchone()
    rows = meta["rows"]
    cols = meta["cols"]
    raw_rows = conn.execute("SELECT addr, raw FROM cells").fetchall()
    conn.close()

    raw_map = {r["addr"]: r["raw"] for r in raw_rows}
    memo = {}
    cells = {}
    for addr, raw in raw_map.items():
        val = evaluate_cell(addr, raw_map, memo, set())
        cells[addr] = {"raw": raw, "value": val}

    return {"rows": rows, "cols": cols, "cells": cells}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/view")
def view_only():
    return render_template("view.html")


@app.route("/api/sheet", methods=["GET"])
def get_sheet():
    return jsonify(current_sheet_model())


@app.route("/api/sheet", methods=["POST"])
def save_sheet():
    data = request.get_json(force=True, silent=False)
    rows = int(data.get("rows", 20))
    cols = int(data.get("cols", 10))
    cells = data.get("cells", {})

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE sheet_meta SET rows = ?, cols = ? WHERE id = 1", (rows, cols))
    cur.execute("DELETE FROM cells")

    for addr, cell in cells.items():
        raw = str((cell or {}).get("raw", ""))
        if raw != "":
            cur.execute("INSERT OR REPLACE INTO cells (addr, raw) VALUES (?, ?)", (addr.upper(), raw))

    conn.commit()
    conn.close()
    return jsonify(current_sheet_model())


@app.route("/api/export.csv", methods=["GET"])
def export_csv():
    model = current_sheet_model()
    output = io.StringIO()
    writer = csv.writer(output)

    for r in range(model["rows"]):
        row_vals = []
        for c in range(model["cols"]):
            a = make_addr(r, c)
            cell = model["cells"].get(a, {"value": ""})
            row_vals.append(cell.get("value", ""))
        writer.writerow(row_vals)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sheet_export.csv"},
    )


@app.route("/api/import.csv", methods=["POST"])
def import_csv():
    if "file" not in request.files:
        return jsonify({"error": "Missing file"}), 400
    f = request.files["file"]
    content = f.read().decode("utf-8", errors="ignore")
    reader = list(csv.reader(io.StringIO(content)))

    rows = len(reader) if reader else 0
    cols = max((len(r) for r in reader), default=0)
    cells = {}

    for r_idx, row in enumerate(reader):
      for c_idx, val in enumerate(row):
            if val != "":
                cells[make_addr(r_idx, c_idx)] = {"raw": val}

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE sheet_meta SET rows = ?, cols = ? WHERE id = 1", (max(rows, 1), max(cols, 1)))
    cur.execute("DELETE FROM cells")
    for addr, cell in cells.items():
        cur.execute("INSERT OR REPLACE INTO cells (addr, raw) VALUES (?, ?)", (addr, cell["raw"]))
    conn.commit()
    conn.close()

    return jsonify(current_sheet_model())


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)
