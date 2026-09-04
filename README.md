# 音色复刻

本地网页工具：上传一段人声，调用阿里云百炼 MiniMax 声音复刻，再用新音色合成任意文本。

## 环境要求

- Python 3.10 或更高
- 能访问 `dashscope.aliyuncs.com`
- 阿里云百炼 API Key（[控制台](https://bailian.console.aliyun.com/) 创建，需开通 MiniMax 声音复刻权限）

## 安装

进入本项目目录后执行：

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 运行

```bash
source .venv/bin/activate          # 若尚未激活
python server.py
```

终端出现类似输出即表示成功：

```text
Uvicorn running on http://127.0.0.1:8765
```

浏览器打开：**http://127.0.0.1:8765**

也可用 uvicorn 直接启动（效果相同）：

```bash
uvicorn server:app --host 127.0.0.1 --port 8765 --reload
```

可选：把 Key 写进环境变量，页面就不用每次填：

```bash
export DASHSCOPE_API_KEY="sk-你的密钥"
python server.py
```

停服务：在运行终端按 `Ctrl + C`。

## 部署到 Render（给别人用）

密钥始终由使用者在网页右上角自己填写，服务器上**不要**配置 `DASHSCOPE_API_KEY`。每个人用自己的百炼 Key，费用各自结算。

Render 会提供 `https://xxx.onrender.com`，自带 HTTPS。本项目用 Docker 部署（镜像里有 ffmpeg，超长音频才能自动截取）。

### 1. 把代码推到 GitHub

本机在项目目录执行（若还没有 git 仓库）：

```bash
cd /Users/chao/Desktop/voice_clone
git init
git add Dockerfile docker-compose.yml render.yaml requirements.txt server.py index.html README.md .dockerignore
git commit -m "Deploy voice clone to Render"
```

到 [GitHub New repository](https://github.com/new) 建一个**私有**仓库（例如 `voice-clone`），不要勾选自动加 README。然后：

```bash
git remote add origin https://github.com/你的用户名/voice-clone.git
git branch -M main
git push -u origin main
```

`.env`、`.venv` 不要提交。

### 2. 在 Render 新建 Web Service

1. 打开 [https://dashboard.render.com](https://dashboard.render.com)，用 GitHub 登录。
2. 右上角 **New** → **Web Service**。
3. 选 **Git Provider**，授权后选中刚才的 `voice-clone` 仓库，点 **Connect**。
4. 按下面填表（其余可保持默认）：

| 项 | 填什么 |
| --- | --- |
| Name | `voice-clone`（子域名会变成 `voice-clone-xxxx.onrender.com`） |
| Region | **Singapore**（离国内最近，访问百炼更稳） |
| Branch | `main` |
| Language / Runtime | **Docker**（有 Dockerfile 时 Render 会自动识别） |
| Instance type | 建议 **Starter**。Free 会休眠，第一次打开要等几十秒 |

5. **不要**添加 `DASHSCOPE_API_KEY`。
6. 展开 **Advanced**，Health Check Path 填：`/health`
7. 点 **Create Web Service**。

也可以不填表，仓库里已有 `render.yaml`：在 Dashboard 选 **New** → **Blueprint**，选这个仓库，按蓝图创建。

### 3. 等构建完成

Builds 页出现 **Live** 即成功。打开 Render 给的地址，例如：

`https://voice-clone-xxxx.onrender.com`

把这个链接发给别人。对方打开后：

1. 右上角填入自己的百炼 API Key  
2. 上传音频 → 开始复刻 → 合成  

### 4. 可选：绑自己的域名

Service → **Settings** → **Custom Domains** → 添加域名，按提示去 DNS 加 CNAME。证书由 Render 自动签。

### 常见问题

**Deploy 失败 / 检测不到端口**  
确认日志里有 `Uvicorn running on http://0.0.0.0:xxxx`。Render 会注入 `PORT`（默认 10000），本服务会读这个变量。

**打开网页很慢或 502**  
Free 实例一段时间没人访问会休眠。换成 Starter 就不会。

**复刻一直转圈后失败**  
Render 机房在海外，请求阿里云百炼可能较慢。Region 选 Singapore；实例不要用太小的 Free。仍不稳定就改部署到国内服务器。

**别人填了 Key 仍鉴权失败**  
那是对方的 Key 无效或未开通 MiniMax 声音复刻，与 Render 无关。

## 怎么用

1. 右上角填入百炼 API Key（`sk-` 开头）。Key 会保存在浏览器本地，下次自动带上。
2. 左侧拖入或选择音频（mp3 / m4a / wav），或粘贴一段公网可访问的音频 URL。
3. 确认模型、`voice_id`、试听文案。`voice_id` 页面会自动生成，也可改成自己的。
4. 点「开始复刻」，等待上传和接口返回。
5. 右侧出现新音色 ID 和官方试听。可改文案、语速、情感，再点「合成语音」。

### 音频要求

| 项 | 限制 |
| --- | --- |
| 格式 | mp3 / m4a / wav |
| 时长 | 10 秒 – 5 分钟 |
| 大小 | ≤ 20 MB |

### voice_id 规则

- 长度 8–256
- 以字母开头，以字母或数字结尾
- 中间只能是字母、数字、连字符 `-`、下划线 `_`

同一 `voice_id` 不要重复复刻；换音色就换一个新 ID。

### 可选参数

- **模型**：`speech-2.8-hd`（默认，音质优先）/ `speech-2.8-turbo` / `speech-02-hd` / `speech-02-turbo`
- **语种增强**：`auto`（默认）、中文、粤语、英语
- **降噪 / 音量归一化**：源音频嘈杂或不齐时再开

## 常见问题

**页面打不开**  
确认服务已在 `8765` 端口跑着，地址是 `http://127.0.0.1:8765`，不是 `https`。

**提示无法连接百炼**  
检查本机网络、代理和防火墙，确保能访问 `dashscope.aliyuncs.com`。本服务会绕过系统 HTTP 代理直连百炼。

**鉴权失败**  
Key 无效、过期，或账号未开通 MiniMax 声音复刻。到百炼控制台核对。

**无复刻权限（2038）**  
账号需要完成认证并开通对应模型权限。

**输入参数不正常（2013）**  
检查音频格式、时长、以及 `voice_id` 是否符合规则。

**voice_id 报错**  
不要用纯数字、不要以下划线结尾。用页面自动生成的 ID 最省事。
