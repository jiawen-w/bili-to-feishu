"""
YouTube 博主追踪 → 新视频自动转知识库 → 飞书
监控指定频道，发现新视频后走 bili_to_feishu.py 完整流水线：
下载音频 → Whisper 转文字 → AI 重构 → 飞书知识库

依赖（与 bili_to_feishu.py 相同）：
    pip install yt-dlp faster-whisper anthropic

用法：
    python youtube_monitor.py               # 检查所有频道，处理新视频
    python youtube_monitor.py --dry-run     # 只预览有哪些新视频，不处理
    python youtube_monitor.py --no-feishu   # 本地生成文件，不上传飞书
    python youtube_monitor.py --reset       # 清空缓存，重新处理所有频道
"""

from __future__ import annotations

import os
import sys
import re
import json
import subprocess
from datetime import datetime

# ── 复用 bili_to_feishu.py 的完整流水线 ──────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bili_to_feishu import (
    download_audio,
    transcribe,
    save_srt,
    ai_restructure,
    upload_to_feishu,
    ensure_package,
)


# ── 追踪频道配置 ──────────────────────────────────────────────────────────────
# language: None=自动检测  "zh"=中文  "en"=英文（影响 Whisper 识别速度和准确率）

CHANNELS = [
    {
        "url":      "https://www.youtube.com/@rileybrownai",
        "name":     "Riley Brown AI",
        "language": "en",
    },
    {
        "url":      "https://www.youtube.com/@xiaojunpodcast",
        "name":     "小Jun Podcast",
        "language": "zh",
    },
    {
        "url":      "https://www.youtube.com/@TikTokEthan",
        "name":     "TikTok Ethan",
        "language": "en",
    },
    {
        "url":      "https://www.youtube.com/@qiuzhi2046",
        "name":     "求知2046",
        "language": "zh",
    },
]

# 输出目录 & 缓存
OUTPUT_DIR          = os.path.expanduser("~/Downloads/youtube_monitor")
CACHE_FILE          = os.path.join(OUTPUT_DIR, "seen_videos.json")

# 每次检查每个频道最新多少个视频
CHECK_LAST_N        = 10

# 每个频道每次最多处理几个新视频（防止一次积压太多）
MAX_NEW_PER_CHANNEL = 2

# Whisper 模型大小（与 bili_to_feishu.py 保持一致）
WHISPER_MODEL       = "medium"


