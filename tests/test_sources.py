"""The readers. Dumb by design: they produce candidates and dedupe, nothing else."""

from __future__ import annotations

import pytest
import requests

from multiagency import sources
from multiagency.config import SourceConfig
from multiagency.sources import fingerprint, read_items

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>A feed</title>
  <item>
    <title>Retries that swallow errors</title>
    <description>We lost two days to a retry loop hiding a 401 from us.</description>
  </item>
  <item>
    <title>Dedupe on content</title>
    <description>Identifiers change when a feed rebuilds itself, words do not.</description>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>The boundary, not the model</title>
    <summary>The prompt is usually fine. The thing feeding it is guessing.</summary>
  </entry>
</feed>"""


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("{} error".format(self.status_code))


def stub_get(monkeypatch, response=None, raise_with=None) -> dict:
    captured: dict = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        if raise_with:
            raise raise_with
        return response

    monkeypatch.setattr(sources.requests, "get", fake_get)
    return captured


def rss_source() -> SourceConfig:
    return SourceConfig("feed", "field_notes", "rss", "https://example.com/feed.xml")


# --- fingerprinting -------------------------------------------------------


def test_whitespace_and_case_are_not_identity():
    assert fingerprint("A note about slots.") == fingerprint("  a  NOTE about   slots. ")


def test_different_words_are_different_material():
    assert fingerprint("A note about slots.") != fingerprint("A note about lanes.")


# --- file readers ---------------------------------------------------------


def test_file_lines_skips_comments_and_short_lines(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text(
        "# a comment\n\nshort\nA line long enough to be worth posting about.\n",
        encoding="utf-8",
    )
    items = read_items(SourceConfig("s", "l", "file_lines", str(path)))
    assert items == ["A line long enough to be worth posting about."]


def test_jsonl_reads_the_first_text_like_field(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text(
        '{"title": "A title that is long enough to keep"}\n'
        '{"text": "A text field that is long enough to keep"}\n'
        '"A bare string that is long enough to keep"\n',
        encoding="utf-8",
    )
    items = read_items(SourceConfig("s", "l", "jsonl", str(path)))
    assert len(items) == 3
    assert items[1] == "A text field that is long enough to keep"


def test_broken_jsonl_names_the_line(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text('{"text": "fine and long enough to keep"}\n{oops\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        read_items(SourceConfig("s", "l", "jsonl", str(path)))


def test_directory_reads_one_item_per_file(tmp_path):
    (tmp_path / "a.md").write_text("The first note, long enough to keep.", encoding="utf-8")
    (tmp_path / "b.txt").write_text("The second note, long enough to keep.", encoding="utf-8")
    (tmp_path / "c.png").write_bytes(b"not text")
    items = read_items(SourceConfig("s", "l", "directory", str(tmp_path)))
    assert len(items) == 2


def test_an_unknown_source_type_is_an_error():
    with pytest.raises(ValueError, match="no reader for source_type"):
        read_items(SourceConfig("s", "l", "carrier_pigeon", "somewhere"))


# --- rss ------------------------------------------------------------------


def test_rss_items_become_title_and_summary(monkeypatch):
    captured = stub_get(monkeypatch, FakeResponse(RSS.encode()))
    items = read_items(rss_source())

    assert captured["url"] == "https://example.com/feed.xml"
    assert captured["timeout"] == 30
    assert captured["headers"]["User-Agent"] == "multiagency-social/0.1"
    assert items[0].startswith("Retries that swallow errors. We lost two days")
    assert len(items) == 2


def test_atom_entries_are_read_too(monkeypatch):
    stub_get(monkeypatch, FakeResponse(ATOM.encode()))
    items = read_items(rss_source())
    assert items == [
        "The boundary, not the model. The prompt is usually fine. "
        "The thing feeding it is guessing."
    ]


def test_an_unreachable_feed_raises_a_connection_error(monkeypatch):
    stub_get(monkeypatch, raise_with=requests.ConnectionError("no route to host"))
    with pytest.raises(ConnectionError, match="could not fetch"):
        read_items(rss_source())


def test_an_http_error_from_a_feed_raises_a_connection_error(monkeypatch):
    stub_get(monkeypatch, FakeResponse(b"", status_code=404))
    with pytest.raises(ConnectionError, match="could not fetch"):
        read_items(rss_source())


def test_a_page_that_parses_but_is_not_a_feed_yields_nothing(monkeypatch):
    """Well-formed XML with no items is empty, not an error. Nothing is invented."""
    stub_get(monkeypatch, FakeResponse(b"<html><body>not a feed</body></html>"))
    assert read_items(rss_source()) == []


def test_malformed_xml_raises(monkeypatch):
    from xml.etree.ElementTree import ParseError

    stub_get(monkeypatch, FakeResponse(b"<rss><channel><item>unclosed"))
    with pytest.raises(ParseError):
        read_items(rss_source())


def test_a_failing_feed_is_recorded_rather_than_raised(conn, monkeypatch):
    """pull_source turns any reader failure into a recorded event."""
    stub_get(monkeypatch, raise_with=requests.Timeout("timed out"))
    result = sources.pull_source(conn, rss_source())

    assert result.new == 0
    assert "ConnectionError" in result.error
    from multiagency import db

    assert "source_failed" in [e["kind"] for e in db.recent_events(conn)]
