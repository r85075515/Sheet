import csv
import io
import json
import os
import re
import subprocess
from pathlib import Path
from flask import Flask, jsonify, render_template, request, Response

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "sheet_data.json"

app = Flask(__name__)


def run_git(*args):
    return subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )


def sync_from_repo():
    if os.getenv("SHEET_DISABLE_GIT_SYNC") == "1":
        return {"ok": True, "skipped": True}
    pull = run_git("pull", "--rebase", "origin", "main")
    return {
        "ok": pull.returncode == 0,
        "stdout": pull.stdout.strip(),
        "stderr": pull.stderr.strip(),
    }


def commit_and_push():
    if os.getenv("SHEET_DISABLE_GIT_SYNC") == "1":
        return {"ok": True, "skipped": True}

    add = run_git("add", "sheet_data.json")
    if add.returncode != 0:
        return {"ok": False, "step": "add", "stderr": add.stderr.strip()}

    commit = run_git("commit", "-m", "Update sheet cells")
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
        return {
            "ok": False,
            "step": "commit",
            "stdout": commit.stdout.strip(),
            "stderr": commit.stderr.strip(),
        }

    push = run_git("push", "origin", "main")
    return {
        "ok": push.returncode == 0,
        "step": "push",
        "stdout": push.stdout.strip(),
        "stderr": push.stderr.strip(),
    }


def load_model():
    if not DATA_PATH.exists():
        model = {"rows": 20, "cols": 10, "cells": {}}
        DATA_PATH.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        return model
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def save_model(model):
    DATA_PATH.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")


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

    sum_match = re.fullmatch(r"SUM\(([A-Z]+[0-9]+):([A-Z]+[0-9]+)\)", text, flags=re.IGNORECASE)
    if sum_match:
        total = 0.0
        for a in iter_range(sum_match.group(1), sum_match.group(2)):
            v = value_of(a)
            if isinstance(v, str) and v.startswith("#"):
                continue
            total += as_number(v)
        return total

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
    model = load_model()
    rows = int(model.get("rows", 20))
    cols = int(model.get("cols", 10))
    input_cells = model.get("cells", {})

    raw_map = {k.upper(): str((v or {}).get("raw", "")) for k, v in input_cells.items() if str((v or {}).get("raw", "")) != ""}

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


@app.route("/api/sync", methods=["POST"])
def sync_now():
    result = sync_from_repo()
    code = 200 if result.get("ok") else 500
    return jsonify(result), code


@app.route("/api/sheet", methods=["GET"])
def get_sheet():
    sync_from_repo()
    return jsonify(current_sheet_model())


@app.route("/api/sheet", methods=["POST"])
def save_sheet_route():
    data = request.get_json(force=True, silent=False)
    rows = int(data.get("rows", 20))
    cols = int(data.get("cols", 10))
    cells = data.get("cells", {})

    normalized = {}
    for addr, cell in cells.items():
        raw = str((cell or {}).get("raw", ""))
        if raw != "":
            normalized[addr.upper()] = {"raw": raw}

    model = {"rows": rows, "cols": cols, "cells": normalized}
    save_model(model)

    sync_result = commit_and_push()
    payload = current_sheet_model()
    payload["git_sync"] = sync_result
    code = 200 if sync_result.get("ok") else 500
    return jsonify(payload), code


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

    rows = len(reader) if reader else 1
    cols = max((len(r) for r in reader), default=1)
    cells = {}

    for r_idx, row in enumerate(reader):
        for c_idx, val in enumerate(row):
            if val != "":
                cells[make_addr(r_idx, c_idx)] = {"raw": val}

    save_model({"rows": rows, "cols": cols, "cells": cells})
    sync_result = commit_and_push()
    payload = current_sheet_model()
    payload["git_sync"] = sync_result
    code = 200 if sync_result.get("ok") else 500
    return jsonify(payload), code


if __name__ == "__main__":
    load_model()
    app.run(host="127.0.0.1", port=5000, debug=False)
