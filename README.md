# 初叶🍂 MetingAPI 点歌插件

基于 MetingAPI 的点歌插件，支持QQ音乐、网易云、酷狗、酷我等音源。

**当前版本：v1.1.0**

- 解決了Discord使用完全沒有訊息的問題 Discord斜槓命令可能存在問題 仍在排查中
- 增加了一些後台以便排查異常用日誌 追查參數值的運行情況

> [!WARNING]
> Discord附加檔案或音訊的存在問題
> 
> ## 替代方案
> 
> 如果遇到檔案或無法正常附加傳送
> 
> 直接返回 MetingAPI得出音樂檔網址


**当前版本：v1.0.9**

- 改善兼容性 修正指令註冊 中文Discord斜槓命令可能造成無法生效的問題
- 修正識別點歌方式 以空格作為第2格參數作為基準



**当前版本：v1.0.8**

> [!WARNING]
> ## 兼容性声明
> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;·&nbsp;因 AstrBot 旧版本缺失对 JSON 消息的兼容性，所以要使用音乐卡片功能，您必须确保您的 AstrBot 版本在 `4.17.6` 以上。<p>
> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;·&nbsp;在遇到问题时，请打开 DEBUG 日志并检查插件输出的兼容性检查结果。<p>
> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;·&nbsp;如果需要帮助，提交 ISSUE 时请附带插件日志，和插件输出的兼容性检查结果，并说明目前 AstrBot 版本及插件版本。<p>

## 音乐卡片说明（推荐使用，因为转语音音质打折扣）

- 音乐卡片功能默认开启
- 开启后，用户输入“搜歌 xxx（歌名）”，再次输入“点歌 x（搜索列表歌曲位置，数字排列）”，会收到包含音乐信息的卡片，点击卡片的歌曲封面上的播放按钮可以直接播放音乐

## 功能特性

- 支持多音源：QQ音乐、网易云、酷狗、酷我（具体需要 API 支持）
- 会话级音源切换，不影响其他会话
- 智能语音分段发送，自动处理超过2分钟的歌曲
- 支持音乐卡片显示（可选）
- 快捷点歌指令，直接指定音源搜索
- 简单易用的命令交互
- 支持两种 API 类型，并支持自定义调用参数，最大限度兼容各种 MetingAPI

## 安装

1. （1）将插件目录 `astrbot_plugin_meting` 放入 AstrBot 的 `data/plugins` 目录
（2）WebUI中从链接安装:https://github.com/chuyegzs/astrbot_plugin_meting/
2. 在 AstrBot WebUI 的插件管理处启用该插件
3. 在插件配置中设置 MetingAPI 地址和类型

## 配置

在 AstrBot WebUI 的插件配置页面中，设置以下参数：

### MetingAPI 配置

**API 地址**
- **描述**：选择预设的 MetingAPI 或自定义
- **可选值**：
  - `https://musicapi.chuyel.top/meting/` - 初叶🍂竹叶 Furry API（带QQ音乐/网易云会员）
  - `https://metingapi.nanorocky.top/` - 呆呆酪灰的 MetingAPI
  - `custom` - 自定义 API
- **默认**：`https://musicapi.chuyel.top/meting/`

**API 类型**（仅在 API 地址为 custom 时生效）
- **描述**：选择 MetingAPI 的类型
- **可选值**：
  - `1` - Node API（默认）：标准 MetingAPI
  - `2` - PHP API：使用 `keyword` 参数传递搜索词
  - `3` - 自定义参数：使用占位符构建请求
- **默认**：`1`

**自定义 API 地址**（仅在 API 地址为 custom 时生效）
- **描述**：自定义 MetingAPI 地址
- **示例**：`https://api.example.com/meting`

**自定义 API 模板**（仅在 API 类型为 3 时生效）
- **描述**：自定义请求模板，必须包含 `:server`、`:type`、`:id`、`:r` 占位符
- **示例**：`server=:server&type=:type&id=:id&r=:r`

### 其他配置

**默认音源**
- **描述**：默认使用的音乐平台
- **可选值**：`tencent`（QQ音乐）、`netease`（网易云）、`kugou`（酷狗）、`kuwo`（酷我）
- **默认**：`netease`

**使用音乐卡片**
- **描述**：是否使用音乐卡片显示搜索结果
- **默认**：`false`

**音乐卡片签名地址**
- **描述**：用于获取音乐卡片签名的 API 地址
- **默认**：`https://oiapi.net/api/QQMusicJSONArk/`

**搜索结果显示数量**
- **描述**：搜索结果显示的歌曲数量
- **范围**：5-30
- **默认**：10

## 使用方法

### 查看帮助

