import json
import tempfile
import unittest
from pathlib import Path

from camadmiral.rtsp_catalog import CatalogError, catalog_candidates, load_catalog


class RtspCatalogTests(unittest.TestCase):
    def test_unidentified_camera_gets_small_deduplicated_candidate_set(self) -> None:
        candidates = catalog_candidates(
            {
                "ip": "192.0.2.20",
                "display_name": "192.0.2.20",
                "rtsp": [{"port": 554}],
            }
        )

        self.assertEqual(len(candidates), 6)
        self.assertEqual(len({candidate.uri for candidate in candidates}), 6)
        self.assertTrue(all(candidate.uri.startswith("rtsp://192.0.2.20/") for candidate in candidates))
        self.assertTrue(all(candidate.source_url.startswith("https://") for candidate in candidates))

    def test_manufacturer_match_is_prioritized_without_expanding_bound(self) -> None:
        candidates = catalog_candidates(
            {
                "ip": "192.0.2.21",
                "display_name": "Reolink camera",
                "rtsp": [{"port": 8554}],
            }
        )

        self.assertEqual(candidates[0].rule_id, "reolink-preview-v1")
        self.assertEqual(candidates[0].uri, "rtsp://192.0.2.21:8554/Preview_01_main")
        self.assertLessEqual(len(candidates), 8)

    def test_invalid_catalog_provenance_is_rejected(self) -> None:
        payload = {
            "revision": "synthetic-1",
            "probe_limit": 2,
            "rules": [
                {
                    "id": "synthetic",
                    "manufacturer_aliases": ["synthetic"],
                    "source_url": "http://untrusted.invalid/reference",
                    "paths": [{"label": "Main", "path": "/stream"}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(CatalogError):
                load_catalog(path)


if __name__ == "__main__":
    unittest.main()
