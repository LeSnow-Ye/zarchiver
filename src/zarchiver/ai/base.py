"""LLM provider abstraction.

A provider is a thin wrapper over a chat-completion API. The summarizer depends
only on this interface, so swapping DeepSeek for another OpenAI-compatible
backend (or a local model) is a matter of adding a subclass and selecting it via
``ai.provider``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Base class for chat-completion providers."""

    name: str = "base"

    @abstractmethod
    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        """Return the assistant's text reply for the given prompts.

        Implementations must return only the user-facing answer content (not any
        hidden reasoning trace).

        Args:
            json_mode: When True, ask the backend to constrain its output to a
                single JSON object (e.g. OpenAI-compatible
                ``response_format={"type": "json_object"}``). Providers that
                can't enforce this should ignore the flag; the caller still
                parses defensively.

        Raises:
            LLMError: on transport or API errors.
        """


class LLMError(Exception):
    """Raised when an LLM request fails."""
