"""Phantom surfaces (card t_0acea075): credential-slot abstractions are not accounts, an unreachable self-hosted box
is a quiet opaque state, and one endpoint is one card with one resolved name.

C1  an account is listed ONLY when a credential resolves for it (empty pool list / orphan provider entry skipped
    and only counted in ``debug.skipped_slots``);
C2  an unreachable box renders opaque ``box_offline`` + last seen, never a red error card;
C3  ``custom`` resolves to the ``custom_providers`` entry that names its base_url, across root + profile configs;
C3b/C5b  the custom spellings of one normalized base_url are ONE card; model rows come from sessions alone.
"""

import socket
import unittest
from pathlib import Path

from plugins.usage_deck.adapters.transport import UrllibTransport
from plugins.usage_deck.discovery import (
    FileInventorySource,
    configured_label,
    discover,
    discover_detailed,
    discover_machine,
    discover_machine_detailed,
    endpoint_names,
    parse_endpoint,
    provider_label_for,
)
from plugins.usage_deck.service import UsageDeckService

from .helpers import NOW, FakeAnalytics, FakeTransport, json_response, raw_response
from .test_analytics_dimensions import DirectSqlAnalytics, Machine, S
from .test_codex_wham import codex_token
from .test_machine_view import MachineBase, pool
from .test_service import METRICS, ServiceBase

PGX = "http://pgx.lan:8000"


def endpoint_label(provider_id, base_url, entries):
    """Label the inventory gives ``provider_id`` at ``base_url`` when ``entries`` are the configured endpoints."""
    return provider_label_for(provider_id, frozenset(), parse_endpoint(base_url), endpoint_names(entries))


class LabelResolutionTable(unittest.TestCase):
    """C3: provider labels resolve custom -> named custom_providers entry by normalized base_url."""

    ENTRIES = [
        {"name": "PGX", "base_url": "http://pgx.lan:8000/v1"},
        {"name": "custom:Desktop", "base_url": "HTTP://Desktop.LAN:8080/v1/"},
        {"name": "Mac", "base_url": "https://llama-mac.example.net"},
        {"provider_key": "keyed", "base_url": "http://keyed.lan:9000/v1"},
        {"name": "Leaky", "base_url": "http://user:pw@leaky.lan:7000/v1"},
        {"name": "NoUrl"},
    ]

    def test_table(self):
        table = [
            # (provider id, endpoint base_url, expected label)
            ("custom", "http://pgx.lan:8000/v1", "PGX"),                    # bare custom -> the named entry
            ("custom", "HTTP://PGX.LAN:8000/v1/", "PGX"),                   # normalized: scheme/host case, trailing slash
            ("custom:pgx", "http://pgx.lan:8000/v1/", "PGX"),               # the session spelling, configured name wins
            ("custom:PGX", "http://pgx.lan:8000/v1", "PGX"),
            ("custom", "http://desktop.lan:8080/v1", "Desktop"),            # a ``custom:`` name prefix is stripped
            ("custom", "https://llama-mac.example.net/v1", "Custom endpoint · llama-mac.example.net"),  # scheme/port differ
            ("custom", "https://llama-mac.example.net", "Mac"),
            ("custom", "http://keyed.lan:9000/v1", "keyed"),                # provider_key names an entry too
            ("custom", "http://other.lan:1234/v1", "Custom endpoint · other.lan:1234"),   # unresolved keeps host:port
            ("custom", "http://other.lan/v1", "Custom endpoint · other.lan"),
            ("custom:unnamed", "http://other.lan:1234/v1", "unnamed"),      # an explicit name is never replaced by a url
            ("custom", "http://leaky.lan:7000/v1", "Custom endpoint · leaky.lan:7000"),  # unsafe entry names nothing
            ("openrouter", "http://pgx.lan:8000/v1", "OpenRouter"),         # other providers keep their labels
            ("anthropic", "http://pgx.lan:8000/v1", "Anthropic"),
        ]
        for provider_id, base_url, expected in table:
            with self.subTest(provider_id=provider_id, base_url=base_url):
                self.assertEqual(endpoint_label(provider_id, base_url, self.ENTRIES), expected)

    def test_without_an_endpoint_the_literal_label_stays(self):
        self.assertEqual(provider_label_for("custom", frozenset()), "Custom endpoint")
        self.assertEqual(provider_label_for("custom:pgx", frozenset()), "pgx")

    def test_userinfo_never_reaches_a_label(self):
        label = endpoint_label("custom", "http://user:pw@host.lan:80/v1", [])
        self.assertEqual(label, "Custom endpoint")  # unsafe endpoint: no suffix at all
        self.assertNotIn("pw", label)

    def test_first_configured_name_wins_and_viewed_config_comes_first(self):
        names = endpoint_names([{"name": "A", "base_url": "http://x.lan:1/v1"}],
                               [{"name": "B", "base_url": "http://x.lan:1/v1"}])
        self.assertEqual(names, {"http://x.lan:1/v1": "A"})

    def test_configured_label_matches_custom_ids_case_insensitively(self):
        names = endpoint_names(self.ENTRIES)
        self.assertEqual(configured_label("custom:pgx", names), "PGX")
        self.assertEqual(configured_label("custom:DESKTOP", names), "Desktop")
        self.assertIsNone(configured_label("custom:nobody", names))
        self.assertIsNone(configured_label("custom", names))       # the bare/roll-up id is not a name
        self.assertIsNone(configured_label("openrouter", names))


