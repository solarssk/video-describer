"""Anthropic Claude provider — uses the official `anthropic` SDK."""

import anthropic

from .base import AIProvider, ProviderResponse


def _clean_error_msg(e: Exception) -> str:
    """Return a human-readable message from an Anthropic SDK exception.

    The default str() looks like:
        Error code: 400 - {'type': 'error', 'error': {'type': '...', 'message': 'Your credit
        balance is too low...'}, 'request_id': 'req_...'}

    We extract just the inner 'message' field so callers see plain prose.
    Falls back to str(e) if the structure is unexpected.
    """
    if isinstance(e, anthropic.APIStatusError):
        body = getattr(e, 'body', None)
        if isinstance(body, dict):
            msg = body.get('error', {}).get('message', '')
            if msg:
                return msg
    return str(e)


class AnthropicProvider(AIProvider):
    def __init__(self, api_key: str, model: str, timeout: int = 600):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.timeout = timeout

    def verify(self) -> tuple:
        try:
            self.client.messages.create(
                model=self.model,
                max_tokens=1,
                messages=[{'role': 'user', 'content': 'hi'}],
            )
            return True, ''
        except Exception as e:
            return False, _clean_error_msg(e)

    def describe(self, content_blocks: list, system_prompt: str,
                 max_tokens: int) -> ProviderResponse:
        # content_blocks is already in Anthropic format — pass through.
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{'role': 'user', 'content': content_blocks}],
                timeout=self.timeout,
            )
        except anthropic.APIStatusError as e:
            # Re-raise with a clean message so callers don't see raw JSON dicts.
            raise RuntimeError(_clean_error_msg(e)) from e
        return ProviderResponse(
            text=response.content[0].text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
