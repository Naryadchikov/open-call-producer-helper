"""Read-only assessment of a version-2 opportunity against optional applicant facts.

Does not prepare packages, invent eligibility badges, or require artwork/profile.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .io import ProducerError, checked_root, load_json, local_path, sha256, utc_time

INTERNAL_CAPACITY = 30
_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")

# Knowledge statuses that assert a modelled value with evidence.
_VALUE_STATUSES = frozenset({"known", "explicitly_unrestricted", "not_required"})

# Source kinds allowed to establish organiser requirements (not merely context).
# demo is further gated by demo_only in origin / evidence checks.
# secondary_aggregator / access_blocker_evidence / other are contextual only.
AUTHORITATIVE_SOURCE_KINDS = frozenset({
    "original_page",
    "user_supplied_copy",
    "application_instructions",
    "organiser_listing",
    "organiser_services",
    "demo",
})

APPLICANT_ALLOWED_KEYS = frozenset({
    "residence_country",
    "fee_budget",
    "image_count",
    "selected_optional_charges",
    "media",
})

# Controlled vocabulary for hard media mismatch decisions. Labels outside this
# set (after casefold/strip/synonym resolve) never produce known_hard_mismatch.
MEDIA_VOCAB_IDS = frozenset({
    "photography",
    "video",
    "sculpture",
    "installation",
    "painting",
    "drawing",
    "mixed_media",
    "other",
})

# True synonyms → same hard-comparison ID (explicit same meaning).
MEDIA_VOCAB_SYNONYMS = {
    "photo": "photography",
    "photograph": "photography",
    "photographs": "photography",
    "photos": "photography",
}

# Narrow / child labels under a broader vocab category. These are recognized for
# taxonomy review but must NOT drive hard reject against the parent category.
MEDIA_VOCAB_CHILD_OF = {
    "digital_photography": "photography",
    "digital photography": "photography",
    "analogue_photography": "photography",
    "analog_photography": "photography",
    "analogue photography": "photography",
    "analog photography": "photography",
}

# Generic mirrorable requirements that may legitimately be explicitly_unrestricted.
# Values compatible with unrestricted status (else contradiction → review).
def _unrestricted_value_ok(name: str, value: Any) -> bool:
    if name == "allowed_formats":
        return value is None or (isinstance(value, list) and value == ["*"])
    if name == "filename_rule":
        return value is None or value == "any"
    if name in (
        "min_long_edge_px", "max_long_edge_px", "max_file_bytes",
        "statement_max_words", "bio_max_words", "rights_clause",
    ):
        return value is None
    return value is None


_UNUSABLE_REASON_LABELS = {
    "integrity_failure": "snapshot integrity failure",
    "demo_origin": "demo origin while demo_only=false",
    "missing_required_url": "missing required organiser URL",
    "non_authoritative_kind": "non-authoritative/contextual source kind",
    "stale_or_future_capture": "capture freshness (future-dated or older than 72 hours)",
}


def _normalize_media_token(label: str) -> str:
    # Collapse internal whitespace; keep spaces so child keys can match either form.
    return " ".join(str(label).strip().casefold().split())


def _resolve_media_token(token: str) -> tuple[str | None, str | None]:
    """Resolve a normalized token to (vocab_id, parent_id).

    - (id, None) for hard vocab IDs / synonyms
    - (None, parent) for known child/narrow labels under a parent category
    - (None, None) for unknown free-form labels
    """
    if not token:
        return None, None
    underscored = token.replace(" ", "_")
    if token in MEDIA_VOCAB_IDS:
        return token, None
    if underscored in MEDIA_VOCAB_IDS:
        return underscored, None
    syn = MEDIA_VOCAB_SYNONYMS.get(token) or MEDIA_VOCAB_SYNONYMS.get(underscored)
    if syn is not None:
        return syn, None
    parent = MEDIA_VOCAB_CHILD_OF.get(token) or MEDIA_VOCAB_CHILD_OF.get(underscored)
    if parent is not None:
        return None, parent
    return None, None


def _media_vocab_projection(
    labels: list[str],
) -> tuple[set[str] | None, list[str], list[tuple[str, str]]]:
    """Project labels onto MEDIA_VOCAB_IDS with synonym/child awareness.

    Returns (id_set, unknown_original_labels, child_labels).
    - id_set is None when any free-form unknown is present (hard mismatch unsafe).
    - child_labels are (original_label, parent_id) for recognized narrow media;
      callers must review parent/child relationships rather than hard-reject.
    """
    ids: set[str] = set()
    unknown: list[str] = []
    children: list[tuple[str, str]] = []
    for label in labels:
        raw = label if isinstance(label, str) else str(label)
        token = _normalize_media_token(raw)
        if not token:
            continue
        vocab_id, parent_id = _resolve_media_token(token)
        if vocab_id is not None:
            ids.add(vocab_id)
        elif parent_id is not None:
            children.append((raw, parent_id))
        else:
            unknown.append(raw)
    if unknown:
        return None, unknown, children
    return ids, [], children


def all_sources(call: dict) -> list[dict]:
    return [call["source"], *call.get("additional_sources", [])]


def source_index(call: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for source in all_sources(call):
        sid = source["source_id"]
        if sid in index:
            raise ProducerError(f"Duplicate source_id: {sid}")
        index[sid] = source
    return index


def load_opportunity_document(path: Path) -> dict:
    """Load and schema-validate an opportunity; detect version via schema_version."""
    return load_json(path, "opportunity")


def load_applicant(path: Path | None) -> dict:
    if path is None:
        return {}
    data = load_json(path)
    unknown = set(data) - APPLICANT_ALLOWED_KEYS
    if unknown:
        raise ProducerError(f"Unsupported applicant keys: {sorted(unknown)}")
    if "residence_country" in data and data["residence_country"] is not None:
        value = data["residence_country"]
        if not isinstance(value, str) or not _COUNTRY_CODE_RE.fullmatch(value):
            raise ProducerError(
                "applicant.residence_country must be a two-letter uppercase ISO-style code or null"
            )
    if "fee_budget" in data and data["fee_budget"] is not None:
        budget = data["fee_budget"]
        if not isinstance(budget, dict):
            raise ProducerError("applicant.fee_budget must be an object")
        if set(budget) - {"amount", "currency"}:
            raise ProducerError("applicant.fee_budget may only contain amount and currency")
        if "amount" in budget and (
            isinstance(budget["amount"], bool)
            or not isinstance(budget["amount"], (int, float))
            or budget["amount"] < 0
        ):
            raise ProducerError("applicant.fee_budget.amount must be a nonnegative number")
        if "currency" in budget and budget["currency"] is not None:
            cur = budget["currency"]
            if not isinstance(cur, str) or not _CURRENCY_CODE_RE.fullmatch(cur):
                raise ProducerError("applicant.fee_budget.currency must be ISO 4217 or null")
    if "image_count" in data and data["image_count"] is not None:
        n = data["image_count"]
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ProducerError("applicant.image_count must be a positive integer")
    if "selected_optional_charges" in data and data["selected_optional_charges"] is not None:
        ids = data["selected_optional_charges"]
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise ProducerError("applicant.selected_optional_charges must be a string list")
    if "media" in data and data["media"] is not None:
        media = data["media"]
        if not isinstance(media, list) or not all(isinstance(x, str) for x in media):
            raise ProducerError("applicant.media must be a string list")
        # Empty list is absent/unknown — never a positive media match.
        if len(media) == 0:
            data = dict(data)
            data.pop("media", None)
        else:
            if any(not x.strip() for x in media):
                raise ProducerError("applicant.media entries must be non-empty strings")
            if len(set(media)) != len(media):
                raise ProducerError("applicant.media must contain unique values")
    return data


def _to_decimal(value: Any) -> Decimal:
    """Parse a monetary amount via str() to avoid binary float artifacts."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProducerError(f"Invalid monetary amount: {value!r}") from exc


def _json_money(value: Decimal | None) -> int | float | None:
    """Stable JSON number for a Decimal money value (no binary float math)."""
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)
    # Emit via decimal text so JSON sees the exact decimal digits, not binary float noise.
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if "." not in text:
        return int(text)
    return float(text)


