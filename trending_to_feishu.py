"""
GitHub Trending → AI 过滤 → 飞书知识库子文档
每日自动爬取 GitHub Trending，筛选 AI 相关仓库，
跨日去重后以子文档形式写入指定飞书知识库页面。

功能：
1. 抓取 https://github.com/trending 当日热门仓库
2. 两级关键词过滤 AI 相关仓库（强词直接命中，弱词需 ≥2 个）
3. 对比本地缓存，跳过已处理仓库（跨日去重）
4. 每日最多处理 5 个新仓库：
   - 抓取 GitHub 源码/文档 → AI 重构知识库文章 → 作为子文档写入飞书知识库
5. 另生成今日趋势日报，同样写为知识库子文档
6. 更新本地 JSON 缓存

依赖：
    pip install requests beautifulsoup4 anthropic

用法：
    python trending_to_feishu.py                # 正常运行
    python trending_to_feishu.py --dry-run      # 预览，不调用 AI 和飞书
    python trending_to_feishu.py --no-feishu    # 只生成本地文件，不上传飞书
    python trending_to_feishu.py --weekly       # 改为抓取本周热门
"""

from __future__ import annotations

import os
import re
import sys
import json
import subprocess
import requests
from datetime import datetime
from bs4 import BeautifulSoup


# ── 配置 ──────────────────────────────────────────────────────────────────────

AI_BASE_URL  = "https://ark.cn-beijing.volces.com/api/coding"
AI_API_KEY   = "1d3ace95-c577-4eee-ae9d-4fc85f3d07ee"
AI_MODEL     = "doubao-seed-2.0-pro"
LARK_CLI     = "/Users/chenjiawen/.hermes/node/bin/lark-cli"

# 目标飞书知识库节点（所有子文档将挂在此节点下）
# https://icanx2007.feishu.cn/wiki/YUXew6c5ci2FQbkrQzmc13axnTc
WIKI_NODE_TOKEN = "YUXew6c5ci2FQbkrQzmc13axnTc"

# 输出目录 & 缓存文件
OUTPUT_DIR   = os.path.expanduser("~/Downloads/trending_to_feishu")
CACHE_FILE   = os.path.join(OUTPUT_DIR, "seen_repos.json")

# 每次最多处理几个仓库（每个仓库对应一个知识库子文档）
MAX_REPOS_PER_RUN = 5

# 抓取这些扩展名的文件
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

# 弱关键词：命中 ≥2 个才算 AI 相关
AI_KEYWORDS_WEAK = {
    "ai", "ml", "neural", "model", "inference", "training", "dataset",
    "agent", "chatbot", "nlp", "computer-vision", "deep-learning",
    "machine-learning", "reinforcement", "reward", "alignment", "rlhf",
    "foundation", "generative", "synthetic", "prompt", "token", "context",
    "cuda", "gpu", "accelerate", "机器学习", "深度学习", "神经网络",
    "训练", "推理", "微调", "向量", "模型",
}


def is_ai_related(repo: dict) -> bool:
    text = f"{repo['full_name']} {repo['description']}".lower()
    if any(kw in text for kw in AI_KEYWORDS_STRONG):
        return True
    return sum(1 for kw in AI_KEYWORDS_WEAK if kw in text) >= 2


# ── 缓存（跨日去重）──────────────────────────────────────────────────────────

def load_cache() -> dict:
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

def fetch_trending(since: str = "daily") -> list[dict]:
    url = f"https://github.com/trending?since={since}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    print(f"  抓取：{url}")
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    repos = []

    for article in soup.select("article.Box-row"):
        h2 = article.select_one("h2 a")
        if not h2:
            continue
        full_name = h2.get("href", "").strip("/")
        if not full_name or "/" not in full_name:
            continue

        desc_el   = article.select_one("p")
        lang_el   = article.select_one("[itemprop='programmingLanguage']")

        stars_today = ""
        for span in article.select("span.d-inline-block"):
            txt = span.get_text(strip=True)
            if "stars today" in txt or "star today" in txt:
                stars_today = txt
                break

        total_stars = ""
        for a in article.select("a.Link--muted"):
            if a.get("href", "").endswith("/stargazers"):
                total_stars = a.get_text(strip=True)
                break

        repos.append({
            "full_name":   full_name,
            "url":         f"https://github.com/{full_name}",
            "description": desc_el.get_text(strip=True) if desc_el else "",
            "language":    lang_el.get_text(strip=True) if lang_el else "",
            "stars_today": stars_today,
            "total_stars": total_stars,
        })

    return repos


