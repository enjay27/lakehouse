"""kubeconform schemas from CustomResourceDefinitions (ADR-010, ADR-038): the static gate
validates rendered charts against the CRDs in use, today CNPG's from the gate's pinned chart.

`write(docs, root)` writes `<root>/<group>/<kind>_<version>.json` for each served version of
each CRD among `docs`, from its openAPIV3Schema. The schema is made strict, as kubeconform's
openapi2jsonschema does in strict mode: every object with `properties` and no
`additionalProperties` refuses unknown fields, unless it is marked
`x-kubernetes-preserve-unknown-fields`. So a misspelt or misplaced field fails the gate
instead of being dropped by the API server (ADR-016).

`location(root)` is the matching `-schema-location` for kubeconform.
"""

import json
import os

COMBINATORS = ("allOf", "anyOf", "oneOf")


def strict(schema):
    """A copy of schema in which objects with properties refuse unknown fields."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if "properties" in out:
        out["properties"] = {k: strict(v) for k, v in out["properties"].items()}
        if ("additionalProperties" not in out
                and not out.get("x-kubernetes-preserve-unknown-fields")):
            out["additionalProperties"] = False
    if isinstance(out.get("additionalProperties"), dict):
        out["additionalProperties"] = strict(out["additionalProperties"])
    if isinstance(out.get("items"), dict):
        out["items"] = strict(out["items"])
    for key in COMBINATORS:
        if key in out:
            out[key] = [strict(s) for s in out[key]]
    return out


def write(docs, root):
    """Writes the schemas; returns their paths. No CRD among docs is an error."""
    paths = []
    for doc in docs:
        if not doc or doc.get("kind") != "CustomResourceDefinition":
            continue
        group, kind = doc["spec"]["group"], doc["spec"]["names"]["kind"].lower()
        for version in doc["spec"]["versions"]:
            if not version.get("served", True):
                continue
            path = os.path.join(root, group, f"{kind}_{version['name']}.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(strict(version["schema"]["openAPIV3Schema"]), f)
            paths.append(path)
    if not paths:
        raise ValueError("no CustomResourceDefinition among the documents")
    return paths


def location(root):
    return os.path.join(root, "{{ .Group }}", "{{ .ResourceKind }}_{{ .ResourceAPIVersion }}.json")
