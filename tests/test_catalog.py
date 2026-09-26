import base64
import time

import pytest
from typer.testing import CliRunner

from littleuqu.api import API, AuthError, capture_headers
from littleuqu.catalog import Catalog, decode_url, playback_urls
from littleuqu.cli import app
from littleuqu.config import UquError, config_dir, read_json, scrub, write_json


class FakeAPI:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def request(self, path, params=None):
        self.calls.append((path, params))
        return next(self.pages)


def test_pagination_and_audio_id_namespaces():
    api = FakeAPI(
        [
            {
                "data": [{"id": 1, "rssType": "A"}, {"id": 1, "rssType": "B"}],
                "page": {"hasMore": True},
            },
            {
                "data": [{"id": 1, "rssType": "A"}, {"id": 2, "rssType": "A"}],
                "page": {"hasMore": False},
            },
        ]
    )
    assert len(list(Catalog(api).items("熏听", all_pages=True))) == 3
    assert [c[1]["page.offset"] for c in api.calls] == [1, 2]


def test_repeat_page_is_error():
    page = {"data": [{"ipId": 1}], "page": {"hasMore": True}}
    with pytest.raises(UquError, match="重复页"):
        list(Catalog(FakeAPI([page, page])).items("动画", all_pages=True))


def test_filter_names_and_values():
    cat = Catalog(
        FakeAPI(
            [
                {
                    "data": [
                        {
                            "name": "语言",
                            "key": "filterLang",
                            "conditions": [{"type": 2, "name": "英文"}],
                        }
                    ]
                }
            ]
        )
    )
    assert cat.filters("动画", ["语言=英文"]) == {"filterLang": 2}


def test_url_decode_and_preview():
    url = "https://cdn.example/a.m3u8?auth_key=test"
    encoded = base64.b64encode(url.encode()).decode()
    assert decode_url(encoded) == url
    assert playback_urls({"playUrl": encoded, "cdnList": [{"playUrl": encoded}]}) == [url]
    with pytest.raises(UquError):
        playback_urls({"playUrl": encoded, "preview": True})
    with pytest.raises(UquError):
        decode_url(base64.b64encode(b"file:///etc/passwd").decode())
    assert scrub({"playUrl": encoded}) == {"playUrl": "<redacted>"}


def test_capture_import_private(tmp_path, monkeypatch):
    monkeypatch.setenv("LITTLEUQU_CONFIG_DIR", str(tmp_path / "config"))
    capture = tmp_path / "sample.md"
    capture.write_text(
        'curl -H "token: secret" -H "Host: evil.example" -H "chdId: 123" "https://fastapi.ukids.cn/"'
    )
    assert "Host" not in capture_headers(capture)
    api = API()
    api.import_capture(capture)
    assert api.path.stat().st_mode & 0o777 == 0o600
    assert API().headers["chdId"] == "123"


def test_cli_no_network():
    runner = CliRunner()
    result = runner.invoke(app, ["categories"])
    assert result.exit_code == 0
    assert "熏听" in result.stdout
    assert runner.invoke(app, ["download", "--help"]).exit_code == 0


def test_movie_api_mapping():
    api = FakeAPI([{"data": {"id": 116}}, {"data": {"format": "m3u8"}}])
    cat = Catalog(api)
    assert cat.movie(116)["id"] == 116
    assert cat.play_movie(116)["format"] == "m3u8"
    assert api.calls == [
        ("/coreapp/film/detail/v2", {"filmDramaId": 116}),
        (
            "/coreapp/play/video/V9/online",
            {"sType": 0, "definition": "SD", "id": 116, "type": 51, "lang": 2, "pure": 3},
        ),
    ]


def test_listen_vd_content_and_play_mapping():
    api = FakeAPI([{"data": [{"id": 11281}]}, {"data": {"format": "mp4"}}])
    cat = Catalog(api)
    album = {"id": 382, "rssType": "LISTEN_VD"}
    assert cat.listen_content(album) == [{"id": 11281}]
    track = {"id": 11281, "rssType": "LISTEN_VD"}
    assert cat.play_listen(track)["format"] == "mp4"
    assert api.calls == [
        (
            "/coreapp/listen/content/list",
            {"rssType": "LISTEN_VD", "vdId": 382, "page.offset": 0, "page.limit": 0},
        ),
        (
            "/coreapp/play/video/V9/online",
            {"sType": 0, "definition": "FD", "id": 11281, "type": 1, "lang": -1, "pure": 3},
        ),
    ]


