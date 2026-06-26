# Terraform Drift Detector

An intelligent Terraform drift detection system that compares Terraform’s desired state against live AWS infrastructure and uses Claude to explain, classify, and risk-rank detected drift in plain English.

Unlike `terraform plan`, which only reports that drift exists, this tool determines:

* how serious the drift is,
* whether it represents a security risk or harmless noise,
* what likely caused it,
* and what action an engineer should take next.

The output is designed for both:

* individual infrastructure review,
* and team-facing operational reporting such as Slack notifications.

---

# Why this exists

Terraform can detect infrastructure drift.

But it does not help engineers prioritize it.

For example:

```bash
terraform plan -detailed-exitcode
```

might produce dozens of changes across:

* tags,
* security groups,
* instance configuration,
* or bucket settings.

In real production environments, that quickly becomes operational noise.

Over time, engineers begin ignoring drift reports entirely — which defeats the purpose of drift detection.

This project focuses on triage rather than raw diff output.

The goal is to surface the findings that actually matter:

* security exposure,
* unintended infrastructure modification,
* or structural configuration drift,

while suppressing cosmetic noise.

---

# Technical highlights

* Real Terraform state vs live AWS infrastructure comparison
* Deterministic structural diff engine
* Severity classification with security-aware prioritization
* Claude-powered human-readable explanation layer
* Slack-ready reporting output
* Noise suppression for AWS/Terraform schema inconsistencies
* Verified against live AWS infrastructure instead of mocked responses
* Clear separation between deterministic logic and AI reasoning

---

# Architecture

```text
Terraform State
(terraform show -json)
            |
            v
+-------------------------+
| state_extractor.py      |
| Normalize TF resources  |
+-------------------------+
            |
            v

                    +--------------------------+
                    | live_state_fetcher.py    |
                    | Query AWS via boto3      |
                    +--------------------------+
                                ^
                                |
                                |
                    AWS Live Infrastructure
                    (Security Groups, EC2, S3)

            +-----------------------------------+
            |
            v

+-----------------------------------------------+
| differ.py                                     |
| Structural diff engine                        |
| - Drift detection                             |
| - Severity classification                     |
| - Noise suppression                           |
+-----------------------------------------------+
            |
            v

+-----------------------------------------------+
| triage.py                                     |
| Claude reasoning layer                        |
| - Explain findings                            |
| - Risk-rank drift                             |
| - Suggest remediation                         |
| - Generate Slack-friendly summaries           |
+-----------------------------------------------+
            |
            v

+-----------------------------------------------+
| main.py                                       |
| CLI orchestrator                              |
| stdout / Slack webhook output                 |
+-----------------------------------------------+
```

---

# Design principle

## Separate deterministic logic from AI reasoning

This project intentionally separates:

### Deterministic infrastructure analysis

Handled by rule-based, testable code.

Includes:

* resource comparison,
* structural diffing,
* normalization,
* and severity classification.

If drift is detected incorrectly, that is a code bug.

This logic lives primarily in:

```text
differ.py
```

and the resource comparison rules defined in:

```text
COMPARABLE_FIELDS
```

---

### AI-powered reasoning and explanation

Handled by Claude.

Includes:

* explaining findings in plain English,
* generating likely causes,
* summarizing operational risk,
* and producing human-friendly remediation guidance.

If an explanation is unclear, that is a prompt/design issue rather than a diff-engine issue.

Keeping these responsibilities separate makes the system debuggable and operationally trustworthy.

---

# Resource types currently supported

## `aws_security_group`

Tracks drift in:

* ingress rules
* egress rules
* tags

---

## `aws_instance`

Tracks drift in:

* AMI
* instance type
* security group attachments
* tags

---

## `aws_s3_bucket`

Tracks drift in:

* versioning configuration
* tags

---

Adding support for a new AWS resource type requires only:

1. one live-state fetcher in:

```text
live_state_fetcher.py
```

2. one comparison rule entry in:

```text
COMPARABLE_FIELDS
```

No other architectural changes are required.

---

# Real bugs discovered during verification

This system was tested continuously against a real AWS sandbox environment rather than only unit-tested against static mocks.

That verification process uncovered several real operational edge cases.

---

## 1. Missing egress rule detection

The original implementation only inspected:

```python
IpPermissions
```

which represents ingress rules.

Egress drift was completely invisible.

That meant dangerous outbound access changes could occur without detection.

### Fix

Added explicit support for:

```python
IpPermissionsEgress
```

and normalized both ingress and egress comparison paths.

---

## 2. AWS vs Terraform normalization mismatch for “allow all traffic”

AWS omits:

* `FromPort`
* `ToPort`

when protocol is:

```text
-1
```

(all protocols).

Terraform represents the same rule as:

```text
0 / 0
```

Without normalization, every unrestricted rule falsely appeared as drift on every run.

### Fix

Added protocol-aware normalization before structural comparison.

---

## 3. Schema mismatch between Terraform and AWS resource shapes

Terraform includes additional rule fields such as:

* `description`
* `ipv6_cidr_blocks`
* `self`

that were not present in normalized AWS responses.

