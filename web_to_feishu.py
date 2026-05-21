"""
网页正文（含图片）→ 飞书知识库
支持 CSDN、知乎、掘金、博客园、微信公众号等平台
图片自动处理，完整嵌入文档

依赖安装:
    pip install requests beautifulsoup4 markdownify browser-cookie3 playwright
    playwright install chromium
"""

from __future__ import annotations

import os
import re
import sys
import json
import subprocess
import mimetypes
import requests
from bs4 import BeautifulSoup
import markdownify
from urllib.parse import urljoin, urlparse


# ── 配置 ───────────────────────────────────────────────────────────────────────

LARK_CLI = "/Users/chenjiawen/.hermes/node/bin/lark-cli"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 需要浏览器 Cookie 才能访问的平台
COOKIE_REQUIRED = {"zhihu", "weibo"}

# 需要 Playwright 真实浏览器渲染的平台
PLAYWRIGHT_REQUIRED = {"weixin"}

# 图片公开可访问、无需下载、飞书可直接渲染的平台
PUBLIC_IMG_PLATFORMS = {"csdn", "juejin", "cnblogs", "segfault", "weixin", "default"}

# 各平台正文容器的 CSS 选择器
PLATFORM_SELECTORS = {
    "csdn":     "article#article-content, div#content_views",
    "zhihu":    "div.Post-RichText, div.RichContent-inner",
    "juejin":   "div.markdown-body",
    "cnblogs":  "div#cnblogs_post_body",
    "segfault": "div.article-content",
    "weixin":   "div#js_content",
    "default":  "article, main, div.post-content, div.article-body, div.entry-content",
}


# ── 工具函数 ───────────────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    if "csdn.net"        in url: return "csdn"
    if "zhihu.com"       in url: return "zhihu"
    if "juejin.cn"       in url: return "juejin"
    if "cnblogs.com"     in url: return "cnblogs"
    if "segmentfault"    in url: return "segfault"
    if "mp.weixin.qq.com" in url: return "weixin"
    return "default"


