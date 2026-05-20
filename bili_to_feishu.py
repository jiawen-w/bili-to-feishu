"""
B 站 / YouTube 视频 → 音频 → 文字 → AI 重构 → 飞书知识库文章
一键流程：输入链接，自动完成所有步骤

依赖安装:
    pip install yt-dlp faster-whisper anthropic
    brew install ffmpeg
    npm install -g @larksuite/cli

飞书首次登录:
    /Users/chenjiawen/.hermes/node/bin/lark-cli config init --new
    /Users/chenjiawen/.hermes/node/bin/lark-cli auth login --recommend
"""

from __future__ import annotations

import os
import re
import sys
import subprocess


# ── AI / 飞书配置 ──────────────────────────────────────────────────────────────

AI_BASE_URL  = "https://ark.cn-beijing.volces.com/api/coding"
AI_API_KEY   = "1d3ace95-c577-4eee-ae9d-4fc85f3d07ee"
AI_MODEL     = "doubao-seed-2.0-pro"
LARK_CLI     = "/Users/chenjiawen/.hermes/node/bin/lark-cli"
BILI_BROWSER = "chrome"   # B 站下载用的浏览器 Cookie：chrome 或 safari

PROMPT_TEMPLATE = """\
你是专业技术文档/知识库编撰工程师，我给你一段视频的完整字幕，请把它重构为**标准化精品技术知识库教程**。

输出结构：
1. 教程名称
2. 适用人群 & 学习前置条件
3. 内容总览
4. 分章节结构化正文（层级标题清晰）
5. 实操步骤拆解（逐条可跟着照做）
6. 核心原理讲解（把视频口语内容转化为专业原理说明）
7. 配置/命令/关键参数汇总表
8. 坑点避坑 & 注意事项
9. 章节知识点复盘
10. 课后小结 & 延伸学习建议

写作要求：
1. 口语转书面，精简冗余，保留所有干货细节
2. 代码、命令、路径、配置单独代码块展示
3. 关键操作、易错点、核心结论加粗
4. 逻辑重新归纳，不照搬视频流水账，做成永久可复用知识库
5. 语言严谨、条理清晰，适合收藏、内部团队学习使用

视频标题：{title}
视频链接：{url}

字幕内容：
{transcript}
"""


# ── 工具函数 ───────────────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    """识别平台：bilibili / youtube / unknown"""
    if "bilibili.com" in url or url.startswith("BV"):
        return "bilibili"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return "unknown"


def clean_url(url: str) -> str:
    """标准化 URL"""
    url = url.strip()
    platform = detect_platform(url)

    if platform == "bilibili":
        if url.startswith("bilibili.com"):
            url = "https://www." + url
        elif url.startswith("www.bilibili.com"):
            url = "https://" + url
        match = re.search(r"BV\w+", url)
        if match:
            return f"https://www.bilibili.com/video/{match.group(0)}/"

    if platform == "youtube":
        # 短链转完整链
        match = re.search(r"youtu\.be/([^?&]+)", url)
        if match:
            return f"https://www.youtube.com/watch?v={match.group(1)}"

    return url


def build_ydl_opts(url: str, output_dir: str) -> dict:
    """根据平台返回对应的 yt-dlp 参数"""
    base = {
        "quiet": False,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
    }
    if detect_platform(url) == "bilibili":
        base["cookiesfrombrowser"] = (BILI_BROWSER,)
    return base


