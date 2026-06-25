"""
main.py

Orchestrates the full drift detection run:
  1. Extract desired state from Terraform
  2. Fetch live state from AWS for each managed resource
  3. Diff desired vs actual
  4. Triage drift findings through Claude (engineer or slack format)
  5. Output a report (stdout by default, Slack if configured)

Usage:
    python main.py --working-dir ../tf-sg-explore --region us-east-1
    python main.py --working-dir ../tf-sg-explore --format slack --slack-webhook https://hooks.slack.com/...
"""

import argparse
import json
import os
import sys

from differ import diff_resource
from live_state_fetcher import LiveStateFetcher
from state_extractor import TerraformStateExtractor
from triage import triage_drifts


def run(working_dir: str, region: str, output_format: str) -> dict:
    extractor = TerraformStateExtractor(working_dir=working_dir)
    desired_resources = extractor.extract()

    if not desired_resources:
        return {"findings": [], "summary": "No managed resources found in Terraform state."}

    fetcher = LiveStateFetcher(region=region)

    drifts = []
    skipped = []
    for resource in desired_resources:
        try:
            live = fetcher.fetch(resource.address, resource.resource_type, resource.attributes)
        except NotImplementedError:
            # Resource type has no fetcher registered yet - skip rather than
            # crash the whole run. Surfaced separately so it's not silently lost.
            skipped.append(resource.address)
            continue

        drifts.append(diff_resource(resource, live))

    result = triage_drifts(drifts, output_format=output_format)
    if skipped:
        result["skipped"] = skipped

    return result


def print_report(result: dict) -> None:
    print("\n=== Drift Detection Report ===\n")
    print(result["summary"])
    print()

    if not result["findings"]:
        print("No actionable drift found.\n")
    else:
        for finding in result["findings"]:
            field_info = f".{finding['field']}" if finding.get("field") else " (resource deleted)"
            print(f"  [{finding.get('likely_cause', 'unknown cause')}] {finding['address']}{field_info}")
            print(f"    {finding['explanation']}\n")

    if result.get("skipped"):
        print(f"Skipped (no fetcher registered): {', '.join(result['skipped'])}\n")


def post_to_slack(result: dict, webhook_url: str) -> None:
    import urllib.request

    lines = ["*Drift Detection Report*", result["summary"], ""]
    for finding in result["findings"]:
        field_info = f".{finding['field']}" if finding.get("field") else " (deleted)"
        lines.append(f"\u2022 `{finding['address']}{field_info}` \u2014 {finding['explanation']}")

    payload = json.dumps({"text": "\n".join(lines)}).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Terraform drift detector with Claude-powered triage")
    parser.add_argument("--working-dir", default=".", help="Path to Terraform working directory")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument(
        "--format", choices=["engineer", "slack"], default="engineer",
        help="Triage output style: 'engineer' for direct technical explanation, 'slack' for team-channel tone",
    )
    parser.add_argument("--slack-webhook", default=None, help="Slack incoming webhook URL to auto-post the report")
    args = parser.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        from dotenv import load_dotenv
        load_dotenv()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set (checked .env too).", file=sys.stderr)
        sys.exit(1)

    result = run(args.working_dir, args.region, args.format)
    print_report(result)

    if args.slack_webhook:
        post_to_slack(result, args.slack_webhook)
