import hashlib
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from littleuqu.download import MediaError, direct, hls


@pytest.fixture
def server():
    payloads = {}
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append((self.path, dict(self.headers)))
            value = payloads.get(self.path.split("?", 1)[0])
            if value is None:
                self.send_error(404)
                return
            status = 200
            body = value
            if self.headers.get("Range") and self.path.startswith("/resume"):
                offset = int(self.headers["Range"].split("=")[1].split("-")[0])
                status = 206
                body = value[offset:]
            self.send_response(status)
            self.send_header("ETag", '"version1"')
            self.send_header("Content-Length", str(len(body)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {offset}-{len(value) - 1}/{len(value)}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", payloads, requests_seen
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.mark.parametrize("endpoint", ["/resume.bin", "/ignore-range.bin"])
def test_direct_resume_and_server_ignores_range(tmp_path, server, endpoint):
    base, payloads, seen = server
    data = b"hello world!" * 500
    payloads[endpoint] = data
    target = tmp_path / "asset.bin"
    url = base + endpoint
    (tmp_path / "asset.bin.part").write_bytes(data[:50])
    (tmp_path / "asset.bin.part.json").write_text(
        json.dumps({"source": hashlib.sha256(url.encode()).hexdigest(), "validator": '"version1"'})
    )
    assert direct(url, target) == "downloaded"
    assert target.read_bytes() == data
    assert seen[0][1]["Range"] == "bytes=50-"
    assert "token" not in seen[0][1]
    assert direct(url, target) == "skipped"
    assert len(seen) == 1


def test_http_error_never_becomes_completed_file(tmp_path, server):
    base, _, _ = server
    target = tmp_path / "missing.mp3"
    with pytest.raises(MediaError):
        direct(base + "/missing", target)
    assert not target.exists()


def test_encrypted_hls_merge_and_resume(tmp_path, server):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    base, payloads, seen = server
    ts = tmp_path / "source.ts"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x32:r=5",
            "-t",
            "1",
            "-c:v",
            "mpeg2video",
            "-f",
            "mpegts",
            str(ts),
        ],
        check=True,
        capture_output=True,
    )
    key = b"0123456789abcdef"
    payloads["/key"] = key
    # 默认 IV 来自 media sequence，第二片使用显式 IV 与换 key。
    second_key = b"fedcba9876543210"
    payloads["/key2"] = second_key
    payloads["/0.ts"] = AES.new(key, AES.MODE_CBC, (8).to_bytes(16, "big")).encrypt(
        pad(ts.read_bytes(), 16)
    )
    encrypted_second = AES.new(second_key, AES.MODE_CBC, (42).to_bytes(16, "big")).encrypt(
        pad(ts.read_bytes(), 16)
    )
    payloads["/list.m3u8"] = b"""#EXTM3U
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:8
#EXT-X-KEY:METHOD=AES-128,URI="key"
#EXTINF:1,
0.ts
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=AES-128,URI="key2",IV=0x2a
#EXTINF:1,
1.ts
#EXT-X-ENDLIST
"""
    target = tmp_path / "result.mp4"
    with pytest.raises(MediaError):
        hls(base + "/list.m3u8?auth_key=first", target, jobs=1)
    assert not target.exists()
    assert (tmp_path / ".result.mp4.hls" / "000000.ts").exists()
    payloads["/1.ts"] = encrypted_second
    assert hls(base + "/list.m3u8?auth_key=second", target, jobs=1) == "downloaded"
    assert target.stat().st_size > 0
    assert sum(path == "/0.ts" for path, _ in seen) == 1
    assert hls(base + "/list.m3u8", target) == "skipped"
    assert not (tmp_path / ".result.mp4.hls").exists()
