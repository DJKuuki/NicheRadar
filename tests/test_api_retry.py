from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot.api import PolymarketApiClient


class FakeResponse:
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps([]).encode("utf-8")


class ApiRetryTests(unittest.TestCase):
    def test_get_json_retries_transient_failures(self) -> None:
        calls = {"count": 0}

        def fake_urlopen(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TimeoutError("temporary")
            return FakeResponse()

        client = PolymarketApiClient(retries=2, retry_backoff=0)

        with patch("bot.api.urlopen", side_effect=fake_urlopen):
            payload = client.list_markets_by_params({"closed": "false"})

        self.assertEqual(payload, [])
        self.assertEqual(calls["count"], 2)

    def test_get_json_uses_cache_before_network(self) -> None:
        calls = {"count": 0}

        def fake_urlopen(*args, **kwargs):
            calls["count"] += 1
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            client = PolymarketApiClient(
                cache_path=str(Path(directory) / "http_cache.sqlite"),
                gamma_cache_seconds=60,
            )
            with patch("bot.api.urlopen", side_effect=fake_urlopen):
                self.assertEqual(client.list_markets_by_params({"closed": "false"}), [])
                self.assertEqual(client.list_markets_by_params({"closed": "false"}), [])

        self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()
