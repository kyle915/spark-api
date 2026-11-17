"""Read-only inventory for making the media bucket private.

    python manage.py private_media_audit                 # GCS counts per prefix
    python manage.py private_media_audit --db            # + DB references per prefix
    python manage.py private_media_audit --runbook       # print the flip runbook only
    python manage.py private_media_audit --prefix recaps/ --max-objects 50000

Never writes: no GCS mutation, no DB write, no IAM change. It lists object
names (metadata only, never bytes) and prints the exact gcloud commands Kyle
runs to flip the bucket, in order, with rollback.

Report columns:
  * class      — "public" (catalog: products/, tenants/images/,
                 receipt-campaign-images/ and their thumbs) or "private".
  * objects / bytes per top-level prefix (two levels for recaps/ + thumbs/).
  * guessable  — private objects whose file name is NOT a uuid4 hex. #1424
                 made new uploads uuid4-named; older keys (timestamps,
                 original filenames) are the ones a public bucket leaks.
  * move?      — single-bucket plan (recommended): nothing moves. The
                 split-bucket alternative copies the PUBLIC rows only.
"""

from __future__ import annotations

import re
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand

from utils.private_media import PUBLIC_PREFIXES, is_public_blob

_UUID_NAME = re.compile(r"(^|[^0-9a-f])[0-9a-f]{32}(\.[A-Za-z0-9]{1,10})?$")


def _group(key: str) -> str:
    parts = key.split("/")
    if parts[0] in ("recaps", "thumbs", "exports", "custom-recaps") and len(parts) > 2:
        return "/".join(parts[:2]) + "/"
    return parts[0] + "/" if len(parts) > 1 else "(root)"


RUNBOOK = r"""
PRIVATE MEDIA FLIP RUNBOOK  (bucket = $BUCKET, Cloud Run service = spark-api-new)
Nothing below has been run. Every step is reversible until step 6.

0. Look first (read-only)
   BUCKET=$(gcloud run services describe spark-api-new --region us-central1 \
     --format='value(spec.template.spec.containers[0].env)' | tr ';' '\n' \
     | sed -n "s/.*'name': 'GS_BUCKET_NAME', 'value': '\([^']*\)'.*/\1/p")
   SA=$(gcloud run services describe spark-api-new --region us-central1 \
     --format='value(spec.template.spec.serviceAccountName)')
   gcloud storage buckets get-iam-policy gs://$BUCKET --format=json   # expect allUsers: roles/storage.objectViewer
   gcloud storage buckets describe gs://$BUCKET --format="yaml(uniform_bucket_level_access,cors_config,public_access_prevention)"
   gcloud iam service-accounts get-iam-policy $SA --format=json        # SA needs roles/iam.serviceAccountTokenCreator on itself (signBlob)
   python manage.py private_media_audit --db                           # counts per prefix

1. Merge + deploy the code (flag OFF). No behaviour change.

2. Turn the flag on while the bucket is STILL public (safe: old public URLs keep working)
   gcloud run services update spark-api-new --region us-central1 \
     --update-env-vars PRIVATE_MEDIA_ENABLED=true
   (optional) dedicated keyring instead of the SECRET_KEY-derived default:
     --update-secrets PRIVATE_MEDIA_TOKEN_KEYS=private-media-token-keys:latest
   Verify: recaps list/detail, a custom recap, a web check-in, /r/<share>, BA
   profile photos on mobile, chat attachments, documents, a video, thumbnails.
   Every private image URL should be .../api/public/media/o/<token>/<name> and
   302 to storage.googleapis.com/...?X-Goog-Signature=...

3. Re-issue durable links that were already handed out as raw public URLs
   (these WILL 403 after step 6): re-run export_recap_fields / the receipts
   CSV + PDF exports for anything a client still uses. Daily recap sheet export
   and the LD sync never write photo URLs (image fields are excluded) — no action.

4. Bucket CORS for fetch() after a redirect (zip downloads): the browser sends
   Origin: null on the cross-origin hop, so GET must allow origin "*":
   cat > /tmp/cors.json <<'JSON'
   [{"origin": ["*"], "method": ["GET", "HEAD"], "responseHeader": ["Content-Type"], "maxAgeSeconds": 3600}]
   JSON
   gcloud storage buckets update gs://$BUCKET --cors-file=/tmp/cors.json
   (keep any existing PUT entry for browser uploads — merge with step-0 output)

5. Stop handing out direct catalog URLs (products/, tenants/images/,
   receipt-campaign-images/) — they get durable tokens instead:
   gcloud run services update spark-api-new --region us-central1 \
     --update-env-vars PRIVATE_MEDIA_DIRECT_PUBLIC_PREFIXES=false
   Wait >= 24h (PRIVATE_MEDIA_URL_WINDOW_HOURS x 2) so no open page still
   holds a direct URL; re-verify step-2 surfaces + public pages (/checkin,
   /c/<slug>, /jobs, /training) + product images.

6. Make the bucket private
   gcloud storage buckets remove-iam-policy-binding gs://$BUCKET \
     --member=allUsers --role=roles/storage.objectViewer
   (optional hardening, after a quiet week)
   gcloud storage buckets update gs://$BUCKET --public-access-prevention

   Verify: curl -sI https://storage.googleapis.com/$BUCKET/<a recaps/ key>  -> 403
           curl -sI <a /api/public/media/o/... URL from the app>             -> 302, then 200

ROLLBACK (any time after 6, takes effect immediately):
   gcloud storage buckets update gs://$BUCKET --no-public-access-prevention   # only if set
   gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
     --member=allUsers --role=roles/storage.objectViewer
   Then, if wanted: --update-env-vars PRIVATE_MEDIA_DIRECT_PUBLIC_PREFIXES=true,PRIVATE_MEDIA_ENABLED=false
   (Already-issued media URLs keep working either way: with the flag off the
   endpoint redirects to the public URL.)

REVOKE every media link (e.g. a leaked durable link): rotate the keyring —
   PRIVATE_MEDIA_TOKEN_KEYS="k2:<new>"  (drop k1). Interactive links re-mint on
   the next page load; durable links in client sheets/CSVs must be re-exported.

ALTERNATIVE (not recommended): split buckets. Create gs://$BUCKET-public with
allUsers:objectViewer, copy the PUBLIC rows of this report
   gcloud storage cp -r gs://$BUCKET/products gs://$BUCKET-public/   (etc.)
and teach upload signing + django-storages to route those prefixes to it.
More moving parts for little gain over step 5.
"""


