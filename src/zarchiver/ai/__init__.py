"""AI module: LLM-backed summarization, tagging, and categorization."""

from zarchiver.ai.base import LLMError, LLMProvider
from zarchiver.ai.summarizer import Summarizer
from zarchiver.config import AIConfig


def build_provider(config: AIConfig) -> LLMProvider:
    """Construct the configured LLM provider."""
    provider = config.provider.lower()
    if provider == "deepseek":
        from zarchiver.ai.deepseek import DeepSeekProvider

        return DeepSeekProvider(config)
    raise LLMError(f"unknown AI provider: {config.provider!r}")


__all__ = ["LLMProvider", "LLMError", "Summarizer", "build_provider"]
