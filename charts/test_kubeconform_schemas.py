"""Tests for kubeconform_schemas.py. Run: python3 -m unittest discover -s charts"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kubeconform_schemas  # noqa: E402


def crd(group="postgresql.cnpg.io", kind="Cluster", versions=(("v1", True),), schema=None):
    schema = schema or {"type": "object", "properties": {"spec": {
        "type": "object", "properties": {"instances": {"type": "integer"}}}}}
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "spec": {"group": group, "names": {"kind": kind},
                     "versions": [{"name": v, "served": served,
                                   "schema": {"openAPIV3Schema": schema}}
                                  for v, served in versions]}}


class Write(unittest.TestCase):
    def write(self, *docs):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        paths = kubeconform_schemas.write(docs, d.name)
        return d.name, paths

    def load(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_one_file_per_served_version_where_the_location_points(self):
        root, paths = self.write(crd(versions=(("v1", True), ("v1beta1", False))))
        self.assertEqual(paths, [os.path.join(root, "postgresql.cnpg.io", "cluster_v1.json")])
        location = kubeconform_schemas.location(root)
        self.assertEqual(location.replace("{{ .Group }}", "postgresql.cnpg.io")
                         .replace("{{ .ResourceKind }}", "cluster")
                         .replace("{{ .ResourceAPIVersion }}", "v1"), paths[0])

    def test_objects_with_properties_refuse_unknown_fields(self):
        _, paths = self.write(crd())
        schema = self.load(paths[0])
        self.assertIs(schema["additionalProperties"], False)
        self.assertIs(schema["properties"]["spec"]["additionalProperties"], False)

    def test_preserve_unknown_fields_and_explicit_maps_stay_open(self):
        _, paths = self.write(crd(schema={"type": "object", "properties": {
            "free": {"type": "object", "x-kubernetes-preserve-unknown-fields": True,
                     "properties": {"a": {"type": "string"}}},
            "labels": {"type": "object", "additionalProperties": {"type": "string"}}}}))
        props = self.load(paths[0])["properties"]
        self.assertNotIn("additionalProperties", props["free"])
        self.assertEqual(props["labels"]["additionalProperties"], {"type": "string"})

    def test_list_items_and_combinators_are_made_strict_too(self):
        item = {"type": "object", "properties": {"name": {"type": "string"}}}
        _, paths = self.write(crd(schema={"type": "object", "properties": {
            "list": {"type": "array", "items": dict(item)},
            "either": {"anyOf": [dict(item)]}}}))
        props = self.load(paths[0])["properties"]
        self.assertIs(props["list"]["items"]["additionalProperties"], False)
        self.assertIs(props["either"]["anyOf"][0]["additionalProperties"], False)

    def test_other_documents_are_ignored(self):
        _, paths = self.write(None, {"kind": "ConfigMap"}, crd())
        self.assertEqual(len(paths), 1)

    def test_no_crd_at_all_is_an_error(self):
        with self.assertRaises(ValueError):
            self.write({"kind": "ConfigMap"})


if __name__ == "__main__":
    unittest.main()
