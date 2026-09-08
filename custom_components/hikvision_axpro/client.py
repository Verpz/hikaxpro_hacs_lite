"""Bound the pinned client's HTTP calls without changing its alarm protocol."""

from datetime import datetime
from threading import RLock
from urllib.parse import quote
from xml.etree import ElementTree

import hikaxpro
import requests

HTTP_TIMEOUT = (
    3,
    5,
)  # connect, read seconds; applies to login as well as status/control


class AuthenticationError(Exception):
    """The panel rejected authentication."""


class LiteHikAxPro(hikaxpro.HikAxPro):
    """Keep upstream commands/encoding, with finite retries and serialized I/O."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_lock = RLock()

    def get_session_params(self):
        response = requests.get(
            f"http://{self.host}{hikaxpro.consts.Endpoints.Session_Capabilities}{quote(self.username)}",
            headers={"X-Userlevel": str(self.user_level)},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return self.parse_session_response(response.text)

    def connect(self):
        with self._request_lock:
            self._cookie = None
            params = self.get_session_params()
            xml = hikaxpro.xmlBuilder.serialize_object(
                hikaxpro.SessionLogin.SessionLogin(
                    params.session_id,
                    self.username,
                    self.encode_password(params),
                    params.session_id_version,
                )
            )
            response = requests.post(
                f"http://{self.host}{hikaxpro.consts.Endpoints.Session_Login}"
                f"?timeStamp={int(datetime.now().timestamp())}",
                data=xml,
                timeout=HTTP_TIMEOUT,
            )
            if response.status_code == 401:
                return False
            response.raise_for_status()
            cookie = response.headers.get("Set-Cookie")
            if cookie:
                self._cookie = cookie.split(";", 1)[0]
            else:
                root = ElementTree.fromstring(response.text)
                session_id = self._root_get_value(
                    root, {"xmlns": hikaxpro.consts.XML_SCHEMA}, "xmlns:sessionID"
                )
                if session_id:
                    self._cookie = "WebSession=" + session_id
            return self._cookie is not None

    def make_request(self, endpoint, method, data=None, is_json=False):
        with self._request_lock:
            for attempt in range(2):
                headers = {"Cookie": self._cookie}
                if self.user_level is not None:
                    headers["X-Userlevel"] = str(self.user_level)
                response = requests.request(
                    method,
                    endpoint,
                    headers=headers,
                    timeout=HTTP_TIMEOUT,
                    **({"json": data} if is_json else {"data": data}),
                )
                if response.status_code != 401:
                    response.raise_for_status()
                    return response
                self._cookie = None
                if attempt or not self.connect():
                    raise AuthenticationError("AX Pro authentication failed")
            raise AuthenticationError("AX Pro authentication failed")
