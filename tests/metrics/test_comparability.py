from app.metrics.external import comparability

OK = dict(mapped=True, in_snapshot=True, placement="cloud", provider="azure",
          speed_n=100, ttft_n=100, median_prompt_tokens=1000.0)


def c(**kw):
    return comparability(**{**OK, **kw})


def test_unmapped_not_comparable():
    r = c(mapped=False, in_snapshot=False)
    assert (r["overall"], r["speed"], r["ttft"], r["price"]) == ("not_comparable",) * 4
    assert "not mapped to an Artificial Analysis model" in r["reasons"]


def test_onprem_not_comparable_for_all_three():
    r = c(placement="onprem", provider="onprem")
    assert (r["speed"], r["ttft"], r["price"], r["overall"]) == ("not_comparable",) * 4
    assert "AA measures hosted APIs; this is your own deployment" in r["reasons"]


def test_small_n_insufficient():
    r = c(speed_n=5, ttft_n=100)
    assert r["speed"] == "insufficient_local_data" and r["ttft"] == "comparable"
    assert r["price"] == "comparable" and r["overall"] == "insufficient_local_data"
    r = c(ttft_n=3)
    assert r["ttft"] == "insufficient_local_data" and r["speed"] == "comparable"


def test_prompt_size_partial():
    r = c(median_prompt_tokens=4000.0)
    assert r["speed"] == r["ttft"] == "partially_comparable"
    assert r["price"] == "comparable" and r["overall"] == "partially_comparable"
    assert any("your median is 4000" in x for x in r["reasons"])
    assert c(median_prompt_tokens=499.0)["speed"] == "partially_comparable"
    assert c(median_prompt_tokens=500.0)["speed"] == "comparable"
    assert c(median_prompt_tokens=2000.0)["speed"] == "comparable"


def test_cloud_price_comparable():
    r = c()
    assert r["price"] == "comparable" and r["overall"] == "comparable"


def test_provider_reason_always_present_and_label_unchanged():
    r = c()
    assert "AA measures its own chosen provider; this endpoint is azure" in r["reasons"]
    assert r["overall"] == "comparable"
    assert any("this endpoint is" in x for x in c(mapped=False, in_snapshot=False)["reasons"])
    assert any("this endpoint is" in x for x in c(placement="onprem", provider="onprem")["reasons"])


def test_overall_is_worst():
    # insufficient beats partially
    r = c(speed_n=1, median_prompt_tokens=5000.0)
    assert r["speed"] == "insufficient_local_data" and r["ttft"] == "partially_comparable"
    assert r["overall"] == "insufficient_local_data"


def test_mapped_slug_missing_from_snapshot():
    r = c(in_snapshot=False)
    assert r["overall"] == "not_comparable"
    assert "not in latest snapshot" in r["reasons"]
