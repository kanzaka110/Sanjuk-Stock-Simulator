"""tests/test_kr_market.py

KRX 수급(외국인·기관) 파일 캐시 테스트 — 배치 사전수집 소비 경로.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── 수급 파일 캐시 (배치 사전수집 소비 경로) ─────────────────────

class TestFrgnFileCache(unittest.TestCase):
    def _rows(self):
        return [{"date": "20260714", "close": 100.0,
                 "inst_shares": 10.0, "foreign_shares": 20.0}]

    def test_file_cache_roundtrip_and_ttl(self):
        import core.kr_market as km
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kr_frgn_cache.json"
            with patch.object(km, "_frgn_file_cache_path", return_value=p):
                km._save_frgn_file_entry("005930", self._rows())
                self.assertEqual(km._load_frgn_file_entry("005930"), self._rows())
                # TTL 초과 → None (stale을 신선한 척 반환하지 않음)
                data = json.loads(p.read_text(encoding="utf-8"))
                data["005930"]["fetched_at"] = "2020-01-01T00:00:00+00:00"
                p.write_text(json.dumps(data), encoding="utf-8")
                self.assertIsNone(km._load_frgn_file_entry("005930"))

    def test_empty_rows_do_not_overwrite(self):
        import core.kr_market as km
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kr_frgn_cache.json"
            with patch.object(km, "_frgn_file_cache_path", return_value=p):
                km._save_frgn_file_entry("005930", self._rows())
                km._save_frgn_file_entry("005930", [])   # 실패분은 무시
                self.assertEqual(km._load_frgn_file_entry("005930"), self._rows())

    def test_fetch_uses_file_cache_without_network(self):
        import core.kr_market as km
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kr_frgn_cache.json"
            with patch.object(km, "_frgn_file_cache_path", return_value=p), \
                 patch.object(km, "_FRGN_CACHE", {}), \
                 patch.object(km.requests, "get",
                              side_effect=AssertionError("network hit")):
                km._save_frgn_file_entry("000660", self._rows())
                self.assertEqual(km._fetch_naver_frgn("000660"), self._rows())

    def test_corrupt_cache_file_returns_none(self):
        import core.kr_market as km
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kr_frgn_cache.json"
            p.write_text("{broken", encoding="utf-8")
            with patch.object(km, "_frgn_file_cache_path", return_value=p):
                self.assertIsNone(km._load_frgn_file_entry("005930"))