def ensure_package(pkg: str, import_name: str | None = None) -> None:
    import importlib
    try:
        importlib.import_module(import_name or pkg)
    except ImportError:
        print(f"正在安装 {pkg} …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])


def get_browser_cookies(domain: str) -> dict:
    try:
        import browser_cookie3
        jar = browser_cookie3.chrome(domain_name=domain)
        return {c.name: c.value for c in jar}
    except Exception as e:
        print(f"  ⚠ 读取 Chrome Cookie 失败：{e}")
        return {}


def fetch_html(url: str, platform: str = "default") -> str:
    cookies = {}
    if platform in COOKIE_REQUIRED:
        domain = re.search(r"([\w-]+\.[\w]+)(?:/|$)", url.split("//")[-1])
        if domain:
            print(f"  正在读取 Chrome Cookie（{domain.group(1)}）…")
            cookies = get_browser_cookies(domain.group(1))

    resp = requests.get(url, headers=HEADERS, cookies=cookies, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def fetch_html_playwright(url: str) -> str:
    """用 Playwright 无头浏览器渲染页面，自动滚动触发懒加载图片"""
    from playwright.sync_api import sync_playwright

    print("  启动浏览器渲染 …")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="zh-CN")
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        try:
            page.wait_for_selector("div#js_content, article, main", timeout=8000)
        except Exception:
            pass
        # 分段滚动，触发所有懒加载图片
        total_height = page.evaluate("document.body.scrollHeight")
        for pos in range(0, total_height, 600):
            page.evaluate(f"window.scrollTo(0, {pos})")
            page.wait_for_timeout(200)
        page.wait_for_timeout(1000)
        html = page.content()
        browser.close()
    print("  ✓ 页面渲染完成")
    return html


# ── 图片处理 ───────────────────────────────────────────────────────────────────

def normalize_img_src(src: str, page_url: str) -> str:
    """补全相对路径，返回完整 URL"""
    if not src or src.startswith("data:"):
        return ""
    return urljoin(page_url, src)


def download_image(img_url: str, save_dir: str, page_url: str, cookies: dict = {}) -> str | None:
    """下载图片到本地，返回本地路径；失败返回 None"""
    try:
        if not img_url.startswith("http"):
            return None
        os.makedirs(save_dir, exist_ok=True)
        headers = {**HEADERS, "Referer": page_url}
        resp = requests.get(img_url, headers=headers, cookies=cookies, timeout=10)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".jpg"
        ext = ext.replace(".jpe", ".jpg")
        filename = re.sub(r"[^\w]", "_", urlparse(img_url).path.split("/")[-1])[:40] or "img"
        if not filename.endswith(ext):
            filename += ext
        save_path = os.path.join(save_dir, filename)
        with open(save_path, "wb") as f:
            f.write(resp.content)
        return save_path
    except Exception:
        return None


def process_images(content_el, page_url: str, platform: str,
                   img_dir: str, cookies: dict = {}) -> dict[str, str]:
    """
    处理正文中的图片，返回 {原始src: 最终URL} 映射表。
    - 公开平台（CSDN/掘金等）：直接用原始 URL，飞书可渲染
    - 防盗链平台（知乎等）：下载到本地后保存
    """
    url_map = {}
    imgs = content_el.find_all("img")
    if not imgs:
        return url_map

    use_original = platform in PUBLIC_IMG_PLATFORMS
    print(f"  发现 {len(imgs)} 张图片（{'保留原始链接' if use_original else '下载到本地'}）")

    for img in imgs:
        # 微信懒加载：优先取 data-src，再取 src
        src = img.get("data-src") or img.get("src") or img.get("data-original", "")
        full_src = normalize_img_src(src, page_url)
        if not full_src:
            continue

        if use_original:
            # 公开图片直接用原始 URL，飞书能直接渲染
            url_map[src] = full_src
        else:
            # 防盗链图片：下载到本地
            local_path = download_image(full_src, img_dir, page_url, cookies)
            url_map[src] = local_path if local_path else full_src

    return url_map


# ── 正文提取 ───────────────────────────────────────────────────────────────────

def extract_content(html: str, platform: str, page_url: str,
                    img_dir: str, cookies: dict = {}) -> tuple[str, str]:
    """返回 (标题, markdown正文)，图片已替换为飞书 URL"""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    for tag in ["h1", "title"]:
        el = soup.find(tag)
        if el:
            title = el.get_text(strip=True)
            break

    for sel in ["script", "style", "nav", "footer", "header",
                ".ad", ".advertisement", "#comment", ".comment",
                ".related", ".recommend", ".toc"]:
        for el in soup.select(sel):
            el.decompose()

    selector = PLATFORM_SELECTORS.get(platform, PLATFORM_SELECTORS["default"])
    content_el = None
    for sel in selector.split(","):
        content_el = soup.select_one(sel.strip())
        if content_el:
            break

    if not content_el:
        divs = soup.find_all("div")
        content_el = max(divs, key=lambda d: len(d.get_text()), default=soup.body)

    # 处理图片：公开平台保留原始链接，防盗链平台下载本地
    url_map = process_images(content_el, page_url, platform, img_dir, cookies)
    for img in content_el.find_all("img"):
        src = img.get("data-src") or img.get("src") or img.get("data-original", "")
        new_src = url_map.get(src)
        if new_src:
            img.attrs = {"src": new_src, "alt": img.get("alt", "")}

    md = markdownify.markdownify(
        str(content_el),
        heading_style="ATX",
        bullets="-",
        code_language_callback=lambda el: el.get("class", [""])[0].replace("language-", "") if el.get("class") else "",
    )
    md = re.sub(r"\n{3,}", "\n\n", md).strip()

    return title, md


# ── 飞书写入 ───────────────────────────────────────────────────────────────────

import json

def upload_to_feishu(title: str, content: str) -> None:
    """使用 v2 API 创建飞书文档，直接返回链接"""
    result = subprocess.run(
        [LARK_CLI, "docs", "+create",
         "--api-version", "v2",
         "--title", title,
         "--content", content,
         "--doc-format", "markdown"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"\n✗ 飞书写入失败：{result.stderr.strip()}")
        return
    try:
        data = json.loads(result.stdout)
        doc_url = data.get("data", {}).get("document", {}).get("url", "")
        if doc_url:
            print(f"\n✓ 飞书文档已创建！")
            print(f"  🔗 {doc_url}")
        else:
            print(f"\n✓ 飞书文档已创建！\n{result.stdout.strip()}")
    except Exception:
        print(f"\n✓ 飞书文档已创建！\n{result.stdout.strip()}")


# ── 主流程 ─────────────────────────────────────────────────────────────────────

def run(url: str, upload_feishu: bool = True, save_md: bool = True,
        output_dir: str = "~/Downloads/web_to_feishu") -> None:

    ensure_package("requests")
    ensure_package("beautifulsoup4", "bs4")
    ensure_package("markdownify")
    ensure_package("browser-cookie3", "browser_cookie3")

    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print(f"[1/3] 抓取网页 …\n  {url}")
    platform = detect_platform(url)
    print(f"  平台识别：{platform}")

    if platform in PLAYWRIGHT_REQUIRED:
        ensure_package("playwright")
        html = fetch_html_playwright(url)
        cookies = {}
    else:
        cookies = {}
        if platform in COOKIE_REQUIRED:
            domain = re.search(r"([\w-]+\.[\w]+)(?:/|$)", url.split("//")[-1])
            if domain:
                print(f"  正在读取 Chrome Cookie（{domain.group(1)}）…")
                cookies = get_browser_cookies(domain.group(1))
        resp = requests.get(url, headers=HEADERS, cookies=cookies, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        html = resp.text

    print("[2/3] 提取正文 + 处理图片 …")
    safe_prefix = re.sub(r"[^\w]", "_", urlparse(url).path.split("/")[-1])[:30] or "article"
    img_dir = os.path.join(output_dir, f"{safe_prefix}_images")

    title, md_content = extract_content(html, platform, url, img_dir, cookies)
    print(f"  标题：{title}")
    print(f"  正文：{len(md_content)} 字符")

    if save_md:
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:80]
        md_path = os.path.join(output_dir, f"{safe_title}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{md_content}\n\n---\n\n> 来源：{url}")
        print(f"  已保存：{md_path}")

    if upload_feishu:
        print("[3/3] 写入飞书 …")
        upload_to_feishu(title=title, content=f"# {title}\n\n{md_content}\n\n---\n\n> 来源：{url}")

    print("\n✅ 完成！")


# ── 多链接批量流程 ─────────────────────────────────────────────────────────────

def run_batch(
    urls:          list[str],
    upload_feishu: bool = True,
    save_md:       bool = True,
    output_dir:    str  = "~/Downloads/web_to_feishu",
) -> None:
    """按顺序处理多个链接，最后汇总输出所有飞书链接"""

    # 记录每条链接的处理结果
    results = []   # {"url", "title", "doc_url", "ok", "error"}

    for i, url in enumerate(urls, 1):
        print(f"\n{'═'*55}")
        print(f"  [{i}/{len(urls)}]  {url}")
        print(f"{'═'*55}")
        try:
            doc_url = _run_single(
                url          = url,
                upload_feishu= upload_feishu,
                save_md      = save_md,
                output_dir   = output_dir,
            )
            results.append({"url": url, "doc_url": doc_url, "ok": True, "title": "", "error": ""})
        except Exception as e:
            import traceback
            print(f"✗ 处理失败：{e}")
            traceback.print_exc()
            results.append({"url": url, "doc_url": "", "ok": False, "title": "", "error": str(e)})

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    ok_list   = [r for r in results if r["ok"]]
    fail_list = [r for r in results if not r["ok"]]

    print(f"\n{'═'*55}")
    print(f"  全部完成！成功 {len(ok_list)}/{len(results)}  失败 {len(fail_list)}/{len(results)}")
    print(f"{'═'*55}")

    if ok_list:
        print("\n✅ 已写入飞书：")
        for r in ok_list:
            link = r["doc_url"] or "（无链接）"
            short_url = r["url"][:60] + "…" if len(r["url"]) > 60 else r["url"]
            print(f"  • {short_url}")
            print(f"    🔗 {link}")

    if fail_list:
        print("\n❌ 失败：")
        for r in fail_list:
            print(f"  • {r['url']}")
            print(f"    原因：{r['error']}")


def _run_single(
    url:           str,
    upload_feishu: bool = True,
    save_md:       bool = True,
    output_dir:    str  = "~/Downloads/web_to_feishu",
) -> str:
    """
    处理单条链接，返回飞书文档 URL。
    原 run() 逻辑完全不变，只是把 doc_url 作为返回值透传出来。
    """
    ensure_package("requests")
    ensure_package("beautifulsoup4", "bs4")
    ensure_package("markdownify")
    ensure_package("browser-cookie3", "browser_cookie3")

    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print(f"[1/3] 抓取网页 …\n  {url}")
    platform = detect_platform(url)
    print(f"  平台识别：{platform}")

    if platform in PLAYWRIGHT_REQUIRED:
        ensure_package("playwright")
        html    = fetch_html_playwright(url)
        cookies = {}
    else:
        cookies = {}
        if platform in COOKIE_REQUIRED:
            domain = re.search(r"([\w-]+\.[\w]+)(?:/|$)", url.split("//")[-1])
            if domain:
                print(f"  正在读取 Chrome Cookie（{domain.group(1)}）…")
                cookies = get_browser_cookies(domain.group(1))
        resp = requests.get(url, headers=HEADERS, cookies=cookies, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        html = resp.text

    print("[2/3] 提取正文 + 处理图片 …")
    safe_prefix = re.sub(r"[^\w]", "_", urlparse(url).path.split("/")[-1])[:30] or "article"
    img_dir     = os.path.join(output_dir, f"{safe_prefix}_images")

    title, md_content = extract_content(html, platform, url, img_dir, cookies)
    print(f"  标题：{title}")
    print(f"  正文：{len(md_content)} 字符")

    if save_md:
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:80]
        md_path = os.path.join(output_dir, f"{safe_title}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{md_content}\n\n---\n\n> 来源：{url}")
        print(f"  已保存：{md_path}")

    doc_url = ""
    if upload_feishu:
        print("[3/3] 写入飞书 …")
        doc_url = _upload_and_return(
            title   = title,
            content = f"# {title}\n\n{md_content}\n\n---\n\n> 来源：{url}",
        )

    print("✅ 完成！")
    return doc_url


def _upload_and_return(title: str, content: str) -> str:
    """调用 upload_to_feishu 并返回文档 URL"""
    result = subprocess.run(
        [LARK_CLI, "docs", "+create",
         "--api-version", "v2",
         "--title", title,
         "--content", content,
         "--doc-format", "markdown"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"\n✗ 飞书写入失败：{result.stderr.strip()}")
        return ""
    try:
        data    = json.loads(result.stdout)
        doc_url = data.get("data", {}).get("document", {}).get("url", "")
        if doc_url:
            print(f"  ✓ 飞书文档已创建：{doc_url}")
        return doc_url
    except Exception:
        print(f"  ✓ 飞书文档已创建\n{result.stdout.strip()}")
        return ""


def _collect_urls() -> list[str]:
    """收集多个链接：命令行参数 或 交互式逐行输入（空行结束）"""
    # 命令行传入
    cli_urls = [a for a in sys.argv[1:] if not a.startswith("--")]
    if cli_urls:
        print(f"共 {len(cli_urls)} 个链接（来自命令行参数）")
        return cli_urls

    # 交互式输入
    print("请输入网页链接，每行一个，输入空行开始处理：")
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
        # 支持直接粘贴多个链接（空格/换行分隔）
        for part in line.split():
            if part.startswith("http"):
                urls.append(part)
    return urls


# ── 入口 ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    no_feishu = "--no-feishu" in sys.argv
    no_save   = "--no-save"   in sys.argv

    urls = _collect_urls()
    if not urls:
        print("未输入任何链接，退出。")
        sys.exit(0)

    print(f"\n共 {len(urls)} 个链接，开始按顺序处理 …\n")

    run_batch(
        urls          = urls,
        upload_feishu = not no_feishu,
        save_md       = not no_save,
        output_dir    = "~/Downloads/web_to_feishu",
    )