class Command(BaseCommand):
    help = "Read-only: object counts per prefix + the private-bucket flip runbook."

    def add_arguments(self, parser):
        parser.add_argument("--prefix", default="", help="Only list keys under this prefix.")
        parser.add_argument(
            "--max-objects",
            type=int,
            default=0,
            help="Stop listing after N objects (0 = all).",
        )
        parser.add_argument("--db", action="store_true", help="Also count DB references per prefix.")
        parser.add_argument("--runbook", action="store_true", help="Print the runbook only.")

    def handle(self, *args, **opts):
        if opts["runbook"]:
            self.stdout.write(RUNBOOK)
            return

        self.stdout.write(
            f"private_media_enabled={getattr(settings, 'PRIVATE_MEDIA_ENABLED', False)} "
            f"direct_public_prefixes={getattr(settings, 'PRIVATE_MEDIA_DIRECT_PUBLIC_PREFIXES', True)} "
            f"public_prefixes={','.join(PUBLIC_PREFIXES)}"
        )
        self._gcs(opts["prefix"], opts["max_objects"])
        if opts["db"]:
            self._db()
        self.stdout.write("\nRunbook: python manage.py private_media_audit --runbook")

    def _gcs(self, prefix: str, max_objects: int) -> None:
        bucket_name = getattr(settings, "GS_BUCKET_NAME", "")
        if not bucket_name:
            self.stdout.write(self.style.WARNING("GS_BUCKET_NAME unset — skipping GCS listing."))
            return
        from utils.gcs import get_gcs_client

        client = get_gcs_client()
        stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # objects, bytes, guessable
        seen = 0
        # fields= keeps the listing to names+sizes (metadata only).
        for blob in client.list_blobs(
            bucket_name, prefix=prefix or None, fields="items(name,size),nextPageToken"
        ):
            key = blob.name
            row = stats[_group(key)]
            row[0] += 1
            row[1] += int(blob.size or 0)
            if not is_public_blob(key) and not _UUID_NAME.search(key.rsplit("/", 1)[-1]):
                row[2] += 1
            seen += 1
            if max_objects and seen >= max_objects:
                self.stdout.write(self.style.WARNING(f"Stopped at --max-objects={max_objects}."))
                break

        self.stdout.write(f"\nBucket gs://{bucket_name} ({seen} objects listed)")
        self.stdout.write(
            f"{'prefix':38} {'class':8} {'objects':>9} {'MB':>10} {'guessable':>10}  move? (split plan)"
        )
        tot = [0, 0, 0]
        for group in sorted(stats, key=lambda g: -stats[g][1]):
            n, b, g = stats[group]
            klass = "public" if is_public_blob(group + "x") else "private"
            move = "copy to public bucket" if klass == "public" else "no"
            self.stdout.write(
                f"{group:38} {klass:8} {n:>9} {b / 1e6:>10.1f} {g:>10}  {move}"
            )
            tot = [tot[0] + n, tot[1] + b, tot[2] + g]
        self.stdout.write(f"{'TOTAL':38} {'':8} {tot[0]:>9} {tot[1] / 1e6:>10.1f} {tot[2]:>10}")
        self.stdout.write(
            "Single-bucket plan (recommended): no object moves; flip IAM per runbook."
        )

    def _db(self) -> None:
        """Count stored keys per prefix across the file-bearing columns."""
        from ambassadors.models import Ambassador, AmbassadorPhoto
        from chats.models import ChatMessageAttachment
        from documents.models import AmbassadorDocument
        from events.models import Product
        from recaps.models import CustomRecapFile, RecapFile
        from tenants.models import Tenant, User

        from utils.gcs import extract_blob_name_from_url

        sources = [
            (RecapFile, "file"),
            (CustomRecapFile, "url"),
            (AmbassadorDocument, "file"),
            (ChatMessageAttachment, "file"),
            (AmbassadorPhoto, "image"),
            (Ambassador, "headshot"),
            (Ambassador, "resume"),
            (Product, "image"),
            (Tenant, "image"),
            (User, "image"),
        ]
        self.stdout.write("\nDB references (rows with a stored key)")
        for model, col in sources:
            counts: dict[str, int] = defaultdict(int)
            try:
                qs = model.objects.exclude(**{f"{col}__isnull": True}).exclude(**{col: ""})
                for raw in qs.values_list(col, flat=True).iterator(chunk_size=2000):
                    key = extract_blob_name_from_url(str(raw)) or ""
                    counts[_group(key) if key else "(unparseable)"] += 1
            except Exception as exc:  # noqa: BLE001 — model/column drift
                self.stdout.write(f"  {model.__name__}.{col}: skipped ({exc})")
                continue
            summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
            self.stdout.write(f"  {model.__name__}.{col}: {summary or '(none)'}")
