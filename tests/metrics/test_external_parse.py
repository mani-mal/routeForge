import math

import pytest

from app.metrics.external import AAPayloadError, parse_aa_payload

FULL = {
    "slug": "gpt-x", "name": "GPT X", "model_creator": {"name": "Acme"},
    "evaluations": {"artificial_analysis_intelligence_index": 55.5,
                    "artificial_analysis_coding_index": 40,
                    "artificial_analysis_math_index": 70.1},
    "pricing": {"price_1m_input_tokens": 1.5, "price_1m_output_tokens": 6.0,
                "price_1m_blended_3_to_1": 2.6},
    "median_output_tokens_per_second": 90.2,
    "median_time_to_first_token_seconds": 0.45,
    "median_time_to_first_answer_token": 1.2,
}


def test_parses_list_and_data_wrapper():
    a = parse_aa_payload([FULL])
    b = parse_aa_payload({"data": [FULL]})
    assert a == b and len(a) == 1
    m = a[0]
    assert (m.slug, m.name, m.creator) == ("gpt-x", "GPT X", "Acme")
    assert (m.intelligence_index, m.coding_index, m.math_index) == (55.5, 40.0, 70.1)
    assert (m.price_in, m.price_out, m.price_blended) == (1.5, 6.0, 2.6)
    assert (m.output_tps, m.ttft_s, m.ttfat_s) == (90.2, 0.45, 1.2)


def test_missing_sections_are_null():
    m = parse_aa_payload([{"slug": "s", "name": "S", "pricing": None, "evaluations": None}])[0]
    for f in ("intelligence_index", "coding_index", "math_index", "price_in", "price_out",
              "price_blended", "output_tps", "ttft_s", "ttfat_s", "creator"):
        assert getattr(m, f) is None
    m2 = parse_aa_payload([{"slug": "s"}])[0]
    assert m2.price_in is None and m2.name == "s"


def test_non_numeric_is_null():
    m = parse_aa_payload([{"slug": "s", "pricing": {"price_1m_input_tokens": "1.5"},
                           "median_output_tokens_per_second": True}])[0]
    assert m.price_in is None and m.output_tps is None


def test_nan_is_null():
    m = parse_aa_payload([{"slug": "s", "median_output_tokens_per_second": math.nan,
                           "median_time_to_first_token_seconds": math.inf}])[0]
    assert m.output_tps is None and m.ttft_s is None


@pytest.mark.parametrize("bad", [{}, [], "x", [1], [{"name": "no slug"}],
                                 [FULL, {"name": "no slug"}], {"data": {}}])
def test_refuses_empty_or_wrong_shape(bad):
    with pytest.raises(AAPayloadError):
        parse_aa_payload(bad)


def test_slug_falls_back_to_id():
    assert parse_aa_payload([{"id": "abc", "name": "A"}])[0].slug == "abc"


def test_duplicate_slug_keeps_first():
    out = parse_aa_payload([{"slug": "d", "name": "first"}, {"slug": "d", "name": "second"}])
    assert [m.name for m in out] == ["first"]


def test_slug_over_128_refuses_whole_payload():
    with pytest.raises(AAPayloadError, match="slug"):
        parse_aa_payload([FULL, {**FULL, "slug": "x" * 129}])
    assert parse_aa_payload([{**FULL, "slug": "x" * 128}])[0].slug == "x" * 128


def test_name_and_creator_truncated_to_column_lengths():
    m = parse_aa_payload([{**FULL, "name": "n" * 500, "model_creator": {"name": "c" * 300}}])[0]
    assert len(m.name) == 200 and len(m.creator) == 100


def test_price_drift_flags_free_at_aa():
    from app.config.schema import PriceCfg
    from app.metrics.compute import _price_drift
    aa = parse_aa_payload([{**FULL, "pricing": {"price_1m_input_tokens": 0,
                                                 "price_1m_output_tokens": None}}])[0]
    out = _price_drift("m", aa, PriceCfg(input=1.0, output=2.0, tier="mid", provider="azure"))
    assert out and "free at AA" in out and "output" not in out
