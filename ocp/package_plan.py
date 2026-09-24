"""Read-only preparation plan for schema_version 2 opportunities.

Answers: what would OCP need to do to turn explicitly approved finished artworks
into a compliant draft package, and what still needs human resolution?

Does NOT execute prepare, modify images, submit, pay, or fill portals.
"""
from __future__ import annotations

import json
import re
import tempfile
import warnings
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .call_assessment import (
    APPLICANT_ALLOWED_KEYS,
    INTERNAL_CAPACITY,
    assess_call,
    calculate_fee,
)
from .imaging import MAX_PIXELS, TOOL_MAX_FILE_BYTES, inspect_icc_profile, resolve_file_byte_bounds
from .io import ProducerError, checked_root, load_json, local_path, sha256, utc_time

Image.MAX_IMAGE_PIXELS = MAX_PIXELS

PLAN_APPLICANT_ALLOWED_KEYS = frozenset({
    "residence_country",
    "fee_budget",
    "image_count",
    "selected_optional_charges",
    "media",
    "first_name",
    "last_name",
})

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'-]{0,79}$")

# Explicit A. Smith-style adapter only — not a general NL filename interpreter.
# Require the recognizable FirstName_LastName token (not generic "consecutive numbers").
_ASMITH_TOKEN_RE = re.compile(r"FirstName_LastName", re.IGNORECASE)
_ASMITH_STRUCTURED_IDS = frozenset({"asmith_numbered_name", "asmith_firstname_lastname"})

_PPI_RE = re.compile(r"\b72\s*pp[iI]\b")
_PPI_NEG_RE = re.compile(
    r"(?i)(?:\b(?:not|no|without|isn'?t|is\s+not)\b[\s\S]{0,40}\b72\s*pp[iI]\b)"
    r"|(?:\b72\s*pp[iI]\b[\s\S]{0,40}\b(?:not\s+required|not\s+needed|unnecessary|optional)\b)"
)
_WATERMARK_RE = re.compile(r"\bwatermark\b", re.IGNORECASE)
_THEME_RE = re.compile(
    r"\b(theme|diptych|triptych|multiples|collage|AI|generative|prior exhibition|rights)\b",
    re.IGNORECASE,
)

CLASS_AUTOMATIC = "automatic_supported"
CLASS_MANUAL = "manual_check"
CLASS_BLOCKED = "blocked_unsupported"
CLASS_UNRESOLVED = "unresolved_source"
CLASS_NA = "not_applicable"

def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value



def normalize_plan_applicant(data: dict) -> dict:
    """Validate a plan-only applicant dict (reusable for in-memory planning)."""
    if not isinstance(data, dict):
        raise ProducerError("applicant must be a JSON object")
    unknown = set(data) - PLAN_APPLICANT_ALLOWED_KEYS
    if unknown:
        raise ProducerError(f"Unsupported applicant keys: {sorted(unknown)}")
    for key in ("first_name", "last_name"):
        if key in data and data[key] is not None:
            value = data[key]
            if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
                raise ProducerError(
                    f"applicant.{key} must be a short alphabetic name (optional hyphen/apostrophe)"
                )
    return data


def load_plan_applicant(path: Path | None) -> dict:
    """Minimal applicant facts for planning; extends assess-call keys with name fields."""
    if path is None:
        return {}
    return normalize_plan_applicant(load_json(path))


def load_artwork_manifest(path: Path) -> dict:
    data = load_json(path, schema="artwork_manifest")
    artworks = data["artworks"]
    ids = [a["id"] for a in artworks]
    if len(ids) != len(set(ids)):
        raise ProducerError("artwork_manifest: duplicate artwork id")
    paths = [a["path"] for a in artworks]
    if len(paths) != len(set(paths)):
        raise ProducerError("artwork_manifest: duplicate artwork path")
    orders = [a["submission_order"] for a in artworks]
    if len(orders) != len(set(orders)):
        raise ProducerError("artwork_manifest: duplicate submission_order")
    if sorted(orders) != list(range(1, len(artworks) + 1)):
        raise ProducerError(
            "artwork_manifest: submission_order must be contiguous 1..N "
            f"(got {sorted(orders)})"
        )
    # Approval gating is enforced in plan_package (blocked status), not as a schema/tool error.
    return data


def _evidence_payload(req: dict | None) -> dict | None:
    if not req:
        return None
    evid = req.get("evidence")
    if not isinstance(evid, dict):
        return None
    return {
        "source_id": evid.get("source_id"),
        "quote": evid.get("quote"),
        "locator": evid.get("locator"),
    }


def _req(opportunity: dict, name: str) -> dict:
    return opportunity["requirements"][name]


def _is_asmith_filename_rule(value: str | None, evidence: dict | None) -> bool:
    """True only for the explicit A. Smith FirstName_LastName adapter (or structured id)."""
    if isinstance(value, str) and value.strip() in _ASMITH_STRUCTURED_IDS:
        return True
    texts: list[str] = []
    if isinstance(value, str):
        texts.append(value)
    if evidence and isinstance(evidence.get("quote"), str):
        texts.append(evidence["quote"])
    blob = " ".join(texts)
    if not blob:
        return False
    # Require the recognizable FirstName_LastName token pattern — not generic
    # phrases like "consecutive numbers followed by artwork title".
    return bool(_ASMITH_TOKEN_RE.search(blob))


def _asmith_filename(order: int, first: str, last: str) -> str:
    return f"{order}{first}_{last}.jpg"


