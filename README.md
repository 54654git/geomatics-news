# Geomatics News：测绘与海洋测绘新闻周报

这是一个面向测绘工程（海洋测绘方向）学生的新闻采集器：读取官方 RSS 和 GDELT 公开新闻检索结果，重点补充中文与海洋测绘资讯，抓取可访问的正文，按专业关键词评分、去重、翻译后保存到 SQLite，并生成 Markdown 周报。

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

不调用翻译服务：

```powershell
python src/geomatics_news.py run --no-translate
```

默认会先尝试 Google 翻译接口，再回退到 MyMemory；翻译失败时保留原标题和原文摘要，不会让任务失败。原文链接始终保留。

## 增加新闻源

编辑 `config/sources.yaml`。当前配置包含中文海洋测绘、中文测绘、国际海洋测绘三个 GDELT 检索源，以及 NASA、ESA 官方 RSS。GDELT 是新闻聚合检索源，原文链接仍指向实际新闻网站。增加 HTML 源前先确认网站服务条款和 robots 规则。

GDELT 接口有访问频率限制，程序会在多个检索源之间自动等待并对限流进行一次重试。

## 自动运行

`.github/workflows/weekly.yml` 会在每周一北京时间 09:00 运行，也可以在 GitHub Actions 页面手动触发。它会把周报和数据库提交回仓库。

## 后续可扩展

- 增加飞书、企业微信、Telegram 或邮件通知；
- 使用 Playwright 处理动态页面；
- 接入更稳定的翻译 API 或大语言模型，但始终保留原文链接；
- 把“海洋测绘”关键词和评分权重按你的学习反馈持续校准。

