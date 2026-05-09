# bili-to-feishu

> B 站视频 / GitHub 项目 → AI 一键生成知识库文章 → 飞书文档

输入一个 B 站链接，自动完成：下载音频 → Whisper 转文字 → AI 重构为知识库教程 → 写入飞书。  
也支持把 GitHub 仓库直接转成飞书知识库文章。

---

## 功能

| 脚本 | 功能 |
|---|---|
| `bili_to_feishu.py` | B站/YouTube视频 → 音频 → 文字 → AI重构 → 飞书（主流程）|
| `web_to_feishu.py` | 网页文章（CSDN/知乎/掘金/微信公众号等）→ 飞书 |
| `github_to_feishu.py` | GitHub仓库 → AI重构 → 飞书 |
| `bilibili_downloader.py` | 单独下载 B 站视频/音频/字幕 |
| `audio_to_text.py` | 单独将音频转为文字/字幕 |

---

## 环境要求

- Python 3.10+
- ffmpeg（音频提取）
- Node.js + lark-cli（飞书写入）

---

## 安装

**1. 克隆项目**
```bash
git clone https://github.com/你的用户名/bili-to-feishu.git
cd bili-to-feishu
```

**2. 安装 Python 依赖**
```bash
pip install -r requirements.txt
playwright install chromium   # 微信公众号抓取需要
```

**3. 安装 ffmpeg**
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
AI_API_KEY=你的API Key
AI_MODEL=doubao-seed-2.0-pro
LARK_CLI=/usr/local/bin/lark-cli   # which lark-cli 查看路径
BILI_BROWSER=chrome                 # 或 safari
```

---

## 使用

### B 站视频 → 飞书知识库

```bash
python bili_to_feishu.py
# 输入 B 站链接，回车即可
```

或直接传链接：
```bash
python bili_to_feishu.py "https://www.bilibili.com/video/BV1xxxxx"
```

**输出文件**（保存在 `~/Downloads/bili_to_feishu/`）：
- `视频标题.mp3` — 音频
- `视频标题.srt` — 字幕
- `视频标题.txt` — 原始文字
- `视频标题_知识库.md` — AI 重构后的教程
- 飞书文档（自动创建）

### 网页文章 → 飞书知识库

支持 CSDN、知乎、掘金、博客园、微信公众号等平台，自动处理图片：

```bash
python web_to_feishu.py "https://blog.csdn.net/xxx/article/details/xxx"
python web_to_feishu.py "https://mp.weixin.qq.com/s/xxx"
python web_to_feishu.py "https://juejin.cn/post/xxx"
```

- 公开平台（CSDN/掘金/微信）：图片保留原始链接，飞书直接渲染
- 需登录平台（知乎）：自动读取 Chrome Cookie
- 微信公众号：自动启动无头浏览器渲染，触发懒加载图片

### GitHub 仓库 → 飞书知识库

```bash
python github_to_feishu.py "https://github.com/owner/repo/tree/main/path"
```

---

## 跳过转录，直接用已有 txt 重新生成

编辑 `bili_to_feishu.py` 底部，将 `MODE = "A"` 改为 `MODE = "B"`，填入 txt 路径，再运行即可。

---

## AI 模型支持

默认使用火山引擎 doubao-seed-2.0-pro，兼容任意 Anthropic API 格式的服务，修改 `.env` 中的配置即可切换。

---

## License

MIT
