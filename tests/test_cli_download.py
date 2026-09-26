from pathlib import Path

import pytest
from typer.testing import CliRunner

from littleuqu import cli
from littleuqu.api import AuthError
from littleuqu.config import read_json, scrub, write_json
from littleuqu.download import mark_done


@pytest.fixture
def job():
    return {
        "kind": "动画",
        "ip_id": 44,
        "ip_name": "作品",
        "season_id": 169,
        "season_name": "第一季",
        "index": 1,
        "id": 5294,
        "name": "第一集",
        "status": "pending",
    }


def finish(path):
    path.write_bytes(b"complete media")
    mark_done(path)


def unexpected(*args, **kwargs):
    pytest.fail("Completed downloads must not request playback or process media")


@pytest.fixture
def no_network(monkeypatch):
    for name in ("_play", "_download_playback", "direct", "extract_audio"):
        monkeypatch.setattr(cli, name, unexpected)


@pytest.mark.parametrize("kind", ["动画", "电影", "LISTEN_AD", "LISTEN_VD"])
def test_completed_media_skips_playback(tmp_path, job, no_network, kind):
    if kind == "电影":
        job["kind"] = kind
    elif kind.startswith("LISTEN_"):
        job.update(kind="熏听", rssType=kind, album_id=382, album_name="专辑")
    audio = job["kind"] == "熏听"
    folder, stem = cli._paths(job, tmp_path, "SD", -1)
    suffix = ".mp3" if kind == "LISTEN_AD" else ".m4a" if audio else ".mp4"
    finish(folder / f"{stem}{suffix}")
    result = cli.content_download(
        None, job, tmp_path, {"audio" if audio else "video"}, "SD", -1, 4, False
    )
    assert result["status"] == "complete"
    assert [a["status"] for a in result["assets"]] == ["skipped"]
    # Audio-only reruns do not need the intermediate source video.
    assert not (folder / f".{stem}.source.mp4").exists()


@pytest.mark.parametrize("pdf", [False, True])
def test_existing_metadata_skips_files_and_preserves_warnings(tmp_path, job, no_network, pdf):
    folder, stem = cli._paths(job, tmp_path, "SD", -1)
    data = {"subtitleUrl": "https://cdn.example/sub.srt?auth_key=expired", "pdfDownload": int(pdf)}
    finish(folder / f"{stem}.mp4")
    finish(folder / f"{stem}.srt")
    metadata = folder / f"{stem}.json"
    write_json(metadata, scrub({"item": job, "playback": data}))
    before = metadata.stat().st_mtime_ns

    result = cli.content_download(None, job, tmp_path, {"video", "files"}, "SD", -1, 4, False)

    assert result["status"] == ("partial" if pdf else "complete")
    assert bool(result["warnings"]) == pdf
    assert [a["status"] for a in result["assets"]] == ["skipped", "skipped"]
    assert metadata.stat().st_mtime_ns == before


@pytest.mark.parametrize("damage", ["missing", "truncated", "marker"])
def test_incomplete_attachment_refreshes_signed_url(tmp_path, job, monkeypatch, damage):
    folder, stem = cli._paths(job, tmp_path, "SD", -1)
    finish(folder / f"{stem}.mp4")
    subtitle = folder / f"{stem}.srt"
    finish(subtitle)
    if damage == "missing":
        subtitle.unlink()
    elif damage == "truncated":
        subtitle.write_bytes(b"short")
    else:
        Path(str(subtitle) + ".done.json").unlink()
    write_json(folder / f"{stem}.json", scrub({
        "item": job,
        "playback": {"subtitleUrl": "https://cdn.example/sub.srt?auth_key=expired"},
    }))
    fresh = "https://cdn.example/sub.srt?auth_key=fresh"
    calls = []

    def play(*args):
        calls.append("play")
        return {"subtitleUrl": fresh}

    def direct(url, path, overwrite):
        assert url == fresh
        finish(path)
        calls.append("subtitle")
        return "downloaded"

    monkeypatch.setattr(cli, "_play", play)
    monkeypatch.setattr(cli, "direct", direct)
    monkeypatch.setattr(cli, "_download_playback", unexpected)
    result = cli.content_download(None, job, tmp_path, {"video", "files"}, "SD", -1, 4, False)
    assert result["status"] == "complete"
    assert calls == ["play", "subtitle"]


@pytest.mark.parametrize("cache", [None, "broken", {"item": {}}, {"item": {}, "playback": {}}])
def test_missing_or_invalid_metadata_refreshes(tmp_path, job, monkeypatch, cache):
    folder, stem = cli._paths(job, tmp_path, "SD", -1)
    metadata = folder / f"{stem}.json"
    if cache == "broken":
        metadata.write_text("{")
    elif cache is not None:
        write_json(metadata, cache)
    calls = []
    monkeypatch.setattr(cli, "_play", lambda *args: calls.append("play") or {})
    result = cli.content_download(None, job, tmp_path, {"files"}, "SD", -1, 4, False)
    assert result["status"] == "complete"
    assert calls == ["play"]
    assert read_json(metadata)["item"] == job


