# Sheet

Spreadsheet-like web app with data stored directly in the GitHub repo (`sheet_data.json`).

## What changed

- Cell data is now persisted in `sheet_data.json` (tracked by git)
- Every save/import creates a git commit and pushes to `origin/main`
- Any user with push permission on this repo can update sheet content by running the app and saving

## Run

```bash
py -3.11 -m pip install -r requirements.txt
py -3.11 app.py
```

Open:

- Edit: `http://127.0.0.1:5000`
- Read-only: `http://127.0.0.1:5000/view`

## API

- `GET /api/sheet` - read sheet (pull latest from repo first)
- `POST /api/sheet` - save sheet, commit + push
- `POST /api/import.csv` - import csv, commit + push
- `POST /api/sync` - manual git pull --rebase

## Notes

- Requires local git auth already configured for the repo.
- Current sync model is last-write-wins. Concurrent edits can conflict.
