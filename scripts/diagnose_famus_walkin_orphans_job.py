#!/usr/bin/env python3
"""One-shot Cloud Run Job: list Famus filed walk-up orphans (read-only).

Clones the live spark-api-new revision image/env/secrets/Cloud SQL, then
runs Django shell diagnose code. Invoked from the matching workflow.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

PROJECT = "spark-479222"
REGION = "us-central1"
SERVICE = "spark-api-new"
JOB = "spark-api-diagnose-walkin"
RUNTIME_SA = "spark-api-new-sa@spark-479222.iam.gserviceaccount.com"

DIAGNOSE_PY = r'''
import json
from datetime import timedelta
from django.utils import timezone as djtz
from ambassadors.checkin_web import event_calendar_date
from events.models import Event, Request
from events.walkin_link import has_filed_recap, _orphan_matches_request
from tenants.models import Tenant
import os
days = int(os.environ.get("DAYS_BACK", "45"))
tenant = (
    Tenant.objects.filter(slug="famus-energy").first()
    or Tenant.objects.filter(request_url_name="famus-energy").first()
)
assert tenant, "no famus tenant"
lo = djtz.now() - timedelta(days=max(1, days))
orphans = list(
    Event.objects.filter(
        tenant=tenant,
        request__isnull=True,
        date__gte=lo,
        co_activation_host__isnull=True,
    )
    .select_related("retailer")
    .order_by("id")
)
orphans = [e for e in orphans if has_filed_recap(e.id)]
due = list(
    Request.objects.filter(
        tenant=tenant, deleted_at__isnull=True, date__gte=lo
    )
    .select_related("retailer")
    .prefetch_related("event_set__retailer")
    .order_by("id")
)
due_open = []
for req in due:
    shells = list(req.event_set.all())
    if not shells:
        continue
    if any(has_filed_recap(ev.id) for ev in shells):
        continue
    day = None
    for ev in shells:
        day = event_calendar_date(ev) or day
    if day is None and req.date is not None:
        rd = req.date
        day = rd.date() if hasattr(rd, "date") else rd
    due_open.append((req, shells, day))
out = []
for o in orphans:
    oday = event_calendar_date(o)
    same_day = []
    matched = []
    for req, shells, day in due_open:
        if day is not None and oday is not None and day != oday:
            continue
        entry = {
            "request_id": req.id,
            "name": req.name,
            "address": req.address or "",
            "day": day.isoformat() if day else None,
        }
        same_day.append(entry)
        if _orphan_matches_request(o, req, shells):
            matched.append(entry)
    if not same_day:
        reason = "no-same-day-due"
    elif matched:
        reason = "place-match-present"
    else:
        reason = "same-day-no-place-match"
    out.append(
        {
            "event_id": o.id,
            "name": o.name,
            "address": o.address or "",
            "day": oday.isoformat() if oday else None,
            "retailer": getattr(getattr(o, "retailer", None), "name", None),
            "reason": reason,
            "matched_due": matched,
            "same_day_due_count": len(same_day),
            "same_day_due": same_day[:15],
        }
    )
print(
    "JSON_ORPHANS:"
    + json.dumps(
        {
            "tenant": "famus-energy",
            "days_back": days,
            "orphans_with_filed": len(out),
            "orphans": out,
        },
        default=str,
    )
)
'''


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
    days_back = (sys.argv[1] if len(sys.argv) > 1 else "45").strip() or "45"
    rev = gcloud(
        "run",
        "services",
        "describe",
        SERVICE,
        "--region",
        REGION,
        "--format=value(status.latestCreatedRevisionName)",
    ).strip()
    print(f"revision={rev}", flush=True)
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
    env_pairs.append(f"DAYS_BACK={days_back}")

    svc = gcloud(
        "run", "services", "describe", SERVICE, "--region", REGION, json_out=True
    )
    cloudsql = cloudsql_from(spec) or cloudsql_from(svc)
    if not cloudsql:
        print("::error::Could not resolve Cloud SQL instances.", file=sys.stderr)
        return 1

    env_file = Path("/tmp/spark-api-diagnose.env.yaml")
    lines = []
    for pair in env_pairs:
        key, _, value = pair.partition("=")
        lines.append(f"{key}: {json.dumps(value)}\n")
    env_file.write_text("".join(lines))

    b64 = base64.b64encode(DIAGNOSE_PY.encode("utf-8")).decode("ascii")
    shell_c = (
        f"exec(__import__('base64').b64decode({b64!r}).decode())"
    )
    # Cloud Run --args is comma-separated; keep shell -c as a single arg
    # by using the args list form via gcloud's repeated flag isn't available,
    # so use a tiny wrapper script path that already exists: manage.py shell.
    args = f"run,python,manage.py,shell,-c,{shell_c}"

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
        "uv",
        "--args",
        args,
        "--max-retries",
        "0",
        "--task-timeout",
        "10m",
        "--cpu",
        "1",
        "--memory",
        "1Gi",
        "--set-cloudsql-instances",
        cloudsql,
        "--env-vars-file",
        str(env_file),
        "--execute-now",
        "--wait",
        "--quiet",
    ]
    if secret_pairs:
        cmd += ["--set-secrets", ",".join(secret_pairs)]
    print("+", " ".join(cmd[:14]), "...", flush=True)
    subprocess.run(cmd, check=True)

    print("===== fetching execution logs =====", flush=True)
    # Prefer job execution logs API (deploy SA often lacks logging.read).
    exe = gcloud(
        "run",
        "jobs",
        "executions",
        "list",
        f"--job={JOB}",
        f"--region={REGION}",
        "--limit=1",
        "--format=value(name)",
    ).strip()
    print(f"execution={exe}", flush=True)
    proc = subprocess.run(
        [
            "gcloud",
            "run",
            "jobs",
            "executions",
            "logs",
            "read",
            exe,
            f"--region={REGION}",
            f"--project={PROJECT}",
        ],
        capture_output=True,
        text=True,
    )
    logs = (proc.stdout or "") + (proc.stderr or "")
    print(logs)
    if proc.returncode != 0:
        print(f"::warning::execution logs read rc={proc.returncode}", flush=True)
    for line in logs.splitlines():
        if "JSON_ORPHANS:" in line:
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
