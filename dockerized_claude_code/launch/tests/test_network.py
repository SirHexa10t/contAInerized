"""Tests for launch.network — the {auto}-mode firewall's host-side logic.

Everything here runs without DNS, docker, or threads-under-test: the pure
transformations (_expand_whitelist, _cdn_provider_ranges, _tokens_for,
_iptables_rules_for, _index_by_host) are exercised directly; the cascade and
the refresher pass get a fake resolver; the updater worker is driven
synchronously with a pre-filled queue; the status tracker writes into a tmp
dir. Module-level state the functions share (_seen_cdn_ranges,
_resolution_cache, _fresh_resolutions, _emitted_tokens, _all_entries_by_host,
_phase2_queue) is reset around each test that touches it."""

import contextlib
import io
import ipaddress
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from launch import network
from launch.network import (
    HostnameEntry, _cascade, _cdn_provider_ranges, _clean_cidrs,
    _expand_whitelist, _index_by_host, _iptables_rules_for, _is_ipv6_literal,
    _subtract_networks, _tokens_for,
)
from launch.template_code.firewall_domains import BUILTIN_FIREWALL_DOMAINS

# Provider ranges are fetched at launch, never baked — tests seed this stand-in
# table via network._set_provider_blocks so widening policy is exercised
# against known data. Addresses below sit inside these seeded blocks.
_TEST_PROVIDER_BLOCKS = {
    "cloudflare": ["104.16.0.0/13", "172.64.0.0/13", "198.41.128.0/17"],
    "fastly": ["151.101.0.0/16", "199.232.0.0/16"],
    "multi": ["192.0.2.0/24", "198.18.0.0/15", "203.0.112.0/24"],
}
_CLOUDFLARE_IP = "104.16.1.1"        # inside seeded cloudflare 104.16.0.0/13
_FASTLY_IP = "151.101.1.1"           # inside seeded fastly 151.101.0.0/16
_MULTI_IP = "198.18.5.5"             # inside seeded multi 198.18.0.0/15
_NON_CDN_IP = "203.0.113.7"          # TEST-NET-3 — in no seeded block


class TestExpandWhitelist(unittest.TestCase):
    """_expand_whitelist is the security-relevant entry transformation:
    IPv6 skip, dedupe, wildcard flagging, apex-from-www, literal/hostname
    split."""

    def test_hostname_entry_shape(self):
        result = _expand_whitelist(["foo.com:8443"])
        self.assertEqual(result.hostnames,
                         [HostnameEntry(entry="foo.com:8443", host="foo.com", port="8443", wildcard=False)])

    def test_www_entry_also_allows_apex(self):
        result = _expand_whitelist(["www.foo.com"])
        self.assertEqual({h.host for h in result.hostnames}, {"www.foo.com", "foo.com"})

    def test_bare_apex_does_not_add_www(self):
        # The convenience is one-directional — apex entries stay apex-only.
        result = _expand_whitelist(["foo.com"])
        self.assertEqual({h.host for h in result.hostnames}, {"foo.com"})

    def test_wildcard_kept_with_stripped_host(self):
        # `*.` is no longer discarded: the base host resolves, and the flag
        # asks _tokens_for for whole-provider widening.
        result = _expand_whitelist(["*.foo.com"])
        self.assertEqual(result.hostnames,
                         [HostnameEntry(entry="*.foo.com", host="foo.com", port="", wildcard=True)])

    def test_wildcard_port_preserved(self):
        result = _expand_whitelist(["*.foo.com:8443"])
        self.assertEqual(result.hostnames,
                         [HostnameEntry(entry="*.foo.com:8443", host="foo.com", port="8443", wildcard=True)])

    def test_wildcard_wins_over_plain_duplicate(self):
        # Same (host, port) with and without `*.` collapses to the wildcard —
        # it's the superset grant.
        result = _expand_whitelist(["foo.com", "foo.com", "*.foo.com"])
        self.assertEqual(len(result.hostnames), 1)
        self.assertTrue(result.hostnames[0].wildcard)

    def test_ipv6_literals_skipped_with_reason(self):
        # Pasted v6 addresses used to burn the full DNS cascade and land in
        # failed: — now they're set aside with an actionable reason.
        result = _expand_whitelist(["2a00:1450:400c:c07::", "2a04:4e42::/32", "foo.com"])
        self.assertEqual([h.host for h in result.hostnames], ["foo.com"])
        self.assertEqual(
            {entry for entry, _ in result.skipped},
            {"2a00:1450:400c:c07::", "2a04:4e42::/32"},
        )
        for _entry, reason in result.skipped:
            self.assertEqual(reason, network._SKIPPED_IPV6_REASON)

    def test_literal_ip_and_cidr_split_out(self):
        result = _expand_whitelist(["1.2.3.4", "10.0.0.0/8:443", "foo.com"])
        self.assertEqual(result.literals, ["1.2.3.4", "10.0.0.0/8:443"])
        self.assertEqual([h.host for h in result.hostnames], ["foo.com"])

    def test_port_preserved_on_hostname(self):
        result = _expand_whitelist(["foo.com:8080"])
        self.assertEqual(result.hostnames[0].port, "8080")

    def test_output_sorted_deterministically(self):
        first = _expand_whitelist(["b.com", "a.com", "c.com"]).hostnames
        second = _expand_whitelist(["c.com", "a.com", "b.com"]).hostnames
        self.assertEqual(first, second)
        self.assertEqual([h.host for h in first], ["a.com", "b.com", "c.com"])

    def test_builtin_list_expands_cleanly(self):
        # The shipped domain list must all classify as hostnames (no stray
        # literal IPs or v6 hiding in it) and produce a non-trivial pending set.
        result = _expand_whitelist(BUILTIN_FIREWALL_DOMAINS)
        self.assertEqual(result.literals, [])
        self.assertEqual(result.skipped, [])
        self.assertGreater(len(result.hostnames), len(BUILTIN_FIREWALL_DOMAINS))   # www-stripping adds apexes