# ── GitHub 内容抓取（来自 github_to_feishu.py）───────────────────────────────

def fetch_dir(owner: str, repo: str, path: str) -> list[dict]:
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        resp = requests.get(api_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠ GitHub API 失败：{e}")
        return []

    items = resp.json()
    if not isinstance(items, list):
        return []

    files = []
    for item in items:
        name = item.get("name", "")
        if item["type"] == "dir":
            if name not in SKIP_DIRS:
                files.extend(fetch_dir(owner, repo, item["path"]))
        elif item["type"] == "file":
            if os.path.splitext(name)[1].lower() in INCLUDE_EXTS:
                files.append(item)
    return files


def fetch_repo_content(github_url: str) -> tuple[str, str]:
    url   = github_url.rstrip("/")
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)(?:/tree/[^/]+/(.*))?", url)
    if not match:
        raise ValueError(f"无法解析：{url}")
    owner, repo, path = match.group(1), match.group(2), match.group(3) or ""
    title = f"{repo}/{path}" if path else repo

    print(f"  仓库：{owner}/{repo}")
    files = fetch_dir(owner, repo, path)
    print(f"  文件数：{len(files)}")

    parts       = [f"# 项目：{title}\n来源：{github_url}\n"]
    total_chars = 0

    for f in files:
        if total_chars >= MAX_CONTENT_CHARS:
            parts.append("\n\n---\n**（内容过长，已截断）**")
            break
        print(f"  读取：{f['path']}")
        try:
            r    = requests.get(f["download_url"], timeout=15)
            r.raise_for_status()
            ext  = os.path.splitext(f["name"])[1]
            chunk = f"\n\n---\n## 文件：{f['path']}\n\n```{ext.lstrip('.')}\n{r.text}\n```"
            parts.append(chunk)
            total_chars += len(chunk)
        except Exception as e:
            parts.append(f"\n\n---\n## 文件：{f['path']}\n\n（读取失败：{e}）")

    return title, "\n".join(parts)


# ── AI 重构 ───────────────────────────────────────────────────────────────────

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
    import anthropic
    print("  AI 重构中 …")
    client  = anthropic.Anthropic(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    message = client.messages.create(
        model=AI_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": KNOWLEDGE_PROMPT.format(
            title=title, url=url, content=content
        )}],
    )
    print("  ✓ AI 重构完成")
    return message.content[0].text


# ── 写入飞书知识库子文档（两步法）────────────────────────────────────────────

def upload_to_wiki(title: str, content: str) -> str:
    """
    在 WIKI_NODE_TOKEN 下创建一个子文档，写入 Markdown 内容。
    返回飞书 wiki 页面 URL；失败返回空字符串。

    两步：
    1. wiki +node-create → 创建 wiki 子节点，拿到 obj_token（即文档 ID）
    2. docs +update --command overwrite → 将内容写入该文档
    """
    # ── Step 1: 创建 wiki 子节点 ──────────────────────────────────
    r1 = subprocess.run(
        [
            LARK_CLI, "wiki", "+node-create",
            "--parent-node-token", WIKI_NODE_TOKEN,
            "--title", title,
        ],
        capture_output=True, text=True,
    )

    # lark-cli 在 stdout 混入了进度文字，只取 JSON 部分
    json_text = ""
    for line in r1.stdout.splitlines():
        if line.strip().startswith("{"):
            json_text = "\n".join(
                l for l in r1.stdout.splitlines()
                if l.strip().startswith("{") or json_text
            )
            break

    # 更健壮：直接从整段输出里提取 JSON 块
    try:
        json_match = re.search(r"\{[\s\S]+\}", r1.stdout)
        if not json_match:
            raise ValueError("未找到 JSON")
        node_data = json.loads(json_match.group())
        obj_token  = node_data["data"]["obj_token"]
        wiki_url   = node_data["data"]["url"]
    except Exception as e:
        print(f"  ✗ wiki 节点创建失败：{e}\n  原始输出：{r1.stdout[:300]}")
        if r1.stderr:
            print(f"  stderr：{r1.stderr[:200]}")
        return ""

    print(f"  ✓ wiki 子节点已创建：{wiki_url}")

    # ── Step 2: 写入内容 ───────────────────────────────────────────
    r2 = subprocess.run(
        [
            LARK_CLI, "docs", "+update",
            "--api-version", "v2",
            "--doc", obj_token,
            "--content", content,
            "--doc-format", "markdown",
            "--command", "overwrite",
        ],
        capture_output=True, text=True,
    )

    if r2.returncode != 0:
        print(f"  ✗ 内容写入失败：{r2.stderr.strip()}")
        return wiki_url  # 节点已建，返回 URL，内容可手动补填

    try:
        r2_data = json.loads(r2.stdout)
        if r2_data.get("ok"):
            print(f"  ✓ 内容写入成功")
        else:
            print(f"  ⚠ 内容写入返回异常：{r2.stdout[:200]}")
    except Exception:
        pass

    return wiki_url


