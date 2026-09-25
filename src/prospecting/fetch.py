"""Polite page fetcher for company research.

Checks robots.txt, rate-limits per host, and returns readable text.

    python -m prospecting.fetch https://example.com/about [--max-chars 8000]
"""
import argparse
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.robotparser
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

USER_AGENT = "Mozilla/5.0 (compatible; DogstarProspectingResearch/0.1)"
MIN_SECONDS_PER_HOST = 2.0
STAMP_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "prospecting_fetch"


def _ssl_context():
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile) if cafile and Path(cafile).exists() else ssl.create_default_context()


def _get(url: str, timeout: int = 20) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, resp.geturl(), resp.read(3_000_000).decode(charset, errors="replace")


def allowed_by_robots(url: str) -> bool:
    parts = urlparse(url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        _status, _final, body = _get(f"{parts.scheme}://{parts.netloc}/robots.txt", timeout=10)
        rp.parse(body.splitlines())
    except Exception:
        return True  # no readable robots.txt: allowed
    return rp.can_fetch(USER_AGENT, url)


def _wait_for_host(host: str):
    STAMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = STAMP_DIR / host
    if stamp.exists():
        wait = MIN_SECONDS_PER_HOST - (time.time() - stamp.stat().st_mtime)
        if wait > 0:
            time.sleep(wait)
    stamp.touch()


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|footer)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def fetch_text(url: str) -> str:
    if not allowed_by_robots(url):
        raise PermissionError(f"robots.txt disallows {url}")
    _wait_for_host(urlparse(url).netloc)
    status, final_url, body = _get(url)
    return f"URL: {final_url}\nSTATUS: {status}\n\n{html_to_text(body)}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--max-chars", type=int, default=8000)
    args = ap.parse_args(argv)
    try:
        print(fetch_text(args.url)[: args.max_chars])
    except Exception as e:  # report and exit non-zero; caller decides what to do
        print(f"FETCH FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
