"""
B 站视频 → 关键帧截图 → 图文教程（Markdown + 飞书文档）
流程：下载视频 → Whisper 转录（带时间戳）→ AI 分析关键知识点 → ffmpeg 截帧 → AI 视觉生成笔记 → 输出 Markdown → 上传飞书

支持缓存复用：同一视频再次运行时自动跳过已完成的步骤（下载/转录/截帧）

依赖安装:
    pip install yt-dlp faster-whisper anthropic
    brew install ffmpeg
    npm install -g @larksuite/cli

飞书首次登录:
    lark-cli config init --new
    lark-cli auth login --recommend

用法:
    python bili_to_tutorial.py https://www.bilibili.com/video/BVxxxxxx
    或直接运行后输入链接
"""

import os
import re
import sys
import json
import base64
import subprocess
import textwrap
from pathlib import Path


# ── 配置 ───────────────────────────────────────────────────────────────────────

AI_BASE_URL    = "https://ark.cn-beijing.volces.com/api/coding"
AI_API_KEY     = "1d3ace95-c577-4eee-ae9d-4fc85f3d07ee"
AI_MODEL       = "doubao-seed-2.0-pro"   # 视觉 + 长文生成
AI_FAST_MODEL  = "deepseek-v3.2"         # 快速文本分析

BILI_BROWSER   = "chrome"
OUTPUT_BASE    = os.path.expanduser("~/Downloads/bili_tutorial")
LARK_CLI       = "/Users/chenjiawen/.hermes/node/bin/lark-cli"

# 最多提取多少个关键帧（AI 会自动决定，这是上限）
MAX_KEYFRAMES  = 20
# 每个关键时间点，在前后多少秒内取最清晰的帧
FRAME_WINDOW   = 2


# ── Prompt 模板 ────────────────────────────────────────────────────────────────

KEYPOINT_PROMPT = """\
你是一名教育内容专家，我给你一段视频的完整字幕（带时间戳）。
请仔细阅读，找出视频中所有**关键知识点/重点讲解/操作演示**的时刻。

要求：
1. 找出 10~{max_frames} 个最有价值的关键时刻（宁多勿少，只要内容重要就选）
2. 每个时刻必须是"讲到重点内容"或"屏幕上显示关键操作/图表/代码"的瞬间
3. 避免选择纯废话、开场白、结束语、广告等无实质内容的片段
4. 时间戳请精确到秒（取该知识点刚开始讲的那一秒）

请以 JSON 数组格式输出，每项包含：
- timestamp_sec: 时间（整数秒）
- title: 这个知识点的标题（10字以内）
- summary: 这一段讲了什么（50字以内）

只输出 JSON，不要有其他内容。

视频标题：{title}

字幕内容（格式：[开始秒-结束秒] 文字）：
{transcript_with_ts}
"""

VISION_NOTE_PROMPT = """\
你是一名专业教程作者。我给你一张视频截图和对应的字幕片段，请生成这个知识点的详细学习笔记。

知识点标题：{title}
时间戳：{timestamp}
字幕内容：
{subtitle_context}

请生成笔记，包含：
1. **核心要点**：用 3-5 条 bullet 总结这段内容的关键知识
2. **详细说明**：展开解释（150-300字），结合截图中看到的内容
3. **注意事项**：如有易错点或重要提示，列出来（没有可省略）

写作要求：
- 口语转书面，精准专业
- 代码/命令/路径用代码块
- 重要内容加粗
- 直接写笔记内容，不要重复标题
"""

SUMMARY_PROMPT = """\
你是一名教程总结专家。根据以下视频信息和关键知识点列表，写一段视频总览介绍（200-300字）。

视频标题：{title}
视频链接：{url}
知识点列表：
{keypoints}

要求：
1. 概括视频的主要内容和学习目标
2. 说明适合什么人观看
3. 用学完能掌握什么结尾
4. 语言简洁，像教程导言
"""