def test_extracts_missing_audio_from_local_video(tmp_path, job, monkeypatch):
    folder, stem = cli._paths(job, tmp_path, "SD", -1)
    video = folder / f"{stem}.mp4"
    finish(video)
    monkeypatch.setattr(cli, "_play", unexpected)
    monkeypatch.setattr(cli, "_download_playback", unexpected)

    def extract(source, target, overwrite):
        assert source == video
        finish(target)
        return "downloaded"

    monkeypatch.setattr(cli, "extract_audio", extract)
    result = cli.content_download(None, job, tmp_path, {"video", "audio"}, "SD", -1, 4, False)
    assert [a["status"] for a in result["assets"]] == ["skipped", "downloaded"]


@pytest.mark.parametrize("overwrite", [False, True])
def test_movie_files_round_trip(tmp_path, job, monkeypatch, overwrite):
    job.update(kind="电影", detail={
        "coverUrl": "https://cdn.example/cover.png",
        "subtitlePdfUrl": "https://cdn.example/subtitle.pdf",
        "subtitlePdfEnUrl": "https://cdn.example/en.pdf",
    })
    calls = []

    def play(*args):
        calls.append("play")
        return {"subtitleUrl": "https://cdn.example/subtitle.vtt", "pdfDownload": 1}

    def direct(url, path, overwrite):
        finish(path)
        calls.append(path.suffix)
        return "downloaded"

    monkeypatch.setattr(cli, "_play", play)
    monkeypatch.setattr(cli, "direct", direct)
    first = cli.content_download(None, job, tmp_path, {"files"}, "SD", 2, 4, False)
    assert first["status"] == "complete"
    assert calls == ["play", ".vtt", ".png", ".pdf", ".pdf"]
    calls.clear()
    second = cli.content_download(None, job, tmp_path, {"files"}, "SD", 2, 4, overwrite)
    assert second["status"] == "complete"
    assert calls == (["play", ".vtt", ".png", ".pdf", ".pdf"] if overwrite else [])
    assert all(a["status"] == ("downloaded" if overwrite else "skipped") for a in second["assets"])


@pytest.mark.parametrize("overwrite", [False, True])
def test_damaged_video_or_overwrite_downloads(tmp_path, job, monkeypatch, overwrite):
    folder, stem = cli._paths(job, tmp_path, "SD", -1)
    video = folder / f"{stem}.mp4"
    finish(video)
    if not overwrite:
        video.write_bytes(b"short")
    calls = []

    def download(*args):
        calls.append(args[-1])
        finish(video)
        return {}, "downloaded"

    monkeypatch.setattr(cli, "_download_playback", download)
    result = cli.content_download(None, job, tmp_path, {"video"}, "SD", -1, 4, overwrite)
    assert calls == [overwrite]
    assert result["assets"][0]["status"] == "downloaded"


def test_auth_failure_aborts_remaining_jobs(tmp_path, job, monkeypatch):
    class Catalog:
        def animation(self, rid):
            return {"name": job["ip_name"], "seasonList": [{
                "seasonId": job["season_id"], "seasonName": job["season_name"],
                "dramaVOList": [{"id": i, "name": f"第{i}集"} for i in range(1, 4)],
            }]}

    attempted = []

    def download(cat, j, target, quality, lang, jobs, overwrite):
        attempted.append(j["id"])
        if j["id"] == 2:
            raise AuthError("登录已失效（HTTP 401），请重新运行 littleuqu login 登录")
        finish(target)
        return {}, "downloaded"

    monkeypatch.setattr(cli, "Catalog", lambda api: Catalog())
    monkeypatch.setattr(cli, "API", lambda: None)
    monkeypatch.setattr(cli, "_download_playback", download)
    result = CliRunner().invoke(cli.app, [
        "download", "动画", "44", "--media", "video", "--output", str(tmp_path),
    ])
    # 第 2 集鉴权失败后必须中止：第 3 集不再请求播放接口。
    assert attempted == [1, 2]
    assert result.exit_code == 2, result.output
    report = read_json(tmp_path / "download-report.json")
    assert [item["status"] for item in report] == ["complete", "failed", "skipped"]


def test_completed_batch_without_ffmpeg_and_batched_report(tmp_path, job, no_network, monkeypatch):
    class Catalog:
        def animation(self, rid):
            return {"name": job["ip_name"], "seasonList": [{
                "seasonId": job["season_id"], "seasonName": job["season_name"],
                "dramaVOList": [{"id": i, "name": f"第{i}集"} for i in range(1, 21)],
            }]}

    for i in range(1, 21):
        item = {**job, "id": i, "index": i, "name": f"第{i}集"}
        folder, stem = cli._paths(item, tmp_path, "SD", -1)
        finish(folder / f"{stem}.mp4")
    monkeypatch.setattr(cli, "Catalog", lambda api: Catalog())
    monkeypatch.setattr(cli, "API", lambda: None)
    monkeypatch.setattr("littleuqu.download.require_ffmpeg", unexpected)
    monkeypatch.setattr(cli.time, "monotonic", lambda: 0)
    writes = []

    def record(path, value):
        writes.append(path)
        write_json(path, value)

    monkeypatch.setattr(cli, "write_json", record)
    result = CliRunner().invoke(cli.app, [
        "download", "动画", "44", "--media", "video", "--output", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    report = tmp_path / "download-report.json"
    assert writes == [report, report]
    assert len(read_json(report)) == 20
    assert all(item["status"] == "complete" for item in read_json(report))
