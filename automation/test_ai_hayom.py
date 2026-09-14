import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from pathlib import Path

from image_generation import OCR_CLUSTER_CONFIDENCE_THRESHOLD, OCR_CONFIDENCE_THRESHOLD, OCR_SHORT_WORD_CONFIDENCE_THRESHOLD, generate_fixture, generate_images, ocr_evidence, promote_fixture_role, quarantine_rejected, scan_image_for_text, validate_existing_role_asset, validate_ocr_output
from approval_worker import process as process_approval
from ai_hayom import (
    CARTOON_MODES,
    Config,
    LockBusy,
    ApprovalError,
    atomic_write_json,
    build_final_proof,
    build_approval_message,
    canonical_reading_time,
    content_hash,
    collect_feeds,
    daily,
    dry_run,
    deduplicate_items,
    edition_prompt,
    breaking_fingerprint,
    handle_approval,
    historical_edition_ids,
    historical_research_bundle,
    is_ai_relevant,
    map_inference_to_edition,
    next_edition_number,
    parse_inference_output,
    parse_with_one_format_repair,
    proposal_from_inference,
    rank_items,
    resolve_source_references,
    select_research_items,
    build_plan_message,
    create_fixture_proposal,
    final_proof_hash,
    select_cartoon_mode,
    prepare_publication,
    publish_local_commit,
    push_main,
    telegram_send,
    validate_final_proof_package,
    verify_production_content,
    validate_edition,
)