def _measure_source(path: Path) -> dict:
    """Read-only source measurement; never writes."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in ("JPEG", "PNG"):
                    raise ProducerError(
                        f"Only JPEG/PNG sources are supported for planning: {path.name}"
                    )
                if getattr(image, "n_frames", 1) != 1:
                    raise ProducerError(f"Animated/multipage sources not supported: {path.name}")
                w, h = image.size
                if w * h > MAX_PIXELS:
                    raise ProducerError(f"Source exceeds 40 MP pixel limit: {path.name}")
                mode = image.mode
                has_transparency = (
                    mode in ("RGBA", "LA", "PA")
                    or "transparency" in image.info
                    or (mode == "P" and "transparency" in image.info)
                )
                icc = image.info.get("icc_profile")
                icc_info = inspect_icc_profile(
                    bytes(icc) if isinstance(icc, (bytes, bytearray)) else None
                )
                return {
                    "width": w,
                    "height": h,
                    "long_edge_px": max(w, h),
                    "format": image.format,
                    "mode": mode,
                    "has_transparency": bool(has_transparency),
                    "has_icc_profile": bool(icc_info["present"]),
                    "icc_bytes": icc_info["bytes"],
                    "icc_within_limit": bool(icc_info["within_limit"]),
                    "icc_parseable": bool(icc_info["parseable"]),
                    "icc_ok": bool(icc_info["present"] and icc_info["within_limit"] and icc_info["parseable"]),
                    "icc_error": icc_info["error"],
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
    except (UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, OSError, ValueError) as exc:
        if isinstance(exc, ProducerError):
            raise
        raise ProducerError(f"Cannot measure source {path.name}: {exc}") from exc


def _classification_row(
    requirement_id: str,
    classification: str,
    *,
    knowledge_status: str | None = None,
    helper_support: str | None = None,
    message: str,
    evidence: dict | None = None,
    organiser_vs_tool: str = "organiser",
) -> dict:
    return {
        "requirement_id": requirement_id,
        "classification": classification,
        "knowledge_status": knowledge_status,
        "helper_support": helper_support,
        "message": message,
        "evidence": evidence,
        "organiser_vs_tool": organiser_vs_tool,
    }


def _action(
    action_id: str,
    *,
    requirement_id: str,
    operation: str,
    artwork_ids: list[str],
    changes: dict,
    safety_constraints: list[str],
    validation: str,
    evidence: dict | None,
    detail: dict | None = None,
) -> dict:
    row = {
        "id": action_id,
        "requirement_id": requirement_id,
        "operation": operation,
        "artwork_ids": list(artwork_ids),
        "changes": changes,
        "safety_constraints": safety_constraints,
        "validation": validation,
        "evidence": evidence,
    }
    if detail:
        row["detail"] = detail
    return row


def _collect_ppi_signals(opportunity: dict) -> tuple[bool, dict | None, str | None]:
    """Return (stated_72_ppi, evidence_or_none, source_note).

    Only promote from verified applicable organiser evidence with provenance.
    Never invent automatic PPI actions from unresolved_requirements text, and
    never treat negative phrasing as an automatic requirement.
    """
    for name in ("allowed_formats", "max_long_edge_px"):
        req = _req(opportunity, name)
        evid = req.get("evidence") or {}
        quote = evid.get("quote") if isinstance(evid, dict) else None
        if not isinstance(quote, str) or not _PPI_RE.search(quote):
            continue
        if _PPI_NEG_RE.search(quote):
            continue
        return True, _evidence_payload(req), f"from {name} evidence quote"
    # unresolved_requirements stay manual/unresolved — do not regex-promote PPI.
    return False, None, None


def _jpeg_source_compatibility(rows: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Classify measured sources for the existing opaque RGB/L + ICC encode path.

    Returns (blockers, manual_checks, automatic_artwork_ids).
    """
    blockers: list[dict] = []
    manuals: list[dict] = []
    automatic_ids: list[str] = []
    unsupported_modes = {"RGBA", "LA", "PA", "CMYK", "P", "YCbCr", "LAB", "HSV", "RGBa"}
    for row in rows:
        src = row["source"]
        mode = src.get("mode")
        art_id = row["id"]
        if mode in unsupported_modes or src.get("has_transparency"):
            blockers.append({
                "id": "source_mode_unsupported_for_jpeg",
                "artwork_id": art_id,
                "message": (
                    f"Artwork {art_id} mode={mode!r} transparency="
                    f"{src.get('has_transparency')} cannot use automatic JPEG "
                    "encode (opaque RGB/L only; alpha/palette/CMYK need manual conversion)."
                ),
            })
            continue
        if mode not in ("RGB", "L"):
            blockers.append({
                "id": "source_mode_unsupported_for_jpeg",
                "artwork_id": art_id,
                "message": (
                    f"Artwork {art_id} mode={mode!r} is outside the automatic "
                    "opaque RGB/L JPEG encode path."
                ),
            })
            continue
        if src.get("has_icc_profile") and not src.get("icc_ok"):
            detail = src.get("icc_error") or "ICC profile unusable"
            nbytes = int(src.get("icc_bytes") or 0)
            issue = "oversized" if not src.get("icc_within_limit", True) else "malformed"
            blockers.append({
                "id": "source_icc_unusable_for_jpeg",
                "artwork_id": art_id,
                "message": (
                    f"Artwork {art_id} embedded ICC is {issue} "
                    f"({detail}; {nbytes} bytes); "
                    "production encoder rejects ICC over 1 MB or ImageCms-unparsable profiles."
                ),
            })
            continue
        if not src.get("has_icc_profile"):
            manuals.append({
                "id": "untagged_colour_policy",
                "artwork_id": art_id,
                "message": (
                    f"Artwork {art_id} is untagged {mode}; explicit colour policy "
                    "(assume_srgb) or an ICC-profiled copy is required before automatic JPEG encode."
                ),
            })
            continue
        automatic_ids.append(art_id)
    return blockers, manuals, automatic_ids


PREPARE_BLOCKING_MANUAL_IDS = frozenset({
    "fee_schedule",
    "rights_clause",
    "no_watermark",
    "untagged_colour_policy",
    "deadline",
    "media_allowed",
})

INFORMATIONAL_MANUAL_IDS = frozenset({
    "residency",
    "statement_max_words",
    "bio_max_words",
    "unresolved_human_review",
})

_PACKAGING_TRUST_REQUIREMENTS = frozenset({
    "allowed_formats",
    "min_long_edge_px",
    "max_long_edge_px",
    "max_file_bytes",
    "filename_rule",
    "image_count",
})


def _assessment_forbids_production_ready(
    assessment: dict,
    opportunity: dict,
) -> tuple[str | None, str]:
    """Map assess-call trust/deadline outcomes onto plan status.

    Returns (status, reason) when production-ready planning is forbidden.
    """
    if assessment.get("eligibility_decision") == "known_mismatch":
        return (
            "blocked",
            "assess-call eligibility_decision=known_mismatch "
            "(e.g. expired exact deadline); plan cannot be production-ready.",
        )
    if assessment.get("status") == "incomplete":
        return (
            "incomplete",
            "assess-call status=incomplete (stale/corrupted/untrusted or incomplete capture); "
            "plan cannot be production-ready.",
        )
    for check in assessment.get("checks") or []:
        cid = check.get("id") or ""
        if cid.startswith("source_integrity:") and check.get("status") == "fail":
            return (
                "incomplete",
                "assess-call source integrity failure; plan cannot be production-ready.",
            )
    # Asserted packaging facts that assess-call did not establish (wrong-source
    # excerpt, unusable source, etc.) must not drive automatic transforms.
    established = assessment.get("sources_establish") or {}
    for name in _PACKAGING_TRUST_REQUIREMENTS:
        req = opportunity.get("requirements", {}).get(name) or {}
        ks = req.get("knowledge_status")
        if ks in ("known", "explicitly_unrestricted") and name not in established:
            return (
                "incomplete",
                f"assess-call did not establish asserted packaging requirement {name} "
                "(unverified/wrong-source excerpt or untrusted source); "
                "plan cannot be production-ready.",
            )
    return None, ""


def _bridge_assess_call(
    opportunity_path: Path,
    *,
    applicant: dict,
    image_count: int,
    workspace: Path,
    now,
) -> dict:
    """Call assess_call with genuine applicant facts + manifest-derived image_count."""
    facts = {k: applicant[k] for k in APPLICANT_ALLOWED_KEYS if k in applicant}
    # Planning image_count is the approved selection size — do not invent other facts.
    facts["image_count"] = image_count
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="ocp-plan-assess-applicant-",
            delete=False,
        ) as handle:
            json.dump(facts, handle, ensure_ascii=False)
            tmp_path = Path(handle.name)
        return assess_call(
            opportunity_path,
            applicant_path=tmp_path,
            workspace=workspace,
            now=now,
        )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)



def _normalize_manual_checks(manual_checks: list) -> list:
    """Tag each manual row with blocks_prepare (gate vs informational)."""
    out = []
    for item in manual_checks:
        row = dict(item)
        mid = row.get("id")
        if "blocks_prepare" not in row:
            if mid in INFORMATIONAL_MANUAL_IDS:
                row["blocks_prepare"] = False
            elif mid in PREPARE_BLOCKING_MANUAL_IDS:
                row["blocks_prepare"] = True
            else:
                # Safer default per review: unknown manuals gate prepare
                row["blocks_prepare"] = True
        out.append(row)
    return out


def _watermark_mentioned(opportunity: dict) -> bool:
    for item in opportunity.get("unresolved_requirements") or []:
        if isinstance(item, str) and _WATERMARK_RE.search(item):
            return True
    for name in ("allowed_formats", "rights_clause"):
        evid = (_req(opportunity, name).get("evidence") or {})
        quote = evid.get("quote") if isinstance(evid, dict) else None
        if isinstance(quote, str) and _WATERMARK_RE.search(quote):
            return True
    return False


