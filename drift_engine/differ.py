"""
differ.py

Compares Terraform's desired state against AWS's actual state and produces
a structural diff - not a string diff, an attribute-level diff that
distinguishes "this changed" from "this is just how AWS represents it."

This is the piece that makes the difference between a useful drift report
and noise. A naive full-object diff against `describe_*` API responses
will report drift on every single resource, every single run, because
AWS responses include account-specific identifiers, ARNs, and
eventually-consistent fields that were never part of what you declared.

We solve this with an explicit per-resource-type field map: only fields
in this map are compared. Anything not listed is presence-only checked
(does the resource still exist) but never diffed on value.
"""

from dataclasses import dataclass, field
from enum import Enum

from live_state_fetcher import LiveResource
from state_extractor import ManagedResource


class DriftSeverity(str, Enum):
    SECURITY = "security"      # security group rules, IAM, public access changes
    STRUCTURAL = "structural"  # instance type, AMI, resource-defining attributes
    COSMETIC = "cosmetic"      # tags, descriptions


# Fields we actually compare per resource type. Anything outside this map
# is ignored for value-diffing - it's either a Terraform-internal id we
# can't meaningfully compare, or a field AWS mutates outside of any change
# a human made (e.g. LaunchTime).
COMPARABLE_FIELDS = {
    "aws_security_group": {
        "ingress": DriftSeverity.SECURITY,
        "egress": DriftSeverity.SECURITY,
        "tags": DriftSeverity.COSMETIC,
    },
    "aws_instance": {
        "instance_type": DriftSeverity.STRUCTURAL,
        "ami": DriftSeverity.STRUCTURAL,
        "security_group_ids": DriftSeverity.SECURITY,
        "tags": DriftSeverity.COSMETIC,
    },
    "aws_s3_bucket": {
        "versioning_status": DriftSeverity.STRUCTURAL,
        "tags": DriftSeverity.COSMETIC,
    },
}

# For list-of-dict fields (security group rules), Terraform's raw shape
# carries extra keys (description, ipv6_cidr_blocks, prefix_list_ids,
# security_groups, self) that live_state_fetcher.py deliberately doesn't
# track. Without projecting both sides down to the same key set first,
# every ingress/egress comparison would report false drift purely from
# shape mismatch, never from an actual value difference. This map lists
# which keys we keep when comparing list-of-dict fields.
RULE_PROJECTION_FIELDS = {"from_port", "to_port", "protocol", "cidr_blocks"}


@dataclass
class FieldDrift:
    field_name: str
    severity: DriftSeverity
    desired: object
    actual: object


@dataclass
class ResourceDrift:
    address: str
    resource_type: str
    deleted: bool = False  # resource exists in TF state but not in AWS at all
    field_drifts: list[FieldDrift] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return self.deleted or bool(self.field_drifts)


def diff_resource(desired: ManagedResource, live: LiveResource) -> ResourceDrift:
    if not live.found:
        return ResourceDrift(
            address=desired.address,
            resource_type=desired.resource_type,
            deleted=True,
        )

    comparable = COMPARABLE_FIELDS.get(desired.resource_type, {})
    drifts = []

    for field_name, severity in comparable.items():
        desired_value = _normalize(_project(desired.attributes.get(field_name)))
        actual_value = _normalize(_project(live.attributes.get(field_name)))

        if desired_value != actual_value:
            drifts.append(
                FieldDrift(
                    field_name=field_name,
                    severity=severity,
                    desired=desired_value,
                    actual=actual_value,
                )
            )

    return ResourceDrift(
        address=desired.address,
        resource_type=desired.resource_type,
        field_drifts=drifts,
    )


def _project(value):
    """
    Reduce list-of-dict fields (e.g. ingress/egress rules) down to only
    the keys both sides of the comparison actually populate. Terraform's
    native shape and our fetcher's normalized shape disagree on which
    extra metadata (description, ipv6_cidr_blocks, etc.) is present -
    projecting down to the shared key set is what makes the comparison
    meaningful rather than a shape mismatch.

    Non-list values (tags dicts, scalars) pass through unchanged - this
    projection only applies to the rule-list shape problem.
    """
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return [
            {k: v for k, v in item.items() if k in RULE_PROJECTION_FIELDS}
            for item in value
        ]
    return value


def _normalize(value):
    """
    Normalize values so comparison isn't tripped up by ordering or
    representation differences that don't reflect real drift - e.g.
    a list of CIDR blocks in a different order, or rules whose keys
    were inserted in a different order by Terraform vs. our fetcher.

    Sorting by str(x) is deliberately avoided here: a dict's string
    representation reflects key insertion order, not key content, so
    two dicts with identical key/value pairs inserted in a different
    order produce different strings and sort inconsistently relative
    to each other. Sorting by a tuple of sorted (key, value) pairs
    instead compares actual content, independent of insertion order.
    """
    if isinstance(value, list):
        try:
            return sorted(value, key=_sort_key)
        except TypeError:
            return value
    return value


def _sort_key(item):
    if isinstance(item, dict):
        return tuple(sorted((k, str(v)) for k, v in item.items()))
    return str(item)
