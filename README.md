# any2feishu

> 视频 / 网页 / GitHub → AI 一键生成知识库文章 → 飞书

把任意内容源转化为飞书知识库文章：
- **B 站 / YouTube 视频** — 下载音频 → Whisper 转文字 → AI 重构 → 飞书
- **网页文章** — 抓取正文+图片（CSDN / 知乎 / 掘金 / 微信公众号）→ 飞书
- **GitHub 仓库** — 抓取源码+文档 → AI 重构为教程 → 飞书

---

## 功能一览

| 脚本 | 输入 | 输出 |
|---|---|---|
| `bili_to_feishu.py` | B 站 / YouTube 视频链接 | 字幕 + AI 知识库文章 + 飞书文档 |
| `web_to_feishu.py` | 网页链接（CSDN/知乎/掘金/微信等）| 保留图片的飞书文档 |
| `github_to_feishu.py` | GitHub 仓库/目录链接 | AI 重构教程 + 飞书文档 |
| `bilibili_downloader.py` | B 站链接 | 视频 / 音频 / 字幕文件 |
| `audio_to_text.py` | 本地音频文件 | 文字稿 / SRT 字幕 |

---

## 环境要求

- Python 3.10+
- ffmpeg（音频提取，`bili_to_feishu.py` 需要）
- Node.js + lark-cli（飞书写入）

---

## 安装

**1. 克隆项目**
```bash
git clone https://github.com/jiawen-w/bili-to-feishu.git
cd bili-to-feishu
```

**2. 安装 Python 依赖**
```bash
pip install -r requirements.txt
playwright install chromium   # 微信公众号抓取需要
```

**3. 安装 ffmpeg**（视频/音频功能需要）
```bash
brew install ffmpeg   # macOS
```

**4. 安装飞书 CLI**
```bash
npm install -g @larksuite/cli
lark-cli config init --new   # 按提示在浏览器完成授权
lark-cli auth login --recommend
```

**5. 配置环境变量**
```bash
cp .env.example .env
```
编辑 `.env`，填入你的配置：
```ini
AI_BASE_URL=https://ark.cn-beijing.volces.com/api/coding
AI_API_KEY=你的 API Key
AI_MODEL=doubao-seed-2.0-pro
LARK_CLI=/usr/local/bin/lark-cli   # which lark-cli 查看路径
BILI_BROWSER=chrome                 # 或 safari
```

---

## 使用方法

### B 站 / YouTube 视频 → 飞书知识库

```bash
python bili_to_feishu.py "https://www.bilibili.com/video/BV1xxxxx"
python bili_to_feishu.py "https://www.youtube.com/watch?v=xxxxx"
```

**输出文件**（保存在 `~/Downloads/bili_to_feishu/`）：
- `视频标题.mp3` — 音频
- `视频标题.srt` — 字幕
- `视频标题.txt` — 原始文字
- `视频标题_知识库.md` — AI 重构后的教程
- 飞书文档（自动创建并返回链接）

> 跳过转录：编辑脚本底部将 `MODE = "A"` 改为 `MODE = "B"`，填入已有 `.txt` 路径直接跑 AI 步骤。

### 网页文章 → 飞书知识库

```bash
python web_to_feishu.py "https://blog.csdn.net/xxx/article/details/xxx"
python web_to_feishu.py "https://mp.weixin.qq.com/s/xxx"
python web_to_feishu.py "https://juejin.cn/post/xxx"
python web_to_feishu.py "https://zhuanlan.zhihu.com/p/xxx"
```

| 平台 | 图片处理 | 登录 |
|---|---|---|
| CSDN / 掘金 / 博客园 | 保留原始链接 | 无需 |
| 微信公众号 | 保留原始链接 + Playwright 渲染 | 无需 |
| 知乎 | 本地下载 | 自动读取 Chrome Cookie |

### GitHub 仓库 → 飞书知识库

```bash
python github_to_feishu.py "https://github.com/owner/repo"
python github_to_feishu.py "https://github.com/owner/repo/tree/main/path"
```

---

## AI 模型支持

默认使用**火山引擎 doubao-seed-2.0-pro**，兼容任意 Anthropic API 格式的服务（OpenRouter、Claude 官方等），修改 `.env` 中的 `AI_BASE_URL` / `AI_MODEL` 即可切换。

---

## License

MIT