class MachineLabels(MachineBase):
    """C3 across homes: root config + profile configs name the endpoint; the viewed profile names nothing."""

    def source(self, root_cfg=None, profile_cfgs=None):
        configs = {"": root_cfg, **(profile_cfgs or {})}

        def home_config(directory):
            name = Path(directory).name
            return configs.get("" if name == self.s.root.name else name)

        return FileInventorySource(
            self.s.home, self.s.root, config_loader=lambda: self.config,
            custom_provider_loader=lambda cfg: cfg.get("custom_providers", []) if cfg is not self.config else self.custom,
            env_lookup=lambda n: None, home_config_loader=home_config)

    def test_root_and_profile_configs_name_a_bare_custom_primary(self):
        # the viewed profile's primary is the bare ``custom`` provider at PGX; only the ROOT config names it
        self.config = {"model": {"provider": "custom", "base_url": PGX + "/v1"}}
        root = {"custom_providers": [{"name": "PGX", "base_url": PGX + "/v1"}]}
        (item,) = [i for i in discover_machine(self.source(root)) if i.provider_id.lower() in ("custom", "custom:pgx")]
        # the bare primary and the root's named entry are one endpoint: one card under the configured name
        self.assertEqual(item.provider_label, "PGX")
        # a profile config names a different url; the root one is not needed for it
        self.config = {"model": {"provider": "custom", "base_url": "http://lab.lan:9/v1"}}
        prof = {"verifier": {"custom_providers": [{"name": "Lab", "base_url": "http://lab.lan:9/v1"}]}}
        self.s.write_profile("verifier", {"credential_pool": {}})
        labels = {i.provider_label for i in discover_machine(self.source(None, prof))
                  if i.endpoint is not None and i.endpoint.normalized == "http://lab.lan:9/v1"}
        self.assertEqual(labels, {"Lab"})  # one card for the endpoint, named by the OTHER profile's config

    def test_unresolved_custom_keeps_host_port_suffix(self):
        self.config = {"model": {"provider": "custom", "base_url": "http://mystery.lan:4321/v1"}}
        (item,) = [i for i in discover_machine(self.source({"custom_providers": [{"name": "PGX", "base_url": PGX + "/v1"}]}))
                   if i.provider_id == "custom"]
        self.assertEqual(item.provider_label, "Custom endpoint · mystery.lan:4321")

    def test_profile_view_never_reads_another_homes_config(self):
        self.config = {"model": {"provider": "custom", "base_url": PGX + "/v1"}}
        root = {"custom_providers": [{"name": "PGX", "base_url": PGX + "/v1"}]}
        (item,) = discover(self.source(root))  # profile view: the viewed config only (self.custom is empty)
        self.assertEqual(item.provider_label, "Custom endpoint · pgx.lan:8000")


