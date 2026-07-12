"""Tests for launch.network — the {auto}-mode firewall's host-side logic.

Everything here runs without DNS, docker, or threads-under-test: the pure
transformations (_expand_whitelist, _cdn_provider_ranges, _tokens_for,
_iptables_rules_for, _index_by_host) are exercised directly; the cascade gets
a fake resolver; the updater worker is driven synchronously with a pre-filled
queue; the status tracker writes into a tmp dir. Module-level state the
functions share (_seen_cdn_ranges, _resolution_cache, _phase2_queue) is
reset around each test that touches it."""

import contextlib
import io
import ipaddress
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from launch import network
from launch.network import (
    HostnameEntry, _cascade, _cdn_provider_ranges, _expand_whitelist,
    _index_by_host, _iptables_rules_for, _tokens_for,
)
from launch.template_code.firewall_domains import BUILTIN_FIREWALL_DOMAINS, CDN_IPV4_RANGES

# Addresses used as fixtures below — inside well-known curated blocks.
_CLOUDFLARE_IP = "104.16.1.1"        # inside cloudflare 104.16.0.0/13
_FASTLY_IP = "151.101.1.1"           # inside fastly 151.101.0.0/16
_NON_CDN_IP = "203.0.113.7"          # TEST-NET-3 — in no provider block


class TestExpandWhitelist(unittest.TestCase):
    """_expand_whitelist is the security-relevant entry transformation:
    dedupe, `*.` strip, apex-from-www, literal/hostname split."""

    def test_hostname_entry_shape(self):
        _, hostnames = _expand_whitelist(["foo.com:8443"])
        self.assertEqual(hostnames, [HostnameEntry(entry="foo.com:8443", host="foo.com", port="8443")])

    def test_www_entry_also_allows_apex(self):
        _, hostnames = _expand_whitelist(["www.foo.com"])
        self.assertEqual({h.host for h in hostnames}, {"www.foo.com", "foo.com"})

    def test_bare_apex_does_not_add_www(self):
        # The convenience is one-directional — apex entries stay apex-only.
        _, hostnames = _expand_whitelist(["foo.com"])
        self.assertEqual({h.host for h in hostnames}, {"foo.com"})

    def test_wildcard_prefix_stripped(self):
        _, hostnames = _expand_whitelist(["*.foo.com"])
        self.assertEqual({h.host for h in hostnames}, {"foo.com"})

    def test_duplicates_collapse(self):
        _, hostnames = _expand_whitelist(["foo.com", "foo.com", "*.foo.com"])
        self.assertEqual(len(hostnames), 1)

    def test_literal_ip_and_cidr_split_out(self):
        literals, hostnames = _expand_whitelist(["1.2.3.4", "10.0.0.0/8:443", "foo.com"])
        self.assertEqual(literals, ["1.2.3.4", "10.0.0.0/8:443"])
        self.assertEqual([h.host for h in hostnames], ["foo.com"])

    def test_port_preserved_on_hostname(self):
        _, hostnames = _expand_whitelist(["foo.com:8080"])
        self.assertEqual(hostnames[0].port, "8080")

    def test_output_sorted_deterministically(self):
        _, first = _expand_whitelist(["b.com", "a.com", "c.com"])
        _, second = _expand_whitelist(["c.com", "a.com", "b.com"])
        self.assertEqual(first, second)
        self.assertEqual([h.host for h in first], ["a.com", "b.com", "c.com"])

    def test_builtin_list_expands_cleanly(self):
        # The shipped domain list must all classify as hostnames (no stray
        # literal IPs hiding in it) and produce a non-trivial pending set.
        literals, hostnames = _expand_whitelist(BUILTIN_FIREWALL_DOMAINS)
        self.assertEqual(literals, [])
        self.assertGreater(len(hostnames), len(BUILTIN_FIREWALL_DOMAINS))   # www-stripping adds apexes


class TestCdnData(unittest.TestCase):
    """The curated CDN table itself — every block must parse as IPv4 CIDR
    (a malformed or v6 entry would break the import-time _CDN_NETWORKS build)."""

    def test_every_block_parses_as_ipv4_network(self):
        for provider, cidrs in CDN_IPV4_RANGES.items():
            self.assertTrue(cidrs, f"{provider} has no blocks")
            for cidr in cidrs:
                with self.subTest(provider=provider, cidr=cidr):
                    ipaddress.IPv4Network(cidr)   # raises on malformed / v6

    def test_expected_providers_present(self):
        self.assertEqual(set(CDN_IPV4_RANGES), {"cloudflare", "fastly", "github", "cloudfront"})


class TestCdnProviderRanges(unittest.TestCase):
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