# ── 工具函数 ───────────────────────────────────────────────────────────────────

def ensure_package(package: str, import_name: str | None = None) -> None:
    import importlib
    name = import_name or package
    try:
        importlib.import_module(name)
    except ImportError:
        print(f"正在安装 {package} …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])


def safe_filename(name: str) -> str:
    """去掉文件名中的非法字符"""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def format_hms(seconds: float) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02}:{m:02}:{s:02}"


def format_timestamp_srt(seconds: float) -> str:
    ms = int((seconds - int(seconds)) * 1000)
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def ai_client(fast: bool = False):
    ensure_package("anthropic")
    import anthropic
    return anthropic.Anthropic(api_key=AI_API_KEY, base_url=AI_BASE_URL), \
           (AI_FAST_MODEL if fast else AI_MODEL)


def ai_text(prompt: str, fast: bool = False, max_tokens: int = 4096) -> str:
    client, model = ai_client(fast)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def ai_vision(image_paths: "str | list[str]", prompt: str, max_tokens: int = 2048) -> str:
    """发送一张或多张图片 + 文字给视觉模型"""
    if isinstance(image_paths, str):
        image_paths = [image_paths]

    client, model = ai_client(fast=False)

    content: list = []
    for image_path in image_paths:
        with open(image_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode()
        ext = Path(image_path).suffix.lower()
        media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                      "png": "image/png", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": img_b64},
        })
    content.append({"type": "text", "text": prompt})

    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    )
    return msg.content[0].text


# ── Step 1: 获取视频信息 ────────────────────────────────────────────────────────

def get_video_info(url: str) -> dict:
    ensure_package("yt_dlp", "yt_dlp")
    import yt_dlp
    opts = {"quiet": True, "cookiesfrombrowser": (BILI_BROWSER,)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# ── Step 2: 下载视频 ────────────────────────────────────────────────────────────

def download_video(url: str, work_dir: str) -> str:
    ensure_package("yt_dlp", "yt_dlp")
    import yt_dlp

    os.makedirs(work_dir, exist_ok=True)
    outtmpl = os.path.join(work_dir, "%(title)s.%(ext)s")

    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "cookiesfrombrowser": (BILI_BROWSER,),
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url)
        title = info.get("title", "video")

    # 找到实际下载的文件
    safe_name = safe_filename(title)
    video_path = os.path.join(work_dir, f"{title}.mp4")
    if not os.path.exists(video_path):
        mp4_files = sorted(
            [f for f in os.listdir(work_dir) if f.endswith(".mp4")],
            key=lambda f: os.path.getmtime(os.path.join(work_dir, f)),
            reverse=True,
        )
        if not mp4_files:
            raise FileNotFoundError("视频下载失败，未找到 mp4 文件")
        video_path = os.path.join(work_dir, mp4_files[0])

    print(f"✓ 视频已保存：{video_path}")
    return video_path


# ── Step 3: 提取音频并转录 ──────────────────────────────────────────────────────

def extract_audio(video_path: str, work_dir: str) -> str:
    audio_path = os.path.join(work_dir, "audio.mp3")
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn",
           "-ar", "16000", "-ac", "1", "-b:a", "64k", audio_path]
    subprocess.run(cmd, capture_output=True, check=True)
    print(f"✓ 音频已提取：{audio_path}")
    return audio_path


def transcribe(audio_path: str, model_size: str = "medium", language: str | None = None) -> list:
    ensure_package("faster-whisper", "faster_whisper")
    from faster_whisper import WhisperModel

    print(f"加载 Whisper 模型（{model_size}）…")
    model = WhisperModel(model_size, device="auto", compute_type="auto")
    print("转录中，请稍候 …")
    segments, info = model.transcribe(audio_path, language=language, beam_size=5, vad_filter=True)
    segs = list(segments)
    print(f"✓ 检测语言：{info.language}  共 {len(segs)} 个字幕段")
    return segs


