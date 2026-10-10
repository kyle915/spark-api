"""Which brands run a public BA job board, and who hears about bookings.

Opt-in per brand. Matched on the exact tenant slug first, then the exact
``request_url_name`` (prod Torch's slug differs from ``keee-torch-thc``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobBoardConfig:
    brand_label: str
    internal_emails: tuple[str, ...]


TORCH_JOB_BOARD = JobBoardConfig(
    brand_label="Torch THC",
    internal_emails=(
        "events@igniteproductions.co",
        "nevena@igniteproductions.co",
        "kyle@igniteproductions.co",
    ),
)

JOB_BOARD_BY_SLUG: dict[str, JobBoardConfig] = {
    "torch": TORCH_JOB_BOARD,
    "torch-thc": TORCH_JOB_BOARD,
    "keee-torch-thc": TORCH_JOB_BOARD,
}


def job_board_config_for(tenant) -> JobBoardConfig | None:
    slug = (getattr(tenant, "slug", None) or "").strip().lower()
    if slug in JOB_BOARD_BY_SLUG:
        return JOB_BOARD_BY_SLUG[slug]
    url_name = (getattr(tenant, "request_url_name", None) or "").strip().lower()
    return JOB_BOARD_BY_SLUG.get(url_name)
