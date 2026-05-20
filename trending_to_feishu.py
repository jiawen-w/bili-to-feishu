"""
GitHub Trending → AI 过滤 → 飞书知识库
每日自动爬取 GitHub Trending，筛选 AI 相关仓库，跨日去重后写入飞书

功能：
1. 抓取 https://github.com/trending 当日热门仓库（默认 daily）
2. 关键词快速过滤 AI 相关仓库
3. 对比缓存，跳过已处理过的仓库（跨日去重）
4. 为每个新 AI 仓库抓取源码/文档 → AI 重构知识库文章 → 写入飞书
5. 额外生成一份"今日 AI 趋势日报"汇总写入飞书
6. 更新本地缓存

依赖：
    pip install requests beautifulsoup4 anthropic

用法：
    python trending_to_feishu.py                # 正常运行
    python trending_to_feishu.py --dry-run      # 预览，不调用 AI 和飞书
    python trending_to_feishu.py --no-feishu    # 只生成本地文件，不上传飞书
"""

import os
import re
import sys
import json
import subprocess
import requests
from datetime import datetime
from bs4 import BeautifulSoup


# ── 配置（与 github_to_feishu.py 保持一致）─────────────────────────────────────

AI_BASE_URL  = "https://ark.cn-beijing.volces.com/api/coding"
AI_API_KEY   = "1d3ace95-c577-4eee-ae9d-4fc85f3d07ee"
AI_MODEL     = "doubao-seed-2.0-pro"
LARK_CLI     = "/Users/chenjiawen/.hermes/node/bin/lark-cli"

# 输出目录 & 缓存文件
OUTPUT_DIR   = os.path.expanduser("~/Downloads/trending_to_feishu")
CACHE_FILE   = os.path.join(OUTPUT_DIR, "seen_repos.json")

# 每次最多处理几个仓库（防止 API 费用过高）
MAX_REPOS_PER_RUN = 5

