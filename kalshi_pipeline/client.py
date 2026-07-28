"""Thin Kalshi REST client: signing, rate limiting, retries, pagination.

Read-only by design - this module only issues GET requests.
"""
import logging
import random
import time
from urllib.parse import urlparse

import requests

from .auth import KalshiSigner
from .config import Config

logger = logging.getLogger("kalshi_pipeline.client")

# Kalshi caps candlestick responses around 10k rows; stay comfortably under that
# per request by windowing the start_ts/end_ts range we ask for.
MAX_CANDLES_PER_REQUEST = 9000


class KalshiAPIError(RuntimeError):
    def __init__(self, status_code: int, path: str, body: str):
        self.status_code = status_code
        self.path = path
        self.body = body
        super().__init__(f"Kalshi API error {status_code} on {path}: {body[:500]}")


class KalshiClient:
    def __init__(self, config: Config):
        self.base_url = config.api_base
        self.base_path = urlparse(config.api_base).path  # e.g. /trade-api/v2
        self.signer = KalshiSigner(config.api_key_id, config.private_key_path)
        self.session = requests.Session()
        self.min_interval = 1.0 / config.max_requests_per_second
        self._last_request_at = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict | None = None, max_retries: int = 10) -> dict:
        url = self.base_url + path
        sign_path = self.base_path + path
        attempt = 0
        while True:
            self._throttle()
            headers = self.signer.headers("GET", sign_path)
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
            except requests.RequestException as exc:
                if attempt >= max_retries:
                    raise
                backoff = min(60, 2 ** attempt) + random.uniform(0, 1)
                logger.warning("Network error on %s (%s); retrying in %.1fs", path, exc, backoff)
                time.sleep(backoff)
                attempt += 1
                continue

            if resp.status_code == 200:
                return resp.json()

            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt < max_retries:
                backoff = min(60, 2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "%s on %s (attempt %d/%d); retrying in %.1fs",
                    resp.status_code, path, attempt + 1, max_retries, backoff,
                )
                time.sleep(backoff)
                attempt += 1
                continue

            raise KalshiAPIError(resp.status_code, path, resp.text)

    def get_series_list(self) -> list[dict]:
        data = self._get("/series")
        return data.get("series", [])

    def iter_events(self, status: str | None = None, limit: int = 200):
        cursor = None
        while True:
            params = {"limit": limit}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            data = self._get("/events", params=params)
            events = data.get("events", [])
            for event in events:
                yield event
            cursor = data.get("cursor") or None
            if not cursor or not events:
                break

    def get_event(self, event_ticker: str) -> dict | None:
        """Fetch a single event. Needed as a fallback for multivariate-event
        (MVE) combo markets, whose parent events don't appear in the bulk
        /events listing but do exist individually."""
        data = self._get(f"/events/{event_ticker}")
        return data.get("event")

    def iter_markets(self, status: str, limit: int = 1000, mve_filter: str | None = "exclude"):
        cursor = None
        while True:
            params = {"limit": limit, "status": status}
            if mve_filter:
                params["mve_filter"] = mve_filter
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params)
            markets = data.get("markets", [])
            for market in markets:
                yield market
            cursor = data.get("cursor") or None
            if not cursor or not markets:
                break

    def iter_candlesticks(
        self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int
    ):
        """Yields candlestick dicts across [start_ts, end_ts], chunking the range
        so no single request risks exceeding Kalshi's response size cap."""
        window_seconds = MAX_CANDLES_PER_REQUEST * period_interval * 60
        window_start = start_ts
        while window_start <= end_ts:
            window_end = min(window_start + window_seconds, end_ts)
            path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
            data = self._get(
                path,
                params={
                    "start_ts": window_start,
                    "end_ts": window_end,
                    "period_interval": period_interval,
                },
            )
            candles = data.get("candlesticks", [])
            for candle in candles:
                yield candle
            window_start = window_end + 1
