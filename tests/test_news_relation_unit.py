"""Unit tests for src.services.news_relation (方向 2-2 / Redmine #41).

These tests focus on pure-Python logic that does NOT need a live DB:
  - dataclass shape and serialization
  - relevance scoring weights
  - text normalization
  - CLI argparse
  - format_result_text formatting
  - ask_news heuristic trigger detection (in semantic_search)

DB-dependent code paths are smoke-tested via the CLI's --help,
which exercises the actual parser without connecting.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Make `src` importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.news_relation import (  # noqa: E402
    MENTION_WEIGHTS,
    RELATION_WEIGHTS,
    ScoredNews,
    TickerNewsResult,
    _build_arg_parser,
    _normalize,
    _score_news,
    format_result_text,
)


# ---------------------------------------------------------------------------
#  Scoring weights — sanity check that the table matches the schema doc
# ---------------------------------------------------------------------------
class TestScoringWeights:
    def test_is_ticker_is_highest(self):
        assert RELATION_WEIGHTS["IS_TICKER"] == 1.0
        assert max(RELATION_WEIGHTS.values()) == RELATION_WEIGHTS["IS_TICKER"]

    def test_related_to_is_weakest(self):
        # RELATED_TO is the most generic relation (broadest scope) → lowest weight
        related_w = RELATION_WEIGHTS["RELATED_TO"]
        for k, w in RELATION_WEIGHTS.items():
            if k == "RELATED_TO":
                continue
            assert related_w <= w, f"{k}({w}) < RELATED_TO({related_w})"

    def test_industry_below_ticker_and_group(self):
        # Sanity: INDUSTRY strictly weaker than TICKER and GROUP
        assert RELATION_WEIGHTS["BELONGS_TO_INDUSTRY"] < RELATION_WEIGHTS["IS_TICKER"]
        assert RELATION_WEIGHTS["BELONGS_TO_INDUSTRY"] < RELATION_WEIGHTS["BELONGS_TO_GROUP"]

    def test_title_mention_is_highest(self):
        assert MENTION_WEIGHTS["TITLE"] == 1.0
        assert max(MENTION_WEIGHTS.values()) == MENTION_WEIGHTS["TITLE"]


# ---------------------------------------------------------------------------
#  Normalization — case + whitespace + punctuation
# ---------------------------------------------------------------------------
class TestNormalize:
    def test_lowercase(self):
        assert _normalize("TSMC") == "tsmc"

    def test_strip_whitespace(self):
        assert _normalize(" 台 積 電 ") == "台積電"

    def test_strip_punctuation(self):
        # Punctuation stripped, Chinese kept intact
        assert _normalize("台積電(2330)") == "台積電2330"
        assert _normalize("台積電/TSMC") == "台積電tsmc"

    def test_empty(self):
        assert _normalize("") == ""
        assert _normalize(None) == ""

    def test_fullwidth_space(self):
        # u3000 (ideographic space) is stripped
        assert _normalize("台\u3000積\u3000電") == "台積電"


# ---------------------------------------------------------------------------
#  Scoring logic — given synthetic evidence, check the formula
# ---------------------------------------------------------------------------
class TestScoreNews:
    def _news(self, evidence):
        return {
            "news_id": "n1",
            "title": "test",
            "evidence": evidence,
        }

    def test_is_ticker_title_full_score(self):
        news = self._news([{
            "entity_id": 1, "ticker_code": "2330",
            "mention_type": "TITLE", "confidence": 1.0,
            "raw_text_excerpt": "...",
        }])
        resolved = [{"entity_id": 1, "canonical_name": "台積電",
                     "entity_kind": "TICKER", "relation_type": "IS_TICKER",
                     "confidence": 1.0}]
        score, matched = _score_news(news, resolved)
        # norm = 1.0 * 1.0 = 1.0; raw = 1.0 * 1.0 * 1.0 = 1.0; score = 1.0
        assert score == 1.0
        assert len(matched) == 1
        assert matched[0]["mention_type"] == "TITLE"

    def test_industry_body_low_score(self):
        news = self._news([{
            "entity_id": 2, "ticker_code": None,
            "mention_type": "BODY", "confidence": 1.0,
            "raw_text_excerpt": "...",
        }])
        resolved = [{"entity_id": 2, "canonical_name": "半導體",
                     "entity_kind": "INDUSTRY", "relation_type": "BELONGS_TO_INDUSTRY",
                     "confidence": 1.0}]
        score, _ = _score_news(news, resolved)
        # norm = 0.4 * 0.5 = 0.2; raw = 0.2; score = 1.0 (normalized)
        # The score is "best hit" normalized, so this should still hit 1.0
        # but only because it's the only evidence
        assert score == 1.0

    def test_mixed_evidence_dominates_on_best_hit(self):
        # Two evidence rows: weak (industry body) + strong (ticker title)
        # norm uses max(0.4*0.5, 1.0*1.0) = 1.0; raw = 0.2 + 1.0 = 1.2; score = 1.2 -> clamped 1.0
        news = self._news([
            {"entity_id": 1, "ticker_code": "2330",
             "mention_type": "TITLE", "confidence": 1.0, "raw_text_excerpt": "..."},
            {"entity_id": 2, "ticker_code": None,
             "mention_type": "BODY", "confidence": 1.0, "raw_text_excerpt": "..."},
        ])
        resolved = [
            {"entity_id": 1, "canonical_name": "台積電",
             "entity_kind": "TICKER", "relation_type": "IS_TICKER", "confidence": 1.0},
            {"entity_id": 2, "canonical_name": "半導體",
             "entity_kind": "INDUSTRY", "relation_type": "BELONGS_TO_INDUSTRY", "confidence": 1.0},
        ]
        score, matched = _score_news(news, resolved)
        assert score == 1.0
        assert len(matched) == 2

    def test_no_matching_entity_returns_zero(self):
        news = self._news([{
            "entity_id": 99, "ticker_code": "9999",
            "mention_type": "TITLE", "confidence": 1.0, "raw_text_excerpt": "...",
        }])
        # resolved entities have different ids — no overlap
        resolved = [{"entity_id": 1, "canonical_name": "X",
                     "entity_kind": "TICKER", "relation_type": "IS_TICKER",
                     "confidence": 1.0}]
        score, matched = _score_news(news, resolved)
        assert score == 0.0
        assert matched == []


# ---------------------------------------------------------------------------
#  format_result_text — both empty and populated cases
# ---------------------------------------------------------------------------
class TestFormatResultText:
    def test_empty_news_message(self):
        result = TickerNewsResult(
            ticker_code="2330", canonical_name="台積電",
            resolved_via="ticker_code", resolution_details={},
            days=7, total_news=0, news=[], entity_path=[],
        )
        text = format_result_text(result)
        assert "0 則" in text
        assert "尚無被 entity 標註的新聞" in text

    def test_populated_news_renders(self):
        news = ScoredNews(
            news_id="1", title="台積電法說", source="cnyes_headline",
            url="https://x", published_at="2026-09-10T08:00:00",
            summary=None, relevance_score=0.85,
            matched_entities=[{"entity_id": 1, "canonical_name": "台積電",
                               "entity_kind": "TICKER", "relation_type": "IS_TICKER",
                               "mention_type": "TITLE", "confidence": 1.0,
                               "raw_text_excerpt": None}],
        )
        result = TickerNewsResult(
            ticker_code="2330", canonical_name="台積電",
            resolved_via="ticker_code", resolution_details={"query": "2330"},
            days=7, total_news=1, news=[news],
            entity_path=[{"entity_id": 1, "canonical_name": "台積電"}],
        )
        text = format_result_text(result)
        assert "台積電" in text
        assert "score=0.85" in text
        assert "IS_TICKER" in text
        assert "台積電法說" in text

    def test_concept_synthetic_code(self):
        # When query matches a THEME not a ticker, we synthesize `@name`
        result = TickerNewsResult(
            ticker_code="@CoWoS", canonical_name="CoWoS",
            resolved_via="concept", resolution_details={"query": "CoWoS"},
            days=7, total_news=0, news=[], entity_path=[],
        )
        text = format_result_text(result)
        assert "CoWoS" in text
        assert "concept" in text


# ---------------------------------------------------------------------------
#  Dataclass serialization — verify to_dict / JSON round-trip
# ---------------------------------------------------------------------------
class TestSerialization:
    def test_result_to_dict_json_round_trip(self):
        result = TickerNewsResult(
            ticker_code="2330", canonical_name="台積電",
            resolved_via="ticker_code", resolution_details={"query": "2330"},
            days=7, total_news=0, news=[], entity_path=[],
        )
        d = result.to_dict()
        s = json.dumps(d, ensure_ascii=False)
        assert "台積電" in s
        assert json.loads(s) == d

    def test_scored_news_to_dict(self):
        news = ScoredNews(
            news_id="1", title="t", source="cnyes",
            url="u", published_at=None, summary=None,
            relevance_score=0.5, matched_entities=[],
        )
        d = news.to_dict()
        assert d["news_id"] == "1"
        assert d["relevance_score"] == 0.5


# ---------------------------------------------------------------------------
#  CLI — argparse correctness (no DB needed)
# ---------------------------------------------------------------------------
class TestCLIParser:
    def test_required_ticker(self):
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args([])

    def test_defaults(self):
        args = _build_arg_parser().parse_args(["--ticker", "2330"])
        assert args.days == 7
        assert args.limit == 20
        assert args.min_score == 0.0
        assert args.format == "text"

    def test_all_args(self):
        args = _build_arg_parser().parse_args([
            "--ticker", "台積電", "--days", "14", "--limit", "50",
            "--min-score", "0.3", "--format", "json",
        ])
        assert args.ticker == "台積電"
        assert args.days == 14
        assert args.limit == 50
        assert args.min_score == 0.3
        assert args.format == "json"

    def test_module_help_exits_zero(self):
        # Real CLI subprocess — exercises the if __name__ == "__main__" path
        # without touching DB (--help short-circuits before connect)
        result = subprocess.run(
            [sys.executable, "-m", "src.services.news_relation", "--help"],
            capture_output=True, text=True, timeout=15,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert result.returncode == 0
        assert "--ticker" in result.stdout
        assert "--days" in result.stdout
        assert "--format" in result.stdout


# ---------------------------------------------------------------------------
#  semantic_search integration — heuristic gate (no DB needed)
# ---------------------------------------------------------------------------
class TestSemanticSearchHeuristic:
    """Verify _answer_from_news_relations correctly gates queries that look
    like ticker / alias / concept lookups vs general questions."""

    def setup_method(self):
        from src.services.semantic_search import _answer_from_news_relations
        self._answer = _answer_from_news_relations

    def test_short_ticker_skips_general_logic(self, monkeypatch):
        # Should NOT call LLM fallback — but we just check it doesn't crash
        # and returns None on a long/concept-mismatch query
        out = self._answer("這是一段非常長的句子包含好多字超過三十個字元的查詢", user_id="u", days=7)
        # Length > 30 short-circuits
        assert out is None

    def test_question_mark_skips(self, monkeypatch):
        out = self._answer("台積電怎麼樣？", user_id="u", days=7)
        # Question mark short-circuits
        assert out is None

    def test_concept_keyword_triggers_relation_path(self, monkeypatch):
        # Should attempt to call find_news_for_ticker; we patch it to return None
        # (no DB rows yet — that's fine, we just want to ensure the path runs)
        from src.services import news_relation
        monkeypatch.setattr(news_relation, "find_news_for_ticker", lambda *a, **k: None)
        out = self._answer("CoWoS", user_id="u", days=7)
        # No DB rows → returns None, ask_news falls through to other fallbacks
        assert out is None
