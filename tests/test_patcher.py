import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import patch_hook


ROOT = Path(__file__).resolve().parents[1]


class PatcherTests(unittest.TestCase):
    def test_rejects_unsupported_original(self):
        for content in (b"", b"unrelated script\n", b"already patched\n"):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "SHA256"):
                patch_hook.patch_hook(content)

    def test_functions_are_environment_independent(self):
        functions = patch_hook.FUNCTIONS.read_text(encoding="utf-8")
        for name in ("cleanupIP", "enableIP", "deleteIP"):
            self.assertEqual(functions.count(f"function {name}\n"), 1)
        self.assertNotIn("@SUBSCRIPTION@", functions)
        self.assertNotIn("example-", functions)
        self.assertIn("/metadata/instance?", functions)
        self.assertIn("mmces node list -Y", functions)

    def test_preserves_vendor_header_and_dispatcher(self):
        original = b'header\nfunction cleanupIP {\nold functions\nprint -- "callout=original"\ndispatch\n'
        with mock.patch.object(patch_hook.hashlib, "sha256") as digest:
            digest.return_value.hexdigest.return_value = patch_hook.VENDOR_SHA256
            result = patch_hook.patch_hook(original).decode()
        self.assertTrue(result.startswith("header\n"))
        self.assertTrue(result.endswith('print -- "callout=original"\ndispatch\n'))
        self.assertNotIn("old functions", result)

    def test_output_creation_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, output = Path(tmp) / "original", Path(tmp) / "output"
            original.write_bytes(b"untouched original")
            args = ["patch_hook.py", str(original), str(output)]
            with mock.patch.object(sys, "argv", args), \
                    mock.patch.object(patch_hook, "patch_hook", return_value=b"candidate\n"), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(patch_hook.main(), 0)
                self.assertEqual(patch_hook.main(), 1)
                args[2] = str(original)
                self.assertEqual(patch_hook.main(), 1)
            self.assertEqual(original.read_bytes(), b"untouched original")
            self.assertEqual(output.read_bytes(), b"candidate\n")
            if os.name != "nt":
                self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_cli_failure_leaves_output_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, output = Path(tmp) / "original", Path(tmp) / "output"
            original.write_bytes(b"unsupported version")
            result = subprocess.run(
                [sys.executable, str(ROOT / "patch_hook.py"), str(original), str(output)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("SHA256 mismatch", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
