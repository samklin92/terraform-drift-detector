"""
triage.py

Takes structural drift output and uses Claude to:
  1. Explain each drift in plain English (what changed, plausible cause)
  2. Risk-rank findings (security-relevant drift surfaces first)
  3. Produce a narrative summary in one of two formats:
       - "engineer": direct technical explanation for personal review
       - "slack": team-facing, ready to paste into a channel

We deliberately don't ask Claude to *decide* severity from scratch - that's
already determined deterministically in differ.py via COMPARABLE_FIELDS.
Mixing "what changed" (deterministic) with "how risky is this" (deterministic,
rule-based) and "why might this have happened / what should you do" (genuinely
needs language reasoning) keeps the system debuggable: if a finding is
mis-ranked, that's a code bug you can fix; if the narrative explanation is
unclear, that's a prompt issue. Conflating the two makes failures hard to
diagnose months from now.
"""

import json
import os

import anthropic
from dotenv import load_dotenv

from differ import DriftSeverity, ResourceDrift

load_dotenv()

MODEL = "claude-sonnet-4-6"

SEVERITY_ORDER = {
    DriftSeverity.SECURITY: 0,
    DriftSeverity.STRUCTURAL: 1,
    DriftSeverity.COSMETIC: 2,
}

ENGINEER_SYSTEM_PROMPT = """You are a drift-triage assistant for a DevOps engineer reviewing \
infrastructure drift between Terraform-managed state and live AWS state.

You will be given a list of detected drifts. For each one, write a one-sentence \
plain-English explanation of what changed and the single most plausible cause \
(e.g. manual console edit, another automation tool, an auto-scaling action). \
Do not hedge with multiple possible causes unless genuinely ambiguous - pick the \
most likely one and say so. Be direct and technical - this is for the engineer \
who owns the infrastructure, not a general audience.

Then write a 2-3 sentence overall summary prioritizing what the engineer should \
look at first.

Respond ONLY with JSON in this exact shape, no markdown fences, no preamble:
{
  "findings": [
    {"address": "...", "field": "...", "explanation": "...", "likely_cause": "..."}
  ],
  "summary": "..."
}
"""

SLACK_SYSTEM_PROMPT = """You are a drift-triage assistant posting infrastructure drift \
findings to a team Slack channel. Your audience includes engineers who may not have \
deep context on this specific resource, so explanations should be clear without being \
condescending. Use a calm, factual tone - drift findings can be alarming, but \
overly dramatic language erodes trust in the alerts over time.

For each drift, write a one-sentence explanation of what changed and the most \
plausible cause. Then write a 2-3 sentence summary suitable as the opening of a \
Slack message, prioritizing what needs attention first.

Respond ONLY with JSON in this exact shape, no markdown fences, no preamble:
{
  "findings": [
    {"address": "...", "field": "...", "explanation": "...", "likely_cause": "..."}
  ],
  "summary": "..."
}
"""

PROMPTS = {
    "engineer": ENGINEER_SYSTEM_PROMPT,
    "slack": SLACK_SYSTEM_PROMPT,
}


def triage_drifts(drifts: list[ResourceDrift], output_format: str = "engineer") -> dict:
    """
    Returns the structured triage result, or a deterministic fallback
    if there's nothing to triage (avoids a pointless API call).
    """
    if output_format not in PROMPTS:
        raise ValueError(f"Unknown output_format={output_format!r}. Use 'engineer' or 'slack'.")

    actionable = [d for d in drifts if d.has_drift]

    if not actionable:
        return {"findings": [], "summary": "No drift detected. All resources match desired state."}

    payload = _build_payload(actionable)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=PROMPTS[output_format],
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )

    raw_text = response.content[0].text
    result = _parse_json_response(raw_text)
    result["findings"] = _sort_by_severity(result["findings"], actionable)

    return result


def _build_payload(drifts: list[ResourceDrift]) -> list[dict]:
    payload = []
    for drift in drifts:
        if drift.deleted:
            payload.append(
                {
                    "address": drift.address,
                    "resource_type": drift.resource_type,
                    "deleted": True,
                }
            )
            continue

        for fd in drift.field_drifts:
            payload.append(
                {
                    "address": drift.address,
                    "resource_type": drift.resource_type,
                    "field": fd.field_name,
                    "severity": fd.severity.value,
                    "desired": fd.desired,
                    "actual": fd.actual,
                }
            )
    return payload


def _parse_json_response(raw_text: str) -> dict:
    # Defensive: strip markdown fences if the model adds them despite instructions.
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())


def _sort_by_severity(findings: list[dict], drifts: list[ResourceDrift]) -> list[dict]:
    severity_lookup = {}
    for drift in drifts:
        if drift.deleted:
            severity_lookup[(drift.address, None)] = DriftSeverity.STRUCTURAL
        for fd in drift.field_drifts:
            severity_lookup[(drift.address, fd.field_name)] = fd.severity

    def sort_key(finding):
        sev = severity_lookup.get((finding["address"], finding.get("field")), DriftSeverity.COSMETIC)
        return SEVERITY_ORDER[sev]

    return sorted(findings, key=sort_key)
