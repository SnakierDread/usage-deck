"""Combined regression: the Codex spend adapter (DESIGN §9.2) under the machine-view isolation rule (§12.5).

Two independently reviewed features meet here and both must hold at once:

* only a member of the SELECTED profile's effective pool may supply the Codex bearer and the account/residency
  routing claims sent with it; a machine-only holder issues no request and supplies no credential;
* every private routing value of EVERY member (including machine-only ones and short values) stays in the
  masking context and never reaches a DTO, cache or log line.

Synthetic tokens and an injected transport only: no live request, no refresh, no real credential.
"""

import logging
import unittest

from plugins.usage_deck.adapters.cloud import CODEX_TARGET, CODEX_USAGE_URL
from plugins.usage_deck.adapters.transport import GuardedTransport, TransportError
from plugins.usage_deck.discovery import FileInventorySource, discover_machine

from .helpers import NOW, json_response, scan_for
from .test_codex_wham import codex_token, limit_body
from .test_schema_preservation import SchemaAsserts
from .test_service import ServiceBase

CODEX_HEADERS = ("Authorization", "ChatGPT-Account-ID", "x-openai-internal-codex-residency")


class CodexMachineScope(ServiceBase, SchemaAsserts):
    """Selected profile ``work``, global root and scratch profiles (the shared service fixtures)."""

    def setUp(self):
        super().setUp()
        self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body())

    def seed(self, *rows, where="profile"):
        self.s.write_auth({"credential_pool": {"openai-codex": list(rows)}}, where=where)

    def codex(self, svc=None, view="profile"):
        status, body = (svc or self.make()).handle("subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        return [c for c in body["data"]["cards"] if c["provider_id"] == "openai-codex"]

    def pool(self, *rows):
        return {"credential_pool": {"openai-codex": [{"id": rid, "access_token": tok} for rid, tok in rows]}}

    def sent(self):
        return [(h["Authorization"], h["ChatGPT-Account-ID"], h.get("x-openai-internal-codex-residency"))
                for _url, h, _t in self.transport.requests]

    # -------------------------------------------------------------- selected-source success

    def test_selected_and_cross_profile_credentials_each_go_out_with_their_own_routing(self):
        mine = codex_token(account="udsyn-acct-S", user="udsyn-user-S", residency="udsyn-region-S", jti="mine")
        other = codex_token(account="udsyn-acct-O", user="udsyn-user-O", residency="udsyn-region-O", jti="other")
        self.seed({"id": "w1", "access_token": mine})                       # selected profile 'work'
        self.s.write_profile("verifier", self.pool(("v1", other)))          # another profile's seat (§13)
        cards = self.codex(view="machine")
        self.assertEqual(len(cards), 2)
        # one request per seat; the bearer and routing claims of each come from that seat's own token
        self.assertEqual(sorted(self.sent()), sorted([("Bearer " + mine, "udsyn-acct-S", "udsyn-region-S"),
                                                      ("Bearer " + other, "udsyn-acct-O", "udsyn-region-O")]))
        self.assertEqual([c["state"] for c in cards], ["ready", "ready"])
        for card in cards:
            self.assertEqual((card["confidence"], card["reason"], card["error"], card["auth_status"]),
                             ("exact", None, None, "available"))
            (m,) = card["meters"]
            self.assertEqual((m["id"], m["unit"], m["source"], m["value"], m["limit"], m["remaining"]),
                             ("workspace_spend", "credit", "chatgpt_wham_usage", 864.5, 1500.0, 635.5))
        self.assertEqual(sorted(h["profile"] for c in cards for h in c["holders"]), ["verifier", "work"])  # attribution kept

    def test_global_fallback_is_the_selected_profiles_effective_source(self):
        glob = codex_token(account="udsyn-acct-G", user="udsyn-user-G", residency="udsyn-region-G", jti="g")
        self.seed({"id": "g1", "access_token": glob}, where="global")      # 'work' has no codex row -> global applies
        cards = self.codex(view="machine")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["state"], "ready")
        self.assertEqual(self.sent(), [("Bearer " + glob, "udsyn-acct-G", "udsyn-region-G")])

    # -------------------------------------------------------------- other profiles' seats (DESIGN §13)

    def test_machine_view_reads_other_profiles_codex_seats_with_their_own_credential(self):
        self.s.write_auth({"credential_pool": {}})                         # work and global hold no Codex row
        a = codex_token(account="udsyn-acct-A", user="udsyn-user-A", residency="zq", jti="a")
        b = codex_token(account="udsyn-acct-B", user="udsyn-user-B", residency="udsyn-region-B", jti="b")
        self.s.write_profile("a", self.pool(("a1", a)))
        self.s.write_profile("b", self.pool(("b1", b)))
        svc = self.make()
        cards = self.codex(svc=svc, view="machine")
        self.assertEqual(len(cards), 2)
        self.assertEqual(sorted(self.sent()), sorted([("Bearer " + a, "udsyn-acct-A", "zq"),
                                                      ("Bearer " + b, "udsyn-acct-B", "udsyn-region-B")]))
        for card in cards:
            self.assertEqual((card["state"], card["confidence"], card["reason"], card["error"]),
                             ("ready", "exact", None, None))
            self.assertEqual(len(card["meters"]), 1)
        # item level: the credential comes from the holder, the item is flagged cross-profile, and the
        # service grants nothing but the one wham GET (read-only allowlist, in code)
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        items = [i for i in discover_machine(src) if i.provider_id == "openai-codex"]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertTrue(item.live_source and item.cross_profile)
            self.assertIsNotNone(item.credential)
            ctx = svc._context(item, None, NOW, machine_view=True)
            self.transport.requests.clear()
            for url in ("https://chatgpt.com/backend-api/usage", "https://chatgpt.com/backend-api/wham/usage?x=1",
                        "https://api.openai.com/v1/usage", "https://chatgpt.com.evil.example/backend-api/wham/usage"):
                with self.assertRaises(TransportError):
                    ctx.transport.get(url, headers={})
            with self.assertRaises(TransportError):
                ctx.transport.request("POST", CODEX_USAGE_URL, headers={})
            self.assertEqual(self.transport.requests, [])
            ctx.transport.get(CODEX_USAGE_URL, headers={})
            self.assertEqual(len(self.transport.requests), 1)

    def test_cross_profile_item_outside_the_machine_view_is_inventory_only_again(self):
        # Defence in depth: even a hand-built cross-profile item gets no credential/target from the service
        # unless the deck being built is the machine view.
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("a", self.pool(("a1", codex_token(account="udsyn-acct-A", user="udsyn-user-A", jti="a"))))
        svc = self.make()
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        (item,) = [i for i in discover_machine(src) if i.provider_id == "openai-codex"]
        self.assertTrue(item.cross_profile)
        ctx = svc._context(item, None, NOW, machine_view=False)
        self.assertEqual((ctx.credential, ctx.live_source, ctx.cross_profile), (None, False, False))
        with self.assertRaises(TransportError):
            ctx.transport.get(CODEX_USAGE_URL, headers={})
        self.assertEqual(self.transport.requests, [])

    def test_profile_view_never_spends_another_profiles_credential(self):
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("verifier", self.pool(("v1", codex_token(account="udsyn-acct-V", user="udsyn-user-V", jti="v"))))
        for query in ([("view", "profile")], [("view", "machine")]):
            status, body = self.make().handle("subscriptions", query, profile_only=True)  # restricted principal
            self.assertEqual((status, body["data"]["view"], body["data"]["cards"]), (200, "profile", []))
        status, body = self.make().handle("subscriptions", [("view", "profile")])
        self.assertEqual(body["data"]["cards"], [])
        self.assertEqual(self.transport.requests, [])                      # zero egress: nothing of verifier's left

    def test_machine_only_adapter_is_inert_even_if_a_credential_and_target_were_wired(self):
        # Defence in depth: the adapter itself refuses on live_source=False (probe and usage).
        from plugins.usage_deck.adapters import create_adapter
        from .helpers import FakeTransport, make_ctx
        token = codex_token()
        inner = FakeTransport({CODEX_USAGE_URL: json_response(limit_body())})
        ctx = make_ctx("openai-codex", credential=token, secrets=[token], live_source=False,
                       transport=GuardedTransport(inner, [CODEX_TARGET]))
        adapter = create_adapter(ctx)
        probe = adapter.probe()
        usage = adapter.get_usage(30)
        self.assertEqual((probe.reason, usage.reason, usage.meters, usage.error), ("source_unavailable", "source_unavailable", (), None))
        self.assertEqual(inner.requests, [])

    # -------------------------------------------------------------- merged members

    def merged_fixture(self):
        """One seat (same account+user claims) held in three homes under three different tokens/residencies."""
        tok = {name: codex_token(account="udsyn-acct-M", user="udsyn-user-M", residency=res, jti=name)
               for name, res in (("g", "udsyn-region-G"), ("w", "udsyn-region-W"), ("v", "zq"))}
        self.seed({"id": "g1", "access_token": tok["g"]}, where="global")
        self.s.write_profile("verifier", self.pool(("v1", tok["v"])))
        return tok

    def test_merged_members_use_the_selected_members_token_and_its_routing_not_the_first_listed(self):
        tok = self.merged_fixture()
        self.seed({"id": "w1", "access_token": tok["w"]})                  # 'work' shadows the global pool
        cards = self.codex(view="machine")
        self.assertEqual(len(cards), 1)                                    # one display-merged card ...
        self.assertEqual([h["profile"] for h in cards[0]["holders"]], ["global", "verifier", "work"])  # ... global listed first
        self.assertEqual(cards[0]["state"], "ready")
        # ... but the request is exactly work's token with work's own residency, never global's or verifier's
        self.assertEqual(self.sent(), [("Bearer " + tok["w"], "udsyn-acct-M", "udsyn-region-W")])

    def test_merged_card_on_global_fallback_uses_the_global_token_and_its_routing(self):
        tok = self.merged_fixture()                                        # 'work' has no row: global is effective
        cards = self.codex(view="machine")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["state"], "ready")
        self.assertEqual(self.sent(), [("Bearer " + tok["g"], "udsyn-acct-M", "udsyn-region-G")])
        # verifier's token (and its short residency) was never used for anything
        self.assertNotIn("Bearer " + tok["v"], [s[0] for s in self.sent()])

    def test_bearer_and_routing_always_come_from_the_same_token(self):
        tok = self.merged_fixture()
        self.seed({"id": "w1", "access_token": tok["w"]})
        self.codex(view="machine")
        from plugins.usage_deck.adapters.cloud import codex_routing
        for bearer, account, residency in self.sent():
            routing = codex_routing(bearer.split(" ", 1)[1])
            self.assertEqual((account, residency), (routing.account_id, routing.residency))

    def test_every_members_private_routing_values_stay_masked_including_short_ones(self):
        tok = self.merged_fixture()
        self.seed({"id": "w1", "access_token": tok["w"]})
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        # make every private value show up as free text: the plan claim carries them
        svc = self.make()
        status, envelope = svc.handle("subscriptions", [("view", "machine")])
        self.assertEqual(status, 200)
        _h, health = svc.handle("health", [("view", "machine")])
        needles = ["udsyn-acct-M", "udsyn-region-G", "udsyn-region-W", *tok.values(),
                   *[t.split(".")[1] for t in tok.values()], *[t.split(".")[2] for t in tok.values() if t.split(".")[2]]]
        for name, obj in (("subscriptions", envelope), ("health", health), ("cache", repr(svc._cache)),
                          ("logs", [r.getMessage() + repr(r.args) for r in records])):
            self.assertEqual(scan_for(obj, needles), [], name)
        # the short value belongs to the masking context of the merged item even though its holder is machine-only
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        (item,) = [i for i in discover_machine(src) if i.provider_id == "openai-codex"]
        self.assertTrue({"zq", "udsyn-region-G", "udsyn-region-W", "udsyn-acct-M"} <= item.secrets)

    def test_profile_restricted_principal_still_gets_only_the_profile_view_and_one_request(self):
        tok = self.merged_fixture()
        self.seed({"id": "w1", "access_token": tok["w"]})
        status, body = self.make().handle("subscriptions", [("view", "machine")], profile_only=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["view"], "profile")
        self.assertEqual(self.sent(), [("Bearer " + tok["w"], "udsyn-acct-M", "udsyn-region-W")])


if __name__ == "__main__":
    unittest.main()
