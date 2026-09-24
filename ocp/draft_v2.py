"""v2 prepare-draft / validate-draft: execute a bounded plan into a submission draft.

Never submits, pays, or fills portals. Never overwrites originals.
"""
from __future__ import annotations

import hashlib
import html
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .decisions import (
    acknowledged_manual_ids,
    compute_plan_fingerprint,
    has_execute_approval,
    load_decisions,
)
from .imaging import create_jpeg, inspect_jpeg, resolve_file_byte_bounds
from .io import ProducerError, checked_root, load_json, local_path, sha256, slug, word_count, write_json
from .package_plan import PLAN_APPLICANT_ALLOWED_KEYS, plan_package
from .packaging import make_zip

SUPPORTED_OPERATIONS = frozenset({
    "encode_jpeg_submission_copy",
    "downscale_longest_edge",
    "no_pixel_resize",
    "set_jpeg_density_metadata",
    "assign_asmith_numbered_filename",
    "jpeg_quality_fit_max_bytes",
})

UNSUPPORTED_OPERATION_MARKERS = (
    "crop",
    "upscale",
    "restyle",
    "retouch",
    "sharpen",
    "filter",
    "composite",
)

DRAFT_APPLICANT_EXTRA_KEYS = frozenset({
    "statement",
    "approved_bio",
    "credit",
})


class DraftGateError(ProducerError):
    """Structured prepare/validate gate with a CLI exit code."""

    def __init__(self, message: str, *, exit_code: int, status: str):
        super().__init__(message)
        self.exit_code = exit_code
        self.status = status


