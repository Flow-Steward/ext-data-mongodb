"""Validate the release binary provenance and native safety patch."""

import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeBuildTests(unittest.TestCase):
    def test_pinned_source_and_patch(self):
        provenance = json.loads((ROOT / "toolbox-provenance.json").read_text())
        self.assertEqual(provenance["tag"], "v1.3.0")
        self.assertEqual(provenance["commit"], "b0010060a939455d91e4f8ba62dc3e72b1892469")
        patch = ROOT / provenance["patch"]
        self.assertEqual(hashlib.sha256(patch.read_bytes()).hexdigest(), provenance["patch_sha256"])
        self.assertIn("@sha256:", provenance["go_image"])
        self.assertEqual(len(provenance["packages"]), 10)
        for package in provenance["packages"]:
            self.assertRegex(package, r"/internal/(sources/mongodb|tools/mongodb/)")
        self.assertEqual(set(provenance["binaries"]), {"linux-amd64", "linux-arm64"})
        for target, details in provenance["binaries"].items():
            binary = (ROOT / "bin" / target / "toolbox").read_bytes()
            self.assertEqual(binary[:4], b"\x7fELF")
            self.assertEqual(len(binary), details["size"])
            self.assertEqual(hashlib.sha256(binary).hexdigest(), details["sha256"])

    def test_minimal_import_build(self):
        script = (ROOT / "scripts" / "build_toolbox.sh").read_text()
        self.assertIn("= 10", script)
        self.assertIn("sources\\/mongodb", script)
        self.assertNotIn("sources/redis", script)
        self.assertIn("CGO_ENABLED=0", script)
        self.assertIn(
            "go test -p 1 ./internal/sources/mongodb ./internal/tools/mongodb/...", script
        )


if __name__ == "__main__":
    unittest.main()
