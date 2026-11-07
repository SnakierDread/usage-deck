"""Scoped machine inventory (DESIGN.md §12.5): identity policy, holder-scoped fallback, credential-use isolation,
symlinked ``profiles`` parent, scoped capture references. All tokens/claims here are SYNTHETIC.
"""

import json
import os
import tempfile
import types
import unittest
from pathlib import Path

from plugins.usage_deck.adapters.cloud import OPENROUTER_KEY_URL
from plugins.usage_deck.discovery import (
    FileInventorySource,
    RawInventory,
    _machine_identity,
    discover,
    discover_machine,
    identity_json,
)
from plugins.usage_deck.models.dto import Holder
from plugins.usage_deck.models.identity import make_account_id
from plugins.usage_deck.service import UsageDeckService

from .helpers import NOW, FakeAnalytics, FakeTransport, ScratchHome, json_response, make_jwt, raw_response, scan_for
from .test_machine_view import MachineBase, codex_token, pool
from .test_schema_preservation import SchemaAsserts
from .test_service import GPU, METRICS

AUTH = "https://api.openai.com/auth"
KEY_WORK, KEY_VERI, KEY_GLOB = "or-key-work-AAAA1111", "or-key-veri-BBBB2222", "or-key-glob-CCCC3333"
OR_OK = json_response({"data": {"usage": 1.5, "limit": 5, "limit_remaining": 3.5}})


def codex_rows(*specs):
    return pool("openai-codex", *[{"id": rid, "access_token": tok} for rid, tok in specs])


def only(items, provider):
    return [i for i in items if i.provider_id == provider]


