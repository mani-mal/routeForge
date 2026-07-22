from app.models import CompressionRecord


def test_compression_record_defaults_and_dump():
    r = CompressionRecord(enabled=True, applied=True, bypass_reason=None,
                          tokens_before=1000, tokens_after=300,
                          reduction_pct=70.0, compressor="deterministic",
                          ccr_stored=True, ccr_retrievals=0,
                          ccr_retrievals_to_cloud=0, latency_ms=1.2)
    d = r.model_dump()
    assert d["step"] == "compression"
    assert d["tokens_before"] == 1000 and d["tokens_after"] == 300
    assert d["reduction_pct"] == 70.0
    assert "value" not in d and "original" not in d  # no CCR contents ever
