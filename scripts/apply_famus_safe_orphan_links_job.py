#!/usr/bin/env python3
"""One-shot: apply high-confidence Famus orphan→request links (never remint)."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

PROJECT = "spark-479222"
REGION = "us-central1"
SERVICE = "spark-api-new"
JOB = "spark-api-apply-walkin-links"
RUNTIME_SA = "spark-api-new-sa@spark-479222.iam.gserviceaccount.com"

# (orphan_event_id, request_id) — reviewed as unique same-day place matches
# where addresses_fuzzy_match failed on KY/ky or Highway/Blvd noise.
PAIRS = [(6840, 5337), (7013, 5347), (7034, 5323)]

APPLY_PY = f"""
from events.models import Event
pairs = {PAIRS!r}
out = []
for eid, rid in pairs:
    n = Event.objects.filter(pk=eid, request__isnull=True).update(request_id=rid)
    out.append({{"event_id": eid, "request_id": rid, "updated": int(n)}})
print("JSON_APPLY:" + __import__("json").dumps({{"pairs": out}}))
"""


def gcloud(*args: str, json_out: bool = False):
    cmd = ["gcloud", *args, "--project", PROJECT]
    if json_out:
        cmd.append("--format=json")
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    if json_out:
        return json.loads(proc.stdout or "null")
    return proc.stdout


def cloudsql_from(spec: dict) -> str:
    meta = spec.get("metadata") or {}
    annotations = meta.get("annotations") or {}
    found = annotations.get("run.googleapis.com/cloudsql-instances") or ""
    if found:
        return found
    template = (spec.get("spec") or {}).get("template") or {}
    t_ann = (template.get("metadata") or {}).get("annotations") or {}
    return t_ann.get("run.googleapis.com/cloudsql-instances") or ""


def main() -> int:
    rev = gcloud(
        "run",
        "services",
        "describe",
        SERVICE,
        "--region",
        REGION,
        "--format=value(status.latestCreatedRevisionName)",
    ).strip()
    spec = gcloud(
        "run", "revisions", "describe", rev, "--region", REGION, json_out=True
    )
    container = spec["spec"]["containers"][0]
    image = container["image"]
    env_pairs: list[str] = []
    secret_pairs: list[str] = []
    for item in container.get("env") or []:
        name = item["name"]
        if "valueFrom" in item:
            ref = item["valueFrom"]["secretKeyRef"]
            secret_pairs.append(
                f"{name}={ref['name']}:{ref.get('version', 'latest')}"
            )
        elif "value" in item:
            env_pairs.append(f"{name}={item['value']}")
    svc = gcloud(
        "run", "services", "describe", SERVICE, "--region", REGION, json_out=True
    )
    cloudsql = cloudsql_from(spec) or cloudsql_from(svc)
    if not cloudsql:
        print("::error::No Cloud SQL", file=sys.stderr)
        return 1
    env_file = Path("/tmp/spark-api-apply.env.yaml")
    lines = []
    for pair in env_pairs:
        key, _, value = pair.partition("=")
        lines.append(f"{key}: {json.dumps(value)}\n")
    env_file.write_text("".join(lines) if lines else "")
    b64 = base64.b64encode(APPLY_PY.encode()).decode("ascii")
    shell_c = f"exec(__import__('base64').b64decode({b64!r}).decode())"
    args = f"run,python,manage.py,shell,-c,{shell_c}"
    cmd = [
        "gcloud", "run", "jobs", "deploy", JOB,
        "--image", image,
        "--region", REGION,
        "--project", PROJECT,
        "--service-account", RUNTIME_SA,
        "--command", "uv",
        "--args", args,
        "--max-retries", "0",
        "--task-timeout", "10m",
        "--cpu", "1",
        "--memory", "1Gi",
        "--set-cloudsql-instances", cloudsql,
        "--env-vars-file", str(env_file),
        "--execute-now", "--wait", "--quiet",
    ]
    if secret_pairs:
        cmd += ["--set-secrets", ",".join(secret_pairs)]
    print("+", " ".join(cmd[:12]), "...", flush=True)
    subprocess.run(cmd, check=True)
    print("APPLY_JOB_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
