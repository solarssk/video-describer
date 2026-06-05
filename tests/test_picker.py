import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import web_app
import media_selection


class PickerTests(unittest.TestCase):
    def setUp(self):
        with web_app._last_picker_dir_lock:
            web_app._last_picker_dir = ''
        self.swift_picker = patch('web_app._run_swift_picker', return_value=None)
        self.swift_picker.start()
        self.addCleanup(self.swift_picker.stop)

    def test_pick_path_uses_swift_helper_when_available(self):
        with tempfile.TemporaryDirectory() as picked_dir:
            with patch('web_app._run_swift_picker', return_value={'ok': True, 'path': picked_dir}), \
                    patch('web_app.subprocess.run') as run:
                result = web_app._pick_path('folder')

        self.assertEqual({'ok': True, 'path': picked_dir}, result)
        self.assertEqual(picked_dir, web_app._last_picker_dir)
        run.assert_not_called()

    def test_pick_path_returns_swift_helper_cancel(self):
        with patch('web_app._run_swift_picker', return_value={'ok': False, 'cancelled': True, 'path': ''}), \
                patch('web_app.subprocess.run') as run:
            result = web_app._pick_path('folder')

        self.assertEqual({'ok': False, 'cancelled': True, 'path': ''}, result)
        run.assert_not_called()

    def test_pick_folder_success_updates_last_directory(self):
        with tempfile.TemporaryDirectory() as picked_dir:
            with patch('web_app.subprocess.run') as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=picked_dir + '\n', stderr='')

                result = web_app._pick_path('folder')

        self.assertEqual({'ok': True, 'path': picked_dir}, result)
        self.assertEqual(picked_dir, web_app._last_picker_dir)

    def test_pick_file_success_updates_last_directory_to_parent(self):
        with tempfile.TemporaryDirectory() as picked_dir:
            picked_path = picked_dir + '/movie.mp4'
            with patch('web_app.subprocess.run') as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=picked_path + '\n', stderr='')

                result = web_app._pick_path('file')

        self.assertEqual({'ok': True, 'path': picked_path}, result)
        self.assertEqual(picked_dir, web_app._last_picker_dir)

    def test_pick_path_uses_last_directory_as_default_location(self):
        with tempfile.TemporaryDirectory() as picked_dir:
            with web_app._last_picker_dir_lock:
                web_app._last_picker_dir = picked_dir

            with patch('web_app.subprocess.run') as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=picked_dir + '\n', stderr='')

                web_app._pick_path('folder')

        script = run.call_args.args[0][2]
        self.assertIn('activate', script)
        self.assertIn('default location POSIX file', script)
        self.assertIn(picked_dir, script)

    def test_pick_path_skips_inaccessible_last_directory(self):
        with tempfile.TemporaryDirectory() as picked_dir:
            with web_app._last_picker_dir_lock:
                web_app._last_picker_dir = picked_dir

            with patch('web_app.os.access', return_value=False), \
                    patch('web_app.subprocess.run') as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=picked_dir + '\n', stderr='')

                web_app._pick_path('folder')

        script = run.call_args.args[0][2]
        self.assertNotIn('default location POSIX file', script)

    def test_pick_path_distinguishes_user_cancel(self):
        with patch('web_app.subprocess.run') as run:
            run.return_value = SimpleNamespace(returncode=1, stdout='', stderr='User canceled. (-128)')

            result = web_app._pick_path('folder')

        self.assertEqual({'ok': False, 'cancelled': True, 'path': ''}, result)

    def test_pick_path_reports_timeout(self):
        with patch('web_app.subprocess.run') as run:
            run.side_effect = subprocess.TimeoutExpired(['osascript'], web_app._PICKER_TIMEOUT_SECONDS)

            result = web_app._pick_path('folder')

        self.assertFalse(result['ok'])
        self.assertFalse(result['cancelled'])
        self.assertEqual('timeout', result['code'])
        self.assertEqual('', result['path'])

    def test_pick_folder_endpoint_returns_picker_payload(self):
        with patch('web_app._pick_path') as pick_path:
            pick_path.return_value = {'ok': False, 'cancelled': True, 'path': ''}

            response = web_app.app.test_client().get('/pick-folder')

        self.assertEqual(200, response.status_code)
        self.assertEqual({'ok': False, 'cancelled': True, 'path': ''}, response.get_json())

    def test_pick_folder_endpoint_registers_selection_and_returns_id(self):
        media_selection.clear()
        with tempfile.TemporaryDirectory() as picked_dir:
            with patch('web_app._pick_path', return_value={'ok': True, 'path': picked_dir}):
                response = web_app.app.test_client().get('/pick-folder')

        data = response.get_json()
        self.assertEqual(200, response.status_code)
        self.assertTrue(data.get('ok'))
        self.assertIn('selection_id', data)
        sel_id = data['selection_id']
        from pathlib import Path
        self.assertEqual(media_selection.lookup(sel_id), [Path(picked_dir)])

    def test_pick_file_endpoint_multi_paths_registers_all(self):
        media_selection.clear()
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = tmpdir + '/clip1.mp4'
            f2 = tmpdir + '/clip2.insv'
            with patch('web_app._pick_path', return_value={
                'ok': True, 'path': f1, 'paths': [f1, f2]
            }):
                response = web_app.app.test_client().get('/pick-file')

        data = response.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('selection_id', data)
        from pathlib import Path
        resolved = media_selection.lookup(data['selection_id'])
        self.assertEqual(resolved, [Path(f1), Path(f2)])


if __name__ == '__main__':
    unittest.main()