def _optional_charge_amount(
    charge: dict, image_count: int
) -> tuple[Decimal | None, str | None]:
    """Resolve a selected optional charge amount.

    Returns (amount, unresolved_reason). amount is None when the charge cannot
    be priced machine-readably (never invent conditions from description text).

    Supported condition kinds:
    - complimentary_at_or_above_image_count: charge amount applies when
      image_count < threshold; at/above threshold the selected charge is $0.
    - unresolved: selected charge blocks machine total (manual review).
    """
    condition = charge.get("condition")
    if condition is None:
        return _to_decimal(charge["amount"]), None
    if not isinstance(condition, dict):
        return None, f"optional charge {charge['id']}: condition must be an object or null"
    kind = condition.get("kind")
    if kind == "unresolved":
        return None, (
            f"optional charge {charge['id']}: pricing condition unresolved; "
            "total not machine-calculable"
        )
    if kind == "complimentary_at_or_above_image_count":
        threshold = condition.get("threshold")
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            return None, (
                f"optional charge {charge['id']}: "
                "complimentary_at_or_above_image_count needs positive integer threshold"
            )
        if image_count < threshold:
            return _to_decimal(charge["amount"]), None
        return Decimal("0"), None
    return None, (
        f"optional charge {charge['id']}: unsupported condition kind {kind!r}; "
        "total not machine-calculable"
    )


def calculate_fee(schedule: dict, image_count: int,
                  selected_optional_ids: list[str] | None = None) -> dict:
    """Compute fee for N images from a fee_schedule requirement.

    Optional charges are included only when explicitly selected by the caller.
    Conditionally priced optionals use machine-readable `condition` only — never
    inferred from description. Unsupported/unresolved conditions make the total
    non-calculable (manual) rather than a hard budget mismatch.
    Returns calculation/currency capability flags — never an affirmative budget
    approval. Budget affordability belongs to assess_call after an explicit budget.
    """
    selected = set(selected_optional_ids if selected_optional_ids is not None else [])
    known_ids = {c["id"] for c in schedule.get("optional_charges", [])}
    unknown_selected = sorted(selected - known_ids)
    if unknown_selected:
        raise ProducerError(f"Unknown optional charge ids: {unknown_selected}")

    base = schedule.get("base_fee")
    included = schedule.get("images_included")
    additional = schedule.get("additional_image_fee")
    can_compute = (
        schedule.get("knowledge_status") in _VALUE_STATUSES
        and schedule.get("knowledge_status") != "not_required"
        and base is not None
        and included is not None
        and additional is not None
        and image_count >= 1
    )
    total: Decimal | None = None
    extras: Decimal | None = None
    optional_applied: list[dict] = []
    condition_blockers: list[str] = []
    if can_compute:
        extras = _to_decimal(max(0, image_count - included)) * _to_decimal(additional)
        total = _to_decimal(base) + extras
        for charge in schedule.get("optional_charges", []):
            if charge["id"] not in selected:
                continue
            amount, blocker = _optional_charge_amount(charge, image_count)
            if blocker:
                condition_blockers.append(blocker)
                continue
            assert amount is not None
            total += amount
            optional_applied.append({
                "id": charge["id"],
                "amount_applied": _json_money(amount),
                "condition": charge.get("condition"),
            })
        if condition_blockers:
            can_compute = False
            total = None

    currency_code = schedule.get("currency_code")
    currency_known = currency_code is not None
    if condition_blockers:
        note = (
            "Selected optional charge has unresolved/unsupported pricing condition; "
            "fee total not machine-calculable (manual review) — not a budget mismatch"
        )
    elif not currency_known:
        note = "Currency code unverified; schedule preserved but affordability cannot be decided"
    elif can_compute:
        note = "Fee computed from stated schedule"
    else:
        note = "Fee schedule incomplete"
    return {
        "image_count": image_count,
        "base_fee": _json_money(_to_decimal(base)) if base is not None else None,
        "images_included": included,
        "additional_units": max(0, image_count - included) if included is not None else None,
        "additional_image_fee": (
            _json_money(_to_decimal(additional)) if additional is not None else None
        ),
        "additional_subtotal": _json_money(extras),
        "optional_charges_included": sorted(selected),
        "optional_charges_applied": optional_applied if can_compute else [],
        "condition_blockers": condition_blockers,
        "total": _json_money(total) if can_compute else None,
        "total_decimal": total if can_compute else None,  # internal; stripped before JSON output
        "currency_raw": schedule.get("currency_raw"),
        "currency_code": currency_code,
        "schedule_preserved": True,
        "fee_total_calculable": can_compute,
        "currency_code_known": currency_known,
        "note": note,
    }


def _resolve_root(workspace: Path | None, opportunity_path: Path) -> Path:
    if workspace is not None:
        return checked_root(workspace)
    parent = opportunity_path.parent
    return checked_root(parent)


def _load_snapshot_texts(call: dict, root: Path) -> dict[str, tuple[dict, str, bool]]:
    """Return source_id -> (source, text, integrity_ok)."""
    result: dict[str, tuple[dict, str, bool]] = {}
    for source in all_sources(call):
        path = local_path(root, source["snapshot_path"])
        if path.stat().st_size > 1_000_000:
            raise ProducerError(f"Source snapshot exceeds 1 MB text limit: {source['snapshot_path']}")
        text = path.read_text(encoding="utf-8")
        integrity = sha256(path) == source["snapshot_sha256"]
        result[source["source_id"]] = (source, text, integrity)
    return result


def _evidence_payload(req: dict) -> dict | None:
    evidence = req.get("evidence")
    if not evidence:
        return None
    return {
        "source_id": evidence["source_id"],
        "quote": evidence["quote"],
        "locator": evidence["locator"],
        "literal_excerpt_integrity": "verified",
        "semantic_verification": "not_performed",
    }


def _source_metadata_map(sources: dict[str, dict]) -> dict[str, dict]:
    """Public source map without embedding snapshot bodies."""
    out: dict[str, dict] = {}
    for sid, source in sources.items():
        out[sid] = {
            "source_id": sid,
            "kind": source["kind"],
            "label": source.get("label"),
            "url": source.get("url"),
            "captured_at": source["captured_at"],
            "snapshot_path": source["snapshot_path"],
            "snapshot_sha256": source["snapshot_sha256"],
            "complete_rules_captured": source.get("complete_rules_captured", True),
        }
    return out


def _evidence_verified(
    req: dict,
    snapshots: dict[str, tuple[dict, str, bool]],
    *,
    demo_only: bool,
    unusable_sources: set[str],
) -> bool:
    evidence = req.get("evidence")
    if not evidence:
        return False
    sid = evidence.get("source_id")
    if sid not in snapshots:
        return False
    if sid in unusable_sources:
        return False
    source, text, integrity = snapshots[sid]
    kind = source.get("kind")
    if kind not in AUTHORITATIVE_SOURCE_KINDS:
        return False
    if not demo_only and kind == "demo":
        return False
    quote = evidence.get("quote") or ""
    return bool(integrity and quote and quote in text)


def _parse_tz(name: str):
    if name.upper() in ("UTC", "Z", "ETC/UTC", "GMT"):
        return timezone.utc
    if name.startswith(("+", "-")) and len(name) in (3, 5, 6):
        # Offset forms like +00:00 already handled via fromisoformat on combined string
        try:
            probe = datetime.fromisoformat(f"2000-01-01T00:00:00{name}")
            if probe.tzinfo is not None:
                return probe.tzinfo
        except ValueError:
            pass
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ProducerError(f"Unknown deadline_timezone: {name}") from exc


def _parse_deadline_time(t_raw: str) -> time:
    parts = t_raw.split(":")
    if len(parts) == 2:
        hh, mm = int(parts[0]), int(parts[1])
        ss = 0
    elif len(parts) == 3:
        hh, mm, ss = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        raise ValueError("bad time")
    return time(hh, mm, ss)


