"""Tests for launch.firewall.cdn_ranges — the fetched provider-range table.

No network: `_http_get` is patched per test, so the parsers, the cache chain
(fresh → fetch → stale → skip) and the containment scan are all exercised
against known payloads. These moved here with the module they cover
(2026-09-03); the widening POLICY that consumes the table stays in
test_firewall's TestTokensFor, which is where the resolver applies it.

This file owns the stand-in provider table: provider ranges are fetched at
launch and never baked, so every widening test needs seeded data, and the
table is this module's fixture rather than the resolver's."""

import contextlib
import io
import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from launch.firewall import cdn_ranges
from launch.firewall.cdn_ranges import _clean_cidrs, _subtract_networks

# Addresses below sit inside these seeded blocks. Imported by test_firewall
# for the policy tests that widen against them.
_TEST_PROVIDER_BLOCKS = {
    "cloudflare": ["104.16.0.0/13", "172.64.0.0/13", "198.41.128.0/17"],
    "fastly": ["151.101.0.0/16", "199.232.0.0/16"],
    "multi": ["192.0.2.0/24", "198.18.0.0/15", "203.0.112.0/24"],
}
_CLOUDFLARE_IP = "104.16.1.1"        # inside seeded cloudflare 104.16.0.0/13
_FASTLY_IP = "151.101.1.1"           # inside seeded fastly 151.101.0.0/16
_MULTI_IP = "198.18.5.5"             # inside seeded multi 198.18.0.0/15
_NON_CDN_IP = "203.0.113.7"          # TEST-NET-3 — in no seeded block


class _SeededProviders(unittest.TestCase):
    """Install the stand-in provider table around each test."""

    def setUp(self):
        cdn_ranges.set_provider_blocks(_TEST_PROVIDER_BLOCKS)

    def tearDown(self):
        cdn_ranges.set_provider_blocks({})


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
        with patch.object(cdn_ranges, "_http_get", return_value=body):
            return fetcher()

    def test_cloudflare_plain_text_lines(self):
        self.assertEqual(
            self._with_body(cdn_ranges._cloudflare_ranges, "104.16.0.0/13\n172.64.0.0/13\n"),
            ["104.16.0.0/13", "172.64.0.0/13"],
        )

    def test_fastly_addresses_key(self):
        body = '{"addresses": ["151.101.0.0/16"], "ipv6_addresses": ["2a04:4e40::/32"]}'
        self.assertEqual(self._with_body(cdn_ranges._fastly_ranges, body), ["151.101.0.0/16"])

    def test_github_meta_edge_services_v4_only(self):
        body = ('{"web": ["140.82.112.0/20", "2a0a:a440::/29"], "api": ["140.82.112.0/20"],'
                ' "git": ["192.30.252.0/22"], "packages": [], "pages": ["185.199.108.0/22"],'
                ' "actions": ["4.148.0.0/16"]}')   # actions is NOT an edge service — ignored
        self.assertEqual(
            self._with_body(cdn_ranges._github_ranges, body),
            ["140.82.112.0/20", "185.199.108.0/22", "192.30.252.0/22"],
        )

    def test_cloudfront_filters_aws_service(self):
        body = ('{"prefixes": ['
                '{"ip_prefix": "13.32.0.0/15", "service": "CLOUDFRONT"},'
                '{"ip_prefix": "52.94.76.0/22", "service": "EC2"}]}')
        self.assertEqual(self._with_body(cdn_ranges._cloudfront_ranges, body), ["13.32.0.0/15"])

    def test_google_subtracts_rentable_cloud_space(self):
        payloads = {
            "https://www.gstatic.com/ipranges/goog.json":
                '{"prefixes": [{"ipv4Prefix": "192.0.2.0/24"}, {"ipv4Prefix": "198.51.100.0/24"}, {"ipv6Prefix": "2001:db8::/32"}]}',
            "https://www.gstatic.com/ipranges/cloud.json":
                '{"prefixes": [{"ipv4Prefix": "198.51.100.0/25"}]}',
        }
        with patch.object(cdn_ranges, "_http_get", side_effect=payloads.__getitem__):
            self.assertEqual(cdn_ranges._google_ranges(), ["192.0.2.0/24", "198.51.100.128/25"])

    def test_registry_names_match_fetchers(self):
        self.assertEqual(set(cdn_ranges._RANGE_FETCHERS),
                         {"cloudflare", "fastly", "github", "cloudfront", "google"})


class TestLoadCdnRanges(unittest.TestCase):
    """The per-provider degradation chain: fresh cache → fetch(+save) →
    stale cache → skipped provider."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self._patches = [
            patch.object(cdn_ranges, "cdn_ranges_cache_path", lambda p: tmp / f"{p}.txt"),
            patch.object(cdn_ranges, "_RANGE_FETCHERS", {"good": lambda: ["192.0.2.0/24"],
                                                      "bad": self._raise}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        cdn_ranges.set_provider_blocks({})
        self.tmpdir.cleanup()

    @staticmethod
    def _raise():
        raise OSError("fetch refused")

    def test_fetch_populates_and_saves(self):
        with contextlib.redirect_stderr(io.StringIO()):
            cdn_ranges.load()
        self.assertEqual(cdn_ranges._provider_blocks["good"], ["192.0.2.0/24"])
        self.assertEqual(cdn_ranges._read_cached_ranges("good"), ["192.0.2.0/24"])

    def test_fresh_cache_skips_fetch(self):
        cdn_ranges._save_cached_ranges("good", ["198.51.100.0/24"])
        fetchers = {"good": MagicMock()}
        with patch.object(cdn_ranges, "_RANGE_FETCHERS", fetchers):
            cdn_ranges.load()
        fetchers["good"].assert_not_called()
        self.assertEqual(cdn_ranges._provider_blocks["good"], ["198.51.100.0/24"])

    def test_failed_fetch_falls_back_to_stale_cache(self):
        cdn_ranges._save_cached_ranges("bad", ["203.0.113.0/24"])
        with patch.object(cdn_ranges, "is_file_recent", return_value=False), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            cdn_ranges.load()
        self.assertEqual(cdn_ranges._provider_blocks["bad"], ["203.0.113.0/24"])
        self.assertIn("stale", err.getvalue())

    def test_failed_fetch_without_cache_skips_provider_only(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cdn_ranges.load()
        self.assertNotIn("bad", cdn_ranges._provider_blocks)
        self.assertIn("good", cdn_ranges._provider_blocks)   # one dead provider can't sink the rest
        self.assertIn("no ranges for bad", err.getvalue())


class TestProviderRanges(_SeededProviders):
    def test_cloudflare_ip_detected_with_containing_block(self):
        provider, ranges = cdn_ranges.provider_ranges([_CLOUDFLARE_IP])
        self.assertEqual(provider, "cloudflare")
        self.assertEqual(ranges, ["104.16.0.0/13"])

    def test_non_cdn_ip_yields_nothing(self):
        self.assertEqual(cdn_ranges.provider_ranges([_NON_CDN_IP]), (None, []))

    def test_mixed_ips_collect_all_matched_blocks(self):
        provider, ranges = cdn_ranges.provider_ranges([_CLOUDFLARE_IP, _NON_CDN_IP, "104.16.200.1"])
        self.assertEqual(provider, "cloudflare")
        self.assertEqual(ranges, ["104.16.0.0/13"])   # both CF IPs share one block — no duplicate

    def test_malformed_token_skipped(self):
        self.assertEqual(cdn_ranges.provider_ranges(["not-an-ip", ""]), (None, []))


if __name__ == "__main__":
    unittest.main()