def plan_package(
    opportunity_path: Path,
    artwork_manifest_path: Path,
    *,
    applicant_path: Path | None = None,
    applicant: dict | None = None,
    workspace: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Build a deterministic read-only preparation plan. Never modifies sources.

    Pass either ``applicant_path`` or in-memory ``applicant`` (mutually exclusive).
    """
    opportunity_path = opportunity_path.absolute()
    artwork_manifest_path = artwork_manifest_path.absolute()
    root = checked_root(workspace) if workspace else checked_root(opportunity_path.parent)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    opportunity = load_json(opportunity_path, schema="opportunity")
    if opportunity.get("schema_version") != 2:
        raise ProducerError(
            "plan-package requires opportunity schema_version 2; "
            "do not reuse v1 prepare for v2 planning"
        )

    if applicant_path is not None and applicant is not None:
        raise ProducerError("applicant and applicant_path are mutually exclusive")
    if applicant is not None:
        applicant = normalize_plan_applicant(applicant)
    else:
        applicant = load_plan_applicant(applicant_path)
    manifest = load_artwork_manifest(artwork_manifest_path)

    classifications: list[dict] = []
    automatic_actions: list[dict] = []
    manual_checks: list[dict] = []
    blockers: list[dict] = []
    unresolved: list[dict] = []
    tool_limitations: list[dict] = []
    artwork_rows: list[dict] = []
    assessment: dict | None = None

    # --- Resolve & measure approved artworks (read-only) ---
    ordered = sorted(manifest["artworks"], key=lambda a: a["submission_order"])
    if any(a.get("approved_for_submission_copy") is not True for a in ordered):
        blockers.append({
            "id": "missing_artwork_approval",
            "message": "Explicit approved_for_submission_copy=true is required for every artwork; plan stops.",
        })
        return _finalize(
            opportunity, status="blocked", classifications=classifications,
            automatic_actions=automatic_actions, manual_checks=manual_checks,
            blockers=blockers, unresolved=unresolved, tool_limitations=tool_limitations,
            artwork_rows=artwork_rows, applicant=applicant,
            summary_extra="Blocked: artwork approval missing.",
        )

    if len(ordered) > INTERNAL_CAPACITY:
        tool_limitations.append({
            "id": "INTERNAL_CAPACITY",
            "message": (
                f"Helper INTERNAL_CAPACITY={INTERNAL_CAPACITY} cannot plan more than "
                f"{INTERNAL_CAPACITY} artworks; this is a tool limit, not organiser ineligibility."
            ),
            "organiser_vs_tool": "tool",
        })
        blockers.append({
            "id": "INTERNAL_CAPACITY",
            "message": f"Cannot plan: {len(ordered)} artworks exceeds helper INTERNAL_CAPACITY={INTERNAL_CAPACITY}.",
        })
        return _finalize(
            opportunity, status="blocked", classifications=classifications,
            automatic_actions=automatic_actions, manual_checks=manual_checks,
            blockers=blockers, unresolved=unresolved, tool_limitations=tool_limitations,
            artwork_rows=artwork_rows, applicant=applicant,
            summary_extra="Blocked: helper capacity exceeded.",
        )

    # --- Bridge hardened assess-call trust / freshness / deadline layer ---
    assessment = _bridge_assess_call(
        opportunity_path,
        applicant=applicant,
        image_count=len(ordered),
        workspace=root,
        now=now,
    )
    gate_status, gate_reason = _assessment_forbids_production_ready(assessment, opportunity)
    if gate_status is not None:
        mismatches = assessment.get("applicant_mismatches") or []
        image_count_mismatch = any(
            (m.get("requirement") == "image_count" or m.get("id") == "image_count")
            for m in mismatches
        )
        if gate_status == "blocked" and image_count_mismatch:
            # Distinct production-plan blocker: approved selection vs organiser bounds.
            # Planner never selects/drops/adds works.
            blockers.append({
                "id": "image_count_selection_violation",
                "message": (
                    f"Approved artwork count violates organiser image_count bounds "
                    f"(assess-call known_mismatch). Supply a different approved manifest; "
                    f"this planner never selects, drops, or adds artworks. ({gate_reason})"
                ),
            })
        elif gate_status == "blocked":
            blockers.append({
                "id": "assess_call_trust_gate",
                "message": gate_reason,
                "assess_call_status": assessment.get("status"),
                "eligibility_decision": assessment.get("eligibility_decision"),
            })
        else:
            unresolved.append({
                "id": "assess_call_trust_gate",
                "message": gate_reason,
                "assess_call_status": assessment.get("status"),
                "eligibility_decision": assessment.get("eligibility_decision"),
            })
        return _finalize(
            opportunity, status=gate_status, classifications=classifications,
            automatic_actions=[], manual_checks=manual_checks,
            blockers=blockers, unresolved=unresolved, tool_limitations=tool_limitations,
            artwork_rows=artwork_rows, applicant=applicant, assessment=assessment,
            summary_extra=f"Assess-call gate: {gate_reason}",
        )

    for art in ordered:
        try:
            source_path = local_path(root, art["path"])
        except ProducerError as exc:
            blockers.append({
                "id": "source_path_unsafe_or_missing",
                "artwork_id": art["id"],
                "message": str(exc),
            })
            continue
        if not source_path.is_file():
            blockers.append({
                "id": "source_artwork_missing",
                "artwork_id": art["id"],
                "message": f"Source artwork missing: {art['path']}",
            })
            continue
        try:
            measured = _measure_source(source_path)
        except ProducerError as exc:
            blockers.append({
                "id": "source_unreadable",
                "artwork_id": art["id"],
                "message": str(exc),
            })
            continue
        artwork_rows.append({
            "id": art["id"],
            "path": art["path"],
            "submission_order": art["submission_order"],
            "title": art.get("title"),
            "year": art.get("year"),
            "medium": art.get("medium"),
            "approved_for_submission_copy": True,
            "source": measured,
        })

    if blockers:
        return _finalize(
            opportunity, status="blocked", classifications=classifications,
            automatic_actions=automatic_actions, manual_checks=manual_checks,
            blockers=blockers, unresolved=unresolved, tool_limitations=tool_limitations,
            artwork_rows=artwork_rows, applicant=applicant,
            summary_extra="Blocked: source path / missing / unreadable artwork.",
        )

    if not artwork_rows:
        blockers.append({
            "id": "no_approved_artworks",
            "message": "No explicitly approved artworks to plan; plan does not select works.",
        })
        return _finalize(
            opportunity, status="blocked", classifications=classifications,
            automatic_actions=automatic_actions, manual_checks=manual_checks,
            blockers=blockers, unresolved=unresolved, tool_limitations=tool_limitations,
            artwork_rows=artwork_rows, applicant=applicant,
            summary_extra="Blocked: no approved artworks.",
        )

    reqs = opportunity["requirements"]
    n = len(artwork_rows)

    # --- allowed_formats / JPEG ---
    fmt = reqs["allowed_formats"]
    fmt_ks, fmt_hs = fmt["knowledge_status"], fmt["helper_support"]
    fmt_val = fmt.get("value")
    fmt_evid = _evidence_payload(fmt)
    if fmt_ks in ("not_stated", "inaccessible", "conflicting"):
        classifications.append(_classification_row(
            "allowed_formats", CLASS_UNRESOLVED if fmt_ks != "conflicting" else CLASS_BLOCKED,
            knowledge_status=fmt_ks, helper_support=fmt_hs,
            message=f"Format requirement knowledge_status={fmt_ks}",
            evidence=fmt_evid,
        ))
        if fmt_ks == "conflicting":
            blockers.append({"id": "allowed_formats", "message": "Conflicting format requirements block planning."})
        else:
            unresolved.append({"id": "allowed_formats", "message": classifications[-1]["message"], "evidence": fmt_evid})
    elif fmt_ks in ("not_required", "explicitly_unrestricted"):
        classifications.append(_classification_row(
            "allowed_formats", CLASS_NA, knowledge_status=fmt_ks, helper_support=fmt_hs,
            message="No format restriction established as required.",
            evidence=fmt_evid,
        ))
    elif isinstance(fmt_val, list) and "JPEG" in [str(x).upper() for x in fmt_val]:
        mode_blockers, colour_manuals, jpeg_ids = _jpeg_source_compatibility(artwork_rows)
        blockers.extend(mode_blockers)
        manual_checks.extend(colour_manuals)
        if mode_blockers:
            classifications.append(_classification_row(
                "allowed_formats", CLASS_BLOCKED, knowledge_status=fmt_ks, helper_support=fmt_hs,
                message=(
                    "JPEG required, but one or more sources need unsupported "
                    "flatten/conversion (RGBA/CMYK/palette/alpha) before encode."
                ),
                evidence=fmt_evid,
            ))
        elif not jpeg_ids and colour_manuals:
            classifications.append(_classification_row(
                "allowed_formats", CLASS_MANUAL, knowledge_status=fmt_ks, helper_support=fmt_hs,
                message=(
                    "JPEG required, but sources are untagged RGB/L — explicit colour "
                    "policy (assume_srgb) required; not assumed silently."
                ),
                evidence=fmt_evid,
            ))
        elif jpeg_ids:
            note = ""
            if colour_manuals:
                note = (
                    f" {len(colour_manuals)} untagged source(s) need explicit colour "
                    "policy before encode."
                )
            classifications.append(_classification_row(
                "allowed_formats", CLASS_AUTOMATIC, knowledge_status=fmt_ks, helper_support=fmt_hs,
                message=(
                    f"JPEG output planned for {len(jpeg_ids)} tagged opaque RGB/L "
                    f"artwork(s).{note}"
                ),
                evidence=fmt_evid,
            ))
            automatic_actions.append(_action(
                "encode-jpeg",
                requirement_id="allowed_formats",
                operation="encode_jpeg_submission_copy",
                artwork_ids=jpeg_ids,
                changes={"pixels": True, "metadata": True, "filename": False, "packaging": True},
                safety_constraints=[
                    "never_overwrite_source",
                    "opaque_rgb_or_grayscale_only",
                    "no_retouch",
                    "strip_personal_exif_xmp",
                    "no_silent_assume_srgb",
                ],
                validation="Output files are RGB JPEG with measured format=JPEG",
                evidence=fmt_evid,
                detail={
                    "colour_compatible_ids": jpeg_ids,
                    "untagged_manual_ids": [m.get("artwork_id") for m in colour_manuals],
                },
            ))
        else:
            classifications.append(_classification_row(
                "allowed_formats", CLASS_MANUAL, knowledge_status=fmt_ks, helper_support=fmt_hs,
                message="JPEG required but no colour-compatible sources for automatic encode.",
                evidence=fmt_evid,
            ))
            manual_checks.append({
                "id": "allowed_formats",
                "message": classifications[-1]["message"],
                "evidence": fmt_evid,
            })
    elif isinstance(fmt_val, list) and fmt_val and "JPEG" not in [str(x).upper() for x in fmt_val] and "*" not in fmt_val:
        classifications.append(_classification_row(
            "allowed_formats", CLASS_BLOCKED, knowledge_status=fmt_ks, helper_support=fmt_hs,
            message=f"Required formats {fmt_val} do not include JPEG; helper only plans JPEG.",
            evidence=fmt_evid,
        ))
        blockers.append({
            "id": "allowed_formats_unsupported",
            "message": f"Unsupported required formats {fmt_val}; only JPEG encoding is planned.",
        })
    else:
        classifications.append(_classification_row(
            "allowed_formats", CLASS_MANUAL, knowledge_status=fmt_ks, helper_support=fmt_hs,
            message="Format rule needs human confirmation before automatic JPEG planning.",
            evidence=fmt_evid,
        ))
        manual_checks.append({"id": "allowed_formats", "message": classifications[-1]["message"], "evidence": fmt_evid})

    # --- max_long_edge_px (downscale only) ---
    max_edge = reqs["max_long_edge_px"]
    max_ks, max_hs = max_edge["knowledge_status"], max_edge["helper_support"]
    max_val = max_edge.get("value")
    max_evid = _evidence_payload(max_edge)
    if max_ks == "known" and isinstance(max_val, int):
        downscale_ids: list[str] = []
        leave_ids: list[str] = []
        for row in artwork_rows:
            if row["source"]["long_edge_px"] > max_val:
                downscale_ids.append(row["id"])
            else:
                leave_ids.append(row["id"])
        classifications.append(_classification_row(
            "max_long_edge_px", CLASS_AUTOMATIC, knowledge_status=max_ks, helper_support=max_hs,
            message=(
                f"Longest edge <= {max_val} px: downscale planned for {len(downscale_ids)} "
                f"artwork(s); {len(leave_ids)} already within bound (no upscale)."
            ),
            evidence=max_evid,
        ))
        if downscale_ids:
            automatic_actions.append(_action(
                "downscale-long-edge",
                requirement_id="max_long_edge_px",
                operation="downscale_longest_edge",
                artwork_ids=downscale_ids,
                changes={"pixels": True, "metadata": False, "filename": False, "packaging": True},
                safety_constraints=[
                    "never_upscale",
                    "never_crop",
                    "never_change_aspect_ratio",
                    "never_overwrite_source",
                ],
                validation=f"Measured longest edge of each derivative <= {max_val}",
                evidence=max_evid,
                detail={"max_long_edge_px": max_val, "already_within_bound": leave_ids},
            ))
        if leave_ids and not downscale_ids:
            # Still record intentional no-op for clarity when all fit.
            automatic_actions.append(_action(
                "long-edge-already-compliant",
                requirement_id="max_long_edge_px",
                operation="no_pixel_resize",
                artwork_ids=leave_ids,
                changes={"pixels": False, "metadata": False, "filename": False, "packaging": False},
                safety_constraints=["never_upscale", "never_crop", "never_overwrite_source"],
                validation=f"Source longest edge already <= {max_val}; no resize planned",
                evidence=max_evid,
                detail={"max_long_edge_px": max_val},
            ))
    elif max_ks in ("not_required", "explicitly_unrestricted", "not_stated"):
        classifications.append(_classification_row(
            "max_long_edge_px",
            CLASS_NA if max_ks != "not_stated" else CLASS_UNRESOLVED,
            knowledge_status=max_ks, helper_support=max_hs,
            message=f"max_long_edge_px knowledge_status={max_ks}",
            evidence=max_evid,
        ))
        if max_ks == "not_stated":
            unresolved.append({"id": "max_long_edge_px", "message": classifications[-1]["message"]})
    elif max_ks in ("inaccessible", "conflicting"):
        cls = CLASS_UNRESOLVED if max_ks == "inaccessible" else CLASS_BLOCKED
        classifications.append(_classification_row(
            "max_long_edge_px", cls, knowledge_status=max_ks, helper_support=max_hs,
            message=f"max_long_edge_px knowledge_status={max_ks}",
            evidence=max_evid,
        ))
        if max_ks == "conflicting":
            blockers.append({"id": "max_long_edge_px", "message": "Conflicting long-edge rules block planning."})
        else:
            unresolved.append({"id": "max_long_edge_px", "message": classifications[-1]["message"]})
    else:
        classifications.append(_classification_row(
            "max_long_edge_px", CLASS_MANUAL, knowledge_status=max_ks, helper_support=max_hs,
            message="Long-edge rule present but not automatically plannable.",
            evidence=max_evid,
        ))
        manual_checks.append({"id": "max_long_edge_px", "message": classifications[-1]["message"]})

    # --- min_long_edge_px: never upscale ---
    min_edge = reqs["min_long_edge_px"]
    min_ks, min_hs = min_edge["knowledge_status"], min_edge["helper_support"]
    min_val = min_edge.get("value")
    min_evid = _evidence_payload(min_edge)
    if min_ks == "known" and isinstance(min_val, int):
        too_small = [r["id"] for r in artwork_rows if r["source"]["long_edge_px"] < min_val]
        if too_small:
            classifications.append(_classification_row(
                "min_long_edge_px", CLASS_BLOCKED, knowledge_status=min_ks, helper_support=min_hs,
                message=(
                    f"Artwork(s) {too_small} below min long edge {min_val} px; "
                    "upscaling is prohibited — blocked."
                ),
                evidence=min_evid,
            ))
            blockers.append({
                "id": "min_long_edge_would_require_upscale",
                "message": classifications[-1]["message"],
                "artwork_ids": too_small,
            })
        else:
            classifications.append(_classification_row(
                "min_long_edge_px", CLASS_AUTOMATIC, knowledge_status=min_ks, helper_support=min_hs,
                message=f"All sources already meet min long edge {min_val} px (no upscale).",
                evidence=min_evid,
            ))
    elif min_ks in ("not_required", "explicitly_unrestricted", "not_stated"):
        classifications.append(_classification_row(
            "min_long_edge_px",
            CLASS_NA if min_ks != "not_stated" else CLASS_UNRESOLVED,
            knowledge_status=min_ks, helper_support=min_hs,
            message=f"min_long_edge_px knowledge_status={min_ks}",
            evidence=min_evid,
        ))
        if min_ks == "not_stated":
            unresolved.append({"id": "min_long_edge_px", "message": classifications[-1]["message"]})
    elif min_ks == "inaccessible":
        classifications.append(_classification_row(
            "min_long_edge_px", CLASS_UNRESOLVED, knowledge_status=min_ks, helper_support=min_hs,
            message="min_long_edge_px inaccessible",
            evidence=min_evid,
        ))
        unresolved.append({"id": "min_long_edge_px", "message": "min_long_edge_px inaccessible"})
    else:
        classifications.append(_classification_row(
            "min_long_edge_px", CLASS_MANUAL, knowledge_status=min_ks, helper_support=min_hs,
            message=f"min_long_edge_px knowledge_status={min_ks}",
            evidence=min_evid,
        ))
        manual_checks.append({"id": "min_long_edge_px", "message": classifications[-1]["message"]})

    # --- 72 ppi metadata-only (evidence-backed only; never from unresolved text) ---
    ppi_stated, ppi_evid, ppi_note = _collect_ppi_signals(opportunity)
    unresolved_ppi_mentions = [
        item for item in (opportunity.get("unresolved_requirements") or [])
        if isinstance(item, str) and _PPI_RE.search(item)
    ]
    if ppi_stated:
        classifications.append(_classification_row(
            "output_ppi", CLASS_AUTOMATIC, knowledge_status="known", helper_support="supported",
            message=(
                f"72 ppi planned as output metadata only ({ppi_note}); "
                "pixel dimensions are not altered merely for the PPI tag."
            ),
            evidence=ppi_evid,
        ))
        automatic_actions.append(_action(
            "set-ppi-metadata",
            requirement_id="output_ppi",
            operation="set_jpeg_density_metadata",
            artwork_ids=[a["id"] for a in artwork_rows],
            changes={"pixels": False, "metadata": True, "filename": False, "packaging": True},
            safety_constraints=[
                "metadata_only_unless_physical_print_dims_require_resample",
                "never_alter_pixel_dims_for_ppi_tag_alone",
                "never_overwrite_source",
            ],
            validation="Derivative JPEG reports 72 ppi density metadata; pixel dims unchanged by PPI step",
            evidence=ppi_evid,
            detail={"ppi": 72, "note": ppi_note},
        ))
    elif unresolved_ppi_mentions:
        classifications.append(_classification_row(
            "output_ppi", CLASS_UNRESOLVED, knowledge_status="not_stated", helper_support="manual",
            message=(
                "72 ppi appears only in unresolved_requirements / non-applicable phrasing; "
                "not promoted to automatic metadata action."
            ),
        ))
        unresolved.append({
            "id": "output_ppi",
            "message": classifications[-1]["message"],
        })
        for item in unresolved_ppi_mentions:
            if _PPI_NEG_RE.search(item):
                manual_checks.append({
                    "id": "output_ppi_negative_or_unresolved",
                    "message": item,
                })
    else:
        classifications.append(_classification_row(
            "output_ppi", CLASS_NA,
            message="No verified applicable 72 ppi organiser evidence for automatic metadata action.",
        ))

    # --- filename_rule (A. Smith adapter only) ---
    fn = reqs["filename_rule"]
    fn_ks, fn_hs = fn["knowledge_status"], fn["helper_support"]
    fn_val = fn.get("value")
    fn_evid = _evidence_payload(fn)
    if fn_ks in ("not_required", "explicitly_unrestricted") or fn_val in (None, "any"):
        if fn_ks in ("not_required", "explicitly_unrestricted") or fn_val == "any":
            classifications.append(_classification_row(
                "filename_rule", CLASS_NA, knowledge_status=fn_ks, helper_support=fn_hs,
                message="Filenames unrestricted / not required.",
                evidence=fn_evid,
            ))
        elif fn_ks in ("not_stated", "inaccessible"):
            classifications.append(_classification_row(
                "filename_rule", CLASS_UNRESOLVED, knowledge_status=fn_ks, helper_support=fn_hs,
                message=f"filename_rule knowledge_status={fn_ks}",
                evidence=fn_evid,
            ))
            unresolved.append({"id": "filename_rule", "message": classifications[-1]["message"]})
        else:
            classifications.append(_classification_row(
                "filename_rule", CLASS_MANUAL, knowledge_status=fn_ks, helper_support=fn_hs,
                message="Filename rule unclear; human review.",
                evidence=fn_evid,
            ))
            manual_checks.append({"id": "filename_rule", "message": classifications[-1]["message"]})
    elif _is_asmith_filename_rule(fn_val if isinstance(fn_val, str) else None, fn.get("evidence")):
        first = applicant.get("first_name")
        last = applicant.get("last_name")
        if not first or not last:
            classifications.append(_classification_row(
                "filename_rule", CLASS_BLOCKED, knowledge_status=fn_ks, helper_support=fn_hs,
                message=(
                    "A. Smith-style numbered filename adapter selected, but applicant "
                    "first_name/last_name are required and missing — filename planning blocked."
                ),
                evidence=fn_evid,
            ))
            blockers.append({
                "id": "filename_fields_missing",
                "message": classifications[-1]["message"],
            })
        else:
            planned_names = {
                row["id"]: _asmith_filename(row["submission_order"], first, last)
                for row in artwork_rows
            }
            classifications.append(_classification_row(
                "filename_rule", CLASS_AUTOMATIC, knowledge_status=fn_ks, helper_support=fn_hs,
                message=(
                    "A. Smith-style adapter: planned filenames "
                    + ", ".join(planned_names[r["id"]] for r in artwork_rows)
                ),
                evidence=fn_evid,
            ))
            automatic_actions.append(_action(
                "rename-asmith-numbered",
                requirement_id="filename_rule",
                operation="assign_asmith_numbered_filename",
                artwork_ids=[a["id"] for a in artwork_rows],
                changes={"pixels": False, "metadata": False, "filename": True, "packaging": True},
                safety_constraints=[
                    "adapter_asmith_only",
                    "no_general_nl_filename_interpreter",
                    "never_overwrite_source",
                    "order_follows_submission_order",
                ],
                validation="Package filenames match {n}FirstName_LastName.jpg in submission_order",
                evidence=fn_evid,
                detail={"planned_filenames": planned_names, "adapter": "asmith_numbered_name"},
            ))
    else:
        classifications.append(_classification_row(
            "filename_rule", CLASS_BLOCKED, knowledge_status=fn_ks, helper_support=fn_hs,
            message=(
                "Custom filename_rule is not an explicitly supported adapter "
                "(only A. Smith-style 1FirstName_LastName.jpg is planned)."
            ),
            evidence=fn_evid,
        ))
        blockers.append({
            "id": "filename_rule_unsupported",
            "message": classifications[-1]["message"],
        })

    # --- max_file_bytes ---
    # Separate organiser-declared limit, tool ceiling, and effective encode bound.
    mfb = reqs["max_file_bytes"]
    mfb_ks, mfb_hs = mfb["knowledge_status"], mfb["helper_support"]
    mfb_evid = _evidence_payload(mfb)
    mfb_label = mfb.get("declared_label") if isinstance(mfb.get("declared_label"), str) else None
    mfb_bounds = resolve_file_byte_bounds(
        knowledge_status=mfb_ks,
        organiser_value_bytes=mfb.get("value") if isinstance(mfb.get("value"), int) else None,
        declared_label=mfb_label,
    )
    if mfb_ks == "known" and isinstance(mfb.get("value"), int):
        effective = mfb_bounds["effective_max_file_bytes"]
        controlling = mfb_bounds["controlling_bound"]
        classifications.append(_classification_row(
            "max_file_bytes", CLASS_AUTOMATIC, knowledge_status=mfb_ks, helper_support=mfb_hs,
            message=(
                f"Byte-size fitting planned against effective_max_file_bytes={effective} "
                f"(organiser_declared={mfb['value']}, tool_ceiling={TOOL_MAX_FILE_BYTES}, "
                f"controlling_bound={controlling}; quality floor 80–95)."
            ),
            evidence=mfb_evid,
        ))
        automatic_actions.append(_action(
            "fit-max-bytes",
            requirement_id="max_file_bytes",
            operation="jpeg_quality_fit_max_bytes",
            artwork_ids=[a["id"] for a in artwork_rows],
            changes={"pixels": True, "lossy_encoding": True, "metadata": False, "filename": False, "packaging": True},
            safety_constraints=[
                "quality_floor_80",
                "no_silent_extra_dimension_shrink",
                "never_overwrite_source",
            ],
            validation=f"Each derivative size <= {effective} bytes (effective bound)",
            evidence=mfb_evid,
            detail={
                "organiser_declared_bytes": mfb["value"],
                "declared_label": mfb_label,
                "tool_ceiling_bytes": TOOL_MAX_FILE_BYTES,
                "effective_max_file_bytes": effective,
                "controlling_bound": controlling,
                # Keep legacy key as the effective generation/validation bound.
                "max_file_bytes": effective,
            },
        ))
        if controlling == "tool_ceiling":
            tool_limitations.append({
                "id": "TOOL_MAX_FILE_BYTES",
                "message": (
                    f"Organiser max_file_bytes={mfb['value']} exceeds helper "
                    f"TOOL_MAX_FILE_BYTES={TOOL_MAX_FILE_BYTES}; effective bound is the "
                    "tool ceiling (not recorded as the organiser rule)."
                ),
                "organiser_vs_tool": "tool",
                "file_byte_bounds": mfb_bounds,
            })
    elif mfb_ks == "known" and mfb.get("value") is None:
        # Label/evidence without verified exact bytes — representable, not encode-ready.
        label_note = f" declared_label={mfb_label!r}" if mfb_label else ""
        classifications.append(_classification_row(
            "max_file_bytes", CLASS_UNRESOLVED, knowledge_status=mfb_ks, helper_support=mfb_hs,
            message=(
                "max_file_bytes known without verified exact byte value"
                f"{label_note}; exact-byte planning unresolved "
                "(do not invent MB/MiB conversions)."
            ),
            evidence=mfb_evid,
        ))
        unresolved.append({
            "id": "max_file_bytes",
            "message": classifications[-1]["message"],
            "evidence": mfb_evid,
            "file_byte_bounds": mfb_bounds,
        })
    elif mfb_ks in ("inaccessible", "not_stated"):
        classifications.append(_classification_row(
            "max_file_bytes", CLASS_UNRESOLVED, knowledge_status=mfb_ks, helper_support=mfb_hs,
            message=f"max_file_bytes knowledge_status={mfb_ks} (e.g. portal upload limit unknown).",
            evidence=mfb_evid,
        ))
        unresolved.append({
            "id": "max_file_bytes",
            "message": classifications[-1]["message"],
            "evidence": mfb_evid,
            "file_byte_bounds": mfb_bounds,
        })
    elif mfb_ks in ("not_required", "explicitly_unrestricted"):
        classifications.append(_classification_row(
            "max_file_bytes", CLASS_NA, knowledge_status=mfb_ks, helper_support=mfb_hs,
            message=(
                "No max file bytes requirement established; helper encode may use "
                f"TOOL_MAX_FILE_BYTES={TOOL_MAX_FILE_BYTES} only (not an organiser rule)."
            ),
            evidence=mfb_evid,
        ))
    elif mfb_ks == "conflicting":
        classifications.append(_classification_row(
            "max_file_bytes", CLASS_BLOCKED, knowledge_status=mfb_ks, helper_support=mfb_hs,
            message="Conflicting max_file_bytes blocks planning.",
            evidence=mfb_evid,
        ))
        blockers.append({"id": "max_file_bytes", "message": classifications[-1]["message"]})
    else:
        classifications.append(_classification_row(
            "max_file_bytes", CLASS_MANUAL, knowledge_status=mfb_ks, helper_support=mfb_hs,
            message=f"max_file_bytes knowledge_status={mfb_ks}",
            evidence=mfb_evid,
        ))
        manual_checks.append({"id": "max_file_bytes", "message": classifications[-1]["message"]})

    # --- image_count ---
    ic = reqs["image_count"]
    ic_ks, ic_hs = ic["knowledge_status"], ic["helper_support"]
    ic_evid = _evidence_payload(ic)
    rule = ic.get("rule")
    if ic_ks in ("known", "explicitly_unrestricted") and rule in ("limited", "unlimited"):
        messages = [f"Approved artwork count for this plan: {n}."]
        ok = True
        if rule == "limited":
            if ic.get("min") is not None and n < ic["min"]:
                ok = False
                messages.append(f"Below organiser min={ic['min']}.")
            if ic.get("max") is not None and n > ic["max"]:
                ok = False
                messages.append(f"Above organiser max={ic['max']}.")
        elif rule == "unlimited" and ic.get("min") is not None and n < ic["min"]:
            ok = False
            messages.append(f"Below organiser min={ic['min']} (unlimited max).")
        if ok:
            classifications.append(_classification_row(
                "image_count", CLASS_AUTOMATIC, knowledge_status=ic_ks, helper_support=ic_hs,
                message=" ".join(messages) + " Count is within established organiser bounds (eligibility mismatch is separate from package planning).",
                evidence=ic_evid,
            ))
        else:
            # Known selection-count violation on this approved manifest is a production-plan
            # blocker. Distinct from applicant eligibility mismatch; planner does not select
            # which works to drop/add.
            classifications.append(_classification_row(
                "image_count", CLASS_BLOCKED, knowledge_status=ic_ks, helper_support=ic_hs,
                message=(
                    " ".join(messages)
                    + " Approved selection violates organiser count bounds; supply a different "
                    "approved manifest. This planner never selects, drops, or adds artworks."
                ),
                evidence=ic_evid,
            ))
            blockers.append({
                "id": "image_count_selection_violation",
                "message": classifications[-1]["message"],
                "evidence": ic_evid,
            })
    elif ic_ks in ("not_stated", "inaccessible"):
        classifications.append(_classification_row(
            "image_count", CLASS_UNRESOLVED, knowledge_status=ic_ks, helper_support=ic_hs,
            message=f"image_count knowledge_status={ic_ks}",
            evidence=ic_evid,
        ))
        unresolved.append({"id": "image_count", "message": classifications[-1]["message"]})
    elif ic_ks == "not_required":
        classifications.append(_classification_row(
            "image_count", CLASS_NA, knowledge_status=ic_ks, helper_support=ic_hs,
            message="No image-count requirement.",
            evidence=ic_evid,
        ))
    elif ic_ks == "conflicting":
        classifications.append(_classification_row(
            "image_count", CLASS_BLOCKED, knowledge_status=ic_ks, helper_support=ic_hs,
            message="Conflicting image_count blocks planning.",
            evidence=ic_evid,
        ))
        blockers.append({"id": "image_count", "message": classifications[-1]["message"]})
    else:
        classifications.append(_classification_row(
            "image_count", CLASS_MANUAL, knowledge_status=ic_ks, helper_support=ic_hs,
            message=f"image_count rule={rule!r} knowledge_status={ic_ks}",
            evidence=ic_evid,
        ))
        manual_checks.append({"id": "image_count", "message": classifications[-1]["message"]})

    # --- fee_schedule ---
    fee = reqs["fee_schedule"]
    fee_ks, fee_hs = fee["knowledge_status"], fee["helper_support"]
    fee_evid = _evidence_payload(fee)
    if fee_ks == "known":
        fee_detail: dict[str, Any] = {"currency_raw": fee.get("currency_raw"), "currency_code": fee.get("currency_code")}
        try:
            calc = calculate_fee(fee, n, selected_optional_ids=applicant.get("selected_optional_charges"))
            fee_detail["calculated"] = _jsonable(calc)
        except Exception as exc:  # noqa: BLE001 — surface as manual
            fee_detail["calculate_error"] = str(exc)
        classifications.append(_classification_row(
            "fee_schedule", CLASS_MANUAL, knowledge_status=fee_ks, helper_support=fee_hs,
            message="Fee schedule recorded for human payment decision; helper never pays.",
            evidence=fee_evid,
        ))
        manual_checks.append({
            "id": "fee_schedule",
            "message": classifications[-1]["message"],
            "evidence": fee_evid,
            "detail": fee_detail,
        })
    elif fee_ks == "not_required":
        classifications.append(_classification_row(
            "fee_schedule", CLASS_NA, knowledge_status=fee_ks, helper_support=fee_hs,
            message="No entry fee required.",
            evidence=fee_evid,
        ))
    elif fee_ks in ("not_stated", "inaccessible"):
        classifications.append(_classification_row(
            "fee_schedule", CLASS_UNRESOLVED, knowledge_status=fee_ks, helper_support=fee_hs,
            message=f"fee_schedule knowledge_status={fee_ks}",
            evidence=fee_evid,
        ))
        unresolved.append({"id": "fee_schedule", "message": classifications[-1]["message"]})
    else:
        classifications.append(_classification_row(
            "fee_schedule", CLASS_MANUAL, knowledge_status=fee_ks, helper_support=fee_hs,
            message=f"fee_schedule knowledge_status={fee_ks}",
            evidence=fee_evid,
        ))
        manual_checks.append({"id": "fee_schedule", "message": classifications[-1]["message"]})

    # --- watermark ---
    if _watermark_mentioned(opportunity):
        classifications.append(_classification_row(
            "no_watermark", CLASS_MANUAL, knowledge_status="known", helper_support="manual",
            message="No-watermark requirement remains MANUAL (no reliable non-destructive checker).",
        ))
        manual_checks.append({
            "id": "no_watermark",
            "message": "Visually confirm images have no watermark before prepare-draft.",
        })
    else:
        classifications.append(_classification_row(
            "no_watermark", CLASS_NA, message="No watermark requirement detected in unresolved/evidence text.",
        ))

    # --- rights / theme / AI / prior exhibition from unresolved ---
    for item in opportunity.get("unresolved_requirements") or []:
        if not isinstance(item, str):
            continue
        if _THEME_RE.search(item) or "rights" in item.lower() or "AI" in item or "prior exhibition" in item.lower():
            manual_checks.append({
                "id": "unresolved_human_review",
                "message": item,
            })

    rights = reqs["rights_clause"]
    rights_evid = _evidence_payload(rights)
    if rights["knowledge_status"] == "known":
        classifications.append(_classification_row(
            "rights_clause", CLASS_MANUAL, knowledge_status=rights["knowledge_status"],
            helper_support=rights["helper_support"],
            message="Rights clause always requires human legal/rights review.",
            evidence=rights_evid,
        ))
        manual_checks.append({
            "id": "rights_clause",
            "message": "Human legal/rights review of rights_clause before any submission.",
            "evidence": rights_evid,
        })
    elif rights["knowledge_status"] in ("not_stated", "inaccessible"):
        classifications.append(_classification_row(
            "rights_clause", CLASS_UNRESOLVED, knowledge_status=rights["knowledge_status"],
            helper_support=rights["helper_support"],
            message=f"rights_clause knowledge_status={rights['knowledge_status']}",
            evidence=rights_evid,
        ))
        unresolved.append({"id": "rights_clause", "message": classifications[-1]["message"]})
    else:
        classifications.append(_classification_row(
            "rights_clause", CLASS_NA if rights["knowledge_status"] in ("not_required", "explicitly_unrestricted") else CLASS_MANUAL,
            knowledge_status=rights["knowledge_status"], helper_support=rights["helper_support"],
            message=f"rights_clause knowledge_status={rights['knowledge_status']}",
            evidence=rights_evid,
        ))

    # --- residency / media / deadline / statement / bio: planning awareness ---
    for name, default_cls in (
        ("residency", CLASS_MANUAL),
        ("media_allowed", CLASS_MANUAL),
        ("deadline", CLASS_MANUAL),
        ("statement_max_words", CLASS_MANUAL),
        ("bio_max_words", CLASS_MANUAL),
    ):
        req = reqs[name]
        ks, hs = req["knowledge_status"], req["helper_support"]
        evid = _evidence_payload(req)
        if ks in ("not_required", "explicitly_unrestricted"):
            classifications.append(_classification_row(
                name, CLASS_NA, knowledge_status=ks, helper_support=hs,
                message=f"{name} not required / unrestricted for package mechanics.",
                evidence=evid,
            ))
        elif ks in ("not_stated", "inaccessible"):
            classifications.append(_classification_row(
                name, CLASS_UNRESOLVED, knowledge_status=ks, helper_support=hs,
                message=f"{name} knowledge_status={ks}",
                evidence=evid,
            ))
            unresolved.append({"id": name, "message": classifications[-1]["message"], "evidence": evid})
        elif ks == "conflicting":
            classifications.append(_classification_row(
                name, CLASS_BLOCKED, knowledge_status=ks, helper_support=hs,
                message=f"Conflicting {name} blocks trustworthy planning.",
                evidence=evid,
            ))
            blockers.append({"id": name, "message": classifications[-1]["message"]})
        else:
            classifications.append(_classification_row(
                name, default_cls, knowledge_status=ks, helper_support=hs,
                message=f"{name} retained for human review; package plan does not certify eligibility.",
                evidence=evid,
            ))
            manual_checks.append({"id": name, "message": classifications[-1]["message"], "evidence": evid})

    # unresolved_requirements: crop/upscale/restyle → block; known handlers already
    # classified above (watermark/theme/AI/prior/rights/PPI); everything else must
    # land in unresolved so the incomplete gate sees it — never silently drop.
    _pixel_block_tokens = (
        "must crop", "require crop", "upscale required", "restyle", "artistic crop",
    )
    for item in opportunity.get("unresolved_requirements") or []:
        if not isinstance(item, str):
            continue
        low = item.lower()
        if any(tok in low for tok in _pixel_block_tokens):
            blockers.append({
                "id": "unsupported_pixel_transform",
                "message": f"Unsupported required transformation mentioned: {item}",
            })
            continue
        if _WATERMARK_RE.search(item):
            continue  # already manual via no_watermark
        if (
            _THEME_RE.search(item)
            or "rights" in low
            or "AI" in item
            or "prior exhibition" in low
        ):
            continue  # already manual via unresolved_human_review
        if _PPI_RE.search(item):
            continue  # already handled by output_ppi automatic/unresolved paths
        # Generic organiser mechanic (e.g. portal max files/bytes) — must surface
        unresolved.append({
            "id": "unresolved_organiser_mechanic",
            "message": item,
        })
        classifications.append(_classification_row(
            "unresolved_organiser_mechanic",
            CLASS_UNRESOLVED,
            knowledge_status="not_stated",
            helper_support="unsupported",
            message=item,
        ))

    # Organiser facts retained with evidence
    organiser_facts_retained = {}
    for name, req in reqs.items():
        organiser_facts_retained[name] = {
            "knowledge_status": req.get("knowledge_status"),
            "helper_support": req.get("helper_support"),
            "evidence": _evidence_payload(req),
            "value_summary": _value_summary(name, req),
        }

    if blockers:
        status = "blocked"
        summary_extra = f"Blocked by {len(blockers)} gate(s)."
    elif unresolved:
        # Unresolved required mechanics (e.g. portal max_file_bytes) → incomplete,
        # not plan_ready. Manual checks alone may still yield plan_ready with review.
        status = "incomplete"
        summary_extra = (
            f"Incomplete: {len(unresolved)} unresolved organiser requirement(s) remain; "
            f"not ready for prepare-draft. "
            f"{len(automatic_actions)} provisional automatic action(s), "
            f"{len(manual_checks)} manual check(s)."
        )
    else:
        status = "plan_ready"
        summary_extra = (
            f"Plan ready with {len(automatic_actions)} automatic action(s), "
            f"{len(manual_checks)} manual check(s)."
        )

    return _finalize(
        opportunity, status=status, classifications=classifications,
        automatic_actions=automatic_actions, manual_checks=manual_checks,
        blockers=blockers, unresolved=unresolved, tool_limitations=tool_limitations,
        artwork_rows=artwork_rows, applicant=applicant,
        organiser_facts_retained=organiser_facts_retained,
        assessment=assessment,
        summary_extra=summary_extra,
    )


def _value_summary(name: str, req: dict) -> Any:
    if name == "deadline":
        return {
            "deadline_date": req.get("deadline_date"),
            "deadline_time": req.get("deadline_time"),
            "deadline_timezone": req.get("deadline_timezone"),
            "deadline_datetime": req.get("deadline_datetime"),
        }
    if name == "fee_schedule":
        return {
            "base_fee": req.get("base_fee"),
            "images_included": req.get("images_included"),
            "additional_image_fee": req.get("additional_image_fee"),
            "currency_raw": req.get("currency_raw"),
            "currency_code": req.get("currency_code"),
        }
    if name == "image_count":
        return {"rule": req.get("rule"), "min": req.get("min"), "max": req.get("max")}
    if name == "max_file_bytes":
        return {
            "value": req.get("value"),
            "declared_label": req.get("declared_label"),
        }
    return req.get("value")


def _finalize(
    opportunity: dict,
    *,
    status: str,
    classifications: list,
    automatic_actions: list,
    manual_checks: list,
    blockers: list,
    unresolved: list,
    tool_limitations: list,
    artwork_rows: list,
    applicant: dict,
    organiser_facts_retained: dict | None = None,
    assessment: dict | None = None,
    summary_extra: str = "",
) -> dict:
    manual_checks = _normalize_manual_checks(manual_checks)
    lines = [
        "# Preparation plan (read-only)",
        "",
        f"**Status:** `{status}`",
        f"**Opportunity:** {opportunity.get('title', opportunity.get('id'))}",
        f"**Approved artworks planned:** {len(artwork_rows)}",
        "",
        "This plan does **not** execute prepare, modify source files, submit, or pay.",
        "",
    ]
    if summary_extra:
        lines.extend([summary_extra, ""])
    if automatic_actions:
        lines.append("## Automatic actions")
        for act in automatic_actions:
            lines.append(f"- `{act['id']}` — {act['operation']} ({', '.join(act['artwork_ids']) or 'n/a'})")
        lines.append("")
    if manual_checks:
        lines.append("## Manual checks")
        for item in manual_checks[:20]:
            lines.append(f"- {item.get('id')}: {item.get('message')}")
        if len(manual_checks) > 20:
            lines.append(f"- … and {len(manual_checks) - 20} more")
        lines.append("")
    if unresolved:
        lines.append("## Unresolved source / knowledge")
        for item in unresolved:
            lines.append(f"- {item.get('id')}: {item.get('message')}")
        lines.append("")
    if blockers:
        lines.append("## Blockers")
        for item in blockers:
            lines.append(f"- {item.get('id')}: {item.get('message')}")
        lines.append("")
    if tool_limitations:
        lines.append("## Tool limitations (not organiser rules)")
        for item in tool_limitations:
            lines.append(f"- {item.get('id')}: {item.get('message')}")
        lines.append("")

    outstanding_prepare_gates = [
        m for m in manual_checks if m.get("blocks_prepare")
    ]
    ready_for_prepare = (
        status == "plan_ready"
        and not unresolved
        and not blockers
        and not outstanding_prepare_gates
    )
    assessment_summary = None
    if assessment is not None:
        assessment_summary = {
            "status": assessment.get("status"),
            "eligibility_decision": assessment.get("eligibility_decision"),
            "assessed_at": assessment.get("assessed_at"),
            "unusable_sources": assessment.get("unusable_sources"),
            "next_action": assessment.get("next_action"),
        }
    return {
        "schema_version": 1,
        "command": "plan-package",
        "status": status,
        "ready_for_prepare": ready_for_prepare,
        "demo_only": bool(opportunity.get("demo_only")),
        "opportunity_id": opportunity.get("id"),
        "opportunity_title": opportunity.get("title"),
        "summary_markdown": "\n".join(lines),
        "classifications": classifications,
        "automatic_actions": automatic_actions,
        "manual_checks": manual_checks,
        "blockers": blockers,
        "unresolved": unresolved,
        "tool_limitations": tool_limitations,
        "artworks": artwork_rows,
        "applicant_fields_used": sorted(k for k in applicant if k in ("first_name", "last_name", "residence_country", "fee_budget", "selected_optional_charges", "media", "image_count")),
        "organiser_facts_retained": organiser_facts_retained or {},
        "assess_call": assessment_summary,
        "safety": {
            "executes_prepare": False,
            "modifies_sources": False,
            "overwrites_originals": False,
            "upscale_allowed": False,
            "crop_allowed": False,
            "restyle_allowed": False,
            "submission_automation": False,
            "payment_automation": False,
        },
        "executes_prepare": False,
        "modifies_sources": False,
        "human_review_required": True,
        "next_milestone": "prepare-draft / validate-draft (v2 draft production)",
        "note": (
            "Read-only plan. Successful plan_ready is not eligibility, rights clearance, "
            "or permission to submit. Blocked production action ≠ eligibility mismatch. "
            "Unresolved organiser mechanics yield incomplete / ready_for_prepare=false. "
            "Outstanding prepare-blocking manuals (fee/rights/watermark/colour/deadline/media) keep ready_for_prepare=false; informational manuals do not."
        ),
    }


def exit_code_for_plan_package(result: dict) -> int:
    """Map plan-package result to process exit code.

    0 — plan_ready (no unresolved required mechanics; manuals may still keep ready_for_prepare false)
    2 — blocked (cannot plan under safety / assess-call known_mismatch gates)
    3 — incomplete (unresolved organiser mechanics or assess-call trust/freshness gaps)
    1 — tool/input error (raised as exception before this mapping)
    """
    status = result.get("status")
    if status == "blocked":
        return 2
    if status == "incomplete":
        return 3
    if status == "plan_ready":
        return 0
    return 1
