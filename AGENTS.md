# 项目交付约定

完成项目修改时，同步更新 GitHub 仓库 `zhusongmei9-tech/sre-failure-lab` 的 `main`。在同一批改动中维护源码、`docs/from-zero/` 教程、配置示例和验收方法，让读者能用当前的下载包复现当前行为。先运行相关测试，提交前检查文件清单，排除本机凭据、私有 inventory、kubeconfig 和运行数据。

日常修改沿用现有项目版本和 `v0.6.0` Release，沿用附件名 `sre-faliure-lab-v0.6.0.tar.gz`。已发布的 Git 标签固定在原提交。推送后跟踪该提交的代码检查与发布任务直到完成，核对 Release 中的源码提交、唯一自定义附件和 SHA-256，再回报交付结果。以使用者对本次修改的明确要求为准。
