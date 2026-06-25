"""
state_extractor.py

Extracts the *desired* state from Terraform — what Terraform believes it
manages and what attributes it expects each resource to have.

We use `terraform show -json` rather than reading the .tfstate file directly.
The raw state file format is an internal implementation detail and has
changed across Terraform versions; `terraform show -json` is the documented,
stable interface for machine consumption.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ManagedResource:
    """A single resource Terraform believes it owns, normalized for diffing."""

    address: str          # e.g. aws_security_group.sandbox
    resource_type: str     # e.g. aws_security_group
    name: str               # e.g. sandbox
    provider: str           # e.g. registry.terraform.io/hashicorp/aws
    attributes: dict = field(default_factory=dict)


class TerraformStateExtractor:
    """Wraps `terraform show -json` and normalizes the output."""

    def __init__(self, working_dir: str):
        self.working_dir = Path(working_dir)

    def extract(self) -> list[ManagedResource]:
        raw = self._run_terraform_show()
        return self._parse(raw)

    def _run_terraform_show(self) -> dict:
        result = subprocess.run(
            ["terraform", "show", "-json"],
            cwd=self.working_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def _parse(self, raw: dict) -> list[ManagedResource]:
        resources = []

        root_module = raw.get("values", {}).get("root_module", {})
        for res in root_module.get("resources", []):
            # Skip data sources — they're read-only lookups, not managed state,
            # and have no "desired" form to drift from.
            if res.get("mode") != "managed":
                continue

            resources.append(
                ManagedResource(
                    address=res["address"],
                    resource_type=res["type"],
                    name=res["name"],
                    provider=res.get("provider_name", "unknown"),
                    attributes=res.get("values", {}),
                )
            )

        return resources


if __name__ == "__main__":
    import sys

    extractor = TerraformStateExtractor(working_dir=sys.argv[1] if len(sys.argv) > 1 else ".")
    for r in extractor.extract():
        print(f"{r.address} ({r.resource_type})")
