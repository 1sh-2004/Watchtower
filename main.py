"""Sentinel entry point.

    python main.py                 # http://127.0.0.1:8000
    uvicorn main:app               # same app, run by uvicorn

Environment:
    SENTINEL_CONFIG   path to a YAML config (default ./sentinel.yaml, optional)
    SENTINEL_HOST     bind address (default 127.0.0.1; use 0.0.0.0 only in a container)
    SENTINEL_PORT     port (default 8000)
    SENTINEL_DB_PATH  SQLite file (default ./sentinel.db)
"""
import os

import uvicorn

from sentinel.app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("SENTINEL_HOST", "127.0.0.1")
    port = int(os.environ.get("SENTINEL_PORT", "8000"))
    print(f"Sentinel starting: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