def segments_to_timed_text(segments: list) -> str:
    """转成 [开始秒-结束秒] 文字 的格式，方便AI分析"""
    lines = []
    for seg in segments:
        lines.append(f"[{int(seg.start)}-{int(seg.end)}] {seg.text.strip()}")
    return "\n".join(lines)


def get_context_around(segments: list, ts: float, window: float = 30.0) -> str:
    """获取某个时间戳前后 window 秒的字幕文本"""
    start = max(0, ts - window)
    end = ts + window
    texts = [seg.text.strip() for seg in segments if start <= seg.start <= end]
    return " ".join(texts)


# ── Step 4: AI 分析关键知识点 ───────────────────────────────────────────────────

def analyze_keypoints(title: str, segments: list) -> list[dict]:
    timed_text = segments_to_timed_text(segments)

    # 超长转录分批处理（超过 60000 字符则裁剪，保留首尾）
    max_chars = 60000
    if len(timed_text) > max_chars:
        half = max_chars // 2
        timed_text = timed_text[:half] + "\n...(中间内容略)...\n" + timed_text[-half:]

    prompt = KEYPOINT_PROMPT.format(
        max_frames=MAX_KEYFRAMES,
        title=title,
        transcript_with_ts=timed_text,
    )

    print("AI 分析关键知识点 …")
    raw = ai_text(prompt, fast=True, max_tokens=4096)

    # 从返回中提取 JSON
    json_match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not json_match:
        raise ValueError(f"AI 未返回有效 JSON，原始输出：\n{raw[:500]}")

    keypoints = json.loads(json_match.group(0))
    print(f"✓ 识别到 {len(keypoints)} 个关键知识点")
    return keypoints


# ── Step 5: 截取关键帧 ──────────────────────────────────────────────────────────

def extract_frame(video_path: str, timestamp_sec: float, output_path: str) -> bool:
    """在 timestamp_sec 处截一帧，返回是否成功"""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp_sec),
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",        # 高质量 JPEG
        "-vf", "scale=1280:-2",  # 最宽 1280px
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and os.path.exists(output_path)


def pick_best_frames(video_path: str, ts: float, frames_dir: str, idx: int,
                     max_count: int = 2) -> list:
    """
    在知识点时间戳附近扫描多帧，返回最多 max_count 张最优帧路径列表。

    扫描策略：
    - 向后扫 0~8 秒（标题/字幕通常在讲解开始后几秒才出现）
    - 适当向前扫 1~2 秒（有些片头在转场前）
    - 文件大小作为"画面内容丰富度"的代理指标
    - 两帧之间时间间隔 >= 2 秒，避免重复相似帧
    """
    offsets = [0, 1, 2, 3, 4, 5, 6, 7, 8, -1, -2]
    candidates = []
    for offset in offsets:
        t = max(0, ts + offset)
        path = os.path.join(frames_dir, f"_cand_{idx}_{offset:+d}.jpg")
        if extract_frame(video_path, t, path):
            size = os.path.getsize(path)
            candidates.append((size, float(t), path))

    if not candidates:
        return []

    # 按内容丰富度降序
    candidates.sort(reverse=True)

    # 选出时间间隔 >= 2s 的前 max_count 帧
    selected: list[tuple] = []
    for size, t, path in candidates:
        if all(abs(t - st) >= 2.0 for _, st, _ in selected):
            selected.append((size, t, path))
            if len(selected) >= max_count:
                break

    # 按时间顺序排列，视觉更自然
    selected.sort(key=lambda x: x[1])

    # 重命名为正式帧（frame_01a.jpg, frame_01b.jpg …）
    final_paths = []
    used_paths = set()
    for sub_idx, (_, _, path) in enumerate(selected):
        suffix = chr(ord("a") + sub_idx)
        final_path = os.path.join(frames_dir, f"frame_{idx:02d}{suffix}.jpg")
        os.rename(path, final_path)
        final_paths.append(final_path)
        used_paths.add(path)

    # 清理所有未用候选
    for _, _, path in candidates:
        if path not in used_paths:
            try:
                os.remove(path)
            except OSError:
                pass

    return final_paths