# 抓取这些扩展名的文件（与 github_to_feishu.py 一致）
INCLUDE_EXTS = {".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".txt", ".sh"}
SKIP_DIRS    = {"node_modules", ".git", "__pycache__", ".venv", "dist", "build", "evals", "reports"}

# 仓库内容超过此字符数时截断，避免 token 超限
MAX_CONTENT_CHARS = 60_000


# ── AI 相关关键词 ──────────────────────────────────────────────────────────────

# 强关键词：命中即判定为 AI 相关
AI_KEYWORDS_STRONG = {
    "llm", "gpt", "chatgpt", "openai", "anthropic", "claude", "gemini",
    "langchain", "llamaindex", "huggingface", "diffusion", "stable-diffusion",
    "midjourney", "vllm", "ollama", "rag", "embedding", "vector-db",
    "fine-tun", "lora", "qlora", "gguf", "ggml", "quantiz",
    "transformer", "attention", "bert", "llama", "mistral", "qwen",
    "whisper", "tts", "asr", "multimodal", "vision-language",
    "text-to-image", "image-to-text", "text-generation", "copilot",
    "大模型", "语言模型", "人工智能", "生成式", "文生图", "图生文",
    "自然语言处理", "向量数据库", "知识图谱",
}

# 弱关键词：需要组合出现或在关键位置才算 AI
AI_KEYWORDS_WEAK = {
    "ai", "ml", "neural", "model", "inference", "training", "dataset",
    "agent", "chatbot", "nlp", "computer-vision", "deep-learning",
    "machine-learning", "reinforcement", "reward", "alignment", "rlhf",
    "foundation", "generative", "synthetic", "prompt", "token", "context",
    "cuda", "gpu", "accelerate", "机器学习", "深度学习", "神经网络",
    "训练", "推理", "微调", "向量", "模型",
}


def is_ai_related(repo: dict) -> bool:
    """
    两级关键词判断：
    - 命中强关键词 → 直接判定为 AI 相关
    - 命中 2 个及以上弱关键词 → 判定为 AI 相关
    """
    text = f"{repo['full_name']} {repo['description']}".lower()
    # 强关键词
    for kw in AI_KEYWORDS_STRONG:
        if kw in text:
            return True
    # 弱关键词：至少命中 2 个
    hits = sum(1 for kw in AI_KEYWORDS_WEAK if kw in text)
    return hits >= 2


# ── 缓存（跨日去重）──────────────────────────────────────────────────────────

def load_cache() -> dict:
    """
    缓存格式：
    {
      "seen": ["owner/repo1", "owner/repo2", ...],
      "history": [
        {"date": "2026-05-09", "repos": ["owner/repo1", ...]},
        ...
      ]
    }
    """
    if not os.path.exists(CACHE_FILE):
        return {"seen": [], "history": []}
    with open(CACHE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"✓ 缓存已更新：{CACHE_FILE}")


# ── 抓取 GitHub Trending ───────────────────────────────────────────────────────

def fetch_trending(language: str = "", since: str = "daily") -> list[dict]:
    """
    抓取 GitHub Trending，返回仓库列表。
    每条记录：{full_name, url, description, language, stars_today}
    """
    url = f"https://github.com/trending/{language}?since={since}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    print(f"  抓取 GitHub Trending：{url}")
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    repos = []

    for article in soup.select("article.Box-row"):
        # 仓库名
        h2 = article.select_one("h2 a")
        if not h2:
            continue
        full_name = h2.get("href", "").strip("/")  # "owner/repo"
        if not full_name or "/" not in full_name:
            continue

        # 简介
        desc_el = article.select_one("p")
        description = desc_el.get_text(strip=True) if desc_el else ""

        # 编程语言
        lang_el = article.select_one("[itemprop='programmingLanguage']")
        lang = lang_el.get_text(strip=True) if lang_el else ""

        # 今日 Star 数
        stars_today = ""
        for span in article.select("span.d-inline-block"):
            txt = span.get_text(strip=True)
            if "stars today" in txt or "star today" in txt:
                stars_today = txt
                break

        # 总 Star 数
        total_stars = ""
        for a in article.select("a.Link--muted"):
            href = a.get("href", "")
            if href.endswith("/stargazers"):
                total_stars = a.get_text(strip=True)
                break

        repos.append({
            "full_name": full_name,
            "url": f"https://github.com/{full_name}",
            "description": description,
            "language": lang,
            "stars_today": stars_today,
            "total_stars": total_stars,
        })

    return repos


# ── 复用 github_to_feishu.py 的核心逻辑 ──────────────────────────────────────

def fetch_dir(owner: str, repo: str, path: str) -> list[dict]:
    """递归获取目录下所有文件信息（来自 github_to_feishu.py）"""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        resp = requests.get(api_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠ GitHub API 请求失败：{e}")
        return []

    items = resp.json()
    if not isinstance(items, list):
        return []

    files = []
    for item in items:
        name = item.get("name", "")
        if item["type"] == "dir":
            if name in SKIP_DIRS:
                continue
            files.extend(fetch_dir(owner, repo, item["path"]))
        elif item["type"] == "file":
            ext = os.path.splitext(name)[1].lower()
            if ext in INCLUDE_EXTS:
                files.append(item)
    return files


def fetch_repo_content(github_url: str) -> tuple[str, str]:
    """
    抓取仓库内容，返回 (项目标题, 合并后的文本)
    内容超过 MAX_CONTENT_CHARS 时自动截断
    （来自 github_to_feishu.py，增加截断保护）
    """
    url = github_url.rstrip("/")
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)(?:/tree/[^/]+/(.*))?", url)
    if not match:
        raise ValueError(f"无法解析 GitHub URL：{url}")
    owner = match.group(1)
    repo  = match.group(2)
    path  = match.group(3) or ""
    title = f"{repo}/{path}" if path else repo

    print(f"  仓库：{owner}/{repo}")
    files = fetch_dir(owner, repo, path)
    print(f"  找到 {len(files)} 个文件")

    parts = [f"# 项目：{title}\n来源：{github_url}\n"]
    total_chars = 0

    for f in files:
        if total_chars >= MAX_CONTENT_CHARS:
            parts.append(f"\n\n---\n**（内容过长，已截断，后续 {len(files)} 个文件略过）**")
            break
        print(f"  读取：{f['path']}")
        try:
            resp = requests.get(f["download_url"], timeout=15)
            resp.raise_for_status()
            content = resp.text
            ext = os.path.splitext(f["name"])[1]
            chunk = f"\n\n---\n## 文件：{f['path']}\n\n```{ext.lstrip('.')}\n{content}\n```"
            parts.append(chunk)
            total_chars += len(chunk)
        except Exception as e:
            parts.append(f"\n\n---\n## 文件：{f['path']}\n\n（读取失败：{e}）")

    return title, "\n".join(parts)


KNOWLEDGE_PROMPT = """\
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


def ai_restructure(title: str, url: str, content: str) -> str:
    """AI 重构为知识库文章（来自 github_to_feishu.py）"""
    import anthropic
    print("  AI 重构中，请稍候 …")
    client = anthropic.Anthropic(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    prompt = KNOWLEDGE_PROMPT.format(title=title, url=url, content=content)
    message = client.messages.create(
        model=AI_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    print("  ✓ AI 重构完成")
    return message.content[0].text


def upload_to_feishu(title: str, content: str) -> str:
    """写入飞书，返回文档 URL（来自 github_to_feishu.py）"""
    result = subprocess.run(
        [LARK_CLI, "docs", "+create",
         "--api-version", "v2",
         "--title", title,
         "--content", content,
         "--doc-format", "markdown"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ✗ 飞书写入失败：{result.stderr.strip()}")
        return ""
    try:
        import json as _json
        data = _json.loads(result.stdout)
        doc_url = data.get("data", {}).get("document", {}).get("url", "")
        if doc_url:
            print(f"  ✓ 飞书文档已创建：{doc_url}")
        return doc_url
    except Exception:
        print(f"  ✓ 飞书文档已创建\n{result.stdout.strip()}")
        return ""


# ── 今日日报生成 ─────────────────────────────────────────────────────────────

def build_digest(
    date_str: str,
    all_ai_repos: list[dict],
    new_repos: list[dict],
    processed: list[dict],
) -> str:
    """生成今日 AI 趋势日报 Markdown"""
    lines = [
        f"# GitHub AI 趋势日报 · {date_str}",
        "",
        f"> 今日 GitHub Trending 共发现 **{len(all_ai_repos)}** 个 AI 相关仓库，"
        f"其中 **{len(new_repos)}** 个为新上榜，已为 **{len(processed)}** 个生成知识库文章。",
        "",
        "---",
        "",
        "## 今日新上榜 AI 仓库",
        "",
        "| 仓库 | 简介 | 语言 | 今日 Star |",
        "|---|---|---|---|",
    ]
    for r in new_repos:
        name = r["full_name"]
        url  = r["url"]
        desc = r["description"][:60] + "…" if len(r["description"]) > 60 else r["description"]
        lang = r["language"] or "-"
        stars = r["stars_today"] or "-"
        lines.append(f"| [{name}]({url}) | {desc} | {lang} | {stars} |")

    if processed:
        lines += [
            "",
            "---",
            "",
            "## 已生成知识库文章",
            "",
        ]
        for r in processed:
            doc_url = r.get("doc_url", "")
            link = f"[飞书文档]({doc_url})" if doc_url else "（本地已保存）"
            lines.append(f"- **[{r['full_name']}]({r['url']})** — {r['description'][:80]}  \n  {link}")

    if len(all_ai_repos) > len(new_repos):
        skipped = [r for r in all_ai_repos if r["full_name"] not in {n["full_name"] for n in new_repos}]
        lines += [
            "",
            "---",
            "",
            f"## 历史已收录（今日重复，跳过，共 {len(skipped)} 个）",
            "",
            "| 仓库 | 语言 |",
            "|---|---|",
        ]
        for r in skipped[:20]:  # 最多显示 20 条
            lines.append(f"| [{r['full_name']}]({r['url']}) | {r['language'] or '-'} |")

    lines += [
        "",
        "---",
        "",
        f"> 来源：https://github.com/trending · 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run(
    since: str = "daily",
    max_repos: int = MAX_REPOS_PER_RUN,
    upload_feishu: bool = True,
    dry_run: bool = False,
) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 60)
    print(f"GitHub AI Trending  ·  {today}")
    print("=" * 60)

    # ── Step 1：抓取 Trending ──────────────────────────────────────
    print("\n[1/5] 抓取 GitHub Trending …")
    try:
        repos = fetch_trending(since=since)
    except Exception as e:
        print(f"✗ 抓取失败：{e}")
        return
    print(f"✓ 共找到 {len(repos)} 个热门仓库")

    # ── Step 2：AI 关键词过滤 ──────────────────────────────────────
    print("\n[2/5] AI 关键词过滤 …")
    ai_repos = [r for r in repos if is_ai_related(r)]
    print(f"✓ 过滤出 {len(ai_repos)} 个 AI 相关仓库")
    for r in ai_repos:
        print(f"  • {r['full_name']}  [{r['language']}]  {r['stars_today']}")
        print(f"    {r['description'][:80]}")

    # ── Step 3：跨日去重 ───────────────────────────────────────────
    print("\n[3/5] 对比历史缓存，去重 …")
    cache = load_cache()
    seen_set = set(cache.get("seen", []))
    new_ai_repos = [r for r in ai_repos if r["full_name"] not in seen_set]
    print(f"✓ 新仓库 {len(new_ai_repos)} 个（历史已收录 {len(ai_repos) - len(new_ai_repos)} 个，跳过）")

    if not new_ai_repos:
        print("\n今日无新增 AI 仓库，写入日报并退出。")
        digest = build_digest(today, ai_repos, new_ai_repos, [])
        if upload_feishu and not dry_run:
            upload_to_feishu(title=f"GitHub AI 趋势日报 · {today}", content=digest)
        return

    # 取前 max_repos 个处理
    to_process = new_ai_repos[:max_repos]
    if len(new_ai_repos) > max_repos:
        print(f"  （本次最多处理 {max_repos} 个，剩余 {len(new_ai_repos) - max_repos} 个留到下次）")

    if dry_run:
        print("\n[DRY RUN] 以下仓库将被处理：")
        for r in to_process:
            print(f"  → {r['full_name']}  {r['url']}")
        print("\n[DRY RUN] 跳过 AI 重构和飞书上传。")
        return

    # ── Step 4：逐仓库处理 ─────────────────────────────────────────
    print(f"\n[4/5] 处理 {len(to_process)} 个仓库 …")
    processed = []
    newly_seen = []

    for i, repo in enumerate(to_process, 1):
        print(f"\n  [{i}/{len(to_process)}] {repo['full_name']}")
        try:
            title, content = fetch_repo_content(repo["url"])
            article = ai_restructure(title=title, url=repo["url"], content=content)

            # 本地保存
            safe_name = re.sub(r'[\\/:*?"<>|]', "_", repo["full_name"].replace("/", "_"))
            date_prefix = today.replace("-", "")
            out_path = os.path.join(OUTPUT_DIR, f"{date_prefix}_{safe_name}_知识库.md")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(article)
            print(f"  ✓ 本地已保存：{out_path}")

            doc_url = ""
            if upload_feishu:
                doc_url = upload_to_feishu(
                    title=f"【AI趋势】{repo['full_name']}",
                    content=article,
                )

            processed.append({**repo, "doc_url": doc_url})
            newly_seen.append(repo["full_name"])

        except Exception as e:
            print(f"  ✗ 处理失败：{e}")
            # 失败的仓库也记入 seen，避免反复重试卡住（可根据需求改为不记入）
            newly_seen.append(repo["full_name"])

    # 未处理的新仓库也加入 seen（下次再处理）
    # 如果不想这样，将下行注释掉，让未处理的仓库下次继续排队
    for r in new_ai_repos[max_repos:]:
        newly_seen.append(r["full_name"])

    # ── Step 5：写日报 ─────────────────────────────────────────────
    print("\n[5/5] 生成今日 AI 趋势日报 …")
    digest = build_digest(today, ai_repos, new_ai_repos, processed)

    digest_path = os.path.join(OUTPUT_DIR, f"{today.replace('-', '')}_AI趋势日报.md")
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(digest)
    print(f"✓ 日报已保存：{digest_path}")

    if upload_feishu:
        upload_to_feishu(title=f"GitHub AI 趋势日报 · {today}", content=digest)

    # ── 更新缓存 ───────────────────────────────────────────────────
    seen_set.update(newly_seen)
    cache["seen"] = list(seen_set)
    cache.setdefault("history", []).append({
        "date": today,
        "all_ai_repos": [r["full_name"] for r in ai_repos],
        "new_repos": [r["full_name"] for r in new_ai_repos],
        "processed": [r["full_name"] for r in processed],
    })
    save_cache(cache)

    print(f"\n✅ 完成！本次处理 {len(processed)} 个仓库，日报已写入飞书。")


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run    = "--dry-run"   in sys.argv
    no_feishu  = "--no-feishu" in sys.argv

    run(
        since="daily",           # daily / weekly / monthly
        max_repos=5,             # 每次最多处理几个仓库
        upload_feishu=not no_feishu,
        dry_run=dry_run,
    )