class TestIsIpv6Literal(unittest.TestCase):
    def test_v6_address_and_cidr_detected(self):
        self.assertTrue(_is_ipv6_literal("2a00:1450:400c:c07::"))
        self.assertTrue(_is_ipv6_literal("2a04:4e42::/32"))

    def test_hostnames_and_v4_pass_through(self):
        for entry in ("foo.com", "www.foo.com:443", "1.2.3.4", "10.0.0.0/8", "*.foo.com"):
            self.assertFalse(_is_ipv6_literal(entry), entry)


class TestCleanCidrs(unittest.TestCase):
    """Fetched range lists are external input: only well-formed IPv4 CIDRs
    may survive into rule generation, in collapsed canonical form."""

    def test_garbage_and_v6_dropped(self):
        cleaned = _clean_cidrs(["104.16.0.0/13", "2606:4700::/32", "not-a-range", ""])
        self.assertEqual(cleaned, ["104.16.0.0/13"])

    def test_adjacent_blocks_collapse(self):
        self.assertEqual(_clean_cidrs(["10.0.0.0/25", "10.0.0.128/25"]), ["10.0.0.0/24"])

    def test_host_bits_normalized(self):
        # Lenient on sloppy published data: host bits are masked, not fatal.
        self.assertEqual(_clean_cidrs(["10.0.0.5/24"]), ["10.0.0.0/24"])


class TestSubtractNetworks(unittest.TestCase):
    """Netmask-aware difference — the 'provider's own services' computation
    for providers that publish all-space and rentable-space separately."""

    def test_removal_inside_base_splits_it(self):
        result = _subtract_networks(["10.0.0.0/8"], ["10.1.0.0/16"])
        self.assertNotIn("10.0.0.0/8", result)
        nets = [ipaddress.IPv4Network(c) for c in result]
        self.assertFalse(any(ipaddress.IPv4Address("10.1.2.3") in n for n in nets))
        self.assertTrue(any(ipaddress.IPv4Address("10.2.2.3") in n for n in nets))

    def test_removal_covering_base_erases_it(self):
        self.assertEqual(_subtract_networks(["10.5.0.0/16"], ["10.0.0.0/8"]), [])

    def test_equal_networks_cancel(self):
        self.assertEqual(_subtract_networks(["192.0.2.0/24"], ["192.0.2.0/24"]), [])

    def test_disjoint_removal_changes_nothing(self):
        self.assertEqual(_subtract_networks(["192.0.2.0/24"], ["198.51.100.0/24"]), ["192.0.2.0/24"])


class TestRangeParsers(unittest.TestCase):
    """Each provider fetcher's parsing, driven by canned payloads — no
    network involved (_http_get is patched per test)."""

    def _with_body(self, fetcher, body):
        with patch.object(network, "_http_get", return_value=body):
            return fetcher()

    def test_cloudflare_plain_text_lines(self):
        self.assertEqual(
            self._with_body(network._cloudflare_ranges, "104.16.0.0/13\n172.64.0.0/13\n"),
            ["104.16.0.0/13", "172.64.0.0/13"],
        )

    def test_fastly_addresses_key(self):
        body = '{"addresses": ["151.101.0.0/16"], "ipv6_addresses": ["2a04:4e40::/32"]}'
        self.assertEqual(self._with_body(network._fastly_ranges, body), ["151.101.0.0/16"])

    def test_github_meta_edge_services_v4_only(self):
        body = ('{"web": ["140.82.112.0/20", "2a0a:a440::/29"], "api": ["140.82.112.0/20"],'
                ' "git": ["192.30.252.0/22"], "packages": [], "pages": ["185.199.108.0/22"],'
                ' "actions": ["4.148.0.0/16"]}')   # actions is NOT an edge service — ignored
        self.assertEqual(
            self._with_body(network._github_ranges, body),
            ["140.82.112.0/20", "185.199.108.0/22", "192.30.252.0/22"],
        )

    def test_cloudfront_filters_aws_service(self):
        body = ('{"prefixes": ['
                '{"ip_prefix": "13.32.0.0/15", "service": "CLOUDFRONT"},'
                '{"ip_prefix": "52.94.76.0/22", "service": "EC2"}]}')
        self.assertEqual(self._with_body(network._cloudfront_ranges, body), ["13.32.0.0/15"])

    def test_google_subtracts_rentable_cloud_space(self):
        payloads = {
            "https://www.gstatic.com/ipranges/goog.json":
                '{"prefixes": [{"ipv4Prefix": "192.0.2.0/24"}, {"ipv4Prefix": "198.51.100.0/24"}, {"ipv6Prefix": "2001:db8::/32"}]}',
            "https://www.gstatic.com/ipranges/cloud.json":
                '{"prefixes": [{"ipv4Prefix": "198.51.100.0/25"}]}',
        }
        with patch.object(network, "_http_get", side_effect=payloads.__getitem__):
            self.assertEqual(network._google_ranges(), ["192.0.2.0/24", "198.51.100.128/25"])

    def test_registry_names_match_fetchers(self):
        self.assertEqual(set(network._RANGE_FETCHERS),
                         {"cloudflare", "fastly", "github", "cloudfront", "google"})


