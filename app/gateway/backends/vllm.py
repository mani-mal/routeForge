"""vLLM backend — STUB (design §10.6). Phase 9."""


class VLLMBackend:
    name = "vllm"

    async def generate(self, *a, **k):
        raise NotImplementedError("vLLM backend lands in Phase 9")
