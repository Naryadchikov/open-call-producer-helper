"""Small JSON-output CLI suitable for use by a bot or a human."""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

from .assessment import assess
from .call_assessment import assess_call, exit_code_for_assess_call
from .draft_v2 import (
    exit_code_for_prepare_draft,
    exit_code_for_validate_draft,
    prepare_draft,
    validate_draft,
    DraftGateError,
)
from .package_plan import exit_code_for_plan_package, plan_package
from .demo import create_demo
from .io import ProducerError, utc_time
from .packaging import prepare, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open-Call Producer — offline draft preparation; never submits"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Create a new synthetic workspace; no real images or opportunity")
    demo.add_argument("--out", type=Path, required=True)
    demo.add_argument("--scenario", choices=["ready", "ineligible", "unknown", "expired"], default="ready")
    for name in ("assess", "prepare", "validate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--workspace", type=Path, required=True)
        if name == "prepare":
            cmd.add_argument("--name", default="draft-001")
        if name == "validate":
            cmd.add_argument("--package", required=True,
                             help="Workspace-relative package directory, e.g. output/draft-001")
    call = sub.add_parser(
        "assess-call",
        help=(
            "Read-only analysis of a schema_version 2 opportunity (optional minimal applicant facts). "
            "Does not require artwork/profile/application. Successful analysis is not eligibility."
        ),
    )
    call.add_argument("--opportunity", type=Path, required=True,
                      help="Path to opportunity JSON (schema_version 2 required)")
    call.add_argument("--applicant", type=Path, default=None,
                      help="Optional minimal JSON with only explicitly supplied applicant facts")
    call.add_argument("--workspace", type=Path, default=None,
                      help="Workspace root for resolving snapshot_path values (defaults to opportunity parent)")
    call.add_argument("--now", type=str, default=None,
                      help="ISO timestamp with UTC offset for controlled freshness/deadline tests")
    plan = sub.add_parser(
        "plan-package",
        help=(
            "Read-only preparation plan for schema_version 2 opportunity + approved artwork "
            "manifest. Does not prepare, modify images, submit, or pay."
        ),
    )
    plan.add_argument("--opportunity", type=Path, required=True,
                      help="Path to opportunity JSON (schema_version 2 required)")
    plan.add_argument("--artwork-manifest", type=Path, required=True,
                      help="Path to approved artwork manifest JSON")
    plan.add_argument("--applicant", type=Path, default=None,
                      help="Optional minimal applicant facts (may include first_name/last_name)")
    plan.add_argument("--workspace", type=Path, default=None,
                      help="Workspace root for resolving artwork/snapshot paths")
    plan.add_argument("--now", type=str, default=None,
                      help="ISO timestamp with UTC offset for assess-call freshness/deadline gates")
    prep_d = sub.add_parser(
        "prepare-draft",
        help=(
            "Execute a bounded v2 preparation plan into a new draft package "
            "(submission copies + review proof + ZIP). Never submits or pays."
        ),
    )
    prep_d.add_argument("--opportunity", type=Path, required=True)
    prep_d.add_argument("--artwork-manifest", type=Path, required=True)
    prep_d.add_argument("--applicant", type=Path, default=None,
                        help="Optional applicant facts (names + optional approved statement/bio)")
    prep_d.add_argument("--workspace", type=Path, required=True)
    prep_d.add_argument("--decisions", type=Path, required=True,
                        help="Path to prepare-decisions JSON bound to the plan fingerprint")
    prep_d.add_argument("--name", required=True, help="New output run name under output/")
    prep_d.add_argument("--now", type=str, default=None,
                        help="ISO timestamp with UTC offset for plan freshness/deadline gates")
    val_d = sub.add_parser(
        "validate-draft",
        help=(
            "Validate an actual saved v2 draft package against current plan fingerprint and inputs. "
            "Never weakens expectations to force a pass."
        ),
    )
    val_d.add_argument("--workspace", type=Path, required=True)
    val_d.add_argument("--package", required=True,
                       help="Workspace-relative package directory, e.g. output/draft-001")
    val_d.add_argument("--opportunity", type=Path, required=True)
    val_d.add_argument("--artwork-manifest", type=Path, required=True)
    val_d.add_argument("--applicant", type=Path, default=None)
    val_d.add_argument("--decisions", type=Path, required=True)
    val_d.add_argument("--now", type=str, default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            result = create_demo(args.out, args.scenario)
        elif args.command == "assess":
            result = assess(args.workspace)
        elif args.command == "prepare":
            result = prepare(args.workspace, args.name)
        elif args.command == "validate":
            result = validate(args.workspace, args.package)
        elif args.command == "plan-package":
            now = utc_time(args.now) if args.now else None
            result = plan_package(
                args.opportunity,
                args.artwork_manifest,
                applicant_path=args.applicant,
                workspace=args.workspace,
                now=now,
            )
        elif args.command == "prepare-draft":
            now = utc_time(args.now) if args.now else None
            result = prepare_draft(
                args.opportunity,
                args.artwork_manifest,
                workspace=args.workspace,
                decisions_path=args.decisions,
                name=args.name,
                applicant_path=args.applicant,
                now=now,
            )
        elif args.command == "validate-draft":
            now = utc_time(args.now) if args.now else None
            result = validate_draft(
                args.workspace,
                args.package,
                opportunity_path=args.opportunity,
                artwork_manifest_path=args.artwork_manifest,
                decisions_path=args.decisions,
                applicant_path=args.applicant,
                now=now,
            )
        else:
            now = utc_time(args.now) if args.now else None
            result = assess_call(
                args.opportunity,
                applicant_path=args.applicant,
                workspace=args.workspace,
                now=now,
            )
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        if args.command == "assess":
            return {"checks_passed": 0, "needs_review": 3, "blocked": 2}[result["status"]]
        if args.command == "assess-call":
            return exit_code_for_assess_call(result)
        if args.command == "plan-package":
            return exit_code_for_plan_package(result)
        if args.command == "prepare-draft":
            return exit_code_for_prepare_draft(result)
        if args.command == "validate-draft":
            return exit_code_for_validate_draft(result)
        return 0
    except DraftGateError as exc:
        print(json.dumps({"status": exc.status, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    except (ProducerError, OSError, UnicodeError, zipfile.BadZipFile, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
