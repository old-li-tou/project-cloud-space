import json
from pathlib import Path
import subprocess
import sys
import unittest


class IntegrationEntrypointTest(unittest.TestCase):
    def test_connection_failure_is_redacted_at_script_entrypoint(self):
        root = Path(__file__).resolve().parents[1]
        runner = r'''
import runpy
import sys
from unittest.mock import patch
import requests
import cloud_space as c

sys.argv = ['tests/integration_roundtrip.py', '--live']
failure = requests.ConnectionError(
    'Failed request https://example.invalid/file?access_token=SYNTHETIC_PRIVATE_TOKEN')
with patch.object(c, 'register', side_effect=lambda root: {'cloud': str(root)}), \
     patch.object(c, 'Cloud', side_effect=failure):
    runpy.run_path('tests/integration_roundtrip.py', run_name='__main__')
'''
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', runner], cwd=root,
            capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 1)
        combined = result.stdout + result.stderr
        for forbidden in ('SYNTHETIC_PRIVATE_TOKEN', 'https://', 'access_token', 'Traceback'):
            self.assertNotIn(forbidden, combined)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), {
            'pass': False,
            'error': '网络连接失败（未收到可用的百度错误码），原件保留',
        })


if __name__ == '__main__':
    unittest.main()
