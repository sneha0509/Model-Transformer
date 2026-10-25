import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fabric.backend import impact as impact_reports
from fabric.backend.copilot_impact import RESPONSE_SCHEMA, _read_cache, _write_cache
from fabric.backend.impact import replacement_sources, save_report, validate_result
from fabric.tests.test_impact_objects import model_fixture, targeted_fixture


class ImpactResultTests(unittest.TestCase):
    def setUp(self):
        _, self.changes, self.scoped = targeted_fixture(model_fixture(), [("Orders", "Amount")])
        self.artifact = self.scoped["artifacts"][0]
        self.artifact["targets"] = [
            target for target in self.artifact["targets"]
            if target["objectName"] in {"Orders.Revenue", "Orders.Revenue Twice"}
        ]
        self.sources = {
            source["objectName"]: source for source in replacement_sources(self.artifact)
        }

    def finding(self, name):
        source = self.sources[name]
        return {
            "artifactKey": self.artifact["key"], "changeIds": ["change-1"],
            "objectType": "measure", "objectName": name,
            "severity": "high", "confidence": "high",
            "impact": [
                "The measure fails column or prerequisite resolution when Orders[Amount] is removed."
            ],
            "recommendation": ["Specify replacement revenue logic or approve retirement of this measure."],
            "evidence": [{
                "file": source["file"], "line": source["startLine"], "excerpt": source["source"],
            }],
            "proposedFixes": [],
            "fixUnavailableReason": (
                "No replacement revenue column or measure-retirement rule was supplied."
            ),
            "validationSteps": [],
        }

    def result(self):
        return {
            "summary": "Removing Orders[Amount] breaks the direct and transitive revenue measures.",
            "impacts": [
                self.finding("Orders.Revenue"), self.finding("Orders.Revenue Twice"),
            ],
            "unaffectedTargets": [], "gaps": [],
        }

    def test_removed_column_retains_direct_and_transitive_measure_impacts_without_fixes(self):
        result = validate_result(self.result(), self.changes, [self.artifact])
        self.assertEqual(len(result["impacts"]), 2)
        self.assertEqual(result["unaffectedTargets"], [])
        self.assertEqual(result["gaps"], [])
        self.assertTrue(all(not finding["proposedFixes"] for finding in result["impacts"]))
        self.assertTrue(all(finding["fixUnavailableReason"] for finding in result["impacts"]))

    def test_missing_fix_requires_a_specific_reason(self):
        for reason in (None, "", " "):
            with self.subTest(reason=reason):
                result = self.result()
                result["impacts"][0]["fixUnavailableReason"] = reason
                with self.assertRaisesRegex(ValueError, "reason"):
                    validate_result(result, self.changes, [self.artifact])

    def test_unfixed_impact_requires_evidence_from_its_target(self):
        for override in (
            {"file": "missing.bim"},
            {"line": 99999},
            {"excerpt": "SUM(Orders[Nonexistent])"},
            {"excerpt": self.sources["Orders.Revenue Twice"]["source"]},
        ):
            with self.subTest(override=override):
                result = self.result()
                result["impacts"][0]["evidence"][0].update(override)
                with self.assertRaisesRegex(ValueError, "source evidence"):
                    validate_result(result, self.changes, [self.artifact])

    def test_existing_generated_replacements_still_validate(self):
        result = self.result()
        for finding in result["impacts"]:
            source = self.sources[finding["objectName"]]
            finding.pop("fixUnavailableReason")
            definition = json.loads(source["source"])
            definition["expression"] += " + 0"
            finding["proposedFixes"] = [{
                "sourceId": source["sourceId"], "after": json.dumps(definition),
                "rationale": "Update the requested calculation.",
            }]
        validated = validate_result(result, self.changes, [self.artifact])
        self.assertTrue(all(item["proposedFixes"][0]["diff"] for item in validated["impacts"]))
        self.assertTrue(all(item["fixUnavailableReason"] is None for item in validated["impacts"]))

    def test_findings_cannot_be_both_fixable_and_blocked(self):
        result = self.result()
        source = self.sources["Orders.Revenue"]
        result["impacts"][0]["proposedFixes"] = [{
            "sourceId": source["sourceId"],
            "after": source["source"].replace("SUM", "MAX"),
            "rationale": "Change aggregation.",
        }]
        with self.assertRaisesRegex(ValueError, "reason"):
            validate_result(result, self.changes, [self.artifact])

    def test_unfixed_impact_cannot_also_be_unaffected(self):
        result = self.result()
        finding = result["impacts"][0]
        result["unaffectedTargets"] = [{
            key: copy.deepcopy(finding[key])
            for key in ("artifactKey", "changeIds", "objectType", "objectName", "evidence")
        }]
        result["unaffectedTargets"][0]["reason"] = "No change."
        with self.assertRaisesRegex(ValueError, "both impacted and unaffected"):
            validate_result(result, self.changes, [self.artifact])

    def test_unfixed_impacts_survive_cache_and_report_persistence(self):
        result = validate_result(self.result(), self.changes, [self.artifact])
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"
            _write_cache(cache, result)
            cached = _read_cache(cache, self.changes, self.artifact)
            self.assertIsNotNone(cached)
            cached["analyzedArtifacts"] = [self.artifact["key"]]
            catalog = {
                **self.scoped, "revision": "test", "instructions": "Remove Orders[Amount].",
                "targets": self.artifact["targets"], "issues": [],
            }
            with patch.object(impact_reports, "REPORTS_DIR", Path(directory)):
                report = save_report("a" * 64, catalog, self.changes, [cached], [])
            path = Path(directory) / ("a" * 64) / f"{report['reportId']}.json"
            persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted["impacts"]), 2)
        self.assertEqual(persisted["coverage"], "lineage_scope_analyzed")
        self.assertEqual(persisted["impacts"][0]["proposedFixes"], [])
        self.assertEqual(persisted["schemaVersion"], 5)

    def test_response_schema_allows_explicitly_blocked_fixes(self):
        finding = RESPONSE_SCHEMA["properties"]["impacts"]["items"]["properties"]
        self.assertEqual(finding["proposedFixes"].get("minItems", 0), 0)
        self.assertEqual(set(finding["fixUnavailableReason"]["type"]), {"string", "null"})


if __name__ == "__main__":
    unittest.main()