# ── 缓存 ─────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    """
    格式：
    {
      "seen": {
        "@rileybrownai": ["video_id1", "video_id2", ...],
        "@xiaojunpodcast": [...],
        ...
      },
      "history": [
        {"date": "...", "channel": "...", "video_id": "...", "title": "..."},
        ...
      ]
    }
    """
    if not os.path.exists(CACHE_FILE):
        return {"seen": {}, "history": []}
    with open(CACHE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def channel_key(channel_url: str) -> str:
    """从 URL 提取 @handle 作为缓存 key"""
    match = re.search(r"@([\w.-]+)", channel_url)
    return match.group(0) if match else channel_url


# ── 获取频道最新视频列表 ──────────────────────────────────────────────────────

def get_channel_videos(channel_url: str, n: int = CHECK_LAST_N) -> list[dict]:
    """
    通过 yt-dlp 获取频道最新 n 个视频（flat 模式，不下载）
    返回：[{id, title, url, upload_date, duration, channel}, ...]
    """
    ensure_package("yt_dlp", "yt_dlp")
    import yt_dlp

    # /videos 只取正片，排除 Shorts 和直播
    url = channel_url.rstrip("/") + "/videos"
    opts = {
        "quiet":         True,
        "extract_flat":  True,
        "playlistend":   n,
        "ignoreerrors":  True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"  ✗ 获取频道视频列表失败：{e}")
        return []

    if not info:
        return []

    channel_name = (
        info.get("channel")
        or info.get("uploader")
        or channel_url.split("@")[-1]
    )
    entries = info.get("entries") or []

    videos = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        videos.append({
            "id":          e["id"],
            "title":       e.get("title") or "（无标题）",
            "url":         f"https://www.youtube.com/watch?v={e['id']}",
            "upload_date": e.get("upload_date") or "",
            "duration":    e.get("duration") or 0,
            "channel":     channel_name,
        })
    return videos


# ── 单视频处理流水线 ──────────────────────────────────────────────────────────

def process_video(
    video: dict,
    language: str | None,
    upload_feishu: bool = True,
) -> bool:
    """
    完整流水线：下载音频 → Whisper 转文字 → AI 重构 → 飞书
    复用 bili_to_feishu.py 的所有函数。
    成功返回 True，失败返回 False。
    """
    title     = video["title"]
    url       = video["url"]
    channel   = video["channel"]
    work_dir  = os.path.join(OUTPUT_DIR, re.sub(r'[\\/:*?"<>|]', "_", channel))
    os.makedirs(work_dir, exist_ok=True)

    print(f"\n  ▶ [{channel}] {title}")
    print(f"    {url}")

    try:
        # Step 1: 下载音频
        print("    [1/4] 下载音频 …")
        audio_path = download_audio(url, work_dir)

        # Step 2: 转文字
        print(f"    [2/4] Whisper 转文字（语言：{language or '自动'}）…")
        full_text, segment_list = transcribe(audio_path, model_size=WHISPER_MODEL, language=language)
        base = os.path.splitext(audio_path)[0]

        # 保存 SRT 字幕 & 原始文本
        save_srt(segment_list, base + ".srt")
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write(full_text)
        print(f"    ✓ 文字稿：{len(full_text)} 字")

        # Step 3: AI 重构
        print("    [3/4] AI 重构知识库文章 …")
        article = ai_restructure(title=title, url=url, transcript=full_text)

        md_path = base + "_知识库.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(article)
        print(f"    ✓ 知识库文章：{md_path}")

        # Step 4: 写飞书
        if upload_feishu:
            print("    [4/4] 写入飞书 …")
            upload_to_feishu(
                title=f"【{channel}】{title}",
                content=article,
            )
        else:
            print("    [4/4] 跳过飞书（--no-feishu）")

        return True

    except Exception as e:
        print(f"    ✗ 处理失败：{e}")
        return False


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run(
    upload_feishu: bool = True,
    dry_run: bool       = False,
    reset: bool         = False,
) -> None:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 60)
    print(f"YouTube 博主监控  ·  {today}")
    print(f"追踪 {len(CHANNELS)} 个频道")
    print("=" * 60)

    # 清空缓存（--reset 模式）
    cache = {} if reset else load_cache()
    if reset:
        print("⚠ 已清空缓存，将重新处理所有频道最新视频")
        cache = {"seen": {}, "history": []}

    total_new     = 0
    total_done    = 0

    for ch in CHANNELS:
        ch_url  = ch["url"]
        ch_name = ch["name"]
        lang    = ch.get("language")
        key     = channel_key(ch_url)

        print(f"\n{'─'*50}")
        print(f"📺 {ch_name}  ({ch_url})")

        # 获取最新视频列表
        videos = get_channel_videos(ch_url, n=CHECK_LAST_N)
        if not videos:
            print("  ✗ 获取视频列表失败，跳过")
            continue
        print(f"  ✓ 获取到 {len(videos)} 个视频")

        # 找出新视频
        seen_ids  = set(cache.get("seen", {}).get(key, []))
        new_videos = [v for v in videos if v["id"] not in seen_ids]

        if not new_videos:
            print(f"  ✓ 无新视频（已全部收录）")
            continue

        print(f"  ✓ 发现 {len(new_videos)} 个新视频：")
        for v in new_videos:
            dur = f"{v['duration']//60}min" if v["duration"] else "?"
            print(f"    • [{dur}] {v['title']}")
            print(f"      {v['url']}")

        total_new += len(new_videos)

        if dry_run:
            continue

        # 每次最多处理 MAX_NEW_PER_CHANNEL 个
        to_process = new_videos[:MAX_NEW_PER_CHANNEL]
        if len(new_videos) > MAX_NEW_PER_CHANNEL:
            print(f"  （本次处理前 {MAX_NEW_PER_CHANNEL} 个，剩余 {len(new_videos)-MAX_NEW_PER_CHANNEL} 个下次处理）")

        processed_ids = list(seen_ids)

        for video in to_process:
            ok = process_video(video, language=lang, upload_feishu=upload_feishu)
            if ok:
                total_done += 1
                processed_ids.append(video["id"])
                cache.setdefault("history", []).append({
                    "date":     datetime.now().isoformat(),
                    "channel":  ch_name,
                    "key":      key,
                    "video_id": video["id"],
                    "title":    video["title"],
                    "url":      video["url"],
                })
            else:
                # 失败的也记入 seen，避免反复卡在同一个视频
                processed_ids.append(video["id"])

        # 未处理的新视频不写入 seen，下次继续排队
        cache.setdefault("seen", {})[key] = processed_ids

    # 保存缓存
    if not dry_run:
        save_cache(cache)

    print(f"\n{'='*60}")
    if dry_run:
        print(f"[DRY RUN] 发现 {total_new} 个新视频，未实际处理。")
    else:
        print(f"✅ 完成！共处理 {total_done} 个新视频。")
    print("=" * 60)


# ── 入口 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run      = "--dry-run"   in sys.argv
    no_feishu    = "--no-feishu" in sys.argv
    reset        = "--reset"     in sys.argv

    run(
        upload_feishu = not no_feishu,
        dry_run       = dry_run,
        reset         = reset,
    )
