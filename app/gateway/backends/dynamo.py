"""Dynamo + TensorRT-LLM backend — STUB (design §10.7). Phase 9."""


class DynamoBackend:
    name = "dynamo"

    async def generate(self, *a, **k):
        raise NotImplementedError("Dynamo backend lands in Phase 9")