def load_draft_applicant(path: Path | None) -> dict:
    """Applicant facts for draft production: plan keys plus optional approved text."""
    if path is None:
        return {}
    data = load_json(path)
    from .package_plan import PLAN_APPLICANT_ALLOWED_KEYS, _NAME_RE
    allowed = PLAN_APPLICANT_ALLOWED_KEYS | DRAFT_APPLICANT_EXTRA_KEYS
    unknown = set(data) - allowed
    if unknown:
        raise ProducerError(f"Unsupported applicant keys: {sorted(unknown)}")
    for key in ("first_name", "last_name"):
        if key in data and data[key] is not None:
            value = data[key]
            if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
                raise ProducerError(
                    f"applicant.{key} must be a short alphabetic name (optional hyphen/apostrophe)"
                )
    for key in ("statement", "approved_bio", "credit"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            raise ProducerError(f"applicant.{key} must be a string when provided")
    return data


def _result(status: str, **kwargs) -> dict:
    payload = {"status": status, "human_review_required": True, "submitted": False}
    payload.update(kwargs)
    return payload


def exit_code_for_prepare_draft(result: dict) -> int:
    status = result.get("status")
    if status == "prepared":
        return 0
    if status == "blocked":
        return 2
    if status == "incomplete":
        return 3
    return 1


def exit_code_for_validate_draft(result: dict) -> int:
    status = result.get("status")
    if status == "validated_draft":
        return 0
    if status == "blocked":
        return 2
    if status == "incomplete":
        return 3
    return 1


def _untagged_artwork_ids(plan: dict) -> list[str]:
    ids = []
    for row in plan.get("artworks") or []:
        source = row.get("source") or {}
        if source.get("mode") in ("RGB", "L") and not source.get("has_icc_profile"):
            ids.append(row["id"])
    # Also from colour manuals
    for m in plan.get("manual_checks") or []:
        if m.get("id") == "untagged_colour_policy" and m.get("artwork_id"):
            if m["artwork_id"] not in ids:
                ids.append(m["artwork_id"])
    return ids


def _plan_has_unsupported_required_ops(plan: dict) -> str | None:
    for action in plan.get("automatic_actions") or []:
        op = (action.get("operation") or "").lower()
        if action.get("operation") not in SUPPORTED_OPERATIONS:
            return f"Unsupported automatic operation in plan: {action.get('operation')}"
        for marker in UNSUPPORTED_OPERATION_MARKERS:
            if marker in op and action.get("operation") not in SUPPORTED_OPERATIONS:
                return f"Unsupported operation marker '{marker}' in {action.get('operation')}"
    for blocker in plan.get("blockers") or []:
        bid = (blocker.get("id") or "").lower()
        msg = (blocker.get("message") or "").lower()
        for marker in ("crop", "upscale", "restyle"):
            if marker in bid or marker in msg:
                return f"Plan blocker requires unsupported transform: {blocker.get('id')}"
    return None


def _encode_facts(opportunity: dict, plan: dict) -> dict:
    """Build create_jpeg facts from v2 requirements + separated byte bounds.

    Organiser-declared max_file_bytes, TOOL_MAX_FILE_BYTES, and the effective
    encode bound are distinct. create_jpeg uses the effective bound only.
    """
    reqs = opportunity["requirements"]
    max_req = reqs["max_long_edge_px"]
    min_req = reqs["min_long_edge_px"]
    mfb = reqs["max_file_bytes"]

    if max_req.get("knowledge_status") == "known" and isinstance(max_req.get("value"), int):
        max_edge = max_req["value"]
    else:
        # No organiser max: do not invent one; use a no-op ceiling so thumbnail leaves pixels.
        edges = [row["source"]["long_edge_px"] for row in plan["artworks"]]
        max_edge = max(edges) if edges else 1

    if min_req.get("knowledge_status") == "known" and isinstance(min_req.get("value"), int):
        min_edge = min_req["value"]
    else:
        # Align with plan preflight: not_required → tool minimum of 1 px.
        min_edge = 1

    bounds = resolve_file_byte_bounds(
        knowledge_status=mfb.get("knowledge_status") or "not_stated",
        organiser_value_bytes=mfb.get("value") if isinstance(mfb.get("value"), int) else None,
        declared_label=mfb.get("declared_label"),
    )
    if not bounds["exact_bytes_ready"] or bounds["effective_max_file_bytes"] is None:
        # Caller gates should prevent encode when unresolved; keep a safe tool
        # ceiling only for not_required-style fallbacks already marked ready.
        raise ProducerError(
            "Cannot encode without an exact-byte effective bound "
            f"(controlling_bound={bounds['controlling_bound']}; "
            f"{bounds.get('note') or 'unresolved max_file_bytes'})"
        )
    max_bytes = bounds["effective_max_file_bytes"]
    # Legacy single-field note: which bound controls the encode ceiling.
    bytes_note = bounds["controlling_bound"]

    return {
        "max_long_edge_px": {"value": max_edge},
        "min_long_edge_px": {"value": min_edge},
        "max_file_bytes": {"value": max_bytes},
        "_bytes_bound_kind": bytes_note,
        "_file_byte_bounds": bounds,
    }


def _planned_filenames(plan: dict) -> dict[str, str]:
    for action in plan.get("automatic_actions") or []:
        detail = action.get("detail") or {}
        if "planned_filenames" in detail:
            return dict(detail["planned_filenames"])
    # Default deterministic names when no filename adapter planned.
    names = {}
    for row in sorted(plan["artworks"], key=lambda r: r["submission_order"]):
        names[row["id"]] = f"{row['submission_order']:02d}-{row['id']}.jpg"
    return names


def _needs_ppi(plan: dict) -> bool:
    return any(a.get("id") == "set-ppi-metadata" for a in plan.get("automatic_actions") or [])


def _text_plan(opportunity: dict, applicant: dict, artworks: list[dict]) -> dict[str, str | None]:
    """Decide which text files to emit. Never invent credentials or rewrite text.

    Rules:
    - artist-statement.txt: when statement_max_words knowledge_status is known
      (limit established) AND applicant supplied approved statement text; OR when
      the call marks the field required via known status and text is provided.
      If known with a limit but no text → incomplete (caller handles).
    - artist-bio.txt: same for bio_max_words / approved_bio.
    - image-list.txt: when any artwork has a non-empty title.
    - Do not emit statement/bio when knowledge_status is not_required and no
      approved text was supplied for a required field.
    """
    reqs = opportunity["requirements"]
    out: dict[str, str | None] = {
        "artist-statement.txt": None,
        "artist-bio.txt": None,
        "image-list.txt": None,
    }

    stmt_req = reqs.get("statement_max_words") or {}
    bio_req = reqs.get("bio_max_words") or {}
    statement = applicant.get("statement")
    bio = applicant.get("approved_bio")

    stmt_ks = stmt_req.get("knowledge_status")
    bio_ks = bio_req.get("knowledge_status")

    if stmt_ks == "known":
        if not isinstance(statement, str) or not statement.strip():
            raise DraftGateError(
                "Call requires a statement (statement_max_words known) but applicant.statement is missing",
                exit_code=3,
                status="incomplete",
            )
        limit = stmt_req.get("value")
        if isinstance(limit, int) and word_count(statement) > limit:
            raise DraftGateError(
                f"Statement exceeds word limit ({word_count(statement)} > {limit})",
                exit_code=2,
                status="blocked",
            )
        out["artist-statement.txt"] = statement.rstrip() + "\n"
    elif isinstance(statement, str) and statement.strip() and stmt_ks not in (None, "not_required"):
        # Applicant supplied text for a non-not_required field — include as approved draft text.
        out["artist-statement.txt"] = statement.rstrip() + "\n"

    if bio_ks == "known":
        if not isinstance(bio, str) or not bio.strip():
            raise DraftGateError(
                "Call requires a bio (bio_max_words known) but applicant.approved_bio is missing",
                exit_code=3,
                status="incomplete",
            )
        limit = bio_req.get("value")
        if isinstance(limit, int) and word_count(bio) > limit:
            raise DraftGateError(
                f"Bio exceeds word limit ({word_count(bio)} > {limit})",
                exit_code=2,
                status="blocked",
            )
        out["artist-bio.txt"] = bio.rstrip() + "\n"
    elif isinstance(bio, str) and bio.strip() and bio_ks not in (None, "not_required"):
        out["artist-bio.txt"] = bio.rstrip() + "\n"

    if any((a.get("title") or "").strip() for a in artworks):
        parts = []
        for art in sorted(artworks, key=lambda r: r["submission_order"]):
            title = art.get("title") or art["id"]
            year = art.get("year")
            medium = art.get("medium") or ""
            year_bit = f" ({year})" if year is not None else ""
            parts.append(
                f"{art['submission_order']:02d}. {title}{year_bit}\n"
                f"ID: {art['id']}\nMedium: {medium}\n"
            )
        out["image-list.txt"] = "\n".join(parts)

    return out


def _input_hashes(
    opportunity_path: Path,
    artwork_manifest_path: Path,
    applicant_path: Path | None,
    decisions_path: Path,
    opportunity: dict,
    root: Path,
) -> dict[str, str]:
    hashes = {
        "opportunity.json": sha256(opportunity_path),
        "artwork_manifest.json": sha256(artwork_manifest_path),
        "decisions.json": sha256(decisions_path),
    }
    if applicant_path is not None:
        hashes["applicant.json"] = sha256(applicant_path)
    primary = opportunity.get("source") or {}
    snap = primary.get("snapshot_path")
    if snap:
        hashes[snap] = sha256(local_path(root, snap))
    for art in opportunity.get("additional_sources") or []:
        # additional sources hashed via snapshot when present
        pass
    for extra in opportunity.get("additional_sources") or []:
        if isinstance(extra, dict) and extra.get("snapshot_path"):
            sp = extra["snapshot_path"]
            hashes[sp] = sha256(local_path(root, sp))
    return hashes


def _check_gates(
    plan: dict,
    decisions: dict,
    fingerprint: str,
) -> dict | None:
    """Return an early result dict if prepare must stop; else None."""
    if plan.get("blockers"):
        return _result(
            "blocked",
            message="Plan has blockers; resolve before prepare-draft",
            blockers=plan["blockers"],
            plan_fingerprint=fingerprint,
        )
    unsupported = _plan_has_unsupported_required_ops(plan)
    if unsupported:
        return _result("blocked", message=unsupported, plan_fingerprint=fingerprint)

    if plan.get("unresolved"):
        return _result(
            "incomplete",
            message="Unresolved required mechanics remain; cannot prepare-draft",
            unresolved=plan["unresolved"],
            plan_fingerprint=fingerprint,
        )

    if plan.get("status") != "plan_ready":
        return _result(
            "incomplete" if plan.get("status") == "incomplete" else "blocked",
            message=f"Plan status is {plan.get('status')}, not plan_ready",
            plan_fingerprint=fingerprint,
        )

    if not has_execute_approval(decisions, fingerprint):
        return _result(
            "incomplete",
            message=(
                "Missing execute approval for current plan fingerprint. "
                f"Current fingerprint={fingerprint}"
            ),
            plan_fingerprint=fingerprint,
            decisions_fingerprint=decisions.get("plan_fingerprint"),
        )

    selection = decisions.get("artwork_selection") or {}
    if selection.get("approved") is not True:
        return _result(
            "incomplete",
            message="artwork_selection.approved must be true",
            plan_fingerprint=fingerprint,
        )
    planned_ids = [a["id"] for a in sorted(plan["artworks"], key=lambda r: r["submission_order"])]
    if list(selection.get("artwork_ids_in_order") or []) != planned_ids:
        return _result(
            "blocked",
            message="artwork_selection order/ids do not match current plan artworks",
            plan_fingerprint=fingerprint,
            planned_ids=planned_ids,
            decided_ids=selection.get("artwork_ids_in_order"),
        )

    untagged = _untagged_artwork_ids(plan)
    colour = (decisions.get("processing") or {}).get("untagged_colour_policy")
    if untagged and colour not in ("assume_srgb", "reject"):
        return _result(
            "incomplete",
            message=(
                "Untagged RGB/L source(s) present; set processing.untagged_colour_policy "
                "to assume_srgb or reject explicitly (never assumed silently). "
                f"Artwork ids: {untagged}"
            ),
            plan_fingerprint=fingerprint,
            untagged_artwork_ids=untagged,
        )
    if untagged and colour == "reject":
        return _result(
            "blocked",
            message=f"Untagged sources rejected by colour policy: {untagged}",
            plan_fingerprint=fingerprint,
        )

    blocking = [m for m in (plan.get("manual_checks") or []) if m.get("blocks_prepare")]
    if not plan.get("ready_for_prepare"):
        try:
            ack_ids = acknowledged_manual_ids(decisions)
        except ProducerError as exc:
            return _result("blocked", message=str(exc), plan_fingerprint=fingerprint)
        missing = [m["id"] for m in blocking if m["id"] not in ack_ids]
        if missing:
            return _result(
                "incomplete",
                message=(
                    "ready_for_prepare=false and prepare-blocking manuals are not "
                    f"acknowledged: {missing}. Prefer fixtures with knowledge_status="
                    "not_required for fee/rights/deadline/media rather than inventing facts."
                ),
                plan_fingerprint=fingerprint,
                missing_acknowledgements=missing,
            )

    return None


def prepare_draft(
    opportunity_path: Path,
    artwork_manifest_path: Path,
    *,
    workspace: Path,
    decisions_path: Path,
    name: str,
    applicant_path: Path | None = None,
    now=None,
) -> dict:
    """Execute a bounded v2 preparation plan into a new draft package."""
    root = checked_root(workspace)
    opportunity_path = opportunity_path.absolute()
    artwork_manifest_path = artwork_manifest_path.absolute()
    decisions_path = decisions_path.absolute()
    name = slug(name)

    opportunity = load_json(opportunity_path, schema="opportunity")
    if opportunity.get("schema_version") != 2:
        raise ProducerError("prepare-draft requires opportunity schema_version 2")

    applicant = load_draft_applicant(applicant_path)
    plan_only = {k: v for k, v in applicant.items() if k in PLAN_APPLICANT_ALLOWED_KEYS}
    plan = plan_package(
        opportunity_path,
        artwork_manifest_path,
        applicant=plan_only,
        workspace=root,
        now=now,
    )
    decisions = load_decisions(decisions_path)
    fingerprint = compute_plan_fingerprint(
        plan,
        opportunity,
        applicant=applicant,
        processing=decisions.get("processing"),
    )

    gate = _check_gates(plan, decisions, fingerprint)
    if gate is not None:
        return gate

    colour_policy = (decisions.get("processing") or {}).get("untagged_colour_policy")
    # Tagged-only plans may leave policy null; open_pixels still needs reject|assume_srgb.
    untagged_policy = colour_policy if colour_policy in ("assume_srgb", "reject") else "reject"

    facts = _encode_facts(opportunity, plan)
    filenames = _planned_filenames(plan)
    use_dpi = (72, 72) if _needs_ppi(plan) else None

    try:
        text_files = _text_plan(opportunity, applicant, plan["artworks"])
    except DraftGateError as exc:
        return _result(exc.status, message=str(exc), plan_fingerprint=fingerprint)

    output_root = local_path(root, "output", must_exist=False)
    output_root.mkdir(exist_ok=True)
    target = local_path(root, f"output/{name}", must_exist=False)
    if target.exists():
        return _result(
            "blocked",
            message="Output already exists. Choose a new run name; nothing is overwritten.",
            plan_fingerprint=fingerprint,
        )

    temporary = Path(tempfile.mkdtemp(prefix=".preparing-", dir=output_root))
    try:
        submission = temporary / "submission"
        submission.mkdir()
        review = temporary / "review"
        review.mkdir()

        # Ensure image parent dirs exist for default images/ paths
        for fname in filenames.values():
            dest = submission / fname
            dest.parent.mkdir(parents=True, exist_ok=True)

        rows = []
        ordered = sorted(plan["artworks"], key=lambda r: r["submission_order"])
        for art in ordered:
            rel = art["path"]
            original = local_path(root, rel)
            original_hash = sha256(original)
            if original_hash != (art.get("source") or {}).get("sha256"):
                raise ProducerError(
                    f"Artwork {art['id']} source hash changed since plan; discard and re-plan"
                )
            dest_name = filenames[art["id"]]
            dest = submission / dest_name
            result = create_jpeg(
                original,
                dest,
                facts,
                untagged_policy,
                dpi=use_dpi,
            )
            if sha256(original) != original_hash or result["source_sha256"] != original_hash:
                raise ProducerError("Source changed while preparing a derivative; discard this run")
            rows.append({
                "artwork_id": art["id"],
                "path": dest_name,
                "submission_order": art["submission_order"],
                **result,
            })

        for text_name, content in text_files.items():
            if content is not None:
                (submission / text_name).write_text(content, encoding="utf-8")

        submission_names = sorted(
            [r["path"] for r in rows]
            + [n for n, c in text_files.items() if c is not None]
        )

        input_hashes = _input_hashes(
            opportunity_path, artwork_manifest_path, applicant_path,
            decisions_path, opportunity, root,
        )
        # Also bind each source artwork hash
        artwork_hashes = {
            art["path"]: (art.get("source") or {}).get("sha256")
            for art in ordered
        }

        manifest = {
            "schema_version": 2,
            "state": "DRAFT_AWAITING_HUMAN_REVIEW",
            "demo_only": bool(opportunity.get("demo_only")),
            "plan_fingerprint": fingerprint,
            "input_hashes": input_hashes,
            "artwork_source_hashes": artwork_hashes,
            "images": rows,
            "files": [
                {
                    "path": path,
                    "bytes": (submission / path).stat().st_size,
                    "sha256": sha256(submission / path),
                }
                for path in submission_names
            ],
            "human_review_required": True,
            "submitted": False,
            "bytes_bound_kind": facts["_bytes_bound_kind"],
            "file_byte_bounds": facts["_file_byte_bounds"],
            "integrity_note": (
                "Hashes bind this local draft to its inputs and plan fingerprint; "
                "not a digital signature or independent eligibility proof."
            ),
        }
        write_json(temporary / "manifest.json", manifest)
        write_json(review / "plan.json", plan)
        (review / "plan-summary.md").write_text(plan.get("summary_markdown") or "", encoding="utf-8")
        write_json(review / "assessment_summary.json", plan.get("assess_call") or {})
        write_json(review / "fingerprint.json", {
            "plan_fingerprint": fingerprint,
            "decisions_plan_fingerprint": decisions.get("plan_fingerprint"),
        })

        report_lines = [
            "# v2 draft preparation report",
            "",
            "**DRAFT — NOT SUBMITTED. Human review required.**",
            "",
            f"Opportunity: {plan.get('opportunity_title')}",
            f"Plan fingerprint: `{fingerprint}`",
            f"Demo only: {opportunity.get('demo_only')}",
            f"ready_for_prepare (at plan time): {plan.get('ready_for_prepare')}",
            "",
            "## Automatic actions executed",
            "",
        ]
        for act in plan.get("automatic_actions") or []:
            report_lines.append(f"- `{act['id']}` — {act['operation']}")
        report_lines += ["", "## Required human checks", ""]
        for item in plan.get("manual_checks") or []:
            report_lines.append(f"- [ ] {item.get('id')}: {item.get('message')}")
        bounds = facts["_file_byte_bounds"]
        org = bounds["organiser_declared"]
        report_lines += [
            "",
            "## File byte bounds (source / tool / effective)",
            "",
            f"- Organiser declared value_bytes: {org.get('value_bytes')!r}",
            f"- Organiser declared_label: {org.get('declared_label')!r}",
            f"- Organiser knowledge_status: {org.get('knowledge_status')!r}",
            f"- Tool ceiling (TOOL_MAX_FILE_BYTES): {bounds['tool_ceiling_bytes']}",
            f"- Effective max_file_bytes for this draft: {bounds['effective_max_file_bytes']}",
            f"- Controlling bound: {bounds['controlling_bound']}",
            "",
            "Tool ceiling is never recorded as the organiser's upload rule.",
            "",
            "## Note",
            "",
            "Acknowledgements authorize prepare only; they are not organiser evidence.",
            "Validate with validate-draft against actual files before any manual upload.",
            "",
        ]
        (review / "report.md").write_text("\n".join(report_lines), encoding="utf-8")

        cards = []
        for art, row in zip(ordered, rows):
            title = html.escape(str(art.get("title") or art["id"]), quote=True)
            cards.append(
                f'<figure><img src="../submission/{html.escape(row["path"], quote=True)}" '
                f'alt="{title}">'
                f'<figcaption>{html.escape(str(art.get("title") or art["id"]))} · '
                f'{row["width"]} × {row["height"]} px · {row["bytes"]:,} bytes · '
                f'{html.escape(row["path"])}</figcaption></figure>'
            )
        page = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width">'
            '<title>Submission proof — human review</title>'
            '<style>body{font:16px system-ui;margin:32px;max-width:1100px}'
            'main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px}'
            'figure{margin:0}img{width:100%;height:280px;object-fit:contain}'
            'figcaption{padding:12px 0}</style>'
            '<h1>Submission proof (v2 draft)</h1>'
            '<p>DRAFT — NOT SUBMITTED. Inspect every image before approval.</p>'
            f'<p>{html.escape(str(plan.get("opportunity_title") or ""))}</p>'
            f'<p>Fingerprint: <code>{html.escape(fingerprint)}</code></p>'
            f'<main>{"".join(cards)}</main></html>'
        )
        (review / "contact-sheet.html").write_text(page, encoding="utf-8")

        make_zip(submission, temporary / "submission.zip", submission_names)

        # Self-validate temporary package before promoting.
        validation = validate_draft(
            root,
            temporary.relative_to(root).as_posix(),
            opportunity_path=opportunity_path,
            artwork_manifest_path=artwork_manifest_path,
            decisions_path=decisions_path,
            applicant_path=applicant_path,
            now=now,
            _allow_temp=True,
        )
        vstatus = validation.get("status")
        if vstatus != "validated_draft":
            shutil.rmtree(temporary, ignore_errors=True)
            if vstatus in ("blocked", "incomplete"):
                return _result(
                    vstatus,
                    message=validation.get("message")
                    or f"Self-validation returned {vstatus}; draft not promoted",
                    plan_fingerprint=fingerprint,
                )
            return _result(
                "blocked",
                message=validation.get("message")
                or f"Self-validation unexpected status {vstatus!r}; draft not promoted",
                plan_fingerprint=fingerprint,
            )

        if target.exists():
            raise ProducerError("Output appeared during preparation; choose a new run name")
        temporary.rename(target)
        return _result(
            "prepared",
            state=manifest["state"],
            package=f"output/{name}",
            demo_only=bool(opportunity.get("demo_only")),
            images=len(rows),
            plan_fingerprint=fingerprint,
            submission_files=submission_names,
        )
    except DraftGateError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        return _result(exc.status, message=str(exc), plan_fingerprint=fingerprint)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def validate_draft(
    workspace: Path,
    package: str,
    *,
    opportunity_path: Path,
    artwork_manifest_path: Path,
    decisions_path: Path,
    applicant_path: Path | None = None,
    now=None,
    _allow_temp: bool = False,
) -> dict:
    """Validate an actual saved v2 draft package against current inputs + plan."""
    root = checked_root(workspace)
    opportunity_path = opportunity_path.absolute()
    artwork_manifest_path = artwork_manifest_path.absolute()
    decisions_path = decisions_path.absolute()

    opportunity = load_json(opportunity_path, schema="opportunity")
    if opportunity.get("schema_version") != 2:
        raise ProducerError("validate-draft requires opportunity schema_version 2")

    folder = local_path(root, package, must_exist=False)
    if not folder.is_dir():
        raise ProducerError("Package directory not found")
    if not _allow_temp and folder.name.startswith(".preparing-"):
        raise ProducerError("Refusing to validate an in-progress temporary draft as deliverable")

    for child in folder.rglob("*"):
        if child.is_symlink():
            raise ProducerError("Symlink found in package")

    applicant = load_draft_applicant(applicant_path)
    plan_only = {k: v for k, v in applicant.items() if k in PLAN_APPLICANT_ALLOWED_KEYS}
    plan = plan_package(
        opportunity_path,
        artwork_manifest_path,
        applicant=plan_only,
        workspace=root,
        now=now,
    )
    decisions = load_decisions(decisions_path)
    fingerprint = compute_plan_fingerprint(
        plan,
        opportunity,
        applicant=applicant,
        processing=decisions.get("processing"),
    )

    if not has_execute_approval(decisions, fingerprint):
        return _result(
            "incomplete",
            message="Decisions execute approval does not match current plan fingerprint",
            plan_fingerprint=fingerprint,
        )

    unsupported = _plan_has_unsupported_required_ops(plan)
    if unsupported or plan.get("blockers") or plan.get("unresolved") or plan.get("status") != "plan_ready":
        return _result(
            "blocked" if (unsupported or plan.get("blockers")) else "incomplete",
            message="Current plan is not prepare-ready; draft cannot be validated as current",
            plan_fingerprint=fingerprint,
        )

    manifest = load_json(local_path(folder, "manifest.json"))
    if manifest.get("schema_version") != 2 or manifest.get("state") != "DRAFT_AWAITING_HUMAN_REVIEW":
        raise ProducerError("Unrecognised v2 draft manifest")
    if manifest.get("plan_fingerprint") != fingerprint:
        return _result(
            "blocked",
            message="Manifest plan_fingerprint does not match current plan; create a new draft",
            plan_fingerprint=fingerprint,
            manifest_fingerprint=manifest.get("plan_fingerprint"),
        )
    if manifest.get("demo_only") != bool(opportunity.get("demo_only")):
        raise ProducerError("Manifest demo_only does not match opportunity")

    expected_input = _input_hashes(
        opportunity_path, artwork_manifest_path, applicant_path,
        decisions_path, opportunity, root,
    )
    if manifest.get("input_hashes") != expected_input:
        return _result(
            "blocked",
            message="Inputs changed since preparation; create a new draft",
            plan_fingerprint=fingerprint,
        )

    filenames = _planned_filenames(plan)
    text_files = _text_plan(opportunity, applicant, plan["artworks"])
    expected_names = sorted(
        list(filenames.values())
        + [n for n, c in text_files.items() if c is not None]
    )

    submission = local_path(folder, "submission", must_exist=False)
    if not submission.is_dir():
        raise ProducerError("submission/ directory missing")
    actual = {p.relative_to(submission).as_posix() for p in submission.rglob("*") if p.is_file()}
    if actual != set(expected_names):
        return _result(
            "blocked",
            message="Unexpected or missing submission files",
            expected=expected_names,
            actual=sorted(actual),
            plan_fingerprint=fingerprint,
        )

    rows = manifest.get("files") or []
    if len(rows) != len(expected_names) or {row["path"] for row in rows} != set(expected_names):
        raise ProducerError("Manifest file list does not match required files")
    for row in rows:
        path = local_path(submission, row["path"])
        if path.stat().st_size != row["bytes"] or sha256(path) != row["sha256"]:
            return _result(
                "blocked",
                message=f"Changed file: {row['path']}",
                plan_fingerprint=fingerprint,
            )

    facts = _encode_facts(opportunity, plan)
    use_dpi = (72, 72) if _needs_ppi(plan) else None
    images = manifest.get("images") or []
    ordered = sorted(plan["artworks"], key=lambda r: r["submission_order"])
    if len(images) != len(ordered):
        raise ProducerError("Manifest image count mismatch")

    for art, row in zip(ordered, images):
        expected_name = filenames[art["id"]]
        if row.get("artwork_id") != art["id"] or row.get("path") != expected_name:
            raise ProducerError("Manifest image identity/order mismatch")
        source_path = local_path(root, art["path"])
        if row.get("source_sha256") != sha256(source_path):
            return _result(
                "blocked",
                message=f"Original image changed since preparation: {art['id']}",
                plan_fingerprint=fingerprint,
            )
        measured = inspect_jpeg(
            local_path(submission, expected_name),
            facts,
            expected_dpi=use_dpi,
        )
        for key in ("width", "height", "format", "bytes", "sha256"):
            if row.get(key) != measured[key]:
                return _result(
                    "blocked",
                    message=f"Manifest image measurements no longer match disk: {expected_name}",
                    plan_fingerprint=fingerprint,
                )
        if use_dpi and row.get("dpi") != list(use_dpi):
            return _result(
                "blocked",
                message=f"Manifest DPI metadata mismatch: {expected_name}",
                plan_fingerprint=fingerprint,
            )

    for text_name, content in text_files.items():
        if content is None:
            continue
        on_disk = local_path(submission, text_name).read_text(encoding="utf-8")
        if on_disk != content:
            return _result(
                "blocked",
                message=f"Submission text differs from approved inputs: {text_name}",
                plan_fingerprint=fingerprint,
            )

    archive_path = local_path(folder, "submission.zip")
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(expected_names) or set(names) != set(expected_names):
            return _result(
                "blocked",
                message="ZIP has unexpected, missing or duplicate entries",
                expected=expected_names,
                actual=names,
                plan_fingerprint=fingerprint,
            )
        for row in rows:
            info = archive.getinfo(row["path"])
            if info.file_size != row["bytes"]:
                return _result(
                    "blocked",
                    message="ZIP entry size mismatch",
                    plan_fingerprint=fingerprint,
                )
            with archive.open(info) as stream:
                data = stream.read(row["bytes"] + 1)
            if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                return _result(
                    "blocked",
                    message="ZIP entry differs from measured submission file",
                    plan_fingerprint=fingerprint,
                )

    # Contact sheet / review artifacts must exist for a complete draft.
    for rel in (
        "review/contact-sheet.html",
        "review/plan.json",
        "review/report.md",
        "manifest.json",
    ):
        if not local_path(folder, rel, must_exist=False).is_file():
            raise ProducerError(f"Missing review artifact: {rel}")

    return _result(
        "validated_draft",
        images=len(images),
        demo_only=bool(opportunity.get("demo_only")),
        plan_fingerprint=fingerprint,
        package=package,
        note=(
            "Checks actual files and ZIP against current plan fingerprint and inputs. "
            "Not legal clearance or proof of complete eligibility."
        ),
    )
