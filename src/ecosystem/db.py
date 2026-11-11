from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import database_url

_engines: dict[str, object] = {}


def engine(url: str | None = None):
    url = url or database_url()
    if url not in _engines:
        _engines[url] = create_engine(url, future=True)
    return _engines[url]


@contextmanager
def session_scope(url: str | None = None):
    """One transaction: commit on success, roll back on any error."""
    s: Session = sessionmaker(bind=engine(url), future=True)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
