"""Create the Bedrock Guardrail the wire-facing agents run with, and write its id to data/guardrail.json.

  AWS_REGION=us-east-1 .venv/bin/python scripts/create_guardrail.py

Health and personal-finance topics are denied; email, phone and address are anonymised; card and
government numbers are blocked. Applied to the model whose output goes over A2A (cahoots/network.py).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    br = boto3.client("bedrock", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    existing = [g for g in br.list_guardrails().get("guardrails", []) if g["name"] == "cahoots-wire"]
    if existing:
        gid = existing[0]["id"]
    else:
        gid = br.create_guardrail(
            name="cahoots-wire",
            description="Cahoots: what a person's agent may put on the wire.",
            topicPolicyConfig={"topicsConfig": [
                {"name": "health-and-medical", "definition": "Statements about a person's health, medical conditions, medication, therapy, allergies or disabilities.",
                 "examples": ["She takes lithium for bipolar II", "He has a severe shellfish allergy", "Her therapist said"], "type": "DENY"},
                {"name": "personal-finances", "definition": "A person's income, debt, salary, loans or financial trouble beyond a rough outing budget.",
                 "examples": ["He earns $48k and has $19k of debt", "She is behind on rent"], "type": "DENY"},
            ]},
            sensitiveInformationPolicyConfig={"piiEntitiesConfig": [
                {"type": "EMAIL", "action": "ANONYMIZE"}, {"type": "PHONE", "action": "ANONYMIZE"},
                {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCK"}, {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
                {"type": "CA_SOCIAL_INSURANCE_NUMBER", "action": "BLOCK"},
            ]},
            blockedInputMessaging="Cahoots agents only discuss outings and cards.",
            blockedOutputsMessaging="[withheld by guardrail]",
        )["guardrailId"]
    for _ in range(30):
        if br.get_guardrail(guardrailIdentifier=gid, guardrailVersion="DRAFT")["status"] == "READY":
            break
        time.sleep(3)
    published = [v["version"] for v in br.list_guardrails(guardrailIdentifier=gid).get("guardrails", []) if v["version"] != "DRAFT"]
    ver = sorted(published, key=int)[-1] if published else br.create_guardrail_version(guardrailIdentifier=gid, description="v1")["version"]
    (ROOT / "data" / "guardrail.json").write_text(json.dumps({"guardrail_id": gid, "guardrail_version": ver}, indent=2) + "\n")
    print(gid, ver)


if __name__ == "__main__":
    main()
