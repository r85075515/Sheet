# Sheet

A lightweight web spreadsheet app (Google Sheets-like basics) with persistent storage.

## Features

- Editable grid cells
- Add rows/columns
- Formula support (`=A1+B2`, `=SUM(A1:B3)`)
- CSV import/export
- Auto-save to SQLite
- Read-only shared view at `/view`

## Run

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
python app.py
```

Open: `http://127.0.0.1:5000`
Read-only view: `http://127.0.0.1:5000/view`
