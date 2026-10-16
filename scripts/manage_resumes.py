"""Command-line entry point for the private resume workflow (Phase 6).

Subcommands:
  ingest PATH [--label TEXT] [--activate]   Store, extract, and parse a PDF or DOCX resume.
  list                                      Show stored resume versions and review status.
  activate RESUME_VERSION_ID                Make a stored resume the active one.
  reextract [RESUME_VERSION_ID]             Parse stored resumes again with the current parser.

Output is JSON and never includes resume text.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from profiles import ProfileRepositoryError, ResumeDocumentError, ResumeIngestionService
from utils.config import Config
from utils.logger import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for resume management."""
    parser = argparse.ArgumentParser(description="Manage private resume versions")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Ingest a PDF or DOCX resume")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--label", help="Short name, for example 'Data engineering v2'")
    ingest.add_argument("--activate", action="store_true", help="Make it the active resume")
    commands.add_parser("list", help="List stored resume versions")
    activate = commands.add_parser("activate", help="Make a stored resume active")
    activate.add_argument("resume_version_id")
    reextract = commands.add_parser("reextract", help="Parse stored resumes again")
    reextract.add_argument("resume_version_id", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one resume-management command and print a JSON summary."""
    args = build_parser().parse_args(argv)
    config = Config()
    configure_logging(config)
    service = ResumeIngestionService(config)
    try:
        if args.command == "ingest":
            result = service.ingest(args.path, label=args.label, activate=args.activate)
            payload: object = result.summary()
        elif args.command == "list":
            payload = _listing(service)
        elif args.command == "activate":
            activated, message = service.activate(args.resume_version_id)
            payload = {"activated": activated, "message": message}
        else:
            profile = service.profile()
            ids = (
                [args.resume_version_id]
                if args.resume_version_id
                else [
                    version.resume_version_id
                    for version in service.repository.list_resume_versions(profile.profile_id)
                ]
            )
            payload = [
                {
                    "resume_version_id": version_id,
                    "extraction_id": extraction.extraction_id,
                    "status": extraction.status.value,
                    "fact_counts": extraction.facts.section_counts(),
                }
                for version_id in ids
                for extraction in [service.reextract(version_id)]
            ]
    except (FileNotFoundError, ResumeDocumentError, ProfileRepositoryError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _listing(service: ResumeIngestionService) -> dict[str, object]:
    profile = service.profile()
    versions = []
    for version in service.repository.list_resume_versions(profile.profile_id):
        extractions = service.repository.list_extractions(version.resume_version_id)
        latest = extractions[0] if extractions else None
        versions.append(
            {
                "resume_version_id": version.resume_version_id,
                "label": version.label,
                "media_type": version.media_type.value,
                "uploaded_at": version.uploaded_at.isoformat(),
                "active": version.resume_version_id == profile.active_resume_version_id,
                "latest_extraction": None
                if latest is None
                else {
                    "extraction_id": latest.extraction_id,
                    "status": latest.status.value,
                    "review_status": latest.review_status.value,
                    "approved_sections": [section.value for section in latest.approved_sections],
                },
            }
        )
    return {"profile_id": profile.profile_id, "resume_versions": versions}


if __name__ == "__main__":
    raise SystemExit(main())
