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
    def complete(self, system: str, user: str) -> str:
        """Return the assistant's text reply for the given prompts.

        Implementations must return only the user-facing answer content (not any
        hidden reasoning trace).

        Raises:
            LLMError: on transport or API errors.
        """


class LLMError(Exception):
    """Raised when an LLM request fails."""
