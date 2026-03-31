# 爬评论投币（开源版）

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)
[![Security Policy](https://img.shields.io/badge/Security-Policy-orange.svg)](./SECURITY.md)
[![Code of Conduct](https://img.shields.io/badge/Code-Of%20Conduct-purple.svg)](./CODE_OF_CONDUCT.md)

一个用于 B 站评论抓取与自动化互动（点赞/投币）的 Python 项目，提供命令行与 Web 两种使用方式。

## 目录

- [免责声明](#免责声明)
- [功能概览](#功能概览)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [凭证配置](#凭证配置)
- [使用方式](#使用方式)
- [常见问题（FAQ）](#常见问题faq)
- [Roadmap](#roadmap)
- [贡献指南](#贡献指南)
- [许可证](#许可证)

## 免责声明

本项目仅用于学习与技术研究。请严格遵守平台协议、当地法律法规与账号安全规范。因不当使用导致的任何后果由使用者自行承担。

## 功能概览

- 抓取指定视频一级评论并落盘为 Excel
- 评论用户去重，避免重复处理
- 按流程执行点赞 + 投币
- 提供 Web 后端与日志流式输出能力

## 项目结构

```text
.
├── main.py                 # 命令行入口
├── server.py               # Web 后端入口
├── templates/              # 前端页面模板
├── credentials.example.json
├── requirements.txt
├── CONTRIBUTING.md
├── SECURITY.md
├── CODE_OF_CONDUCT.md
└── LICENSE
```

## 快速开始

### 环境要求

- Python 3.10+
- macOS / Linux / Windows

### 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate
pip install -r requirements.txt
```

## 凭证配置

1. 复制示例文件：

```bash
cp credentials.example.json credentials.json
```

2. 在 `credentials.json` 填写你自己的字段：

- `sessdata`
- `bili_jct`
- `buvid3`
- `dedeuserid`

> `credentials.json` 已在 `.gitignore` 中，不会被提交到仓库。

## 使用方式

### 命令行模式

```bash
python main.py BVxxxxxxxxxx
# 或
python main.py https://www.bilibili.com/video/BVxxxxxxxxxx
```

### Web 模式

```bash
python server.py
```

浏览器访问：`http://localhost:8765`

## 常见问题（FAQ）

### 1) 为什么抓不到评论或评论数为 0？

- 常见原因是 Cookie 过期或字段不完整。
- 请更新 `credentials.json`，并确认四个字段都非空。

### 2) 出现 `-412` 风控怎么办？

- 这是请求频率相关风控。
- 建议增大间隔、降低单次处理量，分批执行。

### 3) 开源后会泄露我的 Cookie 吗？

- 代码中不再硬编码凭证。
- `credentials.json` 已被忽略，不会被 Git 跟踪。
- 仍建议你定期更换 Cookie，避免历史泄露风险。

### 4) 数据文件会不会被提交？

- 默认已忽略评论 Excel、日志、运行状态等本地数据文件。

## Roadmap

- [ ] 补充单元测试和基础 CI
- [ ] 增加配置文件化（延迟、重试、风控策略）
- [ ] 增加 Docker 运行方式
- [ ] 增加更细粒度的错误码与诊断提示
- [ ] 增加“仅采集/仅处理/全流程”三种任务模式

## 贡献指南

- 贡献流程见 `CONTRIBUTING.md`
- 行为准则见 `CODE_OF_CONDUCT.md`
- 安全问题反馈见 `SECURITY.md`

## 许可证

本项目基于 [MIT License](./LICENSE) 开源。
