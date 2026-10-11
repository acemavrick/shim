# Shim × BeFreed 演示

[观看 MP4](shim-befreed-demo.mp4) · [英文讲稿](script-en.md) · [英文字幕](shim-befreed-demo.en.srt) · [中文参考稿](script-zh.md) · [实测证据](metadata.json)

成片约 **54 秒**，1280×720，H.264 + AAC；有英文旁白（macOS Samantha，en-US）和画面内英文说明。可直接用浏览器、QuickTime 或常见播放器播放。

顺序：

1. BeFreed 真实首页：网站为人类浏览设计，agent 需要可复用工具接口。
2. 真实公开书籍页 Attached：标题、作者、分类。
3. 边探索边出现候选、验证状态：**真实编译记录回放，已压缩等待**（画面明确标记 Real compilation replay）。
4. 加载实际完成的 saved compilation，查看 `get_book(slug)` 输入与请求/提取规则。
5. 点击 Call，真实请求当前 BeFreed：HTTP 200，1 条结构化书籍记录。
6. 查看保存的产物：OpenAPI、llms.txt 等；同一 manifest 可以通过 `shim serve www.befreed.ai` 暴露为 MCP tools。

## 真实性和复现

第三段使用 `/tmp/shim-streaming-live.jsonl` 中真实编译事件，在当前 UI 重放，并在画面/旁白明确标注。事件副本保存在 `compile-events.jsonl`。此段没有新模型请求；其他浏览和工具调用是实际访问。保存结果的时间戳、输入、现场调用响应和 ffprobe 时长证据见 `metadata.json`。候选状态从未被当作已验证工具展示。

本演示表达的是“把人类浏览流程转换为 agent 可调用的工具接口”。没有声称 BeFreed 没有 API，也没有访问付费、登录或个人内容。现场调用不使用模型。

复现脚本：`scripts/record_demo.py`，依赖项目 Playwright、系统 ffmpeg 和 macOS `say` 的 Samantha 英文语音。优先读取仓库中的 `reports/demo/compile-events.jsonl`，缺失时才使用 `/tmp/shim-streaming-live.jsonl`。需要本地现有的 `sites/www.befreed.ai/manifest.json` 及运行中的 Shim UI；如果没有 saved manifest，先实际编译 BeFreed。录制脚本使用保存结果与捕获事件，不跑新编译。可设置 `SHIM_DEMO_SCENE=05_call` 单独重录现场调用片段。`raw/` 是原始无旁白 WebM；编号 MP4 是分镜。完整片为 `shim-befreed-demo.mp4`。
