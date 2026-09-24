"""Create bounded JPEG submission copies; never crop, upscale or retouch."""
from __future__ import annotations

import io
import warnings
from pathlib import Path

from PIL import Image, ImageCms, ImageFile, ImageOps, UnidentifiedImageError

from .io import ProducerError, sha256

MAX_PIXELS = 40_000_000
MAX_ICC_BYTES = 1_000_000
# Helper processing/output ceiling for encode + validate. Never record this as the
# organiser's upload rule. When an exact organiser byte limit is known, effective
# bound is min(organiser_bytes, TOOL_MAX_FILE_BYTES).
TOOL_MAX_FILE_BYTES = 64 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def resolve_file_byte_bounds(
    *,
    knowledge_status: str,
    organiser_value_bytes: int | None = None,
    declared_label: str | None = None,
) -> dict:
    """Separate organiser-declared limit, tool ceiling, and effective encode bound.

    - Verified exact organiser bytes → effective = min(organiser, tool); report
      which bound controls.
    - not_required / explicitly_unrestricted → encode uses tool ceiling only;
      tool ceiling is never written back as the organiser rule.
    - Unknown / label-only / conflicting → no exact-byte effective bound
      (not silently unrestricted).
    """
    organiser = {
        "value_bytes": organiser_value_bytes if isinstance(organiser_value_bytes, int) else None,
        "declared_label": declared_label if isinstance(declared_label, str) and declared_label else None,
        "knowledge_status": knowledge_status,
    }
    tool = TOOL_MAX_FILE_BYTES

    if knowledge_status == "known" and isinstance(organiser_value_bytes, int):
        effective = min(organiser_value_bytes, tool)
        if organiser_value_bytes <= tool:
            controlling = "organiser"
        else:
            controlling = "tool_ceiling"
        return {
            "organiser_declared": organiser,
            "tool_ceiling_bytes": tool,
            "effective_max_file_bytes": effective,
            "controlling_bound": controlling,
            "exact_bytes_ready": True,
        }

    if knowledge_status in ("not_required", "explicitly_unrestricted"):
        return {
            "organiser_declared": organiser,
            "tool_ceiling_bytes": tool,
            "effective_max_file_bytes": tool,
            "controlling_bound": "tool_ceiling",
            "exact_bytes_ready": True,
            "note": (
                "No organiser max_file_bytes established; encoding uses helper "
                "TOOL_MAX_FILE_BYTES only (not an organiser rule)."
            ),
        }

    # Label-only known (value null), inaccessible, not_stated, conflicting, etc.
    reason = "exact organiser byte limit not established"
    if knowledge_status == "known" and organiser["declared_label"] and organiser["value_bytes"] is None:
        reason = (
            f"organiser label {organiser['declared_label']!r} recorded without "
            "verified exact byte conversion"
        )
    elif knowledge_status in ("inaccessible", "not_stated", "conflicting"):
        reason = f"max_file_bytes knowledge_status={knowledge_status}"
    return {
        "organiser_declared": organiser,
        "tool_ceiling_bytes": tool,
        "effective_max_file_bytes": None,
        "controlling_bound": "unresolved",
        "exact_bytes_ready": False,
        "note": reason,
    }


