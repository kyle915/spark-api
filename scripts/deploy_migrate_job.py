#!/usr/bin/env python3
"""Clone spark-api-new's new revision into Cloud Run Job spark-api-migrate.

Runs `entrypoint.sh migrate-only` (Django migrate + Girl Beer template
repair) once per deploy so request-serving containers can skip both on
boot. Invoked from .github/workflows/deploy-cloud-run.yml.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT = "spark-479222"
REGION = "us-central1"
SERVICE = "spark-api-new"
JOB = "spark-api-migrate"
RUNTIME_SA = "spark-api-new-sa@spark-479222.iam.gserviceaccount.com"


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
        print(
            "::error::Could not resolve Cloud SQL instances for the migrate job.",
            file=sys.stderr,
        )
        return 1

    env_file = Path("/tmp/spark-api-migrate.env.yaml")
    # YAML env-vars-file: KEY: "value" — avoids comma-in-value breakage.
    lines = []
    for pair in env_pairs:
        key, _, value = pair.partition("=")
        dumped = json.dumps(value)
        lines.append(f"{key}: {dumped}\n")
    env_file.write_text("".join(lines) if lines else "")

    cmd = [
        "gcloud",
        "run",
        "jobs",
        "deploy",
        JOB,
        "--image",
        image,
        "--region",
        REGION,
        "--project",
        PROJECT,
        "--service-account",
        RUNTIME_SA,
        "--command",
        "/code/scripts/entrypoint.sh",
        "--args",
        "migrate-only",
        "--max-retries",
        "1",
        "--task-timeout",
        "15m",
        "--cpu",
        "1",
        "--memory",
        "1Gi",
        "--set-cloudsql-instances",
        cloudsql,
        "--execute-now",
        "--wait",
        "--quiet",
    ]
    if lines:
        cmd += ["--env-vars-file", str(env_file)]
    if secret_pairs:
        cmd += ["--set-secrets", ",".join(secret_pairs)]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
