#!/usr/bin/env python3
"""B站视频转录混合管线：字幕优先 → ASR 兜底。

- 字幕优先：有 UP 主上传的 CC 字幕时秒级直取（字幕源为适配器封装，可替换）
- ASR 兜底：无可用字幕时下载音频，用 faster-whisper（默认 small/int8）本地转录

调用：
  uv run --with faster-whisper python3 bili_hybrid.py <BV号或含BV号的链接> [--out DIR] [--force-asr] [--no-cache]
  # 或在已装好 faster-whisper 的环境里直接 python3 bili_hybrid.py ...

输出（默认 ./out/<BV>/）：transcript.txt / transcript.srt / transcript.md，
md 头部标注来源（B站CC字幕 / 本地ASR转录）与置信度说明。
转录结果缓存在 ~/.cache/bili_hybrid/，重复调用秒返回。

实现笔记：
1. x/web-interface/view 在部分网络环境下会被 412，
   BV→cid 改走第三方解析服务 injahow bparse（带重试），
   备用 B站官方 pagelist+playurl
2. x/player/v2 带 Chrome UA + buvid3 + Referer 可用，返回字幕轨列表
3. AI 字幕（ai-zh）需登录态（SESSDATA），未提供时自动跳过；
   仅 UP 主自传 CC 字幕可匿名获取
4. 实测多数视频没有 CC 轨（多为需登录的 AI 字幕）——字幕优先是廉价的
   乐观尝试，命中则秒级，不命中自动降级 ASR
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import wave
from abc import ABC, abstractmethod
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PROXY = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
CACHE_DIR = Path.home() / ".cache" / "bili_hybrid"
CACHE_SCHEMA = 2  #  bump 时旧缓存自动失效
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# 解析结果进程内复用：字幕阶段与 ASR 阶段共用一次请求
# 主源 injahow bparse（一次给 cid+直链，带指数退避重试）
# 备用 B站官方 pagelist（cid）+ playurl（直链）
_BPARSE_CACHE: dict[str, dict] = {}


def _resolve_injahow(bv: str) -> dict:
    """injahow 解析。网络错误/超时/5xx 指数退避重试 3 次；
    4xx 或返回体无直链直接抛（重试无意义）。"""
    url = f"https://api.injahow.cn/bparse/?bv={bv}"
    last: Exception | None = None
    for i, wait in enumerate((1.0, 2.0, 4.0)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _opener().open(req, timeout=20) as r:
                data = json.loads(r.read().decode())
            mp4 = data.get("url")
            if not mp4:
                raise RuntimeError("injahow 未返回可用直链")
            m = re.search(r"/(\d+)/\d+-1-", mp4)
            if not m:
                raise RuntimeError("injahow 直链格式变化，取不到 cid")
            return {"url": mp4, "cid": m.group(1)}
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise RuntimeError(f"injahow 拒绝请求（HTTP {e.code}）") from e
            last = e
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            last = e
        if i < 2:
            time.sleep(wait + random.uniform(0, 0.5))
    raise RuntimeError(f"injahow 解析失败（已重试 3 次）：{last}")


def _resolve_bilibili(bv: str) -> dict:
    """备用源：B站官方 pagelist 取 cid，playurl 取 mp4 直链。"""
    pg = _get_json(
        f"https://api.bilibili.com/x/player/pagelist?bvid={bv}&jsonp=jsonp")
    pages = pg.get("data") or []
    if not pages or not pages[0].get("cid"):
        raise RuntimeError("pagelist 未返回 cid")
    cid = str(pages[0]["cid"])
    pu = _get_json(
        f"https://api.bilibili.com/x/player/playurl?bvid={bv}&cid={cid}&qn=64")
    durl = (pu.get("data") or {}).get("durl") or []
    if not durl or not durl[0].get("url"):
        raise RuntimeError("playurl 未返回直链")
    return {"url": durl[0]["url"], "cid": cid}


def _bparse(bv: str) -> dict:
    """BV→{"url": mp4直链, "cid"}：injahow 主源（重试）→ B站官方备用。"""
    if bv not in _BPARSE_CACHE:
        try:
            _BPARSE_CACHE[bv] = _resolve_injahow(bv)
        except Exception as e:
            print(f"  主解析源失败（{e}），切 B站官方备用…", flush=True)
            _BPARSE_CACHE[bv] = _resolve_bilibili(bv)
    return _BPARSE_CACHE[bv]

# 字幕轨语言优先级（ai-zh 需登录，匿名场景下实际取不到，仅作排序）
LANG_PRIORITY = ("ai-zh", "zh-Hans", "zh-CN", "zh")


def _opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
    return urllib.request.build_opener()


def _get_json(url: str, timeout: int = 20) -> dict:
    buvid = f"buvid3={uuid.uuid4().hex}infoc"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": buvid,
    })
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def _norm_bv(text: str) -> str:
    m = BV_RE.search(text or "")
    if not m:
        raise ValueError(f"输入里没有找到 BV 号：{text}")
    return m.group(0)


def _video_title(bv: str) -> str:
    """经搜索接口拿视频标题；失败则回退 BV 号（view 接口 412，用搜索绕行）。"""
    try:
        d = _get_json(
            "https://api.bilibili.com/x/web-interface/search/type"
            f"?search_type=video&keyword={bv}&page=1&page_size=3")
        for v in (d.get("data") or {}).get("result", []):
            if v.get("bvid") == bv:
                t = str(v.get("title", ""))
                return re.sub(r"</?em[^>]*>", "", t).strip() or bv
    except Exception:
        pass
    return bv


# ---------------------------------------------------------------- 适配器
class SubtitleProvider(ABC):
    """字幕源适配器接口：给 BV 号，还字幕段落或 None（无字幕）。"""
    name = "base"

    @abstractmethod
    def fetch(self, bv: str) -> list[tuple[float, float, str]] | None:
        """返回 [(start_s, end_s, text)]；无可用字幕返回 None。异常向上抛。"""


class DefaultSubtitleProvider(SubtitleProvider):
    """默认字幕源（参考 turnscribe 的公开字幕获取方案）。
    injahow 解析 cid → player/v2 查字幕轨 → aisubtitle 下载 body。
    """
    name = "turnscribe-style"

    def _resolve_cid(self, bv: str) -> str:
        return str(_bparse(bv)["cid"])

    def fetch(self, bv: str) -> list[tuple[float, float, str]] | None:
        cid = self._resolve_cid(bv)
        player = _get_json(
            f"https://api.bilibili.com/x/player/v2?bvid={bv}&cid={cid}")
        tracks = (((player.get("data") or {}).get("subtitle") or {})
                  .get("subtitles")) or []
        # 跳过需登录的 AI 字幕轨（匿名拿不到）；优先 UP 主自传
        usable = [t for t in tracks if t.get("lan") != "ai-zh"]
        if not usable:
            return None
        by_lan = {t.get("lan"): t for t in usable}
        track = next((by_lan[l] for l in LANG_PRIORITY if l in by_lan), usable[0])
        sub_url = str(track.get("subtitle_url", ""))
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        body = _get_json(sub_url).get("body") or []
        segs = []
        for item in body:
            text = str(item.get("content", "")).strip()
            if not text:
                continue
            segs.append((float(item["from"]), float(item["to"]), text))
        return segs or None


# ---------------------------------------------------------------- ASR 兜底
def _resolve_mp4(bv: str) -> str:
    mp4 = _bparse(bv).get("url")
    if not mp4:
        raise RuntimeError("未取到视频直链")
    return mp4


def asr_transcribe(bv: str, workdir: Path,
                   model_size: str = "small") -> list[tuple[float, float, str]]:
    """下载 mp4 → ffmpeg 提 wav → faster-whisper。返回 [(s,e,text)]。"""
    import numpy as np
    from faster_whisper import WhisperModel

    mp4 = workdir / "v.mp4"
    wav = workdir / "a.wav"
    env = dict(os.environ)
    # -f：HTTP 错误直接失败，避免把错误页当视频存下导致静默空转录
    # Referer：备用源（playurl 直链）要求带 Referer，否则 403
    subprocess.run(["curl", "-sS", "-f", "-m", "180", "-A", "Mozilla/5.0",
                    "-H", "Referer: https://www.bilibili.com/",
                    "-o", str(mp4), _resolve_mp4(bv)],
                   env=env, check=True, timeout=200)
    subprocess.run(["ffmpeg", "-y", "-i", str(mp4), "-vn", "-ar", "16000",
                    "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                   check=True, timeout=120, capture_output=True)
    with wave.open(str(wav), "rb") as w:
        frames = w.readframes(w.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio, language="zh", beam_size=5)
    return [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]


# ---------------------------------------------------------------- 输出
def _ts(sec: float) -> str:
    return f"{int(sec//60):02d}:{int(sec%60):02d}"


def _srt_ts(sec: float) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(s%1*1000):03d}"


def write_outputs(outdir: Path, bv: str, title: str, source: str,
                  segs: list[tuple[float, float, str]]) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "transcript.txt").write_text(
        "\n".join(f"[{_ts(s)}] {t}" for s, _, t in segs), encoding="utf-8")
    srt = []
    for i, (s, e, t) in enumerate(segs, 1):
        srt.append(f"{i}\n{_srt_ts(s)} --> {_srt_ts(e)}\n{t}\n")
    (outdir / "transcript.srt").write_text("\n".join(srt), encoding="utf-8")
    conf = ("B站 UP 主自传字幕，时间轴与文字以原字幕为准。"
            if source == "cc" else
            "本地 faster-whisper small 模型转录，个别词可能有谐音误差，关键信息建议回看原视频。")
    (outdir / "transcript.md").write_text(
        f"# {title or bv}\n\n- BV：{bv}\n- 来源：{'B站CC字幕' if source == 'cc' else '本地ASR转录'}\n"
        f"- 说明：{conf}\n- 共 {len(segs)} 段\n\n---\n\n" +
        "\n".join(f"**[{_ts(s)}]** {t}" for s, _, t in segs),
        encoding="utf-8")


# ---------------------------------------------------------------- 主流程
def load_cache(bv: str) -> dict | None:
    p = CACHE_DIR / f"{bv}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("v") != CACHE_SCHEMA or not d.get("segments"):
                return None
            return d
        except Exception:
            return None
    return None


def save_cache(bv: str, title: str, source: str,
               segs: list[tuple[float, float, str]]) -> None:
    if not segs:
        return  # 0 段不缓存，避免空结果毒化
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{bv}.json").write_text(json.dumps(
        {"v": CACHE_SCHEMA, "bv": bv, "title": title, "source": source,
         "ts": time.time(), "segments": [[s, e, t] for s, e, t in segs]},
        ensure_ascii=False), encoding="utf-8")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="B站视频转录混合管线：字幕优先 → ASR 兜底")
    ap.add_argument("bv", help="BV号或含BV号的链接")
    ap.add_argument("--out", default="out", help="输出目录（默认 ./out/<BV>/）")
    ap.add_argument("--force-asr", action="store_true", help="跳过字幕，直接 ASR")
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存")
    args = ap.parse_args()

    try:
        bv = _norm_bv(args.bv)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    outdir = Path(args.out) / bv
    provider: SubtitleProvider = DefaultSubtitleProvider()

    if not args.no_cache and not args.force_asr:
        hit = load_cache(bv)
        if hit:
            segs = [(s, e, t) for s, e, t in hit["segments"]]
            write_outputs(outdir, bv, hit.get("title", ""), hit["source"], segs)
            print(f"缓存命中：{outdir}（来源：{hit['source']}，{len(segs)} 段）")
            return

    segs: list[tuple[float, float, str]] | None = None
    source = ""
    title = _video_title(bv)
    if not args.force_asr:
        print("[1/3] 尝试字幕优先…", flush=True)
        try:
            segs = provider.fetch(bv)
        except Exception as e:
            print(f"  字幕源异常（{e}），降级 ASR", flush=True)
        if segs:
            source = "cc"
            print(f"  命中 CC 字幕：{len(segs)} 段，跳过下载/ASR", flush=True)

    if not segs:
        source = "asr"
        print("[2/3] 字幕未命中，走 ASR 兜底（下载+转录，约数分钟）…", flush=True)
        import shutil
        workdir = Path(tempfile.mkdtemp(prefix="bili_hy_"))
        try:
            segs = asr_transcribe(bv, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if not segs:
            print("错误：ASR 转录结果为空，未写缓存", file=sys.stderr)
            sys.exit(3)

    print("[3/3] 写出 txt/srt/md…", flush=True)
    write_outputs(outdir, bv, title, source, segs)
    save_cache(bv, title, source, segs)
    print(f"完成：{outdir}（来源：{'B站CC字幕' if source=='cc' else '本地ASR转录'}，{len(segs)} 段）")


if __name__ == "__main__":
    main()
