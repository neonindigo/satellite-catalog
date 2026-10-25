import copy
from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from publish import UTC, blob, build, date, discover, refresh, timestamp, validate_rows

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
ROW = {"NORAD_CAT_ID": 25544, "OBJECT_NAME": "ISS (ZARYA)", "EPOCH": "2026-10-24T23:00:00",
       "MEAN_MOTION": 15.49, "ECCENTRICITY": 0.0006, "INCLINATION": 51.6,
       "RA_OF_ASC_NODE": 46.5, "ARG_OF_PERICENTER": 282.0, "MEAN_ANOMALY": 77.9,
       "BSTAR": 0.00013, "MEAN_MOTION_DOT": 0.00007, "MEAN_MOTION_DDOT": 0.0}
NAMES = ["stations", "active", "weather", "starlink", "cosmos-2251-debris"]
HTML = "".join(f'<a href="gp.php?GROUP={name}&amp;FORMAT=json">{name}</a>' for name in NAMES)


def state(age=7):
    return {"discoveredAt": timestamp(NOW - timedelta(hours=age)), "groups": [
        {"id": name, "name": name, "query": {"GROUP": name},
         "fetchedAt": timestamp(NOW - timedelta(hours=age)), "objects": [copy.deepcopy(ROW)]}
        for name in NAMES]}


class PublisherTests(unittest.TestCase):
    def test_discovery_deduplicates_and_rejects_unknown_page(self):
        self.assertEqual(len(discover(HTML + HTML)), 5)
        self.assertEqual(discover(HTML)[0]["id"], "stations")
        with self.assertRaises(ValueError):
            discover("<html>Temporarily unavailable</html>")

    def test_wide_catalog_numbers_and_malformed_rows(self):
        wide = ROW | {"NORAD_CAT_ID": 123456789}
        self.assertEqual(validate_rows(json.dumps([wide]))[0]["NORAD_CAT_ID"], 123456789)
        for update in ({"NORAD_CAT_ID": True}, {"NORAD_CAT_ID": 0}, {"ECCENTRICITY": 1}, {"MEAN_MOTION": float("nan")}, {"EPOCH": 123}):
            with self.assertRaises(ValueError):
                validate_rows(json.dumps([ROW | update]))

    def test_no_fetch_before_interval(self):
        def fetch(url):
            self.fail("Source must not be queried before refresh is due")
        refresh(state(age=1), NOW, fetch, lambda _: None)

    def test_http_failure_halts_and_preserves_all_entries(self):
        saved = state()
        calls = []
        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                return HTML
            raise HTTPError(url, 503, "Unavailable", {}, BytesIO())
        refresh(saved, NOW, fetch, lambda _: None)
        self.assertEqual(len(calls), 2)
        self.assertTrue(saved["halted"])
        self.assertTrue(all(group["objects"] == [ROW] for group in saved["groups"]))
        refresh(saved, NOW + timedelta(hours=7), fetch, lambda _: None)
        self.assertEqual(len(calls), 2, "Later scheduled runs must honor the halt")

    def test_network_failure_retains_only_failed_group_and_recovers_others(self):
        saved = state()
        def fetch(url):
            if "gp.php" not in url:
                return HTML
            if "stations" in url:
                raise URLError("offline")
            return json.dumps([ROW | {"EPOCH": "2026-10-25T11:00:00"}])
        refresh(saved, NOW, fetch, lambda _: None)
        self.assertEqual(saved["groups"][0]["objects"], [ROW])
        self.assertIn("error", saved["groups"][0])
        self.assertTrue(all(group["fetchedAt"] == timestamp(NOW) for group in saved["groups"][1:]))

    def test_http_halt_does_not_mark_previously_fresh_groups_stale(self):
        saved = state()
        saved["groups"][1]["fetchedAt"] = timestamp(NOW - timedelta(hours=1))
        def fetch(url):
            if "gp.php" not in url:
                return HTML
            raise HTTPError(url, 403, "Forbidden", {}, BytesIO())
        refresh(saved, NOW, fetch, lambda _: None)
        active = next(group for group in saved["groups"] if group["id"] == "active")
        self.assertNotIn("error", active)

    def test_failed_discovery_does_not_remove_known_groups(self):
        saved = state()
        refresh(saved, NOW, lambda _: "<html>Changed layout</html>", lambda _: None)
        self.assertEqual(len(saved["groups"]), 5)
        self.assertIn("discoveryError", saved)

    def test_explicit_resume_can_clear_halt(self):
        saved = state(age=1)
        saved["halted"] = {"reason": "HTTP 403"}
        refresh(saved, NOW, lambda _: self.fail("Not due"), lambda _: None, resume=True)
        self.assertNotIn("halted", saved)

    def test_publication_deduplicates_and_uses_newest_epoch(self):
        saved = state(age=1)
        saved["groups"][1]["objects"][0]["EPOCH"] = "2026-10-25T10:00:00"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = build(saved, root, NOW)
            index = json.loads((root / manifest["indexPath"]).read_text())
            self.assertEqual(len(index), 1)
            self.assertEqual(len(index[0]["groups"]), 5)
            iss = json.loads((root / "v1/iss.json").read_text())
            self.assertEqual(iss["epoch"], "2026-10-25T10:00:00")
            self.assertNotEqual(iss["epoch"], manifest["publishedAt"])
            self.assertTrue(manifest["complete"])

    def test_stale_group_survives_and_never_becomes_complete(self):
        saved = state()
        saved["groups"][0]["error"] = "offline"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = build(saved, root, NOW)
            self.assertFalse(manifest["complete"])
            self.assertTrue(manifest["groups"][0]["stale"])
            self.assertTrue((root / manifest["groups"][0]["path"]).exists())

    def test_empty_first_run_cannot_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                build({}, Path(folder), NOW)

    def test_blob_filename_matches_bytes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = blob(root, [ROW])
            self.assertEqual(Path(name).stem, hashlib.sha256((root / name).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
