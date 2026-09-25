"""SQLite connection and schema setup."""
import os
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def data_dir() -> Path:
    return Path(os.environ.get("PROSPECTING_DATA_DIR", "data"))


def output_dir() -> Path:
    return Path(os.environ.get("PROSPECTING_OUTPUT_DIR", "output"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or data_dir() / "prospecting.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text())
    return conn
