# GPT Image Studio

个人使用的 GPT 生图工作台，支持普通生图、图片编辑、对话式连续生图。项目不依赖 Docker，后端使用 FastAPI + SQLite，前端使用 React + Vite，生产环境由后端托管前端静态文件。

## 功能

- 前端自定义接口地址和密钥，支持 OpenAI 兼容中转地址。
- 普通生图：强制调用 `/v1/responses` + `image_generation` 工具。
- 图片编辑：同样走 `/v1/responses`，参考图和 mask 会作为 `input_image` 传入。
- 对话式生图：调用 `/v1/responses` + `image_generation` 工具，支持 `auto / generate / edit`。
- 支持常用参数：Responses 模型、图片工具模型、尺寸、质量、数量、背景、输出格式、压缩、moderation、partial images、action。
- 生图失败时前端显示完整失败原因，并支持一键复制。
- 历史对话和历史图库会轻量保存到 SQLite，可查看、修改并继续编辑旧图。
- 本地 SQLite 记录任务、会话、消息和生成图片。
- 移动端适配。
- Ubuntu 一键安装和启动脚本。

## 本地运行

```bash
cp .env.example .env
# 编辑 .env 填入 IMAGE_API_BASE_URL 和 IMAGE_API_KEY
bash scripts/install_ubuntu.sh
bash scripts/start.sh
```

打开 `http://服务器IP:8010`。默认端口是 `8010`，用于尽量避开已有项目常用的 `8000`/`3000`/`5000` 端口。

## 上传 GitHub

`.env`、数据库、上传图片、生成图片都已在 `.gitignore` 中排除。上传前请确认不要手动添加 `.env`。

```bash
git init
git add .
git commit -m "Initial GPT Image Studio"
git remote add origin YOUR_GITHUB_REPO_URL
git push -u origin main
```

## 云服务器部署

Ubuntu 服务器上：

```bash
git clone https://github.com/howarezhh/generate-image.git
cd generate-image
IMAGE_API_KEY='sk-your-key-here' bash scripts/configure_env.sh
bash scripts/install_ubuntu.sh
bash scripts/start.sh
```

如果服务器已有项目占用了 `8010`，请在 `.env` 里改 `PORT`，例如：

```bash
PORT=8020
```

后台运行：

```bash
bash scripts/start_background.sh
```

启动后自检：

```bash
bash scripts/self_check.sh
```

停止后台服务：

```bash
bash scripts/stop.sh
```

## 配置说明

- `IMAGE_API_BASE_URL`：OpenAI 或兼容服务地址，例如 `https://api.openai.com` 或你的中转地址。
- `IMAGE_API_KEY`：API 密钥。
- `PORT`：后端服务端口，默认 `8010`。
- `DATABASE_PATH`：SQLite 数据库位置。
- `STORAGE_DIR`：上传和输出图片目录。

前端右侧配置面板也可以临时覆盖 `.env` 中的接口地址和密钥，配置会保存在浏览器本地。
