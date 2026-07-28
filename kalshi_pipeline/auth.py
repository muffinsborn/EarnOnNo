"""Kalshi request signing: RSA-PSS over `timestamp_ms + METHOD + path`.

Per Kalshi's API docs, every request (including read-only market data) must
carry three headers signed with the account's private key:
  KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE
"""
import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiSigner:
    def __init__(self, api_key_id: str, private_key_path: str):
        self.api_key_id = api_key_id
        with open(private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def headers(self, method: str, path: str) -> dict:
        """`path` must include the /trade-api/v2 prefix and exclude the query string."""
        timestamp_ms = str(int(time.time() * 1000))
        message = (timestamp_ms + method.upper() + path).encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }
