from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import m3u8
import requests

from .config import UquError, ca_bundle, read_json, write_json


class MediaError(UquError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def get(url, **kwargs):
    """媒体请求不携带 API 会话凭据；有限重试，不记录签名 URL。"""
    for attempt in range(4):
        try:
            response = requests.get(url, timeout=(15, 90), verify=ca_bundle(), **kwargs)
            if response.status_code in (429, 500, 502, 503, 504):
                response.close()
                if attempt < 3:
                    time.sleep(0.5 * 2**attempt)
                    continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if isinstance(exc, requests.HTTPError) or attempt == 3:
                status = exc.response.status_code if exc.response is not None else None
                raise MediaError(
                    f"媒体请求失败（{type(exc).__name__}），可重新运行续传", status
                ) from exc
            time.sleep(0.5 * 2**attempt)
    raise MediaError("媒体请求失败")


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise UquError("需要 FFmpeg，请安装后重试（macOS: brew install ffmpeg）")


def ffmpeg(args):
    require_ffmpeg()
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
    )
    if result.returncode:
        raise MediaError("FFmpeg 合并/提取失败，保留临时文件供重试")


def completed(path: Path):
    state = read_json(Path(str(path) + ".done.json"))
    return path.is_file() and path.stat().st_size > 0 and state.get("size") == path.stat().st_size


def mark_done(path):
    write_json(Path(str(path) + ".done.json"), {"size": path.stat().st_size})


def _hls_work_dir(path: Path) -> Path:
    # FFmpeg parses '#' and '?' in local input paths as URL delimiters. Keep the
    # work directory ASCII-only while retaining a stable cache per output file.
    digest = hashlib.sha256(path.name.encode()).hexdigest()[:16]
    return path.parent / f".littleuqu-hls-{digest}"


def direct(url: str, path: Path, overwrite=False):
    if completed(path) and not overwrite:
        return "skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(path) + ".part")
    meta_path = Path(str(path) + ".part.json")
    meta = read_json(meta_path)
    source = hashlib.sha256(url.split("?", 1)[0].encode()).hexdigest()
    size = partial.stat().st_size if partial.exists() and not overwrite else 0
    # 无验证器时从头下载，避免混接被替换的资源。
    if meta.get("source") != source or not meta.get("validator"):
        size = 0
    headers = {"Accept-Encoding": "identity", "User-Agent": "okhttp/3.12.8"}
    if size:
        headers.update(Range=f"bytes={size}-", **{"If-Range": meta["validator"]})
    try:
        response = get(url, headers=headers, stream=True)
    except MediaError as exc:
        if size and exc.status == 416:
            # 完整 partial 或远端文件变短时，重新获取并校验，不循环请求无效范围。
            headers.pop("Range")
            headers.pop("If-Range")
            size = 0
            response = get(url, headers=headers, stream=True)
        else:
            raise
    try:
        with response:
            expected = None
            if response.status_code == 206:
                match = re.fullmatch(
                    r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
                )
                if not match or int(match[1]) != size:
                    raise MediaError("服务器返回错误的续传范围")
                expected = int(match[3])
                mode = "ab" if size else "wb"
            elif response.status_code == 200:
                mode, size = "wb", 0
                if response.headers.get("Content-Length"):
                    expected = int(response.headers["Content-Length"])
            else:
                raise MediaError("媒体服务器返回了非下载响应")
            validator = response.headers.get("ETag") or response.headers.get("Last-Modified")
            write_json(meta_path, {"source": source, "validator": validator})
            with partial.open(mode) as f:
                for chunk in response.iter_content(256 * 1024):
                    if chunk:
                        f.write(chunk)
            if partial.stat().st_size == 0 or (
                expected is not None and partial.stat().st_size != expected
            ):
                raise MediaError("下载长度不匹配，已保留临时文件")
    except requests.RequestException as exc:
        raise MediaError("下载流中断，已保留临时文件供续传") from exc
    partial.replace(path)
    mark_done(path)
    meta_path.unlink(missing_ok=True)
    return "downloaded"


