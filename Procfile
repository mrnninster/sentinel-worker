# Heroku / monorepo entry: app code lives in scrape_worker/
web: uvicorn main:app --app-dir scrape_worker --host 0.0.0.0 --port ${PORT:-8100}