def test_listen_film_reuses_movie_detail():
    detail = {
        "id": 116,
        "cnName": "电影",
        "listenImg": "https://cdn.example/cover.png",
        "subtitleUrl": "https://cdn.example/subtitle.srt",
        "isDownload": 1,
    }
    api = FakeAPI([{"data": detail}])
    track = Catalog(api).listen_content({"id": 116, "rssType": "LISTEN_FILM"})[0]
    assert track["rssType"] == "LISTEN_FILM"
    assert track["movieDetail"] == detail


def test_listen_ad_content_and_play_mapping():
    api = FakeAPI([{"data": [{"id": 918, "title": "Hello"}]}, {"data": {"format": "mp3"}}])
    cat = Catalog(api)
    tracks = cat.listen_content({"id": 65, "rssType": "LISTEN_AD"})
    assert tracks == [{"id": 918, "title": "Hello", "name": "Hello", "rssType": "LISTEN_AD"}]
    assert cat.play_listen(tracks[0], quality="OD", lang=0)["format"] == "mp3"
    assert api.calls == [
        ("/coreapp/songList", {"audioInfoId": 65, "vip": 1}),
        (
            "/coreapp/play/audio/V8/online",
            {"sType": 0, "id": 918, "type": 4, "lang": 0, "pure": 0},
        ),
    ]
    with pytest.raises(UquError, match="原始音质 OD"):
        cat.play_listen(tracks[0], quality="FD", lang=0)


def test_login_payload_and_saved_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LITTLEUQU_CONFIG_DIR", str(tmp_path))
    api = API()
    called = []

    def request(path, **kwargs):
        called.append((path, kwargs))
        return {
            "data": {
                "token": {"token": "test-secret", "refreshToken": "test-refresh", "expires": 18000}
            }
        }

    monkeypatch.setattr(api, "request", request)
    api.login("10000000000", "1234")
    assert called[0][1]["body"] == {"mobile": "10000000000", "verifyCode": "1234"}
    assert API().headers["token"] == "test-secret"
    assert "1234" not in api.path.read_text()


class FakeResponse:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return {"success": True, "data": {}}


def session_with(tmp_path, monkeypatch, status=200, **state):
    """构造带指定会话状态和固定 HTTP 状态码的 API，返回 (api, 已请求 URL 列表)。"""
    monkeypatch.setenv("LITTLEUQU_CONFIG_DIR", str(tmp_path))
    write_json(tmp_path / "session.json", {"token": "t", **state})
    api = API()
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return FakeResponse(status)

    monkeypatch.setattr(api.session, "request", fake_request)
    return api, calls


def test_expired_session_rejected_before_request(tmp_path, monkeypatch):
    api, calls = session_with(
        tmp_path, monkeypatch, expires=18000, login_at=time.time() - 18001
    )
    with pytest.raises(AuthError, match="有效期"):
        api.request("/ucapp/getUser")
    assert calls == []


def test_fresh_session_passes_precheck(tmp_path, monkeypatch):
    api, calls = session_with(tmp_path, monkeypatch, expires=18000, login_at=time.time())
    assert api.request("/ucapp/getUser") == {"success": True, "data": {}}
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_status_codes_are_distinguished(tmp_path, monkeypatch, status):
    api, _ = session_with(tmp_path, monkeypatch, status=status)
    with pytest.raises(UquError) as info:
        api.request("/coreapp/play/video/V9/online")
    assert isinstance(info.value, AuthError) == (status == 401)
    assert f"HTTP {status}" in str(info.value)


def test_import_capture_clears_stale_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("LITTLEUQU_CONFIG_DIR", str(tmp_path / "config"))
    api = API()
    api.state.update(token="old", expires=18000, login_at=time.time() - 99999)
    capture = tmp_path / "sample.md"
    capture.write_text('curl -H "token: fresh" "https://fastapi.ukids.cn/x"')
    api.import_capture(capture)
    state = read_json(api.path)
    assert state["token"] == "fresh"
    assert "expires" not in state and "login_at" not in state
    # 新 token 不应被旧会话的过期记录误判。
    assert API().expired() is False


def test_import_login_response_records_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("LITTLEUQU_CONFIG_DIR", str(tmp_path / "config"))
    capture = tmp_path / "login.md"
    capture.write_text(
        'curl "https://fastapi.ukids.cn/ucapp/mobileLogin"\n\n'
        '{"success": true, "data": {"token":'
        ' {"token": "tk", "refreshToken": "rt", "expires": 18000}}}'
    )
    API().import_capture(capture)
    state = read_json(config_dir() / "session.json")
    assert state["expires"] == 18000
    assert state["refresh_token"] == "rt"
    assert API().expired() is False
