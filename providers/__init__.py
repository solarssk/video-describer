"""AI provider registry.

Provider classes for openai and gemini are imported lazily so missing
optional SDKs (openai, google-generativeai) don't break startup.
"""

from typing import Any

from .anthropic_provider import AnthropicProvider
from .base import AIProvider, ProviderResponse

REGISTRY = {
    'anthropic': AnthropicProvider,
    'openai':    'openai_provider.OpenAIProvider',   # lazy string sentinel
    'gemini':    'gemini_provider.GeminiProvider',   # lazy string sentinel
}


def make_provider(name: str, cfg: dict, api_key: str) -> AIProvider:
    """Instantiates the requested provider with config + api_key.

    Reads provider-specific options from cfg['ai'][<name>].
    Raises ValueError if name is unknown.
    Raises RuntimeError if the provider SDK is not installed.
    """
    if name not in REGISTRY:
        available = ', '.join(REGISTRY.keys())
        raise ValueError(f"Unknown AI provider: '{name}'. Available: {available}")

    provider_cls: Any
    entry = REGISTRY[name]
    if isinstance(entry, str):
        # Lazy import for optional-dependency providers
        module_name, class_name = entry.split('.')
        try:
            import importlib
            mod = importlib.import_module(f'.{module_name}', package=__package__)
            provider_cls = getattr(mod, class_name)
        except ImportError as e:
            sdk = {'openai_provider': 'openai', 'gemini_provider': 'google-generativeai'}.get(module_name, module_name)
            raise RuntimeError(f"Install '{sdk}' to use the {name} provider: pip install {sdk}") from e
    else:
        provider_cls = entry

    provider_cfg = cfg['ai'].get(name)
    if not provider_cfg:
        raise ValueError(f"No config section 'ai.{name}' in config.json")

    return provider_cls(
        api_key=api_key,
        model=provider_cfg['model'],
        timeout=provider_cfg.get('timeout_sec', 600),
    )


__all__ = ['REGISTRY', 'make_provider', 'AIProvider', 'ProviderResponse']
