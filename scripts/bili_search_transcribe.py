#!/usr/bin/env python3
"""B站关键词搜视频 → 转录 → 抓热评，一条龙。

流程：
  1. catbus bilibili item search <关键词> → 视频列表（含 BV、标题、播放量）
  2. 每个 BV 调 bili_hybrid.py（字幕优先 → ASR 兜底）→ transcript
  3. catbus bilibili comment list <BV> --sort popular → top 热评

运行环境：需要 catbus 已安装且 B站已登录（`catbus bilibili auth login` 扫码），
请在能正常访问 B 站的本地网络环境运行；bili_hybrid.py 本体无此限制。

用法：
  python bili_search_transcribe.py "Python 教程" --limit 5 --sort views
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
import os

HERE = Path(__file__).resolve().parent
HYBRID = HERE / "bili_hybrid.py"
# catbus 可执行文件位置可用环境变量 CATBUS_BIN 覆盖，默认从 PATH 找
CATBUS = os.environ.get("CATBUS_BIN", "catbus")


def catbus_cmd(*args: str) -> list[str]:
    return [CATBUS, *args]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def check_catbus() -> None:
    if not shutil.which(CATBUS):
        sys.exit("未找到 catbus：请先 npm i -g catbus-cli")
    r = run(catbus_cmd("bilibili", "auth", "status"))
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.exit(f"catbus auth status 解析失败：{r.stdout[:200]}\n{r.stderr[:200]}")
    d = data.get("data") or {}
    if not data.get("ok") or not d.get("logged_in"):
        sys.exit(
            "B站登录态失效或未登录。\n"
            "请运行以下命令重新扫码登录：\n"
            "  catbus bilibili auth login\n"
            "（二维码 2-3 分钟内有效）"
        )
    user = (d.get("user") or {}).get("name", "?")
    print(f"[ok] catbus B站已登录（{user}）", flush=True)


def search_videos(keyword: str, limit: int, sort: str) -> list[dict]:
    r = run(catbus_cmd(
        "bilibili", "item", "search", keyword,
        "--limit", str(limit), "--sort", sort,
    ))
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.exit(f"搜索结果解析失败：{r.stdout[:300]}")
    if not data.get("ok"):
        sys.exit(f"搜索失败：{(data.get('error') or {}).get('message', r.stdout[:200])}")
    videos = []
    for item in data.get("data") or []:
        bvid = item.get("id") or ""
        if not bvid.startswith("BV"):
            continue
        stats = item.get("stats") or {}
        author = item.get("author") or {}
        videos.append({
            "bvid": bvid,
            "title": item.get("title") or "",
            "url": item.get("url") or f"https://www.bilibili.com/video/{bvid}",
            "author": author.get("name") or "",
            "views": stats.get("views") or 0,
        })
    return videos


def transcribe(bvid: str, out_dir: Path) -> Path | None:
    """调 bili_hybrid.py，返回 transcript.md 路径（失败返回 None）。"""
    # faster-whisper 优先走 uv 的隔离环境（免污染系统环境）
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "run", "--with", "faster-whisper",
               sys.executable, str(HYBRID), bvid, "--out", str(out_dir)]
    else:
        cmd = [sys.executable, str(HYBRID), bvid, "--out", str(out_dir)]
    # 大小写 no_proxy 都清掉（httpx 解析 no_proxy 里的 [::1] 会报错）
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("no_proxy",)}
    r = run(cmd, timeout=900, env=env)
    md = out_dir / bvid / "transcript.md"
    if r.returncode != 0 or not md.exists():
        print(f"[warn] {bvid} 转录失败：{r.stderr[-300:]}", flush=True)
        return None
    return md


def fetch_comments(bvid: str, limit: int = 20) -> list[dict]:
    r = run(catbus_cmd(
        "bilibili", "comment", "list", bvid,
        "--sort", "popular", "--limit", str(limit),
    ))
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"[warn] {bvid} 评论解析失败", flush=True)
        return []
    if not data.get("ok"):
        print(f"[warn] {bvid} 评论获取失败：{(data.get('error') or {}).get('message', '')[:100]}", flush=True)
        return []
    comments = []
    for c in data.get("data") or []:
        author = c.get("author") or {}
        stats = c.get("stats") or {}
        comments.append({
            "author": author.get("name") or "",
            "text": (c.get("text") or "")[:500],
            "likes": stats.get("likes") or 0,
        })
    return comments


def main() -> int:
    ap = argparse.ArgumentParser(description="B站关键词搜视频 → 转录 → 热评")
    ap.add_argument("keyword", help="搜索关键词")
    ap.add_argument("--limit", type=int, default=5, help="取前 N 个视频（默认 5）")
    ap.add_argument("--sort", default="views",
                    choices=["general", "views", "latest", "collects"],
                    help="搜索排序（默认 views）")
    ap.add_argument("--no-comments", action="store_true", help="跳过评论抓取")
    ap.add_argument("--comment-limit", type=int, default=20, help="每个视频取几条热评")
    ap.add_argument("--out", default="out_search", help="输出目录")
    args = ap.parse_args()

    check_catbus()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] 搜索：{args.keyword}", flush=True)
    videos = search_videos(args.keyword, args.limit, args.sort)
    if not videos:
        print("无搜索结果")
        return 1
    for i, v in enumerate(videos, 1):
        print(f"  {i}. [{v['bvid']}] {v['title'][:50]}（播放 {v['views']}）", flush=True)

    report = [f"# B站搜索转录报告：{args.keyword}", ""]
    for i, v in enumerate(videos, 1):
        bvid = v["bvid"]
        print(f"[{i}/{len(videos)}] 转录 {bvid}", flush=True)
        report += [f"## {i}. {v['title']}", f"- BV：{bvid} | UP：{v['author']} | 播放：{v['views']}", f"- 链接：{v['url']}", ""]
        md = transcribe(bvid, out_dir)
        if md:
            report += ["### 转录", f"见 `{md}`", ""]
        else:
            report += ["### 转录", "（失败，见上方警告）", ""]
        if not args.no_comments:
            print(f"[{i}/{len(videos)}] 抓热评 {bvid}", flush=True)
            comments = fetch_comments(bvid, args.comment_limit)
            report += ["### 热评 Top", ""]
            for j, c in enumerate(comments, 1):
                report.append(f"{j}. **{c['author']}**（赞 {c['likes']}）：{c['text']}")
            report += [""]

    report_path = out_dir / "report.md"
    report_path.write_text("\n".join(report), encoding="utf-8")
    print(f"[done] 报告：{report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
