# 飞花令

飞花令是基于 FastAPI 和 SQLite 的诗词检索、对战网站。前端提供三套主题，服务端负责诗句匹配、判题、游客账号和反馈管理。本仓库仅公开程序代码和网站所需的非个人静态素材。

线上演示：https://feihualing.ltkeep.cn/

## 本地运行

需要 Python 3.12，以及自行准备的兼容诗库 `poetry.sqlite3`。本仓库不提供诗词语料、构建清单、数据库文件或下载命令。把诗库放在私有数据目录，再启动服务：

```bash
./scripts/setup.sh
FEIHUA_DATA_DIR=/path/to/private/data ./scripts/start.sh
```

访问 http://127.0.0.1:8765/ 。服务运行时会在私有数据目录建立业务数据库；不要把它加入 Git。

## 测试

不依赖语料的账号、反馈和部署协议测试可直接运行：

```bash
.venv/bin/python -m pytest -q tests/test_accounts.py tests/test_community.py tests/test_verify_community_deployment.py
```

完整历史测试还需要兼容诗库及内部校对清单，这些数据不随代码发布。测试账号、密码和反馈内容均为虚构样例；测试使用临时业务数据库，不应连接生产数据。

## 数据边界

本仓库不包含诗词语料、用户账号、密码哈希、会话、对局、反馈或通知记录，也不包含任何 SQLite 数据库、备份、环境文件、SSH 密钥、服务器配置和个人赞赏码图片。

运行时数据库默认写入 `data/`，可用 `FEIHUA_DATA_DIR` 指定独立的持久化目录。环境变量示例见 `.env.example`；真实密钥只应保存在部署环境。仓库中的支持页面引用了站点所有者单独管理的赞赏码图片，克隆后需自行提供图片，或按需要调整该页面。

页面使用大都会艺术博物馆的公有领域山水画图像和 Lucide 图标；图标许可见 `static/assets/lucide-LICENSE`。
