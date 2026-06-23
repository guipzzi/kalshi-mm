"""
Signed REST client for the Kalshi trading API (v2).

Read-only by default: market and portfolio reads work with a key; no order is
sent unless create_order()/cancel_order() is called explicitly. Auth is the
standard Kalshi scheme — RSA-PSS / SHA-256 over `timestamp_ms + METHOD + path`,
where `path` includes /trade-api/v2 and excludes the query string.

Credentials come from the environment only:
  KALSHI_API_KEY_ID                               key id
  KALSHI_PRIVATE_KEY | KALSHI_PRIVATE_KEY_PATH    RSA private key (PEM inline or file)
"""
from __future__ import annotations

import base64
import os
import socket
import time
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


# --------------------------------------------------------------------------- #
# DNS resilience: some resolvers (VPN/CGNAT) fail for the API host even when
# the internet is fine. If the local resolver can't resolve it, fetch the IP
# via DNS-over-HTTPS (reachable by literal IP) and pin it. Best-effort.
# --------------------------------------------------------------------------- #
_DNS_PINS: dict[str, str] = {}
_real_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, *args, **kwargs):
    return _real_getaddrinfo(_DNS_PINS.get(host, host), *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo


def _doh_resolve(host: str) -> Optional[str]:
    for doh_ip in ("1.1.1.1", "8.8.8.8"):
        try:
            r = requests.get(f"https://{doh_ip}/dns-query",
                             params={"name": host, "type": "A"},
                             headers={"accept": "application/dns-json"},
                             timeout=5)
            for a in r.json().get("Answer", []):
                if a.get("type") == 1:           # 1 = A record
                    return a["data"]
        except Exception:
            continue
    return None


def ensure_resolvable(url_host: str) -> bool:
    if url_host in _DNS_PINS:
        return True
    try:
        _real_getaddrinfo(url_host, 443)
        return True
    except socket.gaierror:
        ip = _doh_resolve(url_host)
        if ip:
            _DNS_PINS[url_host] = ip
            return True
        return False


HOSTS = {
    "prod": "https://external-api.kalshi.com",
    "demo": "https://external-api.demo.kalshi.co",
}
PREFIX = "/trade-api/v2"


def load_private_key(*, pem: Optional[str] = None,
                     path: Optional[str] = None) -> RSAPrivateKey:
    """Load the RSA private key from a PEM string or a file. Order: explicit
    argument -> KALSHI_PRIVATE_KEY (inline PEM, literal \\n accepted) ->
    KALSHI_PRIVATE_KEY_PATH (.pem path)."""
    if pem is None and path is None:
        pem = os.getenv("KALSHI_PRIVATE_KEY")
        path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if pem:
        data = pem.replace("\\n", "\n").encode()
    elif path:
        with open(os.path.expanduser(path), "rb") as f:
            data = f.read()
    else:
        raise ValueError("private key missing: set KALSHI_PRIVATE_KEY (PEM) "
                         "or KALSHI_PRIVATE_KEY_PATH (.pem file)")
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("provided key is not RSA")
    return key


def _sign(private_key: RSAPrivateKey, ts_ms: int, method: str, path: str) -> str:
    msg = f"{ts_ms}{method.upper()}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


class KalshiClient:
    """Signed REST client. Read-only unless create_order/cancel_order is called.
    Use env='demo' for the sandbox."""

    def __init__(self, key_id: Optional[str] = None,
                 private_key: Optional[RSAPrivateKey] = None,
                 env: str = "prod", timeout: float = 15.0,
                 require_auth: bool = False):
        self.key_id = key_id or os.getenv("KALSHI_API_KEY_ID")
        self.private_key = private_key
        if self.key_id and self.private_key is None:
            try:
                self.private_key = load_private_key()
            except Exception:
                self.private_key = None
        self.authed = bool(self.key_id and self.private_key)
        if require_auth and not self.authed:
            raise ValueError("missing Kalshi credential (KALSHI_API_KEY_ID + key)")
        if env not in HOSTS:
            raise ValueError(f"invalid env: {env} (use 'prod' or 'demo')")
        self.env = env
        self.host = HOSTS[env]
        self.timeout = timeout
        from urllib.parse import urlparse
        ensure_resolvable(urlparse(self.host).hostname)
        self._s = requests.Session()
        self._s.headers.update({"Accept": "application/json",
                                "User-Agent": "mm/1.0"})

    def _headers(self, method: str, sign_path: str) -> dict:
        if not self.authed:
            return {}                                  # public mode: no auth headers
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": _sign(self.private_key, ts, method, sign_path),
        }

    def _request(self, method: str, endpoint: str,
                 params: Optional[dict] = None,
                 json: Optional[dict] = None) -> Optional[object]:
        """endpoint starts with '/', e.g. '/markets'. The signature uses the path
        WITH the /trade-api/v2 prefix and WITHOUT the query string."""
        sign_path = PREFIX + endpoint
        url = self.host + sign_path
        try:
            r = self._s.request(method, url, params=params, json=json,
                                 headers=self._headers(method, sign_path),
                                 timeout=self.timeout)
            r.raise_for_status()
            return r.json() if r.content else {}
        except requests.HTTPError:
            try:
                return {"_http_error": r.status_code, "_body": r.json()}
            except Exception:
                return {"_http_error": r.status_code, "_body": r.text[:300]}
        except requests.RequestException as e:
            return {"_error": str(e)}

    # -- reads --------------------------------------------------------------- #
    def markets(self, *, series_ticker: Optional[str] = None,
                event_ticker: Optional[str] = None,
                status: Optional[str] = None, limit: int = 200,
                cursor: Optional[str] = None) -> dict:
        p = {"limit": limit}
        if series_ticker:
            p["series_ticker"] = series_ticker
        if event_ticker:
            p["event_ticker"] = event_ticker
        if status:
            p["status"] = status
        if cursor:
            p["cursor"] = cursor
        return self._request("GET", "/markets", params=p)

    def balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def positions(self, *, limit: int = 200, cursor: Optional[str] = None) -> dict:
        p = {"limit": limit}
        if cursor:
            p["cursor"] = cursor
        return self._request("GET", "/portfolio/positions", params=p)

    def orders(self, *, ticker: Optional[str] = None,
               status: Optional[str] = None, limit: int = 200) -> dict:
        p = {"limit": limit}
        if ticker:
            p["ticker"] = ticker
        if status:
            p["status"] = status
        return self._request("GET", "/portfolio/orders", params=p)

    def fills(self, *, ticker: Optional[str] = None, limit: int = 200) -> dict:
        p = {"limit": limit}
        if ticker:
            p["ticker"] = ticker
        return self._request("GET", "/portfolio/fills", params=p)

    # -- writes (explicit only): sends a real order -------------------------- #
    def create_order(self, *, ticker: str, side: str, count: int,
                     price: float,
                     time_in_force: str = "good_till_canceled",
                     self_trade_prevention_type: str = "taker_at_cross",
                     client_order_id: Optional[str] = None,
                     post_only: bool = False,
                     expiration_ts: Optional[int] = None) -> dict:
        """POST /portfolio/events/orders (V2) — SENDS A REAL ORDER.

        Single-book model: side 'bid' (buy YES) | 'ask' (sell YES). count and
        price are fixed-point strings; price in dollars (0..1). post_only=True
        forces a passive maker order (rejected if it would cross). expiration_ts
        (unix seconds) auto-cancels a GTC order. Unique client_order_id = idempotency.
        """
        body = {
            "ticker": ticker,
            "side": side,
            "count": str(int(count)),
            "price": f"{float(price):.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
        }
        if post_only:
            body["post_only"] = True
        if client_order_id:
            body["client_order_id"] = client_order_id
        if expiration_ts:
            body["expiration_time"] = int(expiration_ts)
        return self._request("POST", "/portfolio/events/orders", json=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}")
