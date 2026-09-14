#!/usr/bin/env python3
"""Minimal, draft-only AI Hayom automation foundation."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import email.utils
import fcntl
import hashlib
import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
PLACEHOLDER = "REPLACE_WITH_"
EDITORIAL_CARTOON_CHARTER = """Premium modern newspaper editorial cartoon. Elegant visual metaphor, restrained caricature, intelligent visual wit, economical linework, strong composition, and immediate readability. Use original visual language only, never imitate a named artist, existing cartoon, composition, character, or signature style.

Avoid grotesque exaggeration, partisan propaganda, photorealism, generic glowing robots, stock AI imagery, excessive detail, text-heavy jokes, and visual clutter.

Maintain a recognizable AI Hayom house style: black editorial ink; warm off-white newsprint background; restrained red accent; confident imperfect linework; one strong visual idea; minimal or no text inside the image; culturally intelligent rather than cruel; suitable for a premium independent newspaper."""

CARTOON_MODES = [
    {"id": "symbolic-clarity", "traits": "clean, symbolic, instantly readable"},
    {"id": "historical-crosshatch", "traits": "engraved newsprint texture and emblematic composition"},
    {"id": "satirical-caricature", "traits": "energetic exaggeration and dense visual satire, without cruelty"},
    {"id": "elegant-political-ink", "traits": "strong silhouettes and economical ink"},
    {"id": "humane-understatement", "traits": "restrained, human-scale observation"},
    {"id": "conceptual-line", "traits": "intelligent idea-first line drawing"},
    {"id": "sharp-contemporary", "traits": "crisp modern editorial illustration"},
    {"id": "expressive-wash", "traits": "loose ink and restrained watercolor"},
    {"id": "kinetic-storyboard", "traits": "digital-first sense of motion in a static frame"},
    {"id": "bold-metaphor", "traits": "one powerful metaphor with minimal elements"},
    {"id": "witty-composition", "traits": "contemporary newspaper wit and highly controlled composition"},
    {"id": "refined-international", "traits": "deceptively simple global-news sensibility"},
]


def select_cartoon_mode(edition: str, date: str, proposal_id: str, prior_mode: str | None = None) -> dict:
    seed = f"{edition}|{date}|{proposal_id}".encode("utf-8")
    index = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") % len(CARTOON_MODES)
    if prior_mode and len(CARTOON_MODES) > 1 and CARTOON_MODES[index]["id"] == prior_mode:
        index = (index + 1) % len(CARTOON_MODES)
    return dict(CARTOON_MODES[index])


class ApprovalError(RuntimeError):
    pass


def content_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_inference_output(raw: str) -> dict:
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("inference output is not JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("inference output must be a JSON object")
    return value


def parse_with_one_format_repair(raw: str, command: list, inference_runner=subprocess.run) -> tuple[dict, str | None]:
    """Repair JSON syntax once without inviting editorial changes or retries."""
    try:
        return parse_inference_output(raw), None
    except RuntimeError:
        repair_prompt = (
            "Repair only the JSON syntax in MALFORMED_RESPONSE. Preserve every fact, URL, Hebrew phrase, "
            "field, array item, and value. Do not summarize, improve, add, remove, or fact-check content. "
            "Return exactly one valid JSON object with no markdown or commentary.\nMALFORMED_RESPONSE:\n" + raw
        )
        completed = inference_runner([str(x) for x in command] + [repair_prompt], capture_output=True,
                                     text=True, timeout=180, check=False)
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError("inference JSON repair failed")
        return parse_inference_output(completed.stdout), completed.stdout


def normalize_text(value: object) -> str:
    return re.sub(r"[^\w\u0590-\u05ff]+", " ", str(value).lower()).strip()


def canonical_reading_time(value: object) -> str:
    """Normalize a model's leading MM:SS duration to the site contract."""
    match = re.match(r"^\s*(\d{1,2}):([0-5]\d)(?:\s|,|$)", str(value))
    if not match:
        raise RuntimeError("totalReadingTime must be MM:SS")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def breaking_fingerprint(item: dict) -> str:
    title = normalize_text(item.get("title", ""))
    # Headlines are the most stable cross-source identity; URLs and update copy are not.
    words = title.split()
    return hashlib.sha256(" ".join(words[:40]).encode("utf-8")).hexdigest()[:24]


def handle_approval(state: dict, command: str, sender: str, proposal: object) -> dict:
    if not state.get("allowedChatId") or not sender or sender != str(state["allowedChatId"]):
        raise ApprovalError("approval sender is not allowlisted")
    if state.get("consumed"):
        raise ApprovalError("approval already consumed")
    if content_hash(proposal) != state.get("contentHash"):
        raise ApprovalError("proposal content hash changed")
    command = command.strip()
    transitions = {
        ("PLAN_PENDING", "APPROVE PLAN"): "PLAN_APPROVED",
        ("HELD", "APPROVE PLAN"): "PLAN_APPROVED",
        ("PROOF_PENDING", "APPROVE PUBLISH"): "PUBLISH_APPROVED",
    }
    next_state = transitions.get((state.get("state"), command))
    if not next_state:
        raise ApprovalError("command does not match current proposal state")
    result = dict(state)
    result.update({"state": next_state, "approvalAt": now_utc(), "consumed": True})
    return result


def now_utc() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class LockBusy(RuntimeError):
    @classmethod
    @contextlib.contextmanager
    def hold(cls, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise cls(f"another run holds {path}") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Config:
    def __init__(self, raw: dict, root: Path):
        self.raw, self.root = raw, root
        self.timezone = raw.get("timezone", "Asia/Jerusalem")
        self.target_time = raw.get("target_time", "08:00")
        self.feeds = raw.get("feeds", [])
        self.breaking = raw.get("breaking", {})
        self.paths = {key: root / value for key, value in raw.get("paths", {}).items()}
        self.inference = raw.get("inference", {})
        self.telegram = raw.get("telegram", {})

    @classmethod
    def load(cls, path: Path) -> "Config":
        return cls(json.loads(path.read_text(encoding="utf-8")), path.parent)

    @property
    def inference_enabled(self) -> bool:
        return bool(self.inference.get("enabled"))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram.get("enabled"))


