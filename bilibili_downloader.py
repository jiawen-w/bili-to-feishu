"""
B 站视频下载器
依赖: pip install yt-dlp
音频转 mp3 需要: brew install ffmpeg
"""

import subprocess
import sys
import os
import re


def install_yt_dlp():
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])


def clean_url(url: str) -> str:
    """只保留 BV 号，去掉多余参数"""
    match = re.search(r"(https://www\.bilibili\.com/video/BV\w+)", url)
    return match.group(1) if match else url


def download(
    url: str,
    output_dir: str = ".",
    quality: str = "best",
    subtitles: bool = False,
    subtitle_langs: list[str] | None = None,
    embed_subtitles: bool = False,
    subtitles_only: bool = False,
) -> None:
    """
    下载 B 站视频。

    quality 可选值:
      best       — 最高画质（默认）
      1080p      — 1080p
      720p       — 720p
      480p       — 480p
      360p       — 360p
      audio_only — 仅音频（mp3）

    subtitles      — 是否下载字幕（保存为 .srt / .vtt 文件）
    subtitle_langs — 指定语言，如 ["zh-Hans", "zh-Hant", "en"]，None 表示全部
    embed_subtitles— 是否将字幕嵌入 mp4（需要 ffmpeg）
    subtitles_only — 仅下载字幕，跳过视频
    """
    try:
        import yt_dlp
    except ImportError:
        print("正在安装 yt-dlp …")
        install_yt_dlp()
        import yt_dlp

    url = clean_url(url)
    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    quality_formats = {
        "best":       "bestvideo+bestaudio/best",
        "1080p":      "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "720p":       "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "480p":       "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "360p":       "bestvideo[height<=360]+bestaudio/best[height<=360]",
        "audio_only": "bestaudio/best",
    }

    fmt = quality_formats.get(quality, quality_formats["best"])

    if subtitles_only:
        subtitles = True
        embed_subtitles = False

    ydl_opts = {
        "format": fmt,
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress": True,
        "skip_download": subtitles_only,
        # ── 字幕 ──
        "writesubtitles": subtitles,
        "writeautomaticsub": subtitles,
        "subtitleslangs": subtitle_langs or ["all"],
        "subtitlesformat": "srt/vtt/best",
        # ── 后处理 ──
        "postprocessors": [
            *(
                [{"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False}]
                if subtitles and embed_subtitles else []
            ),
            *(
                [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
                if quality == "audio_only" else []
            ),
        ],
    }

    print(f"URL   : {url}")
    print(f"画质  : {quality}")
    print(f"输出  : {os.path.abspath(output_dir)}\n")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("\n下载完成！")


# ── 示例 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    VIDEO_URL = "https://www.bilibili.com/video/BV1iVoVBgERD/"

    download(
        url=VIDEO_URL,
        output_dir="~/Downloads/Youtube视频",    # 保存目录
        quality="audio_only",                    # 仅下载音频，保存为 mp3
    )
