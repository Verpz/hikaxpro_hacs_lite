"""Regression tests for bounded login/status/control transport."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from custom_components.hikvision_axpro.client import (
    HTTP_TIMEOUT,
    AuthenticationError,
    LiteHikAxPro,
)


def response(status=200, text="", headers=None):
    result = Mock(status_code=status, text=text, headers=headers or {})
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


@pytest.mark.parametrize("login_ok", [True, False])
def test_401_retries_once(login_ok):
    client = LiteHikAxPro("panel", "user", "password")
    with (
        patch("requests.request", return_value=response(401)) as request,
        patch.object(client, "connect", return_value=login_ok) as login,
    ):
        with pytest.raises(AuthenticationError):
            client.subsystem_status()
        assert request.call_count == (2 if login_ok else 1)
        login.assert_called_once()
        assert all(c.kwargs["timeout"] == HTTP_TIMEOUT for c in request.call_args_list)


def test_expired_session_recovers():
    client = LiteHikAxPro("panel", "user", "password")
    with (
        patch("requests.request", side_effect=[response(401), response()]) as request,
        patch.object(client, "connect", return_value=True) as login,
    ):
        client.arm_home(2)
        login.assert_called_once()
        assert request.call_count == 2
        assert request.call_args.args[0] == "PUT"
        assert "/arm/2?ways=stay" in request.call_args.args[1]


@pytest.mark.parametrize(
    "status,cookie,success", [(401, None, False), (200, "WebSession=abc; Path=/", True)]
)
def test_login_has_timeouts(status, cookie, success):
    client = LiteHikAxPro("panel", "user", "password")
    params = SimpleNamespace(session_id="id", session_id_version="2")
    with (
        patch("requests.get", return_value=response()) as get,
        patch(
            "requests.post",
            return_value=response(
                status, headers={"Set-Cookie": cookie} if cookie else {}
            ),
        ) as post,
        patch.object(client, "parse_session_response", return_value=params),
        patch.object(client, "encode_password", return_value="encoded"),
    ):
        assert client.connect() is success
        assert get.call_args.kwargs["timeout"] == HTTP_TIMEOUT
        assert post.call_args.kwargs["timeout"] == HTTP_TIMEOUT
        assert client._cookie == ("WebSession=abc" if success else None)


@pytest.mark.parametrize(
    "operation", ["subsystem_status", "arm_home", "arm_away", "disarm"]
)
def test_transport_timeout_is_not_retried(operation):
    client = LiteHikAxPro("panel", "user", "password")
    with (
        patch("requests.request", side_effect=requests.ReadTimeout) as request,
        patch.object(client, "connect") as login,
    ):
        with pytest.raises(requests.ReadTimeout):
            getattr(client, operation)()
        request.assert_called_once()
        login.assert_not_called()


def test_session_capability_failure_stops_before_login():
    client = LiteHikAxPro("panel", "user", "password")
    with (
        patch("requests.get", side_effect=requests.ConnectTimeout) as get,
        patch("requests.post") as post,
    ):
        with pytest.raises(requests.ConnectTimeout):
            client.connect()
        get.assert_called_once()
        post.assert_not_called()


def test_login_post_timeout_stops_without_retry():
    client = LiteHikAxPro("panel", "user", "password")
    params = SimpleNamespace(session_id="id", session_id_version="2")
    with (
        patch.object(client, "get_session_params", return_value=params),
        patch.object(client, "encode_password", return_value="encoded"),
        patch("requests.post", side_effect=requests.ReadTimeout) as post,
    ):
        with pytest.raises(requests.ReadTimeout):
            client.connect()
        post.assert_called_once()
        assert client._cookie is None


@pytest.mark.parametrize("cookie_in_header", [True, False])
def test_no_cookie_401_login_retry(cookie_in_header):
    """Exercise real request preparation, login XML and cookie reuse from cold start."""
    client = LiteHikAxPro("panel", "user", "password", user_level=1)
    assert client._cookie is None
    capabilities = """<SessionLoginCap xmlns="http://www.hikvision.com/ver20/XMLSchema">
        <sessionID>challenge-session</sessionID><challenge>challenge</challenge>
        <salt>salt</salt><isIrreversible>true</isIrreversible>
        <iterations>2</iterations><sessionIDVersion>2</sessionIDVersion>
        </SessionLoginCap>"""
    login_xml = """<SessionLogin xmlns="http://www.hikvision.com/ver20/XMLSchema">
        <sessionID>authenticated-session</sessionID></SessionLogin>"""
    status = '{"SubSysList":[{"SubSys":{"id":1,"arming":"disarm","alarm":false}}]}'
    replies = [
        (401, "", {}),
        (200, capabilities, {}),
        (
            200,
            login_xml,
            {"Set-Cookie": "WebSession=authenticated-session; Path=/"}
            if cookie_in_header
            else {},
        ),
        (200, status, {}),
    ]
    sent = []

    def send(session, request, **kwargs):
        sent.append((request, kwargs))
        index = len(sent) - 1
        assert index < len(replies), "Unexpected extra request or authentication loop"
        code, body, headers = replies[index]
        result = requests.Response()
        result.status_code = code
        result._content = body.encode()
        result.headers.update(headers)
        result.request = request
        result.url = request.url
        return result

    # Mock only the wire: connect(), parsing, password hashing and serialization run.
    with patch("requests.sessions.Session.send", autospec=True, side_effect=send):
        result = client.subsystem_status()

    assert result["SubSysList"][0]["SubSys"]["arming"] == "disarm"
    assert len(sent) == 4
    first, capabilities_request, login, retry = [item[0] for item in sent]
    assert first.method == retry.method == "GET"
    assert (
        first.url
        == retry.url
        == "http://panel/ISAPI/SecurityCP/status/subSystems?format=json"
    )
    assert "Cookie" not in first.headers
    assert capabilities_request.method == "GET"
    assert "/sessionLogin/capabilities?username=user" in capabilities_request.url
    assert login.method == "POST"
    assert "/sessionLogin?timeStamp=" in login.url
    assert "challenge-session" in login.body
    assert "<userName>user</userName>" in login.body
    assert "<password>password</password>" not in login.body
    assert retry.headers["Cookie"] == "WebSession=authenticated-session"
    assert retry.headers["X-Userlevel"] == "1"
    assert all(kwargs["timeout"] == HTTP_TIMEOUT for _, kwargs in sent)
    assert client._cookie == "WebSession=authenticated-session"
