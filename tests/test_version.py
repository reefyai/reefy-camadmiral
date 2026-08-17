from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral import version


class VersionTests(unittest.TestCase):
    def test_source_version_uses_reefy_date_format(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolved = version.get_version()
        self.assertRegex(resolved, r"^v\d{4}\.\d{2}\.\d{2}-\d{2}$")

    def test_reefy_injected_version_is_authoritative(self) -> None:
        environment = {
            "REEFY_APP_VERSION": "v2099.02.03-04",
            "CAMADMIRAL_VERSION": "v2099.01.01-00",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(version.get_version(), "v2099.02.03-04")

    def test_standalone_image_version_precedes_version_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            version_file = Path(directory) / "VERSION"
            version_file.write_text("v2099.01.01-00\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"CAMADMIRAL_VERSION": "v2099.01.02-00"}, clear=True),
                patch.object(version, "VERSION_FILE", version_file),
            ):
                self.assertEqual(version.get_version(), "v2099.01.02-00")


if __name__ == "__main__":
    unittest.main()