class EnumerationFilters(MachineBase):
    """C1: only a credential that resolves lists an account; slots are counted, never rendered."""

    def test_empty_pool_orphan_provider_slot_and_real_credential(self):
        self.s.write_auth({
            "credential_pool": {"openai-codex": [], "nous": [{"id": "n1", "access_token": "tok-nous-1"}],
                                "openrouter": [{"id": "or1"}]},          # a row with no secret at all
            "providers": {"openai-codex": {"token_type": "x"}, "xai": {}}})   # orphan provider-level slots
        items, skipped = discover_detailed(self.source())
        self.assertEqual([i.provider_id for i in items], ["nous"])
        # codex: empty pool + orphan slot; openrouter: token-less row; xai: orphan. Counted, never listed.
        self.assertEqual(skipped, {"openai-codex": 2, "openrouter": 1, "xai": 1})

    def test_real_credentials_of_every_shape_are_listed(self):
        self.s.write_auth({
            "credential_pool": {"nous": [{"id": "n1", "access_token": "tok-nous-1"}],
                                "anthropic": [{"id": "a1", "refresh_token": "refresh-only-1"}]},   # refreshable
            "providers": {"openai-codex": {"tokens": {"access_token": codex_token("A", "U", "go")}}}})
        items, skipped = discover_detailed(self.source())
        self.assertEqual(sorted(i.provider_id for i in items), ["anthropic", "nous", "openai-codex"])
        self.assertEqual(skipped, {})

    def test_orphan_provider_entry_next_to_a_populated_pool_is_not_a_second_account(self):
        self.s.write_auth({"credential_pool": {"nous": [{"id": "n1", "access_token": "tok-nous-1"}]},
                           "providers": {"nous": {}}})
        items, _skipped = discover_detailed(self.source())
        self.assertEqual(len(items), 1)

    def test_machine_view_skips_slots_of_every_home_and_counts_them(self):
        self.s.write_auth({"credential_pool": {"openai-codex": []}}, where="global")        # the ROOT slot
        self.s.write_profile("verifier", {"credential_pool": {"openai-codex": [{"id": "v0"}]},
                                          "providers": {"openai-codex": {}}})
        self.s.write_profile("architect", pool("nous", {"id": "n1", "access_token": "tok-nous-1"}))
        items, skipped = discover_machine_detailed(self.source())
        self.assertEqual([i.provider_id for i in items], ["nous"])
        # root empty pool + verifier's secret-less row. The verifier's orphan provider entry sits behind a
        # populated pool list (shadowed), so it is not a separate slot.
        self.assertEqual(skipped.get("openai-codex"), 2)
        self.assertNotIn("nous", skipped)

    def test_a_phantom_codex_slot_is_never_a_card_and_the_debug_count_rides_the_real_cards(self):
        self.s.write_auth({"credential_pool": {"openai-codex": [], "nous": [{"id": "n1", "access_token": "tok-1"}]},
                           "providers": {"openai-codex": {}}})
        svc = UsageDeckService(inventory_factory=self.source, analytics_factory=lambda p: FakeAnalytics([]),
                               transport=FakeTransport(), clock=lambda: NOW)
        for view in ("profile", "machine"):
            with self.subTest(view=view):
                _status, body = svc.handle("subscriptions", [("view", view)])
                cards = body["data"]["cards"]
                self.assertEqual([c["provider_id"] for c in cards], ["nous"])
        _status, body = svc.handle("subscriptions", [("view", "profile")])
        self.assertEqual(body["data"]["cards"][0]["debug"]["skipped_slots"], 0)  # nous itself has no skipped slot

    def test_skipped_slot_count_is_per_provider_debug_only(self):
        self.s.write_auth({"credential_pool": {"nous": [{"id": "n1", "access_token": "tok-1"},
                                                        {"id": "n2"}]}})  # a token-less sibling row of a real provider
        svc = UsageDeckService(inventory_factory=self.source, analytics_factory=lambda p: FakeAnalytics([]),
                               transport=FakeTransport(), clock=lambda: NOW)
        _status, body = svc.handle("subscriptions", [("view", "profile")])
        (card,) = body["data"]["cards"]
        self.assertEqual((card["provider_id"], card["debug"]["skipped_slots"]), ("nous", 1))


