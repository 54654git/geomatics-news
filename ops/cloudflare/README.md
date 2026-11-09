# 独立触发器（代码已准备，尚未替你部署）

GitHub 自带 schedule 可能延迟或丢弃，独立触发器用于补充触发；GitHub 运行机器仍可能排队。保留仓库内的 schedule。

需要仓库拥有者自己登录 Cloudflare 并创建/授权凭据；不需要把任何密钥发给聊天助手或提交仓库。

1. 在 GitHub 的细粒度个人令牌页面，仅授权 `54654git/geomatics-news` 的 **Actions: Read and write**，设合适有效期。公开健康状态可直接访问，无需额外授予代码写入权限。
2. 在本目录终端依次执行 `npx wrangler login`、`npx wrangler secret put GITHUB_TOKEN`（在本机提示框粘贴令牌）、`npx wrangler deploy`。
3. 查看 Cloudflare Cron 日志；随后在 GitHub Actions 确认 workflow_dispatch 被接受、采集步骤是否执行，以及 `reports/health.json` 的 period_id/complete。仅 HTTP 204 不表示周报已交付。

计划时点为 UTC 周一、周二 00:25 和 02:25，即北京时间 08:25、10:25。普通触发不强制覆盖，同周已完成会跳过；补充源最多补采一次、核心失败最多三次。需要重做时在 GitHub Run workflow 勾选 force。

不要将令牌写入 worker.js、wrangler.jsonc、README 或截图。令牌到期需在 Cloudflare 更新 Secret。
