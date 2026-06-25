"""
live_state_fetcher.py

Queries AWS directly for the *actual* state of resources Terraform manages.
This is the other half of the drift comparison: what's really running,
regardless of what Terraform thinks.

Design note: we deliberately keep one fetcher function per resource type
rather than a generic "describe everything" call. AWS's describe APIs
return wildly different shapes per service, and a generic approach would
just push the normalization problem downstream into the differ. Mapping
resource_type -> fetcher keeps each fetcher small and testable, and adding
a new resource type is a one-function addition, not a refactor.
"""

import boto3
from dataclasses import dataclass


@dataclass
class LiveResource:
    address: str
    resource_type: str
    attributes: dict
    found: bool = True  # False if the resource no longer exists in AWS at all


class LiveStateFetcher:
    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.ec2 = boto3.client("ec2", region_name=region)
        self.s3 = boto3.client("s3", region_name=region)

        self._fetchers = {
            "aws_security_group": self._fetch_security_group,
            "aws_instance": self._fetch_instance,
            "aws_s3_bucket": self._fetch_s3_bucket,
        }

    def fetch(self, address: str, resource_type: str, terraform_attrs: dict) -> LiveResource:
        fetcher = self._fetchers.get(resource_type)
        if fetcher is None:
            raise NotImplementedError(
                f"No live-state fetcher registered for resource_type={resource_type}. "
                f"Add one in LiveStateFetcher._fetchers before drift-checking this type."
            )
        return fetcher(address, terraform_attrs)

    # -- Security Group ------------------------------------------------------

    def _fetch_security_group(self, address: str, tf_attrs: dict) -> LiveResource:
        sg_id = tf_attrs.get("id")
        if not sg_id:
            return LiveResource(address, "aws_security_group", {}, found=False)

        try:
            resp = self.ec2.describe_security_groups(GroupIds=[sg_id])
        except self.ec2.exceptions.ClientError:
            return LiveResource(address, "aws_security_group", {}, found=False)

        sg = resp["SecurityGroups"][0]

        ingress = self._parse_rules(sg.get("IpPermissions", []))
        egress = self._parse_rules(sg.get("IpPermissionsEgress", []))

        return LiveResource(
            address=address,
            resource_type="aws_security_group",
            attributes={
                "id": sg["GroupId"],
                "ingress": ingress,
                "egress": egress,
                "tags": {t["Key"]: t["Value"] for t in sg.get("Tags", [])},
            },
        )

    @staticmethod
    def _parse_rules(rules: list[dict]) -> list[dict]:
        """
        Shared parser for IpPermissions (ingress) and IpPermissionsEgress
        (egress) - boto3 returns both in the identical shape, just under
        different top-level keys.

        Normalization note: when IpProtocol is "-1" (all traffic), AWS's
        API omits FromPort/ToPort entirely since there's no port range
        concept for "all protocols." Terraform's state represents this
        same rule with from_port=0, to_port=0. Without normalizing these
        to match, the differ would report false drift on every "allow
        all" rule on every run, even when nothing actually changed.
        """
        parsed = []
        for rule in rules:
            protocol = rule.get("IpProtocol")
            from_port = rule.get("FromPort")
            to_port = rule.get("ToPort")

            if protocol == "-1":
                from_port = 0
                to_port = 0

            parsed.append(
                {
                    "from_port": from_port,
                    "to_port": to_port,
                    "protocol": protocol,
                    "cidr_blocks": sorted(r["CidrIp"] for r in rule.get("IpRanges", [])),
                }
            )
        return parsed

    # -- EC2 Instance ---------------------------------------------------------

    def _fetch_instance(self, address: str, tf_attrs: dict) -> LiveResource:
        instance_id = tf_attrs.get("id")
        if not instance_id:
            return LiveResource(address, "aws_instance", {}, found=False)

        try:
            resp = self.ec2.describe_instances(InstanceIds=[instance_id])
        except self.ec2.exceptions.ClientError:
            return LiveResource(address, "aws_instance", {}, found=False)

        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0]["Instances"]:
            return LiveResource(address, "aws_instance", {}, found=False)

        instance = reservations[0]["Instances"][0]

        if instance["State"]["Name"] in ("terminated", "shutting-down"):
            return LiveResource(address, "aws_instance", {}, found=False)

        return LiveResource(
            address=address,
            resource_type="aws_instance",
            attributes={
                "id": instance["InstanceId"],
                "instance_type": instance["InstanceType"],
                "ami": instance["ImageId"],
                "state": instance["State"]["Name"],
                "tags": {t["Key"]: t["Value"] for t in instance.get("Tags", [])},
                "security_group_ids": sorted(
                    sg["GroupId"] for sg in instance.get("SecurityGroups", [])
                ),
            },
        )

    # -- S3 Bucket --------------------------------------------------------------

    def _fetch_s3_bucket(self, address: str, tf_attrs: dict) -> LiveResource:
        bucket = tf_attrs.get("id") or tf_attrs.get("bucket")
        if not bucket:
            return LiveResource(address, "aws_s3_bucket", {}, found=False)

        try:
            self.s3.head_bucket(Bucket=bucket)
        except self.s3.exceptions.ClientError:
            return LiveResource(address, "aws_s3_bucket", {}, found=False)

        versioning = self.s3.get_bucket_versioning(Bucket=bucket)
        try:
            tagging = self.s3.get_bucket_tagging(Bucket=bucket)
            tags = {t["Key"]: t["Value"] for t in tagging.get("TagSet", [])}
        except self.s3.exceptions.ClientError:
            tags = {}

        return LiveResource(
            address=address,
            resource_type="aws_s3_bucket",
            attributes={
                "id": bucket,
                "versioning_status": versioning.get("Status", "Disabled"),
                "tags": tags,
            },
        )
