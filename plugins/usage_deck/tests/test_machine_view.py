"""Machine-wide account aggregation (DESIGN.md §10): union of global + every profile credential pool,
deduped by account identity, each card listing the homes that hold it.

All data is synthetic: tokens are unsigned JWT-shaped strings built by ``make_jwt`` with invented claims.
"""

import json
import os
import unittest
from pathlib import Path

from plugins.usage_deck.discovery import (
    FileInventorySource,
    InventoryUnavailable,
    account_identity,
    discover,
    discover_machine,
)
from plugins.usage_deck.models.dto import Holder

from .helpers import ALL_SENTINELS, SENTINEL_KEY, SENTINEL_REFRESH, SENTINEL_TOKEN, make_jwt, scan_for
from .test_schema_preservation import SchemaAsserts
from .test_service import GPU, METRICS, ServiceBase

AUTH = "https://api.openai.com/auth"


def codex_token(account, user, plan, jti="1"):
    return make_jwt({AUTH: {"chatgpt_account_id": account, "chatgpt_user_id": user, "chatgpt_plan_type": plan},
                     "jti": jti})


def pool(provider, *rows):
    return {"credential_pool": {provider: list(rows)}}


class MachineBase(ServiceBase):
    """Scratch machine: root (global) + profiles/work (viewed) + extra profiles written by tests."""

    def source(self):
        return FileInventorySource(
            self.s.home, self.s.root, config_loader=lambda: self.config,
            custom_provider_loader=lambda c: self.custom, env_lookup=lambda n: None)

    def machine(self):
        return discover_machine(self.source())

    def seed_machine(self):
        """business seat in global + architect(legacy, same seat); 'go' seat x2 rows in verifier; anthropic in work."""
        self.business = codex_token("acct-biz", "user-1", "business", "a")
        self.business_copy = codex_token("acct-biz", "user-1", "business", "b")  # refreshed token, same identity
        self.go = codex_token("acct-go", "user-2", "go", "c")
        self.go_again = codex_token("acct-go", "user-2", "go", "d")
        self.s.write_auth({"credential_pool": {"openai-codex": [{"id": "g-biz", "access_token": self.business}]}}, where="global")
        self.s.write_profile("architect", {"providers": {"openai-codex": {"tokens": {"access_token": self.business_copy}}}})
        self.s.write_profile("verifier", {"credential_pool": {"openai-codex": [
            {"id": "v-go-1", "access_token": self.go}, {"id": "v-go-2", "access_token": self.go_again}]}})
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "ant-1", "access_token": "sk-ant-oat-sntl-1"}]}})


