"""THROWAWAY (spec §8 step 0): prove Claude Platform on AWS serves our models in a region.

Run with operator AWS credentials in the environment:
    AWS_PROFILE=... python devtools/intel_access_probe.py --region ap-south-1 --workspace <id>
Prints what answered and what it cost.

NOT YET RUN (2026-09-28): controller ruling for Task 0 forbade calling AWS from this session —
the workspace ids do not exist in anything readable here, the prod account is off-limits, and the
calls cost money. This file stays committed until the owner runs it against both the dev
(ap-south-1) and prod (us-east-1) workspaces and the findings file's PENDING rows are filled in
from the output. Delete this file only after that — see
docs/superpowers/specs/2026-09-28-likeness-intel-step0-findings.md.
"""

from __future__ import annotations

import argparse
import json

import anthropic

MODELS = ("claude-sonnet-5", "claude-opus-5-5")


def probe(region: str, workspace: str, web_search_type: str) -> None:
    client = anthropic.AnthropicAWS(aws_region=region, workspace_id=workspace)
    for model in MODELS:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=64,
                messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            )
            print(
                json.dumps(
                    {
                        "model": model,
                        "answered_by": response.model,
                        "stop_reason": response.stop_reason,
                        "usage": response.usage.model_dump(),
                    }
                )
            )
        except anthropic.APIStatusError as exc:
            print(json.dumps({"model": model, "status": exc.status_code, "type": exc.type}))
    response = client.messages.create(
        model=MODELS[0],
        max_tokens=512,
        tools=[{"type": web_search_type, "name": "web_search", "max_uses": 1}],
        messages=[
            {
                "role": "user",
                "content": "Find one news article about a social platform "
                "changing its privacy policy. Reply with its URL only.",
            }
        ],
    )
    print(
        json.dumps(
            {
                "web_search": True,
                "stop_reason": response.stop_reason,
                "usage": response.usage.model_dump(),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--web-search-type", required=True)  # e.g. the 20260209 variant
    args = parser.parse_args()
    probe(args.region, args.workspace, args.web_search_type)
