"""Google Gemini provider — image analysis via google-generativeai SDK."""

import base64
import threading

import google.generativeai as genai

from .base import AIProvider, ProviderResponse

# genai.configure() sets global SDK state — the lock must cover both
# configure() AND the subsequent API call to prevent key bleed between threads.
_api_lock = threading.Lock()


class GeminiProvider(AIProvider):
    """AI provider backed by Google Gemini (gemini-2.0-flash / gemini-1.5-pro)."""

    def __init__(self, api_key: str, model: str, timeout: int = 600):
        """Initialise with credentials and model name."""
        self._api_key = api_key
        self.model_name = model
        self.timeout = timeout

    def verify(self) -> tuple:
        """Check API key and model access via a minimal generate call."""
        with _api_lock:
            try:
                genai.configure(api_key=self._api_key)
                # Validate model access, not just API key — mirrors AnthropicProvider.verify()
                model = genai.GenerativeModel(model_name=self.model_name)
                model.generate_content(
                    'hi',
                    generation_config=genai.GenerationConfig(max_output_tokens=1),
                    request_options={'timeout': self.timeout},
                )
                return True, ''
            except Exception as e:
                return False, str(e)

    def describe(self, content_blocks: list, system_prompt: str,
                 max_tokens: int) -> ProviderResponse:
        """Send multimodal content to Gemini and return a normalised response."""
        parts = _translate_blocks(content_blocks)
        with _api_lock:
            try:
                genai.configure(api_key=self._api_key)
                model = genai.GenerativeModel(
                    model_name=self.model_name,
                    system_instruction=system_prompt,
                )
                response = model.generate_content(
                    parts,
                    generation_config=genai.GenerationConfig(max_output_tokens=max_tokens),
                    request_options={'timeout': self.timeout},
                )
            except Exception as e:
                raise RuntimeError(str(e)) from e

        usage = response.usage_metadata
        return ProviderResponse(
            text=response.text,
            model=self.model_name,
            input_tokens=getattr(usage, 'prompt_token_count', None) or 0,
            output_tokens=getattr(usage, 'candidates_token_count', None) or 0,
        )


def _translate_blocks(blocks: list) -> list:
    """Translate Anthropic-style content blocks to Gemini Part list."""
    parts = []
    for block in blocks:
        if block.get('type') == 'text':
            parts.append(block['text'])
        elif block.get('type') == 'image':
            src = block.get('source', {})
            if src.get('type') == 'base64':
                mime = src.get('media_type', 'image/jpeg')
                data = base64.b64decode(src['data'])
                parts.append({'mime_type': mime, 'data': data})
    return parts
