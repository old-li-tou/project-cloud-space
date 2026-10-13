from pathlib import Path
import subprocess
import sys
import unittest


class AuthDialogTest(unittest.TestCase):
    def test_submit_and_shutdown_without_native_crash(self):
        result = subprocess.run(
            [sys.executable, '-X', 'faulthandler', str(Path(__file__).with_name('gui_auth_smoke.py'))],
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b'PASS:', result.stdout)


if __name__ == '__main__':
    unittest.main()
