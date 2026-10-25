import copy
import re
import unittest
from unittest.mock import patch

from flask import Flask
from jinja2 import StrictUndefined

from fabric.backend.model_lineage import GRAPH_VERSION
from fabric.frontend.routes import fabric_frontend
from shared.frontend.routes import shared_frontend


class PresetPageTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="preset-page-test")
        app.jinja_env.undefined = StrictUndefined
        app.register_blueprint(shared_frontend)
        app.register_blueprint(fabric_frontend)
        app.add_url_rule(
            "/mapping/<preset_id>/<report_format>",
            endpoint="fabric_api.download_fabric_mapping",
            view_func=lambda **kwargs: "",
        )
        app.add_url_rule(
            "/process/<preset_id>",
            endpoint="fabric_api.process_fabric_preset",
            view_func=lambda **kwargs: "",
        )
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["user"] = {"name": "Test user"}
        self.preset = {
            "presetId": "a" * 64,
            "presetName": "Test model",
            "semanticModel": {"name": "Test model", "path": "/Test.SemanticModel"},
            "workspace": {"displayName": "Test workspace"},
            "repository": {"name": "Test repository", "branchName": "main"},
            "localRepositoryPath": "local-snapshot",
        }
        dependency = {
            "table": "sales.orders",
            "status": "resolved",
            "defaultLakehouse": {"default_lakehouse_name": "hidden-lakehouse"},
            "evidence": [{"file": "hidden-notebook.py", "line": 71, "method": "spark.sql"}],
        }
        self.mapping = {
            "graph": {
                "version": GRAPH_VERSION,
                "modelPath": self.preset["semanticModel"]["path"],
                "root": "model",
                "nodes": [{"id": "model", "type": "SemanticModel", "label": "Test model"}],
                "edges": [],
            },
            "modelTables": [
                {
                    "name": "Account Field Geography",
                    "status": "resolved",
                    "sources": [{
                        "table": "omidimensions.dimaccountfieldgeographyhierarchy",
                        "scope": {"workspace": "hidden-scope"},
                        "evidence": [{
                            "file": "hidden-model.tmdl", "line": 22,
                            "partition": "hidden-partition",
                        }],
                    }],
                },
                {
                    "name": "Orders",
                    "status": "resolved",
                    "sources": [
                        {"table": "sales.orders"},
                        {"table": "archive.orders"},
                    ],
                },
                {"name": "Calculated table", "status": "calculated", "sources": []},
            ],
            "pipelines": [{
                "key": "hidden-pipeline-key", "name": "Daily load",
                "file": "hidden-pipeline.json", "id": "hidden-pipeline-id",
            }],
            "pipelineLinks": [{
                "pipelineKey": "hidden-pipeline-key",
                "targetKey": "hidden-notebook-key",
                "targetName": "Load <Orders>", "reference": "hidden-notebook-id",
                "activity": "hidden-activity", "activityPath": "/hidden-activity-path",
                "kind": "TridentNotebook", "status": "resolved",
                "workspaceId": "hidden-workspace", "parameters": {"hidden-parameter": 3},
                "dependsOn": ["hidden-dependency"],
                "reads": [dependency], "writes": [{**dependency, "table": "sales.output"}],
            }],
            "notebooks": [
                {
                    "key": "hidden-notebook-key", "name": "Load <Orders>",
                    "file": "hidden-notebook.py", "id": "hidden-notebook-id",
                    "reads": [dependency],
                    "writes": [{**dependency, "table": "sales.output"}],
                },
                {
                    "key": "hidden-helper-key", "name": "Helper notebook",
                    "file": "hidden-helper.py", "id": "hidden-helper-id",
                    "reads": [], "writes": [],
                },
            ],
            "notebookLinks": [{
                "notebookKey": "hidden-notebook-key", "targetKey": "hidden-helper-key",
                "reference": "hidden-helper-id", "status": "resolved",
                "evidence": {"line": 92},
            }],
            "tables": [{
                "key": "hidden-table-identity", "name": "sales.orders",
                "writers": [{
                    "notebookKey": "hidden-notebook-key",
                    "pipelineKey": "hidden-pipeline-key",
                }],
                "readers": [{
                    "notebookKey": "hidden-helper-key", "pipelineKey": None,
                }],
            }],
            "summary": {
                "pipelines": 1, "notebooks": 2, "pipelineNotebookCalls": 1,
                "readTables": 1, "writtenTables": 1, "gaps": 1,
            },
            "issues": [{"message": "Review missing source", "file": "gap.py", "line": 5}],
            "limitations": ["Static analysis only."],
            "generatedAt": "2026-10-25",
        }

    def render_page(self, mapping):
        with (
            patch("fabric.frontend.routes.load_preset", return_value=self.preset),
            patch("fabric.frontend.routes.load_mapping", return_value=mapping),
        ):
            response = self.client.get(f"/fabric/presets/{self.preset['presetId']}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def records(self, html):
        return re.findall(
            r"<(?:div|details)\b[^>]*\bdata-lineage-record\b[^>]*>"
            r"(.*?)</(?:div|details)>",
            html, flags=re.DOTALL,
        )

    def templates(self, html):
        return dict(re.findall(
            r'<template id="([^"]+)"[^>]*>(.*?)</template>', html, flags=re.DOTALL,
        ))

    def browser_html(self, html):
        return html.split('id="lineageArtifactBrowser"', 1)[1].split(
            '<details class="lineage-record">', 1,
        )[0]

    def test_graph_heading_and_help_removed_without_breaking_accessibility(self):
        html = self.render_page(self.mapping)
        self.assertNotIn('id="lineageGraphHeading"', html)
        self.assertNotIn('id="lineageGraphHelp"', html)
        self.assertIn('aria-label="Semantic model lineage"', html)
        self.assertNotIn("lineageGraphKeyboardHelp", html)
        self.assertNotIn("lineageGraphStatus", html)
        self.assertNotIn("lineageGraphDetails", html)
        self.assertNotIn("Fabric artifact mapping", html)
        self.assertNotIn("All downloaded Fabric pipelines and notebooks", html)
        self.assertIn('id="lineageGraphData"', html)
        self.assertIn('id="lineageGraphTable"', html)

    def test_source_cards_contain_only_names_and_physical_tables(self):
        records = self.records(self.render_page(self.mapping))
        self.assertEqual(len(records), 3)
        for record, table in zip(records, self.mapping["modelTables"]):
            self.assertEqual(re.findall(r"<h4>(.*?)</h4>", record), [table["name"]])
            self.assertEqual(
                re.findall(r"<code>(.*?)</code>", record),
                [source["table"] for source in table["sources"]],
            )
            visible = re.sub(r"<[^>]*>", "", record)
            expected = [table["name"], *(source["table"] for source in table["sources"])]
            self.assertEqual(visible.split(), " ".join(expected).split())

    def test_graph_source_table_toggle_defaults_to_collapsed_without_changing_mapping(self):
        original = copy.deepcopy(self.mapping)
        html = self.render_page(self.mapping)
        toggle = re.search(r'<input\b[^>]*id="lineageGraphTables"[^>]*>', html)
        self.assertIsNotNone(toggle)
        self.assertIn('type="checkbox"', toggle.group())
        self.assertIn('aria-controls="lineageGraph"', toggle.group())
        self.assertNotIn("checked", toggle.group())
        self.assertIn("Show source tables", html)
        self.assertIn('id="lineageGraphReads"', html)
        self.assertIn('id="lineageGraphTable"', html)
        self.assertEqual(self.mapping, original)

    def test_sidebar_has_two_tabs_with_flat_lists_and_no_visible_tables(self):
        original = copy.deepcopy(self.mapping)
        html = self.render_page(self.mapping)
        browser = self.browser_html(html)
        visible = re.sub(r"<template\b.*?</template>", "", browser, flags=re.DOTALL)
        self.assertIn('id="lineagePipelinesTab" role="tab"', visible)
        self.assertIn('id="lineageNotebooksTab" role="tab"', visible)
        self.assertIn('aria-selected="true" tabindex="0">Pipelines</button>', visible)
        self.assertIn('aria-selected="false" tabindex="-1">Notebooks</button>', visible)
        self.assertIn('id="lineagePipelinesList" role="tabpanel" aria-labelledby="lineagePipelinesTab">', visible)
        self.assertIn('id="lineageNotebooksList" role="tabpanel" aria-labelledby="lineageNotebooksTab" hidden>', visible)
        self.assertIn('class="lineage-artifact-name">Daily load</strong>', visible)
        self.assertIn('class="lineage-artifact-name">Load &lt;Orders&gt;</strong>', visible)
        self.assertIn('class="lineage-artifact-name">Helper notebook</strong>', visible)
        self.assertIn('id="lineageNotebookDetails" aria-live="polite"', visible)
        self.assertIn("Select a pipeline to see its notebooks", visible)
        self.assertNotIn("<code>", visible)
        self.assertNotIn("sales.orders", visible)
        self.assertNotIn("sales.output", visible)
        self.assertNotIn("Table &rarr; producers", html)
        self.assertEqual(visible.count('aria-expanded="false"'), 2)
        self.assertEqual(visible.count('aria-controls="lineageNotebookDetails"'), 3)
        sidebar = visible.split("<aside", 1)[1].split("</aside>", 1)[0]
        self.assertNotIn("<details", sidebar)
        self.assertNotIn("<select", sidebar)
        self.assertNotIn("lineagePipelineNotebook", sidebar)
        self.assertEqual(sidebar.count("<ul"), 2)
        self.assertEqual(sidebar.count('role="tab"'), 2)
        self.assertEqual(self.mapping, original)

    def test_pipeline_notebooks_are_only_in_right_panel_templates(self):
        templates = self.templates(self.render_page(self.mapping))
        pipeline = templates["lineagePipelineContent0"]
        self.assertIn("<h4>Daily load</h4>", pipeline)
        self.assertIn('data-lineage-notebook="lineagePipelineNotebook0-0"', pipeline)
        self.assertIn('class="lineage-artifact-name">Load &lt;Orders&gt;</strong>', pipeline)
        self.assertIn('data-lineage-notebook-target="lineageNotebook0"', pipeline)
        self.assertIn("<div data-lineage-pipeline-tables></div>", pipeline)
        self.assertNotIn("<code>", pipeline)
        self.assertNotIn("<details", pipeline)

    def test_notebook_templates_show_only_names_and_read_write_relationships(self):
        html = self.render_page(self.mapping)
        templates = self.templates(html)
        self.assertEqual(len(templates), 4)
        pipeline = templates["lineagePipelineNotebook0-0"]
        notebook = templates["lineageNotebook0"]
        helper = templates["lineageNotebook1"]
        for content in (pipeline, notebook):
            self.assertIn("<h4>Load &lt;Orders&gt;</h4>", content)
            self.assertIn("<h4>Read tables</h4>", content)
            self.assertIn("<h4>Write tables</h4>", content)
            self.assertIn("<code>sales.orders</code>", content)
            self.assertIn("<code>sales.output</code>", content)
        self.assertIn("Daily load", pipeline)
        self.assertIn("Notebook defaults", notebook)
        self.assertIn("Calls notebook: Helper notebook", notebook)
        self.assertIn("<h4>Helper notebook</h4>", helper)
        self.assertIn("No statically identified table dependencies.", helper)
        for record in [*self.records(html), *templates.values()]:
            for metadata in (
                "hidden-", "resolved", "ID:", "Scope:", "Partition:",
                "Parameters:", "Workspace:", "Identity:", "writer contexts",
                "reader contexts", "spark.sql",
            ):
                self.assertNotIn(metadata, record)
        self.assertIn("Download full JSON mapping", html)
        self.assertIn("Download evidence CSV", html)

    def test_analysis_gaps_section_and_summary_count_are_not_displayed(self):
        original = copy.deepcopy(self.mapping)
        html = self.render_page(self.mapping)
        self.assertNotIn("Analysis gaps", html)
        self.assertNotIn("<dt>Gaps</dt>", html)
        self.assertNotIn("Review missing source", html)
        self.assertNotIn("gap.py:5", html)
        self.assertNotIn("gaps below", html)
        self.assertIn("<dt>Pipelines</dt>", html)
        self.assertIn("<dt>Notebooks</dt>", html)
        self.assertIn("<summary>Scope and limitations</summary>", html)
        self.assertIn("Download full JSON mapping", html)
        self.assertEqual(self.mapping, original)

    def test_pipeline_card_counts_notebook_calls_not_child_pipelines(self):
        self.mapping["pipelineLinks"].append({
            "pipelineKey": "hidden-pipeline-key", "kind": "ExecutePipeline",
            "targetKey": None, "targetName": None, "reference": "Missing pipeline",
        })
        self.mapping["pipelines"].append({"key": "empty-pipeline", "name": "Empty_Pipeline"})
        html = self.render_page(self.mapping)
        self.assertIn('class="lineage-artifact-name">Daily load</strong>', html)
        self.assertIn('class="lineage-artifact-count">1 notebook</small>', html)
        self.assertIn('class="lineage-artifact-count">0 notebooks</small>', html)
        self.mapping["pipelineLinks"].append(dict(self.mapping["pipelineLinks"][0]))
        html = self.render_page(self.mapping)
        self.assertIn('class="lineage-artifact-count">2 notebooks</small>', html)

    def test_notebook_card_read_write_counts(self):
        self.mapping["notebooks"][0]["reads"].append({"table": "sales.other"})
        html = self.render_page(self.mapping)
        self.assertIn('class="lineage-artifact-count">2 read tables &middot; 1 write table</small>', html)
        self.assertIn('class="lineage-artifact-count">0 read tables &middot; 0 write tables</small>', html)

    def test_pipeline_redirect_targets_inventory_identity_not_duplicate_name(self):
        self.mapping["notebooks"][1]["name"] = self.mapping["notebooks"][0]["name"]
        self.mapping["pipelineLinks"][0]["targetKey"] = "hidden-helper-key"
        templates = self.templates(self.render_page(self.mapping))
        self.assertIn(
            'data-lineage-notebook-target="lineageNotebook1"',
            templates["lineagePipelineContent0"],
        )
        self.assertNotIn(
            'data-lineage-notebook-target="lineageNotebook0"',
            templates["lineagePipelineContent0"],
        )

    def test_pipeline_invocations_keep_their_own_tables_separate_from_defaults(self):
        first = self.mapping["pipelineLinks"][0]
        first["reads"] = [{"table": "sales.first_invocation"}]
        self.mapping["pipelineLinks"].append({
            **first,
            "activity": "Another invocation",
            "reads": [{"table": "sales.second_invocation"}],
        })
        templates = self.templates(self.render_page(self.mapping))
        for index, name in enumerate(("first", "second")):
            content = templates[f"lineagePipelineNotebook0-{index}"]
            self.assertIn(f"<code>sales.{name}_invocation</code>", content)
            self.assertNotIn("<code>sales.orders</code>", content)
        self.assertNotIn("sales.second_invocation", templates["lineagePipelineNotebook0-0"])
        self.assertNotIn("sales.first_invocation", templates["lineagePipelineNotebook0-1"])
        self.assertIn("<code>sales.orders</code>", templates["lineageNotebook0"])

    def test_child_pipeline_is_navigation_not_a_notebook(self):
        self.mapping["pipelines"].append({
            "key": "child-pipeline-key", "name": "Child pipeline",
        })
        self.mapping["pipelineLinks"].append({
            "pipelineKey": "hidden-pipeline-key", "kind": "ExecutePipeline",
            "targetKey": "child-pipeline-key", "targetName": "Child pipeline",
        })
        html = self.render_page(self.mapping)
        self.assertIn('data-lineage-child-pipeline="lineagePipeline1"', html)
        self.assertIn('id="lineagePipeline1"', html)
        self.assertEqual(len(self.templates(html)), 5)

    def test_notebook_template_ids_match_when_child_pipeline_precedes_notebook(self):
        self.mapping["pipelineLinks"].insert(0, {
            "pipelineKey": "hidden-pipeline-key", "kind": "ExecutePipeline",
            "targetKey": None, "targetName": None, "reference": "Missing pipeline",
        })
        templates = self.templates(self.render_page(self.mapping))
        self.assertIn(
            'data-lineage-notebook="lineagePipelineNotebook0-1"',
            templates["lineagePipelineContent0"],
        )
        self.assertIn("<code>sales.orders</code>", templates["lineagePipelineNotebook0-1"])
        self.assertNotIn("lineagePipelineNotebook0-0", templates)

    def test_unmatched_calls_and_empty_dependencies_remain_visible(self):
        link = self.mapping["pipelineLinks"][0]
        link.update(targetKey=None, targetName=None, reference="Missing notebook", reads=[], writes=[])
        call = self.mapping["notebookLinks"][0]
        call.update(targetKey=None, reference="Missing helper", status="unresolved")
        link["status"] = "unresolved"
        templates = self.templates(self.render_page(self.mapping))
        pipeline = templates["lineagePipelineNotebook0-0"]
        notebook = templates["lineageNotebook0"]
        self.assertIn("<h4>Missing notebook</h4>", pipeline)
        self.assertIn("No statically identified table dependencies.", pipeline)
        self.assertIn("This notebook reference could not be matched.", pipeline)
        self.assertIn("Calls notebook: Missing helper", notebook)
        self.assertNotIn("unresolved", notebook)
        self.assertNotIn("data-lineage-notebook-target", templates["lineagePipelineContent0"])

    def test_empty_inventory(self):
        for field in ("pipelines", "pipelineLinks", "notebooks", "notebookLinks", "tables"):
            self.mapping[field] = []
        html = self.render_page(self.mapping)
        self.assertIn("No Fabric pipeline folders found.", html)
        self.assertIn("No Fabric notebook folders found.", html)
        self.assertEqual(len(self.records(html)), 3)
        self.assertEqual(self.templates(html), {})

    def test_missing_or_outdated_mapping_still_starts_processing(self):
        for mapping in (None, {**self.mapping, "graph": {"version": -1}}):
            with self.subTest(mapping_available=mapping is not None):
                html = self.render_page(mapping)
                self.assertIn('data-auto-process="true"', html)
                self.assertNotIn('id="semanticModelPresetLayout"', html)


if __name__ == "__main__":
    unittest.main()