def _validate_deadline_fields(deadline: dict) -> dict:
    """Parse/validate deadline fields before publishing exact-expiry knowledge.

    Returns a summary dict plus optional 'issues' and 'instant' (UTC datetime).
    Every overlapping representation is reconciled; partial components that cannot
    be proven consistent keep exact-expiry in review.
    """
    issues: list[str] = []
    summary = {
        "deadline_date": deadline.get("deadline_date"),
        "deadline_time": deadline.get("deadline_time"),
        "deadline_timezone": deadline.get("deadline_timezone"),
        "deadline_datetime": deadline.get("deadline_datetime"),
        "exact_expiry_known": False,
    }
    instant: datetime | None = None
    component_instant: datetime | None = None
    raw_aware: datetime | None = None

    raw_dt = deadline.get("deadline_datetime")
    if raw_dt:
        try:
            instant = utc_time(raw_dt)
            normalized = raw_dt.replace("Z", "+00:00") if isinstance(raw_dt, str) else raw_dt
            raw_aware = datetime.fromisoformat(normalized)
            if raw_aware.tzinfo is None:
                issues.append("deadline_datetime is not a valid ISO timestamp with offset")
                instant = None
                raw_aware = None
        except (ProducerError, ValueError, TypeError):
            issues.append("deadline_datetime is not a valid ISO timestamp with offset")
            instant = None
            raw_aware = None

    d_raw = deadline.get("deadline_date")
    t_raw = deadline.get("deadline_time")
    tz_raw = deadline.get("deadline_timezone")
    parsed_date: date | None = None
    if d_raw:
        try:
            parsed_date = date.fromisoformat(d_raw)
        except ValueError:
            issues.append(f"deadline_date is not a valid calendar date: {d_raw}")
            parsed_date = None

    # Time without timezone cannot form or reconcile a component instant.
    if t_raw and not tz_raw:
        issues.append(
            "deadline_time without deadline_timezone cannot establish or reconcile exact expiry"
        )

    if parsed_date is not None and t_raw and tz_raw:
        try:
            local_t = _parse_deadline_time(t_raw)
            tzinfo = _parse_tz(tz_raw)
            component_instant = datetime(
                parsed_date.year, parsed_date.month, parsed_date.day,
                local_t.hour, local_t.minute, local_t.second,
                tzinfo=tzinfo,
            ).astimezone(timezone.utc)
        except (ProducerError, ValueError, TypeError):
            issues.append("deadline date/time/timezone cannot be combined into a valid instant")

    if instant is not None and component_instant is not None:
        if instant != component_instant:
            issues.append(
                "deadline_datetime contradicts deadline_date/time/timezone components"
            )
            instant = None

    # Partial overlaps: date + datetime (with or without organiser timezone).
    if instant is not None and parsed_date is not None and component_instant is None:
        try:
            if tz_raw:
                compare_tz = _parse_tz(tz_raw)
            elif raw_aware is not None and raw_aware.tzinfo is not None:
                compare_tz = raw_aware.tzinfo
            else:
                compare_tz = None
            if compare_tz is None:
                issues.append(
                    "deadline_date and deadline_datetime both present but timezone/offset "
                    "insufficient to reconcile them"
                )
                instant = None
            else:
                local_date = (raw_aware or instant).astimezone(compare_tz).date()
                if local_date != parsed_date:
                    issues.append(
                        "deadline_date contradicts deadline_datetime in its timezone/offset"
                    )
                    instant = None
        except (ProducerError, ValueError, TypeError):
            issues.append(
                "deadline_date and deadline_datetime cannot be reconciled with available timezone"
            )
            instant = None

    # time + timezone + datetime without a separate date: still reconcile local clock time.
    if instant is not None and t_raw and tz_raw and component_instant is None:
        try:
            local_t = _parse_deadline_time(t_raw)
            compare_tz = _parse_tz(tz_raw)
            local_from_dt = (raw_aware or instant).astimezone(compare_tz)
            dt_clock = local_from_dt.timetz().replace(tzinfo=None, microsecond=0)
            stated = time(local_t.hour, local_t.minute, local_t.second)
            if dt_clock != stated:
                issues.append(
                    "deadline_time/timezone contradicts deadline_datetime local clock time"
                )
                instant = None
        except (ProducerError, ValueError, TypeError):
            issues.append(
                "deadline_time/timezone cannot be reconciled with deadline_datetime"
            )
            instant = None

    # Timezone alone alongside datetime: ensure the labelled zone is usable (parseable).
    if instant is not None and tz_raw and not t_raw and parsed_date is None:
        try:
            _parse_tz(tz_raw)
        except (ProducerError, ValueError, TypeError):
            issues.append(f"deadline_timezone is not usable: {tz_raw}")
            instant = None

    if issues:
        summary["exact_expiry_known"] = False
        return {"summary": summary, "issues": issues, "instant": None, "parsed_date": parsed_date}

    if instant is not None:
        summary["exact_expiry_known"] = True
        return {"summary": summary, "issues": [], "instant": instant, "parsed_date": parsed_date}

    summary["exact_expiry_known"] = False
    return {"summary": summary, "issues": [], "instant": None, "parsed_date": parsed_date}


def _strip_internal_fee_fields(calc: dict) -> dict:
    return {k: v for k, v in calc.items() if k != "total_decimal"}


