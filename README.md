# B站爬评论投币

一个开箱即用的小工具：抓评论 + 自动点赞/投币。  
你只需要配置自己的 Cookie 就能跑。

## 3 步使用（前端粘贴 Cookie）

### 1) 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2) 启动 Web 界面

```bash
python server.py
```

然后打开 `http://localhost:8765`

### 3) 在页面里粘贴 Cookie 并开始

在左侧输入框粘贴你的浏览器 `bilibili.com` 的完整 Cookie，点「更新 Cookie」；

在「目标视频」输入 BV 号/链接，点「开始爬取并投币」。

## 常见问题

- 评论抓不到：通常是 Cookie 过期，重新在页面里粘贴更新 Cookie
- Cookie 更新失败：页面会提示“未找到字段”，请检查 Cookie 是否完整（含 `SESSDATA` / `bili_jct` / `buvid3` / `DedeUserID`）
- 出现 `-412`：触发风控，降低频率/分批执行

## 免责声明

仅供学习交流，请遵守平台规则与法律法规，风险自负。

## 许可证

[MIT](./LICENSE)
