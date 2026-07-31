# Heroku / monorepo entry: app code lives in scrape_worker/
# --workers 1: single process (one heartbeat/pool) — important on Basic 512MB
web: uvicorn main:app --app-dir scrape_worker --host 0.0.0.0 --port ${PORT:-8100} --workers 1
