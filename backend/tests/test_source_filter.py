"""Characterization tests for ``backend.utils.news_fetching.source_filter``.

A denylist that silently stops matching (subdomains, ports, ``www.``) is
invisible in production, so the normalization rules are pinned exactly.
"""

from backend.utils.news_fetching import source_filter


class TestNormalizeHost:
    def test_bare_domain(self):
        assert source_filter._normalize_host("Example.com") == "example.com"

    def test_full_url(self):
        assert source_filter._normalize_host("https://www.example.com/path?q=1") == "example.com"

    def test_bare_domain_with_path(self):
        assert source_filter._normalize_host("example.com/some/path") == "example.com"

    def test_port_stripped(self):
        assert source_filter._normalize_host("example.com:8080") == "example.com"

    def test_leading_www_stripped(self):
        assert source_filter._normalize_host("www.example.com") == "example.com"

    def test_comment_line_discarded(self):
        assert source_filter._normalize_host("# a comment") == ""

    def test_blank_discarded(self):
        assert source_filter._normalize_host("   ") == ""

    def test_none_discarded(self):
        assert source_filter._normalize_host(None) == ""

    def test_subdomain_kept(self):
        # Only a literal leading "www." is stripped; other subdomains remain.
        assert source_filter._normalize_host("m.example.com") == "m.example.com"


class TestHostOf:
    def test_simple(self):
        assert source_filter._host_of("https://www.Example.com/x") == "example.com"

    def test_port_stripped(self):
        assert source_filter._host_of("http://example.com:8080/x") == "example.com"

    def test_no_scheme_gives_empty(self):
        # urlparse puts a scheme-less string in `path`, so netloc is empty.
        assert source_filter._host_of("example.com/x") == ""


class TestIsBlockedUrl:
    # These run against the real blocked_sources.txt, whose one live entry is
    # whalesbook.com — the same data production uses.

    def test_blocked_domain(self):
        assert source_filter.is_blocked_url("https://whalesbook.com/article") is True

    def test_blocked_www(self):
        assert source_filter.is_blocked_url("https://www.whalesbook.com/article") is True

    def test_blocked_subdomain(self):
        assert source_filter.is_blocked_url("https://news.whalesbook.com/a") is True

    def test_unrelated_domain_allowed(self):
        assert source_filter.is_blocked_url("https://reuters.com/article") is False

    def test_suffix_lookalike_not_blocked(self):
        # notwhalesbook.com must NOT match whalesbook.com.
        assert source_filter.is_blocked_url("https://notwhalesbook.com/a") is False

    def test_none_allowed(self):
        assert source_filter.is_blocked_url(None) is False

    def test_empty_allowed(self):
        assert source_filter.is_blocked_url("") is False
