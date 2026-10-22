# bili-transcribe

把 B 站视频转成文字的混合管线：**字幕优先 → ASR 兜底**。

- 有 UP 主上传的 CC 字幕时秒级直取；
- 没有字幕时自动下载音频，用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 在本地转录。

## 组成

| 脚本 | 作用 |
| --- | --- |
| `scripts/bili_hybrid.py` | 主流程：给 BV 号 → 输出转录（txt / srt / md） |
| `scripts/bili_search_transcribe.py` | 一条龙：关键词搜视频 → 逐个转录 → 抓 top 热评 → 合成报告 |

## 依赖

- Python 3.10+
- `curl`、`ffmpeg`（系统命令）
- faster-whisper：推荐用 [uv](https://github.com/astral-sh/uv) 免安装运行（见下方用法），或 `pip install -r requirements.txt`（含 faster-whisper 与 numpy）
- 仅 `bili_search_transcribe.py` 需要 [catbus](https://www.npmjs.com/package/catbus-cli)：安装 `npm i -g catbus-cli`，再 `catbus bilibili auth login` 扫码登录（登录凭证由 catbus 自行管理，本仓库不接触）；`bili_hybrid.py` 本体免登录

## 用法

```bash
# 单个视频转录（BV 号或含 BV 号的链接均可）
uv run --with faster-whisper python3 scripts/bili_hybrid.py BV1xx411c7xx

# 输出默认写到 ./out/<BV>/：transcript.txt / transcript.srt / transcript.md
# md 头部标注来源（B站CC字幕 / 本地ASR转录），转录结果缓存在 ~/.cache/bili_hybrid/

# 关键词一条龙：搜视频 + 转录 + 热评报告
python3 scripts/bili_search_transcribe.py "Python 教程" --limit 5 --sort views
```

## 实现要点

- **字幕源是适配器**（`SubtitleProvider` 抽象类）：当前实现走「第三方解析服务取 cid → `x/player/v2` 查字幕轨 → 下载字幕」；解析主源失效时自动切 B 站官方 `pagelist` + `playurl` 备用源。换字幕源只需新增一个 Provider 子类。
- **AI 字幕需登录**：`ai-zh` 等 AI 字幕轨需要登录态（SESSDATA），本工具不处理凭证，匿名场景下自动跳过，只用可匿名获取的 UP 主自传 CC 字幕。实测多数视频没有 CC 轨，所以大多数情况下实际走的是 ASR 路径——字幕优先只是一次廉价的乐观尝试。
- **view 接口 412 绕行**：`x/web-interface/view` 在部分网络环境下会被拦截，本管线全程不依赖它（标题经搜索接口获取）。
- **ASR 喂音频数组**：faster-whisper 读取 wav 后以 numpy 数组喂入模型，规避新版 av 库的兼容问题。

## 已知限制

- 第三方解析服务（injahow bparse）是单点，失效时字幕轨与直链解析会走备用源，仍失败则报错退出
- ASR 速度取决于 CPU：small 模型转录约为实时的数倍耗时，长视频请耐心等待；`bili_search_transcribe.py` 对单个视频的转录设了 900 秒超时，超长视频建议单独用 `bili_hybrid.py` 处理

## 免责声明

- 请仅转录自己上传或已获授权的视频；下载、转录他人视频可能涉及著作权与平台用户协议风险，使用者应遵守 B 站用户协议并控制请求频率
- 默认解析源 injahow 是非官方的第三方服务，其可用性与合规性请自行判断；不可用时会自动切换到 B 站官方 `pagelist` + `playurl` 备用源
- `bili_search_transcribe.py` 生成的报告包含其他用户的公开评论内容，请勿未经授权公开再分发

## License

MIT
