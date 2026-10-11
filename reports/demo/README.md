# Shim × BeFreed 演示

[观看 MP4](shim-befreed-demo.mp4) · [英文分镜与字幕](script-en.md) · [SRT 字幕](shim-befreed-demo.en.srt) · [实测证据](metadata.json)

成片 **35 秒**，1280×720，H.264 + AAC。使用原创轻音乐和简短英文画面字幕，**没有旁白**。可直接用浏览器、QuickTime 或常见播放器播放。

顺序：

| 时间 | 画面与信息 |
|---|---|
| 0–5 秒 | BeFreed 真实首页：给 agent 可复用工具 |
| 5–10 秒 | 真实公开书籍页 Attached：标题、作者、分类 |
| 10–16 秒 | 候选 → 验证 → 可调用；明确标记真实编译回放、压缩等待 |
| 16–22 秒 | 保存的 `get_book(slug)`，放大输入 schema 与提取规则 |
| 22–30 秒 | 原现场点击 Call 的录屏；真实 HTTP 200、1 条结构化书籍记录 |
| 30–35 秒 | 保存的 OpenAPI、llms.txt 等产物；同一 manifest 可通过 MCP 暴露工具 |

## 真实性

使用既有真实浏览器录屏重剪，不重新访问网站或调用模型。增量编译段使用真实事件，画面明确标记 **Real compile replay · waiting compressed**。事件副本在 `compile-events.jsonl`。只有验证成功的工具显示为 Verified；没有把候选当作已通过验证。

工具调用是在原录制时实际发起的 `get_book(slug="attached-by-amir-levine")`：HTTP 200、1 条记录、标题 Attached，耗时 0.255 秒。输入、响应、保存结果时间戳和成片 ffprobe 证据见 `metadata.json`。本次剪辑不产生新的网络或模型请求。

没有声称 BeFreed 没有 API，也没有访问付费、登录或个人内容。现场工具调用仅发 HTTP 请求，不运行模型。

## 音乐和复现

音乐由 `scripts/generate_demo_music.py` 用 Python 标准库原创生成，无外部 samples；已保存小体积 master `demo-music.m4a`。最终视频只映射画面和音乐，源片中的系统语音被全部丢弃。

运行 `.venv/bin/python scripts/record_demo.py` 可重剪。脚本依赖项目 Playwright（仅在本地生成字幕图片）、系统 ffmpeg 和 ffprobe，优先读取 music master；缺失时运行生成器并调整响度。也可通过 `--music PATH` 指定音乐。需要本地已有的六个原始录屏片段 `reports/demo/01_problem.mp4` 至 `06_export.mp4`；这些中间文件未提交仓库，因此仅克隆仓库不能重新剪辑。仓库保存了可直接播放的最终视频、字幕、音乐、脚本及真实编译事件。

原始录屏、分镜 PNG 和 AIFF 是中间素材，不属于当前成片。`script-zh.md` 是此前版本的中文参考稿；当前成片以 `script-en.md` 和英文 SRT 为准。
