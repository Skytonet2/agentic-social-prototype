"""Publishing, and the OAuth 1.0a signing that requests-oauthlib does for us."""

from __future__ import annotations

import json

import pytest
import requests

from multiagency.errors import PublishError
from multiagency.publisher import MockPublisher, XPublisher, get_publisher, post_url
from multiagency.settings import Settings

# Deliberately unmistakable fakes. Nothing here depends on the values being
# realistic, and credential-shaped strings in a repository trip secret
# scanners and worry reviewers for no benefit.
REFERENCE = {
    "api_key": "example-api-key-not-a-real-credential",
    "api_secret": "example-api-secret-not-a-real-credential",
    "access_token": "example-access-token-not-a-real-credential",
    "access_token_secret": "example-token-secret-not-a-real-credential",
}


class FakeResponse:
    """Enough of a requests.Response for the paths publish() cares about."""

    def __init__(self, status_code: int = 201, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def stub_session(publisher: XPublisher, response=None, raise_with=None) -> dict:
    """Replace the session so nothing reaches the network, and record the call."""
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        if raise_with:
            raise raise_with
        return response

    publisher.session.post = fake_post
    return captured


# --- signing --------------------------------------------------------------


def _prepared(publisher: XPublisher, text: str = "a post") -> requests.PreparedRequest:
    """Run the real oauthlib signing over a request, without sending it."""
    return publisher.session.prepare_request(
        requests.Request("POST", publisher.endpoint, json={"text": text}, auth=publisher.auth)
    )


def _auth_header(prepared: requests.PreparedRequest) -> str:
    """oauthlib sets the header as bytes, which requests accepts as-is."""
    header = prepared.headers["Authorization"]
    return header.decode() if isinstance(header, bytes) else header


def test_the_request_is_signed_in_the_authorization_header():
    header = _auth_header(_prepared(XPublisher(**REFERENCE)))

    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="example-api-key-not-a-real-credential"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header
    assert 'oauth_version="1.0"' in header
    assert "oauth_signature=" in header
    assert "oauth_nonce=" in header and "oauth_timestamp=" in header


def test_the_secrets_never_appear_in_the_request():
    prepared = _prepared(XPublisher(**REFERENCE))
    serialised = "{}{}{}".format(prepared.headers, prepared.body, _auth_header(prepared))
    assert REFERENCE["api_secret"] not in serialised
    assert REFERENCE["access_token_secret"] not in serialised


def test_the_json_body_is_not_part_of_the_signature():
    """The v2 endpoint takes JSON, so only the oauth_* parameters are signed.

    Pinned by holding the nonce and timestamp still: two different bodies must
    produce the same signature. If oauthlib ever started signing the body, or
    the body were sent form-encoded by mistake, this fails.
    """
    from requests_oauthlib import OAuth1

    def sign(text: str) -> str:
        auth = OAuth1(
            client_key=REFERENCE["api_key"],
            client_secret=REFERENCE["api_secret"],
            resource_owner_key=REFERENCE["access_token"],
            resource_owner_secret=REFERENCE["access_token_secret"],
            nonce="fixed-nonce-for-this-test",
            timestamp="1318622958",
        )
        prepared = requests.Session().prepare_request(
            requests.Request(
                "POST", "https://api.x.com/2/tweets", json={"text": text}, auth=auth
            )
        )
        return _auth_header(prepared)

    assert sign("one post") == sign("a completely different post")


def test_the_nonce_changes_between_requests():
    publisher = XPublisher(**REFERENCE)
    first = _auth_header(_prepared(publisher))
    second = _auth_header(_prepared(publisher))
    assert first != second


def test_missing_credentials_fail_loudly():
    with pytest.raises(PublishError, match="X_ACCESS_TOKEN"):
        XPublisher("key", "secret", "", "")


def test_credentials_are_not_on_the_publisher_object():
    publisher = XPublisher(**REFERENCE)
    assert REFERENCE["api_secret"] not in repr(publisher)
    assert REFERENCE["api_secret"] not in repr(vars(publisher))


# --- publishing -----------------------------------------------------------


def test_a_successful_post_returns_the_platform_id():
    publisher = XPublisher(**REFERENCE)
    captured = stub_session(
        publisher, FakeResponse(201, {"data": {"id": "1799", "text": "a post"}})
    )

    assert publisher.publish("a post") == "1799"
    assert captured["url"] == "https://api.x.com/2/tweets"
    assert captured["json"] == {"text": "a post"}
    assert captured["timeout"] == 30
    assert captured["auth"] is publisher.auth


def test_an_http_error_becomes_a_publish_error_with_the_body():
    publisher = XPublisher(**REFERENCE)
    stub_session(publisher, FakeResponse(429, text="rate limit exceeded"))

    with pytest.raises(PublishError, match="429.*rate limit exceeded"):
        publisher.publish("a post")


def test_an_unreachable_platform_becomes_a_publish_error():
    publisher = XPublisher(**REFERENCE)
    stub_session(publisher, raise_with=requests.ConnectionError("connection refused"))

    with pytest.raises(PublishError, match="could not reach X"):
        publisher.publish("a post")


def test_a_timeout_becomes_a_publish_error():
    publisher = XPublisher(**REFERENCE)
    stub_session(publisher, raise_with=requests.Timeout("timed out"))

    with pytest.raises(PublishError, match="could not reach X"):
        publisher.publish("a post")


def test_a_non_json_response_becomes_a_publish_error():
    publisher = XPublisher(**REFERENCE)
    stub_session(publisher, FakeResponse(200, None, text="<html>gateway timeout</html>"))

    with pytest.raises(PublishError, match="not JSON"):
        publisher.publish("a post")


def test_a_response_without_an_id_becomes_a_publish_error():
    publisher = XPublisher(**REFERENCE)
    stub_session(publisher, FakeResponse(201, {"data": {}}))

    with pytest.raises(PublishError, match="no post id"):
        publisher.publish("a post")


# --- mock and selection ---------------------------------------------------


def test_mock_publisher_records_what_it_sent(tmp_path):
    publisher = MockPublisher(path=tmp_path / "published.log")
    post_id = publisher.publish("a post that a human approved")
    entry = json.loads((tmp_path / "published.log").read_text(encoding="utf-8"))
    assert entry["id"] == post_id
    assert entry["text"] == "a post that a human approved"


def test_post_url_only_links_real_posts():
    assert post_url("1234567890") == "https://x.com/i/web/status/1234567890"
    assert post_url("mock-1-1") is None
    assert post_url(None) is None


def _settings(**overrides) -> Settings:
    base = dict(
        config_path="config/content.yaml",
        db_path="data/x.db",
        hermes_mode="mock",
        hermes_endpoint="",
        hermes_api_key="",
        publisher="mock",
        ui_username="",
        ui_password="",
    )
    base.update(overrides)
    return Settings(**base)


def test_unknown_publisher_fails_loudly():
    with pytest.raises(PublishError, match="expected 'mock' or 'x'"):
        get_publisher(_settings(publisher="whatever"))
