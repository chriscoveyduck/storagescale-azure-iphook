import copy
import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import patch_hook


ROOT = Path(__file__).resolve().parents[1]


class PatcherTests(unittest.TestCase):
    def setUp(self):
        self.config = patch_hook.load_config(ROOT / "config.example.json")

    def load(self, config):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            return patch_hook.load_config(path)

    def test_example_renders_all_tokens(self):
        rendered = patch_hook.render_patch(self.config)
        self.assertNotRegex(rendered, r"@[A-Z0-9_]+@")
        self.assertIn("10.0.2.20|10.0.2.21", rendered)
        self.assertIn('peer=example-ces02; peerNic="example-ces02-protocol"', rendered)
        self.assertIn('peer=example-ces01; peerNic="example-ces01-protocol"', rendered)

    def test_custom_topology_does_not_assume_nic_suffix(self):
        self.config["nodes"][1]["nic"] = "independent-nic-name"
        self.config["addresses"] = ["192.168.5.20", "192.168.5.21", "192.168.5.22"]
        rendered = patch_hook.render_patch(self.load(self.config))
        self.assertIn('peerNic="independent-nic-name"', rendered)
        self.assertIn("192.168.5.20|192.168.5.21|192.168.5.22", rendered)
        self.assertNotIn('peerNic="${peer}-protocol"', rendered)

    def test_rejects_unsupported_original(self):
        for content in (b"", b"unrelated script\n", b"already patched\n"):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "SHA256"):
                patch_hook.patch_hook(content, self.config)

    def test_configuration_rejects_shell_code_and_patterns(self):
        for value in ("$(id)", "bad'name", "bad name", "node*", "node|other", "node\nexit", ""):
            for key in ("vm", "nic", "ces_node"):
                with self.subTest(value=value, key=key):
                    config = copy.deepcopy(self.config)
                    config["nodes"][0][key] = value
                    with self.assertRaises(ValueError):
                        self.load(config)
            for key in ("resource_group", "ip_config_prefix"):
                config = copy.deepcopy(self.config)
                config[key] = value
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_rejects_invalid_topologies(self):
        configs = []
        for nodes in ([], self.config["nodes"][:1], self.config["nodes"] * 2, None):
            configs.append(dict(self.config, nodes=nodes))
        for addresses in ([], ["10.0.2.20"] * 2, ["invalid"], ["::1"], [True], "10.0.2.20"):
            configs.append(dict(self.config, addresses=addresses))
        for subscription in ("invalid", None, 123):
            configs.append(dict(self.config, subscription=subscription))
        for key in ("vm", "nic", "ces_node"):
            config = copy.deepcopy(self.config)
            config["nodes"][1][key] = config["nodes"][0][key].upper()
            configs.append(config)
        config = copy.deepcopy(self.config)
        config["nodes"][0]["extra"] = "unexpected"
        configs.extend([config, dict(self.config, extra="unexpected"), [], None])
        for config in configs:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.load(config)

    def test_single_and_multiple_patch_hunks(self):
        source = "one\ntwo\nthree\nfour\n"
        patch = "--- old\n+++ new\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n@@ -4 +4 @@\n-four\n+FOUR\n"
        self.assertEqual(patch_hook.apply_unified_patch(source, patch), "one\nTWO\nthree\nFOUR\n")

    def test_bad_patch_does_not_apply(self):
        patches = [
            "",
            "--- old\n+++ new\ninvalid\n",
            "--- old\n+++ new\n@@ -1 +1 @@\n-wrong\n+new\n",
            "--- old\n+++ new\n@@ -1,2 +1 @@\n-one\n+new\n",
            "--- old\n+++ new\n@@ -1 +2 @@\n-one\n+new\n",
            "--- old\n+++ new\n@@ -3 +3 @@\n-one\n+new\n",
            "--- old\n+++ new\n@@ -1 +1 @@\n?invalid\n",
            "--- old\n+++ new\n@@ -1 +1 @@\n-one\n+new\n@@ -1 +1 @@\n-one\n+again\n",
        ]
        for patch in patches:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                patch_hook.apply_unified_patch("one\n", patch)

    def test_bundled_template_hunk_lengths_and_offsets(self):
        patch = patch_hook.render_patch(self.config)
        source = []
        for line in patch.splitlines(keepends=True)[2:]:
            if line.startswith("@@ "):
                start = int(re.match(r"@@ -(\d+)", line)[1]) - 1
                self.assertGreaterEqual(start, len(source))
                source.extend(["# synthetic unchanged line\n"] * (start - len(source)))
            elif line.startswith((" ", "-")):
                source.append(line[1:])
        candidate = patch_hook.apply_unified_patch("".join(source), patch)
        for name in ("enableIP", "cleanupIP", "reconcileAzureCesIP"):
            self.assertEqual(len(re.findall(rf"^function {name}(?:\n| \{{)", candidate, re.M)), 1)

    def test_output_creation_is_exclusive_and_never_overwrites_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, output = Path(tmp) / "original", Path(tmp) / "output"
            original.write_bytes(b"untouched original")
            args = ["patch_hook.py", str(original), str(output),
                    "--config", str(ROOT / "config.example.json")]
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

    def test_cli_failure_leaves_output_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, output = Path(tmp) / "original", Path(tmp) / "output"
            original.write_bytes(b"unsupported version")
            result = subprocess.run(
                [sys.executable, str(ROOT / "patch_hook.py"), str(original), str(output),
                 "--config", str(ROOT / "config.example.json")],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("SHA256 mismatch", result.stderr)
            self.assertFalse(output.exists())
            self.assertEqual(original.read_bytes(), b"unsupported version")


if __name__ == "__main__":
    unittest.main()
