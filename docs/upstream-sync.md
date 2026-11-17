# AiNieePlus 上游同步与发布

更新来源固定为 `Xitaoyou/AiNieePlus`，上游 `NEKOparapa/AiNiee` 仅用于源代码合并。

`sync-upstream.yml` 每天 02:23 UTC 或手动运行：

1. 读取上游 main，未出现新提交则结束。
2. 在干净的临时 CI 检出中普通合并，保留三方合并历史；冲突即中止，报告冲突文件，不推送。
3. 完整后端测试与独立 Qt 界面测试通过后，正常 fast-forward 推送 main；若检查期间 main 变化，拒绝推送，不强推。
4. 显式触发 `main.yml` 构建、安装后自检与发布。GitHub 的 `GITHUB_TOKEN` 推送不会触发 push 工作流，所以使用 [workflow_dispatch](https://docs.github.com/en/actions/concepts/security/github_token) 避免漏构建。

同步报告保存在 Actions 的任务摘要和 `upstream-sync-report` 附件中。合并冲突不会自动选择上游版本覆盖漫画编辑器或 Fork 更新设置。

发布资源中的 `Frontend/resources/version/version.json` 由 CI 写入 Fork 构建号、提交号、仓库与原上游版本。更新器只选择本仓库的完整 Windows 便携包，拒绝其他仓库下载链接；安装版与便携版的数据标记由更新器保留。
