import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import processor
import web_app


class ApiKeyHandlingTests(unittest.TestCase):
    def setUp(self):
        web_app.is_processing = False

    def test_start_rejects_legacy_inline_api_key(self):
        cfg = {
            'connectors': {'anthropic': {'api_key': ''}},
            'ai': {'provider': 'anthropic', 'anthropic': {}},
        }

        with patch.dict(web_app.os.environ, {}, clear=True), \
                patch('web_app.config_loader.load_config', return_value=cfg):
            response = web_app.app.test_client().post('/start', json={
                'path': '/tmp',
                'analyze_images': True,
                'transcribe': False,
                'api_key': 'sk-ant-legacy-inline',
            })

        self.assertEqual(400, response.status_code)
        self.assertIn('Connectors tab', response.get_json()['error'])

    def test_batch_state_strips_all_api_key_fields(self):
        config = {
            'api_key': 'sk-ant-top-level',
            'ai': {
                'provider': 'anthropic',
                'anthropic': {'api_key': 'sk-ant-ai-section'},
            },
            'connectors': {
                'anthropic': {'api_key': 'sk-ant-connector'},
                'openai': {'api_key': 'sk-openai-connector'},
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / 'batch_state.json'
            with patch.object(processor, 'BATCH_STATE_PATH', state_path):
                processor._save_batch_state(
                    config, next_index=1, total=2,
                    processed=1, skipped=0, errors=0,
                    usage={'cost_usd': 0.01},
                )
            saved_config = json.loads(state_path.read_text(encoding='utf-8'))['config']

        self.assertNotIn('api_key', saved_config)
        self.assertNotIn('api_key', saved_config['ai']['anthropic'])
        self.assertNotIn('api_key', saved_config['connectors']['anthropic'])
        self.assertNotIn('api_key', saved_config['connectors']['openai'])


if __name__ == '__main__':
    unittest.main()
