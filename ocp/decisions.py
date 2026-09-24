"""Private prepare-draft decision records bound to a stable plan fingerprint.

Acknowledgements authorize prepare action but must never invent organiser facts,
force-pass mismatches, invent upload limits, or rewrite knowledge_status.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .io import ProducerError, load_json, write_json

# Material requirement ids whose knowledge/value changes invalidate approvals.
MATERIAL_REQUIREMENT_IDS = (
    "allowed_formats",
    "min_long_edge_px",
    "max_long_edge_px",
    "max_file_bytes",
    "filename_rule",
    "image_count",
    "statement_max_words",
    "bio_max_words",
    "media_allowed",
    "deadline",
    "fee_schedule",
    "rights_clause",
    "residency",
)

# Automatic-action detail keys that affect execution identity.
_ACTION_PARAM_KEYS = (
    "max_long_edge_px",
    "max_file_bytes",
    "ppi",
    "planned_filenames",
    "adapter",
    "colour_compatible_ids",
    "untagged_manual_ids",
    "already_within_bound",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _requirement_slice(req: dict | None) -> dict:
    if not isinstance(req, dict):
        return {"knowledge_status": None, "helper_support": None, "value": None}
    slice_: dict[str, Any] = {
        "knowledge_status": req.get("knowledge_status"),
        "helper_support": req.get("helper_support"),
    }
    # Preserve value-like fields without volatile evidence quotes drifting hash
    # when quotes are unchanged — include value / schedule fields only.
    for key in (
        "value",
        "rule",
        "min",
        "max",
        "base_fee",
        "images_included",
        "additional_image_fee",
        "currency_code",
        "currency_raw",
        "deadline_date",
        "deadline_time",
        "deadline_timezone",
        "deadline_datetime",
        "optional_charges",
    ):
        if key in req:
            slice_[key] = req[key]
    return slice_


def _source_hashes(opportunity: dict) -> list[dict]:
    rows = []
    primary = opportunity.get("source") or {}
    if primary:
        rows.append({
            "source_id": primary.get("source_id"),
            "snapshot_sha256": primary.get("snapshot_sha256"),
        })
    for extra in opportunity.get("additional_sources") or []:
        if isinstance(extra, dict):
            rows.append({
                "source_id": extra.get("source_id"),
                "snapshot_sha256": extra.get("snapshot_sha256"),
            })
    rows.sort(key=lambda r: (r.get("source_id") or "", r.get("snapshot_sha256") or ""))
    return rows


def _action_identity(action: dict) -> dict:
    detail = action.get("detail") or {}
    params = {k: detail[k] for k in _ACTION_PARAM_KEYS if k in detail}
    return {
        "id": action.get("id"),
        "operation": action.get("operation"),
        "artwork_ids": list(action.get("artwork_ids") or []),
        "params": params,
    }


def _packaged_text_identity(opportunity: dict, applicant: dict | None) -> dict:
    """Exact statement/bio strings prepare-draft would package (or null).

    Mirrors draft_v2._text_plan emission rules without raising on missing/over-limit
    so the fingerprint still binds content that would be written when present.
    """
    applicant = applicant or {}
    reqs = opportunity.get("requirements") or {}
    stmt_req = reqs.get("statement_max_words") or {}
    bio_req = reqs.get("bio_max_words") or {}
    statement = applicant.get("statement")
    bio = applicant.get("approved_bio")
    stmt_ks = stmt_req.get("knowledge_status")
    bio_ks = bio_req.get("knowledge_status")

    exported_statement = None
    if stmt_ks == "known":
        if isinstance(statement, str) and statement.strip():
            exported_statement = statement.rstrip() + '\n'
    elif isinstance(statement, str) and statement.strip() and stmt_ks not in (None, "not_required"):
        exported_statement = statement.rstrip() + '\n'

    exported_biography = None
    if bio_ks == "known":
        if isinstance(bio, str) and bio.strip():
            exported_biography = bio.rstrip() + '\n'
    elif isinstance(bio, str) and bio.strip() and bio_ks not in (None, "not_required"):
        exported_biography = bio.rstrip() + '\n'

    return {
        "artist-statement.txt": exported_statement,
        "artist-bio.txt": exported_biography,
    }


def plan_fingerprint_payload(
    plan: dict,
    opportunity: dict,
    *,
    applicant: dict | None = None,
    processing: dict | None = None,
) -> dict:
    """Stable execution identity used for execute approval.

    MUST include opportunity id, source snapshot hashes, material requirements,
    artwork ids/paths/submission_order/source hashes + title/year/medium,
    automatic_action ids+operations+key params, filename adapter, packaged
    statement/biography text, and material processing decisions
    (untagged_colour_policy).

    MUST NOT include volatile timestamps (assessed_at, wall-clock, approved_at).
    """
    reqs = opportunity.get("requirements") or {}
    material = {name: _requirement_slice(reqs.get(name)) for name in MATERIAL_REQUIREMENT_IDS}

    artworks = []
    for row in plan.get("artworks") or []:
        source = row.get("source") or {}
        artworks.append({
            "id": row.get("id"),
            "path": row.get("path"),
            "submission_order": row.get("submission_order"),
            "source_sha256": source.get("sha256"),
            "title": row.get("title"),
            "year": row.get("year"),
            "medium": row.get("medium"),
        })
    artworks.sort(key=lambda a: a.get("submission_order") or 0)

    actions = [_action_identity(a) for a in (plan.get("automatic_actions") or [])]
    actions.sort(key=lambda a: a.get("id") or "")

    filename_adapter = None
    for action in plan.get("automatic_actions") or []:
        detail = action.get("detail") or {}
        if detail.get("adapter"):
            filename_adapter = detail["adapter"]
            break

    proc = processing or {}
    return {
        "opportunity_id": plan.get("opportunity_id") or opportunity.get("id"),
        "source_snapshot_hashes": _source_hashes(opportunity),
        "material_requirements": material,
        "artworks": artworks,
        "automatic_actions": actions,
        "filename_adapter": filename_adapter,
        "submission_text": _packaged_text_identity(opportunity, applicant),
        "processing": {
            "untagged_colour_policy": proc.get("untagged_colour_policy"),
        },
    }


def compute_plan_fingerprint(
    plan: dict,
    opportunity: dict,
    *,
    applicant: dict | None = None,
    processing: dict | None = None,
) -> str:
    """SHA-256 of the stable execution identity (plan + text + processing)."""
    payload = plan_fingerprint_payload(
        plan, opportunity, applicant=applicant, processing=processing,
    )
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return digest


def compute_execution_fingerprint(
    plan: dict,
    opportunity: dict,
    *,
    applicant: dict | None = None,
    processing: dict | None = None,
) -> str:
    """Preferred alias for compute_plan_fingerprint (execution-bound identity)."""
    return compute_plan_fingerprint(
        plan, opportunity, applicant=applicant, processing=processing,
    )

def load_decisions(path: Path) -> dict:
    data = load_json(path, schema="prepare_decisions")
    return data


def write_decisions(path: Path, data: dict) -> None:
    write_json(path, data)


def decisions_match_fingerprint(decisions: dict, fingerprint: str) -> bool:
    if decisions.get("plan_fingerprint") != fingerprint:
        return False
    execute = decisions.get("execute_approval") or {}
    if execute.get("plan_fingerprint") != fingerprint:
        return False
    return True


def has_execute_approval(decisions: dict, fingerprint: str) -> bool:
    execute = decisions.get("execute_approval") or {}
    return bool(
        execute.get("approved") is True
        and execute.get("plan_fingerprint") == fingerprint
        and decisions.get("plan_fingerprint") == fingerprint
    )


def forbidden_acknowledgement_claims(ack: dict) -> list[str]:
    """Acknowledgements must not invent organiser facts or force-pass."""
    problems = []
    # Reject any keys that would smuggle invented organiser evidence.
    banned = (
        "invented_knowledge_status",
        "force_pass",
        "invented_max_file_bytes",
        "invented_upload_limit",
        "rewrite_knowledge_status",
        "not_required_override",
        "verified_organiser_evidence",
    )
    for key in banned:
        if key in ack and ack[key]:
            problems.append(f"acknowledgement claims forbidden field {key}")
    note = ack.get("note") or ""
    lowered = note.lower()
    if "force-pass" in lowered or "force_pass" in lowered:
        problems.append("acknowledgement note claims force-pass")
    if "invent" in lowered and ("limit" in lowered or "fee" in lowered or "not_required" in lowered):
        problems.append("acknowledgement note appears to invent organiser facts")
    return problems


def acknowledged_manual_ids(decisions: dict) -> set[str]:
    ids: set[str] = set()
    for ack in decisions.get("manual_acknowledgements") or []:
        if not isinstance(ack, dict):
            continue
        if ack.get("acknowledged") is True and ack.get("manual_id"):
            problems = forbidden_acknowledgement_claims(ack)
            if problems:
                raise ProducerError(
                    "Manual acknowledgement invents facts or force-passes: "
                    + "; ".join(problems)
                )
            ids.add(str(ack["manual_id"]))
    return ids