class OfflineBoxIsQuiet(ServiceBase):
    """C2: a powered-off on-demand machine is a normal condition."""

    def setUp(self):
        super().setUp()
        self.s.write_auth({"credential_pool": {}})
        self.custom = [{"name": "Desktop", "base_url": "http://desktop.lan:8080/v1"}]

    def card_and_health(self, svc):
        _s, subs = svc.handle("subscriptions", [("view", "profile")])
        _s, health = svc.handle("health", [])
        return subs, health

    def test_connection_refused_is_opaque_box_offline_without_an_error(self):
        subs, health = self.card_and_health(self.make())
        (card,) = subs["data"]["cards"]
        self.assertEqual((card["state"], card["confidence"], card["reason"], card["error"], card["meters"]),
                         ("opaque", "opaque", "box_offline", None, []))
        self.assertIsNone(card["last_seen_at"])  # never reached since this process started
        (entry,) = health["data"]["entries"]
        self.assertEqual((entry["status"], entry["reason"], entry["error"]), ("offline", "box_offline", None))
        for body in (subs, health):  # not a partial response, no meta error either
            self.assertFalse(body["meta"]["partial"])
            self.assertEqual(body["meta"]["errors"], [])

    def test_a_real_refused_connection_through_the_default_transport(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]  # closed again on exit: nothing listens, the connect is refused
        self.custom = [{"name": "Desktop", "base_url": f"http://127.0.0.1:{port}/v1"}]
        self.transport = UrllibTransport()
        subs, health = self.card_and_health(self.make())
        (card,) = subs["data"]["cards"]
        self.assertEqual((card["state"], card["reason"], card["error"]), ("opaque", "box_offline", None))
        self.assertEqual(health["data"]["entries"][0]["error"], None)

    def test_last_seen_survives_the_box_going_away(self):
        t = [1000.0]
        svc = self.make(monotonic=lambda: t[0])
        self.transport.routes.update({"http://desktop.lan:8080/metrics": raw_response(METRICS),
                                      "http://desktop.lan:8080/v1/models": json_response({"data": []})})
        subs, _h = self.card_and_health(svc)
        (online,) = subs["data"]["cards"]
        self.assertEqual(online["last_seen_at"], "2026-11-06T12:00:00Z")
        self.assertNotEqual(online["reason"], "box_offline")
        self.transport.routes.clear()  # powered off
        t[0] += 120  # past the 30 s snapshot cache
        subs, health = self.card_and_health(svc)
        (offline,) = subs["data"]["cards"]
        self.assertEqual((offline["state"], offline["reason"], offline["error"], offline["last_seen_at"]),
                         ("opaque", "box_offline", None, "2026-11-06T12:00:00Z"))
        self.assertEqual(health["data"]["entries"][0]["last_seen_at"], "2026-11-06T12:00:00Z")

    def test_cloud_cards_never_carry_a_last_seen(self):
        self.s.write_auth({"credential_pool": {"nous": [{"id": "n1", "access_token": "tok-1"}]}})
        subs, _h = self.card_and_health(self.make())
        self.assertTrue(all(c["last_seen_at"] is None for c in subs["data"]["cards"]))

    def test_a_reachable_box_with_a_broken_answer_is_still_an_error_not_offline(self):
        self.transport.routes.update({"http://desktop.lan:8080/metrics": raw_response("<html>", status=200),
                                      "http://desktop.lan:8080/v1/models": raw_response("<html>", status=200),
                                      "http://desktop.lan:8080/api/tags": raw_response("<html>", status=200)})
        subs, _h = self.card_and_health(self.make())
        (card,) = subs["data"]["cards"]
        self.assertNotEqual(card["reason"], "box_offline")  # only an unreachable box is quiet

    def test_cloud_upstream_failures_stay_errors(self):
        # C4: the offline rule is for self-hosted boxes only; a cloud provider that cannot be reached still errors
        from plugins.usage_deck.adapters.cloud import OPENROUTER_KEY_URL
        self.s.write_auth({"credential_pool": {"openrouter": [{"id": "or1", "access_token": "tok-or-1"}]}})
        self.custom = []
        subs, _h = self.card_and_health(self.make())  # no route: connection failure
        (card,) = subs["data"]["cards"]
        self.assertEqual((card["provider_id"], card["state"]), ("openrouter", "error"))
        self.assertEqual(card["error"]["code"], "upstream_unavailable")
        self.assertTrue(OPENROUTER_KEY_URL)


