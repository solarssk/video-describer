"""AI provider registry. Currently only Anthropic is implemented."""

from .anthropic_provider import AnthropicProvider
from .base import AIProvider, ProviderResponse

REGISTRY = {
    'anthropic': AnthropicProvider,
    # 'openai':   OpenAIProvider,   # future
    # 'gemini':   GeminiProvider,   # future
}


def make_provider(name: str, cfg: dict, api_key: str) -> AIProvider:
    """Instantiates the requested provider with config + api_key.

    Reads provider-specific options from cfg['ai'][<name>].
    Raises ValueError if name is unknown.
    """
    cls = REGISTRY.get(name)
    if not cls:
        available = ', '.join(REGISTRY.keys())
        raise ValueError(f"Unknown AI provider: '{name}'. Available: {available}")

    provider_cfg = cfg['ai'].get(name)
    if not provider_cfg:
        raise ValueError(f"No config section 'ai.{name}' in config.json")

    return cls(
        api_key=api_key,
        model=provider_cfg['model'],
        timeout=provider_cfg.get('timeout_sec', 600),
    )


__all__ = ['REGISTRY', 'make_provider', 'AIProvider', 'ProviderResponse']
