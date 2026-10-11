# The gate's test policy for Polaris's OPA authorizer (ADR-026, ADR-031): on the gate and test
# clusters only; the target clusters' policy is the catalog gateway's. Polaris POSTs to the
# package (`.../v1/data/polaris/authz`) and reads `result.allow`, so `allow` is the package's
# only complete rule: helpers are functions, which stay out of the document Polaris reads.
# The input Polaris 1.6.0 sends: docs/contracts/polaris-opa-input.md.
#
# - root may do everything: in OPA mode Polaris asks about root too, and bootstrap alone
#   bypasses the authorizer, so a policy without this rule locks root out.
# - gate_smoke, the smoke tests' principal, may only do what the smoke tests need, and only
#   inside the catalog gate_catalog.
# - Anything else is refused.
package polaris.authz

default allow := false

allow if input.actor.principal == "root"

allow if {
	input.actor.principal == "gate_smoke"
	input.action in {
		"LIST_NAMESPACES", "CREATE_NAMESPACE", "LOAD_NAMESPACE_METADATA",
		"LIST_TABLES", "CREATE_TABLE_DIRECT", "LOAD_TABLE", "UPDATE_TABLE",
	}
	count(input.resource.targets) > 0
	every target in input.resource.targets {
		in_catalog(target, "gate_catalog")
	}
}

in_catalog(entity, name) if {
	entity.type == "CATALOG"
	entity.name == name
}

in_catalog(entity, name) if {
	some parent in entity.parents
	parent.type == "CATALOG"
	parent.name == name
}