class TestLoadCdnRanges(unittest.TestCase):
    """The per-provider degradation chain: fresh cache → fetch(+save) →
    stale cache → skipped provider."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self._patches = [
            patch.object(network, "cdn_ranges_cache_path", lambda p: tmp / f"{p}.txt"),
            patch.object(network, "_RANGE_FETCHERS", {"good": lambda: ["192.0.2.0/24"],
                                                      "bad": self._raise}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        network._set_provider_blocks({})
        self.tmpdir.cleanup()

    @staticmethod
    def _raise():
        raise OSError("fetch refused")

    def test_fetch_populates_and_saves(self):
        with contextlib.redirect_stderr(io.StringIO()):
            network._load_cdn_ranges()
        self.assertEqual(network._provider_blocks["good"], ["192.0.2.0/24"])
        self.assertEqual(network._read_cached_ranges("good"), ["192.0.2.0/24"])

    def test_fresh_cache_skips_fetch(self):
        network._save_cached_ranges("good", ["198.51.100.0/24"])
        fetchers = {"good": MagicMock()}
        with patch.object(network, "_RANGE_FETCHERS", fetchers):
            network._load_cdn_ranges()
        fetchers["good"].assert_not_called()
        self.assertEqual(network._provider_blocks["good"], ["198.51.100.0/24"])

    def test_failed_fetch_falls_back_to_stale_cache(self):
        network._save_cached_ranges("bad", ["203.0.113.0/24"])
        with patch.object(network, "is_file_recent", return_value=False), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            network._load_cdn_ranges()
        self.assertEqual(network._provider_blocks["bad"], ["203.0.113.0/24"])
        self.assertIn("stale", err.getvalue())

    def test_failed_fetch_without_cache_skips_provider_only(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            network._load_cdn_ranges()
        self.assertNotIn("bad", network._provider_blocks)
        self.assertIn("good", network._provider_blocks)   # one dead provider can't sink the rest
        self.assertIn("no ranges for bad", err.getvalue())


class _SeededProvidersMixin(unittest.TestCase):
    """Install the stand-in provider table around each widening-policy test."""

    def setUp(self):
        network._set_provider_blocks(_TEST_PROVIDER_BLOCKS)
        network._seen_cdn_ranges.clear()

    def tearDown(self):
        network._set_provider_blocks({})
        network._seen_cdn_ranges.clear()


class TestCdnProviderRanges(_SeededProvidersMixin):
    def test_cloudflare_ip_detected_with_containing_block(self):
        provider, ranges = _cdn_provider_ranges([_CLOUDFLARE_IP])
        self.assertEqual(provider, "cloudflare")
        self.assertEqual(ranges, ["104.16.0.0/13"])

    def test_non_cdn_ip_yields_nothing(self):
        self.assertEqual(_cdn_provider_ranges([_NON_CDN_IP]), (None, []))

    def test_mixed_ips_collect_all_matched_blocks(self):
        provider, ranges = _cdn_provider_ranges([_CLOUDFLARE_IP, _NON_CDN_IP, "104.16.200.1"])
        self.assertEqual(provider, "cloudflare")
        self.assertEqual(ranges, ["104.16.0.0/13"])   # both CF IPs share one block — no duplicate

    def test_malformed_token_skipped(self):
        self.assertEqual(_cdn_provider_ranges(["not-an-ip", ""]), (None, []))


class TestTokensFor(_SeededProvidersMixin):
    """_tokens_for encodes the widening policy: containing CDN blocks for
    plain default-port entries, ALL provider blocks for wildcards, pinned IPs
    otherwise, one block emission per launch."""

    def test_non_cdn_ips_pin_exactly(self):
        tokens, provider, gap = _tokens_for("foo.com", [_NON_CDN_IP], "")
        self.assertEqual(tokens, [_NON_CDN_IP])
        self.assertIsNone(provider)
        self.assertFalse(gap)

    def test_port_entry_pins_even_on_cdn(self):
        # Opening a whole provider block on a custom port would be a broader
        # grant than the entry asked for — port entries keep pinned IPs.
        tokens, provider, gap = _tokens_for("foo.com", [_CLOUDFLARE_IP], "8443")
        self.assertEqual(tokens, [f"{_CLOUDFLARE_IP}:8443"])
        self.assertIsNone(provider)
        self.assertFalse(gap)

    def test_cdn_default_port_widens_to_block(self):
        tokens, provider, gap = _tokens_for("foo.com", [_CLOUDFLARE_IP], "")
        self.assertEqual(tokens, ["104.16.0.0/13"])
        self.assertEqual(provider, "cloudflare")
        self.assertFalse(gap)

    def test_block_emitted_once_across_hosts(self):
        first, _, _ = _tokens_for("a.com", [_CLOUDFLARE_IP], "")
        second, provider, _ = _tokens_for("b.com", ["104.17.5.5"], "")   # same 104.16.0.0/13 block
        self.assertEqual(first, ["104.16.0.0/13"])
        self.assertEqual(second, [])                 # block already open; covered IP needs no rule
        self.assertEqual(provider, "cloudflare")     # still annotated as CDN-widened

    def test_uncovered_ips_still_pinned_alongside_block(self):
        # Mixed A records: one CF edge + one origin outside any block — the
        # origin IP keeps its pinned rule.
        tokens, _, _ = _tokens_for("foo.com", [_CLOUDFLARE_IP, _NON_CDN_IP], "")
        self.assertEqual(tokens, ["104.16.0.0/13", _NON_CDN_IP])

    def test_distinct_providers_both_widen(self):
        t1, p1, _ = _tokens_for("a.com", [_CLOUDFLARE_IP], "")
        t2, p2, _ = _tokens_for("b.com", [_FASTLY_IP], "")
        self.assertEqual(t1, ["104.16.0.0/13"])
        self.assertEqual(t2, ["151.101.0.0/16"])
        self.assertEqual((p1, p2), ("cloudflare", "fastly"))

    def test_wildcard_on_cdn_opens_every_provider_block(self):
        # Subdomains can't be enumerated via DNS, so the wildcard grant is
        # the provider's whole published edge — every block, not just the
        # containing one.
        tokens, provider, gap = _tokens_for("foo.com", [_MULTI_IP], "", wildcard=True)
        self.assertEqual(tokens, _TEST_PROVIDER_BLOCKS["multi"])
        self.assertEqual(provider, "multi")
        self.assertFalse(gap)

    def test_wildcard_with_port_narrows_blocks_to_that_port(self):
        # A wildcard without widening would be meaningless, so an explicit
        # :port narrows the block tokens instead of downgrading to pinning.
        tokens, provider, _ = _tokens_for("foo.com", [_FASTLY_IP], "8443", wildcard=True)
        self.assertEqual(tokens, [f"{c}:8443" for c in _TEST_PROVIDER_BLOCKS["fastly"]])
        self.assertEqual(provider, "fastly")

    def test_wildcard_off_cdn_pins_base_and_reports_gap(self):
        tokens, provider, gap = _tokens_for("foo.com", [_NON_CDN_IP], "", wildcard=True)
        self.assertEqual(tokens, [_NON_CDN_IP])
        self.assertIsNone(provider)
        self.assertTrue(gap)

    def test_wildcard_blocks_shared_with_plain_widening(self):
        # A plain host already widened the containing block; the wildcard
        # re-emits only the provider's OTHER blocks.
        plain, _, _ = _tokens_for("a.com", [_CLOUDFLARE_IP], "")
        wild, _, _ = _tokens_for("b.com", [_CLOUDFLARE_IP], "", wildcard=True)
        self.assertEqual(plain, ["104.16.0.0/13"])
        self.assertEqual(set(wild), set(_TEST_PROVIDER_BLOCKS["cloudflare"]) - {"104.16.0.0/13"})


class TestIndexByHost(unittest.TestCase):
    def test_groups_entries_sharing_a_host(self):
        entries = [
            HostnameEntry("foo.com", "foo.com", ""),
            HostnameEntry("foo.com:8443", "foo.com", "8443"),
            HostnameEntry("bar.com", "bar.com", ""),
        ]
        self.assertEqual(_index_by_host(entries), {
            "foo.com": [entries[0], entries[1]],
            "bar.com": [entries[2]],
        })


class TestCascade(unittest.TestCase):
    """_cascade retries failures with growing timeouts and reports each host
    exactly once. The resolver is faked per-host with a scripted schedule."""

    def _run(self, schedules):
        """schedules: {host: [result-per-stage, ...]} — [] means fail that
        stage. Returns (resolved{host:ips}, failed[host], calls{host:count})."""
        calls = {}

        def fake_resolver(host, timeout):
            i = calls.get(host, 0)
            calls[host] = i + 1
            schedule = schedules[host]
            return schedule[i] if i < len(schedule) else []

        resolved = {}
        failed = []
        with patch.object(network, "_resolve_a_records", side_effect=fake_resolver):
            _cascade(list(schedules), resolved.__setitem__, failed.append)
        return resolved, failed, calls

    def test_first_stage_success_resolves_once(self):
        resolved, failed, calls = self._run({"a.com": [["1.1.1.1"]]})
        self.assertEqual(resolved, {"a.com": ["1.1.1.1"]})
        self.assertEqual(failed, [])
        self.assertEqual(calls["a.com"], 1)

    def test_contention_drop_recovered_on_retry(self):
        # Fails stage 1, succeeds stage 2 — the cascade's whole reason to exist.
        resolved, failed, calls = self._run({"a.com": [[], ["2.2.2.2"]]})
        self.assertEqual(resolved, {"a.com": ["2.2.2.2"]})
        self.assertEqual(failed, [])
        self.assertEqual(calls["a.com"], 2)

    def test_all_stages_exhausted_is_terminal_failure(self):
        resolved, failed, calls = self._run({"dead.com": []})
        self.assertEqual(resolved, {})
        self.assertEqual(failed, ["dead.com"])
        self.assertEqual(calls["dead.com"], len(network._RESOLVE_TIMEOUT_STAGES))

    def test_resolved_host_not_requeried_while_others_retry(self):
        resolved, failed, calls = self._run({
            "fast.com": [["1.1.1.1"]],
            "slow.com": [[], [], ["3.3.3.3"]],
        })
        self.assertEqual(calls["fast.com"], 1)
        self.assertEqual(calls["slow.com"], 3)
        self.assertEqual(failed, [])
        self.assertEqual(set(resolved), {"fast.com", "slow.com"})

    def test_duplicate_hosts_share_one_lookup(self):
        _, _, calls = self._run({"a.com": [["1.1.1.1"]]})
        with patch.object(network, "_resolve_a_records", return_value=["1.1.1.1"]) as res:
            _cascade(["a.com", "a.com", "a.com"], lambda *a: None, lambda *a: None)
        self.assertEqual(res.call_count, 1)


class TestResolutionCacheRoundtrip(unittest.TestCase):
    """_save_resolution_cache / _load_resolution_cache persist {host: ips}
    across launches, gated by the file-mtime TTL."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache_file = Path(self.tmpdir.name) / "resolved_domains.txt"
        self._patch = patch.object(network, "RESOLVED_DOMAINS_CACHE_FILE", self.cache_file)
        self._patch.start()
        self._saved_cache = dict(network._resolution_cache)
        network._resolution_cache.clear()
        network._fresh_resolutions.clear()

    def tearDown(self):
        self._patch.stop()
        network._resolution_cache.clear()
        network._resolution_cache.update(self._saved_cache)
        network._fresh_resolutions.clear()
        self.tmpdir.cleanup()

    def test_roundtrip(self):
        network._save_resolution_cache({"a.com": ["1.1.1.1", "2.2.2.2"], "b.com": ["3.3.3.3"]})
        network._load_resolution_cache()
        self.assertEqual(network._resolution_cache,
                         {"a.com": ["1.1.1.1", "2.2.2.2"], "b.com": ["3.3.3.3"]})

    def test_failed_hosts_not_persisted(self):
        network._save_resolution_cache({"a.com": ["1.1.1.1"], "dead.com": []})
        network._load_resolution_cache()
        self.assertNotIn("dead.com", network._resolution_cache)

    def test_stale_file_ignored(self):
        network._save_resolution_cache({"a.com": ["1.1.1.1"]})
        with patch.object(network, "is_file_recent", return_value=False):
            network._load_resolution_cache()
        self.assertEqual(network._resolution_cache, {})

    def test_missing_file_yields_empty_cache(self):
        network._load_resolution_cache()
        self.assertEqual(network._resolution_cache, {})