class IdentityPolicy(MachineBase):
    def test_account_claim_alone_never_merges_or_separates_seats(self):
        no_user = lambda tag: make_jwt({AUTH: {"chatgpt_account_id": "acct-ws"}, "jti": tag})  # noqa: E731
        self.s.write_auth(codex_rows(("g1", no_user("a"))), where="global")
        self.s.write_profile("verifier", codex_rows(("v1", no_user("b"))))
        self.s.write_auth({"credential_pool": {}})
        items = only(self.machine(), "openai-codex")
        self.assertEqual(len(items), 2)  # same workspace, no user claim: holder-scoped fallback, never merged
        self.assertTrue(all(len(i.holders) == 1 for i in items))
        self.assertTrue(all(i.jwt_pair is None for i in items))

    def test_same_holder_no_user_rows_with_equal_tokens_stay_separate_accounts(self):
        token = make_jwt({AUTH: {"chatgpt_account_id": "acct-ws"}})
        self.s.write_auth(codex_rows(("r1", token), ("r2", token)))
        self.assertEqual(len(only(self.machine(), "openai-codex")), 2)

    def test_complete_pair_with_same_seat_merges_across_refreshed_tokens_and_unions_scoped_refs(self):
        self.s.write_auth(codex_rows(("g1", codex_token("acct-1", "user-1", "business", "a"))), where="global")
        self.s.write_profile("verifier", codex_rows(("v1", codex_token("acct-1", "user-1", "business", "b"))))
        self.s.write_auth({"credential_pool": {}})
        (item,) = only(self.machine(), "openai-codex")
        self.assertEqual(item.holders, (Holder("global", True), Holder("verifier", False)))
        self.assertEqual(item.credential_refs, ((True, "global", "g1"), (False, "verifier", "v1")))
        self.assertEqual(item.jwt_pair, ("acct-1", "user-1"))

    def test_different_users_or_accounts_do_not_merge(self):
        self.s.write_auth(codex_rows(("g1", codex_token("acct-1", "user-1", "go"))), where="global")
        self.s.write_profile("a", codex_rows(("a1", codex_token("acct-1", "user-2", "go"))))   # same workspace, other seat
        self.s.write_profile("b", codex_rows(("b1", codex_token("acct-2", "user-1", "go"))))   # same user id, other account
        self.s.write_auth({"credential_pool": {}})
        self.assertEqual(len(only(self.machine(), "openai-codex")), 3)

    def test_no_user_workspace_stays_separate_from_a_complete_seat_of_the_same_workspace(self):
        self.s.write_auth(codex_rows(("g1", codex_token("acct-1", "user-1", "go"))), where="global")
        self.s.write_profile("a", codex_rows(("a1", make_jwt({AUTH: {"chatgpt_account_id": "acct-1"}}))))
        self.s.write_auth({"credential_pool": {}})
        self.assertEqual(len(only(self.machine(), "openai-codex")), 2)

    def test_jwt_grouping_only_for_canonical_openai_codex(self):
        token = lambda tag: codex_token("acct-1", "user-1", "go", tag)  # noqa: E731
        self.s.write_auth(pool("anthropic", {"id": "x1", "access_token": token("1")}), where="global")
        self.s.write_profile("a", pool("anthropic", {"id": "x2", "access_token": token("2")}))
        self.s.write_auth(pool("openai-codex-2", {"id": "y1", "access_token": token("3")}), where="global") if False else None
        self.s.write_profile("b", pool("OpenAI-Codex", {"id": "z1", "access_token": token("4")}))
        self.s.write_profile("c", pool("OpenAI-Codex", {"id": "z2", "access_token": token("5")}))
        self.s.write_auth({"credential_pool": {}})
        items = self.machine()
        self.assertEqual(len(only(items, "anthropic")), 2)
        self.assertEqual(len(only(items, "OpenAI-Codex")), 2)
        self.assertTrue(all(i.jwt_pair is None for i in items))

    def test_identity_json_is_compact_ascii_and_account_id_hashes_exactly_it(self):
        self.s.write_auth(codex_rows(("g1", codex_token("acct-1", "user-1", "go"))), where="global")
        (item,) = only(self.machine(), "openai-codex")
        expected = '["jwt","openai-codex",null,"acct-1","user-1"]'
        self.assertEqual(item.identity_key, expected)
        self.assertEqual(item.account_id, make_account_id("openai-codex", None, "machine:" + expected))
        self.assertEqual(identity_json(["row", False, "w\u00e9", "p", None, 1]), '["row",false,"w\\u00e9","p",null,1]')

    def test_fallback_identity_parts_are_holder_provider_endpoint_row_or_ordinal(self):
        self.s.write_auth(pool("anthropic", {"id": "same", "access_token": "sk-ant-g"}), where="global")
        self.s.write_profile("verifier", {"credential_pool": {"nous": [{"access_token": "n1"}]},
                                          "providers": {"openrouter": {"api_key": "k"}}})
        self.s.write_auth({"credential_pool": {}})
        by = {i.provider_id: i for i in self.machine()}
        self.assertEqual(by["anthropic"].identity_key, '["row",true,"global","anthropic",null,"same"]')
        self.assertEqual(by["nous"].identity_key, '["row",false,"verifier","nous",null,1]')
        self.assertEqual(by["openrouter"].identity_key, '["row",false,"verifier","openrouter",null,1]')
        self.assertEqual(by["anthropic"].account_id, make_account_id(
            "anthropic", None, 'machine:["row",true,"global","anthropic",null,"same"]'))

    def test_endpoint_is_part_of_every_grouping(self):
        def item(endpoint, pair=None):
            ep = types.SimpleNamespace(normalized=endpoint) if endpoint else None
            return types.SimpleNamespace(provider_id="p", endpoint=ep, jwt_pair=pair, row_ref="r1")

        holder = Holder("work", False)
        self.assertNotEqual(_machine_identity(holder, item("http://a:1/v1")), _machine_identity(holder, item("http://b:1/v1")))
        self.assertNotEqual(_machine_identity(holder, item("http://a:1/v1", ("A", "U"))),
                            _machine_identity(holder, item("http://b:1/v1", ("A", "U"))))
        self.assertEqual(_machine_identity(holder, item(None, ("A", "U"))), _machine_identity(Holder("x", False), item(None, ("A", "U"))))
        self.assertNotEqual(_machine_identity(holder, item(None)), _machine_identity(Holder("x", False), item(None)))
        self.assertNotEqual(_machine_identity(Holder("global", True), item(None)), _machine_identity(Holder("global", False), item(None)))

    def test_same_endpoint_in_two_homes_is_two_cards_but_one_health_row(self):
        self.s.write_profile("a", pool("custom:gpu", {"id": "g1", "base_url": GPU + "/v1", "access_token": "k-a"}))
        self.s.write_profile("b", pool("custom:gpu", {"id": "g1", "base_url": GPU + "/v1", "access_token": "k-b"}))
        self.s.write_auth(pool("custom:gpu", {"id": "g1", "base_url": GPU + "/v1", "access_token": "k-w"}))  # viewed profile
        self.transport.routes.update({GPU + "/metrics": raw_response(METRICS), GPU + "/v1/models": json_response({"data": []})})
        status, cards = self.call(self.make(), "subscriptions", [("view", "machine")])
        self.assertEqual(len([c for c in cards["data"]["cards"] if c["provider_id"] == "custom:gpu"]), 3)
        status, health = self.call(self.make(), "health", [("view", "machine")])
        self.assertEqual(len(health["data"]["entries"]), 1)  # one row per endpoint even though cards stay separate
        self.assertEqual(health["data"]["entries"][0]["status"], "healthy")  # owned by the selected profile's live card

    def test_selected_source_precedence_root_then_profiles_alphabetically(self):
        self.s.write_profile("b", pool("nous", {"id": "n", "access_token": "t-b"}))
        self.s.write_profile("a", pool("nous", {"id": "n", "access_token": "t-a"}))
        self.s.write_auth(pool("nous", {"id": "n", "access_token": "t-g"}), where="global")
        self.s.write_auth({"credential_pool": {}})
        labels = [(i.account_label, i.holders[0].profile) for i in only(self.machine(), "nous")]
        self.assertEqual(sorted(labels), [("Account 1", "global"), ("Account 2", "a"), ("Account 3", "b")])


