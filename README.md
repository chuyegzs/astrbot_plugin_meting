<!-- omit in toc -->
# 🍂 初叶 MetingAPI 点歌插件

<p align="center">
  <img src="https://img.shields.io/badge/version-v1.1.1-blue?style=flat-square" alt="Version">
  <img src="https://img.shields.io/badge/AstrBot-≥4.17.6-green?style=flat-square" alt="AstrBot">
  <img src="https://img.shields.io/badge/license-AGPL-orange?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/python-3.10+-steelblue?style=flat-square" alt="Python">
</p>

<p align="center">
  <b>基于 MetingAPI 的多音源点歌插件</b><br>
  支持 QQ 音乐 · 网易云 &nbsp;|&nbsp; 智能语音分段 &nbsp;|&nbsp; 精美音乐卡片
</p>

---

> [!WARNING]
> **兼容性声明**
> - 使用音乐卡片功能，AstrBot 版本须 ≥ `4.17.6`
> - 遇到问题时请开启 **DEBUG 日志**，查看兼容性检查结果
> - 提交 Issue 时请附带：插件日志、兼容性检查结果、AstrBot & 插件版本、消息平台框架

<!-- omit in toc -->
## 📑 目录

- [✨ 功能特性](#-功能特性)
- [📦 安装](#-安装)
- [🎮 使用方法](#-使用方法)
  - [📋 指令速查表](#-指令速查表)
  - [🔍 搜索歌曲](#-搜索歌曲)
  - [▶️ 播放歌曲](#️-播放歌曲)
  - [🔄 切换音源](#-切换音源)
  - [🎨 音乐卡片](#-音乐卡片)
  - [🤖 LLM 调用](#-llm-调用)
- [⚙️ 配置说明](#️-配置说明)
  - [API 配置](#api-配置)
  - [搜索配置](#搜索配置)
  - [音乐发送配置](#音乐发送配置)
  - [下载与缓存配置](#下载与缓存配置)
  - [语音分段配置](#语音分段配置)
- [🔧 技术说明](#-技术说明)
- [❓ 常见问题](#-常见问题)
- [📂 项目结构](#-项目结构)
- [🙏 致谢](#-致谢)
- [📜 许可证](#-许可证)

---

## ✨ 功能特性

| 特性 | 说明 |
|------|------|
| 🎵 **多音源支持** | QQ 音乐、网易云、酷狗、酷我（取决于 API） |
| 💬 **会话级音源** | 每个会话独立切换音源，互不影响 |
| ✂️ **智能分段** | 超长歌曲自动分割发送，支持 30~300s 可配置分段 |
| 🎨 **音乐卡片** | 精美卡片消息，含封面、歌手、跳转链接 |
| ⚡ **快捷点歌** | 一键指定音源直接点歌，无需切换 |
| 🤖 **LLM 友好** | 提供 Function Tool，AI 可直接调用搜歌/点歌 |
| 🗑️ **自动撤回** | 搜索结果定时撤回，保持聊天整洁 |
| 🔒 **结果隔离** | 可选搜歌结果仅对搜索者本人有效 |
| 📁 **文件直传** | 支持以文件方式发送音乐，绕过语音限制 |
| 💾 **缓存机制** | 可选缓存已下载音乐，减少带宽消耗 |

---

## 📦 安装

<details open>
<summary><b>方式一：WebUI 安装（推荐）</b></summary>

1. 打开 AstrBot WebUI → **插件市场**
2. 搜索 `meting`
3. 找到本插件并安装
4. 启用插件并配置

</details>

<details>
<summary><b>方式二：WebUI 手动安装</b></summary>

1. 打开 AstrBot WebUI → **插件管理**
2. 点击 **从链接安装**
3. 输入：`https://github.com/chuyegzs/astrbot_plugin_meting/`
4. 启用插件并配置

</details>

<details>
<summary><b>方式三：手动安装</b></summary>

```bash
# 进入插件目录
cd AstrBot/data/plugins

# 克隆仓库
git clone https://github.com/chuyegzs/astrbot_plugin_meting.git

# 安装依赖
cd astrbot_plugin_meting
pip install -r requirements.txt
```

然后在 WebUI 插件管理页面启用即可。

</details>

> 💡 插件依赖 `aiohttp`、`pydub`、`imageio-ffmpeg`、`packaging`，WebUI 安装时会自动处理。

---

## 🎮 使用方法

### 📋 指令速查表

| 指令 | 别名 | 说明 |
|------|------|------|
| `点歌指令` | `点歌帮助` `song help` | 查看所有指令 |
| `搜歌 <歌名>` | `search` | 在当前音源搜索歌曲 |
| `点歌 <序号>` | `play` | 播放搜索结果中的歌曲 |
| `点歌 <歌名>` | `play` | 直接搜索并播放第一首 |
| `网易点歌 <歌名>` | `netease play` | 网易云快捷点歌 |
| `QQ点歌 <歌名>` | `tencent play` `qq play` | QQ 音乐快捷点歌 |
| `酷狗点歌 <歌名>` | `kugou play` | 酷狗快捷点歌 |
| `酷我点歌 <歌名>` | `kuwo play` | 酷我快捷点歌 |
| `切换网易云` | `switch netease` | 切换会话默认音源为网易云 |
| `切换QQ音乐` | `switch tencent` | 切换会话默认音源为 QQ 音乐 |
| `切换酷狗` | `switch kugou` | 切换会话默认音源为酷狗 |
| `切换酷我` | `switch kuwo` | 切换会话默认音源为酷我 |
| `切换发送模式 <mode>` | `switch meting mode` | 切换会话的发送模式 |

### 🔍 搜索歌曲

使用当前会话设置的音源搜索：

```
搜歌 坠落星空
```

搜索结果示例：

```
🎵 搜索结果 (网易云音乐)
━━━━━━━━━━━━━━
[1] 坠落星空  👤 小星星Aurora  💿 画一个星星一个你
[2] 坠落星空  👤 peony  💿 小星星Aurora 刀小刀 李响/钱润玉- 坠落星空
[3] 坠落星空(合唱版)  👤 跪四人  💿 坠落星空（合唱版）
[4] 溯x大雾x坠落星空  👤 观药  💿 溯x大雾x坠落星空
[5] 坠落星空  👤 李响/钱润玉  💿 坠落星空
[6] 坠落星空 (Live)  👤 小星星Aurora  💿 万魔原创音乐节
[7] 坠落星空(钢琴版)  👤 Ben George  💿 流行钢琴
[8] 坠落星空  👤 栗莓Giovanna  💿 四暖未歇
[9] 坠落星空(合声版)  👤 也许  💿 坠落星空
[10] 坠落星空（男声版）  👤 六月狐狸  💿 六月繁星，七月流火
━━━━━━━━━━━━━━
💡 提示：发送 "点歌 1" 即可播放第一首歌
```

### ▶️ 播放歌曲

**通过序号播放**（需先搜歌）：

```
点歌 1
```

**直接点歌**（自动搜第一首）：

```
点歌 坠落星空
网易点歌 坠落星空
QQ点歌 坠落星空
```

### 🔄 切换音源

音源设置**按会话隔离**，切换后不影响其他群聊/私聊：

```
切换网易云
切换QQ音乐
切换酷狗
切换酷我
```

### 🔄 切换模式

音乐发送模式设置**按会话隔离**，切换后不影响其他群聊/私聊：<br><br>
【仅管理员可使用】

```
切换发送模式 卡片
切换发送模式 语音
切换发送模式 文件
切换发送模式 默认
```

### 🎨 音乐卡片

当 `send_as_music` 设为 `0`（默认）时，点歌会以精美卡片形式展示：<br><br>
【音乐卡片只支持 QQ 平台】

- 包含歌曲封面、歌名、歌手
- 点击可跳转到对应平台播放
- 非 QQ 平台自动降级为文件/语音发送

### 🤖 LLM 调用

插件暴露了 `astr_meting_music` Function Tool，AI 模型可直接调用：

- **搜索**：传入 `keyword` + `source`（`index=-1`），返回歌曲列表 JSON
- **播放**：传入 `keyword` + `source` + `index`，自动发送音乐卡片

---

## ⚙️ 配置说明

### API 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `api_url` | 下拉选择 | `https://musicapi.chuyel.top/meting/` | 预设 API 或选择 `custom` |
| `api_type` | 整数 | `1` | 仅自定义时生效：1=Node API, 2=PHP API, 3=自定义参数 |
| `custom_api_url` | 字符串 | — | 自定义 API 基础地址 |
| `custom_api_template` | 字符串 | — | 自定义参数模板（需含 `:server` `:type` `:id` `:r`） |

<details>
<summary><b>API 类型详解</b></summary>

| 类型 | 请求格式 |
|------|----------|
| **Node API** | `{api_url}/api?server={server}&type={type}&id={id}` |
| **PHP API** | `{api_url}?server={server}&type=search&id=0&keyword={kw}&dwrc=false` |
| **自定义参数** | 完全自定义，支持 `:server` `:type` `:id` `:r` 占位符 |

</details>

### 搜索配置

| 配置项 | 类型 | 默认值 | 范围 | 说明 |
|--------|------|--------|------|------|
| `default_source` | 字符串 | `netease` | — | 默认音源 |
| `search_result_count` | 整数 | `9` | 3~30 | 搜索结果数量 |
| `search_result_expiration_time` | 整数 | `120` | 30~300s | 结果过期时间 |
| `search_results_withdrawn_after_timeout` | 整数 | `60` | -1~300s | 超时自动撤回；`0`=点歌后立即撤回；`-1`=不撤回 |
| `search_result_restrictions` | 布尔 | `false` | — | 搜索结果仅对搜索者本人有效 |

### 音乐发送配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `send_as_music` | 整数 | `0` | `0`=音乐卡片，`1`=语音切片，`2`=文件发送 |
| `send_if_not_supported` | 整数 | `2` | 非 QQ 平台备选发送方式 |

### 音乐卡片配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `api_sign_url` | 字符串 | `https://oiapi.net/api/QQMusicJSONArk/` | 音乐卡片签名 API |

### 下载与缓存配置

| 配置项 | 类型 | 默认值 | 范围 | 说明 |
|--------|------|--------|------|------|
| `max_file_size` | 整数 | `80` | 10~200 MB | 最大下载文件大小 |
| `enable_cache` | 布尔 | `false` | — | 启用缓存可减少带宽 |
| `max_cache_size` | 整数 | `1` | 1~32 GB | 最大缓存占用 |
| `daily_cleanup_time` | 字符串 | `04:30` | HH:MM | 每日定时清理时间 |

### 语音分段配置

| 配置项 | 类型 | 默认值 | 范围 | 说明 |
|--------|------|--------|------|------|
| `segment_duration` | 整数 | `120` | 30~300s | 每条语音最大时长 |
| `send_interval` | 浮点 | `1.0` | 0~10s | 片段发送间隔 |

---

## 🔧 技术说明

### 音源映射

| 用户指令 | API 参数 |
|----------|----------|
| QQ 音乐 | `tencent` |
| 网易云 | `netease` |
| 酷狗 | `kugou` |
| 酷我 | `kuwo` |

### 语音分段机制

```
┌──────────┐    ┌──────────┐    ┌──────────┐
│ 原音频    │ → │ 转码 WAV  │ →  │ 分段发送  │
│ (任意格式)│   │ 24kHz单声道│   │ 每段 ≤120s│
└──────────┘    └──────────┘    └──────────┘
```

- 超过分段时长的歌曲自动切割
- 末尾剩余 ≤7s 时合并到最后一段，避免碎片
- 发送完毕后自动清理临时文件

### 数据存储

| 数据 | 存储位置 | 生命周期 |
|------|----------|----------|
| 会话音源 | 内存 | 重启重置 |
| 搜索结果 | 内存 | 过期自动清除 |
| 下载音频 | 系统临时目录 | 播放后自动删除 |
| 缓存文件 | `%TEMP%/astrbot_meting_cache/` | 每日定时清理 |

---

## ❓ 常见问题

<details>
<summary><b>Q: 提示"请先在插件配置中设置 MetingAPI 地址"</b></summary>

请在 AstrBot WebUI 的插件配置页面中选择或填写正确的 API 地址。

</details>

<details>
<summary><b>Q: 音乐卡片无法显示</b></summary>

1. 确认 AstrBot 版本 ≥ `4.17.6`
2. 检查 `api_sign_url` 签名服务是否可用
3. 查看 DEBUG 日志中的兼容性检查结果

</details>

<details>
<summary><b>Q: 搜索歌曲提示"网络错误"</b></summary>

1. 检查 API 地址是否正确
2. 确认 API 类型选择是否匹配
3. 测试 API 是否可公网访问

</details>

<details>
<summary><b>Q: 播放歌曲提示缺少 pydub 依赖</b></summary>

请确保已安装 FFmpeg，并重新安装插件依赖：
```bash
pip install -r requirements.txt
```

</details>

<details>
<summary><b>Q: 非 QQ 平台如何发送音乐？</b></summary>

插件会自动检测消息平台，非 QQ 平台将按 `send_if_not_supported` 配置降级为文件或语音发送。

</details>

---

## 📂 项目结构

```
astrbot_plugin_meting/
├── main.py              # 🔌 插件主代码
├── metadata.yaml        # 📋 插件元数据
├── _conf_schema.json    # ⚙️ 配置文件 Schema
├── requirements.txt     # 📦 Python 依赖
├── README.md            # 📖 说明文档
├── CHANGELOG.md         # 📝 更新日志
├── LICENSE              # 📜 许可证
└── .gitignore           # 🙈 Git 忽略文件
```

---

## 🙏 致谢

|      |      |
|------|------|
| [初叶🍂 MetingAPI](https://github.com/chuyegzs/Meting-UI-API) | 二次开发的 MetingAPI 服务 |
| [MetingAPI](https://github.com/metowolf/Meting) | 原始音乐 API 服务 |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 多平台 LLM 聊天机器人框架 |
| [OIAPI](https://oiapi.net/) | 音乐卡片签名接口 |
| [NanoRocky](https://github.com/NanoRocky) | 功能添加与代码优化贡献者 |

---

## 📜 许可证

本项目基于 [MIT License](LICENSE) 开源。

<br>

<p align="center">
  <sub>如有问题或建议，欢迎加入反馈 QQ 群：<b>535563643</b>（必点 ⭐Star）</sub>
</p>