def clean_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def env_value(path: Path, name: str) -> str:
    """Read one exact value without loading or exposing the rest of the env file."""
    if not path.is_file() or path.stat().st_mode & 0o077:
        return ""
    prefix = name + "="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def deduplicate_items(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for item in items:
        url = clean_url(str(item.get("url", "")))
        fingerprint = breaking_fingerprint(item)
        if not url or url in seen or fingerprint in seen:
            continue
        seen.add(url)
        seen.add(fingerprint)
        result.append({**item, "url": url})
    return result


def published_story_urls(config: Config, days: int = 7, now: dt.datetime | None = None) -> set[str]:
    """Return source URLs used by recent real editions, excluding historical test editions."""
    repo = Path(config.paths.get("websiteRepo", ""))
    if not repo.is_dir():
        return set()
    cutoff = (now or dt.datetime.now(UTC)) - dt.timedelta(days=days)
    urls: set[str] = set()
    for path in (repo / "edition").glob("[0-9][0-9][0-9]/edition.json"):
        try:
            edition = json.loads(path.read_text(encoding="utf-8"))
            published = dt.date.fromisoformat(str(edition.get("publicationDate", "")))
            if dt.datetime.combine(published, dt.time.min, tzinfo=UTC) < cutoff:
                continue
            for story in edition.get("stories", []):
                for source in story.get("sources", []):
                    value = source.get("url", "") if isinstance(source, dict) else source
                    if value:
                        urls.add(clean_url(str(value)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return urls


def _text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def parse_feed(data: bytes, feed_url: str, retrieved_at: str) -> list[dict]:
    root = ET.fromstring(data)
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    items = []
    for entry in entries:
        link = entry.find("link")
        if link is None:
            link = entry.find("{http://www.w3.org/2005/Atom}link")
        url = (link.get("href", "") if link is not None and link.get("href") else _text(link))
        title = _text(entry.find("title")) or _text(entry.find("{http://www.w3.org/2005/Atom}title"))
        summary = (_text(entry.find("description")) or _text(entry.find("summary")) or
                   _text(entry.find("{http://www.w3.org/2005/Atom}summary")))
        published = (_text(entry.find("pubDate")) or _text(entry.find("published")) or
                     _text(entry.find("updated")) or
                     _text(entry.find("{http://www.w3.org/2005/Atom}published")) or
                     _text(entry.find("{http://www.w3.org/2005/Atom}updated")))
        items.append({"url": url, "title": title[:300], "summary": summary[:1500],
                      "feed": feed_url, "publishedAt": published,
                      "retrievedAt": retrieved_at})
    return items


def collect_feeds(config: Config, allow_network: bool = False, opener=urllib.request.urlopen) -> dict:
    collected, feeds = [], []
    for feed in config.feeds:
        url = str(feed.get("url", "")) if isinstance(feed, dict) else str(feed)
        if not url or PLACEHOLDER in url:
            continue
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            feeds.append({"url": url, "status": "rejected", "error": "invalid HTTPS feed URL"})
            continue
        if not allow_network:
            raise RuntimeError("network collection requested but --allow-network was not supplied")
        retrieved = now_utc()
        request = urllib.request.Request(url, headers={"User-Agent": "AI-Hayom-draft-pilot/1.0"})
        try:
            with opener(request, timeout=20) as response:
                payload = response.read(2_000_001)
                if len(payload) > 2_000_000:
                    raise RuntimeError("feed response exceeded size limit")
            feed_items = parse_feed(payload, url, retrieved)
            if not feed_items:
                raise RuntimeError("feed contained no items")
        except (OSError, TimeoutError, RuntimeError, ET.ParseError, urllib.error.URLError) as exc:
            feeds.append({"url": url, "name": feed.get("name", "") if isinstance(feed, dict) else "",
                          "status": "failed", "error": type(exc).__name__})
            continue
        tier = feed.get("tier", "secondary") if isinstance(feed, dict) else "secondary"
        for item in feed_items:
            source_name = feed.get("name", url) if isinstance(feed, dict) else url
            publisher = feed.get("publisher", source_name) if isinstance(feed, dict) else source_name
            if isinstance(feed, dict):
                host = (urllib.parse.urlsplit(str(item.get("url", ""))).hostname or "").lower()
                for domain, mapped_name in feed.get("publisherByDomain", {}).items():
                    if host == domain or host.endswith("." + domain):
                        source_name = publisher = mapped_name
                        break
            item.update({"sourceName": source_name,
                         "sourceTier": tier,
                         "sourceType": feed.get("sourceType", {
                             "primary": "PRIMARY_DISCLOSURE",
                             "regulator": "REGULATORY_NOTICE",
                             "secondary": "REPUTABLE_JOURNALISM",
                         }.get(tier, "UNCLASSIFIED")) if isinstance(feed, dict) else "UNCLASSIFIED",
                         "publisher": publisher,
                         "discoveryFeed": bool(feed.get("discovery")) if isinstance(feed, dict) else False,
                         "indirectLink": bool(feed.get("indirectLinks")) if isinstance(feed, dict) else False})
        feeds.append({"url": url, "name": feed.get("name", "") if isinstance(feed, dict) else "",
                      "tier": tier, "status": "ok", "retrievedAt": retrieved,
                      "itemCount": len(feed_items)})
        collected.extend(feed_items)
    healthy = sum(1 for feed in feeds if feed.get("status") == "ok")
    minimum = int(config.raw.get("editorial", {}).get("minHealthyFeeds", 2))
    if healthy < minimum:
        raise RuntimeError(f"only {healthy} healthy feeds; minimum is {minimum}")
    editorial = config.raw.get("editorial", {})
    relevance_keywords = editorial.get("aiRelevanceKeywords", [
        "ai", "artificial intelligence", "machine learning", "deep learning",
        "generative", "gpt", "llm", "language model", "foundation model",
        "model", "agent", "robotics", "neural", "inference",
    ])
    relevant = [item for item in deduplicate_items(collected)
                if is_ai_relevant(item, relevance_keywords)]
    ranked = rank_items(relevant, collected, editorial.get("priorityKeywords", []))
    if editorial.get("excludeRecentlyPublished", True):
        recent_urls = published_story_urls(config, int(editorial.get("recentStoryDays", 7)))
        ranked = [item for item in ranked if clean_url(str(item.get("url", ""))) not in recent_urls]
    maximum = int(editorial.get("maxResearchItems", 40))
    fresh_hours = int(editorial.get("freshWindowHours", 24))
    fallback_hours = int(editorial.get("fallbackWindowHours", 72))
    max_age_hours = int(editorial.get("maxStoryAgeHours", 168))
    per_source = int(editorial.get("maxResearchItemsPerSource", 2))
    aggregate_items = [with_freshness_window(item, fresh_hours, fallback_hours, max_age_hours)
                       for item in ranked
                       if item.get("ageHours") is not None and item["ageHours"] <= max_age_hours]
    items = select_research_items(aggregate_items, max_age_hours, per_source, maximum,
                                  fresh_hours=fresh_hours, fallback_hours=fallback_hours)
    minimum_items = int(config.raw.get("editorial", {}).get("minStories", 4))
    if len(items) < minimum_items:
        raise RuntimeError(f"only {len(items)} eligible stories; minimum is {minimum_items}")
    return {"retrievedAt": now_utc(), "feeds": feeds, "healthyFeeds": healthy,
            "failedFeeds": len(feeds) - healthy, "items": items,
            "itemCount": len(items), "eligibleItemCount": len(ranked),
            "_aggregateItems": aggregate_items,
            "selectionPolicy": {"freshWindowHours": fresh_hours,
                                "fallbackWindowHours": fallback_hours,
                                "maxStoryAgeHours": max_age_hours,
                                "minFreshStories": int(editorial.get("minFreshStories", 3)),
                                "maxContextStories": int(editorial.get("maxContextStories", 1)),
                                "requiresAiRelevance": True,
                                "maxResearchItemsPerSource": per_source}}


def _published_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is None:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _story_tokens(item: dict) -> set[str]:
    return {word for word in normalize_text(item.get("title", "")).split() if len(word) >= 4}


def publisher_identity(item: dict) -> str:
    explicit = normalize_text(item.get("publisher", ""))
    if explicit:
        return explicit
    host = urllib.parse.urlsplit(str(item.get("url", ""))).hostname
    return (host or str(item.get("sourceName", "")) or str(item.get("feed", ""))).lower()


def with_freshness_window(item: dict, fresh_hours: int = 24, fallback_hours: int = 72,
                          max_age_hours: int = 168) -> dict:
    age = item.get("ageHours")
    if age is None or age > max_age_hours:
        window = "ineligible"
    elif age <= fresh_hours:
        window = "fresh"
    elif age <= fallback_hours:
        window = "fallback"
    else:
        window = "context"
    return {**item, "freshnessWindow": window}


def is_ai_relevant(item: dict, keywords: list[str]) -> bool:
    """Require an explicit AI signal; an AI-focused feed alone is insufficient."""
    haystack = normalize_text(f"{item.get('title', '')} {item.get('summary', '')}")
    tokens = set(haystack.split())
    for keyword in keywords:
        normalized = normalize_text(keyword)
        if not normalized:
            continue
        if (" " in normalized and normalized in haystack) or normalized in tokens:
            return True
    return False


def rank_items(items: list[dict], all_items: list[dict], keywords: list[str], now: dt.datetime | None = None) -> list[dict]:
    """Apply a small deterministic credibility, recency and corroboration score."""
    now = now or dt.datetime.now(UTC)
    tier_points = {"primary": 40, "regulator": 38, "secondary": 24}
    ranked = []
    for item in items:
        tokens = _story_tokens(item)
        independent = {publisher_identity(other) for other in all_items if publisher_identity(other) and
                       tokens and len(tokens & _story_tokens(other)) / max(1, len(tokens)) >= 0.6}
        published = _published_datetime(str(item.get("publishedAt", "")))
        age_hours = max(0.0, (now - published).total_seconds() / 3600) if published else None
        if age_hours is not None and age_hours <= 24:
            recency = 60 - (age_hours / 24 * 30)
        elif age_hours is not None and age_hours <= 72:
            recency = 20 - ((age_hours - 24) / 48 * 10)
        else:
            recency = 0
        consequence = min(25, score_item(item, keywords) * 5)
        score = tier_points.get(str(item.get("sourceTier", "secondary")), 15) + recency + consequence + min(20, max(0, len(independent) - 1) * 10)
        ranked.append({**item, "score": round(score, 2),
                       "independentPublishers": max(1, len(independent)),
                       "independentSources": max(1, len(independent)),
                       "ageHours": round(age_hours, 1) if age_hours is not None else None})
    return sorted(ranked, key=lambda item: (-item["score"], item.get("title", "")))


def select_research_items(ranked: list[dict], max_age_hours: int, per_source: int,
                          maximum: int, fresh_hours: int = 24, fallback_hours: int = 72) -> list[dict]:
    """Keep the research bundle current and prevent one feed dominating it."""
    selected = []
    source_counts: dict[str, int] = {}
    eligible = [with_freshness_window(item, fresh_hours, fallback_hours, max_age_hours)
                for item in ranked]
    for window in ("fresh", "fallback", "context"):
        for item in eligible:
            source = publisher_identity(item)
            if item["freshnessWindow"] != window or not source:
                continue
            if source_counts.get(source, 0) >= per_source:
                continue
            selected.append(item)
            source_counts[source] = source_counts.get(source, 0) + 1
            if len(selected) >= maximum:
                return selected
    return selected


def auth_confirmed(config: Config) -> bool:
    provider = str(config.inference.get("provider", ""))
    command = config.inference.get("auth_status_command")
    if not config.inference_enabled or not provider or PLACEHOLDER in provider or not isinstance(command, list):
        return False
    if any(PLACEHOLDER in str(part) for part in command):
        return False
    try:
        return subprocess.run([str(part) for part in command], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def edition_prompt(bundle: dict, edition_number: str, publication_date: str) -> str:
    prompt_bundle = json.loads(json.dumps(bundle, ensure_ascii=False))
    for index, item in enumerate(prompt_bundle.get("items", []), 1):
        item["sourceId"] = f"S{index:02d}"
    response_template = {
        "edition": {
            "schemaVersion": 1,
            "number": edition_number,
            "publicationDate": publication_date,
            "status": "draft",
            "language": "Hebrew",
            "headline": "כותרת ראשית קצרה בעברית",
            "coverDescription": "תיאור שער קצר בעברית",
            "introduction": "פתיחה קצרה בעברית",
            "aiDisclosure": "גילוי נאות קצר בעברית על סיוע בינה מלאכותית ובדיקה אנושית",
            "keywords": ["4", "or", "5", "Hebrew topics"],
            "cartoon": {
                "desktop": "cartoon-desktop.webp",
                "mobile": "cartoon-mobile.webp",
                "alt": "תיאור נגיש בעברית ללא טקסט בתוך האיור",
            },
            "stories": [{
                "section": "מדור קצר בעברית",
                "headline": "כותרת בעברית",
                "readingTime": "MM:SS",
                "quickRead": "משפט תקציר אחד בעברית",
                "summary": "סיכום עובדתי תמציתי בעברית",
                "whyItMatters": "מדוע הסיפור חשוב, בעברית",
                "sources": [{"sourceId": "S01"}],
            }],
            "totalReadingTime": "05:00",
            "takeaway": "שורה תחתונה קצרה בעברית",
        },
        "cartoonConcepts": [
            "English visual metaphor 1, no text or text-bearing surfaces",
            "English visual metaphor 2, no text or text-bearing surfaces",
            "English visual metaphor 3, no text or text-bearing surfaces",
        ],
        "recommendedCartoon": "1, 2, or 3",
        "uncertainties": "הסתייגויות עובדתיות קצרות בעברית",
    }
    policy = bundle.get("selectionPolicy", {})
    fresh_hours = int(policy.get("freshWindowHours", 24))
    min_fresh = min(int(policy.get("minFreshStories", 3)),
                    sum(item.get("freshnessWindow") == "fresh" for item in bundle.get("items", [])))
    max_context = int(policy.get("maxContextStories", 1))
    rules = [
        "Return exactly one JSON object with exactly the four top-level keys shown in RESPONSE_TEMPLATE.",
        "Do not wrap the object in a response key, markdown, commentary, or code fences.",
        "Write all edition copy in concise modern Hebrew; cartoon concepts must be English visual directions.",
        "Create 4-6 stories totaling approximately five minutes of reading time.",
        "Set totalReadingTime to exactly 05:00; do not add words or commentary to that field.",
        "Treat sourceType as authoritative metadata: attribute PRIMARY_DISCLOSURE and CORPORATE_PR claims to the organization; label OPINION_PIECE as דעה; prefer REPUTABLE_JOURNALISM for independent confirmation; never present corporate PR as independently verified.",
        "For every story, cite one or more exact sourceId values from RESEARCH; never copy, shorten, or output source URLs.",
        f"Use at least {min_fresh} stories from the previous {fresh_hours} hours when that many are available in RESEARCH.",
        f"Use no more than {max_context} context-window story and label why an older item is still relevant.",
        "Prefer consequential and recent stories; use no more than two stories from the same source or company.",
        "Distinguish reported facts from uncertainty and do not claim corroboration unless the bundle records it.",
        "Cartoon concepts must communicate visually with no words, letters, numbers, captions, logos, signatures, watermarks, screens, signs, labels, books, papers, or other text-bearing surfaces.",
    ]
    return ("Prepare one draft-only AI Hayom editorial proposal. Never publish, send messages, generate images, or modify Git.\n"
            "RESPONSE_TEMPLATE:\n" + json.dumps(response_template, ensure_ascii=False) +
            "\nRULES:\n" + json.dumps(rules, ensure_ascii=False) +
            "\nRESEARCH:\n" + json.dumps(prompt_bundle, ensure_ascii=False))


def resolve_source_references(raw: dict, bundle: dict) -> dict:
    """Replace model-selected source IDs with exact allowlisted URLs."""
    resolved = json.loads(json.dumps(raw, ensure_ascii=False))
    source_urls = {f"S{index:02d}": item.get("url")
                   for index, item in enumerate(bundle.get("items", []), 1) if item.get("url")}
    edition = resolved.get("edition", resolved)
    for story in edition.get("stories", []) if isinstance(edition, dict) else []:
        for source in story.get("sources", []) if isinstance(story, dict) else []:
            if not isinstance(source, dict):
                raise RuntimeError("inference source reference must be an object")
            source_id = str(source.get("sourceId", ""))
            if source_id:
                if source_id not in source_urls:
                    raise RuntimeError(f"inference used unknown source ID {source_id}")
                source.clear()
                source["url"] = source_urls[source_id]
    return resolved


def proposal_from_inference(raw: dict, bundle: dict, edition_number: str, publication_date: str,
                            repo: Path, prior_mode: str | None = None) -> dict:
    raw = resolve_source_references(raw, bundle)
    edition = map_inference_to_edition(raw, edition_number, publication_date, repo, check_assets=False)
    allowed_urls = {item["url"] for item in bundle.get("items", []) if item.get("url")}
    used_urls = {source.get("url") for story in edition.get("stories", []) for source in story.get("sources", []) if isinstance(source, dict)}
    if not used_urls or not used_urls.issubset(allowed_urls):
        raise RuntimeError("inference used a source outside the research bundle")
    items_by_url = {clean_url(str(item.get("url", ""))): item for item in bundle.get("items", [])}
    policy = bundle.get("selectionPolicy", {})
    available_fresh = sum(item.get("freshnessWindow") == "fresh" for item in bundle.get("items", []))
    required_fresh = min(int(policy.get("minFreshStories", 3)), len(edition.get("stories", [])), available_fresh)
    story_windows = []
    for story in edition.get("stories", []):
        windows = {items_by_url.get(clean_url(str(source.get("url", ""))), {}).get("freshnessWindow")
                   for source in story.get("sources", []) if isinstance(source, dict)}
        story_windows.append("fresh" if "fresh" in windows else
                             "fallback" if "fallback" in windows else
                             "context" if "context" in windows else "unknown")
    used_fresh = story_windows.count("fresh")
    if used_fresh < required_fresh:
        raise RuntimeError(f"edition uses only {used_fresh} fresh stories; minimum is {required_fresh}")
    used_context = story_windows.count("context")
    if used_context > int(policy.get("maxContextStories", 1)):
        raise RuntimeError("edition uses too many context-window stories")
    concepts = raw.get("cartoonConcepts", [])
    recommended = str(raw.get("recommendedCartoon", ""))
    if not isinstance(concepts, list) or len(concepts) != 3 or recommended not in {"1", "2", "3"}:
        raise RuntimeError("inference must provide three cartoon concepts and one recommendation")
    proposal_id = f"aihayom-{edition_number}-{hashlib.sha256((publication_date + edition['headline']).encode()).hexdigest()[:10]}"
    mode = select_cartoon_mode(edition_number, publication_date, proposal_id, prior_mode)
    bundle_items = {clean_url(str(item.get("url", ""))): item for item in bundle.get("items", [])}
    proposed_stories = []
    for story in edition["stories"]:
        url = clean_url(str(story["sources"][0]["url"]))
        evidence = bundle_items.get(url, {})
        proposed_stories.append({
            "headline": story["headline"],
            "whyItMatters": story["whyItMatters"],
            "url": url,
            "sourceName": str(evidence.get("sourceName", "")),
            "sourceTier": str(evidence.get("sourceTier", "")),
            "ageHours": evidence.get("ageHours"),
            "freshnessWindow": evidence.get("freshnessWindow", "unknown"),
            "indirectLink": bool(evidence.get("indirectLink")),
            "independentMentions": int(evidence.get("independentPublishers",
                                                    evidence.get("independentSources", 1))),
        })
    return {"proposalId": proposal_id, "edition": edition_number, "date": publication_date,
            "stories": proposed_stories,
            "readingTime": edition["totalReadingTime"], "cartoonConcepts": concepts,
            "recommendedCartoon": recommended, "uncertainties": str(raw.get("uncertainties", "")),
            "cartoonMode": mode, "editionPayload": edition}


def telegram_send(config: Config, text: str, opener=urllib.request.urlopen) -> bool:
    if not config.telegram_enabled:
        return False
    env_path = config.root / ".env"
    token = env_value(env_path, "TELEGRAM_BOT_TOKEN")
    chat_id = env_value(env_path, "TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram is enabled but credentials are unavailable")
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST",
                                     headers={"User-Agent": "AI-Hayom-automation/1.0"})
    try:
        with opener(request, timeout=30) as response:
            result = json.loads(response.read(1_000_000))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Telegram delivery failed") from exc
    if not result.get("ok"):
        raise RuntimeError("Telegram delivery was rejected")
    return True


def ensure_state_available(state_path: Path) -> dict:
    if not state_path.is_file():
        return {}
    current = json.loads(state_path.read_text(encoding="utf-8"))
    if current.get("state") in {"PLAN_PENDING", "HELD", "PLAN_APPROVED", "PROOF_PENDING", "PUBLISH_APPROVED",
                              "LOCAL_COMMIT_READY", "PUSH_PENDING", "VERIFY_PENDING"}:
        raise RuntimeError("an editorial proposal is already active")
    return current


def refresh_aggregate(config: Config, allow_network: bool = False,
                      opener=urllib.request.urlopen) -> tuple[dict, Path]:
    """Refresh the complete eligible feed pool without changing editorial state."""
    bundle = collect_feeds(config, allow_network=allow_network, opener=opener)
    aggregate_items = bundle.pop("_aggregateItems", [])
    aggregate_path = config.paths["research"] / "aggregate-latest.json"
    atomic_write_json(aggregate_path, {
        "contract": "AI Hayom approved-feed aggregate; research only, never publish directly",
        "retrievedAt": bundle["retrievedAt"],
        "feeds": bundle["feeds"],
        "healthyFeeds": bundle["healthyFeeds"],
        "failedFeeds": bundle["failedFeeds"],
        "itemCount": len(aggregate_items),
        "items": aggregate_items,
        "selectionPolicy": bundle.get("selectionPolicy", {}),
    })
    bundle["aggregate"] = str(aggregate_path)
    bundle["aggregateItemCount"] = len(aggregate_items)
    return bundle, aggregate_path


def historical_edition_ids(count: int) -> list[str]:
    """Oldest-to-newest IDs in an isolated pre-001 test namespace."""
    if not 1 <= count <= 30:
        raise ValueError("historical test count must be between 1 and 30")
    return [f"-{index:03d}" for index in range(count, 0, -1)]


def historical_research_bundle(aggregate: dict, publication_date: str, config: Config) -> dict:
    """Build a same-calendar-day replay without leaking later stories into the draft."""
    target = dt.date.fromisoformat(publication_date)
    zone = ZoneInfo(config.timezone)
    day_items = []
    for item in aggregate.get("items", []):
        published = _published_datetime(str(item.get("publishedAt", "")))
        if published and published.astimezone(zone).date() == target:
            day_items.append(item)
    editorial = config.raw.get("editorial", {})
    as_of = dt.datetime.combine(target, dt.time(23, 59, 59), zone).astimezone(UTC)
    ranked = rank_items(day_items, day_items, editorial.get("priorityKeywords", []), now=as_of)
    maximum = int(editorial.get("maxResearchItems", 40))
    per_source = int(editorial.get("maxResearchItemsPerSource", 2))
    selected = select_research_items(ranked, 24, per_source, maximum,
                                     fresh_hours=24, fallback_hours=24)
    minimum = int(editorial.get("minStories", 4))
    if len(selected) < minimum:
        raise RuntimeError(f"historical date {publication_date} has only {len(selected)} eligible stories; minimum is {minimum}")
    return {
        "contract": "AI Hayom historical replay; draft-only, never publish directly",
        "publicationDate": publication_date,
        "retrievedAt": aggregate.get("retrievedAt", ""),
        "feeds": aggregate.get("feeds", []),
        "healthyFeeds": aggregate.get("healthyFeeds", 0),
        "failedFeeds": aggregate.get("failedFeeds", 0),
        "items": selected,
        "itemCount": len(selected),
        "eligibleItemCount": len(ranked),
        "selectionPolicy": {
            "freshWindowHours": 24,
            "fallbackWindowHours": 24,
            "maxStoryAgeHours": 24,
            "minFreshStories": min(int(editorial.get("minFreshStories", 3)), len(selected)),
            "maxContextStories": 0,
            "requiresAiRelevance": True,
            "maxResearchItemsPerSource": per_source,
            "historicalReplay": True,
            "calendarDayBoundary": config.timezone,
        },
    }


def historical_test(config: Config, aggregate_path: Path, end_date: str, count: int,
                    inference_runner=subprocess.run) -> Path:
    """Generate isolated text-only historical editions; never mutate approval state or Git."""
    if not auth_confirmed(config):
        raise RuntimeError("approved inference provider is unavailable")
    command = config.inference.get("command")
    if not isinstance(command, list) or not command or any(PLACEHOLDER in str(x) for x in command):
        raise RuntimeError("inference command is unavailable")
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    end = dt.date.fromisoformat(end_date)
    ids = historical_edition_ids(count)
    dates = [end - dt.timedelta(days=count - 1 - index) for index in range(count)]
    stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    batch_root = config.paths["drafts"] / f"historical-{dates[0]:%Y%m%d}-{end:%Y%m%d}-{stamp}"
    batch_root.mkdir(parents=True, exist_ok=False)
    repo = Path(config.paths["websiteRepo"])
    generated = []
    prior_mode = None
    for edition_id, target in zip(ids, dates):
        publication_date = target.isoformat()
        bundle = historical_research_bundle(aggregate, publication_date, config)
        prompt = edition_prompt(bundle, edition_id, publication_date)
        edition_root = batch_root / edition_id
        edition_root.mkdir(parents=True, exist_ok=False)
        atomic_write_json(edition_root / "research.json", bundle)
        completed = inference_runner([str(x) for x in command] + [prompt], capture_output=True,
                                     text=True, timeout=300, check=False)
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError(f"historical inference failed for {edition_id}")
        (edition_root / "raw-response.txt").write_text(completed.stdout, encoding="utf-8")
        raw, repaired_response = parse_with_one_format_repair(completed.stdout, command, inference_runner)
        if repaired_response is not None:
            (edition_root / "format-repair-response.txt").write_text(repaired_response, encoding="utf-8")
        proposal = proposal_from_inference(raw, bundle, edition_id, publication_date, repo, prior_mode)
        proposal["editionPayload"]["status"] = "historical-test"
        atomic_write_json(edition_root / "proposal.json", proposal)
        atomic_write_json(edition_root / "edition.json", proposal["editionPayload"])
        generated.append({"number": edition_id, "publicationDate": publication_date,
                          "proposalId": proposal["proposalId"], "path": str(edition_root),
                          "formatRepaired": repaired_response is not None})
        prior_mode = proposal["cartoonMode"]["id"]
    manifest = {"contract": "isolated historical text test; no images, state, Telegram, Git, or publication",
                "aggregate": str(aggregate_path), "count": count, "editions": generated}
    atomic_write_json(batch_root / "manifest.json", manifest)
    return batch_root / "manifest.json"


def daily(config: Config, allow_network: bool = False, inference_runner=subprocess.run,
          telegram_sender=telegram_send) -> str:
    previous = ensure_state_available(config.paths["state"])
    bundle, aggregate_path = refresh_aggregate(config, allow_network=allow_network)
    bundle["contract"] = "AI Hayom edition research bundle; draft-only, never publish"
    research_path = config.paths["research"] / f"research-{dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    atomic_write_json(research_path, bundle)
    result = {"researchBundle": str(research_path), "aggregate": str(aggregate_path),
              "aggregateItems": bundle["aggregateItemCount"], "items": bundle["itemCount"], "draft": None,
              "proposal": None, "telegramSent": False}
    if config.inference.get("fixtureOnlyUntilApproved", True):
        result["inference"] = "blocked: production drafting is not enabled"
        print(json.dumps(result, indent=2))
        return str(research_path)
    if auth_confirmed(config):
        command = config.inference.get("command")
        if not isinstance(command, list) or not command or any(PLACEHOLDER in str(x) for x in command):
            result["inference"] = "blocked: command placeholder"
        else:
            repo = Path(config.paths["websiteRepo"])
            number = next_edition_number(repo)
            publication_date = dt.datetime.now(ZoneInfo(config.timezone)).date().isoformat()
            prompt = edition_prompt(bundle, number, publication_date)
            completed = inference_runner([str(x) for x in command] + [prompt], capture_output=True,
                                         text=True, timeout=300, check=False)
            if completed.returncode == 0 and completed.stdout.strip():
                try:
                    raw = parse_inference_output(completed.stdout)
                    prior_mode = previous.get("lastCartoonMode") or previous.get("proposal", {}).get("cartoonMode", {}).get("id")
                    proposal = proposal_from_inference(raw, bundle, number, publication_date, repo, prior_mode)
                except RuntimeError as exc:
                    result["inference"] = "failed closed: " + str(exc)
                    print(json.dumps(result, indent=2))
                    return str(research_path)
                draft = config.paths["drafts"] / f"draft-{dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
                atomic_write_json(draft, raw)
                chat_id = env_value(config.root / ".env", "TELEGRAM_CHAT_ID")
                state = {"proposalId": proposal["proposalId"], "edition": number, "fixture": False,
                         "proposal": proposal, "researchBundle": str(research_path), "state": "PLAN_PENDING",
                         "contentHash": content_hash(proposal), "createdAt": now_utc(), "consumed": False}
                if chat_id:
                    state["allowedChatId"] = chat_id
                atomic_write_json(config.paths["state"], state)
                plan_path = config.paths["drafts"] / f"plan-{proposal['proposalId']}.txt"
                plan_path.write_text(build_plan_message(proposal), encoding="utf-8")
                result["telegramSent"] = telegram_sender(config, build_plan_message(proposal))
                result["draft"] = str(draft)
                result["proposal"] = str(plan_path)
                result["inference"] = "completed locally"
            else:
                result["inference"] = f"failed closed (exit {completed.returncode})"
    else:
        result["inference"] = "blocked: provider/auth not explicitly confirmed"
    print(json.dumps(result, indent=2))
    return str(research_path)


def score_item(item: dict, keywords: list[str]) -> int:
    haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return sum(1 for word in keywords if str(word).lower() in haystack)


def build_approval_message(items: list[dict], threshold: int) -> str:
    lines = ["AI Hayom breaking-news candidates, human approval required:", ""]
    for index, item in enumerate(items, 1):
        lines += [f"{index}. [{item.get('score', 0)}] {item.get('title', '(untitled)')}", f"   {item['url']}"]
    lines += ["", "Commands:", "APPROVE", "REPLACE 03", "ADD TO NEXT EDITION", "IGNORE"]
    lines.insert(1, f"Threshold: {threshold}")
    return "\n".join(lines) + "\n"


def build_plan_message(proposal: dict) -> str:
    lines = [f"AI Hayom editorial proposal | {proposal['edition']} | {proposal['date']}",
             f"Proposal ID: {proposal['proposalId']}", "", "Ranked stories:"]
    for index, story in enumerate(proposal.get("stories", []), 1):
        age = story.get("ageHours")
        freshness = f"{round(float(age))}h old" if isinstance(age, (int, float)) else "age unavailable"
        mentions = int(story.get("independentMentions", 1))
        source_name = story.get("sourceName") or "approved source"
        window = story.get("freshnessWindow", "unknown")
        tier = story.get("sourceTier", "unknown")
        link_route = "indirect discovery link" if story.get("indirectLink") else "direct publisher link"
        lines.extend([f"{index}. {story['headline']}",
                      f"   Freshness: {freshness} ({window}) | Independent publishers: {mentions}",
                      f"   Source: {source_name} | Tier: {tier} | {link_route}",
                      f"   Why it matters: {story['whyItMatters']}", f"   Link: {story['url']}"])
    mode = proposal.get("cartoonMode", {})
    lines.extend(["", f"Estimated reading time: {proposal['readingTime']}", "", f"Editorial cartoon mode: {mode.get('id', '(not selected)')}", f"Mode traits: {mode.get('traits', '(not selected)')}", "", "Cartoon concepts:"])
    for index, concept in enumerate(proposal.get("cartoonConcepts", []), 1):
        lines.append(f"{index}. {concept}")
    lines.extend([f"Recommended: {proposal['recommendedCartoon']}", "", "Uncertainties: " + (proposal.get("uncertainties") or "None reported"),
                  "", "APPROVE PLAN", "REPLACE NN", "REVISE <instruction>", "HOLD", "CANCEL EDITION"])
    return "\n".join(lines) + "\n"


def create_fixture_proposal(prior_mode: str | None = None) -> dict:
    proposal = {
        "proposalId": "dryrun-20260913-aihayom",
        "edition": "002", "date": "2026-09-13",
        "stories": [{"headline": "בדיקת מערכת: מודל חדש מגיע עם תיעוד מלא", "whyItMatters": "הדגמה מקומית של זרימת העריכה, לא חדשות לפרסום.", "url": "https://example.com/fixture"}],
        "readingTime": "5 דקות", "cartoonConcepts": ["מכונת דפוס שמבקשת מהעורך לבדוק את המקור", "עורך שמחזיק זכוכית מגדלת מול כותרת", "עיתון שמפריד בין עובדה לפרשנות"],
        "recommendedCartoon": "1", "uncertainties": "Fixture בלבד; אין להשתמש בתוכן זה לפרסום.",
    }
    proposal["cartoonMode"] = select_cartoon_mode(proposal["edition"], proposal["date"], proposal["proposalId"], prior_mode)
    return proposal


def dry_run(config: Config) -> tuple[Path, str]:
    prior_mode = None
    previous = {}
    if config.paths["state"].is_file():
        try:
            previous = json.loads(config.paths["state"].read_text(encoding="utf-8"))
            prior_mode = previous.get("lastCartoonMode")
        except (OSError, json.JSONDecodeError):
            prior_mode = None
    prior_mode = prior_mode or previous.get("proposal", {}).get("cartoonMode", {}).get("id")
    proposal = create_fixture_proposal(prior_mode=prior_mode)
    state = {"proposalId": proposal["proposalId"], "edition": proposal["edition"], "fixture": True,
             "proposal": proposal, "state": "PLAN_PENDING",
             "contentHash": content_hash(proposal), "createdAt": now_utc(), "consumed": False}
    chat_id = env_value(config.root / ".env", "TELEGRAM_CHAT_ID")
    if chat_id:
        state["allowedChatId"] = chat_id
    state_path = config.paths["state"]
    atomic_write_json(state_path, state)
    output = config.paths["drafts"] / "dry-run-proposal.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_plan_message(proposal), encoding="utf-8")
    return output, build_plan_message(proposal)


def next_edition_number(repo: Path) -> str:
    editions = repo / "edition"
    numbers = [int(p.name) for p in editions.glob("[0-9][0-9][0-9]") if p.is_dir() and p.name.isdigit()]
    catalog = editions / "catalog.json"
    if catalog.exists():
        data = json.loads(catalog.read_text(encoding="utf-8"))
        numbers.extend(int(x) for x in data.get("editions", []) if re.fullmatch(r"\d{3}", str(x)))
    return f"{max(numbers, default=0) + 1:03d}"


def prepare_publication(edition: dict, state: dict, repo: Path) -> dict:
    if state.get("state") != "PUBLISH_APPROVED" or not state.get("consumed"):
        raise ApprovalError("publication requires a consumed APPROVE PUBLISH state")
    if not repo.is_dir() or not (repo / ".git").is_dir():
        raise RuntimeError("publication repository is unavailable")
    number = str(edition.get("number", ""))
    if not re.fullmatch(r"\d{3}", number):
        raise RuntimeError("edition number is invalid")
    edition_dir = repo / "edition" / number
    target = edition_dir / "edition.json"
    if target.exists():
        raise RuntimeError(f"edition collision: {number}")
    if edition_dir.exists():
        raise RuntimeError(f"edition directory collision: {number}")
    errors = validate_edition(edition, repo, check_assets=False)
    if errors:
        raise RuntimeError("edition validation failed: " + "; ".join(errors))
    return {"edition": number, "target": str(target), "catalog": str(repo / "edition" / "catalog.json"), "ready": True}


def validate_final_proof_package(edition: dict, image_records: list[dict], repo: Path) -> list[str]:
    errors = validate_edition(edition, repo, check_assets=False)
    text_fields = [edition.get("headline", ""), edition.get("introduction", ""), edition.get("takeaway", "")]
    text_fields += [story.get("headline", "") + story.get("summary", "") for story in edition.get("stories", []) if isinstance(story, dict)]
    if not any(re.search(r"[\u0590-\u05ff]", str(value)) for value in text_fields):
        errors.append("edition must contain Hebrew editorial text")
    if not isinstance(edition.get("totalReadingTime"), str) or not re.fullmatch(r"\d{2}:[0-5]\d", edition["totalReadingTime"]):
        errors.append("totalReadingTime does not match the reading-time contract")
    if len(image_records) != 2:
        errors.append("final proof requires exactly desktop and mobile image records")
        return errors
    expected = {"desktop": (2172, 724), "mobile": (1122, 1402)}
    for role, dimensions in expected.items():
        record = next((item for item in image_records if item.get("role") == role), None)
        if not record:
            errors.append(f"missing {role} image metadata")
            continue
        if record.get("fixtureOnly") or record.get("nonPublishable"):
            errors.append(f"{role} image is marked non-publishable")
            continue
        path = Path(str(record.get("path", "")))
        if "rejected-review" in path.parts:
            errors.append(f"{role} image is quarantined and non-publishable")
            continue
        if not path.is_file():
            errors.append(f"missing {role} image file")
            continue
        if record.get("format") != "webp" or (record.get("width"), record.get("height")) != dimensions:
            errors.append(f"invalid {role} image dimensions or format")
        if not 10_000 <= path.stat().st_size <= 8_000_000 or record.get("fileSize") != path.stat().st_size:
            errors.append(f"invalid {role} image file size")
        if record.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            errors.append(f"{role} image SHA-256 mismatch")
    return errors


def build_final_proof(edition: dict, image_records: list[dict], repo: Path) -> dict:
    errors = validate_final_proof_package(edition, image_records, repo)
    catalog_path = repo / "edition" / "catalog.json"
    if catalog_path.is_file():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        existing = {str(item) for item in catalog.get("editions", [])}
        if str(edition.get("number")) in existing:
            errors.append("edition number already exists in catalog")
        if str(edition.get("number")) != next_edition_number(repo):
            errors.append("edition number is not the next compatible catalog number")
    if errors:
        raise RuntimeError("final proof validation failed: " + "; ".join(errors))
    proof = {"edition": edition, "images": image_records, "imageHashes": {item["role"]: item["sha256"] for item in image_records}}
    proof["contentHash"] = content_hash(proof)
    return proof


def final_proof_hash(edition: dict, image_records: list[dict]) -> str:
    return content_hash({"edition": edition, "images": image_records, "imageHashes": {item["role"]: item["sha256"] for item in image_records}})


def map_inference_to_edition(raw: dict, edition_number: str, publication_date: str, repo: Path,
                             check_assets: bool = True) -> dict:
    if not isinstance(raw, dict):
        raise RuntimeError("inference output must be an edition object")
    edition = dict(raw.get("edition", raw))
    edition["number"] = edition_number
    edition["publicationDate"] = publication_date
    edition["status"] = "draft"
    edition["totalReadingTime"] = canonical_reading_time(edition.get("totalReadingTime", ""))
    edition["cartoon"] = {**edition.get("cartoon", {}), "desktop": "cartoon-desktop.webp",
                          "mobile": "cartoon-mobile.webp"}
    errors = validate_edition(edition, repo, check_assets=check_assets)
    if errors:
        raise RuntimeError("inference edition schema validation failed: " + "; ".join(errors))
    return edition


def verify_production_content(base_url: str, edition: dict, approved_asset_root: Path | None = None, approved_image_hashes: dict | None = None) -> dict:
    number = str(edition.get("number", ""))
    if not re.fullmatch(r"\d{3}", number):
        raise RuntimeError("production verification requires a three-digit edition")
    root = base_url.rstrip("/")
    def fetch(path: str) -> bytes:
        request = urllib.request.Request(root + path, headers={"User-Agent": "AI-Hayom-production-verifier/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"production returned HTTP {response.status}")
            return response.read(4_000_000)
    remote_json = json.loads(fetch(f"/edition/{number}/edition.json").decode("utf-8"))
    if content_hash(remote_json) != content_hash(edition):
        raise RuntimeError("production edition content mismatch")
    if fetch(f"/{number}") is None:
        raise RuntimeError("production edition route verification failed")
    expected_hashes = dict(approved_image_hashes or edition.get("imageHashes", {}))
    if approved_asset_root:
        edition_dir = approved_asset_root / "edition" / number
        for role in ("desktop", "mobile"):
            path = edition_dir / str(edition["cartoon"][role])
            if not path.is_file():
                raise RuntimeError(f"approved {role} asset is missing")
            expected_hashes[role] = hashlib.sha256(path.read_bytes()).hexdigest()
    if set(expected_hashes) != {"desktop", "mobile"}:
        raise RuntimeError("approved image hashes are required for production verification")
    remote_hashes = {}
    for role in ("desktop", "mobile"):
        asset = fetch(f"/edition/{number}/{edition['cartoon'][role]}")
        remote_hashes[role] = hashlib.sha256(asset).hexdigest()
        if remote_hashes[role] != expected_hashes[role]:
            raise RuntimeError(f"production {role} asset hash mismatch")
    return {"verified": True, "edition": number, "contentHash": content_hash(remote_json), "assetHashes": remote_hashes, "url": f"{root}/{number}"}


def publish_local_commit(edition: dict, image_records: list[dict], state: dict, repo: Path) -> dict:
    """Write and commit one approved edition locally, never push or deploy."""
    if state.get("fixture"):
        raise ApprovalError("fixture publication is permanently blocked")
    if state.get("state") != "PUBLISH_APPROVED" or not state.get("consumed"):
        raise ApprovalError("publication requires consumed APPROVE PUBLISH")
    if state.get("approvedProofHash") != final_proof_hash(edition, image_records):
        raise ApprovalError("approved final proof hash changed")
    prepared = prepare_publication(edition, state, repo)
    target = Path(prepared["target"])
    catalog_path = Path(prepared["catalog"])
    catalog_original = catalog_path.read_bytes()
    edition_dir = repo / "edition" / prepared["edition"]
    catalog = json.loads(catalog_original.decode("utf-8"))
    current = [str(value) for value in catalog.get("editions", [])]
    if prepared["edition"] in current:
        raise RuntimeError(f"edition collision in catalog: {prepared['edition']}")
    proof_errors = validate_final_proof_package(edition, image_records, repo)
    if proof_errors:
        raise RuntimeError("final proof validation failed: " + "; ".join(proof_errors))
    records = {item["role"]: item for item in image_records}
    if set(records) != {"desktop", "mobile"}:
        raise RuntimeError("final proof requires desktop and mobile assets")
    for role in ("desktop", "mobile"):
        source = Path(str(records[role]["path"]))
        if "rejected-review" in source.parts or not source.is_file():
            raise RuntimeError(f"invalid approved {role} source asset")
        if hashlib.sha256(source.read_bytes()).hexdigest() != records[role].get("sha256"):
            raise RuntimeError(f"approved {role} source hash mismatch")
    remote = subprocess.run(["git", "-C", str(repo), "ls-remote", "origin", "refs/heads/main"], capture_output=True, text=True, timeout=30, check=False)
    local = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False).stdout.strip()
    remote_sha = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.split() else ""
    if remote.returncode != 0:
        raise RuntimeError("could not verify remote main before publication")
    if remote_sha != local:
        raise RuntimeError("stale remote main; fetch and reconcile before publication")
    assets = [edition_dir / str(edition["cartoon"][role]) for role in ("desktop", "mobile")]
    add_paths = [str(target.relative_to(repo)), str(catalog_path.relative_to(repo))] + [str(asset.relative_to(repo)) for asset in assets]
    staged = False
    try:
        edition_dir.mkdir(parents=True, exist_ok=False)
        for role in ("desktop", "mobile"):
            source = Path(str(records[role]["path"]))
            destination = edition_dir / str(edition["cartoon"][role])
            shutil.copyfile(source, destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != records[role].get("sha256"):
                raise RuntimeError(f"copied {role} asset hash mismatch")
        atomic_write_json(target, edition)
        catalog["editions"] = current + [prepared["edition"]]
        catalog["latest"] = prepared["edition"]
        atomic_write_json(catalog_path, catalog)
        if validate_edition(edition, repo):
            raise RuntimeError("copied edition failed validation")
        add = subprocess.run(["git", "-C", str(repo), "add", "--"] + add_paths, capture_output=True, text=True, timeout=30, check=False)
        if add.returncode != 0:
            raise RuntimeError("git add failed")
        staged = True
        commit = subprocess.run(["git", "-C", str(repo), "commit", "-m", f"Publish AI Hayom edition {prepared['edition']}"], capture_output=True, text=True, timeout=30, check=False)
        if commit.returncode != 0:
            raise RuntimeError("git commit failed")
    except Exception:
        if staged:
            subprocess.run(["git", "-C", str(repo), "reset", "--"] + add_paths, capture_output=True, text=True, timeout=30, check=False)
        if edition_dir.exists():
            shutil.rmtree(edition_dir)
        catalog_path.write_bytes(catalog_original)
        raise
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    return {"edition": prepared["edition"], "commit": sha, "pushed": False, "deployed": False, "target": str(target), "catalog": str(catalog_path)}


def push_main(repo: Path, expected_sha: str, live_authorized: bool = False) -> str:
    if not live_authorized:
        raise ApprovalError("GitHub push requires separate live authorization")
    current = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False)
    if current.returncode != 0 or current.stdout.strip() != expected_sha:
        raise RuntimeError("local commit does not match approved SHA")
    result = subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError("GitHub push failed")
    return expected_sha


def wait_for_production(base_url: str, edition: dict, image_hashes: dict,
                        timeout_seconds: int = 300, interval_seconds: int = 10,
                        verifier=verify_production_content) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error = "production did not become ready"
    while time.monotonic() < deadline:
        try:
            return verifier(base_url, edition, approved_image_hashes=image_hashes)
        except (OSError, RuntimeError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
            if time.monotonic() + interval_seconds >= deadline:
                break
            time.sleep(interval_seconds)
    raise RuntimeError(f"production verification timed out: {last_error}")


def breaking_check(config: Config) -> None:
    state_path = config.paths["state"]
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    alerted = set(state.get("alertedUrls", []))
    files = sorted(config.paths["research"].glob("research-*.json"))
    items = []
    for path in files:
        items.extend(json.loads(path.read_text(encoding="utf-8")).get("items", []))
    keywords = config.breaking.get("keywords", [])
    threshold = int(config.breaking.get("threshold", 2))
    candidates = []
    for item in deduplicate_items(items):
        item["score"] = score_item(item, keywords)
        if item["score"] >= threshold and item["url"] not in alerted:
            candidates.append(item)
    message = build_approval_message(candidates, threshold) if candidates else "No new breaking-news candidates.\n"
    out = config.paths["alerts"] / f"breaking-{dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(message, encoding="utf-8")
    state["alertedUrls"] = sorted(alerted | {item["url"] for item in candidates})
    state["lastBreakingCheckAt"] = now_utc()
    atomic_write_json(state_path, state)
    print(json.dumps({"candidates": len(candidates), "proposal": str(out), "telegramSent": False}, indent=2))


def validate_edition(edition: dict, assets_root: Path, check_assets: bool = True) -> list[str]:
    errors = []
    required = ["schemaVersion", "number", "publicationDate", "status", "headline", "coverDescription",
                "introduction", "aiDisclosure", "keywords", "cartoon", "stories", "totalReadingTime", "takeaway"]
    for key in required:
        if key not in edition:
            errors.append(f"missing {key}")
    if errors:
        return errors
    if edition["schemaVersion"] != 1: errors.append("schemaVersion must be 1")
    if not isinstance(edition["number"], str) or not re.fullmatch(r"-?\d{3}", edition["number"]): errors.append("number must be three digits, optionally prefixed by - for historical tests")
    if not isinstance(edition["keywords"], list) or not 4 <= len(edition["keywords"]) <= 5: errors.append("keywords must contain 4-5 items")
    if not isinstance(edition["stories"], list) or not 4 <= len(edition["stories"]) <= 6: errors.append("stories must contain 4-6 items")
    if not isinstance(edition["aiDisclosure"], str) or not edition["aiDisclosure"].strip(): errors.append("aiDisclosure is required")
    cartoon = edition["cartoon"]
    edition_assets = assets_root / "edition" / str(edition.get("number", ""))
    for key in ("desktop", "mobile", "alt"):
        if not isinstance(cartoon, dict) or not str(cartoon.get(key, "")).strip(): errors.append(f"cartoon.{key} is required")
    if isinstance(cartoon, dict) and check_assets:
        for key in ("desktop", "mobile"):
            if cartoon.get(key) and not (edition_assets / cartoon[key]).is_file(): errors.append(f"cartoon {key} file missing: {edition_assets / cartoon[key]}")
    story_keys = ("section", "headline", "readingTime", "quickRead", "summary", "whyItMatters", "sources")
    for index, story in enumerate(edition["stories"] if isinstance(edition["stories"], list) else []):
        for key in story_keys:
            if key not in story: errors.append(f"story {index + 1} missing {key}")
        for source in story.get("sources", []) if isinstance(story, dict) else []:
            url = source.get("url", "") if isinstance(source, dict) else ""
            if urllib.parse.urlsplit(url).scheme != "https" or not urllib.parse.urlsplit(url).netloc: errors.append(f"story {index + 1} source must be HTTPS: {url}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.sample.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    daily_parser = sub.add_parser("daily")
    daily_parser.add_argument("--allow-network", action="store_true")
    aggregate_parser = sub.add_parser("aggregate")
    aggregate_parser.add_argument("--allow-network", action="store_true")
    historical_parser = sub.add_parser("historical-test")
    historical_parser.add_argument("--aggregate", type=Path, required=True)
    historical_parser.add_argument("--end-date", required=True)
    historical_parser.add_argument("--count", type=int, default=5)
    sub.add_parser("breaking-check")
    sub.add_parser("dry-run")
    validate = sub.add_parser("validate")
    validate.add_argument("edition", type=Path)
    validate.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    config = Config.load(args.config)
    if args.command == "validate":
        try: edition = json.loads(args.edition.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc: print(f"invalid JSON: {exc}"); return 1
        errors = validate_edition(edition, args.assets)
        if errors: print("\n".join(errors)); return 1
        print("valid"); return 0
    with LockBusy.hold(config.paths["lock"]):
        if args.command == "daily": daily(config, args.allow_network)
        elif args.command == "aggregate":
            bundle, path = refresh_aggregate(config, args.allow_network)
            print(json.dumps({"aggregate": str(path), "items": bundle["aggregateItemCount"],
                              "shortlistItems": bundle["itemCount"],
                              "healthyFeeds": bundle["healthyFeeds"],
                              "failedFeeds": bundle["failedFeeds"]}, indent=2))
        elif args.command == "historical-test":
            manifest = historical_test(config, args.aggregate, args.end_date, args.count)
            print(json.dumps({"manifest": str(manifest), "count": args.count,
                              "published": False, "imagesGenerated": False}, indent=2))
        elif args.command == "breaking-check": breaking_check(config)
        else:
            path, message = dry_run(config)
            print(json.dumps({"proposal": str(path), "telegramSent": False}, ensure_ascii=False, indent=2))
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