# ── Step 6: AI 视觉生成笔记 ─────────────────────────────────────────────────────

def generate_note(frame_paths: list, kp: dict, segments: list) -> str:
    """
    frame_paths: 该知识点对应的截图列表（1~2 张）
    同时把所有截图发给视觉模型，让 AI 综合画面内容生成笔记。
    """
    ctx = get_context_around(segments, kp["timestamp_sec"], window=25.0)
    n_shots = len(frame_paths)
    shot_desc = "一张截图" if n_shots == 1 else f"{n_shots} 张截图（按时间先后顺序）"
    prompt = VISION_NOTE_PROMPT.format(
        title=kp["title"],
        timestamp=format_hms(kp["timestamp_sec"]),
        subtitle_context=ctx or "（该时段无字幕）",
    ).replace(
        "我给你一张视频截图",
        f"我给你 {shot_desc}",
    )
    try:
        note = ai_vision(frame_paths, prompt)
    except Exception as e:
        # 视觉调用失败则降级为纯文本
        print(f"  ⚠ 视觉分析失败（{e}），改用文本模式")
        note = ai_text(
            f"知识点：{kp['title']}\n字幕：{ctx}\n\n请生成学习笔记（核心要点 + 详细说明）",
            fast=False,
        )
    return note


# ── Step 7: 生成 Markdown ───────────────────────────────────────────────────────

