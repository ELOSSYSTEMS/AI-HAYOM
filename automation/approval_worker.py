#!/usr/bin/env python3
"""Deterministic AI Hayom approval worker.

One JSON request enters on stdin. The worker validates identity and immutable
content, performs only the requested state transition, and writes state
atomically. Telegram polling remains owned by the Hermes gateway.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from ai_hayom import (
    ApprovalError, Config, LockBusy, atomic_write_json, auth_confirmed,
    build_final_proof, build_plan_message, content_hash, edition_prompt, final_proof_hash,
    handle_approval, now_utc, proposal_from_inference, publish_local_commit,
    push_main, wait_for_production,
)
from image_generation import generate_fixture, generate_images

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"


def _validate_request(state: dict, request: dict, proposal: object) -> None:
    sender = str(request.get("sender", ""))
    if not state.get("allowedChatId") or sender != str(state["allowedChatId"]):
        raise ApprovalError("approval sender is not allowlisted")
    if content_hash(proposal) != state.get("contentHash"):
        raise ApprovalError("proposal content hash changed")


def _revision(state: dict, instruction: str, config: Config) -> dict:
    if state.get("fixture"):
        raise ApprovalError("fixture revisions are not supported")
    if state.get("state") not in {"PLAN_PENDING", "HELD"}:
        raise ApprovalError("revision requires a pending editorial plan")
    if config.inference.get("fixtureOnlyUntilApproved", True) or not auth_confirmed(config):
        raise ApprovalError("production drafting is not enabled")
    bundle_path = Path(str(state.get("researchBundle", "")))
    if not bundle_path.is_file():
        raise RuntimeError("research bundle is unavailable")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    command = config.inference.get("command")
    if not isinstance(command, list) or not command:
        raise RuntimeError("inference command is unavailable")
    current = state["proposal"]
    prompt = edition_prompt(bundle, state["edition"], current["date"])
    prompt += "\nREVISION:\n" + instruction + "\nCURRENT EDITION:\n" + json.dumps(current["editionPayload"], ensure_ascii=False)
    completed = subprocess.run([str(item) for item in command] + [prompt], capture_output=True,
                               text=True, timeout=300, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("revision inference failed")
    raw = json.loads(completed.stdout)
    proposal = proposal_from_inference(raw, bundle, state["edition"], current["date"],
                                       Path(config.paths["websiteRepo"]))
    proposal["cartoonMode"] = current["cartoonMode"]
    revision = int(state.get("revision", 0)) + 1
    proposal["proposalId"] = f"{state['proposalId']}-r{revision}"
    updated = dict(state)
    updated.update({"proposalId": proposal["proposalId"], "proposal": proposal,
                    "state": "PLAN_PENDING", "contentHash": content_hash(proposal),
                    "createdAt": now_utc(), "consumed": False, "revision": revision})
    updated.pop("finalProof", None)
    return updated


def _current_cartoon_choice(state: dict) -> int:
    proposal = state["proposal"]
    proof = state.get("finalProof", {})
    explicit = proof.get("cartoonChoice")
    if explicit in {1, 2, 3}:
        return explicit
    concept = proof.get("cartoonConcept")
    if concept in proposal.get("cartoonConcepts", []):
        return proposal["cartoonConcepts"].index(concept) + 1
    return int(proposal["recommendedCartoon"])


def _generate_proof(state: dict, config: Config, regenerate: bool = False,
                    cartoon_choice: int | None = None) -> dict:
    proposal = state["proposal"]
    mode = proposal["cartoonMode"]
    if state.get("fixture"):
        result = generate_fixture(ROOT / ".env", ROOT / "output" / "fixtures",
                                  reuse_existing=not regenerate, mode=mode)
        if len(result.get("images", [])) != 2:
            raise RuntimeError("fixture image generation or validation failed")
        proof = {
            "fixture": True, "proposalId": state["proposalId"],
            "headline": "בדיקת מערכת: הוכחת מסלול העריכה",
            "introduction": "הוכחה מקומית בלבד. אין לפרסם.",
            "readingTime": "דקת קריאה",
            "cartoon": {"desktop": result["images"][0]["path"],
                        "mobile": result["images"][1]["path"],
                        "alt": "עורך עוצר מכונת דפוס באמצעות בלם בטיחות אדום, מטפורה לבדיקה לפני פרסום"},
            "sources": ["https://example.com/fixture"], "images": result["images"],
            "cartoonMode": result.get("cartoonMode"),
            "validation": "fixture-only; publication disabled",
        }
    else:
        edition = proposal["editionPayload"]
        choice_number = cartoon_choice or _current_cartoon_choice(state)
        if choice_number not in {1, 2, 3}:
            raise ApprovalError("cartoon choice must be 1, 2, or 3")
        concept = proposal["cartoonConcepts"][choice_number - 1]
        revision = int(state.get("cartoonRevision", 0)) + (1 if regenerate else 0)
        safe_proposal_id = re.sub(r"[^A-Za-z0-9._-]", "-", str(state["proposalId"]))
        batch_id = f"{safe_proposal_id}-c{choice_number}-r{revision}"
        result = generate_images(ROOT / ".env", ROOT / "output" / "proofs" / batch_id,
                                 concept, mode, fixture=False, reuse_existing=True)
        proof = build_final_proof(edition, result["images"], Path(config.paths["websiteRepo"]))
        proof.update({"fixture": False, "proposalId": state["proposalId"],
                      "cartoonMode": mode, "cartoonConcept": concept,
                      "cartoonChoice": choice_number})
    updated = dict(state)
    updated.update({"state": "PROOF_PENDING", "consumed": False,
                    "finalProof": proof, "contentHash": content_hash(proof),
                    "lastCartoonMode": mode["id"], "proofCreatedAt": now_utc()})
    if not state.get("fixture"):
        updated["cartoonRevision"] = revision
    return updated


def _publish(state: dict, request: dict, config: Config, state_path: Path) -> dict:
    if state.get("fixture"):
        updated = handle_approval(state, "APPROVE PUBLISH", str(request.get("sender", "")), state["finalProof"])
        updated.update({"state": "PUBLISH_BLOCKED_FIXTURE",
                        "publication": {"blocked": True, "reason": "fixture is permanently non-publishable"}})
        return updated
    publication = config.raw.get("publication", {})
    if not publication.get("enabled"):
        raise ApprovalError("publication is not enabled")
    proof = state["finalProof"]
    if state.get("state") == "PROOF_PENDING":
        updated = handle_approval(state, "APPROVE PUBLISH", str(request.get("sender", "")), proof)
        edition, images = proof["edition"], proof["images"]
        updated["approvedProofHash"] = final_proof_hash(edition, images)
        local = publish_local_commit(edition, images, updated, Path(config.paths["websiteRepo"]))
        updated.update({"state": "PUSH_PENDING", "publication": local})
        atomic_write_json(state_path, updated)
    elif state.get("state") in {"PUSH_PENDING", "VERIFY_PENDING"}:
        _validate_request(state, request, proof)
        updated = dict(state)
        edition, images = proof["edition"], proof["images"]
    else:
        raise ApprovalError("publication command does not match current state")
    if not publication.get("push"):
        updated["state"] = "LOCAL_COMMIT_READY"
        return updated
    sha = updated["publication"]["commit"]
    if updated["state"] == "PUSH_PENDING":
        push_main(Path(config.paths["websiteRepo"]), sha, live_authorized=True)
        updated["state"] = "VERIFY_PENDING"
        atomic_write_json(state_path, updated)
    if not publication.get("verifyProduction"):
        updated["state"] = "PUSHED"
        return updated
    verification = wait_for_production("https://" + config.raw["deployment"]["productionDomain"], edition,
                                       proof["imageHashes"],
                                       timeout_seconds=int(publication.get("verificationTimeoutSeconds", 300)))
    updated.update({"state": "PUBLISHED", "publishedAt": now_utc(), "verification": verification})
    return updated


def process(request: dict) -> dict:
    state_path = Path(request["statePath"])
    config = Config.load(Path(request.get("configPath", CONFIG)))
    with LockBusy.hold(state_path.with_suffix(".approval.lock")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        command = str(request["command"]).strip()
        proposal = request["proposal"]
        _validate_request(state, request, proposal)
        if command == "APPROVE PLAN":
            handle_approval(state, command, str(request.get("sender", "")), proposal)
            updated = _generate_proof(state, config)
        elif command == "APPROVE PUBLISH":
            updated = _publish(state, request, config, state_path)
        elif command == "REGENERATE CARTOON" or re.fullmatch(r"REGENERATE CARTOON [1-3]", command):
            if state.get("state") != "PROOF_PENDING":
                raise ApprovalError("cartoon regeneration requires a pending final proof")
            requested_choice = int(command[-1]) if re.fullmatch(r"REGENERATE CARTOON [1-3]", command) else None
            updated = _generate_proof(state, config, regenerate=True, cartoon_choice=requested_choice)
        elif command == "HOLD" and state.get("state") == "PLAN_PENDING":
            updated = {**state, "state": "HELD", "heldAt": now_utc()}
        elif command == "CANCEL EDITION" and state.get("state") in {"PLAN_PENDING", "HELD", "PROOF_PENDING"}:
            updated = {**state, "state": "CANCELED", "consumed": True, "canceledAt": now_utc()}
        elif re.fullmatch(r"REPLACE \d{2}", command):
            updated = _revision(state, f"Replace story {command[-2:]} with the strongest unused eligible story.", config)
        elif command.startswith("REVISE ") and command[7:].strip():
            updated = _revision(state, command[7:].strip(), config)
        else:
            raise ApprovalError("command does not match current proposal state")
        atomic_write_json(state_path, updated)
        return {"accepted": True, "state": updated["state"], "fixture": bool(updated.get("fixture")),
                "finalProof": updated.get("finalProof"), "publication": updated.get("publication"),
                "proposal": updated.get("proposal") if updated["state"] == "PLAN_PENDING" else None,
                "planMessage": build_plan_message(updated["proposal"]) if updated["state"] == "PLAN_PENDING" else None}


def main() -> int:
    try:
        print(json.dumps(process(json.load(sys.stdin)), ensure_ascii=False))
        return 0
    except (ApprovalError, KeyError, ValueError, json.JSONDecodeError, OSError, RuntimeError) as exc:
        print(json.dumps({"accepted": False, "error": str(exc)[:1000]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
