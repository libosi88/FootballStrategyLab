# 验证入口

当前源码为 0.7.0（以 `lab/common.py` 的 VERSION 为准）：默认研究目标是全部选定历史（`research_objective=historical`），0.5.0 的比例门槛、样本外留出与赛前即时盘触发保留为显式验证模式（`research_objective=validation`）；随源码分发的摘要见根目录 `本版实测与未完成项.md` 和 `docs/当前交付范围.md`。旧顶层JSON、`frozen_regression.json`和旧固定副本只认证其绑定版本，不因名称为“当前/最终”认证当前代码。

目录口径：

0.7.0 当前源码的验收入口为 `_rationality_finish_20260916_r1/LATEST.json` 指向的报告，状态须为 PASS_SCOPED_070；`_repair_seven_20260916_r1/repair_result.json` 记录的是 0.6.3 工作树那一轮，只认证其自身指纹。须同时核对引擎和完整发布集合指纹；没有最终结果、结果为 PARTIAL/FAIL 或源码已变化时，不能用旧目录的 PASS 替代。本轮不覆盖历史证据，也不自动提交工作区内已有的其他改动。

- `frozen_releases/`：已发布的只读源码与证据副本，不覆盖、不在其中开发。
- `history/`、`legacy_*`、`v03*`、`revision_*`：历史证据。`v04/`、`repair_v048/`、`repair_v049*`、`trial_india_*` 等大体积旧证据已于 2026-09-16 按用户要求清理，引用它们的历史章节只作记录。`history/retired_scripts/` 保存已退役的维护脚本，默认拒绝运行。
- `current_*`、`real_smoke_*`、`*_tests.log`：历次开发或试跑记录；文件名包含 current 不代表它属于当前版本。
- `_audit*`、`_debug*`：独立审阅者的诊断文件，不作为权威验收入口。
- `*_review*.json`、`*_audit.json`：问题处置记录，以其绑定指纹和版本判定适用性。

保留历史路径以免破坏旧证据引用；Git 排除工作区、大型缓存、历史日志与重复源码副本，跟踪源码及关键维护工具。

修订流程：使用项目.venv，保留上版副本，完成代码修订后更新SBOM、停止编辑，运行 `frozen_regression.py --output-dir validation/新修订`；`--standard-empty`/`--standard-nonempty`分别是验证模式下微型全16完整字典空结果和90场全16每方向10节点非空合成验收；默认的全历史研究目标另用 `historical_acceptance.py --case empty|full|limited --output-dir validation/新修订/historical_<case>` 各跑一次（3场完整、90场完整、90场每方向10节点有限）。默认单元测试和引擎自检都不包含这些端到端链路，五份结果都是发布前必跑项。汇集相同引擎、测试/配置的管线、依赖、UI证据，写完最终文档摘要后再跑一次最终隔离回归。接着运行 `finalize_review_status.py --evidence-dir 该目录`、`preserve_frozen_revision.py --evidence-dir 该目录`，核对一致性后本地提交Git。固化脚本只写状态/证据，不修改已经验收的源码摘要。历史副本不覆盖。

详见 [版本与维护说明](../docs/版本与维护说明.md)。真实联赛试跑是否启动，以用户当前授权为准；以上命令只做软件回归和证据固化。