def format_timestamp(seconds: float) -> str:
    ms = int((seconds - int(seconds)) * 1000)
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def ensure_package(package: str, import_name: str | None = None) -> None:
    import importlib
    name = import_name or package
    try:
        importlib.import_module(name)
    except ImportError:
        print(f"正在安装 {package} …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])


# ── Step 1: 获取视频标题 ────────────────────────────────────────────────────────

def get_title(url: str) -> str:
    ensure_package("yt_dlp", "yt_dlp")
    import yt_dlp
    opts = {"quiet": True}
    if detect_platform(url) == "bilibili":
        opts["cookiesfrombrowser"] = (BILI_BROWSER,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info.get("title", "未命名视频")


# ── Step 2: 下载音频 ────────────────────────────────────────────────────────────

def download_audio(url: str, output_dir: str) -> str:
    ensure_package("yt_dlp", "yt_dlp")
    import yt_dlp

    os.makedirs(output_dir, exist_ok=True)

    with yt_dlp.YoutubeDL(build_ydl_opts(url, output_dir)) as ydl:
        info = ydl.extract_info(url)
        title = info.get("title", "audio")
        audio_path = os.path.join(output_dir, f"{title}.mp3")

    if not os.path.exists(audio_path):
        mp3_files = [f for f in os.listdir(output_dir) if f.endswith(".mp3")]
        if not mp3_files:
            raise FileNotFoundError("音频下载失败，未找到 mp3 文件")
        mp3_files.sort(key=lambda f: os.path.getmtime(os.path.join(output_dir, f)), reverse=True)
        audio_path = os.path.join(output_dir, mp3_files[0])

    print(f"\n✓ 音频已保存：{audio_path}")
    return audio_path


# ── Step 3: 音频转文字 ──────────────────────────────────────────────────────────

def transcribe(audio_path: str, model_size: str = "medium", language: str | None = None) -> tuple[str, list]:
    ensure_package("faster-whisper", "faster_whisper")
    from faster_whisper import WhisperModel

    print(f"\n加载 Whisper 模型（{model_size}）…")
    model = WhisperModel(model_size, device="auto", compute_type="auto")

    print("转录中，请稍候 …")
    segments, info = model.transcribe(audio_path, language=language, beam_size=5, vad_filter=True)
    print(f"✓ 检测语言：{info.language}（置信度 {info.language_probability:.0%}）")

    segment_list = list(segments)
    full_text = "\n".join(seg.text.strip() for seg in segment_list)
    return full_text, segment_list


def save_srt(segment_list: list, output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segment_list, 1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n")
            f.write(f"{seg.text.strip()}\n\n")
    print(f"✓ 字幕已保存：{output_path}")


# ── Step 4: AI 重构为知识库文章 ─────────────────────────────────────────────────

def ai_restructure(title: str, url: str, transcript: str) -> str:
    ensure_package("anthropic")
    import anthropic

    print("\nAI 重构中，请稍候 …")
    client = anthropic.Anthropic(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    prompt = PROMPT_TEMPLATE.format(title=title, url=url, transcript=transcript)

    message = client.messages.create(
        model=AI_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    result = message.content[0].text
    print("✓ AI 重构完成")
    return result


# ── Step 5: 写入飞书 ────────────────────────────────────────────────────────────

def upload_to_feishu(title: str, content: str) -> None:
    result = subprocess.run(
        [LARK_CLI, "docs", "+create", "--title", title, "--markdown", content],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"\n✓ 飞书文档已创建！\n{result.stdout.strip()}")
    else:
        print(f"\n✗ 飞书写入失败：{result.stderr.strip()}")
        print("请先运行：lark-cli auth login --recommend")


# ── 主流程 ─────────────────────────────────────────────────────────────────────

def run(
    url: str,
    work_dir: str = "~/Downloads/bili_to_feishu",
    model_size: str = "medium",
    language: str | None = None,   # None = 自动检测，"zh" 中文，"en" 英文
    keep_audio: bool = True,
    save_subtitle: bool = True,
    upload_feishu: bool = True,
) -> None:
    work_dir = os.path.expanduser(work_dir)
    os.makedirs(work_dir, exist_ok=True)

    url = clean_url(url)
    platform = detect_platform(url)

    print("=" * 50)
    print(f"平台：{'B 站' if platform == 'bilibili' else 'YouTube' if platform == 'youtube' else '未知'}")
    print(f"链接：{url}")
    print("=" * 50)

    print("\n[1/5] 获取视频信息 …")
    title = get_title(url)
    print(f"✓ 标题：{title}")

    print("\n[2/5] 下载音频 …")
    audio_path = download_audio(url, work_dir)

    print("\n[3/5] 音频转文字 …")
    full_text, segment_list = transcribe(audio_path, model_size=model_size, language=language)

    base = os.path.splitext(audio_path)[0]

    if save_subtitle:
        save_srt(segment_list, base + ".srt")

    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(full_text)
    print(f"✓ 原始文本已保存：{base}.txt")

    print("\n[4/5] AI 重构为知识库文章 …")
    article = ai_restructure(title=title, url=url, transcript=full_text)

    with open(base + "_知识库.md", "w", encoding="utf-8") as f:
        f.write(article)
    print(f"✓ 知识库文章已保存：{base}_知识库.md")

    if upload_feishu:
        print("\n[5/5] 写入飞书 …")
        upload_to_feishu(title=f"【知识库】{title}", content=article)
    else:
        print("\n[5/5] 跳过飞书上传（upload_feishu=False）")

    if not keep_audio:
        os.remove(audio_path)
        print(f"已删除临时音频：{audio_path}")

    print("\n✅ 全部完成！")


# ── 从已有 txt 直接跑 AI + 飞书 ───────────────────────────────────────────────

def run_from_txt(
    txt_path: str,
    title: str | None = None,
    url: str = "",
    upload_feishu: bool = True,
) -> None:
    txt_path = os.path.expanduser(txt_path)
    if not os.path.exists(txt_path):
        raise FileNotFoundError(f"找不到文件：{txt_path}")

    if title is None:
        title = os.path.splitext(os.path.basename(txt_path))[0]

    with open(txt_path, encoding="utf-8") as f:
        transcript = f.read()

    print(f"✓ 读取文本：{txt_path}（{len(transcript)} 字）")

    print("\n[1/2] AI 重构为知识库文章 …")
    article = ai_restructure(title=title, url=url, transcript=transcript)

    out_path = os.path.splitext(txt_path)[0] + "_知识库.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(article)
    print(f"✓ 知识库文章已保存：{out_path}")

    if upload_feishu:
        print("\n[2/2] 写入飞书 …")
        upload_to_feishu(title=f"【知识库】{title}", content=article)
    else:
        print("\n[2/2] 跳过飞书上传")

    print("\n✅ 全部完成！")


# ── 入口 ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 模式 A：完整流程（下载 → 转录 → AI → 飞书）
    # 模式 B：从已有 txt 直接跑 AI + 飞书（跳过下载和转录）
    MODE = "A"

    if MODE == "B":
        run_from_txt(
            txt_path="~/Downloads/bili_to_feishu/我蒸馏了17个大佬给我打工（开源免费）.txt",
            title="我蒸馏了17个大佬给我打工（开源免费）",
            url="https://www.bilibili.com/video/BV1BXQABNE4y/",
            upload_feishu=True,
        )
    else:
        if len(sys.argv) > 1:
            video_url = sys.argv[1]
        else:
            video_url = input("请输入视频链接（B 站 / YouTube）：").strip()

        run(
            url=video_url,
            work_dir="~/Downloads/bili_to_feishu",
            model_size="medium",     # Whisper 模型：tiny / small / medium / large
            language=None,           # None=自动检测  "zh"=中文  "en"=英文
            keep_audio=True,
            save_subtitle=True,
            upload_feishu=True,
        )
