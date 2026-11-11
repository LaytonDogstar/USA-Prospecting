"""Cache for external API responses (`http_cache`). Never re-fetch inside the cache window unless forced."""
import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone

from . import models as m


def key(provider: str, url: str, params: dict) -> str:
    return hashlib.sha256(json.dumps([provider, url, sorted(params.items())], default=str).encode()).hexdigest()


def get(s, provider: str, url: str, params: dict):
    row = s.get(m.HttpCache, key(provider, url, params))
    if row is None or row.expires_at < datetime.now(timezone.utc):
        return None
    return json.loads(gzip.decompress(row.body))


def put(s, provider: str, url: str, params: dict, status: int, data, days: int) -> None:
    k = key(provider, url, params)
    body = gzip.compress(json.dumps(data).encode())
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    row = s.get(m.HttpCache, k)
    if row is None:
        s.add(m.HttpCache(request_key=k, provider=provider, url=url, status=status, body=body, expires_at=expires))
    else:
        row.status, row.body, row.fetched_at, row.expires_at = status, body, datetime.now(timezone.utc), expires
    s.flush()
