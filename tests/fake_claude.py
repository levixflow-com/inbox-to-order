"""A scripted stand-in for AsyncAnthropic used to test the Claude extractor loop without network calls.

It is NOT a model: each response is written by the test. It lets us check what the extractor sends
(tools, prompt), how it gates tool calls, and how it handles bad outputs and errors.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any


def tool_use(name: str, args: dict[str, Any], id_: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use", id=id_ or f"tu_{name}_{abs(hash(str(args))) % 10_000}", name=name, input=args
    )


def response(*blocks: Any, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks), stop_reason=stop_reason, usage=SimpleNamespace(input_tokens=100, output_tokens=20)
    )


class FakeMessages:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        # Snapshot: the extractor appends to the same messages list after we return.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.script:
            raise AssertionError("fake model script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(kwargs)
        return step


class FakeAnthropic:
    def __init__(self, script: list[Any | Callable[[dict[str, Any]], Any]]) -> None:
        self.messages = FakeMessages(script)
        self.beta = SimpleNamespace(messages=self.messages)


def tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    """tool_result blocks the extractor sent back in its latest user message."""
    last = request["messages"][-1]
    return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
