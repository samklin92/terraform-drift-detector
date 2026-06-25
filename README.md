# Terraform Drift Detector

A drift detection tool that compares Terraform's desired state against live AWS
state, and uses Claude to explain and risk-rank any findings in plain English.

Unlike `terraform plan`, which tells you *that* something drifted, this tool
classifies *how serious* the drift is (security / structural / cosmetic) and
generates a human-readable explanation - including a plausible cause and a
recommended next step - suitable for either a personal review or a Slack
channel post.

## Why this exists

`terraform plan -detailed-exitcode` will tell you a resource has drifted, but
it doesn't distinguish a tag change from an open security group rule. In a
real environment with dozens of resources, that's a wall of noise an engineer
will eventually start ignoring - which defeats the purpose of drift detection
entirely. This tool's value-add is triage: surfacing the two or three findings
that actually matter and explaining them, rather than dumping a raw diff.

## Architecture
ANTHROPIC_API_KEY=your-key-here
AWS credentials are picked up from your standard AWS CLI configuration
(`~/.aws/credentials` or environment variables) - no separate setup needed.

## Usage

```bash
# Direct technical output for personal review
python drift_engine/main.py --working-dir path/to/terraform --region us-east-1

# Team-facing tone, suitable for a Slack channel
python drift_engine/main.py --working-dir path/to/terraform --region us-east-1 --format slack

# Auto-post the report to a Slack channel via webhook
python drift_engine/main.py --working-dir path/to/terraform --format slack --slack-webhook https://hooks.slack.com/...
```

## Project structure
drift_engine/

|-- state_extractor.py      # Parses terraform show -json into normalized resources

|-- live_state_fetcher.py   # Queries AWS directly via boto3, one fetcher per resource type

|-- differ.py                # Structural diff with severity classification and noise suppression

|-- triage.py                 # Claude-powered explanation and risk-ranking (engineer/slack modes)

`-- main.py                   # CLI orchestrator
## Cost

Claude is only called when actual drift is detected - a clean run costs
nothing beyond the AWS API calls (which are free for `describe_*` operations).
For a moderate-sized environment polled hourly, expect low single-digit
dollars per month in Anthropic API usage even with frequent findings.

## Limitations

- Claude's `likely_cause` field is an inference, not a finding - it's a
  plausible explanation based on the type of change, not evidence from
  CloudTrail or any audit log. Treat it as a starting hypothesis to verify,
  not a conclusion.
- Resource type coverage is currently limited to the three types listed
  above. Extending coverage is a small, additive change per resource type.