class DedupeMerge(MachineBase):
    def test_same_account_in_several_homes_is_one_card_with_all_holders(self):
        self.seed_machine()
        items = [i for i in self.machine() if i.provider_id == "openai-codex"]
        self.assertEqual(len(items), 2)  # business (global + architect) and go (verifier x2 rows)
        by_plan = {i.plan_name: i for i in items}
        self.assertEqual(by_plan["business"].holders, (Holder("global", True), Holder("architect", False)))
        self.assertEqual(by_plan["go"].holders, (Holder("verifier", False),))  # two rows, one account, one holder

    def test_profile_mode_still_shows_rows_unmerged(self):
        self.seed_machine()
        self.s.write_auth({"credential_pool": {"openai-codex": [
            {"id": "w1", "access_token": self.go}, {"id": "w2", "access_token": self.go_again}]}})
        self.assertEqual(len([i for i in discover(self.source()) if i.provider_id == "openai-codex"]), 2)
        machine = [i for i in self.machine() if i.provider_id == "openai-codex"]
        self.assertEqual(len(machine), 2)
        self.assertEqual({h.profile for i in machine if i.plan_name == "go" for h in i.holders}, {"verifier", "work"})

    def test_account_id_depends_on_identity_only_not_on_token_or_home(self):
        self.seed_machine()
        first = {i.plan_name: i.account_id for i in self.machine() if i.provider_id == "openai-codex"}
        self.s.write_profile("verifier", {"credential_pool": {"openai-codex": [
            {"id": "other-row", "access_token": codex_token("acct-go", "user-2", "go", "zzz")}]}})
        self.s.write_profile("architect", {"credential_pool": {}})
        again = {i.plan_name: i.account_id for i in self.machine() if i.provider_id == "openai-codex"}
        self.assertEqual(first, again)
        for account_id in first.values():
            self.assertRegex(account_id, r"^acct_[0-9a-f]{24}$")
        self.assertNotIn("acct-go", json.dumps(first))

    def test_workspace_seats_of_different_users_stay_separate(self):
        self.s.write_auth(pool("openai-codex", {"id": "g1", "access_token": codex_token("acct-ws", "user-1", "business")}), where="global")
        self.s.write_profile("verifier", pool("openai-codex", {"id": "v1", "access_token": codex_token("acct-ws", "user-2", "business")}))
        self.assertEqual(len([i for i in self.machine() if i.provider_id == "openai-codex"]), 2)

    def test_account_identity_helper(self):
        self.assertEqual(account_identity("openai-codex", codex_token("A", "U", "go")), ("A", "U"))
        self.assertIsNone(account_identity("openai-codex", make_jwt({"plan": "go"})))
        for token in (None, "", "abc", "a.b.c", make_jwt({AUTH: {"chatgpt_account_id": "bad id!"}}),
                      make_jwt({AUTH: {"chatgpt_account_id": 5}})):
            self.assertIsNone(account_identity("openai-codex", token))
        # account claim alone (no user claim) is NOT an identity (§12.5): it only names a workspace
        no_user = make_jwt({AUTH: {"chatgpt_account_id": "A"}})
        self.assertIsNone(account_identity("openai-codex", no_user))
        # user claim priority: chatgpt_user_id, then user_id, then chatgpt_account_user_id
        both = make_jwt({AUTH: {"chatgpt_account_id": "A", "user_id": "second", "chatgpt_user_id": "first",
                                "chatgpt_account_user_id": "third"}})
        self.assertEqual(account_identity("openai-codex", both), ("A", "first"))
        second = make_jwt({AUTH: {"chatgpt_account_id": "A", "user_id": "second", "chatgpt_account_user_id": "third"}})
        self.assertEqual(account_identity("openai-codex", second), ("A", "second"))
        third = make_jwt({AUTH: {"chatgpt_account_id": "A", "chatgpt_account_user_id": "third"}})
        self.assertEqual(account_identity("openai-codex", third), ("A", "third"))
        # exact claim namespace only; no email / subject / other-provider fallback
        self.assertIsNone(account_identity("openai-codex", make_jwt(
            {"https://api.openai.com/other": {"chatgpt_account_id": "A", "chatgpt_user_id": "U"}})))
        self.assertIsNone(account_identity("openai-codex", make_jwt(
            {"sub": "user-sub", "email": "a@b.example", AUTH: {"chatgpt_account_id": "A"}})))
        self.assertIsNone(account_identity("openai-codex-2", codex_token("A", "U", "go")))
        self.assertIsNone(account_identity("OpenAI-Codex", codex_token("A", "U", "go")))
        self.assertIsNone(account_identity("anthropic", codex_token("A", "U", "go")))

    def test_identical_public_row_ids_in_different_homes_never_merge(self):
        """Public row ids are store-local (§12.5): equal ids across holders are NOT a shared account."""
        self.s.write_auth(pool("anthropic", {"id": "same", "access_token": "sk-ant-1"}), where="global")
        self.s.write_profile("a", pool("anthropic", {"id": "same", "access_token": "sk-ant-1"}))
        self.s.write_profile("b", pool("anthropic", {"id": "different", "access_token": "sk-ant-1"}))  # same secret, other row
        self.s.write_auth({"credential_pool": {}})
        items = [i for i in self.machine() if i.provider_id == "anthropic"]
        self.assertEqual(len(items), 3)  # never merged by row id or by comparing secrets
        self.assertEqual(sorted(len(i.holders) for i in items), [1, 1, 1])
        self.assertEqual(len({i.account_id for i in items}), 3)
        refs = sorted(r for i in items for r in i.credential_refs)
        self.assertEqual(refs, [(False, "a", "same"), (False, "b", "different"), (True, "global", "same")])
        for item in items:  # each card only ever carries ITS OWN store's reference
            self.assertEqual(len(item.credential_refs), 1)
            self.assertEqual((item.credential_refs[0][0], item.credential_refs[0][1]),
                             (item.holders[0].is_global, item.holders[0].profile))

    def test_anonymous_rows_are_never_merged_across_homes(self):
        self.s.write_auth({"credential_pool": {"nous": [{"access_token": "n-1"}]}}, where="global")
        self.s.write_profile("a", {"credential_pool": {"nous": [{"access_token": "n-1"}]}})
        self.s.write_auth({"credential_pool": {}})
        items = [i for i in self.machine() if i.provider_id == "nous"]
        self.assertEqual(len(items), 2)
        self.assertEqual(sorted(len(i.holders) for i in items), [1, 1])

    def test_fallback_to_global_pool_does_not_make_a_profile_a_holder(self):
        self.s.write_auth(pool("openai-codex", {"id": "g1", "access_token": codex_token("A", "U", "go")}), where="global")
        self.s.write_profile("empty", {"credential_pool": {}})
        (item,) = [i for i in self.machine() if i.provider_id == "openai-codex"]
        self.assertEqual(item.holders, (Holder("global", True),))

    def test_labels_follow_first_seen_order_per_provider(self):
        self.seed_machine()
        codex = [i for i in self.machine() if i.provider_id == "openai-codex"]
        labels = {i.plan_name: i.account_label for i in codex}
        self.assertEqual(labels, {"business": "Account 1", "go": "Account 2"})  # global is read before profiles

    def test_merged_item_unions_secrets_and_keeps_a_credential(self):
        self.seed_machine()
        business = next(i for i in self.machine() if i.plan_name == "business")
        self.assertIn(self.business, business.secrets)
        self.assertIn(self.business_copy, business.secrets)
        self.assertEqual(business.auth_status, "available")
        self.assertEqual(business.plan_source, "unverified_claim")

    def test_legacy_provider_entries_count_only_when_the_same_store_has_no_pool(self):
        self.s.write_profile("a", {"credential_pool": {"openai-codex": [{"id": "p1", "access_token": codex_token("A", "U", "go", "1")}]},
                                   "providers": {"openai-codex": {"tokens": {"access_token": codex_token("B", "U2", "plus", "2")}}}})
        self.s.write_auth({"credential_pool": {}})
        items = [i for i in self.machine() if i.provider_id == "openai-codex"]
        self.assertEqual([i.plan_name for i in items], ["go"])


