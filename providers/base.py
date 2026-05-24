"""
AI provider abstraction — describes the contract every provider must fulfill.

To add a new provider (e.g. OpenAI, Gemini):
1. Create providers/<name>_provider.py implementing the AIProvider Protocol.
2. Register it in providers/__init__.py REGISTRY dict.
3. Add a section to config.default.json under 'ai.<name>' with provider-specific fields.
4. Set 'ai.provider' to <name> to switch.
"""

from dataclasses import dataclass


@dataclass
class ProviderResponse:
    """Normalized response across all providers."""
    text: str
    input_tokens: int
    output_tokens: int
    model: str


class AIProvider:
    """Base class — all providers must implement verify() and describe()."""

    def verify(self) -> tuple:
        """Cheap call to confirm credentials + access work.
        Returns (ok: bool, error_msg: str). error_msg empty on success."""
        raise NotImplementedError

    def describe(self, content_blocks: list, system_prompt: str,
                 max_tokens: int) -> ProviderResponse:
        """Send multimodal content (frames + text) and return a description.

        content_blocks is a provider-agnostic list of dicts in Anthropic-style format:
        - {'type': 'text', 'text': '...'}
        - {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': '...'}}

        Future providers should translate this format to their own SDK's expected shape.
        """
        raise NotImplementedError
