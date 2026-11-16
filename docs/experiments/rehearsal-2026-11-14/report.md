# SRE 演练报告

验证范围：component。模拟结果不等于真实部署或自愈。
生成时间：2026-11-14T04:27:31.826946+00:00
统计范围：provided_interval

| 指标 | 结果 |
| --- | --- |
| 完成请求数 | 6 |
| 成功请求数（2xx） | 4 |
| 失败请求数（含 429） | 2 |
| 目标 | 99.00% |
| 成功率 | 66.67% |
| 错误预算剩余 | 0.00% |
| P95（秒） | 0.008499999999999997 |
| P95 口径 | prometheus_histogram_estimate |

## 操作时间线（最近最多 200 条）

| 时间 | 动作 | 事件 ID |
| --- | --- | --- |
| 2026-11-14T04:27:31.778298+00:00 | fault_started | c72d061472f645a2ab27b911a96a4416 |
| 2026-11-14T04:27:31.783634+00:00 | fault_cleared | 7c4425d844664c81bbfa76c431c78e6e |
| 2026-11-14T04:27:31.787362+00:00 | healing_simulation_enabled | 0f8e62d46258442bbe40bafd11e1c7a2 |
| 2026-11-14T04:27:31.789341+00:00 | healing_probe | ecb9fdd3e3004c768e52e952261d5307 |
| 2026-11-14T04:27:31.789403+00:00 | healing_incident_detected | f69427301d3548948c539724481401e1 |
| 2026-11-14T04:27:31.791311+00:00 | healing_probe | 752b3c229cfc497390a04fdf7dfa40b5 |
| 2026-11-14T04:27:31.793381+00:00 | healing_probe | 0fc4d83e611a4758859747dc897a7936 |
| 2026-11-14T04:27:31.793460+00:00 | healing_action_simulated | b0f942f146814f028dbc8b714d52786d |
| 2026-11-14T04:27:31.796299+00:00 | healing_probe | 0b5ecc28a77a472fbbf050614cb1ddcf |
| 2026-11-14T04:27:31.798355+00:00 | healing_probe | f7485770148c4369b4780a5aa299ff43 |
| 2026-11-14T04:27:31.798441+00:00 | healing_recovery_verified | 430d6c635748403d91552f9530ebec27 |
| 2026-11-14T04:27:31.801322+00:00 | deployment_started | c46dd3a45cff4856b4651e67a51d5f49 |
| 2026-11-14T04:27:31.803433+00:00 | deployment_candidate_checked | 29404cd377844d5a94be7f9f08841c6a |
| 2026-11-14T04:27:31.803558+00:00 | deployment_switched | f07ef133bed74982adcf656c61e207ee |
| 2026-11-14T04:27:31.805608+00:00 | deployment_observed | 8e8f3639da774168b5b988e4605fcc3c |
| 2026-11-14T04:27:31.805682+00:00 | deployment_completed | 83b975e5d19b4733b347d60beb95b1fe |
| 2026-11-14T04:27:31.808190+00:00 | deployment_started | 4c93cd2c129b4751a74219c85f158c8e |
| 2026-11-14T04:27:31.809943+00:00 | deployment_candidate_checked | 711dc3c84ea84a91b4a97d2fbcfdd5e3 |
| 2026-11-14T04:27:31.810020+00:00 | deployment_switched | 5ae98a4d3939465c9a0b3849d816a12f |
| 2026-11-14T04:27:31.812062+00:00 | deployment_observed | e50fdf9af134465c8113ef5c2ab450a6 |
| 2026-11-14T04:27:31.812121+00:00 | deployment_completed | 07b6a45358684e15b4e66cd8e427905c |
| 2026-11-14T04:27:31.814141+00:00 | deployment_rolled_back | 7c4a983ef58c4593808589d95ef5d068 |
| 2026-11-14T04:27:31.816567+00:00 | deployment_previous_replaced | cd6b715cb18045ac8dba274907c9bd3d |
| 2026-11-14T04:27:31.816659+00:00 | deployment_started | 6db837a9f1d5487cbcf58c75ebce51a4 |
| 2026-11-14T04:27:31.818724+00:00 | deployment_candidate_checked | a1d27244ccd746939612422152853df4 |
| 2026-11-14T04:27:31.818819+00:00 | deployment_switched | 2551530996fe41cba8c968c46cdc1020 |
| 2026-11-14T04:27:31.821215+00:00 | deployment_observed | ed95d53f867d4a37816af7bd71e0ad8d |
| 2026-11-14T04:27:31.821284+00:00 | deployment_rolled_back | 7e2ecad816a0495aa0217a647d3b4f6f |

## 边界
- Counts measure completed API requests, not client network failures.
- A process restart resets lifetime counters; do not compare different processes.
- Events are bounded in-memory observations, not a persistent audit database.
- Scope is caller-declared; simulation is not proof of real recovery or deployment.