def generate_tutorial(
    title: str,
    url: str,
    keypoints: list,
    notes: list,
    frames_dir: str,
    frame_paths: list,          # list[list[str]]，每个知识点对应 1~2 张帧路径
    output_path: str,
    overview: str = "",
) -> None:
    if not overview:
        kp_list = "\n".join(f"- {kp['title']}（{format_hms(kp['timestamp_sec'])}）：{kp['summary']}"
                            for kp in keypoints)
        print("生成总览介绍 …")
        overview = ai_text(SUMMARY_PROMPT.format(title=title, url=url, keypoints=kp_list), fast=True)

    # 目录
    toc_lines = []
    for i, kp in enumerate(keypoints, 1):
        anchor = re.sub(r'[^\w一-鿿-]', '', kp["title"]).lower()
        toc_lines.append(f"{i}. [{kp['title']}](#{anchor})")

    # 正文
    sections = []
    for i, (kp, note, fps) in enumerate(zip(keypoints, notes, frame_paths), 1):
        # fps 是该知识点对应的帧路径列表（1~2 张），也可能为空列表
        if fps:
            imgs_md = "\n\n".join(
                f"![{kp['title']}截图{chr(ord('A') + j)}]"
                f"({os.path.relpath(fp, os.path.dirname(output_path))})"
                for j, fp in enumerate(fps)
            )
        else:
            imgs_md = "*（截帧失败）*"

        section = (
            f"## {i}. {kp['title']}\n\n"
            f"**时间戳：** [{format_hms(kp['timestamp_sec'])}]({url}&t={kp['timestamp_sec']})\n\n"
            f"{imgs_md}\n\n"
            f"{note.strip()}\n\n"
            f"---\n"
        )
        sections.append(section)

    md = f"""# {title}

> **原始视频：** {url}
> **生成方式：** AI 自动分析字幕，提取关键帧，生成图文笔记
> **关键知识点：** 共 {len(keypoints)} 个

---

## 视频总览

{overview}

---

## 目录

{chr(10).join(toc_lines)}

---

{chr(10).join(sections)}

*本教程由 `bili_to_tutorial.py` 自动生成*
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"✓ 教程已保存：{output_path}")


# ── 缓存检测 ───────────────────────────────────────────────────────────────────

class Cache:
    """检测工作目录中哪些步骤已经完成，避免重复计算"""

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        self.frames_dir = os.path.join(work_dir, "frames")

    def video(self) -> str | None:
        """已下载的 mp4 文件路径"""
        mp4s = sorted(
            [f for f in os.listdir(self.work_dir) if f.endswith(".mp4")],
            key=lambda f: os.path.getmtime(os.path.join(self.work_dir, f)),
            reverse=True,
        ) if os.path.isdir(self.work_dir) else []
        return os.path.join(self.work_dir, mp4s[0]) if mp4s else None

    def srt(self) -> str | None:
        p = os.path.join(self.work_dir, "subtitle.srt")
        return p if os.path.exists(p) else None

    def keypoints(self) -> list | None:
        p = os.path.join(self.work_dir, "keypoints.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return None

    def frames(self) -> list:
        """
        返回每个知识点对应的帧路径列表，形如 [[frame_01a.jpg, frame_01b.jpg], [frame_02a.jpg], ...]
        兼容旧格式 frame_01.jpg（视为单帧）
        """
        if not os.path.isdir(self.frames_dir):
            return []
        groups: dict[int, list[str]] = {}
        for fname in os.listdir(self.frames_dir):
            # 新格式: frame_01a.jpg / frame_01b.jpg
            m = re.match(r"frame_(\d+)([a-z])\.jpg", fname)
            if m:
                num = int(m.group(1))
                groups.setdefault(num, []).append(
                    os.path.join(self.frames_dir, fname)
                )
                continue
            # 旧格式兼容: frame_01.jpg
            m2 = re.match(r"frame_(\d+)\.jpg", fname)
            if m2:
                num = int(m2.group(1))
                groups.setdefault(num, []).append(
                    os.path.join(self.frames_dir, fname)
                )
        # 每组内按文件名排序保证 a < b
        result = []
        for k in sorted(groups.keys()):
            result.append(sorted(groups[k]))
        return result

    def notes(self) -> list | None:
        p = os.path.join(self.work_dir, "notes.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return None


def save_json(data, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_srt_as_segments(srt_path: str) -> list:
    """把 SRT 文件解析成兼容 faster-whisper segment 的对象列表"""
    class FakeSeg:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    segs = []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r"\n\n+", content.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        time_line = lines[1]
        m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", time_line)
        if not m:
            continue
        start = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
        end   = int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7]) + int(m[8]) / 1000
        text = " ".join(lines[2:])
        segs.append(FakeSeg(start, end, text))
    return segs


# ── 飞书上传 ───────────────────────────────────────────────────────────────────

def make_feishu_md(
    title: str,
    url: str,
    keypoints: list,
    notes: list,
    overview: str,
) -> str:
    """
    生成适合飞书的 Markdown 版本：
    - 图片替换为「▶ 跳转到该视频时刻」链接（飞书不支持本地图片路径）
    - 保留完整笔记内容
    """
    toc_lines = [
        f"{i}. {kp['title']}（{format_hms(kp['timestamp_sec'])}）"
        for i, kp in enumerate(keypoints, 1)
    ]

    sections = []
    for i, (kp, note) in enumerate(zip(keypoints, notes), 1):
        ts = kp["timestamp_sec"]
        video_link = f"{url}&t={int(ts)}" if "?" in url else f"{url}?t={int(ts)}"
        section = (
            f"## {i}. {kp['title']}\n\n"
            f"**时间戳：** {format_hms(ts)}  |  "
            f"[▶ 跳转到视频该时刻]({video_link})\n\n"
            f"> {kp['summary']}\n\n"
            f"{note}\n\n---\n"
        )
        sections.append(section)

    return (
        f"# {title}\n\n"
        f"> **原始视频：** {url}\n"
        f"> **关键知识点：** 共 {len(keypoints)} 个\n\n"
        f"---\n\n"
        f"## 视频总览\n\n{overview}\n\n"
        f"---\n\n"
        f"## 目录\n\n" + "\n".join(toc_lines) + "\n\n---\n\n"
        + "\n".join(sections)
        + "\n*本教程由 `bili_to_tutorial.py` 自动生成*\n"
    )


def upload_to_feishu(title: str, content: str) -> None:
    result = subprocess.run(
        [LARK_CLI, "docs", "+create", "--title", title, "--markdown", content],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"✓ 飞书文档已创建！\n{result.stdout.strip()}")
    else:
        print(f"✗ 飞书写入失败：{result.stderr.strip()}")
        print("请先运行：lark-cli auth login --recommend")


# ── 主流程 ─────────────────────────────────────────────────────────────────────

def run(
    url: str,
    output_base: str = OUTPUT_BASE,
    model_size: str = "medium",
    language: str | None = None,
    keep_video: bool = True,
    upload_feishu: bool = True,
) -> None:
    url = url.strip()
    if url.startswith("BV"):
        url = f"https://www.bilibili.com/video/{url}/"

    # ── 获取视频信息（始终需要，用于标题和目录路径）
    print("\n[1/7] 获取视频信息 …")
    info = get_video_info(url)
    title = info.get("title", "未命名视频")
    print(f"✓ 标题：{title}")

    # ── 准备目录 & 缓存检测
    safe_title = safe_filename(title)[:50]
    work_dir = os.path.join(output_base, safe_title)
    frames_dir = os.path.join(work_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    cache = Cache(work_dir)

    # ── 下载视频（有缓存则跳过）
    cached_video = cache.video()
    if cached_video:
        print(f"\n[2/7] 跳过下载（缓存命中）：{os.path.basename(cached_video)}")
        video_path = cached_video
    else:
        print("\n[2/7] 下载视频 …")
        video_path = download_video(url, work_dir)

    # ── 转录（有 SRT 缓存则跳过）
    cached_srt = cache.srt()
    if cached_srt:
        print(f"\n[3/7] 跳过转录（缓存命中）：subtitle.srt")
        print(f"\n[4/7] 读取字幕缓存 …")
        segments = load_srt_as_segments(cached_srt)
        print(f"✓ 读取 {len(segments)} 个字幕段")
    else:
        print("\n[3/7] 提取音频 …")
        audio_path = extract_audio(video_path, work_dir)

        print("\n[4/7] 语音转文字（Whisper）…")
        segments = transcribe(audio_path, model_size=model_size, language=language)

        srt_path = os.path.join(work_dir, "subtitle.srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, 1):
                f.write(f"{i}\n{format_timestamp_srt(seg.start)} --> {format_timestamp_srt(seg.end)}\n{seg.text.strip()}\n\n")
        print(f"✓ 字幕已保存：{srt_path}")

    # ── AI 分析关键点（有缓存则跳过）
    cached_kp = cache.keypoints()
    if cached_kp:
        print(f"\n[5/7] 跳过 AI 分析（缓存命中）：{len(cached_kp)} 个知识点")
        keypoints = cached_kp
    else:
        print("\n[5/7] AI 分析关键知识点 …")
        keypoints = analyze_keypoints(title, segments)
        save_json(keypoints, os.path.join(work_dir, "keypoints.json"))

    # ── 截帧（已有帧且分组数量匹配则跳过）
    cached_frames = cache.frames()
    if cached_frames and len(cached_frames) == len(keypoints):
        total_cached = sum(len(fps) for fps in cached_frames)
        print(f"\n[6/7] 跳过截帧（缓存命中）：{len(cached_frames)} 个知识点 / {total_cached} 张帧")
        frame_paths = cached_frames
    else:
        print("\n[6/7] 截取关键帧（每个知识点最多 2 张）…")
        frame_paths = []
        for i, kp in enumerate(keypoints, 1):
            ts = kp["timestamp_sec"]
            print(f"  [{i}/{len(keypoints)}] {format_hms(ts)} — {kp['title']}")
            fps = pick_best_frames(video_path, ts, frames_dir, i, max_count=2)
            frame_paths.append(fps)
            if fps:
                print(f"    ✓ {', '.join(os.path.basename(fp) for fp in fps)}")
            else:
                print(f"    ✗ 截帧失败")

    # ── AI 笔记生成（有缓存则跳过）
    cached_notes = cache.notes()
    if cached_notes and len(cached_notes) == len(keypoints):
        print(f"\n[7/7] 跳过笔记生成（缓存命中）：{len(cached_notes)} 条笔记")
        notes = cached_notes
    else:
        print("\n[7/7] AI 生成每个知识点的笔记 …")
        notes = []
        for i, (kp, fps) in enumerate(zip(keypoints, frame_paths), 1):
            print(f"  [{i}/{len(keypoints)}] {kp['title']} …")
            note = generate_note(fps, kp, segments) if fps else ai_text(
                f"知识点：{kp['title']}\n字幕：{get_context_around(segments, kp['timestamp_sec'])}\n生成学习笔记",
            )
            notes.append(note)
        save_json(notes, os.path.join(work_dir, "notes.json"))

    # ── 生成总览（本地 Markdown 和飞书版共用）
    kp_list = "\n".join(
        f"- {kp['title']}（{format_hms(kp['timestamp_sec'])}）：{kp['summary']}"
        for kp in keypoints
    )
    print("\n生成总览介绍 …")
    overview = ai_text(SUMMARY_PROMPT.format(title=title, url=url, keypoints=kp_list), fast=True)

    # ── 生成本地 Markdown（含截图）
    output_md = os.path.join(work_dir, "tutorial.md")
    generate_tutorial(title, url, keypoints, notes, frames_dir, frame_paths, output_md, overview=overview)

    # ── 上传飞书
    if upload_feishu:
        print("\n上传到飞书 …")
        feishu_md = make_feishu_md(title, url, keypoints, notes, overview)
        upload_to_feishu(title=f"【图文教程】{title}", content=feishu_md)
    else:
        print("\n跳过飞书上传（upload_feishu=False）")

    if not keep_video:
        os.remove(video_path)

    print(f"\n✅ 完成！")
    print(f"   本地教程（含截图）：{output_md}")
    print(f"   缓存目录：{work_dir}")


# ── 入口 ───────────────────────────────────────────────────────────────────────

def collect_urls() -> list[str]:
    """从命令行参数或交互式输入收集多个视频链接"""
    if len(sys.argv) > 1:
        return list(sys.argv[1:])

    print("请输入 B 站视频链接或 BV 号，每行一个，输入空行结束：")
    urls = []
    while True:
        line = input(f"  链接 {len(urls) + 1}：").strip()
        if not line:
            if urls:
                break
            print("  至少输入一个链接")
        else:
            urls.append(line)
    return urls


if __name__ == "__main__":
    urls = collect_urls()
    total = len(urls)
    results = {"成功": [], "失败": []}

    for idx, url in enumerate(urls, 1):
        print(f"\n{'=' * 60}")
        print(f"[{idx}/{total}] 开始处理：{url}")
        print(f"{'=' * 60}")
        try:
            run(
                url=url,
                output_base=OUTPUT_BASE,
                model_size="medium",      # Whisper 模型：tiny / small / medium / large
                language=None,            # None=自动检测  "zh"=中文  "en"=英文
                keep_video=True,
                upload_feishu=True,
            )
            results["成功"].append(url)
        except Exception as e:
            print(f"\n✗ 处理失败：{e}")
            results["失败"].append((url, str(e)))

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"全部完成：{len(results['成功'])}/{total} 成功")
    for url in results["成功"]:
        print(f"  ✓ {url}")
    for url, err in results["失败"]:
        print(f"  ✗ {url}\n    原因：{err}")
    print(f"{'=' * 60}")
