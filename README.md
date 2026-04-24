# 论文搜索小工具（FastAPI + SQLite）

## 1. 项目目录

- backend/app/main.py: API 入口，搜索/详情/导出接口
- backend/app/search_engine.py: 全文+模糊+语义混合检索
- backend/app/schemas.py: Pydantic 数据模型
- backend/requirements.txt: 依赖
- frontend/index.html: 移动端优先页面原型
- docs/ARCHITECTURE.md: 数据库设计、API 设计、部署方式

## 2. 快速运行

```bash
cd backend
pip install -r requirements.txt
python -m app.main
```

浏览器打开 `http://127.0.0.1:0421/` 即可。

> 默认监听 `0.0.0.0:421`，同一局域网内可用 `http://你的局域网IP:0421/` 访问。
> 后端会自动读取项目根目录下第一个 `.sqlite` 文件。
