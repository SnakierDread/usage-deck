"""Expiry-proof Anthropic meter (DESIGN.md §9.1.1 credential lifecycle, §9.1.2 taxonomy; card t_d3fe7377).

Incident 2026-11-09: the stored access token expired, nothing else rotated it (no direct Anthropic traffic on
that topology), the plugin kept sending the dead bearer. The pull now resolves its bearer through Hermes' own
credential pool: an expiring row is refreshed by the call ``hermes auth refresh`` makes; a dead grant is a
rejected card with the re-auth hint. Tokens are invented; the transport and the pool are injected.
"""

import inspect
import json
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

from plugins.usage_deck.adapters import create_adapter
from plugins.usage_deck.adapters.cloud import ANTHROPIC_USAGE_URL, AnthropicAdapter
from plugins.usage_deck.adapters.credentials import (
    EXPIRY_SKEW_MS,
    OK,
    REAUTH,
    UNAVAILABLE,
    CoreTokenResolver,
    StoredRow,
    TokenResolution,
)
from plugins.usage_deck.adapters.base import SecretValue
from plugins.usage_deck.models import dto

from .helpers import NOW, json_response, make_ctx, raw_response
from .test_anthropic_live import ROW, TOKEN, oauth_row, pool, routed, usage_body
from .test_schema_preservation import SchemaAsserts
from .test_service import ServiceBase

NEW_TOKEN = "sk-ant...en-REFRESHED"
HINT = "stored login needs re-auth: run hermes auth add anthropic"
EXPIRED_MS = int((NOW - timedelta(hours=2)).timestamp() * 1000)  # the incident: expired well before the read
FRESH_MS = int((NOW + timedelta(hours=6)).timestamp() * 1000)
REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).with_name("core_refresh_probe.py")


class FakeEntry:
    def __init__(self, row_id=ROW, access=TOKEN, refresh="sntl-ref-anthropic-1", expires_ms=EXPIRED_MS,
                 status="ok", auth_type="oauth"):
        self.id, self.access_token, self.refresh_token = row_id, access, refresh
        self.expires_at_ms, self.last_status, self.auth_type = expires_ms, status, auth_type


class FakePool:
    """The slice of ``CredentialPool`` the resolver may touch: ``entries`` and ``try_refresh_matching``."""

    def __init__(self, entry=None, on_refresh=None):
        self.rows = [] if entry is None else [entry]
        self.on_refresh = on_refresh
        self.refresh_calls = []
        self.entry_reads = 0

    def entries(self):
        self.entry_reads += 1
        return list(self.rows)

    def try_refresh_matching(self, api_key_hint=None, credential_id=None):
        self.refresh_calls.append(credential_id)
        return self.on_refresh(self) if self.on_refresh else None


def rotate(pool_):
    entry = pool_.rows[0]
    entry.access_token, entry.expires_at_ms, entry.last_status = NEW_TOKEN, FRESH_MS, "ok"
    return entry


def dead(pool_):
    pool_.rows[0].last_status = "dead"  # what core does after a terminal refresh error
    return None