def assess_call(
    opportunity_path: Path,
    *,
    applicant_path: Path | None = None,
    workspace: Path | None = None,
    now: datetime | None = None,
) -> dict:
    opportunity_path = opportunity_path.absolute()
    if not opportunity_path.is_file():
        raise ProducerError(f"Opportunity file not found: {opportunity_path}")
    call = load_opportunity_document(opportunity_path)
    if call.get("schema_version") != 2:
        raise ProducerError(
            "assess-call requires opportunity schema_version 2. "
            "v1 workspaces use `assess --workspace` instead; migrate facts to the v2 requirements model."
        )
    applicant = load_applicant(applicant_path)
    root = _resolve_root(workspace, opportunity_path)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sources = source_index(call)
    snapshots = _load_snapshot_texts(call, root)
    reqs = call["requirements"]
    checks: list[dict] = []
    demo_only = bool(call["demo_only"])
    unusable_sources: set[str] = set()
    unusable_source_reasons: dict[str, str] = {}
    incomplete_rule_source_ids: set[str] = set()
    evidence_untrustworthy = False

    def mark_unusable(sid: str, reason: str) -> None:
        unusable_sources.add(sid)
        # Keep the first (most severe) reason if multiple apply.
        unusable_source_reasons.setdefault(sid, reason)

    def add(key: str, state: str, message: str) -> None:
        checks.append({"id": key, "status": state, "message": message})

    # --- Source integrity / freshness / origin ---
    incomplete_sources: list[dict] = []
    for sid, (source, _text, integrity) in snapshots.items():
        add(f"source_integrity:{sid}", "pass" if integrity else "fail",
            f"Snapshot hash matches for {sid}" if integrity else f"Snapshot changed or hash mismatch: {sid}")
        if not integrity:
            evidence_untrustworthy = True
            mark_unusable(sid, "integrity_failure")
        if not source.get("complete_rules_captured", True):
            add(f"source_completeness:{sid}", "review",
                f"Source {sid} complete_rules_captured is false; portal/rules may be incomplete")
            incomplete_sources.append({
                "source_id": sid,
                "reason": "complete_rules_captured=false",
                "snapshot_path": source["snapshot_path"],
            })
            incomplete_rule_source_ids.add(sid)
            # Incomplete capture: do NOT mark unusable for individually evidenced
            # facts. Facts with verified excerpts may still be established with
            # incomplete-source provenance; hard eligibility rejects are withheld.
        kind = source.get("kind")
        if kind not in AUTHORITATIVE_SOURCE_KINDS:
            add(f"source_origin:{sid}", "review",
                f"Source {sid} kind={kind} is contextual/non-authoritative; "
                "cannot establish organiser requirements "
                "(discovery/aggregator/access-blocker/other)")
            mark_unusable(sid, "non_authoritative_kind")
            # Do not set evidence_untrustworthy globally — other authoritative
            # sources may still establish facts; facts citing this source fail alone.
        elif not demo_only:
            if kind == "demo":
                add(f"source_origin:{sid}", "review",
                    f"Source {sid} is kind=demo while demo_only=false; "
                    "demo-origin evidence cannot establish real-call requirements")
                mark_unusable(sid, "demo_origin")
                evidence_untrustworthy = True
            elif kind == "original_page" and not source.get("url"):
                add(f"source_origin:{sid}", "review",
                    f"original_page source {sid} requires an organiser HTTPS URL for real-call use")
                mark_unusable(sid, "missing_required_url")
                evidence_untrustworthy = True
            # user_supplied_copy and organiser_* kinds may omit URL deliberately
        if not demo_only:
            age = (now - utc_time(source["captured_at"])).total_seconds()
            if age < -300 or age > 72 * 3600:
                add(f"source_freshness:{sid}", "review",
                    f"Recheck original terms for {sid}: future-dated or older than 72 hours")
                # Historical captures may remain on file, but must not solely drive
                # current hard rejects until recaptured/reconfirmed.
                mark_unusable(sid, "stale_or_future_capture")
    if demo_only:
        add("demo", "info", "Fictional fixture; not a real opportunity or eligibility decision")

    # --- Verify each requirement evidence ---
    established: dict[str, Any] = {}
    unsupported: list[dict] = []
    tool_limitations: list[str] = []
    missing_for_decision: list[str] = []
    applicant_matches: list[dict] = []
    applicant_mismatches: list[dict] = []
    known_hard_mismatch = False
    source_review_problems = False

    def mark_established(name: str, summary: Any, req: dict) -> None:
        entry: dict[str, Any] = {
            "knowledge_status": req["knowledge_status"],
            "helper_support": req["helper_support"],
            "summary": summary,
            "evidence": _evidence_payload(req),
        }
        sid = (req.get("evidence") or {}).get("source_id")
        if sid in incomplete_rule_source_ids:
            # Individually evidenced fact kept; capture overall is incomplete.
            entry["incomplete_source_capture"] = True
            entry["incomplete_source_id"] = sid
        established[name] = entry

    def require_evidence(name: str, req: dict, *, value_key: str | None = "value") -> bool:
        nonlocal source_review_problems
        status = req["knowledge_status"]
        if status in ("not_stated", "inaccessible", "conflicting"):
            add(f"req:{name}", "review",
                f"{name}: {status}; silence/inaccessible is not optional or unrestricted")
            if status == "inaccessible":
                incomplete_sources.append({
                    "requirement": name,
                    "reason": "inaccessible",
                })
            missing_for_decision.append(f"{name} ({status})")
            if req["helper_support"] == "unsupported":
                unsupported.append({"id": name, "reason": status, "helper_support": "unsupported"})
            return False
        ok = _evidence_verified(
            req, snapshots, demo_only=demo_only, unusable_sources=unusable_sources
        )
        if not ok:
            sid = (req.get("evidence") or {}).get("source_id")
            reason = unusable_source_reasons.get(sid) if sid else None
            if reason is None and (not demo_only and sid and
                                   snapshots.get(sid, (None,))[0] is not None and
                                   snapshots[sid][0].get("kind") == "demo"):
                reason = "demo_origin"
            if reason is not None or (sid in unusable_sources):
                reason = reason or unusable_source_reasons.get(sid, "unspecified")
                reason_label = _UNUSABLE_REASON_LABELS.get(
                    reason, reason.replace("_", " ")
                )
                add(
                    f"req:{name}",
                    "review",
                    f"{name}: evidence source unusable for real-call conclusions "
                    f"({reason_label})",
                )
                source_review_problems = True
            else:
                add(f"req:{name}", "review",
                    f"{name}: missing, changed, mismatched or wrong-source excerpt; not treated as verified")
            missing_for_decision.append(f"{name} (unverified evidence)")
            return False
        if status == "known" and value_key and req.get(value_key) is None and name not in (
            "deadline", "fee_schedule", "image_count"
        ):
            # max_file_bytes may record a verified UI label without inventing bytes.
            label = req.get("declared_label")
            if (
                name == "max_file_bytes"
                and isinstance(label, str)
                and label.strip()
            ):
                add(
                    f"req:{name}",
                    "pass",
                    f"{name}: evidence verified; declared_label={label!r} without "
                    "verified exact byte value (exact-byte planning remains unresolved)",
                )
                if req["helper_support"] == "unsupported":
                    unsupported.append({
                        "id": name,
                        "reason": "helper cannot enforce",
                        "helper_support": "unsupported",
                    })
                    tool_limitations.append(f"{name}: helper_support=unsupported")
                elif req["helper_support"] == "manual":
                    tool_limitations.append(f"{name}: requires manual/human handling")
                return True
            add(f"req:{name}", "review", f"{name}: knowledge_status=known but value is null")
            missing_for_decision.append(f"{name} (null value)")
            return False
        add(f"req:{name}", "pass", f"{name}: evidence verified against declared source snapshot")
        # Helper limitations only matter when a rule applies; not_required /
        # explicitly_unrestricted mean there is nothing for the helper to enforce.
        if status not in ("not_required", "explicitly_unrestricted"):
            if req["helper_support"] == "unsupported":
                unsupported.append({
                    "id": name,
                    "reason": "helper cannot enforce",
                    "helper_support": "unsupported",
                })
                tool_limitations.append(f"{name}: helper_support=unsupported")
            elif req["helper_support"] == "manual":
                tool_limitations.append(f"{name}: requires manual/human handling")
        return True

    def link_conclusion(bucket: list[dict], *, requirement: str, message: str,
                        req: dict | None = None, conclusion_id: str | None = None) -> None:
        entry: dict[str, Any] = {
            "id": conclusion_id or requirement,
            "requirement": requirement,
            "message": message,
        }
        if req is not None and req.get("evidence"):
            entry["evidence"] = {
                "source_id": req["evidence"]["source_id"],
                "locator": req["evidence"]["locator"],
            }
        bucket.append(entry)

    # Residency
    residency = reqs["residency"]
    res_ok = require_evidence("residency", residency)
    if res_ok and residency["knowledge_status"] == "not_required":
        # Status-first: never hard-reject from leftover value; flag contradiction for review.
        if residency.get("value") is not None:
            source_review_problems = True
            add("applicant:residency", "review",
                "residency knowledge_status=not_required is contradictory with non-null value")
            missing_for_decision.append("residency (not_required contradicts stated value)")
        else:
            mark_established("residency", {"not_required": True}, residency)
            add("applicant:residency", "info",
                "Residency not required by organiser; no residence match/mismatch checks")
    elif res_ok:
        mark_established("residency", residency["value"], residency)
        country = applicant.get("residence_country")
        allowed = residency["value"]
        if residency["knowledge_status"] == "explicitly_unrestricted" or (allowed and "*" in allowed):
            if country:
                link_conclusion(
                    applicant_matches, requirement="residency",
                    message=f"Supplied residence {country} is compatible with unrestricted/international residency",
                    req=residency,
                )
                add("applicant:residency", "pass", "Residence compatible with unrestricted residency rule")
            else:
                missing_for_decision.append("applicant.residence_country (optional confirmation)")
                add("applicant:residency", "info", "Residency unrestricted; applicant residence not supplied")
        elif allowed is not None:
            if country is None:
                missing_for_decision.append("applicant.residence_country")
                add("applicant:residency", "review", "Artist residence unknown; citizenship is not a substitute")
            elif country in allowed:
                link_conclusion(
                    applicant_matches, requirement="residency",
                    message=f"Residence {country} is in allowed list",
                    req=residency,
                )
                add("applicant:residency", "pass", "Supplied residence meets normalised residency rule")
            else:
                known_hard_mismatch = True
                link_conclusion(
                    applicant_mismatches, requirement="residency",
                    message=f"Residence {country} is not in allowed list {allowed}",
                    req=residency,
                )
                add("applicant:residency", "fail", "Supplied residence does not meet normalised residency rule")

    # Media
    media = reqs["media_allowed"]
    media_ok = require_evidence("media_allowed", media)
    if media_ok and media["knowledge_status"] == "not_required":
        if media.get("value") is not None:
            source_review_problems = True
            add("applicant:media", "review",
                "media_allowed knowledge_status=not_required is contradictory with non-null value")
            missing_for_decision.append("media_allowed (not_required contradicts stated value)")
        else:
            mark_established("media_allowed", {"not_required": True}, media)
            add("applicant:media", "info",
                "Media type not required by organiser; no applicant media demand or mismatch")
    elif media_ok and media["knowledge_status"] == "explicitly_unrestricted":
        allowed = media.get("value")
        # Unrestricted media: null or ["*"] — no hard membership checks.
        # A concrete list alongside unrestricted status is contradictory → review.
        if allowed is not None and not (isinstance(allowed, list) and allowed == ["*"]):
            source_review_problems = True
            add(
                "applicant:media",
                "review",
                "media_allowed knowledge_status=explicitly_unrestricted is contradictory "
                "with a concrete allowed-media list (use null or ['*'] for unrestricted)",
            )
            missing_for_decision.append(
                "media_allowed (explicitly_unrestricted contradicts concrete value list)"
            )
        else:
            mark_established(
                "media_allowed",
                {"explicitly_unrestricted": True, "value": allowed},
                media,
            )
            raw_media = applicant.get("media")
            supplied_media = None
            if isinstance(raw_media, list):
                seen: set[str] = set()
                normalized: list[str] = []
                for item in raw_media:
                    if not isinstance(item, str):
                        continue
                    value = item.strip()
                    if not value or value in seen:
                        continue
                    seen.add(value)
                    normalized.append(value)
                supplied_media = normalized or None
            if supplied_media is not None:
                link_conclusion(
                    applicant_matches, requirement="media_allowed",
                    message="Supplied media compatible with explicitly unrestricted media rule",
                    req=media, conclusion_id="media",
                )
                add(
                    "applicant:media",
                    "pass",
                    "Media unrestricted; supplied media do not create a hard mismatch",
                )
            else:
                add(
                    "applicant:media",
                    "info",
                    "Media unrestricted; applicant media optional (not demanded)",
                )
    elif media_ok:
        mark_established("media_allowed", media["value"], media)
        raw_media = applicant.get("media")
        supplied_media = None
        if isinstance(raw_media, list):
            # Empty list / blank strings → treat as absent (no positive match).
            seen: set[str] = set()
            normalized: list[str] = []
            for item in raw_media:
                if not isinstance(item, str):
                    continue
                value = item.strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                normalized.append(value)
            supplied_media = normalized or None
        if supplied_media is not None and media["value"] is not None:
            # Hard mismatch only when every label on both sides maps into the
            # controlled vocabulary (synonyms included). Unknown free-form → review.
            # Known parent/child taxonomy (narrow applicant under broad organiser
            # category) → review, never hard reject.
            allowed_ids, allowed_unknown, allowed_children = _media_vocab_projection(
                [str(a) for a in media["value"]]
            )
            supplied_ids, supplied_unknown, supplied_children = _media_vocab_projection(
                supplied_media
            )
            if allowed_ids is None or supplied_ids is None:
                unknowns = list(allowed_unknown) + list(supplied_unknown)
                add(
                    "applicant:media",
                    "review",
                    "Media labels outside controlled vocabulary require manual free-form "
                    f"comparison; unknown labels: {unknowns} "
                    f"(organiser list: {media['value']}) — not a hard mismatch",
                )
                missing_for_decision.append(
                    "media_allowed (manual free-form label comparison)"
                )
                tool_limitations.append(
                    "media_allowed: non-vocabulary/free-form labels require human comparison"
                )
            elif allowed_children or supplied_children:
                child_desc = [
                    f"{label!r}⊂{parent}" for label, parent in
                    (allowed_children + supplied_children)
                ]
                add(
                    "applicant:media",
                    "review",
                    "Media parent/category relationship requires human review "
                    f"(narrow/broad taxonomy: {child_desc}; organiser list: "
                    f"{media['value']}) — not a hard mismatch",
                )
                missing_for_decision.append(
                    "media_allowed (parent/category taxonomy review)"
                )
                tool_limitations.append(
                    "media_allowed: parent/child media taxonomy needs human comparison"
                )
            else:
                bad = sorted(supplied_ids - allowed_ids)
                if bad:
                    known_hard_mismatch = True
                    link_conclusion(
                        applicant_mismatches, requirement="media_allowed",
                        message=f"Media not accepted: {bad}",
                        req=media, conclusion_id="media",
                    )
                    add(
                        "applicant:media",
                        "fail",
                        "At least one supplied medium is not accepted",
                    )
                else:
                    link_conclusion(
                        applicant_matches, requirement="media_allowed",
                        message=(
                            "Supplied media meet the rule under controlled vocabulary "
                            "and case/whitespace normalization"
                        ),
                        req=media, conclusion_id="media",
                    )
                    add(
                        "applicant:media",
                        "pass",
                        "Supplied media meet the normalised controlled-vocabulary rule",
                    )
        else:
            missing_for_decision.append("applicant artwork media (not supplied)")

    # Deadline — validate before publishing exact-expiry knowledge; never invent midnight
    deadline = reqs["deadline"]
    d_ok = require_evidence("deadline", deadline, value_key=None)
    if d_ok and deadline["knowledge_status"] == "not_required":
        contradictory = any(
            deadline.get(k) not in (None, "")
            for k in ("deadline_date", "deadline_time", "deadline_timezone", "deadline_datetime")
        )
        if contradictory:
            source_review_problems = True
            add("deadline_compare", "review",
                "deadline knowledge_status=not_required is contradictory with non-null deadline fields")
            missing_for_decision.append("deadline (not_required contradicts stated fields)")
        else:
            mark_established("deadline", {
                "not_required": True,
                "exact_expiry_known": False,
                "deadline_date": None,
                "deadline_time": None,
                "deadline_timezone": None,
                "deadline_datetime": None,
            }, deadline)
            add("deadline_compare", "info",
                "Deadline not required by organiser; no open/closed or missing-date checks")
    elif d_ok:
        validated = _validate_deadline_fields(deadline)
        if validated["issues"]:
            source_review_problems = True
            for issue in validated["issues"]:
                add("deadline_compare", "review", issue)
                missing_for_decision.append(f"deadline ({issue})")
            # Establish a non-exact summary only when the calendar date itself is valid.
            # Impossible calendar dates must not appear in sources_establish.
            calendar_bad = any("calendar date" in i for i in validated["issues"])
            if validated["parsed_date"] is not None and not calendar_bad:
                summary = dict(validated["summary"])
                summary["exact_expiry_known"] = False
                mark_established("deadline", summary, deadline)
        else:
            mark_established("deadline", validated["summary"], deadline)
            instant = validated["instant"]
            if instant is not None:
                if now < instant:
                    add("deadline_compare", "pass", "Exact deadline has not passed at --now")
                else:
                    known_hard_mismatch = True
                    link_conclusion(
                        applicant_mismatches, requirement="deadline",
                        message="Exact deadline has passed",
                        req=deadline,
                    )
                    add("deadline_compare", "fail", "Exact deadline has passed at --now")
            elif validated["parsed_date"] is not None:
                due = validated["parsed_date"]
                today = now.date()
                if today > due:
                    add("deadline_compare", "review",
                        f"Calendar date {due.isoformat()} is before --now date {today.isoformat()}; "
                        "time/timezone unknown — human must confirm whether the call is still open")
                    missing_for_decision.append("deadline time/timezone (date alone cannot prove exact expiry)")
                else:
                    add("deadline_compare", "info",
                        f"Deadline date {due.isoformat()} known; time/timezone unknown — "
                        "no exact-expiry claim from date alone")
                    missing_for_decision.append("deadline time/timezone for exact enforcement")
            else:
                missing_for_decision.append("deadline_date")

    # Fee schedule
    fee = reqs["fee_schedule"]
    fee_ok = require_evidence("fee_schedule", fee, value_key=None)
    if fee_ok and fee["knowledge_status"] == "not_required":
        contradictory = (
            fee.get("base_fee") is not None
            or fee.get("images_included") is not None
            or fee.get("additional_image_fee") is not None
            or fee.get("currency_code") is not None
            or bool(fee.get("optional_charges"))
        )
        if contradictory:
            source_review_problems = True
            add("fee_calculation", "review",
                "fee_schedule knowledge_status=not_required is contradictory with non-null fee fields")
            missing_for_decision.append("fee_schedule (not_required contradicts stated fields)")
        else:
            mark_established("fee_schedule", {"not_required": True}, fee)
            add("fee_calculation", "info",
                "Fee not required by organiser; no currency/image-count/affordability checks")
    elif fee_ok:
        mark_established("fee_schedule", {
            "base_fee": fee["base_fee"],
            "images_included": fee["images_included"],
            "additional_image_fee": fee["additional_image_fee"],
            "currency_raw": fee["currency_raw"],
            "currency_code": fee["currency_code"],
            "optional_charges": fee["optional_charges"],
        }, fee)
        if fee["currency_code"] is None:
            tool_limitations.append("fee currency_code unverified; cannot decide affordability")
            missing_for_decision.append("verified fee currency_code")
        # Structural completeness is independent of applicant input: report
        # unresolved organiser fee components even when image_count is absent.
        missing_fee_parts = [
            name for name, val in (
                ("base_fee", fee.get("base_fee")),
                ("images_included", fee.get("images_included")),
                ("additional_image_fee", fee.get("additional_image_fee")),
            )
            if val is None
        ]
        schedule_structurally_complete = not missing_fee_parts
        if missing_fee_parts:
            missing_for_decision.append(
                "fee_schedule incomplete for affordability "
                f"({', '.join(missing_fee_parts)})"
            )
        n = applicant.get("image_count")
        # Distinguish absent vs explicitly empty selection. Organiser `selected`
        # flags are not applicant permission — selection authority is applicant-only.
        if "selected_optional_charges" in applicant and applicant["selected_optional_charges"] is not None:
            selected = list(applicant["selected_optional_charges"])
        else:
            selected = []
        if n is not None:
            calc = calculate_fee(fee, n, selected)
            public_calc = _strip_internal_fee_fields(calc)
            affordability = {
                "status": "incomplete",
                "fee_total": public_calc["total"],
                "fee_total_calculable": public_calc["fee_total_calculable"],
                "currency_code_known": public_calc["currency_code_known"],
                "payment_permission": False,
                "note": "No affirmative budget result without an explicitly supplied complete budget",
                "derived_from_requirement": "fee_schedule",
            }
            established["fee_calculation"] = {
                **public_calc,
                "derived_from_requirement": "fee_schedule",
                "evidence": _evidence_payload(fee),
                "affordability": affordability,
            }
            add("fee_calculation", "info",
                f"Computed fee total={public_calc['total']} raw={public_calc['currency_raw']} "
                f"code={public_calc['currency_code']}; "
                f"fee_total_calculable={public_calc['fee_total_calculable']}; "
                f"currency_code_known={public_calc['currency_code_known']}")
            budget = applicant.get("fee_budget")
            total_dec = calc.get("total_decimal")
            if (
                isinstance(budget, dict)
                and total_dec is not None
                and calc["currency_code"]
                and budget.get("currency")
                and "amount" in budget
            ):
                if budget["currency"] != calc["currency_code"]:
                    add("applicant:fee", "review", "Fee and budget currencies differ; no exchange rate assumed")
                    missing_for_decision.append("currency alignment for budget check")
                    affordability.update({
                        "status": "currency_mismatch",
                        "budget_amount": budget["amount"],
                        "fee_currency": calc["currency_code"],
                        "budget_currency": budget["currency"],
                        "note": "Fee and budget currencies differ; no exchange rate assumed",
                    })
                else:
                    budget_amount = _to_decimal(budget["amount"])
                    if total_dec <= budget_amount:
                        link_conclusion(
                            applicant_matches, requirement="fee",
                            message="Computed fee within supplied budget",
                            req=fee,
                        )
                        add("applicant:fee", "pass", "Computed fee within supplied budget (not payment approval)")
                        affordability.update({
                            "status": "within_budget",
                            "budget_amount": _json_money(budget_amount),
                            "currency": calc["currency_code"],
                            "note": "Computed fee within supplied budget (not payment approval)",
                        })
                    else:
                        known_hard_mismatch = True
                        link_conclusion(
                            applicant_mismatches, requirement="fee",
                            message="Computed fee exceeds budget",
                            req=fee,
                        )
                        add("applicant:fee", "fail", "Computed fee exceeds supplied budget")
                        affordability.update({
                            "status": "exceeds_budget",
                            "budget_amount": _json_money(budget_amount),
                            "currency": calc["currency_code"],
                            "note": "Computed fee exceeds supplied budget (not payment approval)",
                        })
            elif calc.get("condition_blockers"):
                tool_limitations.append(
                    "Selected optional charge condition unresolved; fee total manual"
                )
                missing_for_decision.append(
                    "optional charge condition (manual; not a budget mismatch)"
                )
                affordability.update({
                    "status": "incomplete",
                    "note": calc.get("note") or affordability["note"],
                })
            else:
                budget = applicant.get("fee_budget")
                has_complete_budget = (
                    isinstance(budget, dict)
                    and "amount" in budget
                    and budget.get("currency")
                )
                if missing_fee_parts:
                    # Organiser incompleteness already recorded above; keep
                    # affordability incomplete without re-appending.
                    affordability.update({
                        "status": "incomplete",
                        "note": calc.get("note") or (
                            "Organiser fee schedule incomplete; affordability not decided"
                        ),
                    })
                elif not calc.get("currency_code_known"):
                    if not has_complete_budget:
                        missing_for_decision.append(
                            "applicant.fee_budget (amount and currency) for affordability"
                        )
                    affordability.update({
                        "status": "incomplete",
                        "note": calc.get("note") or affordability["note"],
                    })
                elif not has_complete_budget:
                    missing_for_decision.append(
                        "applicant.fee_budget (amount and currency) for affordability"
                    )
                else:
                    missing_for_decision.append(
                        "fee_schedule (not machine-calculable for affordability)"
                    )
                    affordability.update({
                        "status": "incomplete",
                        "note": calc.get("note") or affordability["note"],
                    })
            established["fee_calculation"]["affordability"] = affordability
            established["budget_comparison"] = dict(affordability)
        else:
            # Ask for applicant count only when the schedule can use it.
            if schedule_structurally_complete:
                missing_for_decision.append("applicant.image_count for fee total")

    # Image count — evaluate each known bound independently; INTERNAL_CAPACITY ≠ organiser max
    images = reqs["image_count"]
    img_ok = require_evidence("image_count", images, value_key=None)
    n = applicant.get("image_count")
    if img_ok and images["knowledge_status"] == "not_required":
        contradictory = (
            images.get("min") is not None
            or images.get("max") is not None
            or images.get("rule") in ("limited", "unlimited", "conflicting")
        )
        if contradictory:
            source_review_problems = True
            add("image_count_range", "review",
                "image_count knowledge_status=not_required is contradictory with "
                "limited/unlimited/conflicting rule or non-null bounds")
            missing_for_decision.append(
                "image_count (not_required contradicts stated rule/bounds)"
            )
        else:
            mark_established("image_count", {
                "not_required": True,
                "helper_internal_capacity": INTERNAL_CAPACITY,
            }, images)
            add("image_count_range", "info",
                "Image count not required by organiser; no organiser match/mismatch checks")
    elif img_ok and images["knowledge_status"] == "explicitly_unrestricted":
        lo, hi = images["min"], images["max"]
        rule = images["rule"]
        # Status-first: unrestricted means no upper membership bound. A limited
        # rule or explicit max alongside unrestricted is contradictory → review.
        if rule == "limited" or hi is not None or rule == "conflicting":
            source_review_problems = True
            add(
                "image_count_range",
                "review",
                "image_count knowledge_status=explicitly_unrestricted is contradictory "
                f"with rule={rule!r} or non-null max; not treated as applicant mismatch",
            )
            missing_for_decision.append(
                "image_count (explicitly_unrestricted contradicts limited/max bounds)"
            )
        else:
            mark_established("image_count", {
                "explicitly_unrestricted": True,
                "rule": "unlimited",
                "min": lo,
                "max": None,
                "helper_internal_capacity": INTERNAL_CAPACITY,
            }, images)
            tool_limitations.append(
                f"Organiser image count is unrestricted/unlimited (no upper bound); "
                f"helper INTERNAL_CAPACITY={INTERNAL_CAPACITY} is a tool limit, "
                "not an organiser maximum"
            )
            if n is None:
                missing_for_decision.append("applicant.image_count")
            elif lo is not None and n < lo:
                known_hard_mismatch = True
                link_conclusion(
                    applicant_mismatches, requirement="image_count",
                    message=(
                        f"{n} below organiser minimum {lo} "
                        "(unrestricted has no maximum)"
                    ),
                    req=images,
                )
                add(
                    "applicant:image_count",
                    "fail",
                    "Image count below organiser minimum under unrestricted rule",
                )
            else:
                link_conclusion(
                    applicant_matches, requirement="image_count",
                    message=(
                        f"{n} images allowed by organiser unrestricted rule"
                        + (f" with minimum {lo}" if lo is not None else "")
                        + f" (helper capacity {INTERNAL_CAPACITY})"
                    ),
                    req=images,
                )
                add(
                    "applicant:image_count",
                    "pass",
                    "Image count meets organiser unrestricted rule "
                    "(capacity checked separately)",
                )
    elif img_ok:
        mark_established("image_count", {
            "rule": images["rule"],
            "min": images["min"],
            "max": images["max"],
            "helper_internal_capacity": INTERNAL_CAPACITY,
        }, images)
        lo, hi = images["min"], images["max"]
        rule = images["rule"]

        if rule == "conflicting" or (lo is not None and hi is not None and lo > hi):
            source_review_problems = True
            add("image_count_range", "review",
                "Conflicting minimum/maximum image requirements in sources; not treated as applicant mismatch")
            missing_for_decision.append("image_count (conflicting organiser bounds)")
        elif rule == "unlimited":
            tool_limitations.append(
                f"Organiser image count is unlimited (no upper bound); helper INTERNAL_CAPACITY={INTERNAL_CAPACITY} "
                "is a tool limit, not an organiser maximum"
            )
            if n is None:
                missing_for_decision.append("applicant.image_count")
            elif lo is not None and n < lo:
                known_hard_mismatch = True
                link_conclusion(
                    applicant_mismatches, requirement="image_count",
                    message=f"{n} below organiser minimum {lo} (unlimited has no maximum)",
                    req=images,
                )
                add("applicant:image_count", "fail",
                    "Image count below organiser minimum under unlimited rule")
            else:
                link_conclusion(
                    applicant_matches, requirement="image_count",
                    message=(
                        f"{n} images allowed by organiser unlimited rule"
                        + (f" with minimum {lo}" if lo is not None else "")
                        + f" (helper capacity {INTERNAL_CAPACITY})"
                    ),
                    req=images,
                )
                add("applicant:image_count", "pass",
                    "Image count meets organiser unlimited rule (capacity checked separately)")
        elif rule == "limited":
            if lo is None and hi is None:
                source_review_problems = True
                add("image_count_range", "review",
                    "rule=limited with min=null and max=null has no known organiser bounds; "
                    "not treated as a positive applicant match")
                missing_for_decision.append("image_count (empty limited rule without min/max bounds)")
            elif n is None:
                missing_for_decision.append("applicant.image_count")
            else:
                below = lo is not None and n < lo
                above = hi is not None and n > hi
                if below or above:
                    known_hard_mismatch = True
                    parts = []
                    if below:
                        parts.append(f"below minimum {lo}")
                    if above:
                        parts.append(f"above maximum {hi}")
                    link_conclusion(
                        applicant_mismatches, requirement="image_count",
                        message=f"{n} is {' and '.join(parts)}",
                        req=images,
                    )
                    add("applicant:image_count", "fail", "Image count outside organiser limited bound(s)")
                else:
                    bound_desc = f"min={lo}" if lo is not None else "min=unspecified"
                    bound_desc += f", max={hi}" if hi is not None else ", max=unspecified"
                    link_conclusion(
                        applicant_matches, requirement="image_count",
                        message=f"{n} within known organiser bounds ({bound_desc})",
                        req=images,
                    )
                    add("applicant:image_count", "pass", "Image count meets known organiser bound(s)")

    # Helper capacity depends on supplied applicant count, not organiser extractability.
    if n is not None:
        if n > INTERNAL_CAPACITY:
            add("image_count_capacity", "review",
                f"Selected {n} images exceeds helper INTERNAL_CAPACITY={INTERNAL_CAPACITY}; "
                "this is a tool/capacity limit, NOT organiser ineligibility")
            missing_for_decision.append("reduce selection to helper capacity or extend tooling")
            tool_limitations.append(
                f"helper INTERNAL_CAPACITY={INTERNAL_CAPACITY} below selected image_count={n}"
            )
        else:
            add("image_count_capacity", "info",
                f"Selected {n} within helper INTERNAL_CAPACITY={INTERNAL_CAPACITY}")

    # Remaining mirrorable facts — status-first for not_required / explicitly_unrestricted
    for name in ("allowed_formats", "min_long_edge_px", "max_long_edge_px", "max_file_bytes",
                 "statement_max_words", "bio_max_words", "filename_rule", "rights_clause"):
        req = reqs[name]
        if not require_evidence(name, req):
            continue
        status = req["knowledge_status"]
        value = req.get("value")
        if status == "not_required":
            if value is not None:
                source_review_problems = True
                add(
                    f"req:{name}:status",
                    "review",
                    f"{name} knowledge_status=not_required is contradictory with non-null value",
                )
                missing_for_decision.append(
                    f"{name} (not_required contradicts stated value)"
                )
            else:
                mark_established(name, {"not_required": True}, req)
            continue
        if status == "explicitly_unrestricted":
            # Only defined mirrorables may be unrestricted; concrete leftover
            # values are contradictions → review (never silently established).
            if not _unrestricted_value_ok(name, value):
                source_review_problems = True
                add(
                    f"req:{name}:status",
                    "review",
                    f"{name} knowledge_status=explicitly_unrestricted is contradictory "
                    f"with concrete value {value!r}",
                )
                missing_for_decision.append(
                    f"{name} (explicitly_unrestricted contradicts concrete value)"
                )
            else:
                mark_established(
                    name,
                    {"explicitly_unrestricted": True, "value": value},
                    req,
                )
            continue
        mark_established(name, value, req)
        if name == "filename_rule" and value not in (None, "any"):
            tool_limitations.append("Custom filename_rule needs a tested adapter")
            unsupported.append({
                "id": "filename_rule",
                "reason": "custom rule",
                "helper_support": req["helper_support"],
            })
        if name == "allowed_formats" and value is not None and "JPEG" not in value:
            tool_limitations.append("Helper exports JPEG only")
        if name == "rights_clause":
            tool_limitations.append("Rights/licensing always require human legal review")

    for item in call.get("unresolved_requirements", []):
        add("unresolved", "review", item)
        missing_for_decision.append(f"unresolved: {item[:120]}")

    # Artwork / profile absence must NOT block analysis
    add("scope:no_artwork_required", "info",
        "assess-call runs without artwork, bio, fee budget or application package")

    # Status / eligibility — never "eligible" / "fully_eligible".
    # Evidence/input failures are NOT applicant ineligibility.
    # Incomplete captures: keep individually evidenced facts, but withhold hard
    # eligibility rejects only when THAT conclusion depends on incomplete capture
    # (provenance-scoped). An unrelated incomplete source must not blanket-disable
    # hard conclusions proved by complete authoritative evidence.
    def _mismatch_depends_on_incomplete(m: dict) -> bool:
        evid_sid = (m.get("evidence") or {}).get("source_id")
        if evid_sid and evid_sid in incomplete_rule_source_ids:
            return True
        req_name = m.get("requirement") or m.get("id") or ""
        est_keys = {req_name, m.get("id") or ""}
        # Conclusion ids / requirement aliases → sources_establish keys.
        aliases = {
            "media": "media_allowed",
            "fee": "fee_schedule",
        }
        for key in list(est_keys):
            if key in aliases:
                est_keys.add(aliases[key])
        for key in est_keys:
            est = established.get(key)
            if est and est.get("incomplete_source_capture"):
                return True
        return False

    def _fail_check_matches_withheld(check_id: str, withheld: list[dict]) -> bool:
        for m in withheld:
            mid = m.get("id") or ""
            req = m.get("requirement") or ""
            if check_id == f"applicant:{mid}" or check_id == f"applicant:{req}":
                return True
            if mid == "deadline" or req == "deadline":
                if check_id == "deadline_compare":
                    return True
        return False

    if incomplete_rule_source_ids and applicant_mismatches:
        withheld: list[dict] = []
        kept: list[dict] = []
        for m in applicant_mismatches:
            if _mismatch_depends_on_incomplete(m):
                withheld.append(m)
            else:
                kept.append(m)
        if withheld:
            for c in checks:
                if c["status"] == "fail" and _fail_check_matches_withheld(c["id"], withheld):
                    c["status"] = "review"
                    c["message"] = (
                        f"{c['message']} — hard reject withheld "
                        "(incomplete organiser rules capture)"
                    )
            add(
                "eligibility:incomplete_capture",
                "review",
                "Apparent applicant mismatch not treated as hard reject because "
                "its evidence cites a source with complete_rules_captured=false",
            )
            applicant_mismatches[:] = kept
            known_hard_mismatch = bool(kept)
            source_review_problems = True
            missing_for_decision.append(
                "complete organiser rules capture (incomplete source blocks hard "
                "eligibility for conclusions that cite it)"
            )

    applicant_fail = any(
        c["id"].startswith("applicant:") and c["status"] == "fail" for c in checks
    )
    integrity_failed = any(
        c["id"].startswith("source_integrity:") and c["status"] == "fail" for c in checks
    )
    has_review = any(c["status"] == "review" for c in checks)

    if known_hard_mismatch or applicant_fail:
        status = "blocked"
        eligibility = "known_mismatch"
    elif evidence_untrustworthy or integrity_failed or source_review_problems:
        # Untrustworthy evidence / demo-origin / source contradictions → incomplete input
        status = "incomplete"
        eligibility = "insufficient_facts"
        # Ensure we do not leave misleading established facts from corrupted evidence
        if integrity_failed:
            # Integrity failure means no excerpt can be trusted from that source;
            # require_evidence already skipped unusable sources. Keep any facts from
            # other intact sources only.
            pass
    elif has_review or missing_for_decision:
        if established:
            status = "analysis_complete"
            eligibility = "provisional_partial" if applicant_matches and not applicant_mismatches else "insufficient_facts"
        else:
            status = "incomplete"
            eligibility = "insufficient_facts"
    else:
        status = "analysis_complete"
        eligibility = "provisional_partial" if applicant_matches else "insufficient_facts"

    # Prefer distinct incomplete when inaccessible dominates and little established
    inaccessible_reqs = [r for r, req in reqs.items() if req.get("knowledge_status") == "inaccessible"]
    if inaccessible_reqs and eligibility != "known_mismatch" and len(established) < 3:
        status = "incomplete"

    next_action = _next_action(
        eligibility,
        missing_for_decision,
        incomplete_sources,
        unsupported,
        known_hard_mismatch,
        integrity_failed=integrity_failed,
        evidence_untrustworthy=evidence_untrustworthy,
        source_review_problems=source_review_problems,
        unusable_source_reasons=unusable_source_reasons,
    )

    return {
        "status": status,
        "eligibility_decision": eligibility,
        "demo_only": demo_only,
        "assessed_at": now.isoformat(),
        "opportunity_id": call["id"],
        "schema_version": 2,
        "sources_establish": established,
        "source_metadata": _source_metadata_map(sources),
        "applicant_matches": applicant_matches,
        "applicant_mismatches": applicant_mismatches,
        "missing_for_decision": _unique(missing_for_decision),
        "unsupported_requirements": unsupported,
        "tool_limitations": _unique(tool_limitations),
        "incomplete_or_inaccessible_sources": incomplete_sources,
        "unusable_sources": [
            {"source_id": sid, "reason": unusable_source_reasons[sid]}
            for sid in sorted(unusable_source_reasons)
        ],
        "next_action": next_action,
        "human_review_required": True,
        "helper_internal_capacity": INTERNAL_CAPACITY,
        "checks": checks,
        "scope": (
            "Read-only analysis of schema_version 2 requirements against optional applicant facts. "
            "Does not verify extraction meaning, organiser authenticity, rights, or complete eligibility. "
            "Never submits or pays. Successful analysis is not an eligibility badge. "
            "Literal excerpt integrity is reported separately from semantic verification."
        ),
        "exit_code_hint": 2 if eligibility == "known_mismatch" else (3 if status == "incomplete" else 0),
    }


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _next_action(
    eligibility,
    missing,
    incomplete_sources,
    unsupported,
    hard_mismatch,
    *,
    integrity_failed: bool = False,
    evidence_untrustworthy: bool = False,
    source_review_problems: bool = False,
    unusable_source_reasons: dict[str, str] | None = None,
) -> str:
    reasons = unusable_source_reasons or {}
    if hard_mismatch or eligibility == "known_mismatch":
        return "Stop: resolve the known hard mismatch (see applicant_mismatches) before investing further effort."
    if integrity_failed or any(r == "integrity_failure" for r in reasons.values()):
        return (
            "Recapture organiser source snapshots and update snapshot_sha256 before treating "
            "requirements as established; evidence integrity failed (not an applicant eligibility decision)."
        )
    # Prefer specific completeness / freshness actions over generic source-review.
    if any(
        isinstance(item, dict) and item.get("reason") == "complete_rules_captured=false"
        for item in incomplete_sources
    ):
        return (
            "Recapture incomplete organiser portal/rules sources "
            "(complete_rules_captured=false), then re-assess; "
            "individually evidenced facts may already be established."
        )
    if any(r == "stale_or_future_capture" for r in reasons.values()):
        return (
            "Refresh stale organiser source snapshots (capture older than 72 hours or "
            "future-dated) and update captured_at/snapshot before using requirements."
        )
    if any(r == "missing_required_url" for r in reasons.values()):
        return (
            "Add the organiser HTTPS URL on original_page sources before treating "
            "requirements as established for real-call decisions."
        )
    if any(r == "demo_origin" for r in reasons.values()):
        return (
            "Replace demo-origin evidence with organiser sources (or set demo_only=true) "
            "before using requirements for real-call decisions."
        )
    if any(r == "non_authoritative_kind" for r in reasons.values()):
        return (
            "Cite an authoritative organiser source (not aggregator/discovery/access-blocker) "
            "before establishing requirements."
        )
    if evidence_untrustworthy or source_review_problems:
        # Surface the most specific remaining unusable reason when present.
        if reasons:
            primary = next(iter(sorted(reasons.items())))
            label = _UNUSABLE_REASON_LABELS.get(primary[1], primary[1])
            return (
                f"Resolve source-review / input problems for {primary[0]} "
                f"({label}) before using requirements for real-call decisions."
            )
        return (
            "Resolve source-review / input problems (demo-origin mismatch, missing original URL, "
            "or contradictory extracted fields) before using requirements for real-call decisions."
        )
    if incomplete_sources:
        return "Recapture or manually inspect incomplete/inaccessible organiser sources (portal fields), then update requirements."
    if any("deadline time" in m or "deadline (" in m for m in missing):
        return "Confirm organiser deadline time and timezone before treating the call as open or closed."
    if any("currency" in m for m in missing):
        return "Confirm fee currency (ISO code) on the payment UI before deciding affordability."
    if unsupported:
        return f"Plan manual handling or an adapter for: {unsupported[0].get('id', 'unsupported requirement')}."
    if missing:
        return f"Supply the smallest missing applicant/organiser fact: {missing[0]}"
    return "Human-review rights, unresolved requirements, and decide whether to gather artworks for a future prepare step."


def exit_code_for_assess_call(result: dict) -> int:
    """Map assess-call result to process exit code.

    0 — analysis ran; no known hard mismatch (may still be incomplete/partial)
    2 — known hard mismatch
    3 — incomplete / needs human review dominant
    """
    if result.get("eligibility_decision") == "known_mismatch":
        return 2
    if result.get("status") == "incomplete":
        return 3
    return 0