def hls(url: str, path: Path, jobs=4, overwrite=False, progress=None):
    if completed(path) and not overwrite:
        return "skipped"
    require_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        with get(url) as response:
            playlist = m3u8.loads(response.text, uri=response.url)
            url = response.url
        if not playlist.is_variant:
            break
        if not playlist.playlists:
            raise MediaError("HLS 主列表为空")
        variant = max(playlist.playlists, key=lambda p: p.stream_info.bandwidth or 0)
        if variant.stream_info.audio:
            raise MediaError("此 HLS 使用独立音轨，目前需补充样例后适配")
        url = urljoin(url, variant.uri)
    else:
        raise MediaError("HLS 嵌套层数过多")
    if not playlist.segments or not playlist.is_endlist:
        raise MediaError("不是完整的点播 HLS 列表")
    if any(s.byterange or s.init_section for s in playlist.segments):
        raise MediaError("此 HLS 使用 byte-range/fMP4，目前需补充样例后适配")
    # 签名更新时 URL 查询串变化不影响同一资源的分片缓存。
    manifest = [
        (
            urlsplit(urljoin(url, s.uri)).path,
            s.duration,
            s.key.method if s.key else None,
            urlsplit(urljoin(url, s.key.uri)).path if s.key and s.key.uri else None,
            s.key.iv if s.key else None,
        )
        for s in playlist.segments
    ]
    signature = hashlib.sha256(repr((playlist.media_sequence, manifest)).encode()).hexdigest()
    work = _hls_work_dir(path)
    legacy_work = path.parent / ("." + path.name + ".hls")
    if legacy_work.exists() and not work.exists():
        legacy_work.replace(work)
    if work.exists() and (
        overwrite or read_json(work / "state.json").get("signature") != signature
    ):
        shutil.rmtree(work)
    work.mkdir(exist_ok=True)
    write_json(work / "state.json", {"signature": signature})
    keys = {}
    for seg in playlist.segments:
        key = seg.key
        if key and key.method != "NONE":
            if key.method != "AES-128" or (key.keyformat and key.keyformat != "identity"):
                raise MediaError(f"不支持此 HLS 加密方式：{key.method}")
            uri = urljoin(url, key.uri)
            if uri not in keys:
                with get(uri) as response:
                    keys[uri] = response.content
                if len(keys[uri]) != 16:
                    raise MediaError("HLS AES 密钥长度无效")

    def segment(index):
        seg = playlist.segments[index]
        target = work / f"{index:06d}.ts"
        if completed(target):
            return
        with get(urljoin(url, seg.uri)) as response:
            data = response.content
        if not data:
            raise MediaError("HLS 分片为空")
        if seg.key and seg.key.method == "AES-128":
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import unpad

            try:
                iv = (
                    int(seg.key.iv, 16) if seg.key.iv else playlist.media_sequence + index
                ).to_bytes(16, "big")
                data = unpad(
                    AES.new(keys[urljoin(url, seg.key.uri)], AES.MODE_CBC, iv).decrypt(data), 16
                )
            except (ValueError, OverflowError) as exc:
                raise MediaError("HLS 分片解密失败") from exc
        temp = target.with_suffix(".part")
        temp.write_bytes(data)
        temp.replace(target)
        mark_done(target)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(segment, i) for i in range(len(playlist.segments))]
        for count, future in enumerate(as_completed(futures), 1):
            future.result()
            if progress:
                progress(count, len(futures))
    local = work / "local.m3u8"
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{playlist.target_duration or 60}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i, seg in enumerate(playlist.segments):
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.extend([f"#EXTINF:{seg.duration},", f"{i:06d}.ts"])
    lines.append("#EXT-X-ENDLIST")
    local.write_text("\n".join(lines) + "\n")
    temp = path.with_name(path.stem + ".part" + path.suffix)
    ffmpeg(["-i", str(local), "-c", "copy", str(temp)])
    if not temp.exists() or not temp.stat().st_size:
        raise MediaError("FFmpeg 未生成有效文件")
    temp.replace(path)
    mark_done(path)
    shutil.rmtree(work)
    return "downloaded"


def media(url, path, jobs=4, overwrite=False, is_hls=False, progress=None):
    if is_hls or urlsplit(url).path.lower().endswith(".m3u8"):
        return hls(url, path, jobs, overwrite, progress)
    return direct(url, path, overwrite)


def extract_audio(source, target, overwrite=False):
    if completed(target) and not overwrite:
        return "skipped"
    temp = target.with_name(target.stem + ".part.m4a")
    ffmpeg(["-i", str(source), "-vn", "-c:a", "copy", str(temp)])
    if not temp.exists() or not temp.stat().st_size:
        raise MediaError("未提取到音轨")
    temp.replace(target)
    mark_done(target)
    return "downloaded"
