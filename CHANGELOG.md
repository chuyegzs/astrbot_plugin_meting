# 更新日志

## 1.0.4 (2025-02-20)

### 改进
- API 配置改为支持三种类型：
  - **1. Node API**（默认）：标准 MetingAPI，地址不带后缀，如 `https://api.example.com/meting`
  - **2. PHP API**：PHP 版本 MetingAPI，使用 `keyword` 参数
  - **3. 自定义参数**：在 MetingAPI 地址填写完整模板，如 `https://api.i-meto.com/meting/api?server=:server&type=:type&id=:id&r=:r`
- 配置项名称改回 `api_url`，更简洁直观
- 新增 `api_type` 配置项用于选择 API 类型

### 更新鸣谢

- 在此非常感谢[NanoRocky](https://github.com/NanoRocky)提供的功能添加与Bug修复代码

## 1.0.3 (2025-02-20)

### 改进
- API 配置改为模板格式，支持自定义占位符 `:server`、`:type`、`:id`、`:r`，最大限度兼容各种 MetingAPI
- 搜索命令改为 `搜歌 <关键词>`，更直观
- 播放命令改为 `点歌 x`（必须带空格），避免与搜索命令冲突

### 移除
- 移除 `_validate_api_url` 方法，统一使用 `_validate_url` 进行 URL 安全验证

## 1.0.0 (2025-02-16)

### 功能
- 基于 MetingAPI 的点歌插件
- 支持自定义 MetingAPI 模板，最大限度兼容各种 API 格式
- 支持QQ音乐、网易云、酷狗、酷我音源
- 支持切换当前会话的音源
- `搜歌 <关键词>` 搜索歌曲
- `点歌 x`（带空格）播放第x首搜索结果
- 歌曲自动分段为语音消息发送

### 安全性
- URL 安全校验，防止 SSRF 攻击
- 文件头魔数检测，验证音频文件有效性
- 临时文件使用唯一命名空间前缀

### 并发安全
- 会话数据封装为 SessionData 类，避免裸 dict 外泄
- 会话清理操作在锁保护下进行
- 临时文件追踪和清理机制
