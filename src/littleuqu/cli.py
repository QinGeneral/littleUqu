from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from . import __version__
from .api import API
from .catalog import KINDS, Catalog, item_id, kind_name, playback_urls, title
from .config import UquError, config_dir, safe_name, scrub, write_json
from .download import MediaError, completed, direct, extract_audio, media, require_ffmpeg

app = typer.Typer(help="小优趣：登录、分类查询与媒体下载", no_args_is_help=True)
auth = typer.Typer(help="管理登录会话", no_args_is_help=True)
app.add_typer(auth, name="auth")
console = Console(stderr=True)


def emit(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command()
def version():
    """显示版本。"""
    typer.echo(__version__)


@app.command()
def login(
    mobile: str | None = typer.Option(None, help="手机号，不传则交互输入"),
    code: str | None = typer.Option(None, help="已有验证码；提供后不发送短信"),
):
    """短信验证码登录，交互输入验证码。"""
    client = API()
    mobile = mobile or typer.prompt("手机号")
    if not mobile.isdigit() or len(mobile) != 11:
        raise UquError("请输入 11 位手机号")
    if code is None:
        client.request("/ucapp/sms", body={"mobile": mobile, "type": "1"}, require_auth=False)
        console.print("验证码已发送")
        code = typer.prompt("验证码", hide_input=True)
    client.login(mobile, code)
    console.print("登录成功，会话已保存")


@auth.command("status")
def auth_status():
    """调用用户接口验证会话，不显示 token。"""
    user = API().request("/ucapp/getUser")["data"]
    emit(
        {
            "logged_in": True,
            "vip": user.get("vip"),
            "vip_end": user.get("vipEnd"),
            "config_dir": str(config_dir()),
        }
    )


@auth.command("import-capture")
def import_capture(path: Path = typer.Argument(..., exists=True, dir_okay=False)):
    """从本地 curl 抓包或登录响应导入会话（不发送短信）。"""
    API().import_capture(path)
    console.print("会话已导入；运行 littleuqu auth status 验证有效性")


@auth.command("logout")
def logout():
    """删除本地会话。"""
    (config_dir() / "session.json").unlink(missing_ok=True)
    console.print("本地会话已删除")


@auth.command("set-child")
def set_child(child_id: int = typer.Argument(..., min=0), age_days: int = typer.Option(-1, min=-1)):
    """设置抓包中的儿童上下文。"""
    client = API()
    client.headers.update(chdId=str(child_id), chdAgeDays=str(age_days))
    client.save()
    console.print("儿童上下文已保存")


@app.command()
def categories(
    kind: str | None = typer.Argument(None), as_json: bool = typer.Option(False, "--json")
):
    """查看三大类别，或某个类别的筛选条件。"""
    if kind is None:
        emit(list(KINDS))
        return
    data = Catalog(API()).categories(kind_name(kind))
    if as_json:
        emit(data)
        return
    table = Table("筛选项", "参数", "可选值（0=全部）")
    for group in data:
        table.add_row(
            group["name"],
            group["key"],
            " / ".join(f"{x['type']}={x['name']}" for x in group["conditions"]),
        )
    console.print(table)


@app.command("list")
def list_items(
    kind: str = typer.Argument(...),
    filters: list[str] = typer.Option([], "--filter", "-f"),
    all_pages: bool = typer.Option(False, "--all-pages"),
    page: int = typer.Option(1, min=1),
    page_size: int = typer.Option(16, min=1, max=100),
    as_json: bool = typer.Option(False, "--json"),
):
    """列出分类下的作品或专辑；支持多次 --filter。"""
    kind = kind_name(kind)
    cat = Catalog(API())
    items = list(cat.items(kind, cat.filters(kind, filters), all_pages, page, page_size))
    if as_json:
        emit(items)
    else:
        table = Table("ID", "名称", "类型 / 集数")
        for item in items:
            table.add_row(
                str(item_id(kind, item)),
                title(item),
                str(item.get("rssType") or item.get("numInfo") or ""),
            )
        console.print(table)
        console.print(f"共 {len(items)} 项")


@app.command()
def show(
    kind: str = typer.Argument(...),
    resource_id: int = typer.Argument(..., min=1),
    as_json: bool = typer.Option(False, "--json"),
):
    """查看电影详情，或动画/熏听的分集目录。"""
    kind = kind_name(kind)
    cat = Catalog(API())
    if kind == "动画":
        data = cat.animation(resource_id)
        if as_json:
            emit(data)
            return
        table = Table("季 ID", "季名", "集 ID", "集名")
        for season in data.get("seasonList", []):
            for episode in season.get("dramaVOList", []):
                table.add_row(
                    str(season["seasonId"]),
                    season["seasonName"],
                    str(episode["id"]),
                    episode["name"],
                )
        console.print(table)
    elif kind == "电影":
        data = cat.movie(resource_id)
        if as_json:
            emit(data)
            return
        table = Table("字段", "内容")
        for key in ("id", "cnName", "enName", "duration", "level", "descp"):
            table.add_row(key, str(data.get(key) or ""))
        console.print(table)
    else:
        albums = [x for x in cat.items(kind, all_pages=True) if item_id(kind, x) == resource_id]
        if not albums:
            raise UquError("未找到指定资源")
        if len(albums) > 1:
            raise UquError("该 ID 对应多个熏听类型，请使用列表的 rssType 区分后下载")
        data = cat.listen_content(albums[0])
        if as_json:
            emit(data)
            return
        table = Table("序号", "音轨 ID", "名称", "类型")
        for index, track in enumerate(data, 1):
            table.add_row(str(index), str(track["id"]), title(track), track.get("rssType", ""))
        console.print(table)


@app.command("fetch")
def fetch(
    url: str = typer.Argument(..., help="已获得的 HTTP 音视频/文件直链"),
    output: Path = typer.Option(..., "--output", "-o"),
    jobs: int = typer.Option(4, min=1, max=16),
    overwrite: bool = typer.Option(False),
    hls: bool = typer.Option(False, help="强制按 HLS 处理"),
):
    """下载已有直链，支持 M3U8、视频、音频和附件。"""
    if urlsplit(url).scheme not in ("https", "http") or not urlsplit(url).netloc:
        raise UquError("URL 必须为 HTTP(S) 地址")
    console.print(media(url, output, jobs=jobs, overwrite=overwrite, is_hls=hls))


def _paths(job, output, quality, lang):
    if job["kind"] == "动画":
        folder = (
            output
            / "动画"
            / f"{safe_name(job['ip_name'])}_{job['ip_id']}"
            / f"{safe_name(job['season_name'])}_{job['season_id']}"
        )
        prefix = f"{job['index']:03d}_"
    elif job["kind"] == "电影":
        folder = output / "电影" / f"{safe_name(job['name'])}_{job['id']}"
        prefix = ""
    else:
        folder = output / "熏听" / f"{safe_name(job['album_name'])}_{job['album_id']}"
        prefix = f"{job['index']:03d}_"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{prefix}{safe_name(job['name'])}_{job['id']}_{safe_name(quality)}_lang{lang}"
    return folder, stem


def _play(cat, job, quality, lang):
    if job["kind"] == "动画":
        return cat.play_animation(job["id"], quality, lang)
    if job["kind"] == "电影":
        return cat.play_movie(job["id"], quality, lang)
    return cat.play_listen(job, quality, lang)


def _download_playback(cat, job, target, quality, lang, jobs, overwrite):
    last_error = None
    for _ in range(2):
        data = _play(cat, job, quality, lang)
        if data.get("def") and data["def"] != quality:
            raise UquError(f"服务端返回清晰度 {data['def']}，与请求 {quality} 不一致")
        for url in playback_urls(data):
            try:
                with Progress(console=console) as progress:
                    task = progress.add_task(job["name"], total=None)
                    status = media(
                        url,
                        target,
                        jobs,
                        overwrite,
                        data.get("format") == "m3u8",
                        lambda done, total, p=progress, t=task: p.update(
                            t, completed=done, total=total
                        ),
                    )
                return data, status
            except MediaError as exc:
                last_error = exc
    raise last_error or MediaError("播放地址下载失败")


def _asset(result, label, url, path, overwrite):
    if not url:
        return
    status = direct(url, path, overwrite)
    result["assets"].append({"type": label, "path": str(path), "status": status})


def content_download(cat, job, output, selected, quality, lang, jobs, overwrite):
    folder, stem = _paths(job, output, quality, lang)
    direct_audio = job["kind"] == "熏听" and job.get("rssType") == "LISTEN_AD"
    if direct_audio and "video" in selected:
        raise UquError("LISTEN_AD 是纯音频资源，不支持 --media video")
    video = folder / f"{stem}.mp4"
    audio = folder / f"{stem}{'.mp3' if direct_audio else '.m4a'}"
    source = (
        audio if direct_audio else video if "video" in selected else folder / f".{stem}.source.mp4"
    )
    result = {**job, "assets": [], "warnings": []}
    data = None
    need_playback = "video" in selected or "audio" in selected
    if need_playback:
        if completed(source) and not overwrite:
            data = _play(cat, job, quality, lang)
            result["assets"].append({"type": "source", "path": str(source), "status": "skipped"})
        else:
            data, status = _download_playback(cat, job, source, quality, lang, jobs, overwrite)
            result["assets"].append({"type": "source", "path": str(source), "status": status})
        if "audio" in selected and not direct_audio:
            status = extract_audio(source, audio, overwrite)
            result["assets"].append({"type": "audio", "path": str(audio), "status": status})
        elif "audio" in selected:
            result["assets"][-1]["type"] = "audio"
    if "files" in selected:
        data = data or _play(cat, job, quality, lang)
        detail = job.get("detail") or job.get("movieDetail") or {}
        subtitle_url = (
            data.get("subtitleUrl") or job.get("subtitleUrl") or detail.get("subtitleUrl")
        )
        subtitle_suffix = (
            Path(urlsplit(subtitle_url).path).suffix.lower() if subtitle_url else ".srt"
        )
        if subtitle_suffix not in (".srt", ".lrc", ".vtt"):
            subtitle_suffix = ".srt"
        _asset(
            result,
            "subtitle",
            subtitle_url,
            folder / f"{stem}{subtitle_suffix}",
            overwrite,
        )
        cover_url = (
            job.get("dmCvUrl")
            or job.get("coverUrl")
            or job.get("img")
            or job.get("roundCoverUrl")
            or job.get("audioCoverUrl")
            or detail.get("coverUrl")
            or detail.get("headImg")
        )
        if cover_url:
            suffix = Path(urlsplit(cover_url).path).suffix.lower()
            suffix = suffix if suffix in (".png", ".jpg", ".jpeg", ".webp") else ".jpg"
            _asset(result, "cover", cover_url, folder / f"cover{suffix}", overwrite)
        _asset(
            result,
            "subtitle_pdf",
            detail.get("subtitlePdfUrl"),
            folder / f"{safe_name(job['name'])}_字幕.pdf",
            overwrite,
        )
        _asset(
            result,
            "subtitle_pdf_en",
            detail.get("subtitlePdfEnUrl"),
            folder / f"{safe_name(job['name'])}_英文字幕.pdf",
            overwrite,
        )
        write_json(folder / f"{stem}.json", scrub({"item": job, "playback": data}))
        has_pdf = (
            data.get("pdfDownload") == 1
            or (data.get("pdfSize") or 0) > 0
            or (data.get("pdfEnSize") or 0) > 0
        )
        if has_pdf and not (detail.get("subtitlePdfUrl") or detail.get("subtitlePdfEnUrl")):
            result["warnings"].append("播放响应显示存在 PDF，但当前抓包未提供该类型的 PDF 地址")
    result["status"] = "partial" if result["warnings"] else "complete"
    return result


@app.command("download")
def download(
    kind: str | None = typer.Argument(None),
    resource_id: int | None = typer.Argument(None),
    filters: list[str] = typer.Option([], "--filter", "-f"),
    all_items: bool = typer.Option(False, "--all"),
    season: int | None = typer.Option(None, min=1),
    episode: list[int] = typer.Option([], "--episode", help="动画集或熏听音轨 ID"),
    rss_type: str | None = typer.Option(None, "--rss-type", help="同 ID 熏听专辑的类型"),
    selected_media: str = typer.Option(
        "auto", "--media", help="auto/video/audio/files，可逗号组合"
    ),
    extract: bool = typer.Option(False, "--extract-audio", help="兼容旧参数；指定后加入 audio"),
    quality: str | None = typer.Option(None, help="动画/电影默认 SD，熏听默认 FD"),
    lang: int | None = typer.Option(None, help="动画/熏听默认 -1，电影默认 2"),
    output: Path = typer.Option(Path("downloads"), "--output", "-o"),
    jobs: int = typer.Option(4, min=1, max=16),
    limit: int | None = typer.Option(None, min=1, help="最多处理多少集/资源"),
    overwrite: bool = typer.Option(False),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """下载电影、动画作品或熏听专辑。"""
    explicit_media = None if selected_media == "auto" else set(selected_media.split(","))
    if explicit_media is not None and (
        not explicit_media or explicit_media - {"video", "audio", "files"}
    ):
        raise UquError("--media 可选 auto 或 video,audio,files 的组合")
    if extract:
        explicit_media = (explicit_media or {"video", "files"}) | {"audio"}
    if resource_id is not None and resource_id < 1:
        raise UquError("资源 ID 必须为正整数")
    if resource_id is not None and (not kind or all_items or filters):
        raise UquError("指定资源 ID 时需提供类别，且不能同时使用 --all/--filter")
    if resource_id is None and not all_items:
        raise UquError("批量下载请显式指定 --all；单作品下载请提供 ID")
    if not kind and (filters or season or episode or rss_type):
        raise UquError("筛选条件或季/集/音轨参数需要指定类别")
    kinds = [kind_name(kind)] if kind else list(KINDS)
    if season and kinds != ["动画"]:
        raise UquError("--season 仅适用于动画")
    if episode and kinds not in (["动画"], ["熏听"]):
        raise UquError("--episode 仅适用于动画或熏听")
    if rss_type and kinds != ["熏听"]:
        raise UquError("--rss-type 仅适用于熏听")
    cat = Catalog(API())
    plan = []
    for current_kind in kinds:
        if resource_id and current_kind == "熏听":
            matches = [
                item
                for item in cat.items(current_kind, all_pages=True)
                if item_id(current_kind, item) == resource_id
                and (rss_type is None or item.get("rssType") == rss_type)
            ]
            if not matches:
                raise UquError(f"未找到熏听专辑 {resource_id}")
            if len(matches) > 1:
                raise UquError("该 ID 对应多个熏听类型，请指定 --rss-type")
            items = matches
        elif resource_id:
            items = [{"ipId": resource_id, "id": resource_id}]
        else:
            items = cat.items(current_kind, cat.filters(current_kind, filters), all_pages=True)
        for item in items:
            rid = item_id(current_kind, item)
            if current_kind == "电影":
                detail = cat.movie(rid)
                plan.append(
                    {
                        "kind": current_kind,
                        "id": rid,
                        "name": title(detail),
                        "detail": detail,
                        "status": "pending",
                    }
                )
            elif current_kind == "动画":
                tree = cat.animation(rid)
                seasons = tree.get("seasonList", [])
                if season and not any(s["seasonId"] == season for s in seasons):
                    raise UquError(f"作品 {rid} 未找到季 {season}")
                for s in seasons:
                    if season and s["seasonId"] != season:
                        continue
                    for index, e in enumerate(s.get("dramaVOList", []), 1):
                        if episode and e["id"] not in episode:
                            continue
                        plan.append(
                            {
                                "kind": current_kind,
                                "ip_id": rid,
                                "ip_name": tree["name"],
                                "season_id": s["seasonId"],
                                "season_name": s["seasonName"],
                                "index": index,
                                **e,
                                "status": "pending",
                            }
                        )
                        if limit and len(plan) >= limit:
                            break
                    if limit and len(plan) >= limit:
                        break
            else:
                tracks = cat.listen_content(item)
                for index, track in enumerate(tracks, 1):
                    if episode and track["id"] not in episode:
                        continue
                    plan.append(
                        {
                            **track,
                            "kind": current_kind,
                            "album_id": rid,
                            "album_name": title(item),
                            "index": index,
                            "rssType": track.get("rssType") or item.get("rssType"),
                            "status": "pending",
                        }
                    )
                    if limit and len(plan) >= limit:
                        break
            if limit and len(plan) >= limit:
                break
        if limit and len(plan) >= limit:
            break
    if not plan:
        raise UquError("没有匹配资源，请检查筛选条件及季/集 ID")
    missing = set(episode) - {j["id"] for j in plan if "id" in j}
    if missing and not limit:
        raise UquError(f"未找到指定集 ID：{sorted(missing)}")
    if dry_run:
        emit(plan)
        return
    if any(j["status"] == "pending" for j in plan) and (
        explicit_media is None or {"video", "audio"} & explicit_media
    ):
        require_ffmpeg()
    output = output.resolve()
    report_path = output / "download-report.json"
    write_json(report_path, plan)
    for i, job in enumerate(plan):
        console.print(f"[{i + 1}/{len(plan)}] {job['name']}", markup=False)
        try:
            selected = explicit_media or (
                {"audio", "files"} if job["kind"] == "熏听" else {"video", "files"}
            )
            if quality:
                job_quality = quality
            elif job.get("rssType") == "LISTEN_AD":
                job_quality = "OD"
            else:
                job_quality = "FD" if job["kind"] == "熏听" else "SD"
            if lang is not None:
                job_lang = lang
            elif job.get("rssType") == "LISTEN_AD":
                job_lang = 0
            else:
                job_lang = 2 if job["kind"] == "电影" else -1
            plan[i] = content_download(
                cat, job, output, selected, job_quality, job_lang, jobs, overwrite
            )
        except (UquError, OSError) as exc:
            plan[i] = {**job, "status": "failed", "reason": str(exc)}
            console.print(f"[red]下载失败：{exc}[/red]")
        write_json(report_path, plan)
    counts = {s: sum(x["status"] == s for x in plan) for s in ("complete", "partial", "failed")}
    console.print(f"结果：{counts}\n报告：{report_path}")
    if counts["failed"] or counts["partial"]:
        raise typer.Exit(2)


def main():
    try:
        app()
    except UquError as exc:
        console.print(f"[red]错误：{exc}[/red]")
        raise SystemExit(1) from None
    except OSError as exc:
        console.print(f"[red]文件/进程操作失败：{exc.strerror or type(exc).__name__}[/red]")
        raise SystemExit(1) from None