# ── 今日日报 ──────────────────────────────────────────────────────────────────

def build_digest(
    date_str:    str,
    all_ai:      list[dict],
    new_repos:   list[dict],
    processed:   list[dict],
) -> str:
    lines = [
        f"# GitHub AI 趋势日报 · {date_str}",
        "",
        f"> 今日 Trending 发现 **{len(all_ai)}** 个 AI 仓库，"
        f"新上榜 **{len(new_repos)}** 个，"
        f"已生成知识库文章 **{len(processed)}** 篇。",
        "",
        "---",
        "",
        "## 今日新上榜 AI 仓库",
        "",
        "| 仓库 | 简介 | 语言 | 今日 ⭐ |",
        "|---|---|---|---|",
    ]
    for r in new_repos:
        desc  = (r["description"][:55] + "…") if len(r["description"]) > 55 else r["description"]
        stars = r["stars_today"] or "-"
        lang  = r["language"] or "-"
        lines.append(f"| [{r['full_name']}]({r['url']}) | {desc} | {lang} | {stars} |")

    if processed:
        lines += ["", "---", "", "## 已生成知识库文章", ""]
        for r in processed:
            wiki_url = r.get("wiki_url", "")
            link = f"[📖 飞书知识库]({wiki_url})" if wiki_url else "（本地已保存）"
            lines.append(
                f"- **[{r['full_name']}]({r['url']})**"
                f"  `{r['language'] or '-'}`  {r['stars_today'] or ''}\n"
                f"  {r['description'][:80]}\n"
                f"  {link}\n"
            )

    skipped = [r for r in all_ai if r["full_name"] not in {x["full_name"] for x in new_repos}]
    if skipped:
        lines += [
            "", "---", "",
            f"## 历史已收录（跳过，共 {len(skipped)} 个）", "",
            "| 仓库 | 语言 |", "|---|---|",
        ]
        for r in skipped[:15]:
            lines.append(f"| [{r['full_name']}]({r['url']}) | {r['language'] or '-'} |")

    lines += [
        "", "---", "",
        f"> 来源：https://github.com/trending · "
        f"生成：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run(
    since:        str  = "daily",
    max_repos:    int  = MAX_REPOS_PER_RUN,
    upload_feishu: bool = True,
    dry_run:      bool = False,
) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 60)
    print(f"GitHub AI Trending → 飞书知识库  ·  {today}")
    print(f"目标节点：https://icanx2007.feishu.cn/wiki/{WIKI_NODE_TOKEN}")
    print("=" * 60)

    # ── Step 1：抓取 Trending ──────────────────────────────────────
    print(f"\n[1/5] 抓取 GitHub Trending（{since}）…")
    try:
        repos = fetch_trending(since=since)
    except Exception as e:
        print(f"✗ 抓取失败：{e}")
        return
    print(f"✓ 共 {len(repos)} 个热门仓库")

    # ── Step 2：AI 关键词过滤 ──────────────────────────────────────
    print("\n[2/5] AI 关键词过滤 …")
    ai_repos = [r for r in repos if is_ai_related(r)]
    print(f"✓ AI 相关：{len(ai_repos)} 个")
    for r in ai_repos:
        flag = "⭐" if r["stars_today"] else " "
        print(f"  {flag} {r['full_name']}  [{r['language']}]  {r['stars_today']}")
        if r["description"]:
            print(f"    {r['description'][:80]}")

    # ── Step 3：跨日去重 ───────────────────────────────────────────
    print("\n[3/5] 去重（对比历史缓存）…")
    cache    = load_cache()
    seen_set = set(cache.get("seen", []))
    new_ai   = [r for r in ai_repos if r["full_name"] not in seen_set]
    skipped  = len(ai_repos) - len(new_ai)
    print(f"✓ 新仓库 {len(new_ai)} 个（跳过历史已收录 {skipped} 个）")

    if not new_ai:
        print("\n今日无新 AI 仓库，仅写日报。")
        digest = build_digest(today, ai_repos, [], [])
        _save_local(digest, f"{today.replace('-','')}_AI趋势日报.md")
        if upload_feishu and not dry_run:
            upload_to_wiki(f"GitHub AI 趋势日报 · {today}", digest)
        return

    to_process = new_ai[:max_repos]
    if len(new_ai) > max_repos:
        print(f"  （每日上限 {max_repos} 个，剩余 {len(new_ai)-max_repos} 个留到明天）")

    if dry_run:
        print(f"\n[DRY RUN] 今日将处理 {len(to_process)} 个仓库：")
        for r in to_process:
            print(f"  → {r['full_name']}")
        print("[DRY RUN] 跳过 AI 重构和飞书上传。")
        return

    # ── Step 4：逐仓库处理 → 写知识库子文档 ──────────────────────
    print(f"\n[4/5] 处理 {len(to_process)} 个仓库，每个写为知识库子文档 …")
    processed  = []
    newly_seen = []

    for i, repo in enumerate(to_process, 1):
        print(f"\n  ── [{i}/{len(to_process)}] {repo['full_name']} ──")
        try:
            title, content = fetch_repo_content(repo["url"])
            article = ai_restructure(title=title, url=repo["url"], content=content)

            # 本地备份
            safe = re.sub(r'[\\/:*?"<>|]', "_", repo["full_name"].replace("/", "_"))
            local_path = _save_local(article, f"{today.replace('-','')}_{safe}_知识库.md")

            # 写飞书知识库子文档
            wiki_url = ""
            if upload_feishu:
                wiki_url = upload_to_wiki(
                    title   = f"【AI趋势·{today}】{repo['full_name']}",
                    content = article,
                )

            processed.append({**repo, "wiki_url": wiki_url})
            newly_seen.append(repo["full_name"])

        except Exception as e:
            import traceback
            print(f"  ✗ 处理失败：{e}")
            traceback.print_exc()
            newly_seen.append(repo["full_name"])  # 失败也记入，避免卡同一仓库

    # 本次未处理的新仓库：留到明天（不写入 seen）
    # 若想今天全部标记掉，取消下面注释：
    # for r in new_ai[max_repos:]: newly_seen.append(r["full_name"])

    # ── Step 5：写日报子文档 ───────────────────────────────────────
    print("\n[5/5] 生成今日 AI 趋势日报 …")
    digest = build_digest(today, ai_repos, new_ai, processed)
    _save_local(digest, f"{today.replace('-','')}_AI趋势日报.md")

    if upload_feishu:
        upload_to_wiki(
            title   = f"GitHub AI 趋势日报 · {today}",
            content = digest,
        )

    # ── 更新缓存 ───────────────────────────────────────────────────
    seen_set.update(newly_seen)
    cache["seen"] = list(seen_set)
    cache.setdefault("history", []).append({
        "date":      today,
        "ai_repos":  [r["full_name"] for r in ai_repos],
        "new":       [r["full_name"] for r in new_ai],
        "processed": [r["full_name"] for r in processed],
    })
    save_cache(cache)

    print(f"\n✅ 完成！生成 {len(processed)} 篇知识库子文档 + 1 份日报。")
    print(f"   飞书知识库：https://icanx2007.feishu.cn/wiki/{WIKI_NODE_TOKEN}")


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _save_local(content: str, filename: str) -> str:
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ 本地保存：{path}")
    return path


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run   = "--dry-run"   in sys.argv
    no_feishu = "--no-feishu" in sys.argv
    weekly    = "--weekly"    in sys.argv

    run(
        since         = "weekly" if weekly else "daily",
        max_repos     = MAX_REPOS_PER_RUN,
        upload_feishu = not no_feishu,
        dry_run       = dry_run,
    )
