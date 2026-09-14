#!/usr/bin/env python3
"""Bounded OpenAI Images generation for AI Hayom.

This module intentionally has no retry loop, no publication path, and no Telegram
or scheduling integration. The API key is read in-process from the requested
owner-only .env file and is never included in arguments, files, or output.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import shutil
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
import re
from ai_hayom import CARTOON_MODES

API_ENDPOINT = "https://api.openai.com/v1/images/generations"
MODEL = "gpt-image-1"
CHARTER = """Premium modern newspaper editorial cartoon. Elegant visual metaphor, restrained caricature, intelligent visual wit, economical linework, strong composition, and immediate readability. Use original visual language only, never imitate a named artist, existing cartoon, composition, character, or signature style.

Avoid grotesque exaggeration, partisan propaganda, photorealism, generic glowing robots, stock AI imagery, excessive detail, text-heavy jokes, and visual clutter.

Maintain a recognizable AI Hayom house style: black editorial ink; warm off-white newsprint background; restrained red accent; confident imperfect linework; one strong visual idea; culturally intelligent rather than cruel; suitable for a premium independent newspaper.

No words, letters, numbers, captions, logos, signatures, or watermarks anywhere inside the artwork. Communicate entirely through visual metaphor."""
NO_TEXT_INVARIANT = "No words, letters, numbers, captions, logos, signatures, or watermarks anywhere inside the artwork. Communicate entirely through visual metaphor."
OCR_CONFIDENCE_THRESHOLD = 70.0
OCR_CLUSTER_CONFIDENCE_THRESHOLD = 85.0
OCR_SHORT_WORD_CONFIDENCE_THRESHOLD = 85.0


def load_key(env_path: Path) -> str:
    if env_path.stat().st_mode & 0o077:
        raise RuntimeError("credential file permissions must be owner-only")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise RuntimeError("OPENAI_API_KEY is missing")


def request_image(key: str, prompt: str, size: str) -> tuple[bytes, dict]:
    body = json.dumps({"model": MODEL, "prompt": prompt, "size": size, "quality": "medium"}).encode()
    request = urllib.request.Request(API_ENDPOINT, data=body, method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "AI-Hayom-fixture/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read(20_000_000))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenAI Images request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenAI Images request failed before an HTTP response") from exc
    try:
        encoded = payload["data"][0]["b64_json"]
        return base64.b64decode(encoded, validate=True), payload.get("usage", {})
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenAI Images returned malformed output") from exc


def ocr_diagnostics(tsv: str, threshold: float = OCR_CONFIDENCE_THRESHOLD) -> list[dict]:
    """Return only sanitized, high-confidence OCR tokens.

    Tesseract's confidence is 0..100. A token is actionable only when its
    confidence is strictly above the documented threshold. Short, low-
    confidence hallucinations from editorial linework are deliberately ignored.
    """
    detected = []
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    for row in reader:
        text = re.sub(r"[^A-Za-z0-9\u0590-\u05ff]", "", row.get("text", ""))
        try:
            confidence = float(row.get("conf", "-1"))
        except ValueError:
            continue
        if text and confidence > threshold:
            detected.append({"token": text[:64], "confidence": round(confidence, 1)})
    return detected


def validate_ocr_output(tsv: str, threshold: float = OCR_CONFIDENCE_THRESHOLD) -> list[dict]:
    return ocr_diagnostics(tsv, threshold)


def _ocr_rows(tsv: str, threshold: float = OCR_CONFIDENCE_THRESHOLD) -> list[dict]:
    rows = []
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    for row in reader:
        token = re.sub(r"[^A-Za-z0-9\u0590-\u05ff]", "", row.get("text", ""))
        try:
            confidence = float(row.get("conf", "-1"))
            x, y, width, height = (int(float(row.get(key, "0"))) for key in ("left", "top", "width", "height"))
        except (TypeError, ValueError):
            continue
        if token and confidence > threshold:
            rows.append({"token": token[:64], "confidence": round(confidence, 1), "x": x, "y": y, "width": width, "height": height})
    return rows


def _boxes_match(a: dict, b: dict) -> bool:
    ax2, ay2 = a["x"] + a["width"], a["y"] + a["height"]
    bx2, by2 = b["x"] + b["width"], b["y"] + b["height"]
    overlap = min(ax2, bx2) > max(a["x"], b["x"]) and min(ay2, by2) > max(a["y"], b["y"])
    ac = (a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
    bc = (b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    center_distance = ((ac[0] - bc[0]) ** 2 + (ac[1] - bc[1]) ** 2) ** 0.5
    return overlap or center_distance <= max(20, min(max(a["width"], a["height"], b["width"], b["height"]), 80))


def _deduplicate_spatial(rows: list[dict]) -> list[dict]:
    unique = []
    for row in sorted(rows, key=lambda item: item["confidence"], reverse=True):
        if not any(_boxes_match(row, existing) for existing in unique):
            unique.append(row)
    return unique


def ocr_evidence(tsv11: str, tsv6: str, threshold: float = OCR_CONFIDENCE_THRESHOLD) -> list[dict]:
    """Classify only strong, corroborated, or spatially clustered OCR evidence."""
    first, second = _ocr_rows(tsv11, threshold), _ocr_rows(tsv6, threshold)
    strong = [
        {"evidence": "strong-token", "token": row["token"], "confidence": row["confidence"]}
        for row in first + second
        if len(row["token"]) >= 4
        or (len(row["token"]) == 3 and row["confidence"] > OCR_SHORT_WORD_CONFIDENCE_THRESHOLD)
    ]
    if strong:
        return strong[:8]
    cross_pass = [{"evidence": "cross-pass", "token": row["token"], "confidence": row["confidence"]} for row in first if 1 <= len(row["token"]) <= 2 and not row["token"].isdigit() and any(row["token"].lower() == other["token"].lower() and _boxes_match(row, other) for other in second if 1 <= len(other["token"]) <= 2 and not other["token"].isdigit())]
    if cross_pass:
        return cross_pass[:8]
    # A loose mixture of low-confidence fragments from two segmentation modes
    # is common in ink drawings. Treat a one-pass cluster as text only when all
    # fragments are independently high-confidence. Real short labels are still
    # rejected when the same token appears in both passes above the base limit.
    for pass_rows in (first, second):
        singles = _deduplicate_spatial([
            row for row in pass_rows
            if 1 <= len(row["token"]) <= 2
            and row["confidence"] > OCR_CLUSTER_CONFIDENCE_THRESHOLD
        ])
        for row in singles:
            cluster = [other for other in singles
                       if abs(other["y"] - row["y"]) <= 40
                       and abs(other["x"] - row["x"]) <= 180]
            if len(cluster) >= 3:
                return [{"evidence": "clustered-single-character", "token": item["token"], "confidence": item["confidence"]} for item in cluster[:8]]
    return []


def scan_image_for_text(path: Path, runner=subprocess.run) -> list[dict]:
    outputs = []
    for psm in (11, 6):
        try:
            result = runner(["tesseract", str(path), "stdout", "--psm", str(psm), "-l", "eng+heb", "tsv"], capture_output=True, text=True, timeout=45, check=False)
        except OSError as exc:
            raise RuntimeError(f"stage=ocr; OCR pass PSM {psm} unavailable") from exc
        if result.returncode != 0:
            raise RuntimeError(f"OCR pass PSM {psm} unavailable")
        outputs.append(result.stdout)
    return ocr_evidence(outputs[0], outputs[1])


def quarantine_rejected(output: Path, error: str, review_dir: Path) -> tuple[Path, Path]:
    review_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time())}-{uuid.uuid4().hex[:10]}"
    image_path = review_dir / f"{output.stem}-{stamp}{output.suffix}"
    sidecar_path = image_path.with_suffix(".json")
    match = re.search(r"stage=([a-z-]+)", error)
    detected = []
    marker = "detected="
    if marker in error:
        try:
            detected = json.loads(error.split(marker, 1)[1])
        except json.JSONDecodeError:
            detected = []
    data = json.dumps({"fixtureReview": True, "nonPublishable": True, "stage": match.group(1) if match else "unknown", "diagnostics": detected[:8] if isinstance(detected, list) else []}, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{sidecar_path.name}.", dir=review_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, sidecar_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    output.replace(image_path)
    return image_path, sidecar_path


def validate_existing_role_asset(path: Path, role: str, expected_sha256: str | None = None, runner=subprocess.run) -> dict:
    dimensions = {"desktop": (2172, 724), "mobile": (1122, 1402)}
    if role not in dimensions:
        raise RuntimeError("stage=asset; unknown fixture image role")
    if "rejected-review" in path.parts:
        # Reviewed quarantine sources may be validated for promotion, but are
        # never valid proof/publication inputs themselves.
        source_review = True
    else:
        source_review = False
    if not path.is_file():
        raise RuntimeError("stage=asset; fixture image is missing")
    if not 10_000 <= path.stat().st_size <= 8_000_000:
        raise RuntimeError("stage=file-size; fixture image is outside safe bounds")
    try:
        probe = runner(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name", "-of", "json", str(path)], capture_output=True, text=True, timeout=20, check=False)
    except OSError as exc:
        raise RuntimeError("stage=dimensions-format; fixture image validation unavailable") from exc
    if probe.returncode != 0:
        raise RuntimeError("stage=dimensions-format; fixture image validation failed")
    try:
        stream = json.loads(probe.stdout)["streams"][0]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("stage=dimensions-format; fixture image validation failed") from exc
    width, height = dimensions[role]
    if (stream.get("width"), stream.get("height"), stream.get("codec_name")) != (width, height, "webp"):
        raise RuntimeError("stage=dimensions-format; fixture image role contract failed")
    try:
        detected = scan_image_for_text(path, runner=runner)
    except RuntimeError as exc:
        raise RuntimeError("stage=ocr; " + str(exc)) from exc
    if detected:
        raise RuntimeError("stage=ocr; detected=" + json.dumps(detected, ensure_ascii=False, separators=(",", ":")))
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected_sha256 and sha256 != expected_sha256:
        raise RuntimeError("stage=sha256; fixture image hash mismatch")
    return {"role": role, "path": str(path), "width": width, "height": height, "format": "webp", "fileSize": path.stat().st_size, "sha256": sha256, "sourceReview": source_review}


def promote_fixture_role(source: Path, role: str, output_dir: Path, runner=subprocess.run) -> dict:
    if "rejected-review" not in source.parts:
        raise RuntimeError("stage=promotion; only reviewed quarantine assets may be promoted")
    validate_existing_role_asset(source, role, runner=runner)
    filename = f"fixture-aihayom-{role}.webp"
    target = output_dir / filename
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", dir=output_dir)
    try:
        with os.fdopen(fd, "wb") as destination, source.open("rb") as origin:
            shutil.copyfileobj(origin, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    promoted = validate_existing_role_asset(target, role, runner=runner)
    promoted.update({"fixtureOnly": True, "nonPublishable": True, "promotedFromReview": True})
    return promoted


def convert_validate(raw: bytes, output: Path, width: int, height: int, runner=subprocess.run) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aihayom-image-") as temp:
        source = Path(temp) / "source.png"
        source.write_bytes(raw)
        # Scale to cover, then center-crop. Each aspect ratio is composed by a
        # separate API prompt and converted independently to the real site size.
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
        command = ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vf", vf, "-frames:v", "1", "-c:v", "libwebp", "-quality", "88", str(output)]
        try:
            conversion = runner(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=90, check=False)
        except OSError as exc:
            raise RuntimeError("stage=conversion; image conversion unavailable") from exc
        if conversion.returncode != 0:
            raise RuntimeError("stage=conversion; image conversion failed")
    try:
        probe = runner(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name", "-of", "json", str(output)], capture_output=True, text=True, timeout=20, check=False)
    except OSError as exc:
        raise RuntimeError("stage=dimensions-format; image validation unavailable") from exc
    if probe.returncode != 0:
        raise RuntimeError("stage=dimensions-format; image validation failed")
    try:
        stream = json.loads(probe.stdout)["streams"][0]
        if (stream.get("width"), stream.get("height"), stream.get("codec_name")) != (width, height, "webp"):
            raise RuntimeError("stage=dimensions-format; image dimensions or format are invalid")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("stage=dimensions-format; image validation failed") from exc
    if not 10_000 <= output.stat().st_size <= 8_000_000:
        raise RuntimeError("stage=file-size; image file size is outside safe bounds")
    try:
        detected = scan_image_for_text(output, runner=runner)
    except RuntimeError as exc:
        raise RuntimeError("stage=ocr; " + str(exc)) from exc
    if detected:
        raise RuntimeError("stage=ocr; detected=" + json.dumps(detected, ensure_ascii=False, separators=(",", ":")))


def generate_images(env_path: Path, output_dir: Path, concept: str, mode: dict,
                    fixture: bool = False, review_rejected: bool = False,
                    reuse_existing: bool = False, requester=request_image,
                    validator=convert_validate,
                    existing_validator=validate_existing_role_asset) -> dict:
    key = None
    output_dir.mkdir(parents=True, exist_ok=True)
    if not isinstance(concept, str) or not concept.strip() or len(concept) > 2000:
        raise RuntimeError("stage=concept; invalid approved cartoon concept")
    if mode.get("id") not in {item["id"] for item in CARTOON_MODES} or not mode.get("traits"):
        raise RuntimeError("stage=mode; invalid approved cartoon mode")
    prefix = "fixture-aihayom-" if fixture else "cartoon-"
    specs = [
        (
            "desktop", f"{prefix}desktop.webp",
            "a native panoramic 3:1 newspaper strip. Arrange the visual idea laterally across the canvas. "
            "Keep every meaningful object and every complete figure fully visible inside the middle 42 percent horizontal safe band, "
            "with expendable plain paper margin above and below. Nothing important may touch or cross any edge. "
            "Do not reuse, crop, zoom, or imitate a portrait composition",
            "1536x1024", 2172, 724,
        ),
        (
            "mobile", f"{prefix}mobile.webp",
            "a native tall newspaper composition with the visual idea arranged vertically. Keep every meaningful object fully visible "
            "with generous breathing room on all four edges. Do not reuse, crop, zoom, or imitate a landscape composition",
            "1024x1536", 1122, 1402,
        ),
    ]
    results = []
    total_usage = {}
    for role, filename, framing, size, width, height in specs:
        scope = "Fixture only, non-publishable." if fixture else "Approved AI Hayom edition artwork."
        prompt = f"{CHARTER}\n\n{NO_TEXT_INVARIANT}\n\nApproved editorial mode: {mode['id']} — {mode['traits']}. Use these high-level traits as original guidance only; do not imitate or copy any named artist. Approved AI Hayom cartoon concept: {concept}\nCompose this version independently as {framing}. Preserve only the central metaphor and house style across formats; the spatial arrangement must be purpose-built for this canvas. {scope}"
        output = output_dir / filename
        if reuse_existing and output.exists():
            reused = existing_validator(output, role)
            reused.update({"reused": True, "fixtureOnly": fixture, "nonPublishable": fixture})
            results.append(reused)
            continue
        if key is None:
            key = load_key(env_path)
        raw, usage = requester(key, prompt, size)
        try:
            validator(raw, output, width, height)
        except (RuntimeError, OSError) as exc:
            if review_rejected:
                if output.exists():
                    quarantine_rejected(output, str(exc), output_dir.parent / "rejected-review")
            else:
                output.unlink(missing_ok=True)
            raise
        results.append({"role": role, "path": str(output), "width": width, "height": height,
                        "format": "webp", "sizeRequest": size,
                        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                        "fileSize": output.stat().st_size, "reused": False,
                        "fixtureOnly": fixture, "nonPublishable": fixture})
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v
    return {"model": MODEL, "endpoint": API_ENDPOINT, "images": results, "usage": total_usage,
            "maxImages": 2, "fixtureOnly": fixture,
            "publication": "blocked" if fixture else "approval-required", "cartoonMode": mode}


def generate_fixture(env_path: Path, output_dir: Path, review_rejected: bool = False,
                     reuse_existing: bool = False, mode: dict | None = None,
                     requester=request_image, validator=convert_validate,
                     existing_validator=validate_existing_role_asset) -> dict:
    concept = "A calm human hand lowers a simple red safety barrier in front of a rushing river of black ink-like wind, while a small warm light reveals the clear path beyond. Purely visual metaphor for verification before release. No paper, screens, signs, labels, keyboards, books, packaging, clothing marks, or text-bearing surfaces."
    return generate_images(env_path, output_dir, concept, mode or CARTOON_MODES[0], fixture=True,
                           review_rejected=review_rejected, reuse_existing=reuse_existing,
                           requester=requester, validator=validator,
                           existing_validator=existing_validator)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "output" / "fixtures")
    parser.add_argument("--review-rejected", action="store_true", help="quarantine rejected fixture output for review; never publishable")
    parser.add_argument("--reuse-existing", action="store_true", help="reuse and validate existing canonical fixture roles")
    parser.add_argument("--promote-source", type=Path, help="promote one reviewed desktop/mobile quarantine asset")
    parser.add_argument("--role", choices=("desktop", "mobile"), help="role for --promote-source")
    parser.add_argument("--mode-id", choices=tuple(item["id"] for item in CARTOON_MODES), default=CARTOON_MODES[0]["id"])
    args = parser.parse_args()
    try:
        if args.promote_source:
            if not args.role:
                raise RuntimeError("--role is required with --promote-source")
            result = promote_fixture_role(args.promote_source, args.role, args.output_dir)
        else:
            result = generate_fixture(args.env, args.output_dir, review_rejected=args.review_rejected, reuse_existing=args.reuse_existing, mode=next(item for item in CARTOON_MODES if item["id"] == args.mode_id))
    except (OSError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
