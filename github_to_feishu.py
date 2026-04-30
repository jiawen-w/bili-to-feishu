"""
GitHub 仓库 → AI 重构 → 飞书知识库文章
输入一个 GitHub 仓库/目录链接，自动抓取代码和文档，生成知识库教程上传飞书

依赖安装:
    pip install -r requirements.txt

配置:
    复制 .env.example 为 .env，填入你的 API Key 和路径
"""

import os
import re
import sys
import subprocess
import requests
from dotenv import load_dotenv

load_dotenv()

# ── 配置（从 .env 读取）──────────────────────────────────────────────────────

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding")
AI_API_KEY  = os.getenv("AI_API_KEY", "")
AI_MODEL    = os.getenv("AI_MODEL", "doubao-seed-2.0-pro")
LARK_CLI    = os.getenv("LARK_CLI", "lark-cli")

# 抓取这些扩展名的文件
INCLUDE_EXTS = {".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".txt", ".sh"}
# 跳过这些目录
SKIP_DIRS    = {"node_modules", ".git", "__pycache__", ".venv", "dist", "build", "evals", "reports"}

PROMPT_TEMPLATE = """\
你是专业技术文档/知识库编撰工程师，我给你一个 GitHub 项目的完整源码和文档，请把它重构为**标准化精品技术知识库教程**。

输出结构：
1. 教程名称
2. 适用人群 & 学习前置条件
3. 项目简介 & 核心价值
4. 分章节结构化正文（层级标题清晰）
5. 实操步骤拆解（逐条可跟着照做）
6. 核心原理讲解（把代码逻辑转化为专业原理说明）
7. 配置/命令/关键参数汇总表
8. 坑点避坑 & 注意事项
9. 章节知识点复盘
10. 课后小结 & 延伸学习建议

写作要求：
1. 把代码和文档转化为书面教程，精简冗余，保留所有干货细节
2. 代码、命令、路径、配置单独代码块展示
3. 关键操作、易错点、核心结论加粗
4. 逻辑重新归纳，做成永久可复用知识库
5. 语言严谨、条理清晰，适合收藏、内部团队学习使用

项目名称：{title}
项目链接：{url}

项目内容：
{content}
"""


# ── GitHub 内容抓取 ────────────────────────────────────────────────────────────

def parse_github_url(url: str) -> tuple[str, str, str]:
    """解析 GitHub URL，返回 (owner, repo, path)"""
    url = url.rstrip("/")
    # https://github.com/owner/repo/tree/branch/path
    match = re.match(
        r"https://github\.com/([^/]+)/([^/]+)(?:/tree/[^/]+/(.*))?",
        url
    )
    if not match:
        raise ValueError(f"无法解析 GitHub URL：{url}")
    owner = match.group(1)
    repo  = match.group(2)
    path  = match.group(3) or ""
    return owner, repo, path


def fetch_dir(owner: str, repo: str, path: str, depth: int = 0) -> list[dict]:
    """递归获取目录下所有文件信息"""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    resp = requests.get(api_url, timeout=15)
    resp.raise_for_status()
    items = resp.json()

    files = []
    for item in items:
        name = item["name"]
        if item["type"] == "dir":
            if name in SKIP_DIRS:
                continue
            files.extend(fetch_dir(owner, repo, item["path"], depth + 1))
        elif item["type"] == "file":
            ext = os.path.splitext(name)[1].lower()
            if ext in INCLUDE_EXTS:
                files.append(item)
    return files


def fetch_file_content(download_url: str) -> str:
    resp = requests.get(download_url, timeout=15)
    resp.raise_for_status()
    return resp.text


def fetch_repo_content(github_url: str) -> tuple[str, str]:
    """抓取仓库内容，返回 (项目标题, 合并后的文本)"""
    owner, repo, path = parse_github_url(github_url)
    title = f"{repo}/{path}" if path else repo

    print(f"  仓库：{owner}/{repo}")
    print(f"  目录：{path or '（根目录）'}")

    files = fetch_dir(owner, repo, path)
    print(f"  找到 {len(files)} 个文件")

    parts = [f"# 项目：{title}\n来源：{github_url}\n"]

    for f in files:
        print(f"  读取：{f['path']}")
        try:
            content = fetch_file_content(f["download_url"])
            ext = os.path.splitext(f["name"])[1]
            parts.append(f"\n\n---\n## 文件：{f['path']}\n\n```{ext.lstrip('.')}\n{content}\n```")
        except Exception as e:
            parts.append(f"\n\n---\n## 文件：{f['path']}\n\n（读取失败：{e}）")

    return title, "\n".join(parts)


# ── AI 重构 ────────────────────────────────────────────────────────────────────

def ai_restructure(title: str, url: str, content: str) -> str:
    import anthropic

    print("\nAI 重构中，请稍候 …")
    client = anthropic.Anthropic(api_key=AI_API_KEY, base_url=AI_BASE_URL)

    prompt = PROMPT_TEMPLATE.format(title=title, url=url, content=content)

    message = client.messages.create(
        model=AI_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    result = message.content[0].text
    print("✓ AI 重构完成")
    return result


# ── 写入飞书 ───────────────────────────────────────────────────────────────────

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


# ── 主流程 ─────────────────────────────────────────────────────────────────────

def run(github_url: str, output_dir: str = "~/Downloads/github_to_feishu", upload_feishu: bool = True) -> None:
    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 50)
    print(f"[1/3] 抓取 GitHub 内容 …")
    title, repo_content = fetch_repo_content(github_url)

    # 保存原始内容
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
    raw_path = os.path.join(output_dir, f"{safe_title}_raw.md")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(repo_content)
    print(f"✓ 原始内容已保存：{raw_path}")

    print(f"\n[2/3] AI 重构为知识库文章 …")
    article = ai_restructure(title=title, url=github_url, content=repo_content)

    article_path = os.path.join(output_dir, f"{safe_title}_知识库.md")
    with open(article_path, "w", encoding="utf-8") as f:
        f.write(article)
    print(f"✓ 知识库文章已保存：{article_path}")

    if upload_feishu:
        print(f"\n[3/3] 写入飞书 …")
        upload_to_feishu(title=f"【知识库】{title}", content=article)
    else:
        print(f"\n[3/3] 跳过飞书上传")

    print("\n✅ 全部完成！")


# ── 入口 ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("请输入 GitHub 仓库/目录链接：").strip()

    run(
        github_url=url,
        output_dir="~/Downloads/github_to_feishu",
        upload_feishu=True,
    )