class TestTokensFor(unittest.TestCase):
    """_tokens_for encodes the widening policy: CDN blocks for default-port
    entries, pinned IPs otherwise, one block emission per launch."""

    def setUp(self):
        network._seen_cdn_ranges.clear()

    def tearDown(self):
        network._seen_cdn_ranges.clear()

    def test_non_cdn_ips_pin_exactly(self):
        tokens, provider = _tokens_for("foo.com", [_NON_CDN_IP], "")
        self.assertEqual(tokens, [_NON_CDN_IP])
        self.assertIsNone(provider)

    def test_port_entry_pins_even_on_cdn(self):
        # Opening a whole provider block on a custom port would be a broader
        # grant than the entry asked for — port entries keep pinned IPs.
        tokens, provider = _tokens_for("foo.com", [_CLOUDFLARE_IP], "8443")
        self.assertEqual(tokens, [f"{_CLOUDFLARE_IP}:8443"])
        self.assertIsNone(provider)

    def test_cdn_default_port_widens_to_block(self):
        tokens, provider = _tokens_for("foo.com", [_CLOUDFLARE_IP], "")
        self.assertEqual(tokens, ["104.16.0.0/13"])
        self.assertEqual(provider, "cloudflare")

    def test_block_emitted_once_across_hosts(self):
        first, _ = _tokens_for("a.com", [_CLOUDFLARE_IP], "")
        second, provider = _tokens_for("b.com", ["104.17.5.5"], "")   # same 104.16.0.0/13 block
        self.assertEqual(first, ["104.16.0.0/13"])
        self.assertEqual(second, [])                 # block already open; covered IP needs no rule
        self.assertEqual(provider, "cloudflare")     # still annotated as CDN-widened

    def test_uncovered_ips_still_pinned_alongside_block(self):
        # Mixed A records: one CF edge + one origin outside any block — the
        # origin IP keeps its pinned rule.
        tokens, _ = _tokens_for("foo.com", [_CLOUDFLARE_IP, _NON_CDN_IP], "")
        self.assertEqual(tokens, ["104.16.0.0/13", _NON_CDN_IP])

    def test_distinct_providers_both_widen(self):
        t1, p1 = _tokens_for("a.com", [_CLOUDFLARE_IP], "")
        t2, p2 = _tokens_for("b.com", [_FASTLY_IP], "")
        self.assertEqual(t1, ["104.16.0.0/13"])
        self.assertEqual(t2, ["151.101.0.0/16"])
        self.assertEqual((p1, p2), ("cloudflare", "fastly"))


class TestIndexByHost(unittest.TestCase):
    def test_groups_entries_sharing_a_host(self):
        entries = [
            HostnameEntry("foo.com", "foo.com", ""),
            HostnameEntry("foo.com:8443", "foo.com", "8443"),
            HostnameEntry("bar.com", "bar.com", ""),
        ]
        self.assertEqual(_index_by_host(entries), {
            "foo.com": [("foo.com", ""), ("foo.com:8443", "8443")],
            "bar.com": [("bar.com", "")],
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

    def tearDown(self):
        self._patch.stop()
        network._resolution_cache.clear()
        network._resolution_cache.update(self._saved_cache)
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

    def test_cached_host_short_circuits_resolver(self):
        network._resolution_cache["hit.com"] = ["9.9.9.9"]
        with patch.object(network, "shell_capture") as cap:
            ips = network._resolve_a_records("hit.com", timeout=1)
        cap.assert_not_called()
        self.assertEqual(ips, ["9.9.9.9"])


class TestResolveARecordsValidation(unittest.TestCase):
    def test_malformed_resolver_tokens_dropped(self):
        # Resolver output is the one externally-influenced string in the
        # pipeline — anything not shaped like a plain IPv4 must not survive
        # (it would otherwise reach the batched `sh -c` script).
        fake = SimpleNamespace(returncode=0, stdout="1.2.3.4 STREAM x\nevil; rm -rf / STREAM x\n5.6.7.8 DGRAM x\n")
        with patch.object(network, "shell_capture", return_value=fake):
            ips = network._resolve_a_records("x.com", timeout=1)
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8"])


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
    the pacing fix: one docker exec per resolution burst, not per rule."""

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
             patch("launch.docker_config.wait_for_container_running", return_value=True), \
             patch("launch.docker_config.docker_exec_root_subprocess", side_effect=fake_exec):
            network._updater_worker("claude-code_test")
        return exec_calls

    def test_prequeued_burst_flushes_in_one_exec(self):
        # 20 tokens → 40 rules → a single exec (vs 40 under the old per-rule scheme).
        exec_calls = self._run_worker([f"1.2.3.{i}" for i in range(20)])
        self.assertEqual(len(exec_calls), 1)
        self.assertEqual(exec_calls[0].count("iptables -I"), 40)

    def test_sentinel_only_queue_execs_nothing(self):
        self.assertEqual(self._run_worker([]), [])

    def test_container_never_up_skips_all_work(self):
        q = queue.Queue()
        q.put("1.2.3.4")
        with patch.object(network, "_phase2_queue", q), \
             patch("launch.docker_config.wait_for_container_running", return_value=False), \
             patch("launch.docker_config.docker_exec_root_subprocess") as ex:
            network._updater_worker("claude-code_test")
        ex.assert_not_called()


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
        self.status.init(self.state_dir)
        self.assertEqual((self.status.resolved, self.status.failed, self.status.cdn), ({}, {}, {}))
        self.assertIn("status: resolving", self._file_text())

    def test_resolved_snapshot_is_a_copy(self):
        self.status.mark_resolved("a.com", ["1.1.1.1"])
        snap = self.status.resolved_snapshot()
        snap["b.com"] = ["2.2.2.2"]
        self.assertNotIn("b.com", self.status.resolved)


if __name__ == "__main__":
    unittest.main()
