"""tests/test_briefing_display_policy.py

briefing_display_policy + notion.py 포트폴리오 렌더링 정책 테스트.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.briefing_display_policy import should_render_full_portfolio


# ─── 1. should_render_full_portfolio ──────────────────────────────

class TestShouldRenderFullPortfolio(unittest.TestCase):
    def test_kr_open_true(self):
        self.assertTrue(should_render_full_portfolio("KR_OPEN"))

    def test_kr_before_true(self):
        self.assertTrue(should_render_full_portfolio("KR_BEFORE"))

    def test_kr_night_false(self):
        self.assertFalse(should_render_full_portfolio("KR_NIGHT"))

    def test_us_night_false(self):
        self.assertFalse(should_render_full_portfolio("US_NIGHT"))

    def test_us_close_false(self):
        self.assertFalse(should_render_full_portfolio("US_CLOSE"))

    def test_us_before_false(self):
        self.assertFalse(should_render_full_portfolio("US_BEFORE"))

    def test_manual_false(self):
        self.assertFalse(should_render_full_portfolio("MANUAL"))

    def test_empty_string_false(self):
        self.assertFalse(should_render_full_portfolio(""))

    def test_unknown_type_false(self):
        self.assertFalse(should_render_full_portfolio("CUSTOM"))


# ─── 2. notion.py 렌더링 분기 ─────────────────────────────────────

def _make_snapshot(movers: dict[str, float] | None = None):
    """MarketSnapshot mock — stocks에 pct 지정."""
    snap = MagicMock()
    stocks = {}
    base = {"NVDA": ("엔비디아", 0.5), "MU": ("마이크론", -1.2), "069500.KS": ("KODEX200", 2.1)}
    if movers:
        base.update(movers)
    for tk, (name, pct) in base.items():
        q = MagicMock()
        q.name = name
        q.pct = pct
        q.price = 100.0
        q.change = pct
        q.high = 110.0
        q.low = 90.0
        stocks[tk] = q
    snap.stocks = stocks
    return snap


def _make_result():
    result = MagicMock()
    result.portfolio_signals = []
    result.raw_json = {"portfolio_rows": []}
    result.strategy_summary = ""
    result.advisor_summary = ""
    result.consensus_text = ""
    result.persona_summaries = []
    return result


class TestNotionPortfolioRender(unittest.TestCase):
    """save_to_notion 내부 블록 조합 검증."""

    def _collect_blocks(self, briefing_type: str, snapshot=None) -> list[dict]:
        """save_to_notion 호출 없이 블록 조합 로직만 테스트."""
        from core import notion as n
        if snapshot is None:
            snapshot = _make_snapshot()
        result = _make_result()
        from core.briefing_display_policy import should_render_full_portfolio

        blocks = []
        if should_render_full_portfolio(briefing_type):
            blocks += n._section_portfolio(result, snapshot)
        else:
            blocks += n._section_portfolio_summary(result, snapshot)
        if should_render_full_portfolio(briefing_type):
            blocks += n._section_portfolio_raw(snapshot)
        return blocks

    def _block_texts(self, blocks: list[dict]) -> str:
        """블록 내 텍스트 합치기 (단순 검사용)."""
        parts = []
        for b in blocks:
            for key in ("heading_2", "heading_1", "paragraph", "callout", "table"):
                if key in b:
                    val = b[key]
                    if isinstance(val, dict):
                        for rts in val.get("rich_text", []):
                            parts.append(rts.get("text", {}).get("content", ""))
                        if "children" in val:
                            for child in val["children"]:
                                for rts in child.get("table_row", {}).get("cells", []):
                                    for rt in rts:
                                        parts.append(rt.get("text", {}).get("content", ""))
        return "\n".join(parts)

    def test_kr_open_has_portfolio_header(self):
        blocks = self._collect_blocks("KR_OPEN")
        texts = self._block_texts(blocks)
        self.assertIn("보유 종목 브리핑", texts)

    def test_kr_before_has_portfolio_header(self):
        blocks = self._collect_blocks("KR_BEFORE")
        texts = self._block_texts(blocks)
        self.assertIn("보유 종목 브리핑", texts)

    def test_kr_open_has_raw_section(self):
        """KR_OPEN은 실시간 현황 섹션 포함."""
        blocks = self._collect_blocks("KR_OPEN")
        texts = self._block_texts(blocks)
        self.assertIn("포트폴리오 실시간 현황", texts)

    def test_kr_night_has_summary_not_full(self):
        blocks = self._collect_blocks("KR_NIGHT")
        texts = self._block_texts(blocks)
        self.assertIn("포트폴리오 요약", texts)
        self.assertNotIn("보유 종목 브리핑", texts)

    def test_us_night_summary_only(self):
        blocks = self._collect_blocks("US_NIGHT")
        texts = self._block_texts(blocks)
        self.assertIn("포트폴리오 요약", texts)
        self.assertNotIn("보유 종목 브리핑", texts)

    def test_us_close_summary_only(self):
        blocks = self._collect_blocks("US_CLOSE")
        texts = self._block_texts(blocks)
        self.assertIn("포트폴리오 요약", texts)
        self.assertNotIn("보유 종목 브리핑", texts)

    def test_manual_summary_only(self):
        blocks = self._collect_blocks("MANUAL")
        texts = self._block_texts(blocks)
        self.assertIn("포트폴리오 요약", texts)
        self.assertNotIn("보유 종목 브리핑", texts)

    def test_kr_night_no_raw_section(self):
        """KR_NIGHT에는 실시간 현황 섹션 없음."""
        blocks = self._collect_blocks("KR_NIGHT")
        texts = self._block_texts(blocks)
        self.assertNotIn("포트폴리오 실시간 현황", texts)


# ─── 3. portfolio_summary 내용 ────────────────────────────────────

class TestPortfolioSummaryContent(unittest.TestCase):
    def _summary_text(self, movers: dict | None = None) -> str:
        from core import notion as n
        snap = _make_snapshot(movers)
        result = _make_result()
        blocks = n._section_portfolio_summary(result, snap)
        parts = []
        for b in blocks:
            if "callout" in b:
                for rt in b["callout"].get("rich_text", []):
                    parts.append(rt.get("text", {}).get("content", ""))
        return "\n".join(parts)

    def test_summary_mentions_morning_briefing(self):
        text = self._summary_text()
        self.assertIn("아침 브리핑", text)

    def test_summary_mentions_portfolio_command(self):
        text = self._summary_text()
        self.assertIn("/portfolio", text)

    def test_summary_shows_big_movers(self):
        # ±3% 이상 종목 포함 snapshot
        snap_movers = {"SOFI": ("SOFI", 5.5), "PLTR": ("팔란티어", -4.1)}
        text = self._summary_text(snap_movers)
        self.assertIn("큰 변동 종목", text)

    def test_summary_no_big_movers(self):
        # 모두 ±3% 미만
        from core import notion as n
        snap = _make_snapshot({"NVDA": ("엔비디아", 0.5), "MU": ("마이크론", 1.2)})
        result = _make_result()
        blocks = n._section_portfolio_summary(result, snap)
        texts = []
        for b in blocks:
            if "callout" in b:
                for rt in b["callout"].get("rich_text", []):
                    texts.append(rt.get("text", {}).get("content", ""))
        combined = "\n".join(texts)
        self.assertIn("큰 변동 종목 없음", combined)

    def test_summary_max_3_movers(self):
        # 4개 이상 ±3% 종목
        snap_movers = {
            "A": ("A", 5.0), "B": ("B", -4.0),
            "C": ("C", 6.0), "D": ("D", -3.5),
        }
        text = self._summary_text(snap_movers)
        # 최대 3개만 표시 (▲ 또는 ▼ count ≤ 3)
        count = text.count("▲") + text.count("▼")
        self.assertLessEqual(count, 3)


# ─── 4. multi_agent.py holdings_text 제거 안 됨 ───────────────────

class TestMultiAgentHoldingsIntact(unittest.TestCase):
    def test_holdings_text_still_in_multi_agent(self):
        src = (_ROOT / "core" / "multi_agent.py").read_text(encoding="utf-8")
        self.assertIn("holdings_text", src)

    def test_briefing_display_policy_not_in_multi_agent(self):
        """multi_agent.py는 display policy를 사용하지 않아야 함 — AI context는 항상 full."""
        src = (_ROOT / "core" / "multi_agent.py").read_text(encoding="utf-8")
        self.assertNotIn("briefing_display_policy", src)


# ─── 5. settings.py 변경 없음 ────────────────────────────────────

class TestSettingsUnchanged(unittest.TestCase):
    def test_briefing_display_policy_not_imported_in_settings(self):
        src = (_ROOT / "config" / "settings.py").read_text(encoding="utf-8")
        self.assertNotIn("briefing_display_policy", src)


# ─── 6. write routes 없음 ────────────────────────────────────────

class TestNoWriteRoutes(unittest.TestCase):
    def test_no_write_routes_in_web_app(self):
        src = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        for pat in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            self.assertNotIn(pat, src.lower())


# ─── 7. telegram_bot.py /portfolio 경로 보존 ─────────────────────

class TestTelegramBotPortfolio(unittest.TestCase):
    def test_portfolio_command_still_in_bot(self):
        src = (_ROOT / "core" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("portfolio", src.lower())

    def test_briefing_display_policy_not_in_telegram_bot(self):
        """telegram_bot.py는 display policy 영향 없어야 함."""
        src = (_ROOT / "core" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertNotIn("briefing_display_policy", src)


if __name__ == "__main__":
    unittest.main()
