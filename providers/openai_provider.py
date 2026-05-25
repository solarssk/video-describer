"""OpenAI GPT-4o provider — image analysis via chat completions API."""

import openai

from .base import AIProvider, ProviderResponse


class OpenAIProvider(AIProvider):
    """AI provider backed by OpenAI chat completions (GPT-4o / GPT-4o-mini)."""

    def __init__(self, api_key: str, model: str, timeout: int = 600):
        """Initialise with credentials and model name."""
        self.client = openai.OpenAI(api_key=api_key, timeout=timeout)
        self.model = model

    def verify(self) -> tuple:
        """Check API key and model access via a minimal chat completion call."""
        try:
            # Validate model access, not just API key — mirrors AnthropicProvider.verify()
            self.client.chat.completions.create(
                model=self.model,
                max_tokens=1,
                messages=[{'role': 'user', 'content': 'hi'}],
            )
            return True, ''
        except Exception as e:
            return False, str(e)

    def describe(self, content_blocks: list, system_prompt: str,
                 max_tokens: int) -> ProviderResponse:
        """Send multimodal content to OpenAI and return a normalised response."""
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': _translate_blocks(content_blocks)},
        ]
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=messages,
            )
        except openai.APIStatusError as e:
            raise RuntimeError(str(e)) from e

        choice = response.choices[0]
        usage = response.usage
        return ProviderResponse(
            text=choice.message.content,
            model=response.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )


def _translate_blocks(blocks: list) -> list:
    """Translate Anthropic-style content blocks to OpenAI chat format."""
    out = []
    for block in blocks:
        if block.get('type') == 'text':
            out.append({'type': 'text', 'text': block['text']})
        elif block.get('type') == 'image':
            src = block.get('source', {})
            if src.get('type') == 'base64':
                mime = src.get('media_type', 'image/jpeg')
                data = src.get('data', '')
                out.append({
                    'type': 'image_url',
                    'image_url': {'url': f'data:{mime};base64,{data}'},
                })
    return out
