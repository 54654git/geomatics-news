# Geomatics News：测绘与海洋测绘新闻周报

这是一个面向测绘工程（海洋测绘方向）学生的新闻采集 MVP：读取公开 RSS/Atom，抓取可访问的正文，按专业关键词评分，去重后保存到 SQLite，并生成 Markdown 周报。

## 本地运行

```powershell
# 进入本项目根目录（即包含 requirements.txt 的目录）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python src/geomatics_news.py run --since-days 8
```

报告位于 `reports/`，数据库位于 `data/news.db`。

调试采集而不抓正文：

```powershell
python src/geomatics_news.py collect --no-content
```

## 增加新闻源

编辑 `config/sources.yaml`。优先添加官方 RSS/Atom；HTML 源只作为后备，并先确认网站服务条款和 robots 规则。启用条目的 `enabled` 改为 `true`。

## 自动运行

`.github/workflows/weekly.yml` 会在每周一北京时间 09:00 运行，也可以在 GitHub Actions 页面手动触发。它会把周报和数据库提交回仓库。

## 后续可扩展

- 增加飞书、企业微信、Telegram 或邮件通知；
- 使用 Playwright 处理动态页面；
- 对摘要调用大语言模型，但始终保留原文链接；
- 把“海洋测绘”关键词和评分权重按你的学习反馈持续校准。