class ScopedProfileViewRefs(MachineBase):
    def refs(self, provider="anthropic"):
        return [i.credential_refs for i in only(discover(self.source()), provider)]

    def test_own_pool_rows_are_scoped_to_the_profile(self):
        self.s.write_auth(pool("anthropic", {"id": "r1", "access_token": "sk-ant-1"}))
        self.s.write_auth(pool("anthropic", {"id": "r1", "access_token": "sk-ant-g"}), where="global")
        self.assertEqual(self.refs(), [((False, "work", "r1"),)])  # profile pool shadows global: never global's r1

    def test_global_fallback_is_scoped_to_the_global_store(self):
        self.s.write_auth(pool("anthropic", {"id": "r1", "access_token": "sk-ant-g"}), where="global")
        self.assertEqual(self.refs(), [((True, "global", "r1"),)])

    def test_viewing_the_root_itself_is_global(self):
        self.s.write_auth(pool("anthropic", {"id": "r1", "access_token": "sk-ant-g"}), where="global")
        src = FileInventorySource(self.s.root, self.s.root, config_loader=lambda: {}, env_lookup=lambda n: None)
        self.assertEqual([i.credential_refs for i in discover(src)], [((True, "global", "r1"),)])

    def test_no_refs_without_a_public_row_id_or_a_known_holder(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"access_token": "sk-ant-1"}]}})
        self.assertEqual(self.refs(), [()])
        raw = RawInventory("home", {"anthropic": [{"id": "r1", "access_token": "x"}]}, {}, [], None)  # no holder info
        src = types.SimpleNamespace(read=lambda: raw, env_secret=lambda n: None)
        (item,) = discover(src)
        self.assertEqual(item.credential_refs, ())  # never guessed

    def test_machine_cards_use_source_store_provenance_not_read_order(self):
        self.s.write_profile("verifier", pool("anthropic", {"id": "r1", "access_token": "sk-ant-v"}))
        self.s.write_auth(pool("anthropic", {"id": "r1", "access_token": "sk-ant-w"}))
        self.s.write_auth(pool("anthropic", {"id": "r1", "access_token": "sk-ant-g"}), where="global")
        refs = sorted(i.credential_refs[0] for i in only(self.machine(), "anthropic"))
        self.assertEqual(refs, [(False, "verifier", "r1"), (False, "work", "r1"), (True, "global", "r1")])

    def test_global_fallback_holder_in_machine_inventory_is_global_only(self):
        self.s.write_auth(pool("anthropic", {"id": "r1", "access_token": "sk-ant-g"}), where="global")
        self.s.write_auth({"credential_pool": {}})
        (item,) = only(self.machine(), "anthropic")
        self.assertEqual((item.holders, item.credential_refs), ((Holder("global", True),), ((True, "global", "r1"),)))