def stored(token=TOKEN, expires_ms=None, row_id=ROW, may_refresh=True):
    return StoredRow(SecretValue(token), 0.0 if expires_ms is None else expires_ms / 1000.0, row_id, may_refresh)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ResolverContract(unittest.TestCase):
    def resolver(self, pool_, **kw):
        return CoreTokenResolver(pool_loader=lambda: pool_, **kw)

    def test_a_fresh_row_is_used_as_core_sees_it_with_no_refresh(self):
        p = FakePool(FakeEntry(access="sk-ant...en-PEER", expires_ms=FRESH_MS))  # a peer already rotated it
        result = self.resolver(p).resolve(stored(expires_ms=FRESH_MS), NOW)
        self.assertEqual((result.status, result.token.reveal(), result.refreshed), (OK, "sk-ant...en-PEER", False))
        self.assertEqual(p.refresh_calls, [])

    def test_expired_but_refreshable_row_is_refreshed_through_the_pool_call_the_cli_makes(self):
        p = FakePool(FakeEntry(), on_refresh=rotate)
        result = self.resolver(p).resolve(stored(expires_ms=EXPIRED_MS), NOW)
        self.assertEqual((result.status, result.token.reveal(), result.refreshed), (OK, NEW_TOKEN, True))
        self.assertEqual(p.refresh_calls, [ROW])  # try_refresh_matching(credential_id=<the row>)

    def test_expiring_within_the_core_skew_counts_as_expiring(self):
        soon = int(NOW.timestamp() * 1000) + EXPIRY_SKEW_MS - 1000
        later = int(NOW.timestamp() * 1000) + EXPIRY_SKEW_MS + 60_000
        for expires, refreshes in ((soon, 1), (later, 0)):
            with self.subTest(expires=expires):
                p = FakePool(FakeEntry(expires_ms=expires), on_refresh=rotate)
                self.resolver(p).resolve(stored(expires_ms=expires), NOW)
                self.assertEqual(len(p.refresh_calls), refreshes)

    def test_core_view_beats_a_stale_discovery_snapshot(self):
        p = FakePool(FakeEntry(access=NEW_TOKEN, expires_ms=FRESH_MS))
        result = self.resolver(p).resolve(stored(expires_ms=EXPIRED_MS), NOW)  # discovery read the old expiry
        self.assertEqual((result.status, result.token.reveal()), (OK, NEW_TOKEN))
        self.assertEqual(p.refresh_calls, [])

    def test_a_dead_grant_is_reauth(self):
        for name, pool_ in (("refresh kills the row", FakePool(FakeEntry(), on_refresh=dead)),
                            ("row already dead", FakePool(FakeEntry(status="dead"))),
                            ("row dropped by core", FakePool(FakeEntry(), on_refresh=lambda p: p.rows.clear())),
                            ("expired, no refresh token", FakePool(FakeEntry(refresh=None)))):
            with self.subTest(name):
                result = self.resolver(pool_).resolve(stored(expires_ms=EXPIRED_MS), NOW)
                self.assertEqual((result.status, result.token), (REAUTH, None))

    def test_a_transient_refresh_failure_is_unavailable_never_reauth_and_backs_off(self):
        clock = Clock()
        p = FakePool(FakeEntry(status="exhausted"), on_refresh=lambda p_: None)  # benched, not dead
        resolver = self.resolver(p, monotonic=clock, backoff_seconds=60.0)
        row = stored(expires_ms=EXPIRED_MS)
        self.assertEqual(resolver.resolve(row, NOW).status, UNAVAILABLE)
        clock.t += 30
        self.assertEqual(resolver.resolve(row, NOW).status, UNAVAILABLE)
        self.assertEqual(len(p.refresh_calls), 1)  # inside the backoff: the token endpoint is not hammered
        clock.t += 31
        resolver.resolve(row, NOW)
        self.assertEqual(len(p.refresh_calls), 2)

    def test_a_raising_pool_is_unavailable_not_a_crash(self):
        def boom(_p):
            raise RuntimeError("socket closed")

        result = self.resolver(FakePool(FakeEntry(), on_refresh=boom)).resolve(stored(expires_ms=EXPIRED_MS), NOW)
        self.assertEqual(result.status, UNAVAILABLE)

    def test_a_sibling_profiles_row_is_never_refreshed_and_an_expired_one_is_never_sent(self):
        p = FakePool(FakeEntry(), on_refresh=rotate)
        resolver = self.resolver(p)
        expired = resolver.resolve(stored(expires_ms=EXPIRED_MS, may_refresh=False), NOW)
        self.assertEqual(expired.status, UNAVAILABLE)
        fresh = resolver.resolve(stored(expires_ms=FRESH_MS, may_refresh=False), NOW)
        self.assertEqual((fresh.status, fresh.token.reveal()), (OK, TOKEN))
        self.assertEqual((p.refresh_calls, p.entry_reads), ([], 0))  # the pool was not even opened

    def test_core_missing_degrades_to_the_stored_token_only_while_it_is_still_valid(self):
        def missing():
            raise ImportError("no agent package")

        resolver = CoreTokenResolver(pool_loader=missing)
        self.assertEqual(resolver.resolve(stored(expires_ms=FRESH_MS), NOW).status, OK)
        self.assertEqual(resolver.resolve(stored(expires_ms=EXPIRED_MS), NOW).status, UNAVAILABLE)
        self.assertEqual(resolver.resolve(stored(), NOW).status, OK)  # unknown expiry: core treats it as valid too

    def test_a_row_without_a_public_id_cannot_be_refreshed(self):
        p = FakePool(FakeEntry(), on_refresh=rotate)
        result = self.resolver(p).resolve(stored(expires_ms=EXPIRED_MS, row_id=None), NOW)
        self.assertEqual((result.status, p.refresh_calls), (UNAVAILABLE, []))