class ConfigDerived(MachineBase):
    def test_pooled_self_hosted_endpoint_and_config_entry_are_one_card(self):
        self.s.write_auth(pool("custom:gpu", {"id": "row1", "base_url": GPU + "/v1", "access_token": SENTINEL_KEY}), where="global")
        self.custom = [{"name": "gpu", "base_url": GPU + "/v1", "api_key": SENTINEL_KEY}]
        items = [i for i in self.machine() if i.provider_id == "custom:gpu"]
        self.assertEqual(len(items), 1)
        self.assertEqual([h.profile for h in items[0].holders], ["global"])

    def test_config_only_endpoint_is_held_by_the_viewed_profile(self):
        self.custom = [{"name": "box", "base_url": "http://box.lan:9000/v1"}]
        (item,) = [i for i in self.machine() if i.provider_id == "custom:box"]
        self.assertEqual(item.holders, (Holder("work", False),))
        self.assertEqual(item.kind, "self_hosted")

    def test_credentialless_configured_cloud_provider_is_dropped_when_a_home_holds_it(self):
        self.config = {"model": {"provider": "openai-codex"}}
        self.s.write_profile("verifier", pool("openai-codex", {"id": "v1", "access_token": codex_token("A", "U", "go")}))
        self.assertEqual(len([i for i in self.machine() if i.provider_id == "openai-codex"]), 1)
        self.s.write_profile("verifier", {"credential_pool": {}})
        (lonely,) = [i for i in self.machine() if i.provider_id == "openai-codex"]  # nobody holds it: keep the configured one
        self.assertEqual((lonely.auth_status, lonely.holders), ("missing", (Holder("work", False),)))


