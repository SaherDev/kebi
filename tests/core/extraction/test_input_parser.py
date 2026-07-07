"""Tests for input parser."""

from kebi.core.extraction.input_parser import parse_input


def test_url_with_text_before_and_after() -> None:
    """Test parsing URL with text before and after."""
    result = parse_input("found this https://tiktok.com/v/123 at the mall")
    assert result.url == "https://tiktok.com/v/123"
    assert result.supplementary_text == "found this at the mall"
    assert result.input_type == "url_with_text"


def test_text_before_url() -> None:
    """Test parsing text before URL."""
    result = parse_input("amazing ramen https://tiktok.com/v/123")
    assert result.url == "https://tiktok.com/v/123"
    assert result.supplementary_text == "amazing ramen"
    assert result.input_type == "url_with_text"


def test_text_after_url() -> None:
    """Test parsing text after URL."""
    result = parse_input("https://tiktok.com/v/123 amazing ramen")
    assert result.url == "https://tiktok.com/v/123"
    assert result.supplementary_text == "amazing ramen"
    assert result.input_type == "url_with_text"


def test_url_only() -> None:
    """Test parsing URL only."""
    result = parse_input("https://tiktok.com/v/123")
    assert result.url == "https://tiktok.com/v/123"
    assert result.supplementary_text == ""
    assert result.input_type == "url_only"


def test_plain_text_only() -> None:
    """Test parsing plain text without URL."""
    result = parse_input("amazing ramen place downtown")
    assert result.url is None
    assert result.supplementary_text == "amazing ramen place downtown"
    assert result.input_type == "text_only"


def test_url_with_extra_whitespace() -> None:
    """Test parsing with extra whitespace."""
    result = parse_input("  found this   https://tiktok.com/v/123   nearby  ")
    assert result.url == "https://tiktok.com/v/123"
    assert result.supplementary_text == "found this nearby"
    assert result.input_type == "url_with_text"


def test_http_url() -> None:
    """Test parsing HTTP URL (not HTTPS)."""
    result = parse_input("check http://example.com description")
    assert result.url == "http://example.com"
    assert result.supplementary_text == "check description"
    assert result.input_type == "url_with_text"


def test_url_with_query_params_stripped_for_recognized_host() -> None:
    """parse_input runs the URL through canonicalize_url, so tracking
    params on recognized hosts (tiktok / instagram / youtube) are
    stripped before the URL is returned. ADR-074 cache keying needs
    this to actually hit on viral content."""
    result = parse_input("https://tiktok.com/v/123?param=value text")
    assert result.url == "https://tiktok.com/v/123"
    assert result.supplementary_text == "text"
    assert result.input_type == "url_with_text"


def test_url_with_query_params_preserved_for_unknown_host() -> None:
    """Unknown hosts pass through unchanged — universal tracking-param
    stripping is a follow-up."""
    result = parse_input("https://example.com/x?param=value text")
    assert result.url == "https://example.com/x?param=value"
    assert result.supplementary_text == "text"


def test_multiple_urls_uses_first() -> None:
    """Test that multiple URLs extracts the first one."""
    result = parse_input(
        "https://tiktok.com/v/123 and https://tiktok.com/v/456 description"
    )
    assert result.url == "https://tiktok.com/v/123"
    assert result.supplementary_text == "and https://tiktok.com/v/456 description"
    assert result.input_type == "url_with_text"


def test_empty_text_before_and_after_url() -> None:
    """Test URL with empty text before/after treated as url_only."""
    result = parse_input("   https://tiktok.com/v/123   ")
    assert result.url == "https://tiktok.com/v/123"
    assert result.supplementary_text == ""
    assert result.input_type == "url_only"


def test_long_supplementary_text() -> None:
    """Test parsing with long supplementary text."""
    long_text = "found this amazing ramen place " * 10
    result = parse_input(f"{long_text}https://tiktok.com/v/123")
    assert result.url == "https://tiktok.com/v/123"
    assert long_text.strip() in result.supplementary_text
    assert result.input_type == "url_with_text"
