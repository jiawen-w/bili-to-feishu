"""
音频转字幕 / 文档，并可直接写入飞书
依赖: pip install faster-whisper
飞书写入: npm install -g @larksuite/cli  （首次需 lark-cli auth login）
模型首次运行会自动下载（约 1-3 GB，取决于选择的 model_size）
"""

import os
import datetime
import subprocess


def format_timestamp(seconds: float) -> str:
    td = datetime.timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    ms = int((seconds - int(seconds)) * 1000)
    h, remainder = divmod(total_seconds, 3600)
    m, s = divmod(remainder, 60)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def upload_to_feishu(title: str, content: str) -> None:
    """用 lark-cli 把文本写入飞书文档（需提前 lark-cli auth login）"""
    result = subprocess.run(
        ["lark-cli", "docs", "+create", "--title", title, "--markdown", content],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"飞书文档已创建！\n{result.stdout.strip()}")
    else:
        print(f"飞书写入失败：{result.stderr.strip()}")


def transcribe(
    audio_path: str,
    output_dir: str | None = None,
    model_size: str = "medium",
    language: str | None = None,
    save_srt: bool = True,
    save_txt: bool = True,
    upload_feishu: bool = False,
) -> None:
    """
    将音频文件转为字幕（.srt）和纯文本（.txt）。

    model_size 可选（越大越准，越慢）:
      tiny   — 最快，准确率一般
      base   — 较快
      small  — 均衡
      medium — 推荐，中文效果好（默认）
      large  — 最准，较慢

    language:      指定语言可加速，如 "zh"（中文）、"en"（英文），None 自动检测
    upload_feishu: 是否将文本写入飞书文档
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        import subprocess, sys
        print("正在安装 faster-whisper …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "faster-whisper"])
        from faster_whisper import WhisperModel

    audio_path = os.path.expanduser(audio_path)
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"找不到文件：{audio_path}")

    if output_dir is None:
        output_dir = os.path.dirname(audio_path)
    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    base_name = os.path.splitext(os.path.basename(audio_path))[0]

    print(f"加载模型：{model_size}")
    model = WhisperModel(model_size, device="auto", compute_type="auto")

    print(f"转录中：{audio_path}\n")
    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        vad_filter=True,   # 过滤静音段
    )
    print(f"检测语言：{info.language}（置信度 {info.language_probability:.0%}）\n")

    segment_list = list(segments)

    if save_srt:
        srt_path = os.path.join(output_dir, base_name + ".srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segment_list, 1):
                f.write(f"{i}\n")
                f.write(f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n")
                f.write(f"{seg.text.strip()}\n\n")
        print(f"字幕已保存：{srt_path}")

    full_text = "\n".join(seg.text.strip() for seg in segment_list)

    if save_txt:
        txt_path = os.path.join(output_dir, base_name + ".txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(full_text)
        print(f"文本已保存：{txt_path}")

    if upload_feishu:
        print("\n正在写入飞书 …")
        upload_to_feishu(title=base_name, content=full_text)


# ── 示例 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    transcribe(
        audio_path="~/Downloads/Youtube视频/你的音频文件.mp3",  # 改成实际文件路径
        output_dir="~/Downloads/Youtube视频",                   # 输出目录，默认同音频目录
        model_size="medium",                                     # 模型大小
        language="zh",                                           # 中文；None = 自动检测
        save_srt=True,                                           # 生成 .srt 字幕
        save_txt=True,                                           # 生成 .txt 纯文本
        upload_feishu=True,                                      # 写入飞书文档
    )