class StoreReading(MachineBase):
    def test_unreadable_other_store_is_skipped_but_viewed_store_fails(self):
        self.s.write_auth(pool("nous", {"id": "n1", "access_token": "n"}), where="global")
        self.s.write_profile("broken", "{not json")
        self.s.write_profile("huge", " " * (4 * 1024 * 1024 + 1))
        self.s.write_profile("list", "[]")
        self.s.write_auth({"credential_pool": {}})
        self.assertEqual([i.provider_id for i in self.machine()], ["nous"])
        self.s.write_auth("{broken")
        with self.assertRaises(InventoryUnavailable):
            self.machine()

    def test_only_real_profile_directories_with_regular_auth_files_are_read(self):
        outside = Path(self.s.root).parent / "usage-deck-outside-probe"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: [p.unlink() for p in outside.iterdir()] or outside.rmdir())
        (outside / "auth.json").write_text(json.dumps(pool("nous", {"id": "leak", "access_token": "x"})))
        profiles = self.s.root / "profiles"
        os.symlink(outside, profiles / "linked")                       # symlinked profile dir
        (profiles / "bad name").mkdir()                                 # invalid profile name
        (profiles / "bad name" / "auth.json").write_text(json.dumps(pool("nous", {"id": "x1", "access_token": "x"})))
        (profiles / ".hidden").mkdir()
        (profiles / ".hidden" / "auth.json").write_text(json.dumps(pool("nous", {"id": "x2", "access_token": "x"})))
        (profiles / "nofile").mkdir()
        (profiles / "filelink").mkdir()
        os.symlink(outside / "auth.json", profiles / "filelink" / "auth.json")  # symlinked auth file
        (profiles / "plainfile").write_text("x")
        self.s.write_auth({"credential_pool": {}})
        self.assertEqual(self.machine(), [])

    def test_viewed_profile_and_global_names(self):
        src = self.source()
        self.assertEqual(src.profile_name(), ("work", False))
        root_view = FileInventorySource(self.s.root, self.s.root)
        self.assertEqual(root_view.profile_name(), ("global", True))
        self.assertEqual([s.profile for s in root_view.read_stores()], [])  # nothing written yet
        self.s.write_auth(pool("nous", {"id": "n", "access_token": "t"}), where="global")
        self.assertEqual([(s.profile, s.is_global) for s in root_view.read_stores()], [("global", True)])