class TestResolveARecords(unittest.TestCase):
    """Fresh DNS is mandatory (stale pins were the whole disease); the
    cross-launch cache only unions in and rescues outright failures."""

    def setUp(self):
        self._saved_cache = dict(network._resolution_cache)
        network._resolution_cache.clear()
        network._fresh_resolutions.clear()

    def tearDown(self):
        network._resolution_cache.clear()
        network._resolution_cache.update(self._saved_cache)
        network._fresh_resolutions.clear()

    def _fake(self, stdout, returncode=0):
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    def test_malformed_resolver_tokens_dropped(self):
        # Resolver output is the one externally-influenced string in the
        # pipeline — anything not shaped like a plain IPv4 must not survive
        # (it would otherwise reach the batched `sh -c` script).
        fake = self._fake("1.2.3.4 STREAM x\nevil; rm -rf / STREAM x\n5.6.7.8 DGRAM x\n")
        with patch.object(network, "shell_capture", return_value=fake):
            ips = network._resolve_a_records("x.com", timeout=1)
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8"])

    def test_cached_host_still_queries_dns_and_unions(self):
        # A cached answer must never suppress the live lookup — the fresh IP
        # joins the cached one so both resolver views stay reachable.
        network._resolution_cache["hit.com"] = ["9.9.9.9"]
        with patch.object(network, "shell_capture",
                          return_value=self._fake("1.1.1.1 STREAM x\n")) as cap:
            ips = network._resolve_a_records("hit.com", timeout=1)
        cap.assert_called_once()
        self.assertEqual(ips, ["1.1.1.1", "9.9.9.9"])

    def test_dns_failure_falls_back_to_cache(self):
        network._resolution_cache["flaky.com"] = ["9.9.9.9"]
        with patch.object(network, "shell_capture", return_value=self._fake("", returncode=2)):
            ips = network._resolve_a_records("flaky.com", timeout=1)
        self.assertEqual(ips, ["9.9.9.9"])

    def test_dns_failure_without_cache_is_empty(self):
        with patch.object(network, "shell_capture", return_value=self._fake("", returncode=2)):
            self.assertEqual(network._resolve_a_records("dead.com", timeout=1), [])

    def test_only_fresh_answers_recorded_for_cache_save(self):
        # The persisted cache must hold live DNS results only — unioned
        # carryover would re-save itself forever (mtime refresh = rolling
        # TTL) and immortalize dead IPs.
        network._resolution_cache["hit.com"] = ["9.9.9.9"]
        with patch.object(network, "shell_capture",
                          return_value=self._fake("1.1.1.1 STREAM x\n")):
            network._resolve_a_records("hit.com", timeout=1)
        self.assertEqual(network._fresh_resolutions, {"hit.com": ["1.1.1.1"]})