Direct comparison produced constant false positives.

### Fix

Implemented schema-aware field filtering using explicit comparable-field definitions.

---

## 4. Order-sensitive sorting bug

The original noise-suppression logic sorted lists using:

```python
str(dict)
```

which depends on dictionary insertion order rather than actual content.

Functionally identical rules with different key ordering were inconsistently sorted and falsely flagged as drift.

### Fix

Implemented deterministic canonical sorting based on normalized content.

---

# Example real-world verification

The tool was validated end-to-end against a deliberately introduced AWS security group misconfiguration:

```text
RDP (3389) open to 0.0.0.0/0
```

Verification confirmed:

* successful drift detection,
* correct severity classification,
* accurate explanation,
* and actionable remediation guidance.

This testing was performed against live AWS infrastructure rather than synthetic mock data.

---

# Why this matters

Infrastructure drift is one of the most dangerous forms of silent operational risk.

Because Terraform only reflects desired state, engineers may assume infrastructure remains compliant long after manual changes occur.

Examples include:

* temporarily opened firewall rules,
* modified EC2 instance types,
* disabled S3 versioning,
* or manual production hotfixes never committed back to Terraform.

The challenge is not merely detecting drift.

The challenge is identifying which drift matters.

This project focuses on operational signal rather than operational noise.

---

# Example output

## Technical mode

```text
[HIGH] aws_security_group.prod-rdp

Detected drift:
- Ingress rule added:
  TCP/3389 from 0.0.0.0/0

Risk:
This exposes Remote Desktop Protocol access publicly to the internet.

Likely cause:
Temporary troubleshooting access manually added through the AWS Console.

Recommended action:
Restrict RDP access to approved administrative CIDR ranges immediately.
```

---

## Slack mode

```text
🚨 High-risk infrastructure drift detected

Resource:
aws_security_group.prod-rdp

Issue:
RDP access is publicly exposed to the internet (0.0.0.0/0).

Likely cause:
Manual AWS Console modification outside Terraform workflow.

Recommended action:
Restrict ingress access and reconcile Terraform state.
```

---

# Setup

Install dependencies:

```bash
pip install boto3 anthropic python-dotenv
```

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=your-api-key
```

AWS credentials are automatically loaded from:

* AWS CLI configuration
* environment variables
* IAM roles

No separate authentication setup is required.

---

# Usage

## Direct technical review

```bash
python drift_engine/main.py \
  --working-dir path/to/terraform \
  --region us-east-1
```

---

## Slack-friendly reporting output

```bash
python drift_engine/main.py \
  --working-dir path/to/terraform \
  --region us-east-1 \
  --format slack
```

---

## Auto-post findings to Slack

```bash
python drift_engine/main.py \
  --working-dir path/to/terraform \
  --format slack \
  --slack-webhook https://hooks.slack.com/...
```

---

# Project structure

```text
drift_engine/
├── state_extractor.py
├── live_state_fetcher.py
├── differ.py
├── triage.py
└── main.py
```

## Components

### `state_extractor.py`

Parses:

```bash
terraform show -json
```

into normalized Terraform resource representations.

---

### `live_state_fetcher.py`

Queries AWS directly using boto3.

Contains one fetcher implementation per supported resource type.

---

### `differ.py`

Core structural diff engine responsible for:

* drift detection,
* severity classification,
* normalization,
* and noise suppression.

---

### `triage.py`

Claude-powered reasoning layer responsible for:

* explanation,
* risk ranking,
* remediation guidance,
* and human-readable summaries.

---

### `main.py`

CLI orchestrator handling:

* execution flow,
* formatting,
* stdout rendering,
* and Slack webhook delivery.

---

# Cost

Claude is only invoked when actual drift is detected.

Clean runs incur only AWS API calls, which are effectively free for standard `describe_*` operations.

For moderate-sized environments scanned hourly, expected Anthropic API costs remain in the low single-digit dollar range per month.

---

# Safety and operational boundaries

This tool intentionally treats AI explanations as advisory rather than authoritative.

The AI layer:

* does not mutate infrastructure,
* does not apply Terraform changes,
* and does not make deployment decisions.

It explains and prioritizes findings for engineers.

Final operational decisions remain human-controlled.

---

# Limitations

* `likely_cause` explanations are inferred hypotheses rather than audited facts
* No CloudTrail correlation currently exists
* Resource coverage is intentionally limited to a small verified set
* Drift history persistence is not yet implemented
* Multi-account aggregation is not yet supported

---

# Future improvements

Planned extensions include:

* CloudTrail correlation
* historical drift tracking
* multi-account AWS support
* IAM policy drift analysis
* Terraform Cloud integration
* GitHub Actions scheduled scanning
* automatic ticket generation
* configurable severity policies
* drift trend analytics
* OpenSearch/SIEM integration

---

# Key takeaway

This project is not simply a Terraform wrapper.

It is an intelligent infrastructure drift triage system designed to answer:

* What changed?
* How dangerous is it?
* Why might it have happened?
* What should an engineer do next?

The focus is not raw infrastructure diffs.

The focus is operational clarity.

  