发送以下任一指令查看所有可用命令：
```
点歌指令
```

### 切换音源

在当前会话中切换音乐平台，不影响其他会话：

- `切换QQ音乐`  - 切换到QQ音乐
- `切换网易云`  - 切换到网易云
- `切换酷狗`  - 切换到酷狗
- `切换酷我`  - 切换到酷我

### 搜索歌曲

使用当前会话的音源搜索歌曲：

```
搜歌 一期一会
```

搜索后会显示歌曲列表，包含歌曲名和歌手信息。

### 播放歌曲

#### 方式一：通过序号播放
在搜索结果后，使用以下命令播放指定序号的歌曲：

```
点歌 1
```

其中 `1` 是歌曲序号（如：点歌 1、点歌 2、点歌 3...）。

**注意**：`点歌` 和数字之间必须有空格。

#### 方式二：直接点歌（快捷指令）
直接指定音源搜索并播放第一首歌曲：

```
网易点歌 一期一会
腾讯点歌 晴天
QQ点歌 稻香
酷狗点歌 演员
酷我点歌 告白气球
```

这些快捷指令会忽略当前会话的音源设置，直接在指定平台搜索。

### 音乐卡片

如果启用了音乐卡片功能（`use_music_card: true`），搜索结果将以精美的卡片形式展示，包含：
- 歌曲封面
- 歌曲名称和歌手
- 点击跳转链接

## 依赖

插件需要以下依赖库（会在安装插件时自动安装）：

- `aiohttp>=3.8.0` - 异步 HTTP 请求
- `pydub>=0.25.1` - 音频处理
- `packaging` - 版本解析

**注意**：`pydub` 需要系统安装 FFmpeg。请确保系统已安装 FFmpeg 并在 PATH 中。

### FFmpeg 安装

**Windows:**
```bash
# 使用 winget
winget install ffmpeg

# 或手动下载：https://ffmpeg.org/download.html
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get update
sudo apt-get install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

## 技术说明

### 音源映射

| 用户输入 | API 参数 |
|---------|---------|
| QQ音乐 | tencent |
| 网易云 | netease |
| 酷狗 | kugou |
| 酷我 | kuwo |

### API 类型说明

**1. Node API（默认）**
- 标准 MetingAPI 格式
- 请求地址：`{api_url}/api?server={server}&type={type}&id={id}`

**2. PHP API**
- PHP 版本 MetingAPI
- 请求地址：`{api_url}?server={server}&type=search&id=0&keyword={keyword}&dwrc=false`

**3. 自定义参数**
- 完全自定义请求格式
- 支持占位符：`:server`、`:type`、`:id`、`:r`

### 语音分段机制

QQ 语音时长上限为2分钟，插件会自动将长歌曲分割为多个片段：
- 每段时长：可配置（默认120秒）
- 格式：WAV
- 发送方式：逐段发送

### 数据存储

- 会话音源设置存储在内存中，重启后恢复为默认音源
- 搜索结果临时存储在内存中，仅用于当前会话
- 下载的音频文件存储在系统临时目录，播放完成后自动删除

## 常见问题

### Q: 提示"请先在插件配置中设置 MetingAPI 地址"
A: 请在 AstrBot WebUI 的插件配置页面中选择或填写正确的 MetingAPI 地址。

### Q: 音乐卡片无法显示
A: 请检查：
1. `use_music_card` 是否设置为 `true`
2. `api_sign_url` 是否配置正确
3. 签名服务是否可用

### Q: 搜索歌曲时提示"网络错误"
A: 请检查：
1. MetingAPI 地址是否正确
2. API 类型是否匹配
3. 网络连接是否正常

### Q: 播放歌曲时提示"缺少 pydub 依赖"
A: 请确保已安装 FFmpeg，并重新安装插件依赖。

## 开发

### 项目结构

```
astrbot_plugin_meting/
├── main.py              # 插件主代码
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置文件 Schema
├── requirements.txt     # Python 依赖
├── README.md           # 说明文档
├── LICENSE             # 许可证
└── .gitignore         # Git 忽略文件
```

### 贡献

欢迎提交 Issue 和 Pull Request！

## 许可证

MIT License

## 致谢

- [初叶🍂MetingAPI](https://github.com/chuyegzs/Meting-UI-API) - 初叶🍂二次开发的MetingAPI
- [MetingAPI](https://github.com/metowolf/Meting) - 音乐 API 服务
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) - AstrBot机器人框架
- [NanoRocky](https://github.com/NanoRocky) - 功能添加与代码优化，部分功能的贡献者

## 支持

如有问题或建议，欢迎加入 初叶🍂Furry 插件反馈QQ群：535563643（必点Star）
