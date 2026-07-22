def test_backend_stubs_import():
    # VllmBackend was implemented for real in Phase 9 Task 2 (see
    # tests/test_vllm_backend.py). Dynamo remains a stub until Task 3.
    from app.gateway.backends.dynamo import DynamoBackend
    assert DynamoBackend().name == "dynamo"