class MachineViaService(MachineBase, SchemaAsserts):
    def cards(self, view=None, endpoint="subscriptions"):
        params = [("view", view)] if view else []
        status, body = self.call(self.make(), endpoint, params)
        self.assert_exact_envelope(endpoint, status, body)
        return body

    def test_default_is_machine_view_with_holders(self):
        self.seed_machine()
        body = self.cards()
        self.assertEqual(body["data"]["view"], "machine")
        by_plan = {c["plan_name"]: c for c in body["data"]["cards"] if c["provider_id"] == "openai-codex"}
        self.assertEqual(by_plan["business"]["holders"], [{"profile": "global", "is_global": True},
                                                          {"profile": "architect", "is_global": False}])
        self.assertEqual(by_plan["go"]["holders"], [{"profile": "verifier", "is_global": False}])
        self.assertEqual((by_plan["go"]["state"], by_plan["go"]["confidence"]), ("opaque", "opaque"))
        ant = next(c for c in body["data"]["cards"] if c["provider_id"] == "anthropic")
        self.assertEqual(ant["holders"], [{"profile": "work", "is_global": False}])

    def test_profile_view_is_the_old_profile_scoped_inventory(self):
        self.seed_machine()
        body = self.cards("profile")
        self.assertEqual(body["data"]["view"], "profile")
        self.assertEqual([c["provider_id"] for c in body["data"]["cards"]], ["anthropic", "openai-codex"])  # work + global fallback
        self.assertTrue(all(c["holders"] == [] for c in body["data"]["cards"]))

    def test_invalid_view_is_rejected(self):
        for value in ("all", "", "Machine"):
            status, body = self.call(self.make(), "subscriptions", [("view", value)])
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_query"), value)
        status, _ = self.call(self.make(), "subscriptions", [("view", "machine"), ("view", "profile")])
        self.assertEqual(status, 400)

    def test_views_are_cached_separately(self):
        self.seed_machine()
        svc = self.make()
        machine = svc.handle("subscriptions", [("view", "machine")])[1]["data"]["cards"]
        profile = svc.handle("subscriptions", [("view", "profile")])[1]["data"]["cards"]
        self.assertGreater(len(machine), len(profile))

    def test_source_without_machine_support_reports_profile_view(self):
        self.seed_machine()

        class ProfileOnly:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                if name in ("read_stores", "profile_name"):
                    raise AttributeError(name)
                return getattr(self._inner, name)

        from plugins.usage_deck.service import UsageDeckService
        from .helpers import NOW, FakeAnalytics
        svc = UsageDeckService(inventory_factory=lambda: ProfileOnly(self.source()),
                               analytics_factory=lambda p: FakeAnalytics(), transport=self.transport, clock=lambda: NOW)
        status, body = svc.handle("subscriptions", [])
        self.assertEqual((status, body["data"]["view"]), (200, "profile"))

    def test_health_strip_lists_endpoints_from_every_home(self):
        from plugins.usage_deck.adapters.cloud import OPENROUTER_KEY_URL  # noqa: F401
        from .helpers import json_response, raw_response
        self.s.write_profile("gpu-owner", pool("custom:gpu", {"id": "g1", "base_url": GPU + "/v1", "access_token": SENTINEL_KEY}))
        self.transport.routes.update({GPU + "/metrics": raw_response(METRICS, latency=2.0),
                                      GPU + "/v1/models": json_response({"data": []}, latency=6.0)})
        entries = self.cards("machine", "health")["data"]["entries"]
        self.assertEqual([e["provider_id"] for e in entries], ["custom:gpu"])
        self.assertEqual(self.cards("profile", "health")["data"]["entries"], [])

    def test_usage_provider_filter_validates_against_the_selected_view(self):
        self.s.write_profile("verifier", pool("nous", {"id": "n1", "access_token": "n-token"}))
        self.s.write_auth({"credential_pool": {}})
        svc = self.make()
        status, body = svc.handle("usage", [("provider", "nous")])
        self.assertEqual((status, body["data"]["reason"]), (200, "attribution_unavailable"))
        status, body = svc.handle("usage", [("provider", "nous"), ("view", "profile")])
        self.assertEqual((status, body["error"]["code"]), (404, "provider_not_found"))

    def test_no_secret_anywhere_in_machine_responses(self):
        self.seed_machine()
        self.s.write_profile("extra", pool("openrouter", {"id": "or1", "access_token": SENTINEL_KEY, "refresh_token": SENTINEL_REFRESH,
                                                          "label": SENTINEL_TOKEN}))
        needles = list(ALL_SENTINELS) + [self.business, self.business_copy, self.go, self.go_again, "sk-ant-oat-sntl-1",
                                         "acct-biz", "acct-go", "user-1", "user-2"]
        svc = self.make()
        for endpoint in ("subscriptions", "health"):
            for view in ("machine", "profile"):
                status, body = svc.handle(endpoint, [("view", view)])
                self.assertEqual(status, 200)
                self.assertEqual(scan_for(body, needles), [], (endpoint, view))
                self.assertNotIn("eyJ", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