class AiHayomTests(unittest.TestCase):
    def test_feed_collection_survives_one_failed_source_and_ranks_results(self):
        xml = b"<rss><channel><item><title>Major model release</title><link>https://example.com/story</link><description>model safety launch</description><pubDate>Sun, 13 Sep 2026 08:00:00 GMT</pubDate></item></channel></rss>"
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _limit): return xml
        def opener(request, timeout):
            if "failed" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, None)
            return Response()
        config = Config({"feeds": [{"name":"good","url":"https://good.example/feed","tier":"primary"},
                                   {"name":"bad","url":"https://failed.example/feed","tier":"regulator"}],
                         "editorial": {"minHealthyFeeds": 1, "minStories": 1,
                                       "priorityKeywords": ["model", "safety"]}}, Path("/tmp"))
        bundle = collect_feeds(config, allow_network=True, opener=opener)
        self.assertEqual((bundle["healthyFeeds"], bundle["failedFeeds"], bundle["itemCount"]), (1, 1, 1))
        self.assertEqual(bundle["feeds"][1]["error"], "HTTPError")
        self.assertGreater(bundle["items"][0]["score"], 40)

    def test_discovery_feed_maps_original_article_domain_to_publisher(self):
        xml = b"<rss><channel><item><title>Reuters AI model report</title><link>https://www.reuters.com/technology/example</link><description>artificial intelligence</description><pubDate>Sun, 13 Sep 2026 08:00:00 GMT</pubDate></item></channel></rss>"
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _limit): return xml
        config = Config({"feeds": [{"name":"wire discovery", "url":"https://discovery.example/feed",
                                    "publisher":"wire", "publisherByDomain":{"reuters.com":"Reuters"},
                                    "tier":"secondary", "discovery":True}],
                         "editorial":{"minHealthyFeeds":1, "minStories":1}}, Path("/tmp"))
        bundle = collect_feeds(config, allow_network=True, opener=lambda *_args, **_kwargs: Response())
        self.assertEqual(bundle["items"][0]["publisher"], "Reuters")
        self.assertEqual(bundle["items"][0]["sourceName"], "Reuters")
        self.assertTrue(bundle["items"][0]["discoveryFeed"])

    def test_ranking_rewards_independent_sources(self):
        base = {"title":"Important model safety release today", "summary":"launch", "sourceTier":"primary", "publishedAt":"2026-09-13T08:00:00Z"}
        first = {**base, "url":"https://a.example/1", "feed":"https://a.example/feed"}
        second = {**base, "url":"https://b.example/2", "feed":"https://b.example/feed"}
        ranked = rank_items([first], [first, second], ["model"], now=__import__("datetime").datetime(2026, 9, 13, 9, tzinfo=__import__("datetime").timezone.utc))
        self.assertEqual(ranked[0]["independentSources"], 2)

    def test_ranking_counts_publishers_not_sibling_feeds(self):
        base = {"title":"Important AI model release today", "summary":"launch",
                "sourceTier":"primary", "publishedAt":"2026-09-13T08:00:00Z"}
        first = {**base, "url":"https://google.example/1", "feed":"https://google.example/ai", "publisher":"Google"}
        sibling = {**base, "url":"https://deepmind.example/2", "feed":"https://deepmind.example/feed", "publisher":"Google"}
        independent = {**base, "url":"https://news.example/3", "feed":"https://news.example/feed", "publisher":"News"}
        ranked = rank_items([first], [first, sibling, independent], ["model"],
                            now=__import__("datetime").datetime(2026, 9, 13, 9,
                            tzinfo=__import__("datetime").timezone.utc))
        self.assertEqual(ranked[0]["independentPublishers"], 2)

    def test_research_selection_orders_fresh_before_higher_scored_context(self):
        ranked = [
            {"title":"context", "url":"https://old.example/1", "publisher":"Old", "ageHours":100, "score":100},
            {"title":"fresh", "url":"https://new.example/1", "publisher":"New", "ageHours":2, "score":50},
            {"title":"fallback", "url":"https://mid.example/1", "publisher":"Mid", "ageHours":30, "score":75},
        ]
        selected = select_research_items(ranked, max_age_hours=168, per_source=2, maximum=40)
        self.assertEqual([item["title"] for item in selected], ["fresh", "fallback", "context"])
        self.assertEqual([item["freshnessWindow"] for item in selected], ["fresh", "fallback", "context"])

    def test_research_selection_rejects_stale_items_and_caps_each_source(self):
        ranked = [
            {"title": "a1", "sourceName": "A", "ageHours": 1},
            {"title": "a2", "sourceName": "A", "ageHours": 2},
            {"title": "a3", "sourceName": "A", "ageHours": 3},
            {"title": "old", "sourceName": "B", "ageHours": 169},
            {"title": "b1", "sourceName": "B", "ageHours": 4},
            {"title": "undated", "sourceName": "C", "ageHours": None},
        ]
        selected = select_research_items(ranked, max_age_hours=168, per_source=2, maximum=40)
        self.assertEqual([item["title"] for item in selected], ["a1", "a2", "b1"])

    def test_ai_relevance_requires_explicit_signal_not_feed_identity(self):
        keywords = ["ai", "artificial intelligence", "gpt", "model"]
        self.assertTrue(is_ai_relevant({"title": "New GPT model ships", "summary": ""}, keywords))
        self.assertTrue(is_ai_relevant({"title": "School standard", "summary": "Safe participation with AI"}, keywords))
        self.assertFalse(is_ai_relevant({"title": "Football features in Search", "summary": "Live scores"}, keywords))
        self.assertFalse(is_ai_relevant({"title": "Payment processor settlement", "summary": "Fraud allegations"}, keywords))

    def test_production_proposal_binds_schema_sources_and_mode(self):
        repo = Path("/home/ubuntu/AI-HAYOM")
        edition = json.loads((repo / "edition/001/edition.json").read_text())
        urls = [source["url"] for story in edition["stories"] for source in story["sources"]]
        raw = {"edition": edition, "cartoonConcepts": ["one", "two", "three"],
               "recommendedCartoon": "2", "uncertainties": "none"}
        bundle = {"items": [{"url": url} for url in urls]}
        proposal = proposal_from_inference(raw, bundle, "002", "2026-09-14", repo)
        self.assertEqual(proposal["editionPayload"]["number"], "002")
        self.assertEqual(proposal["editionPayload"]["cartoon"]["mobile"], "cartoon-mobile.webp")
        self.assertIn(proposal["cartoonMode"], CARTOON_MODES)

    def test_production_images_are_not_fixture_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); env = root / ".env"; env.write_text("OPENAI_API_KEY=x\n"); os.chmod(env, 0o600)
            calls = []
            def requester(_key, prompt, size): calls.append((prompt, size)); return b"raw", {}
            def validator(_raw, path, _width, _height): path.write_bytes(b"x" * 10000)
            result = generate_images(env, root / "proof", "A visual metaphor", CARTOON_MODES[0], requester=requester, validator=validator)
            self.assertEqual(len(calls), 2)
            self.assertFalse(result["fixtureOnly"])
            self.assertTrue(all(not item["nonPublishable"] for item in result["images"]))

    def test_telegram_disabled_never_opens_network(self):
        config = Config({"telegram": {"enabled": False}}, Path("/tmp"))
        self.assertFalse(telegram_send(config, "hello", opener=lambda *_args, **_kwargs: self.fail("network called")))

    def test_hold_and_disabled_publication_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state_path = root / "state.json"; config_path = root / "config.json"
            config_path.write_text(json.dumps({"paths":{"websiteRepo":str(root / "repo")}, "publication":{"enabled":False}}))
            proposal = {"x": 1}
            state = {"allowedChatId":"7", "proposal":proposal, "state":"PLAN_PENDING", "contentHash":content_hash(proposal), "consumed":False, "fixture":False}
            atomic_write_json(state_path, state)
            held = process_approval({"statePath":str(state_path), "configPath":str(config_path), "command":"HOLD", "sender":"7", "proposal":proposal})
            self.assertEqual(held["state"], "HELD")
            proof = {"edition":{}, "images":[]}
            atomic_write_json(state_path, {**state, "state":"PROOF_PENDING", "finalProof":proof, "contentHash":content_hash(proof)})
            with self.assertRaisesRegex(ApprovalError, "publication is not enabled"):
                process_approval({"statePath":str(state_path), "configPath":str(config_path), "command":"APPROVE PUBLISH", "sender":"7", "proposal":proof})
            self.assertEqual(json.loads(state_path.read_text())["state"], "PROOF_PENDING")

    def test_fixture_plan_binds_owner_chat_without_exposing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); env = root / ".env"; env.write_text("TELEGRAM_CHAT_ID=777\nSECRET=nope\n"); os.chmod(env, 0o600)
            config = Config({"paths":{"state":"state/state.json", "drafts":"output/drafts"}}, root)
            dry_run(config)
            state = json.loads((root / "state/state.json").read_text())
            self.assertEqual(state["allowedChatId"], "777")
            self.assertNotIn("SECRET", json.dumps(state))

    def test_daily_creates_valid_production_plan_without_sending_when_disabled(self):
        source = Path("/home/ubuntu/AI-HAYOM")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"
            shutil.copytree(source, repo)
            expected_edition = next_edition_number(repo)
            edition = json.loads((repo / "edition/001/edition.json").read_text())
            urls = [item["url"] for story in edition["stories"] for item in story["sources"]]
            bundle = {"retrievedAt":"2026-09-14T06:00:00Z", "feeds":[], "healthyFeeds":2,
                      "failedFeeds":1, "items":[{"url":url} for url in urls], "itemCount":len(urls)}
            raw = {"edition":edition, "cartoonConcepts":["one", "two", "three"],
                   "recommendedCartoon":"1", "uncertainties":"none"}
            config = Config({"timezone":"Asia/Jerusalem", "paths":{"state":"state/state.json", "lock":"state/run.lock",
                            "research":"output/research", "drafts":"output/drafts", "alerts":"output/alerts",
                            "websiteRepo":str(repo)}, "inference":{"enabled":True, "fixtureOnlyUntilApproved":False,
                            "command":["fake"]}, "telegram":{"enabled":False}}, root)
            runner = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, json.dumps(raw), "")
            with patch("ai_hayom.collect_feeds", return_value=bundle), patch("ai_hayom.auth_confirmed", return_value=True):
                daily(config, inference_runner=runner, telegram_sender=lambda *_: False)
            state = json.loads((root / "state/state.json").read_text())
            self.assertEqual((state["state"], state["edition"], state["fixture"]), ("PLAN_PENDING", expected_edition, False))
            self.assertTrue((root / "output/drafts" / f"plan-{state['proposalId']}.txt").is_file())

    def test_production_approve_plan_builds_immutable_proof(self):
        source = Path("/home/ubuntu/AI-HAYOM")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; shutil.copytree(source, repo)
            output = root / "generated"; output.mkdir()
            next_number = next_edition_number(repo)
            edition = json.loads((repo / "edition/001/edition.json").read_text()); edition["number"] = next_number
            records = []
            for role, dimensions in (("desktop", (2172,724)), ("mobile", (1122,1402))):
                path = output / f"cartoon-{role}.webp"; path.write_bytes(b"x" * 10000)
                records.append({"role":role, "path":str(path), "width":dimensions[0], "height":dimensions[1],
                                "format":"webp", "fileSize":path.stat().st_size,
                                "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                                "fixtureOnly":False, "nonPublishable":False})
            proposal = {"proposalId":"p", "edition":next_number, "date":"2026-09-14",
                        "stories":[], "readingTime":"5 דקות", "cartoonConcepts":["one","two","three"],
                        "recommendedCartoon":"1", "uncertainties":"", "cartoonMode":CARTOON_MODES[0],
                        "editionPayload":edition}
            state_path = root / "state.json"; config_path = root / "config.json"
            atomic_write_json(state_path, {"allowedChatId":"7", "proposalId":"p", "edition":next_number, "fixture":False,
                              "proposal":proposal, "state":"PLAN_PENDING", "contentHash":content_hash(proposal), "consumed":False})
            config_path.write_text(json.dumps({"paths":{"websiteRepo":str(repo)}, "publication":{"enabled":False}}))
            fake_result = {"images":records, "cartoonMode":CARTOON_MODES[0]}
            with patch("approval_worker.generate_images", return_value=fake_result) as generator:
                result = process_approval({"statePath":str(state_path), "configPath":str(config_path),
                                           "command":"APPROVE PLAN", "sender":"7", "proposal":proposal})
            saved = json.loads(state_path.read_text())
            self.assertEqual(result["state"], "PROOF_PENDING")
            self.assertEqual(saved["contentHash"], content_hash(saved["finalProof"]))
            self.assertEqual(set(saved["finalProof"]["imageHashes"]), {"desktop", "mobile"})
            self.assertTrue(generator.call_args.kwargs["reuse_existing"])
            self.assertEqual(generator.call_args.args[1].name, "p-c1-r0")
            self.assertEqual(saved["finalProof"]["cartoonChoice"], 1)
            with patch("approval_worker.generate_images", return_value=fake_result) as regeneration:
                result = process_approval({"statePath":str(state_path), "configPath":str(config_path),
                                           "command":"REGENERATE CARTOON 3", "sender":"7",
                                           "proposal":saved["finalProof"]})
            regenerated = json.loads(state_path.read_text())
            self.assertEqual(result["state"], "PROOF_PENDING")
            self.assertEqual(regenerated["finalProof"]["cartoonChoice"], 3)
            self.assertEqual(regenerated["finalProof"]["cartoonConcept"], "three")
            self.assertEqual(regeneration.call_args.args[1].name, "p-c3-r1")
            self.assertTrue(regeneration.call_args.kwargs["reuse_existing"])

    def test_cartoon_mode_selection_is_deterministic_and_avoids_prior(self):
        first = select_cartoon_mode("002", "2026-09-13", "proposal-1")
        self.assertEqual(first, select_cartoon_mode("002", "2026-09-13", "proposal-1"))
        self.assertNotEqual(first["id"], select_cartoon_mode("002", "2026-09-13", "proposal-1", first["id"])["id"])
        self.assertEqual(len(CARTOON_MODES), 12)

    def test_proposal_and_plan_bind_and_show_full_cartoon_mode(self):
        proposal = create_fixture_proposal()
        self.assertEqual(content_hash(proposal), content_hash(dict(proposal)))
        changed = dict(proposal); changed["cartoonMode"] = {**proposal["cartoonMode"], "traits": "changed"}
        self.assertNotEqual(content_hash(proposal), content_hash(changed))
        message = build_plan_message(proposal)
        self.assertIn(proposal["cartoonMode"]["id"], message)
        self.assertIn(proposal["cartoonMode"]["traits"], message)
        measured = dict(proposal)
        measured["stories"] = [{**proposal["stories"][0], "sourceName": "Primary feed",
                                "ageHours": 3.4, "independentMentions": 2, "indirectLink": True}]
        measured_message = build_plan_message(measured)
        self.assertIn("Freshness: 3h old", measured_message)
        self.assertIn("Independent publishers: 2", measured_message)
        self.assertIn("Source: Primary feed", measured_message)
        self.assertIn("indirect discovery link", measured_message)

    def test_daily_writes_full_atomic_aggregate_beside_shortlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config({
                "paths": {"state":"state/state.json", "research":"output/research",
                          "drafts":"output/drafts", "alerts":"output/alerts"},
                "inference": {"enabled":False, "fixtureOnlyUntilApproved":True},
            }, root)
            aggregate_item = {"title":"fresh AI story", "url":"https://example.com/1",
                              "ageHours":1, "freshnessWindow":"fresh"}
            bundle = {"retrievedAt":"2026-09-14T06:00:00Z", "feeds":[], "healthyFeeds":2,
                      "failedFeeds":0, "items":[aggregate_item], "itemCount":1,
                      "eligibleItemCount":1, "_aggregateItems":[aggregate_item],
                      "selectionPolicy":{"freshWindowHours":24}}
            with patch("ai_hayom.collect_feeds", return_value=bundle):
                daily(config)
            aggregate = json.loads((root / "output/research/aggregate-latest.json").read_text())
            self.assertEqual(aggregate["itemCount"], 1)
            self.assertEqual(aggregate["items"][0]["url"], "https://example.com/1")

    def test_fixture_prompt_contains_approved_mode_traits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / "fixtures"; env = root / ".env"
            env.write_text("OPENAI_API_KEY=fixture\n"); os.chmod(env, 0o600)
            prompts = []
            def requester(key, prompt, size): prompts.append(prompt); return b"raw", {}
            def validator(raw, path, width, height): path.write_bytes(b"x" * 10000)
            generate_fixture(env, output, mode={"id": "bold-metaphor", "traits": "one powerful metaphor with minimal elements"}, requester=requester, validator=validator)
            self.assertEqual(len(prompts), 2)
            self.assertTrue(all("bold-metaphor" in p and "one powerful metaphor with minimal elements" in p for p in prompts))

    def test_desktop_and_mobile_prompts_require_independent_complete_compositions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / "fixtures"; env = root / ".env"
            env.write_text("OPENAI_API_KEY=fixture\n"); os.chmod(env, 0o600)
            prompts = []
            def requester(key, prompt, size): prompts.append((prompt, size)); return b"raw", {}
            def validator(raw, path, width, height): path.write_bytes(b"x" * 10000)
            generate_fixture(env, output, requester=requester, validator=validator)
            desktop, mobile = prompts
            self.assertEqual((desktop[1], mobile[1]), ("1536x1024", "1024x1536"))
            self.assertIn("native panoramic 3:1 newspaper strip", desktop[0])
            self.assertIn("middle 42 percent horizontal safe band", desktop[0])
            self.assertIn("Nothing important may touch or cross any edge", desktop[0])
            self.assertIn("native tall newspaper composition", mobile[0])
            self.assertIn("purpose-built for this canvas", desktop[0])
            self.assertIn("purpose-built for this canvas", mobile[0])

    def test_deduplicate_items_preserves_first_url(self):
        items = [
            {"url": "https://example.com/a#x", "title": "first"},
            {"url": "https://example.com/a", "title": "duplicate"},
            {"url": "https://example.com/b", "title": "second"},
        ]
        result = deduplicate_items(items)
        self.assertEqual([item["title"] for item in result], ["first", "second"])

    def test_validator_accepts_contract_and_requires_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "edition/002").mkdir(parents=True)
            (root / "edition/002/desktop.webp").write_bytes(b"RIFF")
            (root / "edition/002/mobile.webp").write_bytes(b"RIFF")
            edition = {
                "schemaVersion": 1,
                "number": "002",
                "publicationDate": "2026-09-13",
                "status": "draft",
                "headline": "A headline",
                "coverDescription": "A cover",
                "introduction": "Intro",
                "editorialNote": "Note",
                "aiDisclosure": "AI-assisted draft; human review required.",
                "keywords": ["one", "two", "three", "four"],
                "cartoon": {"desktop": "desktop.webp", "mobile": "mobile.webp", "alt": "Alt text"},
                "stories": [
                    {
                        "section": "World",
                        "headline": "Story headline",
                        "readingTime": "3 min",
                        "quickRead": "Quick",
                        "summary": "Summary",
                        "whyItMatters": "Why",
                        "sources": [{"url": "https://example.com/source"}],
                    }
                ] * 4,
                "totalReadingTime": "12 min",
                "takeaway": "Takeaway",
            }
            self.assertEqual(validate_edition(edition, root), [])
            (root / "edition/002/mobile.webp").unlink()
            self.assertTrue(any("mobile" in error for error in validate_edition(edition, root)))

    def test_approval_message_has_exact_human_commands(self):
        message = build_approval_message([
            {"url": "https://example.com/a", "title": "Urgent", "score": 3}
        ], threshold=2)
        self.assertIn("APPROVE", message)
        self.assertIn("REPLACE 03", message)
        self.assertIn("ADD TO NEXT EDITION", message)
        self.assertIn("IGNORE", message)

    def test_atomic_write_and_lock_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text())["ok"], True)
            lock_path = Path(tmp) / "run.lock"
            with LockBusy.hold(lock_path):
                with self.assertRaises(LockBusy):
                    with LockBusy.hold(lock_path):
                        pass

    def test_default_config_is_fail_closed(self):
        config = Config.load(Path(__file__).with_name("config.sample.json"))
        self.assertFalse(config.inference_enabled)
        self.assertEqual(len(config.feeds), 26)
        self.assertTrue(all(feed["url"].startswith("https://") for feed in config.feeds))
        self.assertFalse(config.telegram_enabled)

    def test_fingerprint_deduplicates_updated_syndicated_stories(self):
        a = {"title": "Open model released", "summary": "A long factual summary", "url": "https://a.example/x"}
        b = {"title": "Open model released!", "summary": "A long factual summary updated", "url": "https://b.example/y"}
        self.assertEqual(breaking_fingerprint(a), breaking_fingerprint(b))

    def test_approval_requires_sender_state_and_hash_and_is_single_use(self):
        state = {"proposalId": "p1", "state": "PLAN_PENDING", "allowedChatId": "123", "contentHash": content_hash({"x": 1}), "consumed": False}
        with self.assertRaises(ApprovalError):
            handle_approval(state, "APPROVE PLAN", "wrong", {"x": 1})
        approved = handle_approval(state, "APPROVE PLAN", "123", {"x": 1})
        self.assertEqual(approved["state"], "PLAN_APPROVED")
        with self.assertRaises(ApprovalError):
            handle_approval(approved, "APPROVE PLAN", "123", {"x": 1})

    def test_unapproved_publish_is_rejected(self):
        state = {"proposalId": "p1", "state": "PROOF_PENDING", "allowedChatId": "123", "contentHash": content_hash({"x": 1}), "consumed": False}
        with self.assertRaises(ApprovalError):
            handle_approval(state, "PUBLISH", "123", {"x": 1})

    def test_repository_contract_and_collision_guard(self):
        repo = Path("/home/ubuntu/AI-HAYOM")
        self.assertTrue((repo / "edition/catalog.json").is_file())
        self.assertTrue((repo / "edition/001/edition.json").is_file())
        catalog = json.loads((repo / "edition/catalog.json").read_text())
        expected = f"{max(int(number) for number in catalog['editions']) + 1:03d}"
        self.assertEqual(next_edition_number(repo), expected)
        edition = json.loads((repo / "edition/001/edition.json").read_text())
        self.assertEqual(validate_edition(edition, repo), [])
        state = {"state": "PUBLISH_APPROVED", "consumed": True}
        with self.assertRaises(RuntimeError):
            prepare_publication(edition, state, repo)

    def test_historical_ids_are_ordered_before_001_without_changing_next_number(self):
        self.assertEqual(historical_edition_ids(5), ["-005", "-004", "-003", "-002", "-001"])
        self.assertEqual(next_edition_number(Path("/home/ubuntu/AI-HAYOM")), "003")

    def test_historical_bundle_uses_only_the_target_local_calendar_day(self):
        aggregate = {"retrievedAt":"2026-09-14T08:00:00Z", "items":[
            {"title":"AI model launch alpha", "summary":"artificial intelligence model", "url":"https://a.example/1", "sourceName":"A", "publisher":"A", "sourceTier":"primary", "publishedAt":"Tue, 08 Sep 2026 05:00:00 GMT"},
            {"title":"AI model launch beta", "summary":"artificial intelligence model", "url":"https://b.example/2", "sourceName":"B", "publisher":"B", "sourceTier":"secondary", "publishedAt":"Tue, 08 Sep 2026 12:00:00 GMT"},
            {"title":"AI safety regulation gamma", "summary":"artificial intelligence safety", "url":"https://c.example/3", "sourceName":"C", "publisher":"C", "sourceTier":"regulator", "publishedAt":"Tue, 08 Sep 2026 16:00:00 GMT"},
            {"title":"AI robotics research delta", "summary":"artificial intelligence robotics", "url":"https://d.example/4", "sourceName":"D", "publisher":"D", "sourceTier":"primary", "publishedAt":"Tue, 08 Sep 2026 18:00:00 GMT"},
            {"title":"Future AI item", "summary":"artificial intelligence", "url":"https://e.example/5", "sourceName":"E", "publisher":"E", "sourceTier":"primary", "publishedAt":"Wed, 09 Sep 2026 06:00:00 GMT"},
        ]}
        config = Config({"timezone":"Asia/Jerusalem", "editorial":{"minStories":4,
                        "maxResearchItemsPerSource":2, "priorityKeywords":["model","safety"]}}, Path("/tmp"))
        bundle = historical_research_bundle(aggregate, "2026-09-08", config)
        self.assertEqual(bundle["itemCount"], 4)
        self.assertTrue(all(item["freshnessWindow"] == "fresh" for item in bundle["items"]))
        self.assertNotIn("https://e.example/5", {item["url"] for item in bundle["items"]})

    def test_website_loader_uses_self_contained_edition_routes(self):
        app = Path("/home/ubuntu/AI-HAYOM/app.js").read_text(encoding="utf-8")
        page = Path("/home/ubuntu/AI-HAYOM/index.html").read_text(encoding="utf-8")
        self.assertIn("/edition/catalog.json", app)
        self.assertIn("/edition/${number}/edition.json", app)
        self.assertIn("/^\\/(-?\\d{3})\\/?$/", app)
        self.assertIn("/edition/${edition.number}/${edition.cartoon.desktop}", app)
        self.assertNotIn("/editions/catalog.json", app)
        self.assertIn("4: 'ארבעת'", app)
        self.assertIn("5: 'חמשת'", app)
        self.assertIn("6: 'ששת'", app)
        self.assertIn("const storyCountLabel = storyCountLabels[edition.stories.length] || 'מספר';", app)
        self.assertIn("querySelectorAll('[data-previous]')", app)
        self.assertIn("installKeyboard(previous, next)", app)
        self.assertIn('class="edition-label"', page)
        self.assertIn('.edition-arrow{position:absolute', page)
        self.assertNotIn('class="edition-picker"', page)
        self.assertIn('class="edition-nav"', page)

    def test_isolated_publisher_target_uses_edition_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp); repo.mkdir(exist_ok=True); (repo / "edition").mkdir()
            (repo / "edition/catalog.json").write_text('{"latest":"001","editions":["001"]}\n')
            source = Path("/home/ubuntu/AI-HAYOM")
            edition = json.loads((source / "edition/001/edition.json").read_text())
            edition["number"] = "002"
            edition["cartoon"] = {"desktop":"desktop.webp","mobile":"mobile.webp","alt":edition["cartoon"]["alt"]}
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            state = {"state":"PUBLISH_APPROVED","consumed":True}
            prepared = prepare_publication(edition, state, repo)
            self.assertEqual(prepared["target"], str(repo / "edition/002/edition.json"))
            self.assertEqual(prepared["catalog"], str(repo / "edition/catalog.json"))

    def test_simulated_publication_failure_is_fail_closed(self):
        with self.assertRaises(ApprovalError):
            prepare_publication({"number": "002"}, {"state": "PROOF_PENDING", "consumed": False}, Path("/tmp/missing"))

    def test_malformed_inference_output_is_rejected(self):
        with self.assertRaises(RuntimeError):
            parse_inference_output("not json")

    def test_single_markdown_json_fence_is_accepted_without_accepting_prose(self):
        self.assertEqual(parse_inference_output('```json\n{"edition": {"number": "002"}}\n```'),
                         {"edition": {"number": "002"}})
        with self.assertRaises(RuntimeError):
            parse_inference_output('Here is the result:\n```json\n{"edition": {}}\n```')

    def test_malformed_json_gets_exactly_one_bounded_format_repair(self):
        calls = []
        def runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, '{"edition":{"number":"-001"}}', "")
        parsed, repaired = parse_with_one_format_repair('{"edition":', ["fake"], runner)
        self.assertEqual(parsed["edition"]["number"], "-001")
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(repaired)

    def test_edition_prompt_uses_exact_live_schema_without_response_wrapper(self):
        prompt = edition_prompt({"items": [{"url":"https://example.com/long-source"}]}, "002", "2026-09-14")
        self.assertIn('"quickRead"', prompt)
        self.assertIn('"whyItMatters"', prompt)
        self.assertIn('"sources"', prompt)
        self.assertIn('"cartoonConcepts"', prompt)
        self.assertIn("exactly the four top-level keys", prompt)
        self.assertIn("no more than two stories from the same source or company", prompt)
        self.assertIn('"totalReadingTime": "05:00"', prompt)
        self.assertIn('"sourceId": "S01"', prompt)
        self.assertIn("never copy, shorten, or output source URLs", prompt)
        self.assertNotIn('"response":', prompt)

    def test_source_ids_resolve_to_exact_allowlisted_urls(self):
        bundle = {"items":[{"url":"https://example.com/a/very/long/path"}]}
        raw = {"edition":{"stories":[{"sources":[{"sourceId":"S01"}]}]}}
        resolved = resolve_source_references(raw, bundle)
        self.assertEqual(resolved["edition"]["stories"][0]["sources"],
                         [{"url":"https://example.com/a/very/long/path"}])
        with self.assertRaisesRegex(RuntimeError, "unknown source ID"):
            resolve_source_references({"edition":{"stories":[{"sources":[{"sourceId":"S99"}]}]}}, bundle)

    def test_reading_time_is_canonicalized_before_approval(self):
        self.assertEqual(canonical_reading_time("05:00, approximately 05:00 total"), "05:00")
        self.assertEqual(canonical_reading_time("5:00"), "05:00")
        with self.assertRaises(RuntimeError):
            canonical_reading_time("about five minutes")

    def test_ocr_synthetic_news_is_rejected(self):
        tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t96.2\tNEWS\n"
        self.assertEqual(validate_ocr_output(tsv), [{"token": "NEWS", "confidence": 96.2}])

    def test_ocr_high_confidence_hebrew_digits_and_signature_are_rejected(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        rows = "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t91.0\tחדשות\n5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t88.0\t2026\n5\t1\t1\t1\t1\t3\t0\t0\t10\t10\t94.0\tAlex\n"
        self.assertEqual(validate_ocr_output(header + rows), [{"token": "חדשות", "confidence": 91.0}, {"token": "2026", "confidence": 88.0}, {"token": "Alex", "confidence": 94.0}])

    def test_ocr_diagnostics_are_sanitized_and_bounded(self):
        tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t99.9\t<logo/>SIGNED!!!\n"
        result = validate_ocr_output(tsv)
        self.assertEqual(result, [{"token": "logoSIGNED", "confidence": 99.9}])

    def test_ocr_blank_editorial_line_art_passes(self):
        tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t-1\t\n"
        self.assertEqual(validate_ocr_output(tsv), [])

    def test_ocr_low_confidence_noise_does_not_trigger(self):
        tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t12.4\tif\n"
        self.assertEqual(validate_ocr_output(tsv), [])
        self.assertEqual(OCR_CONFIDENCE_THRESHOLD, 70.0)

    def test_ocr_two_isolated_standalone_digits_pass(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        t = h + "5\t1\t1\t1\t1\t1\t10\t10\t10\t10\t78.5\t2\n5\t1\t1\t1\t1\t2\t500\t400\t10\t10\t95.4\t2\n"
        self.assertEqual(ocr_evidence(t, h), [])

    def test_ocr_strong_word_and_multidigit_tokens_fail(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        self.assertEqual(ocr_evidence(h + "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t90\t2026\n", h)[0]["evidence"], "strong-token")
        self.assertEqual(ocr_evidence(h + "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t90\tחדשות\n", h)[0]["evidence"], "strong-token")

    def test_ocr_one_pass_two_character_60_is_weak(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        self.assertEqual(ocr_evidence(h + "5\t1\t1\t1\t1\t1\t100\t100\t20\t10\t77.5\t60\n", h), [])

    def test_ocr_spatially_matched_two_character_60_fails(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        row = "5\t1\t1\t1\t1\t1\t100\t100\t20\t10\t77.5\t60\n"
        self.assertEqual(ocr_evidence(h + row, h + row), [])

    def test_ocr_same_short_token_at_unrelated_positions_does_not_correlate(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        first = "5\t1\t1\t1\t1\t1\t100\t100\t20\t10\t77.5\t60\n"
        second = "5\t1\t1\t1\t1\t1\t800\t700\t20\t10\t95.0\t60\n"
        self.assertEqual(ocr_evidence(h + first, h + second), [])

    def test_ocr_overlapping_duplicate_boxes_do_not_create_cluster(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        first = "5\t1\t1\t1\t1\t1\t100\t100\t20\t10\t90\tA\n5\t1\t1\t1\t1\t2\t130\t100\t20\t10\t90\tB\n"
        second = "5\t1\t1\t1\t1\t1\t100\t100\t20\t10\t95\tC\n5\t1\t1\t1\t1\t2\t130\t100\t20\t10\t95\tD\n"
        self.assertEqual(ocr_evidence(h + first, h + second), [])

    def test_ocr_single_character_cross_pass_and_cluster_fail(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        one = "5\t1\t1\t1\t1\t1\t20\t20\t10\t10\t90\tA\n"
        self.assertEqual(ocr_evidence(h + one, h + one)[0]["evidence"], "cross-pass")
        clustered = "".join(f"5\t1\t1\t1\t1\t{i}\t{i*30}\t20\t10\t10\t90\t{i}\n" for i in range(1, 4))
        self.assertEqual(ocr_evidence(h + clustered, h)[0]["evidence"], "clustered-single-character")

    def test_ocr_low_confidence_mixed_fragments_do_not_form_text_cluster(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        fragments = (
            "5\t1\t1\t1\t1\t1\t20\t20\t10\t10\t77.8\t5\n"
            "5\t1\t1\t1\t1\t2\t50\t20\t15\t10\t70.2\tלק\n"
            "5\t1\t1\t1\t1\t3\t80\t20\t15\t10\t70.1\tSe\n"
        )
        self.assertEqual(OCR_CLUSTER_CONFIDENCE_THRESHOLD, 85.0)
        self.assertEqual(ocr_evidence(h + fragments, h), [])

    def test_ocr_weak_three_character_fragment_requires_stronger_evidence(self):
        h = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        weak = "5\t1\t1\t1\t1\t1\t20\t20\t20\t10\t78.1\tWhe\n"
        strong = "5\t1\t1\t1\t1\t1\t20\t20\t20\t10\t90.0\tWhe\n"
        self.assertEqual(OCR_SHORT_WORD_CONFIDENCE_THRESHOLD, 85.0)
        self.assertEqual(ocr_evidence(h + weak, h), [])
        self.assertEqual(ocr_evidence(h + strong, h)[0]["evidence"], "strong-token")

    def test_ocr_either_layout_pass_failure_fails_closed(self):
        class Result:
            returncode = 1
            stdout = ""
        calls = []
        def one_pass(command, **kwargs):
            calls.append(command)
            return Result()
        with self.assertRaises(RuntimeError): scan_image_for_text(Path("/tmp/no-image"), runner=one_pass)
        self.assertEqual(len(calls), 1)

    def test_ocr_runner_file_not_found_is_sanitized(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("tesseract-secret-path")
        with self.assertRaisesRegex(RuntimeError, r"^stage=ocr; OCR pass PSM 11 unavailable$"):
            scan_image_for_text(Path("/tmp/no-image"), runner=missing)

    def test_fixture_default_cleanup_deletes_output_on_runner_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); env = root / ".env"; env.write_text("OPENAI_API_KEY=fixture\n"); os.chmod(env, 0o600)
            calls = []
            def requester(*args): calls.append(args); return b"raw", {}
            def failing_validator(raw, output, *args): output.write_bytes(b"converted"); raise FileNotFoundError("ffprobe-secret-path")
            with self.assertRaises(FileNotFoundError): generate_fixture(env, root / "fixtures", requester=requester, validator=failing_validator)
            self.assertEqual(len(calls), 1); self.assertFalse(any((root / "fixtures").glob("*")))

    def test_fixture_review_quarantines_runner_oserror_without_state_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); env = root / ".env"; env.write_text("OPENAI_API_KEY=fixture\n"); os.chmod(env, 0o600)
            state = root / "state.json"; state.write_text('{"state":"PLAN_PENDING"}\n')
            def requester(*args): return b"raw", {}
            def failing_validator(raw, output, *args): output.write_bytes(b"converted"); raise FileNotFoundError("ffprobe-secret-path")
            with self.assertRaises(FileNotFoundError): generate_fixture(env, root / "fixtures", review_rejected=True, requester=requester, validator=failing_validator)
            reviews = list((root / "rejected-review").glob("fixture-aihayom-desktop-*.webp"))
            self.assertEqual(len(reviews), 1); self.assertTrue(reviews[0].with_suffix(".json").is_file()); self.assertEqual(json.loads(state.read_text())["state"], "PLAN_PENDING")

    def test_rejected_fixture_quarantine_is_non_publishable_and_state_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / "fixture.webp"; output.write_bytes(b"fixture")
            state = root / "state.json"; state.write_text('{"state":"PLAN_PENDING"}\n')
            image_path, sidecar = quarantine_rejected(output, "stage=ocr; detected=[{\"token\":\"2\",\"confidence\":95.4}]", root / "rejected-review")
            self.assertTrue(image_path.is_file()); self.assertTrue(sidecar.is_file()); self.assertEqual(json.loads(state.read_text())["state"], "PLAN_PENDING")
            rejected = [{"role":"desktop","path":str(image_path)}, {"role":"mobile","path":str(image_path)}]
            self.assertTrue(any("non-publishable" in error for error in validate_final_proof_package({"number":"002"}, rejected, root)))

    def test_unavailable_ocr_fails_closed(self):
        class Result:
            returncode = 1
            stdout = ""
        def unavailable(*args, **kwargs):
            return Result()
        with self.assertRaises(RuntimeError):
            scan_image_for_text(Path("/tmp/line-art.webp"), runner=unavailable)

    def test_final_proof_requires_matching_asset_hashes(self):
        source = Path("/home/ubuntu/AI-HAYOM")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "edition/002").mkdir(parents=True)
            (root / "edition/002/desktop.webp").write_bytes((source / "edition/001/cartoon-desktop.webp").read_bytes())
            (root / "edition/002/mobile.webp").write_bytes((source / "edition/001/cartoon-mobile.webp").read_bytes())
            edition = json.loads((source / "edition/001/edition.json").read_text())
            edition["number"] = "002"
            edition["cartoon"] = {"desktop": "desktop.webp", "mobile": "mobile.webp", "alt": edition["cartoon"]["alt"]}
            records = []
            for role, name, dimensions in [("desktop", "desktop.webp", (2172, 724)), ("mobile", "mobile.webp", (1122, 1402))]:
                path = root / "edition/002" / name
                records.append({"role": role, "path": str(path), "width": dimensions[0], "height": dimensions[1], "format": "webp", "fileSize": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            self.assertEqual(validate_final_proof_package(edition, records, root), [])
            proof = build_final_proof(edition, records, root)
            self.assertEqual(proof["contentHash"], content_hash({k: v for k, v in proof.items() if k != "contentHash"}))
            records[0]["sha256"] = "stale"
            self.assertTrue(any("SHA-256" in e for e in validate_final_proof_package(edition, records, root)))

    def test_resumable_reuse_makes_no_requester_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / "fixtures"; output.mkdir()
            (output / "fixture-aihayom-desktop.webp").write_bytes(b"desktop")
            (output / "fixture-aihayom-mobile.webp").write_bytes(b"mobile")
            def existing(path, role): return {"role": role, "path": str(path), "sha256": "bound", "fileSize": path.stat().st_size}
            def requester(*args): raise AssertionError("requester must not run")
            result = generate_fixture(root / ".env", output, reuse_existing=True, requester=requester, existing_validator=existing)
            self.assertEqual([item["reused"] for item in result["images"]], [True, True])

    def test_resumable_missing_role_makes_exactly_one_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / "fixtures"; output.mkdir(); (output / "fixture-aihayom-desktop.webp").write_bytes(b"desktop")
            env = root / ".env"; env.write_text("OPENAI_API_KEY=fixture\n"); os.chmod(env, 0o600)
            calls = []
            def existing(path, role): return {"role": role, "path": str(path), "sha256": "bound", "fileSize": path.stat().st_size}
            def requester(key, prompt, size): calls.append(size); return b"raw", {}
            def validator(raw, output, width, height): output.write_bytes(b"x" * 10000)
            result = generate_fixture(env, output, reuse_existing=True, requester=requester, validator=validator, existing_validator=existing)
            self.assertEqual(calls, ["1024x1536"]); self.assertEqual([item["reused"] for item in result["images"]], [True, False])

    def test_malformed_existing_role_fails_before_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / "fixtures"; output.mkdir(); (output / "fixture-aihayom-desktop.webp").write_bytes(b"bad")
            def existing(*args): raise RuntimeError("stage=ocr; invalid existing asset")
            def requester(*args): raise AssertionError("requester must not run")
            with self.assertRaisesRegex(RuntimeError, "invalid existing"):
                generate_fixture(root / ".env", output, reuse_existing=True, requester=requester, existing_validator=existing)

    def test_promoted_desktop_uses_offline_validation_and_no_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / "rejected-review" / "review.webp"; source.parent.mkdir(); source.write_bytes(b"x" * 10000)
            calls = []
            header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            def runner(command, **kwargs):
                calls.append(command[0])
                if command[0] == "ffprobe": return subprocess.CompletedProcess(command, 0, '{"streams":[{"width":2172,"height":724,"codec_name":"webp"}]}', "")
                return subprocess.CompletedProcess(command, 0, header, "")
            result = promote_fixture_role(source, "desktop", root / "fixtures", runner=runner)
            self.assertTrue(result["fixtureOnly"] and result["nonPublishable"] and result["promotedFromReview"])
            self.assertEqual(calls.count("tesseract"), 4); self.assertFalse((root / "fixtures/fixture-aihayom-desktop.json").exists())

    def test_publication_push_failure_and_stale_remote_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir(); (repo / "edition/001").mkdir(parents=True)
            (repo / "edition/catalog.json").write_text('{"latest":"001","editions":["001"]}\n')
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True); subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            edition = {"number":"002"}; records = []; state = {"state":"PUBLISH_APPROVED","consumed":True,"approvedProofHash":final_proof_hash(edition, records)}
            with self.assertRaises(RuntimeError): publish_local_commit(edition, records, state, repo)

    def _external_publication_fixture(self):
        source = Path("/home/ubuntu/AI-HAYOM")
        repo = Path(tempfile.mkdtemp()) / "repo"
        shutil.copytree(source, repo)
        edition = json.loads((repo / "edition/001/edition.json").read_text())
        edition["number"] = next_edition_number(repo)
        external = repo.parent / "approved-assets"; external.mkdir()
        records = []
        for role, name, dims in (("desktop", "cartoon-desktop.webp", (2172, 724)), ("mobile", "cartoon-mobile.webp", (1122, 1402))):
            path = external / name
            shutil.copy2(repo / "edition/001" / name, path)
            records.append({"role": role, "path": str(path), "width": dims[0], "height": dims[1], "format": "webp", "fileSize": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        state = {"state": "PUBLISH_APPROVED", "consumed": True, "approvedProofHash": final_proof_hash(edition, records)}
        return repo, edition, records, state

    def test_publisher_copies_external_approved_proof_assets_and_commits(self):
        repo, edition, records, state = self._external_publication_fixture()
        real_run = subprocess.run
        def fake_run(command, *args, **kwargs):
            if command[0:4] == ["git", "-C", str(repo), "ls-remote"]:
                head = real_run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
                return subprocess.CompletedProcess(command, 0, head + " refs/heads/main\n", "")
            return real_run(command, *args, **kwargs)
        with patch("ai_hayom.subprocess.run", side_effect=fake_run):
            result = publish_local_commit(edition, records, state, repo)
        edition_dir = repo / "edition" / edition["number"]
        self.assertEqual(result["target"], str(edition_dir / "edition.json"))
        self.assertEqual((edition_dir / "cartoon-desktop.webp").read_bytes(), Path(records[0]["path"]).read_bytes())
        self.assertEqual((edition_dir / "cartoon-mobile.webp").read_bytes(), Path(records[1]["path"]).read_bytes())
        self.assertTrue(real_run(["git", "-C", str(repo), "diff", "--cached", "--exit-code"], capture_output=True).returncode == 0)

    def test_publisher_changed_external_source_fails_closed(self):
        repo, edition, records, state = self._external_publication_fixture()
        Path(records[0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            publish_local_commit(edition, records, state, repo)
        self.assertFalse((repo / "edition" / edition["number"]).exists())

    def test_publisher_image_hash_mismatch_fails_closed(self):
        repo, edition, records, state = self._external_publication_fixture()
        records[1]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ApprovalError, "approved final proof hash changed"):
            publish_local_commit(edition, records, state, repo)
        self.assertFalse((repo / "edition" / edition["number"]).exists())

    def test_publisher_commit_failure_restores_catalog_and_removes_new_directory(self):
        repo, edition, records, state = self._external_publication_fixture()
        catalog = repo / "edition/catalog.json"; original = catalog.read_bytes()
        real_run = subprocess.run
        def failing_commit(command, *args, **kwargs):
            if command[0:4] == ["git", "-C", str(repo), "ls-remote"]:
                head = real_run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
                return subprocess.CompletedProcess(command, 0, head + " refs/heads/main\n", "")
            if command[0:4] == ["git", "-C", str(repo), "commit"]:
                return subprocess.CompletedProcess(command, 1, "", "simulated commit failure")
            return real_run(command, *args, **kwargs)
        with patch("ai_hayom.subprocess.run", side_effect=failing_commit):
            with self.assertRaisesRegex(RuntimeError, "git commit failed"):
                publish_local_commit(edition, records, state, repo)
        self.assertFalse((repo / "edition" / edition["number"]).exists())
        self.assertEqual(catalog.read_bytes(), original)
        staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(staged, "")

    def test_stale_remote_main_is_rejected_before_write(self):
        source = Path("/home/ubuntu/AI-HAYOM")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(source, repo)
            edition = json.loads((repo / "edition/001/edition.json").read_text()); edition["number"] = next_edition_number(repo)
            records = [{"role": role, "path": str(repo / "edition/001" / f"cartoon-{role}.webp"), "width": dims[0], "height": dims[1], "format": "webp", "fileSize": (repo / "edition/001" / f"cartoon-{role}.webp").stat().st_size, "sha256": hashlib.sha256((repo / "edition/001" / f"cartoon-{role}.webp").read_bytes()).hexdigest()} for role, dims in (("desktop", (2172,724)), ("mobile", (1122,1402)))]
            state = {"state":"PUBLISH_APPROVED","consumed":True,"approvedProofHash":final_proof_hash(edition, records)}
            real_run = subprocess.run
            def fake_run(command, *args, **kwargs):
                if command[0:4] == ["git", "-C", str(repo), "ls-remote"]:
                    return subprocess.CompletedProcess(command, 0, "remote-sha refs/heads/main\n", "")
                if command[0:4] == ["git", "-C", str(repo), "rev-parse"]:
                    return subprocess.CompletedProcess(command, 0, "local-sha\n", "")
                return real_run(command, *args, **kwargs)
            with patch("ai_hayom.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "stale remote"):
                    publish_local_commit(edition, records, state, repo)
                self.assertFalse((repo / "edition" / edition["number"]).exists())
                self.assertEqual((repo / "edition/catalog.json").read_bytes(), (source / "edition/catalog.json").read_bytes())
                staged = real_run(["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(staged, "")

    def test_normal_inference_mapping_requires_real_schema(self):
        source = Path("/home/ubuntu/AI-HAYOM")
        raw = json.loads((source / "edition/001/edition.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "edition/002").mkdir(parents=True)
            shutil.copy2(source / "edition/001/cartoon-desktop.webp", root / "edition/002/cartoon-desktop.webp")
            shutil.copy2(source / "edition/001/cartoon-mobile.webp", root / "edition/002/cartoon-mobile.webp")
            mapped = map_inference_to_edition(raw, "002", "2026-09-14", root)
            self.assertEqual(mapped["number"], "002")
            self.assertEqual(mapped["publicationDate"], "2026-09-14")
            with self.assertRaises(RuntimeError): map_inference_to_edition({"headline":"bad"}, "002", "2026-09-14", root)

    def test_duplicate_edition_is_rejected(self):
        repo = Path("/home/ubuntu/AI-HAYOM")
        edition = json.loads((repo / "edition/001/edition.json").read_text())
        state = {"state":"PUBLISH_APPROVED","consumed":True,"approvedEditionHash":content_hash(edition)}
        with self.assertRaises(RuntimeError): prepare_publication(edition, state, repo)

    def test_fake_production_exact_content_and_wrong_content_fail(self):
        edition = json.loads(Path("/home/ubuntu/AI-HAYOM/edition/001/edition.json").read_text())
        root = Path("/home/ubuntu/AI-HAYOM")
        responses = {"/edition/001/edition.json": json.dumps(edition).encode(), "/001": b"<html><script>app</script></html>", "/edition/001/cartoon-desktop.webp": (root / "edition/001/cartoon-desktop.webp").read_bytes(), "/edition/001/cartoon-mobile.webp": (root / "edition/001/cartoon-mobile.webp").read_bytes()}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = responses.get(self.path, b"missing")
                self.send_response(200 if self.path in responses else 404); self.end_headers(); self.wfile.write(payload)
            def log_message(self, *_): pass
        server = HTTPServer(("127.0.0.1", 0), Handler); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            result = verify_production_content(f"http://127.0.0.1:{server.server_port}", edition, approved_asset_root=root)
            self.assertTrue(result["verified"])
            responses["/edition/001/edition.json"] = b"{}"
            with self.assertRaises(RuntimeError): verify_production_content(f"http://127.0.0.1:{server.server_port}", edition, approved_asset_root=root)
            responses["/edition/001/edition.json"] = json.dumps(edition).encode()
            responses["/edition/001/cartoon-mobile.webp"] = b"mismatch"
            with self.assertRaisesRegex(RuntimeError, "asset hash mismatch"):
                verify_production_content(f"http://127.0.0.1:{server.server_port}", edition, approved_asset_root=root)
        finally: server.shutdown(); server.server_close()

    def test_push_requires_separate_live_authorization(self):
        with self.assertRaises(ApprovalError):
            push_main(Path("/tmp/no-such-repo"), "not-a-real-sha")

    def test_production_timeout_fails_closed(self):
        edition = json.loads(Path("/home/ubuntu/AI-HAYOM/edition/001/edition.json").read_text())
        with patch("ai_hayom.urllib.request.urlopen", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError): verify_production_content("http://fake", edition)



if __name__ == "__main__":
    unittest.main()
