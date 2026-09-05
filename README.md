# littleuqu

小优趣 Python CLI：短信登录、电影/动画/熏听分类查询、目录展开与下载。

## 安装

```bash
uv tool install littleuqu
littleuqu --help
```

升级到最新版：`uv tool upgrade littleuqu`。

HLS 合并与音轨提取需要 `ffmpeg`（macOS：`brew install ffmpeg`）。
从源码开发：`uv sync --group dev`，随后 `uv run littleuqu --help`。

## 登录

```bash
littleuqu login
# 或导入本机已有抓包中的 token 与设备上下文
littleuqu auth import-capture crawl/动画/list.md
littleuqu auth status
littleuqu auth logout
```

`login` 输入手机号后会发送短信。已有验证码可用 `login --mobile PHONE --code CODE`，
但交互输入更适合避免 shell 历史保存验证码。会话保存在 platformdirs 用户配置目录，权限 0600。
可用 `LITTLEUQU_CONFIG_DIR` 指定目录。原始抓包不会进入发行包；请不要提交包含凭据的抓包。
客户端优先使用 Python 运行时的系统 CA bundle，兼容安装了本机代理证书的环境；也可用
`LITTLEUQU_CA_BUNDLE=/path/to/ca.pem` 显式指定，TLS 校验不会被关闭。
登录返回 refreshToken，但刷新接口尚缺抓包，目前过期后需要重新登录。
新设备的必要请求头仍需真实联调；可通过导入抓包复用设备上下文。
儿童档案查询接口尚缺抓包，可 `auth set-child ID --age-days N` 设置。

## 查询

```bash
littleuqu categories
littleuqu categories 电影
littleuqu categories 动画 --json
littleuqu list 电影 --filter type=2 --all-pages
littleuqu list 动画 --filter 主题=儿歌 --filter 语言=英文 --all-pages
littleuqu list 熏听 --filter type=3 --json
littleuqu show 动画 44
littleuqu show 电影 116
littleuqu show 熏听 382
```

类别别名：`movie` / `animation` / `audio`。筛选项来自接口，支持名称和数字值，0 表示全部。
`list` 默认第 1 页、每页 16 条，`--all-pages` 遍历全部页。按类别和资源类型/ID 去重。
抓包仅提供第一页，当前将 `page.offset` 按页码递增，检测重复页时会报错而非静默截断。

## 下载

```bash
# 查看计划，不请求播放、不创建下载文件
littleuqu download 动画 44 --season 169 --dry-run
# 下载一集视频（不请求 PDF 附件）
littleuqu download 动画 44 --episode 5294 --media video
# 视频、字幕、封面和元数据
littleuqu download 动画 44 --season 169 --media video,files
# 显式从视频提取音轨（保留中间视频）
littleuqu download 动画 44 --episode 5294 --media video,audio --extract-audio
# 按分类批量下载，最多 5 集
littleuqu download 动画 --filter subjectType=1001 --all --limit 5 --media video
# 下载电影视频、字幕、中英文 PDF、封面和元数据
littleuqu download 电影 116
# 下载熏听专辑的一条音轨并提取为 M4A
littleuqu download 熏听 382 --episode 11281 --media audio
# 下载整个熏听专辑；auto 对熏听等同于 audio,files
littleuqu download 熏听 382
# 已有音频、PDF、ZIP、MP4 或 M3U8 直链
littleuqu fetch 'https://example.com/audio.mp3' -o audio.mp3
littleuqu fetch 'https://example.com/playlist.m3u8' -o video.mp4
```

`--output` 指定目录；`--jobs` 控制 HLS 分片并发（默认 4）；`--overwrite` 强制重下。
`--quality` 默认对动画/电影使用 SD，对熏听使用 FD；`--lang` 默认对电影使用 2，
对动画/熏听使用 -1。其他值需服务端支持。

文件按 `动画/作品_ID/季_ID/序号_集名_ID` 组织。完成标记记录文件长度，重复运行跳过已完成文件。
直链使用 `.part`、ETag/Last-Modified 与 Range 续传；无验证器或服务器忽略 Range 时重新下载。
HLS 支持普通点播 TS、主列表选择、AES-128 正确 IV/密钥、分片缓存、签名刷新和 CDN 切换。
目前 byte-range/fMP4、独立音轨主列表和非 AES-128 加密会明确报错。
媒体请求不发送账户 token；API 请求保留 TLS 校验。

下载报告 `download-report.json` 包含每项 complete/partial/failed 状态。
退出码：0 成功（或 dry-run 成功生成计划）；1 参数/接口/环境错误；2 批量结果存在未完成项。
使用 `--media files` 时，已知 SRT、封面和元数据可以保存；若服务端显示存在 PDF，
报告会明确标记 partial，避免误认为附件齐全。报告不会保存临时签名播放地址。

## 当前接口覆盖

| 类别 | 分类/列表 | 季集目录 | 播放下载 |
|---|---|---|---|
| 动画 | 已实现 | 已实现（`/coreapp/v2/ip/pdf`） | 已实现 HLS、SRT |
| 电影 | 已实现 | 已实现（`film/detail/v2`） | 已实现 HLS、SRT、中英文 PDF、封面 |
| 熏听 | 已实现 | 已实现（`listen/content/list`、`songList`） | 已实现 MP4/M3U8 获取、M4A 提取、原始 MP3、SRT/LRC、封面 |

熏听当前覆盖抓包中出现的 `LISTEN_AD`、`LISTEN_VD` 和 `LISTEN_FILM`。遇到其他 `rssType` 会明确报错，
需要补充该类型的内容列表和播放抓包。`download --all` 会遍历三类。
`fetch` 可下载手头已有的音视频/附件直链。

待补充：熏听除 `LISTEN_AD`/`LISTEN_VD`/`LISTEN_FILM` 外的资源类型、动画和熏听的 PDF 地址、
动画整季 ZIP、儿童档案与 token 刷新接口。
`crawl/动画/video_list.md` 虽名为视频目录，但接口是 PDF 目录，完整性需要与 App 集数实际比对。
已用现有抓包会话完成只读联调：登录状态有效；三类分类及第 1/2 页列表正常，页码递增有效；
动画 44 的季集目录和 5294 的播放地址正常。未发送短信，短信登录流程由合成响应测试验证。

## 验证

```bash
uv run pytest
uv run ruff check src tests
uv build
```

测试使用脱敏合成响应和本地 HTTP 服务，不发送短信、不访问真实内容服务。

### 本次验证结果

- 15 项测试通过；Ruff 检查通过。
- 真实下载 Baby Shark（动画 44 / 集 5294），MP4 约 41 MB、178.926 秒，包含 H.264 视频与 AAC 音轨。
- 真实下载熏听专辑 382 的音轨 11281，并提取为 AAC/M4A，约 2.4 MB、310.1 秒。
- 真实下载 `LISTEN_AD` 专辑 65 的歌曲 918，保存原始 320 kbps MP3、LRC 和封面。
- 电影 116 的详情和播放接口实测通过，返回完整 SD M3U8、字幕和中英文 PDF 地址。
- 电影 116 的 SRT、中英文 PDF 和封面已实际下载并校验文件格式。
- 本机代理环境使用系统 CA 完成严格 TLS 校验；未关闭证书验证。
- wheel 与源码包构建通过，打包内容不含原始抓包和下载文件。
