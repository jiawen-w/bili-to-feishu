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

# 全局 Whisper 模型缓存：批量处理时只加载一次，节省每次 ~30s 的加载时间
_whisper_cache: dict = {}


def transcribe(audio_path: str, model_size: str = "medium", language: str | None = None) -> tuple[str, list]:
    ensure_package("faster-whisper", "faster_whisper")
    from faster_whisper import WhisperModel

    # 命中缓存则直接复用，否则加载并缓存
    if model_size not in _whisper_cache:
        print(f"\n加载 Whisper 模型（{model_size}）…")
        _whisper_cache[model_size] = WhisperModel(model_size, device="auto", compute_type="auto")
    else:
        print(f"\n复用已加载的 Whisper 模型（{model_size}）")
    model = _whisper_cache[model_size]

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

def upload_to_feishu(title: str, content: str) -> str:
    """创建飞书文档，返回文档 URL（失败返回空字符串）"""
    import json as _json
    result = subprocess.run(
        [LARK_CLI, "docs", "+create",
         "--api-version", "v2",
         "--title", title,
         "--content", content,
         "--doc-format", "markdown"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"\n✗ 飞书写入失败：{result.stderr.strip()}")
        print("请先运行：lark-cli auth login --recommend")
        return ""
    try:
        data = _json.loads(result.stdout)
        doc_url = data.get("data", {}).get("document", {}).get("url", "")
        print(f"\n✓ 飞书文档已创建：{doc_url or result.stdout.strip()}")
        return doc_url
    except Exception:
        print(f"\n✓ 飞书文档已创建\n{result.stdout.strip()}")
        return ""


# ── 单视频主流程 ───────────────────────────────────────────────────────────────

def run(
    url: str,
    work_dir: str = "~/Downloads/bili_to_feishu",
    model_size: str = "medium",
    language: str | None = None,   # None = 自动检测，"zh" 中文，"en" 英文
    keep_audio: bool = True,
    save_subtitle: bool = True,
    upload_feishu: bool = True,
) -> dict:
    """
    处理单个视频，返回结果字典：
    {"ok": bool, "title": str, "url": str, "doc_url": str, "error": str}
    """
    work_dir = os.path.expanduser(work_dir)
    os.makedirs(work_dir, exist_ok=True)

    url = clean_url(url)
    platform = detect_platform(url)

    print(f"\n平台：{'B 站' if platform == 'bilibili' else 'YouTube' if platform == 'youtube' else '未知'}")
    print(f"链接：{url}")

    try:
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

        doc_url = ""
        if upload_feishu:
            print("\n[5/5] 写入飞书 …")
            doc_url = upload_to_feishu(title=f"【知识库】{title}", content=article)
        else:
            print("\n[5/5] 跳过飞书上传（upload_feishu=False）")

        if not keep_audio:
            os.remove(audio_path)
            print(f"已删除临时音频：{audio_path}")

        return {"ok": True, "title": title, "url": url, "doc_url": doc_url, "error": ""}

    except Exception as e:
        import traceback
        print(f"\n✗ 处理失败：{e}")
        traceback.print_exc()
        return {"ok": False, "title": "", "url": url, "doc_url": "", "error": str(e)}


# ── 批量处理多个视频 ───────────────────────────────────────────────────────────

def run_batch(
    urls: list[str],
    work_dir: str = "~/Downloads/bili_to_feishu",
    model_size: str = "medium",
    language: str | None = None,
    keep_audio: bool = True,
    save_subtitle: bool = True,
    upload_feishu: bool = True,
) -> None:
    """
    按顺序处理多个视频链接。
    单个失败不中断，继续处理下一个，最后打印汇总结果。
    Whisper 模型只加载一次，全程复用。
    """
    total   = len(urls)
    results = []

    print("=" * 60)
    print(f"批量处理模式  ·  共 {total} 个视频")
    print("=" * 60)

    for i, url in enumerate(urls, 1):
        print(f"\n{'━' * 60}")
        print(f"  [{i}/{total}]  {url.strip()}")
        print(f"{'━' * 60}")

        result = run(
            url          = url,
            work_dir     = work_dir,
            model_size   = model_size,
            language     = language,
            keep_audio   = keep_audio,
            save_subtitle= save_subtitle,
            upload_feishu= upload_feishu,
        )
        results.append(result)

        status = "✅ 成功" if result["ok"] else "❌ 失败"
        print(f"\n{status}  [{i}/{total}]  {result.get('title') or url}")

    # ── 汇总 ──────────────────────────────────────────────────────
    succeeded = [r for r in results if r["ok"]]
    failed    = [r for r in results if not r["ok"]]

    print(f"\n{'═' * 60}")
    print(f"  全部完成！成功 {len(succeeded)}/{total}  失败 {len(failed)}/{total}")
    print(f"{'═' * 60}")

    if succeeded:
        print("\n✅ 成功：")
        for r in succeeded:
            doc = f"  → {r['doc_url']}" if r["doc_url"] else ""
            print(f"  • {r['title']}{doc}")

    if failed:
        print("\n❌ 失败：")
        for r in failed:
            print(f"  • {r['url']}")
            print(f"    原因：{r['error']}")


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

def _collect_urls() -> list[str]:
    """
    收集视频链接：
    - 命令行传入多个链接：python bili_to_feishu.py url1 url2 url3
    - 交互式：每行一个，空行结束（支持直接粘贴多行）
    """
    # 命令行参数（跳过脚本名本身，过滤掉 --xxx 参数）
    cli_urls = [a for a in sys.argv[1:] if not a.startswith("--")]
    if cli_urls:
        return cli_urls

    # 交互式输入
    print("请输入视频链接（B 站 / YouTube），每行一个，输入空行开始处理：")
    urls = []
    while True:
        try:
            line = input("  > ").strip()
        except EOFError:
            break
        if not line:
            if urls:
                break
            continue
        urls.append(line)
    return urls


if __name__ == "__main__":
    # ── 模式 B：从已有 txt 直接跑 AI + 飞书（改这里） ──────────────────────────
    MODE = "A"   # "A" = 完整流程   "B" = 从 txt 跳过下载和转录

    if MODE == "B":
        run_from_txt(
            txt_path="~/Downloads/bili_to_feishu/我蒸馏了17个大佬给我打工（开源免费）.txt",
            title="我蒸馏了17个大佬给我打工（开源免费）",
            url="https://www.bilibili.com/video/BV1BXQABNE4y/",
            upload_feishu=True,
        )
        sys.exit(0)

    # ── 模式 A：批量完整流程 ────────────────────────────────────────────────────
    urls = _collect_urls()
    if not urls:
        print("未输入任何链接，退出。")
        sys.exit(0)

    if len(urls) == 1:
        # 单个链接：走原有单视频流程，保持输出简洁
        run(
            url          = urls[0],
            work_dir     = "~/Downloads/bili_to_feishu",
            model_size   = "medium",
            language     = None,
            keep_audio   = True,
            save_subtitle= True,
            upload_feishu= True,
        )
    else:
        # 多个链接：批量模式
        run_batch(
            urls         = urls,
            work_dir     = "~/Downloads/bili_to_feishu",
            model_size   = "medium",
            language     = None,
            keep_audio   = True,
            save_subtitle= True,
            upload_feishu= True,
        )