class CredentialUseIsolation(MachineBase, SchemaAsserts):
    """Machine-wide inventory is not permission to USE another home's credentials (§12.5)."""

    def setUp(self):
        super().setUp()
        self.transport.routes[OPENROUTER_KEY_URL] = OR_OK

    def seed_openrouter(self, work=True):
        if work:
            self.s.write_auth(pool("openrouter", {"id": "or-w", "access_token": KEY_WORK}))
        else:
            self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("verifier", pool("openrouter", {"id": "or-v", "access_token": KEY_VERI}))
        self.s.write_auth(pool("openrouter", {"id": "or-g", "access_token": KEY_GLOB}), where="global")

    def bearer_tokens(self):
        return sorted(h.get("Authorization", "") for _url, h, _t in self.transport.requests)

    def machine_cards(self, svc=None):
        status, body = self.call(svc or self.make(), "subscriptions", [("view", "machine")])
        self.assert_exact_envelope("subscriptions", status, body)
        return body, {c["holders"][0]["profile"]: c for c in body["data"]["cards"]}

    def test_only_the_selected_profiles_effective_credential_is_ever_sent(self):
        self.seed_openrouter()
        body, cards = self.machine_cards()
        self.assertEqual(self.bearer_tokens(), ["Bearer " + KEY_WORK])
        self.assertEqual(cards["work"]["state"], "ready")
        for name in ("verifier", "global"):  # inventory only: rendered, never probed
            self.assertEqual((cards[name]["state"], cards[name]["reason"], cards[name]["meters"], cards[name]["error"]),
                             ("opaque", "source_unavailable", [], None), name)
            self.assertEqual(cards[name]["auth_status"], "available")
        self.assertEqual(scan_for(body, [KEY_WORK, KEY_VERI, KEY_GLOB]), [])

    def test_global_fallback_credential_is_the_selected_profiles_effective_one(self):
        self.seed_openrouter(work=False)  # the viewed profile has no openrouter rows: it uses the global pool
        _body, cards = self.machine_cards()
        self.assertEqual(self.bearer_tokens(), ["Bearer " + KEY_GLOB])
        self.assertEqual(cards["global"]["state"], "ready")
        self.assertEqual(cards["verifier"]["reason"], "source_unavailable")

    def test_machine_only_world_makes_no_request_at_all_even_on_a_real_transport(self):
        from .test_observer import NetworkSpy
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("verifier", pool("openrouter", {"id": "or-v", "access_token": KEY_VERI}))
        self.s.write_profile("other", pool("custom:gpu", {"id": "g", "base_url": GPU + "/v1", "access_token": "k"}))
        src = self.source()
        svc = UsageDeckService(inventory_factory=lambda: src, analytics_factory=lambda p: FakeAnalytics(), clock=lambda: NOW)
        with NetworkSpy() as spy:  # default UrllibTransport: any attempt would reach socket.connect
            body, _ = svc.handle("subscriptions", [("view", "machine")])[1], None
            health = svc.handle("health", [("view", "machine")])[1]
        self.assertEqual(spy.calls, [])
        self.assertEqual(len(body["data"]["cards"]), 2)
        self.assertTrue(all(c["state"] != "error" for c in body["data"]["cards"]))
        self.assertTrue(all(e["status"] == "unknown" for e in health["data"]["entries"]))

    def test_another_profiles_self_hosted_endpoint_is_not_probed_but_the_selected_one_is(self):
        other = "http://other-box.lan:9000"
        self.s.write_profile("verifier", pool("custom:other", {"id": "o1", "base_url": other + "/v1", "access_token": "k-v"}))
        self.s.write_auth(pool("custom:gpu", {"id": "g1", "base_url": GPU + "/v1", "access_token": "k-w"}))
        self.transport.routes.update({GPU + "/metrics": raw_response(METRICS), GPU + "/v1/models": json_response({"data": []}),
                                      other + "/metrics": raw_response(METRICS), other + "/v1/models": json_response({"data": []})})
        _body, health = self.call(self.make(), "health", [("view", "machine")])
        hosts = {u.split("/")[2] for u in self.transport.urls()}
        self.assertEqual(hosts, {GPU.split("/")[2]})
        by = {e["provider_id"]: e["status"] for e in health["data"]["entries"]}
        self.assertEqual(by, {"custom:gpu": "healthy", "custom:other": "unknown"})
        for _url, headers, _t in self.transport.requests:
            self.assertNotIn("k-v", json.dumps(headers))

    def test_env_credential_belongs_to_the_selected_profile_only(self):
        self.s.write_auth({"credential_pool": {"openrouter": [{"id": "or-w"}]}})  # selected: row without a token
        self.s.write_profile("verifier", {"credential_pool": {"openrouter": [{"id": "or-v"}]}})
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: "ENV-OR-KEY")
        items = {i.holders[0].profile: i for i in only(discover_machine(src), "openrouter")}
        self.assertEqual(items["work"].credential.reveal(), "ENV-OR-KEY")
        self.assertIsNone(items["verifier"].credential)
        self.assertFalse(items["verifier"].live_source)
        self.assertNotIn("ENV-OR-KEY", items["verifier"].secrets)

    def test_merged_card_selects_the_eligible_member_not_the_first_one(self):
        first = codex_token("acct-1", "user-1", "business", "global-copy")
        mine = codex_token("acct-1", "user-1", "business", "work-copy")
        self.s.write_auth(codex_rows(("g1", first)), where="global")
        self.s.write_auth(codex_rows(("w1", mine)))  # selected profile's own pool shadows the global one
        (item,) = only(self.machine(), "openai-codex")
        self.assertEqual([h.profile for h in item.holders], ["global", "work"])  # global is listed first ...
        self.assertEqual(item.credential.reveal(), mine)                          # ... but never supplies the credential
        self.assertTrue(item.live_source)
        self.assertEqual(item.plan_name, "business")

    def test_merged_card_with_only_machine_only_members_has_no_usable_credential(self):
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("a", codex_rows(("a1", codex_token("acct-1", "user-1", "go", "a"))))
        self.s.write_profile("b", codex_rows(("b1", codex_token("acct-1", "user-1", "go", "b"))))
        (item,) = only(self.machine(), "openai-codex")
        self.assertEqual(len(item.holders), 2)
        self.assertEqual((item.credential, item.health_credential, item.live_source, item.auth_status),
                         (None, None, False, "available"))
        # secrets are still unioned for masking: both tokens plus the private account routing claim (§9.2 term 2)
        self.assertEqual(len(item.secrets), 3)
        self.assertIn("acct-1", item.secrets)

    def test_selected_profile_config_endpoints_stay_live(self):
        self.custom = [{"name": "box", "base_url": GPU + "/v1"}]
        self.transport.routes.update({GPU + "/metrics": raw_response(METRICS), GPU + "/v1/models": json_response({"data": []})})
        _body, health = self.call(self.make(), "health", [("view", "machine")])
        self.assertEqual([e["status"] for e in health["data"]["entries"]], ["healthy"])

    def test_profile_view_is_unchanged_by_the_isolation(self):
        self.seed_openrouter()
        status, body = self.call(self.make(), "subscriptions", [("view", "profile")])
        self.assertEqual(self.bearer_tokens(), ["Bearer " + KEY_WORK])
        self.assertEqual([c["state"] for c in body["data"]["cards"]], ["ready"])