class RealCorePool(unittest.TestCase):
    """The resolver against Hermes' REAL credential pool on a throwaway auth.json (network call replaced)."""

    def run_probe(self, scenario):
        try:
            import agent.credential_pool  # noqa: F401  # type: ignore
        except ImportError:
            self.skipTest("hermes core is not importable here")
        done = subprocess.run([sys.executable, str(PROBE), str(REPO_ROOT), scenario], capture_output=True,
                              text=True, timeout=120, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_expired_token_is_rotated_and_persisted_exactly_as_core_does(self):
        out = self.run_probe("rotate")
        self.assertEqual((out["status"], out["refreshed"], out["token"]), (OK, True, "NEW-ACCESS"))
        self.assertEqual(out["posts"], ["OLD-REFRESH"])  # one rotation, with the stored refresh token
        self.assertEqual((out["stored_access"], out["stored_refresh"]), ("NEW-ACCESS", "NEW-REFRESH"))

    def test_a_terminally_rejected_grant_is_reauth_and_core_marks_the_row_dead(self):
        out = self.run_probe("dead")
        self.assertEqual((out["status"], out["token"], out["stored_status"]), (REAUTH, None, "dead"))

    def test_a_transient_failure_is_unavailable_not_reauth(self):
        out = self.run_probe("transient")
        self.assertEqual((out["status"], out["token"]), (UNAVAILABLE, None))
        self.assertNotEqual(out["stored_status"], "dead")


class StubResolver:
    def __init__(self, resolution):
        self.resolution, self.rows = resolution, []

    def resolve(self, row, now):
        self.rows.append(row)
        return self.resolution


class NoRawTokenReads(unittest.TestCase):
    def test_the_pull_path_never_reveals_the_stored_credential(self):
        source = "".join(inspect.getsource(getattr(AnthropicAdapter, name))
                         for name in ("_pull", "_bearer", "_resolve", "_can_pull", "_do_usage", "_do_probe"))
        self.assertNotIn("ctx.credential.reveal", source)
        self.assertNotIn("credential.reveal", source)
        self.assertEqual(source.count(".reveal("), 1)  # the resolved bearer, revealed only at the header

    def test_the_header_carries_the_resolvers_token_not_the_stored_one(self):
        transport = routed(json_response(usage_body()))
        ctx = make_ctx("anthropic", credential=TOKEN, transport=transport, oauth_credential=True,
                       token_resolver=StubResolver(TokenResolution(OK, SecretValue(NEW_TOKEN), refreshed=True)))
        usage = create_adapter(ctx).get_usage(30)
        self.assertEqual(len(usage.meters), 2)
        (request,) = transport.requests
        self.assertEqual(request[1]["Authorization"], "Bearer " + NEW_TOKEN)
        self.assertNotIn(TOKEN, json.dumps(request[1]))

    def test_without_a_resolver_there_is_no_request_at_all(self):
        transport = routed(json_response(usage_body()))
        ctx = make_ctx("anthropic", credential=TOKEN, transport=transport, oauth_credential=True)
        usage = create_adapter(ctx).get_usage(30)
        self.assertEqual((transport.requests, usage.meters), ([], ()))

    def test_a_crashing_resolver_degrades_to_unavailable(self):
        class Boom:
            def resolve(self, row, now):
                raise RuntimeError("x")

        transport = routed(json_response(usage_body()))
        ctx = make_ctx("anthropic", credential=TOKEN, transport=transport, oauth_credential=True,
                       token_resolver=Boom())
        usage = create_adapter(ctx).get_usage(30)
        self.assertEqual((transport.requests, usage.error.code), ([], "upstream_unavailable"))

    def test_the_resolver_gets_the_row_inputs_and_may_not_refresh_a_cross_profile_credential(self):
        stub = StubResolver(TokenResolution(OK, SecretValue(TOKEN)))
        for cross, may in ((False, True), (True, False)):
            with self.subTest(cross=cross):
                ctx = make_ctx("anthropic", credential=TOKEN, transport=routed(json_response(usage_body())),
                               oauth_credential=True, cross_profile=cross, token_resolver=stub,
                               credential_row_id=ROW, credential_expires_at=123.0)
                create_adapter(ctx).get_usage(30)
                row = stub.rows[-1]
                self.assertEqual((row.row_id, row.expires_at, row.may_refresh), (ROW, 123.0, may))


class ExpiredTokenThroughTheService(ServiceBase, SchemaAsserts):
    """Full card path: auth.json row -> discovery -> service -> adapter -> resolver -> transport."""

    def setUp(self):
        super().setUp()
        self.transport.routes[ANTHROPIC_USAGE_URL] = json_response(usage_body())

    def card(self, pool_, view="machine", row=None, resolver=None):
        self.s.write_auth(pool(row or oauth_row(ROW, expires_at_ms=EXPIRED_MS)))
        self.transport.requests.clear()
        svc = self.make(token_resolver=resolver or CoreTokenResolver(pool_loader=lambda: pool_))
        status, body = svc.handle("subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        (card,) = [c for c in body["data"]["cards"] if c["provider_id"] == "anthropic"]
        return card, body

    def test_expired_but_refreshable_token_is_refreshed_then_pulls_with_the_new_bearer(self):
        p = FakePool(FakeEntry(), on_refresh=rotate)
        for view in ("machine", "profile"):
            with self.subTest(view=view):
                p.rows[0].access_token, p.rows[0].expires_at_ms = TOKEN, EXPIRED_MS
                p.refresh_calls.clear()
                card, body = self.card(p, view=view)
                self.assertEqual((card["state"], card["auth_status"], card["error"]), ("ready", "available", None))
                self.assertEqual([m["source"] for m in card["meters"]], ["anthropic_oauth_usage"] * 2)
                self.assertEqual(p.refresh_calls, [ROW])
                (request,) = self.transport.requests
                self.assertEqual(request[1]["Authorization"], "Bearer " + NEW_TOKEN)  # never the expired one
                self.assertNotIn(NEW_TOKEN, json.dumps(body))
                self.assertNotIn(TOKEN, json.dumps(body))

    def test_a_dead_grant_is_a_rejected_card_with_the_reauth_hint_and_no_request(self):
        card, _ = self.card(FakePool(FakeEntry(), on_refresh=dead))
        self.assertEqual((card["state"], card["auth_status"], card["meters"]), ("error", "rejected", []))
        self.assertEqual((card["error"]["code"], card["error"]["message"], card["error"]["retryable"]),
                         ("auth_required", HINT, False))
        self.assertEqual(self.transport.requests, [])  # a dead bearer is never replayed at the endpoint

    def test_a_dead_grant_gets_no_header_fallback(self):
        card, _ = self.card(FakePool(FakeEntry(status="dead")))
        self.assertEqual((card["state"], card["auth_status"]), ("error", "rejected"))

    def test_a_transient_refresh_failure_is_retry_later_not_rejected(self):
        card, _ = self.card(FakePool(FakeEntry(status="exhausted"), on_refresh=lambda p: None))
        self.assertEqual((card["state"], card["auth_status"], card["reason"]), ("error", "available", "source_unavailable"))
        self.assertEqual((card["error"]["code"], card["error"]["retryable"]), ("upstream_unavailable", True))
        self.assertEqual(self.transport.requests, [])

    def test_a_valid_token_is_resolved_through_core_without_a_refresh(self):
        p = FakePool(FakeEntry(expires_ms=FRESH_MS))
        card, _ = self.card(p, row=oauth_row(ROW, expires_at_ms=FRESH_MS))
        self.assertEqual(card["state"], "ready")
        self.assertEqual((p.refresh_calls, self.transport.requests[0][1]["Authorization"]), ([], "Bearer " + TOKEN))

    def test_the_services_resolver_is_shared_across_snapshots(self):
        p = FakePool(FakeEntry(status="exhausted"), on_refresh=lambda p_: None)
        self.s.write_auth(pool(oauth_row(ROW, expires_at_ms=EXPIRED_MS)))
        svc = self.make(token_resolver=CoreTokenResolver(pool_loader=lambda: p), cache_ttl=0.0)
        svc.handle("subscriptions", [("view", "machine")])
        svc.handle("subscriptions", [("view", "machine")])
        self.assertEqual(len(p.refresh_calls), 1)  # the failure backoff survives the 30 s snapshot boundary


class StatusTaxonomy(ServiceBase, SchemaAsserts):
    """401 / 404 / 429 are distinguishable on the card; none is an estimate; only 401 is `rejected`."""

    def card(self, outcome):
        self.s.write_auth(pool(oauth_row(ROW)))
        self.transport.routes[ANTHROPIC_USAGE_URL] = outcome
        status, body = self.make(token_resolver=StubResolver(TokenResolution(OK, SecretValue(TOKEN)))).handle(
            "subscriptions", [("view", "machine")])
        self.assert_exact_envelope("subscriptions", status, body)
        (card,) = [c for c in body["data"]["cards"] if c["provider_id"] == "anthropic"]
        return card

    def test_401_404_429_each_get_their_own_card_error(self):
        cards = {status: self.card(raw_response("{}", status=status)) for status in (401, 404, 429)}
        for status, card in cards.items():
            self.assertEqual((card["state"], card["meters"]), ("error", []), status)
        self.assertEqual(len({c["error"]["message"] for c in cards.values()}), 3)  # distinguishable
        self.assertEqual([(c["error"]["code"], c["auth_status"]) for c in cards.values()],
                         [("auth_required", "rejected"), ("permission_denied", "available"),
                          ("upstream_unavailable", "available")])
        for status, card in cards.items():
            self.assertIn("HTTP %d" % status, card["error"]["message"])

    def test_401_carries_the_reauth_hint(self):
        self.assertIn(HINT, self.card(raw_response("{}", status=401))["error"]["message"])

    def test_429_is_source_unavailable_retry_later_and_not_rejected(self):
        card = self.card(raw_response("{}", status=429))
        self.assertEqual((card["reason"], card["auth_status"], card["error"]["retryable"]),
                         ("source_unavailable", "available", True))
        self.assertIn("retry later", card["error"]["message"])

    def test_404_is_a_credential_or_endpoint_error_never_an_estimate(self):
        card = self.card(raw_response("{}", status=404))
        self.assertEqual((card["reason"], card["error"]["code"], card["error"]["retryable"]),
                         ("permission_denied", "permission_denied", False))
        self.assertEqual(card["meters"], [])
        self.assertIn("no reading is estimated", card["error"]["message"])

    def test_variant_messages_are_closed_wire_vocabulary(self):
        for key, (code, message, retryable) in dto.ERROR_VARIANTS.items():
            with self.subTest(key):
                self.assertIn(code, dto.ERROR_CODES)
                self.assertTrue(dto.preserves_wire_value("message", message))
                err = dto.make_error(code, variant=key)
                self.assertEqual((err.code, err.message, err.retryable), (code, message, retryable))
        with self.assertRaises(dto.DtoError):
            dto.make_error("auth_required", variant="nope")
        self.assertFalse(dto.preserves_wire_value("message", "Anthropic usage answered HTTP 418; invented"))


if __name__ == "__main__":
    unittest.main()
