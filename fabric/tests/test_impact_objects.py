import json
import unittest

from fabric.backend.impact_context import scope_catalog
from fabric.backend.impact_objects import _model_objects, _wire_model, target_catalog


def model_fixture(tmdl=False):
    tables = [
        {
            "name": "Orders",
            "columns": [
                {"name": name, "dataType": "int64", "sourceColumn": name}
                for name in ("CustomerKey", "DateKey", "Amount")
            ],
            "measures": [
                {"name": "Revenue", "expression": "SUM(Orders[Amount])"},
                {"name": "Revenue Twice", "expression": "[Revenue] * 2"},
                {"name": "By Date", "expression": (
                    "CALCULATE([Revenue], USERELATIONSHIP(Orders[DateKey], 'Date'[Key]))"
                )},
                {"name": "No Customer Filter", "expression": (
                    "CALCULATE([Revenue], CROSSFILTER(Customers[Key], Orders[CustomerKey], NONE))"
                )},
                {"name": "Constant", "expression": "1"},
                {"name": "Comment Only", "expression": (
                    '1 // USERELATIONSHIP(Orders[DateKey], \'Date\'[Key])\n'
                    '/* SUM(Orders[Amount]) */ + LEN("Orders[Amount]")'
                )},
            ],
        },
        {
            "name": "Customers",
            "columns": [{"name": "Key", "dataType": "int64", "sourceColumn": "Key"}],
            "measures": [{"name": "Customer Count", "expression": "COUNTROWS(Customers)"}],
        },
        {
            "name": "Date",
            "columns": [{"name": "Key", "dataType": "int64", "sourceColumn": "Key"}],
        },
        {
            "name": "Disconnected",
            "columns": [{"name": "Key", "dataType": "int64", "sourceColumn": "Key"}],
            "measures": [{"name": "Other Count", "expression": "COUNTROWS(Disconnected)"}],
        },
    ]
    relationships = [
        {
            "name": "customer_orders", "fromTable": "Orders", "fromColumn": "CustomerKey",
            "toTable": "Customers", "toColumn": "Key",
        },
        {
            "name": "date_orders", "fromTable": "Orders", "fromColumn": "DateKey",
            "toTable": "Date", "toColumn": "Key", "isActive": False,
        },
    ]
    if tmdl:
        files = {}
        for table in tables:
            lines = [f"table '{table['name']}'\n"]
            lines.extend(
                f"\tcolumn '{column['name']}'\n\t\tdataType: int64\n"
                for column in table["columns"]
            )
            lines.extend(
                f"\tmeasure '{measure['name']}' =\n"
                + "".join(f"\t\t{line}\n" for line in measure["expression"].splitlines())
                for measure in table.get("measures", [])
            )
            files[f"{table['name']}.tmdl"] = "".join(lines)
        files["relationships.tmdl"] = "".join(
            f"relationship {relationship['name']}\n"
            f"\tfromColumn: '{relationship['fromTable']}'.'{relationship['fromColumn']}'\n"
            f"\ttoColumn: '{relationship['toTable']}'.'{relationship['toColumn']}'\n"
            + ("\tisActive: false\n" if not relationship.get("isActive", True) else "")
            for relationship in relationships
        )
    else:
        files = {"model.bim": json.dumps({"model": {"tables": tables, "relationships": relationships}})}
    return {
        "key": "Test.SemanticModel", "name": "Test", "type": "SemanticModel",
        "files": files, "context": {"tables": [
            {**table, "sources": [], "sourceStatus": "unresolved"} for table in tables
        ]},
    }


def targeted_fixture(artifact, selections):
    changes = [{
        "id": f"change-{index}", "table": table, "column": column,
        "current": {"sourceColumn": column}, "sources": [],
    } for index, (table, column) in enumerate(selections, 1)]
    catalog = {
        "selection": {"semanticModel": {"path": "/" + artifact["key"]}},
        "artifacts": [artifact], "issues": [],
        "lineage": {"tables": [], "notebookLinks": [], "pipelineLinks": []},
    }
    scoped = target_catalog(scope_catalog(catalog, changes), catalog, changes)
    return catalog, changes, scoped