def inspect_icc_profile(icc: bytes | None) -> dict:
    """Shared ICC inspection for planning measurement and execution encode.

    Rules must not drift: oversized or unparseable ICC is rejected the same way
    in plan-package preflight and in open_pixels.

    Returned keys (canonical): has_icc_profile, icc_bytes, icc_ok, icc_error.
    Also exposes present/bytes/within_limit/parseable/error aliases used by callers.
    """
    if not icc:
        return {
            "has_icc_profile": False,
            "present": False,
            "icc_bytes": 0,
            "bytes": 0,
            "icc_ok": False,
            "icc_error": None,
            "error": None,
            "within_limit": True,
            "parseable": False,
        }
    nbytes = len(icc)
    if nbytes > MAX_ICC_BYTES:
        return {
            "has_icc_profile": True,
            "present": True,
            "icc_bytes": nbytes,
            "bytes": nbytes,
            "icc_ok": False,
            "icc_error": "ICC profile exceeds 1 MB limit",
            "error": "ICC profile exceeds 1 MB limit",
            "within_limit": False,
            "parseable": False,
        }
    try:
        ImageCms.ImageCmsProfile(io.BytesIO(icc))
    except Exception as exc:  # Pillow raises PyCMSError / OSError / ValueError
        msg = f"Invalid ICC profile: {exc}"
        return {
            "has_icc_profile": True,
            "present": True,
            "icc_bytes": nbytes,
            "bytes": nbytes,
            "icc_ok": False,
            "icc_error": msg,
            "error": msg,
            "within_limit": True,
            "parseable": False,
        }
    return {
        "has_icc_profile": True,
        "present": True,
        "icc_bytes": nbytes,
        "bytes": nbytes,
        "icc_ok": True,
        "icc_error": None,
        "error": None,
        "within_limit": True,
        "parseable": True,
    }


