# 视频字幕提取与AI智能总结工具

支持 Bilibili 和 YouTube，自动提取字幕，使用 AI 生成结构化笔记，一键保存到 Obsidian 知识库。

## ✨ 功能特性

- 🎬 **双平台支持** — YouTube 和 Bilibili 视频字幕提取
- 🤖 **AI 智能总结** — 生成结构化 Markdown 笔记（概述 / 要点 / 详细笔记 / 金句 / 标签）
- ⚡ **一键全自动** — 提取 → 总结 → 保存，一气呵成
- 💾 **Obsidian 集成** — 自动保存为带 YAML frontmatter 的 Markdown 文件
- 📊 **多模型选择** — 支持 GPT-4o / GPT-4 / GPT-3.5 Turbo 等
- 📜 **超长字幕处理** — 自动分段总结后合并
- 📚 **历史记录** — 自动保存处理记录
- 🌙 **现代暗色 UI** — 玻璃拟态设计，流畅动画

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入配置：

```ini
OPENAI_API_KEY=sk-xxxx
OPENAI_BASE_URL=              # 可选，兼容 API 代理地址
OPENAI_MODEL=gpt-4o           # 默认模型
OBSIDIAN_VAULT_PATH=C:\Users\你的用户名\Documents\Obsidian\你的库名
```

### 3. 运行服务

```bash
python app.py
```

### 4. 打开浏览器

访问 `http://localhost:5000`

## 使用方式

### 手动三步流程
1. 粘贴视频链接
2. 点击「提取字幕」
3. 点击「AI 智能总结」
4. 点击「保存到 Obsidian」

### 一键全自动
1. 粘贴视频链接
2. 点击「⚡ 一键全自动」— 自动完成提取、总结、保存

## 输出格式

生成的 Markdown 文件包含：
- **YAML frontmatter**: title, source, platform, date, duration, uploader, tags
- **视频信息**: 原始链接、平台、生成时间
- **AI 总结**: 概述 → 核心要点 → 详细笔记 → 关键引用 → 建议标签
- **原始字幕** (可选): 可折叠的完整字幕文本

文件默认保存到 Obsidian 库的 `视频笔记/` 子目录，可通过以下方式自定义：
- **`.env` 配置**: 设置 `OBSIDIAN_SUBFOLDER=你的子目录名`（留空 = 保存到库根目录）
- **页面临时指定**: 在「📂 子目录」输入框中填写（留空 = 保存到库根目录）

### 文件名模板

在「📝 文件名」输入框中自定义，支持以下变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| `{date}` | 日期 | `20260530` |
| `{time}` | 时间 | `164900` |
| `{datetime}` | 日期时间 | `20260530_164900` |
| `{platform}` | 平台 | `youtube` / `bilibili` |
| `{title}` | 视频标题 | `xxx教程` |
| `{id}` | 视频 ID | `BVxxx` / `dQw4w...` |

默认模板: `{datetime}_{platform}_{title}`
示例: `{date}_{title}` → `20260530_一个视频标题.md`

## 注意事项

- 需要有效的 OpenAI API Key（或兼容 API，如 DeepSeek）
- Bilibili 部分视频可能需要登录 cookies（可配置 yt-dlp cookie 文件）
- 字幕过长时自动分段总结后合并
