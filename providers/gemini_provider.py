"""Google Gemini provider — image analysis via google-generativeai SDK."""

import base64

import google.generativeai as genai

from .base import AIProvider, ProviderResponse


class GeminiProvider(AIProvider):
    def __init__(self, api_key: str, model: str, timeout: int = 600):
        genai.configure(api_key=api_key)
        self.model_name = model
        self.timeout = timeout

    def verify(self) -> tuple:
        try:
            list(genai.list_models())
            return True, ''
        except Exception as e:
            return False, str(e)

    def describe(self, content_blocks: list, system_prompt: str,
                 max_tokens: int) -> ProviderResponse:
        model = genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=system_prompt,
        )
        parts = _translate_blocks(content_blocks)
        try:
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
            input_tokens=usage.prompt_token_count,
            output_tokens=usage.candidates_token_count,
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
