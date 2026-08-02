# Marathon Runners Depository

This app is a Python web server for registering and managing marathon runners.

## Deploying to Render

1. Create a Render account and connect your GitHub repository.
2. Set up a new Web Service using `render.yaml`.
3. Add environment variables:
   - `DATABASE_URL` (Postgres connection string)
   - `GOOGLE_SHEETS_ENABLED` (optional, `true` or `false`)
   - `GOOGLE_SHEETS_SPREADSHEET_ID` (optional)
   - `GOOGLE_SHEETS_CREDENTIALS_JSON` (optional service account JSON)
4. Deploy.

## Local development

```bash
pip install -r requirements.txt
python app.py
```

If no `DATABASE_URL` is provided, the app will use local `runners.db`.