class ModelDependencyTests(unittest.TestCase):
    def test_active_relationship_reaches_fact_measures_and_transitive_consumers(self):
        for tmdl in (False, True):
            with self.subTest(tmdl=tmdl):
                _, _, scoped = targeted_fixture(model_fixture(tmdl), [("Customers", "Key")])
                targets = {target["objectName"]: target for target in scoped["artifacts"][0]["targets"]}
                for name in ("customer_orders", "Orders.Revenue", "Orders.Revenue Twice"):
                    self.assertIn(name, targets)
                for name in ("Orders.Constant", "Orders.Comment Only", "Disconnected.Other Count"):
                    self.assertNotIn(name, targets)
                self.assertNotIn("date_orders", targets)
                self.assertIn({
                    "source": targets["customer_orders"]["id"],
                    "target": targets["Orders.Revenue"]["id"],
                    "kind": "model_dependency", "confidence": "static",
                }, scoped["impactLineage"]["edges"])

    def test_explicit_inactive_relationship_is_dependency_and_context(self):
        for tmdl in (False, True):
            with self.subTest(tmdl=tmdl):
                artifact = model_fixture(tmdl)
                objects = _model_objects(artifact)
                issues = []
                _wire_model(objects, issues)
                by_name = {item["objectName"]: item for item in objects}
                relationship_id = by_name["date_orders"]["id"]
                self.assertIn(relationship_id, by_name["Orders.By Date"]["dependencies"])
                self.assertNotIn(relationship_id, by_name["Orders.Revenue"]["dependencies"])
                _, _, scoped = targeted_fixture(artifact, [("Date", "Key")])
                targets = {target["objectName"] for target in scoped["artifacts"][0]["targets"]}
                self.assertIn("Orders.By Date", targets)
                self.assertNotIn("Orders.Revenue", targets)
                self.assertNotIn("Orders.Comment Only", targets)

    def test_crossfilter_resolves_relationship_with_reversed_endpoints(self):
        objects = _model_objects(model_fixture())
        _wire_model(objects, [])
        by_name = {item["objectName"]: item for item in objects}
        self.assertIn(
            by_name["customer_orders"]["id"],
            by_name["Orders.No Customer Filter"]["dependencies"],
        )

    def test_relationship_direction_is_not_reversed(self):
        objects = _model_objects(model_fixture())
        _wire_model(objects, [])
        by_name = {item["objectName"]: item for item in objects}
        self.assertNotIn(
            by_name["customer_orders"]["id"], by_name["Customers.Customer Count"]["dependencies"],
        )

    def test_unresolved_relationship_endpoint_is_a_gap(self):
        artifact = model_fixture()
        document = json.loads(artifact["files"]["model.bim"])
        document["model"]["relationships"][0]["fromColumn"] = "MissingKey"
        artifact["files"]["model.bim"] = json.dumps(document)
        issues = []
        _wire_model(_model_objects(artifact), issues)
        self.assertTrue(any("MissingKey" in issue["message"] for issue in issues))

    def test_bidirectional_filter_path_reaches_dimension_measure(self):
        artifact = model_fixture()
        document = json.loads(artifact["files"]["model.bim"])
        document["model"]["relationships"][0]["crossFilteringBehavior"] = "bothDirections"
        artifact["files"]["model.bim"] = json.dumps(document)
        _, _, scoped = targeted_fixture(artifact, [("Orders", "CustomerKey")])
        names = {target["objectName"] for target in scoped["artifacts"][0]["targets"]}
        self.assertIn("Customers.Customer Count", names)
        self.assertNotIn("Disconnected.Other Count", names)

    def test_multi_hop_filter_path_and_cycles_terminate(self):
        artifact = model_fixture()
        document = json.loads(artifact["files"]["model.bim"])
        document["model"]["relationships"].append({
            "name": "region_customers", "fromTable": "Customers", "fromColumn": "Key",
            "toTable": "Disconnected", "toColumn": "Key", "crossFilteringBehavior": "bothDirections",
        })
        artifact["files"]["model.bim"] = json.dumps(document)
        _, _, scoped = targeted_fixture(artifact, [("Disconnected", "Key")])
        names = {target["objectName"] for target in scoped["artifacts"][0]["targets"]}
        self.assertIn("Orders.Revenue Twice", names)
        self.assertIn("region_customers", names)

    def test_related_calculated_column_retains_relationship(self):
        artifact = model_fixture()
        document = json.loads(artifact["files"]["model.bim"])
        document["model"]["tables"][0]["columns"].append({
            "name": "Customer Label", "type": "calculated", "expression": "RELATED(Customers[Key])",
        })
        artifact["files"]["model.bim"] = json.dumps(document)
        objects = _model_objects(artifact)
        _wire_model(objects, [])
        by_name = {item["objectName"]: item for item in objects}
        self.assertIn(
            by_name["customer_orders"]["id"], by_name["Orders.Customer Label"]["dependencies"],
        )

    def test_tmdl_endpoint_names_with_dots_and_escaped_quotes(self):
        artifact = {
            "key": "Test.SemanticModel", "files": {
                "orders.tmdl": "table Orders\n\tcolumn 'Customer.Key'\n",
                "customers.tmdl": "table 'Customer''s'\n\tcolumn 'Key.Value'\n",
                "relationships.tmdl": (
                    "relationship customer_orders\n"
                    "\tfromColumn: Orders.'Customer.Key'\n"
                    "\ttoColumn: 'Customer''s'.'Key.Value'\n"
                ),
            },
        }
        objects = _model_objects(artifact)
        issues = []
        _wire_model(objects, issues)
        relationship = next(item for item in objects if item["objectType"] == "relationship")
        self.assertEqual(len(relationship["dependencies"]), 2)
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