class OneEndpointOneCard(MachineBase):
    """C3b/C5b: custom spellings (bare, custom:<name>, config entry) of one normalized url are ONE card."""

    URL = PGX + "/v1"

    def listed(self, view):
        items = discover_machine(self.source()) if view == "machine" else discover(self.source())
        return [i for i in items if i.endpoint is not None]

    def test_named_entry_pool_row_and_bare_primary_collapse_to_the_config_name(self):
        self.custom = [{"name": "PGX", "base_url": self.URL}]
        self.config = {"model": {"provider": "custom", "base_url": self.URL}}
        self.s.write_auth(pool("custom:pgx", {"id": "p1", "access_token": "tok-pgx", "base_url": self.URL}))
        for view in ("profile", "machine"):
            with self.subTest(view=view):
                (item,) = self.listed(view)
                self.assertEqual(item.provider_label, "PGX")
                self.assertEqual(item.endpoint.normalized, self.URL)

    def test_config_entry_plus_bare_custom_without_a_pool_is_one_card(self):
        self.custom = [{"name": "PGX", "base_url": self.URL}]
        self.config = {"model": {"provider": "custom", "base_url": self.URL}}
        for view in ("profile", "machine"):
            with self.subTest(view=view):
                (item,) = self.listed(view)
                self.assertEqual(item.provider_label, "PGX")

    def test_url_spelling_aliases_are_one_endpoint(self):
        self.custom = [{"name": "PGX", "base_url": "http://PGX.lan:8000/v1/"},
                       {"name": "pgx-alias", "base_url": "http://pgx.lan:8000/v1"}]
        self.s.write_auth(pool("custom", {"id": "p1", "access_token": "tok-pgx", "base_url": "http://pgx.LAN:8000/v1"}))
        for view in ("profile", "machine"):
            with self.subTest(view=view):
                self.assertEqual(len(self.listed(view)), 1)

    def test_distinct_endpoints_stay_distinct(self):
        self.custom = [{"name": "PGX", "base_url": self.URL}, {"name": "Other", "base_url": "http://other.lan:8000/v1"}]
        self.config = {"model": {"provider": "custom", "base_url": "http://third.lan:8000/v1"}}
        for view in ("profile", "machine"):
            with self.subTest(view=view):
                labels = sorted(i.provider_label for i in self.listed(view))
                self.assertEqual(labels, ["Custom endpoint · third.lan:8000", "Other", "PGX"])

    def test_two_pooled_keys_of_one_endpoint_stay_two_accounts(self):
        self.custom = [{"name": "PGX", "base_url": self.URL}]
        self.s.write_auth(pool("custom:pgx", {"id": "p1", "access_token": "tok-1", "base_url": self.URL},
                               {"id": "p2", "access_token": "tok-2", "base_url": self.URL}))
        self.assertEqual(len(self.listed("profile")), 2)  # accounts of the box are not collapsed, spellings are


class SessionObservedModelRows(unittest.TestCase):
    """C4b/C5b: the model catalog is session observation; an endpoint probe is enrichment only."""

    def setUp(self):
        self.m = Machine(self)
        sessions = [
            S("a", 1, "qwen-a", None, "custom", 10, 1, 0.0, 1, "unknown"),
            S("b", 2, "qwen-b", None, "custom:pgx", 20, 2, 0.0, 1, "custom:pgx"),
            S("c", 3, "qwen-c", None, "custom:pgx", 30, 3, 0.0, 1, "custom:pgx"),
            S("d", 4, "qwen-a", None, "custom:PGX", 40, 4, 0.0, 1, "custom:PGX"),
        ]
        self.m.write_db(None, sessions)

    def service(self, routes, custom):
        src = FileInventorySource(self.m.root, self.m.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda cfg: custom, env_lookup=lambda n: None)

        def analytics(profile):
            return DirectSqlAnalytics(self.m.root)

        return UsageDeckService(inventory_factory=lambda: src, analytics_factory=analytics,
                                transport=FakeTransport(routes), clock=lambda: NOW)

    def usage(self, svc, params):
        status, body = svc.handle("usage", params)
        self.assertEqual(status, 200, body)
        return body["data"]

    def test_model_rows_do_not_depend_on_the_probe(self):
        custom = [{"name": "PGX", "base_url": PGX + "/v1"}]
        online = {PGX + "/metrics": raw_response(METRICS),
                  PGX + "/v1/models": json_response({"data": [{"id": "qwen-a"}, {"id": "an-unloaded-model"}]})}
        params = [("mode", "profile"), ("dimension", "model")]
        up = self.usage(self.service(online, custom), params)
        down = self.usage(self.service({}, custom), params)  # nothing answers: box offline
        self.assertEqual(up["groups"], down["groups"])
        self.assertEqual(up["rows"], down["rows"])
        # three observed ids = three model rows; a probe-listed model nobody ran adds none, none disappears
        self.assertEqual(sorted(g["model_id"] for g in up["groups"] if g["model_id"]), ["qwen-a", "qwen-b", "qwen-c"])
        self.assertNotIn("an-unloaded-model", str(up))

    def test_the_usage_provider_option_resolves_the_configured_name(self):
        custom = [{"name": "PGX", "base_url": PGX + "/v1"}]
        data = self.usage(self.service({}, custom), [("mode", "profile"), ("dimension", "all")])
        labels = {o["id"]: o["label"] for o in data["providers"]}
        self.assertEqual(labels["custom:pgx"], "PGX")      # the session spelling takes the configured name
        self.assertEqual(labels["custom:PGX"], "PGX")
        self.assertEqual(labels["custom"], "Custom endpoint")   # the roll-up option keeps its generic name
        # without a configured entry the session spelling keeps its own name
        bare = self.usage(self.service({}, []), [("mode", "profile"), ("dimension", "all")])
        self.assertEqual({o["id"]: o["label"] for o in bare["providers"]}["custom:pgx"], "pgx")


if __name__ == "__main__":
    unittest.main()