class SymlinkedProfilesParent(unittest.TestCase):
    def test_a_symlinked_profiles_directory_is_not_enumerated(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "root"
            outside = Path(base) / "outside"
            (outside / "evil").mkdir(parents=True)
            (outside / "evil" / "auth.json").write_text(json.dumps(pool("nous", {"id": "leak", "access_token": "x"})))
            root.mkdir()
            (root / "auth.json").write_text(json.dumps(pool("nous", {"id": "g", "access_token": "t"})))
            os.symlink(outside, root / "profiles")
            src = FileInventorySource(root, root, config_loader=lambda: {}, env_lookup=lambda n: None)
            self.assertEqual([(s.profile, s.is_global) for s in src.read_stores()], [("global", True)])
            self.assertEqual([i.holders[0].profile for i in discover_machine(src)], ["global"])

    def test_real_profiles_directory_still_enumerates(self):
        s = ScratchHome()
        self.addCleanup(s.cleanup)
        s.write_profile("verifier", pool("nous", {"id": "n", "access_token": "t"}))
        src = FileInventorySource(s.home, s.root, config_loader=lambda: {}, env_lookup=lambda n: None)
        self.assertEqual([s_.profile for s_ in src.read_stores()], ["verifier"])


class ProfileOnlyPrincipal(MachineBase):
    def test_profile_only_clamps_every_view_and_endpoint(self):
        self.s.write_profile("verifier", pool("nous", {"id": "n1", "access_token": "tok-v"}))
        self.s.write_auth({"credential_pool": {}})
        svc = self.make()
        for query in ([], [("view", "machine")], [("view", "profile")]):
            status, body = svc.handle("subscriptions", query, profile_only=True)
            self.assertEqual((status, body["data"]["view"], body["data"]["cards"]), (200, "profile", []))
        status, body = svc.handle("subscriptions", [("view", "machine")])
        self.assertEqual((body["data"]["view"], len(body["data"]["cards"])), ("machine", 1))
        status, body = svc.handle("usage", [("provider", "nous"), ("view", "machine")], profile_only=True)
        self.assertEqual((status, body["error"]["code"]), (404, "provider_not_found"))  # cannot probe other homes' inventory
        status, body = svc.handle("subscriptions", [("view", "everything")], profile_only=True)
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_query"))  # still validated, never widened

    def test_cache_never_serves_a_machine_snapshot_to_a_clamped_request(self):
        self.s.write_profile("verifier", pool("nous", {"id": "n1", "access_token": "tok-v"}))
        self.s.write_auth({"credential_pool": {}})
        svc = self.make()
        self.assertEqual(len(svc.handle("subscriptions", [("view", "machine")])[1]["data"]["cards"]), 1)
        self.assertEqual(svc.handle("subscriptions", [("view", "machine")], profile_only=True)[1]["data"]["cards"], [])


if __name__ == "__main__":
    unittest.main()
