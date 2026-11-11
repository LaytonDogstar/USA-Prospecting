"""Settings from environment variables. Secrets live in Railway variables or a local .env, never in git."""
import os
from pathlib import Path


def _load_dotenv() -> None:
    env = Path(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def database_url(test: bool = False) -> str:
    """Railway gives postgresql://...; SQLAlchemy needs the psycopg driver name."""
    key = "TEST_DATABASE_URL" if test else "DATABASE_URL"
    default = "postgresql://postgres@localhost:5433/ecosystem" + ("_test" if test else "")
    url = os.environ.get(key, default)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATA_DIR = Path(os.environ.get("ECOSYSTEM_DATA_DIR", "./data"))
RAW_DIR = DATA_DIR / "raw"
REFERENCE_DIR = DATA_DIR / "reference"