def open_pixels(path: Path, untagged_policy: str) -> tuple[Image.Image, list[int], str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in ("JPEG", "PNG"):
                    raise ProducerError("Only JPEG/PNG source files are supported")
                if getattr(image, "n_frames", 1) != 1:
                    raise ProducerError("Animated/multipage sources are not supported")
                if image.width * image.height > MAX_PIXELS:
                    raise ProducerError("Source exceeds the 40 MP pixel limit")
                if image.mode not in ("RGB", "L") or "transparency" in image.info:
                    raise ProducerError("Use an opaque RGB or grayscale finished source; alpha/palette/CMYK inputs need manual conversion")
                icc = image.info.get("icc_profile")
                icc_info = inspect_icc_profile(icc if isinstance(icc, (bytes, bytearray)) else None)
                oriented = ImageOps.exif_transpose(image)
                oriented.load()
                original_size = list(oriented.size)
                if icc_info["has_icc_profile"]:
                    if not icc_info["icc_ok"]:
                        raise ProducerError(icc_info["icc_error"] or "ICC profile unusable")
                    source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                    pixels = ImageCms.profileToProfile(oriented, source_profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
                    colour_note = "Converted embedded ICC profile to sRGB"
                elif untagged_policy == "assume_srgb":
                    pixels = oriented.convert("RGB")
                    colour_note = "Untagged source explicitly assumed sRGB by application configuration"
                else:
                    raise ProducerError("Untagged source: explicitly choose assume_srgb or provide a colour-profiled copy")
                # A new pixel-only surface prevents source metadata from following the export.
                clean = Image.frombytes("RGB", pixels.size, pixels.tobytes())
                return clean, original_size, colour_note
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            OSError, ImageCms.PyCMSError, ValueError) as exc:
        if isinstance(exc, ProducerError):
            raise
        raise ProducerError(f"Cannot safely decode/colour-convert {path.name}: {exc}") from exc



def _jpeg_save_bytes(image: Image.Image, save_kw: dict) -> bytes:
    """Encode JPEG into memory with an encoder buffer sized for optimize mode.

    Pillow's default optimize ``bufsize`` (~2×width×height for quality ≥ 95) can
    undershoot high-entropy RGB, so libjpeg-turbo raises
    ``OSError: broken data stream when writing image file`` /
    ``Suspension not allowed here`` before any byte-limit check runs.

    Raise the floor to uncompressed RGB size (+ small APP headroom) without
    changing quality, subsampling, optimize, colour, or dimensions. Bound by
    ``MAX_PIXELS`` already applied to the loaded surface.

    Per-call path only: mirrors ``Image.save`` → ``JpegImagePlugin._save`` setup
    for our RGB encode kwargs, then invokes ``ImageFile._save(...)`` directly with
    the floored bufsize. Never assigns to process-global ``ImageFile._save`` /
    ``MAXBLOCK`` (or any other Pillow global), so overlapping in-process encodes
    cannot race on a swapped wrapper.
    """
    # Local imports keep the JPEG-plugin helpers scoped to this encode path.
    from PIL.JpegImagePlugin import RAWMODE, o8, o16

    width, height = image.size
    # RGB only here: open_pixels returns a clean RGB surface.
    min_buf = (3 * width * height) + (64 * 1024)

    # Same as Image.save **params: format is the handler key, not encoderinfo.
    info = {key: value for key, value in save_kw.items() if key != "format"}

    try:
        rawmode = RAWMODE[image.mode]
    except KeyError as exc:
        raise OSError(f"cannot write mode {image.mode} as JPEG") from exc

    dpi = [round(x) for x in info.get("dpi", (0, 0))]
    quality = info.get("quality", -1)
    subsampling = info.get("subsampling", -1)
    if not isinstance(quality, int):
        raise ValueError("Invalid quality setting")
    # Map string aliases the same way JpegImagePlugin does (our callers pass 0).
    if subsampling == "4:4:4":
        subsampling = 0
    elif subsampling == "4:2:2":
        subsampling = 1
    elif subsampling == "4:2:0":
        subsampling = 2
    elif subsampling == "4:1:1":
        subsampling = 2

    extra = info.get("extra", b"")
    max_bytes_in_marker = 65533
    if icc_profile := info.get("icc_profile"):
        overhead_len = 14  # b"ICC_PROFILE\0" + o8(i) + o8(len(markers))
        max_data_bytes_in_marker = max_bytes_in_marker - overhead_len
        markers = []
        while icc_profile:
            markers.append(icc_profile[:max_data_bytes_in_marker])
            icc_profile = icc_profile[max_data_bytes_in_marker:]
        for index, marker in enumerate(markers, start=1):
            size = o16(2 + overhead_len + len(marker))
            extra += (
                b"\xff\xe2"
                + size
                + b"ICC_PROFILE\0"
                + o8(index)
                + o8(len(markers))
                + marker
            )

    progressive = bool(info.get("progressive", False) or info.get("progression", False))
    optimize = bool(info.get("optimize", False))
    exif = info.get("exif", b"")
    if isinstance(exif, Image.Exif):
        exif = exif.tobytes()
    if len(exif) > max_bytes_in_marker:
        raise ValueError("EXIF data is too long")
    comment = info.get("comment", image.info.get("comment"))
    qtables = info.get("qtables")

    # Pillow's JpegImagePlugin optimize/progressive bufsize guess, then floor.
    if optimize or progressive:
        if image.mode == "CMYK":
            bufsize = 4 * width * height
        elif quality >= 95 or quality == -1:
            bufsize = 2 * width * height
        else:
            bufsize = width * height
        if exif:
            bufsize += len(exif) + 5
        if extra:
            bufsize += len(extra) + 1
    else:
        bufsize = max(len(exif) + 5, len(extra) + 1)
    bufsize = max(int(bufsize), min_buf)

    had_encoderinfo = hasattr(image, "encoderinfo")
    had_encoderconfig = hasattr(image, "encoderconfig")
    prior_encoderinfo = getattr(image, "encoderinfo", None)
    prior_encoderconfig = getattr(image, "encoderconfig", None)

    buf = io.BytesIO()
    try:
        image.encoderinfo = info
        image.encoderconfig = (
            quality,
            progressive,
            info.get("smooth", 0),
            optimize,
            info.get("keep_rgb", False),
            info.get("streamtype", 0),
            dpi,
            subsampling,
            info.get("restart_marker_blocks", 0),
            info.get("restart_marker_rows", 0),
            qtables,
            comment,
            extra,
            exif,
        )
        # Invoke the real helper as a call — do not assign ImageFile._save.
        ImageFile._save(
            image,
            buf,
            [ImageFile._Tile("jpeg", (0, 0) + image.size, 0, rawmode)],
            bufsize,
        )
    finally:
        if had_encoderinfo:
            image.encoderinfo = prior_encoderinfo
        elif hasattr(image, "encoderinfo"):
            delattr(image, "encoderinfo")
        if had_encoderconfig:
            image.encoderconfig = prior_encoderconfig
        elif hasattr(image, "encoderconfig"):
            delattr(image, "encoderconfig")
    return buf.getvalue()


def create_jpeg(
    source: Path,
    destination: Path,
    facts: dict,
    untagged_policy: str,
    *,
    dpi: tuple[int, int] | None = None,
) -> dict:
    """Encode a downscale-only JPEG submission copy.

    Optional ``dpi`` sets JFIF density metadata only (e.g. (72, 72)); pixels are
    unchanged by the density tag. Used when the plan includes set-ppi-metadata.
    """
    pixels, original_size, colour_note = open_pixels(source, untagged_policy)
    max_edge = facts["max_long_edge_px"]["value"]
    min_edge = facts["min_long_edge_px"]["value"]
    pixels.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    if max(pixels.size) < min_edge:
        raise ProducerError(f"{source.name}: original is too small; upscaling is prohibited")
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    max_bytes = facts["max_file_bytes"]["value"]
    encoded = None
    used_quality = None
    # Bounded search. Do not silently reduce quality below the declared floor or dimensions further.
    for quality in (95, 90, 85, 80):
        save_kw: dict = {
            "format": "JPEG",
            "quality": quality,
            "subsampling": 0,
            "optimize": True,
            "icc_profile": srgb,
        }
        if dpi is not None:
            save_kw["dpi"] = dpi
        # Encode to memory with an adequate optimize buffer; write destination only
        # after a candidate fits (no orphan partial output on byte-limit failure).
        candidate = _jpeg_save_bytes(pixels, save_kw)
        if len(candidate) <= max_bytes:
            encoded, used_quality = candidate, quality
            break
    if encoded is None:
        raise ProducerError(f"{source.name}: cannot meet byte limit at JPEG quality 80–95; ask for an approved alternative")
    destination.write_bytes(encoded)
    info = inspect_jpeg(destination, facts, expected_dpi=dpi)
    meta_note = "Source EXIF/XMP/comments removed; generated sRGB ICC retained"
    if dpi is not None:
        meta_note += f"; JFIF density set to {dpi[0]}×{dpi[1]} ppi (metadata only)"
    info.update({"source_sha256": sha256(source), "oriented_source_size": original_size,
                 "jpeg_quality": used_quality, "colour_handling": colour_note,
                 "metadata_policy": meta_note})
    if dpi is not None:
        info["dpi"] = list(dpi)
    return info


def inspect_jpeg(
    path: Path,
    facts: dict,
    *,
    expected_dpi: tuple[int, int] | None = None,
) -> dict:
    if path.stat().st_size > facts["max_file_bytes"]["value"]:
        raise ProducerError(f"Oversized derivative: {path.name}")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ProducerError("Derivative exceeds pixel bound")
            image.load()
            edge = max(image.size)
            if image.format != "JPEG" or image.mode != "RGB":
                raise ProducerError(f"Not an RGB JPEG: {path.name}")
            if not facts["min_long_edge_px"]["value"] <= edge <= facts["max_long_edge_px"]["value"]:
                raise ProducerError(f"Derivative dimension rule failed: {path.name}")
            if image.getexif() or any(key in image.info for key in ("xmp", "XML:com.adobe.xmp", "comment")):
                raise ProducerError(f"Unexpected personal metadata: {path.name}")
            if not image.info.get("icc_profile"):
                raise ProducerError(f"Missing output ICC profile: {path.name}")
            measured_dpi = image.info.get("dpi")
            if expected_dpi is not None:
                if not measured_dpi:
                    raise ProducerError(f"Missing required DPI metadata: {path.name}")
                got = (int(round(measured_dpi[0])), int(round(measured_dpi[1])))
                if got != expected_dpi:
                    raise ProducerError(
                        f"DPI metadata mismatch for {path.name}: expected {expected_dpi}, got {got}"
                    )
            result = {"width": image.width, "height": image.height, "format": "JPEG",
                      "bytes": path.stat().st_size, "sha256": sha256(path)}
            if measured_dpi:
                result["dpi"] = [int(round(measured_dpi[0])), int(round(measured_dpi[1]))]
            return result
