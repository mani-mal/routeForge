"""A small LangChain chat model adapter that retains RouteForge metadata."""

from __future__ import annotations

import time
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr, SecretStr


class RouteForgeError(RuntimeError):
    """Base error returned by the RouteForge serving API."""


class RouteForgeAuthenticationError(RouteForgeError):
    """The application token is missing, invalid, revoked, or disabled."""


class RouteForgeRateLimitError(RouteForgeError):
    """The application exceeded its rate or daily spend limit."""


class RouteForgePolicyError(RouteForgeError):
    """RouteForge declined to serve the request safely."""


class RouteForgeChatModel(BaseChatModel):
    """LangChain model backed by RouteForge's OpenAI-compatible endpoint.

    A dedicated adapter is used instead of discarding the non-standard response
    fields. Each ``AIMessage.response_metadata["routeforge"]`` value therefore
    contains the request id, selected model, policy, cost, and decision trace.
    """

    base_url: str = "http://localhost:8080/v1"
    api_key: SecretStr = Field(repr=False)
    request_timeout_s: float = 60.0
    max_rate_limit_retries: int = 2
    session_id: str | None = None  # B17: per-workflow-run correlation id sent as X-RouteForge-Session
    _client: httpx.Client = PrivateAttr()
    _audit_records: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._client = httpx.Client(timeout=self.request_timeout_s)

    @property
    def _llm_type(self) -> str:
        return "routeforge"

    @property
    def audit_records(self) -> tuple[dict[str, Any], ...]:
        """A copy-safe view of RouteForge metadata in call order."""
        return tuple(dict(record) for record in self._audit_records)

    def close(self) -> None:
        self._client.close()

    def health(self) -> dict[str, Any]:
        response = self._client.get(f"{self.base_url.removesuffix('/v1')}/health")
        response.raise_for_status()
        return response.json()

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        if stop:
            # The documented RouteForge contract has no stop field. Keeping the
            # adapter strict prevents an accidental dependency on ignored extras.
            raise ValueError("RouteForgeChatModel does not support stop sequences")

        payload = {
            "model": "auto",
            "stream": False,
            "messages": [self._to_wire_message(message) for message in messages],
        }
        # No model-pinning option is exposed: RouteForge policy owns selection.
        payload.update({key: value for key, value in kwargs.items() if key.startswith("mock_")})
        data = self._post_with_rate_limit_retry(payload)

        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        metadata = {
            "request_id": data["id"],
            "model": data["model"],
            "model_display": data.get("model_display", data["model"]),
            "cost": data.get("cost", {}),
            "quality": data.get("quality", {}),
            "policy": data.get("policy", {}),
            "trace": data.get("trace", []),
            "langfuse_url": data.get("langfuse_url"),
        }
        self._audit_records.append(metadata)

        usage = data.get("usage", {})
        usage_metadata = None
        if usage:
            usage_metadata = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
        message = AIMessage(
            content=content,
            id=data["id"],
            response_metadata={"routeforge": metadata},
            usage_metadata=usage_metadata,
        )
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={"routeforge": metadata},
        )

    def _post_with_rate_limit_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(self.max_rate_limit_retries + 1):
            headers = {
                "Authorization": f"Bearer {self.api_key.get_secret_value()}",
                "Content-Type": "application/json",
            }
            if self.session_id:
                headers["X-RouteForge-Session"] = self.session_id
            response = self._client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            if response.status_code != 429 or attempt == self.max_rate_limit_retries:
                break
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
            time.sleep(min(delay, 5.0))

        assert response is not None
        if response.is_success:
            return response.json()

        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        message = f"RouteForge returned {response.status_code}: {detail}"
        if response.status_code == 401:
            raise RouteForgeAuthenticationError(message)
        if response.status_code == 429:
            raise RouteForgeRateLimitError(message)
        if response.status_code == 503:
            raise RouteForgePolicyError(message)
        raise RouteForgeError(message)

    @staticmethod
    def _to_wire_message(message: BaseMessage) -> dict[str, str]:
        role_by_type = {"system": "system", "human": "user", "ai": "assistant"}
        try:
            role = role_by_type[message.type]
        except KeyError as exc:
            raise TypeError(
                f"Unsupported LangChain message type {message.type!r}; "
                "feed tool output back as a HumanMessage so RouteForge gates it"
            ) from exc
        if not isinstance(message.content, str):
            raise TypeError("RouteForge sample supports string message content only")
        return {"role": role, "content": message.content}

