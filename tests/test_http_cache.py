from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from bot.http_cache import HttpCache


class HttpCacheTests(unittest.TestCase):
    def test_get_returns_none_after_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = HttpCache(str(Path(directory) / "cache.sqlite"))
            cache.set("key", "value", ttl_seconds=0.01)
            self.assertEqual(cache.get("key"), "value")
            time.sleep(0.02)
            self.assertIsNone(cache.get("key"))


if __name__ == "__main__":
    unittest.main()
