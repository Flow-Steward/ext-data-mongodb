"""Preserve the unmodified Apache license shipped by pinned Toolbox v1.16.0."""

import hashlib
from pathlib import Path


def test_upstream_license_is_preserved_verbatim():
    license_bytes = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == (
        "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd"
    )