class TestIptablesRulesFor(unittest.TestCase):
    def test_default_ports_open_https_and_http(self):
        rules = _iptables_rules_for("1.2.3.4")
        self.assertEqual(rules, [
            "iptables -I OUTPUT 1 -d 1.2.3.4 -p tcp --dport 443 -j ACCEPT",
            "iptables -I OUTPUT 1 -d 1.2.3.4 -p tcp --dport 80 -j ACCEPT",
        ])

    def test_explicit_port_opens_only_that_port(self):
        rules = _iptables_rules_for("1.2.3.4:8443")
        self.assertEqual(rules, ["iptables -I OUTPUT 1 -d 1.2.3.4 -p tcp --dport 8443 -j ACCEPT"])

    def test_cidr_token_accepted(self):
        rules = _iptables_rules_for("104.16.0.0/13")
        self.assertEqual(len(rules), 2)
        self.assertIn("-d 104.16.0.0/13", rules[0])

    def test_malformed_token_dropped_with_warning(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rules = _iptables_rules_for("$(reboot)")
        self.assertEqual(rules, [])
        self.assertIn("dropping malformed", err.getvalue())

    def test_shell_injection_attempt_dropped(self):
        # These strings are `&&`-joined into a `sh -c` script — nothing that
        # fails the strict address shape may produce a rule.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_iptables_rules_for("1.2.3.4; reboot"), [])
            self.assertEqual(_iptables_rules_for("1.2.3.4:80; reboot"), [])


class TestFlushRules(unittest.TestCase):
    def _flush(self, tokens, returncodes):
        """Run _flush_rules with a scripted docker-exec; returns the scripts
        executed (one per exec call)."""
        scripts = []
        codes = iter(returncodes)

        def fake_exec(container, *cmd):
            scripts.append(cmd[-1])
            return SimpleNamespace(returncode=next(codes, 0), stdout="", stderr="boom")

        with patch("launch.docker_config.docker_exec_root_subprocess", side_effect=fake_exec):
            network._flush_rules("c", tokens)
        return scripts

    def test_burst_becomes_single_exec(self):
        # 10 tokens × 2 default ports = 20 rules — well under the chunk cap.
        scripts = self._flush([f"1.2.3.{i}" for i in range(10)], [0])
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0].count("iptables -I"), 20)
        self.assertIn(" && ", scripts[0])

    def test_large_burst_chunks_at_cap(self):
        # 75 tokens × 2 ports = 150 rules → 2 execs at the 100-rule cap.
        scripts = self._flush([f"10.0.0.{i}:443" for i in range(150)], [0, 0])
        self.assertEqual(len(scripts), 2)

    def test_failed_chunk_retries_once_then_warns(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            scripts = self._flush(["1.2.3.4"], [1, 1])
        self.assertEqual(len(scripts), 2)   # first try + one retry
        self.assertIn("batched iptables insert failed", err.getvalue())

    def test_retry_success_does_not_warn(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            scripts = self._flush(["1.2.3.4"], [1, 0])
        self.assertEqual(len(scripts), 2)
        self.assertEqual(err.getvalue(), "")


class TestUpdaterWorkerBatching(unittest.TestCase):
    """_updater_worker drains everything already queued into one flush —
    the pacing fix: one docker exec per resolution burst, not per rule —
    then hands off to the refresher daemon when the stream ends."""

    def _run_worker(self, tokens):
        q = queue.Queue()
        for t in tokens:
            q.put(t)
        q.put(network._phase2_done)
        exec_calls = []

        def fake_exec(container, *cmd):
            exec_calls.append(cmd[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(network, "_phase2_queue", q), \
             patch.object(network, "_start_refresher") as start_refresher, \
             patch("launch.docker_config.wait_for_container_running", return_value=True), \
             patch("launch.docker_config.docker_exec_root_subprocess", side_effect=fake_exec):
            network._updater_worker("claude-code_test")
        return exec_calls, start_refresher

    def test_prequeued_burst_flushes_in_one_exec(self):
        # 20 tokens → 40 rules → a single exec (vs 40 under the old per-rule scheme).
        exec_calls, _ = self._run_worker([f"1.2.3.{i}" for i in range(20)])
        self.assertEqual(len(exec_calls), 1)
        self.assertEqual(exec_calls[0].count("iptables -I"), 40)

    def test_sentinel_only_queue_execs_nothing(self):
        exec_calls, _ = self._run_worker([])
        self.assertEqual(exec_calls, [])

    def test_stream_end_hands_off_to_refresher(self):
        # Both sentinel paths (bare sentinel / sentinel scooped mid-drain)
        # must start the drift-heal daemon exactly once.
        for tokens in ([], ["1.2.3.4"]):
            with self.subTest(tokens=tokens):
                _, start_refresher = self._run_worker(tokens)
                start_refresher.assert_called_once_with("claude-code_test")

    def test_container_never_up_skips_all_work(self):
        q = queue.Queue()
        q.put("1.2.3.4")
        with patch.object(network, "_phase2_queue", q), \
             patch.object(network, "_start_refresher") as start_refresher, \
             patch("launch.docker_config.wait_for_container_running", return_value=False), \
             patch("launch.docker_config.docker_exec_root_subprocess") as ex:
            network._updater_worker("claude-code_test")
        ex.assert_not_called()
        start_refresher.assert_not_called()


class TestWhitelistResolutionStatus(unittest.TestCase):
    """The agent-visible status tracker: state transitions + the YAML the
    agent actually reads (FIREWALL_NOTICE points it here)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmpdir.name)
        self.status = network._WhitelistResolutionStatus()
        self.status.init(self.state_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _file_text(self):
        return (self.state_dir / "domains_pending_resolve.yml").read_text()

    def test_init_writes_resolving_status(self):
        self.assertIn("status: resolving", self._file_text())

    def test_pending_listed_sorted(self):
        self.status.set_pending({"b.com", "a.com"})
        text = self._file_text()
        self.assertLess(text.index("- a.com"), text.index("- b.com"))

    def test_mark_resolved_removes_from_pending(self):
        self.status.set_pending({"a.com"})
        self.status.mark_resolved("a.com", ["1.1.1.1"])
        self.assertNotIn("- a.com", self._file_text())
        self.assertEqual(self.status.resolved["a.com"], ["1.1.1.1"])

    def test_mark_failed_records_reason(self):
        self.status.set_pending({"dead.com"})
        self.status.mark_failed("dead.com", "DNS resolution failed after all cascade stages")
        text = self._file_text()
        self.assertIn("dead.com: DNS resolution failed", text)
        self.assertNotIn("- dead.com", text)

    def test_cdn_annotation_rendered(self):
        self.status.set_pending({"cf.com"})
        self.status.mark_resolved("cf.com", [_CLOUDFLARE_IP], cdn="cloudflare")
        text = self._file_text()
        self.assertIn("cdn:", text)
        self.assertIn("cf.com: cloudflare", text)

    def test_non_cdn_host_not_annotated(self):
        self.status.set_pending({"plain.com"})
        self.status.mark_resolved("plain.com", [_NON_CDN_IP])
        self.assertNotIn("plain.com: ", self._file_text().split("cdn:")[1])

    def test_complete_flips_status(self):
        self.status.complete()
        self.assertIn("status: complete", self._file_text())

    def test_init_wipes_previous_run(self):
        self.status.set_pending({"a.com"})
        self.status.mark_resolved("a.com", ["1.1.1.1"], cdn="fastly")
        self.status.mark_skipped([("2a00::", "v6")])
        self.status.mark_wildcard_gap("w.com")
        self.status.init(self.state_dir)
        self.assertEqual(
            (self.status.resolved, self.status.failed, self.status.cdn,
             self.status.skipped, self.status.wildcard_gaps),
            ({}, {}, {}, {}, []),
        )
        self.assertIn("status: resolving", self._file_text())

    def test_skipped_entries_rendered_with_reason(self):
        self.status.mark_skipped([("2a04:4e42::/32", network._SKIPPED_IPV6_REASON)])
        text = self._file_text()
        self.assertIn("skipped:", text)
        self.assertIn(f"2a04:4e42::/32: {network._SKIPPED_IPV6_REASON}", text)

    def test_wildcard_gap_rendered_once(self):
        self.status.mark_wildcard_gap("foo.com")
        self.status.mark_wildcard_gap("foo.com")
        text = self._file_text()
        self.assertIn("wildcard_gaps:", text)
        self.assertEqual(text.count("- foo.com"), 1)

    def test_late_resolution_heals_a_failed_host(self):
        # A refresher pass can resolve a host that was dead at launch — the
        # failed: section must not keep contradicting the resolved: map.
        self.status.set_pending({"flaky.com"})
        self.status.mark_failed("flaky.com", "DNS resolution failed after all cascade stages")
        self.status.mark_resolved("flaky.com", ["1.1.1.1"])
        self.assertNotIn("flaky.com", self.status.failed)
        self.assertIn("flaky.com", self.status.resolved)


class _EmitterStateMixin(unittest.TestCase):
    """Shared seeding/restore for suites that drive _emit_tokens_for_host —
    directly or via _refresh_pass. Gives each test a fresh status singleton,
    empty emission ledgers, and a hand-built _all_entries_by_host."""

    def setUp(self):
        network._set_provider_blocks(_TEST_PROVIDER_BLOCKS)
        network._seen_cdn_ranges.clear()
        network._emitted_tokens.clear()
        network._fresh_resolutions.clear()
        self._saved_entries = dict(network._all_entries_by_host)
        network._all_entries_by_host.clear()
        self._status_patch = patch.object(network, "_status", network._WhitelistResolutionStatus())
        self.status = self._status_patch.start()

    def tearDown(self):
        self._status_patch.stop()
        network._set_provider_blocks({})
        network._seen_cdn_ranges.clear()
        network._emitted_tokens.clear()
        network._fresh_resolutions.clear()
        network._all_entries_by_host.clear()
        network._all_entries_by_host.update(self._saved_entries)

    @staticmethod
    def _entries(*specs):
        """Populate _all_entries_by_host from (host, port, wildcard) triples."""
        for host, port, wildcard in specs:
            entry = ("*." if wildcard else "") + host + (f":{port}" if port else "")
            network._all_entries_by_host.setdefault(host, []).append(
                HostnameEntry(entry, host, port, wildcard))


class TestEmitTokensForHost(_EmitterStateMixin):
    """The single funnel phase 1 / phase 2 / refresher share: per-entry
    token policy, cross-emitter dedupe, status annotations."""

    def test_multiple_entries_of_one_host_all_emit(self):
        self._entries(("foo.com", "", False), ("foo.com", "8443", False))
        tokens, label = network._emit_tokens_for_host("foo.com", [_NON_CDN_IP])
        self.assertEqual(tokens, [_NON_CDN_IP, f"{_NON_CDN_IP}:8443"])
        self.assertIsNone(label)

    def test_already_emitted_tokens_are_suppressed(self):
        self._entries(("foo.com", "", False))
        first, _ = network._emit_tokens_for_host("foo.com", [_NON_CDN_IP])
        second, _ = network._emit_tokens_for_host("foo.com", [_NON_CDN_IP])
        self.assertEqual(first, [_NON_CDN_IP])
        self.assertEqual(second, [])   # the refresher's steady-state no-op

    def test_wildcard_annotation_and_gap_filing(self):
        self._entries(("edge.com", "", True), ("plain.com", "", True))
        _, label = network._emit_tokens_for_host("edge.com", [_MULTI_IP])
        self.assertEqual(label, "multi (all blocks — wildcard)")
        network._emit_tokens_for_host("plain.com", [_NON_CDN_IP])
        self.assertEqual(self.status.wildcard_gaps, ["plain.com"])


class TestRefreshPass(_EmitterStateMixin):
    """_refresh_pass is the mid-session drift heal: fresh-resolve everything,
    flush only the genuinely new addresses, never demote, write only on
    change."""

    def _run(self, resolutions):
        """Drive one pass with a scripted resolver; returns (flushed token
        batches, cache-save count)."""
        flushed, saves = [], []
        with patch.object(network, "_resolve_a_records",
                          side_effect=lambda h, timeout: resolutions.get(h, [])), \
             patch.object(network, "_flush_rules",
                          side_effect=lambda c, tokens: flushed.append(tokens)), \
             patch.object(network, "_save_resolution_cache",
                          side_effect=lambda m: saves.append(m)):
            network._refresh_pass("claude-code_test")
        return flushed, saves

    def test_new_address_is_flushed_and_cache_saved(self):
        self._entries(("foo.com", "", False))
        network._emitted_tokens.add("1.1.1.1")   # what launch already opened
        flushed, saves = self._run({"foo.com": ["1.1.1.1", "2.2.2.2"]})
        self.assertEqual(flushed, [["2.2.2.2"]])
        self.assertEqual(len(saves), 1)

    def test_unchanged_answers_touch_nothing(self):
        # Steady state must be write-free: no exec, no cache rewrite, no
        # status churn.
        self._entries(("foo.com", "", False))
        network._emitted_tokens.add("1.1.1.1")
        flushed, saves = self._run({"foo.com": ["1.1.1.1"]})
        self.assertEqual((flushed, saves), ([], []))
        self.assertEqual(self.status.resolved, {})

    def test_resolution_failure_never_demotes(self):
        # A host that misses one cycle keeps its rules and isn't marked
        # failed — the next cycle simply retries.
        self._entries(("foo.com", "", False))
        flushed, _ = self._run({"foo.com": []})
        self.assertEqual(flushed, [])
        self.assertEqual(self.status.failed, {})

    def test_host_moving_onto_a_cdn_widens_late(self):
        # DNS steering can move a host onto a known provider mid-session —
        # the pass picks up the block grant just like launch would have.
        self._entries(("foo.com", "", False))
        flushed, _ = self._run({"foo.com": [_CLOUDFLARE_IP]})
        self.assertEqual(flushed, [["104.16.0.0/13"]])
        self.assertEqual(self.status.cdn, {"foo.com": "cloudflare"})

    def test_empty_whitelist_is_a_noop(self):
        flushed, saves = self._run({})
        self.assertEqual((flushed, saves), ([], []))


if __name__ == "__main__":
    unittest.main()
