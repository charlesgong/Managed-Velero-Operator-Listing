from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mvo_schedule.core import (
    CommandResult,
    MVOError,
    SHEET_HEADERS,
    ValidationError,
    generate_full_list,
    parse_cluster_source,
    prepare_service_log_targets,
    redact_cluster_identifiers,
    update_sheet_verified,
    validate_collection,
    verify_ocm_production,
)


EXT1 = "11111111-1111-4111-8111-111111111111"
EXT2 = "22222222-2222-4222-8222-222222222222"
INT1 = "a" * 32
INT2 = "b" * 32


class FakeSheet:
    def __init__(self, values, mismatch=False):
        self.values = [list(row) for row in values]
        self.mismatch = mismatch
        self.write_count = 0

    def read(self):
        if self.mismatch and self.write_count == 1:
            broken = [list(row) for row in self.values]
            broken[1][1] = "wrong"
            return broken
        return [list(row) for row in self.values]

    def write(self, values):
        self.write_count += 1
        self.values = [list(row) for row in values]

    def clear(self, start_row, end_row):
        del self.values[start_row - 1 : end_row]


def row(external_id=EXT1, internal_id=INT1, sts=False):
    return {
        "external_id": external_id,
        "internal_id": internal_id,
        "name": "cluster",
        "version": "4.20.1",
        "org": "example",
        "region": "us-east-1",
        "product": "rosa",
        "subscription_id": "sub",
        "sts_enabled": sts,
        "mvo": "Yes",
        "oadp": "No",
        "backups": 0,
        "schedules": 0,
        "status": "ok",
        "error": "",
    }


class PreflightTests(unittest.TestCase):
    def test_production_login_passes(self):
        responses = iter(
            [
                CommandResult("https://api.openshift.com", "", 0),
                CommandResult("test-user", "", 0),
            ]
        )

        def runner(argv, env, timeout):
            return next(responses)

        self.assertEqual(verify_ocm_production(runner), "test-user")

    def test_wrong_environment_stops_before_whoami(self):
        calls = []

        def runner(argv, env, timeout):
            calls.append(list(argv))
            return CommandResult("https://api.stage.openshift.com", "", 0)

        with self.assertRaisesRegex(MVOError, "not configured for production"):
            verify_ocm_production(runner)
        self.assertEqual(calls, [["ocm", "config", "get", "url"]])

    def test_expired_login_is_blocking(self):
        responses = iter(
            [
                CommandResult("https://api.openshift.com", "", 0),
                CommandResult("", "token expired", 1),
            ]
        )

        with self.assertRaisesRegex(MVOError, "token expired"):
            verify_ocm_production(lambda argv, env, timeout: next(responses))


class SourceTests(unittest.TestCase):
    def test_parses_prometheus_json(self):
        payload = {
            "status": "success",
            "data": {"result": [{"metric": {"_id": EXT1}}, {"metric": {"_id": EXT2}}]},
        }
        self.assertEqual(parse_cluster_source(json.dumps(payload)), [EXT1, EXT2])

    def test_rejects_duplicate_ids(self):
        with self.assertRaisesRegex(ValidationError, "Duplicate"):
            parse_cluster_source(f"{EXT1}\n{EXT1}\n")

    def test_generate_full_list_reads_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            output = root / "mvo-full-list.txt"
            source.write_text(f"{EXT1}\n{EXT2}\n", encoding="utf-8")
            self.assertEqual(generate_full_list(output, source_file=source), [EXT1, EXT2])
            self.assertEqual(output.read_text(encoding="utf-8"), f"{EXT1}\n{EXT2}\n")


class CollectionValidationTests(unittest.TestCase):
    def test_partial_collection_is_blocking(self):
        failed = row()
        failed["status"] = "error"
        failed["error"] = "backplane timeout"
        with self.assertRaisesRegex(ValidationError, "incomplete"):
            validate_collection([failed], [EXT1])

    def test_error_summary_redacts_cluster_identifiers(self):
        text = f"external {EXT1}; internal {INT1}"
        redacted = redact_cluster_identifiers(text)
        self.assertNotIn(EXT1, redacted)
        self.assertNotIn(INT1, redacted)
        self.assertIn("11111111…", redacted)


class SheetTests(unittest.TestCase):
    def test_dry_run_does_not_write(self):
        current = [SHEET_HEADERS, [EXT1, "old", "4.19", "org", "region", "Yes", "No", 0, 0]]
        expected = [SHEET_HEADERS, [EXT1, "new", "4.20", "org", "region", "Yes", "No", 0, 0]]
        fake = FakeSheet(current)
        with tempfile.TemporaryDirectory() as directory:
            status = update_sheet_verified(fake, expected, Path(directory) / "snapshot.json", write=False)
        self.assertEqual(status, "dry-run")
        self.assertEqual(fake.write_count, 0)
        self.assertEqual(fake.values, current)

    def test_write_is_read_back(self):
        current = [SHEET_HEADERS, [EXT1, "old", "4.19", "org", "region", "Yes", "No", 0, 0]]
        expected = [SHEET_HEADERS, [EXT1, "new", "4.20", "org", "region", "Yes", "No", 0, 0]]
        fake = FakeSheet(current)
        with tempfile.TemporaryDirectory() as directory:
            status = update_sheet_verified(fake, expected, Path(directory) / "snapshot.json", write=True)
        self.assertEqual(status, "updated")
        self.assertEqual(fake.values, expected)

    def test_mismatch_rolls_back(self):
        current = [SHEET_HEADERS, [EXT1, "old", "4.19", "org", "region", "Yes", "No", 0, 0]]
        expected = [SHEET_HEADERS, [EXT1, "new", "4.20", "org", "region", "Yes", "No", 0, 0]]
        fake = FakeSheet(current, mismatch=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValidationError, "read-back"):
                update_sheet_verified(fake, expected, Path(directory) / "snapshot.json", write=True)
        self.assertEqual(fake.values, current)


class ServiceLogTests(unittest.TestCase):
    def test_first_run_bootstraps_without_mass_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = prepare_service_log_targets(
                [row()], root / "state.json", root / "output", "20260916", commit_state=True
            )
            self.assertTrue(result["bootstrapped"])
            self.assertEqual(result["new_count"], 0)
            self.assertEqual(result["pending_count"], 0)

    def test_new_non_sts_cluster_stays_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            prepare_service_log_targets([row()], state, root / "first", "20260915", commit_state=True)
            rows = [row(), row(EXT2, INT2)]
            result = prepare_service_log_targets(rows, state, root / "second", "20260916", commit_state=True)
            self.assertEqual(result["new_count"], 1)
            self.assertEqual(result["pending_count"], 1)
            internal = json.loads(Path(result["internal_path"]).read_text(encoding="utf-8"))
            self.assertEqual(internal, {"clusters": [INT2]})
            repeated = prepare_service_log_targets(
                rows, state, root / "third", "20260917", commit_state=True
            )
            self.assertEqual(repeated["new_count"], 0)
            self.assertEqual(repeated["pending_count"], 1)

    def test_sts_cluster_is_not_notification_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            prepare_service_log_targets([row()], state, root / "first", "20260915", commit_state=True)
            result = prepare_service_log_targets(
                [row(), row(EXT2, INT2, sts=True)],
                state,
                root / "second",
                "20260916",
                commit_state=True,
            )
            self.assertEqual(result["new_count"], 0)
            self.assertEqual(result["pending_count"], 0)


if __name__ == "__main__":
    unittest.main()
