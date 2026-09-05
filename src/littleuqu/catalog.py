from __future__ import annotations

import base64
import binascii
from urllib.parse import urlparse

from .config import UquError

KINDS = {
    "电影": (
        "/coreapp/library/filmCon",
        "/coreapp/library/film",
        {"level": 0, "filterLang": 0, "type": 0},
    ),
    "动画": (
        "/coreapp/library/ip/con",
        "/coreapp/library/ip/list",
        {"applyAgeN": 0, "level": 0, "vipType": 2, "filterLang": 0, "vip": 1, "subjectType": 0},
    ),
    "熏听": (
        "/coreapp/library/chdSongCon",
        "/coreapp/library/audioSong/v2",
        {"applyAgeN": 0, "audioType": 0, "vipType": 2, "filterLang": 0, "type": 0, "vip": 1},
    ),
}
ALIASES = {"movie": "电影", "animation": "动画", "audio": "熏听"}


def kind_name(kind):
    kind = ALIASES.get(kind, kind)
    if kind not in KINDS:
        raise UquError("类别应为 电影、动画、熏听（或 movie、animation、audio）")
    return kind


def identity(kind, item):
    # 熏听不同 rssType 可能共享数字 ID。
    return f"{item.get('rssType', '')}:{item.get('ipId') if kind == '动画' else item.get('id')}"


def item_id(kind, item):
    return int(item["ipId"] if kind == "动画" else item["id"])


def title(item):
    return (
        item.get("name")
        or item.get("title")
        or item.get("cnName")
        or item.get("enName")
        or str(item.get("id"))
    )


class Catalog:
    def __init__(self, api):
        self.api = api

    def categories(self, kind):
        kind = kind_name(kind)
        path = KINDS[kind][0]
        return self.api.request(path, params={"filterLang": 0} if kind == "熏听" else None)["data"]

    def filters(self, kind, values):
        result = {}
        categories = self.categories(kind) if values else []
        for value in values:
            key, sep, raw = value.partition("=")
            if not sep:
                raise UquError(f"筛选格式应为 key=value：{value}")
            group = next((x for x in categories if key in (x["key"], x["name"])), None)
            if not group:
                raise UquError(f"未知筛选项：{key}，请查看 categories")
            option = next(
                (x for x in group["conditions"] if raw in (str(x["type"]), x["name"])), None
            )
            if raw in ("0", "全部"):
                result[group["key"]] = 0
            elif option:
                result[group["key"]] = option["type"]
            else:
                raise UquError(f"筛选值无效：{value}")
        return result

    def items(self, kind, filters=None, all_pages=False, page=1, limit=16):
        kind = kind_name(kind)
        _, path, defaults = KINDS[kind]
        seen = set()
        page_signatures = set()
        for offset in range(page, page + 10000):
            result = self.api.request(
                path,
                params={**defaults, **(filters or {}), "page.offset": offset, "page.limit": limit},
            )
            items = result.get("data") or []
            if not isinstance(items, list):
                raise UquError("列表接口返回了非列表数据")
            signature = tuple(identity(kind, x) for x in items)
            if signature and signature in page_signatures:
                raise UquError("接口返回重复页，已停止；需验证 page.offset 分页语义")
            page_signatures.add(signature)
            for item in items:
                key = identity(kind, item)
                if key not in seen:
                    seen.add(key)
                    yield item
            pagination = result.get("page") or {}
            if not all_pages or not items or pagination.get("hasMore") is False:
                return
            if "hasMore" not in pagination and len(items) < limit:
                return
        raise UquError("超过分页安全上限")

    def animation(self, ip_id):
        return self.api.request("/coreapp/v2/ip/pdf", params={"ipId": ip_id})["data"]

    def movie(self, film_id):
        return self.api.request("/coreapp/film/detail/v2", params={"filmDramaId": film_id})["data"]

    def listen_content(self, album):
        rss_type = album.get("rssType")
        album_id = album.get("id")
        if rss_type == "LISTEN_VD":
            result = self.api.request(
                "/coreapp/listen/content/list",
                params={
                    "rssType": rss_type,
                    "vdId": album_id,
                    "page.offset": 0,
                    "page.limit": 0,
                },
            )
            return result.get("data") or []
        if rss_type == "LISTEN_FILM":
            detail = self.movie(album_id)
            return [
                {
                    "rssType": rss_type,
                    "id": detail["id"],
                    "name": detail.get("cnName") or detail.get("enName"),
                    "img": detail.get("listenImg") or detail.get("coverUrl"),
                    "subtitleUrl": detail.get("subtitleUrl"),
                    "descp": detail.get("descp"),
                    "lang": detail.get("lang"),
                    "download": bool(detail.get("isDownload")),
                    "movieDetail": detail,
                }
            ]
        if rss_type == "LISTEN_AD":
            result = self.api.request(
                "/coreapp/songList", params={"audioInfoId": album_id, "vip": 1}
            )
            return [
                {**track, "name": title(track), "rssType": rss_type}
                for track in (result.get("data") or [])
            ]
        raise UquError(f"尚未适配熏听资源类型 {rss_type!r}，请补充其内容列表与播放抓包")

    def play(self, resource_id, play_type, quality="SD", lang=-1):
        return self.api.request(
            "/coreapp/play/video/V9/online",
            params={
                "sType": 0,
                "definition": quality,
                "id": resource_id,
                "type": play_type,
                "lang": lang,
                "pure": 3,
            },
        )["data"]

    def play_animation(self, episode_id, quality="SD", lang=-1):
        return self.play(episode_id, 1, quality, lang)

    def play_movie(self, film_id, quality="SD", lang=2):
        return self.play(film_id, 51, quality, lang)

    def play_listen(self, track, quality="FD", lang=-1):
        rss_type = track.get("rssType")
        if rss_type == "LISTEN_VD":
            return self.play(track["id"], 1, quality, lang)
        if rss_type == "LISTEN_FILM":
            return self.play(track["id"], 51, quality, lang)
        if rss_type == "LISTEN_AD":
            if quality != "OD":
                raise UquError("LISTEN_AD 抓包仅支持原始音质 OD；请省略 --quality 或指定 OD")
            return self.api.request(
                "/coreapp/play/audio/V8/online",
                params={"sType": 0, "id": track["id"], "type": 4, "lang": lang, "pure": 0},
            )["data"]
        raise UquError(f"尚未适配熏听资源类型 {rss_type!r} 的播放接口")


def decode_url(value):
    if not value:
        return None
    if not value.startswith(("https://", "http://")):
        try:
            value = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise UquError("播放地址既不是 HTTP URL，也不是有效 Base64 URL") from exc
    if urlparse(value).scheme not in ("https", "http") or not urlparse(value).netloc:
        raise UquError("播放地址无效")
    return value


def playback_urls(data):
    urls = []
    for value in [data.get("playUrl")] + [x.get("playUrl") for x in data.get("cdnList", [])]:
        url = decode_url(value)
        if url and url not in urls:
            urls.append(url)
    if not urls:
        raise UquError("播放响应没有可下载直链")
    if data.get("preview"):
        raise UquError("当前接口仅返回试看资源，未标记为完整下载")
    return urls
