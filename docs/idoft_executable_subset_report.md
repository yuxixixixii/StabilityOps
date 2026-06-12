# IDoFT 可执行修复候选子集构建报告

## 1. 结果定位

本文件记录本项目从 IDoFT / FlakyFix-compatible metadata 中构建可执行 flaky-test repair 候选子集的过程和结果。该结果可以作为 StabilityOps DSL 后续修复实验的主候选池，也可以作为一个独立的数据集构建贡献：

> 在已知 flaky test metadata 中，系统性识别哪些样本在当前可复现实验环境下具备后续 LLM 修复实验所需的基本可执行性，并明确排除无法 checkout、无法解析依赖、无法编译、无法定位测试或基础设施不可用的样本。

该贡献的价值不在于证明这些测试仍然能复现 flaky 行为，而在于将原始 metadata 转换为一个可审计、可复用、可继续执行修复实验的候选池。后续研究可以直接基于该子集开展 LLM 修复、prompt 对比、agent 对比或 developer patch similarity 分析，避免在大量已经无法构建的历史仓库上重复消耗时间。

## 2. 贡献边界

本筛选结果的语义是：

```text
verified_feasible = 样本具备后续修复实验所需的基本可执行性
verified_infeasible = 样本在当前环境和协议下不适合作为执行式修复实验对象
```

其中 `verified_feasible` 表示：

1. GitHub 仓库可获取。
2. IDoFT 记录的检测 commit 可 checkout。
3. Maven module 路径存在。
4. 目标测试文件可定位。
5. PR patch 可获取，并可用于评价或 oracle 分析。
6. 目标 Maven 单测 smoke run 通过一次。

它不表示：

1. 已经复现 pre-fix flaky 行为。
2. 已经证明 developer patch 修复有效。
3. 已经证明该测试在任意机器或任意未来时间都可执行。
4. 已经保证所有依赖长期可下载。

因此，论文中应使用如下表述：

```text
executable repair candidate subset
buildable and target-test-runnable subset
execution-ready IDoFT repair subset
```

避免使用如下过强表述：

```text
reproducible flaky benchmark
fully reproducible flaky-test dataset
fixed flaky benchmark
```

## 3. 输入数据

原始输入来自 IDoFT 的 fixed PR single-label Java/Maven 候选样本。项目当前使用的 metadata 文件为：

```text
data/metadata/idoft_candidate_fixed_pr_single_label.csv
```

输入规模：

```text
total samples: 2349
unique repositories: 262
language/build focus: Java / Maven
label policy: single-label primary set
source status: IDoFT PR-linked known flaky tests
```

选择 single-label 样本的原因是：本项目的核心方法需要把 flaky 根因包装为 stability intent。单标签样本更适合建立清晰的 intent-to-repair 映射，复合标签样本则应留作 multi-intent repair 的扩展分析。

## 4. 筛选协议

### 4.1 总体流程

每个样本按以下流程处理：

```text
读取 IDoFT metadata
        ↓
下载或复用 GitHub repo cache
        ↓
下载或复用 PR patch cache
        ↓
在服务器创建样本 worktree
        ↓
checkout 到 SHA Detected
        ↓
定位 module path 和目标测试文件
        ↓
运行 Maven 单测 smoke validation
        ↓
记录 verified_feasible 或 verified_infeasible
        ↓
删除 infeasible 样本 worktree
```

### 4.2 本地与服务器分工

本地机器负责：

```text
metadata 管理
repo cache 下载
patch cache 下载
状态表维护
batch 调度
结果汇总
文档记录
```

服务器负责：

```text
worktree 准备
checkout
Maven smoke validation
测试日志保存
可用样本 worktree 保留
不可用样本 worktree 删除
```

### 4.3 实验环境

```text
local OS: macOS
remote alias: <remote-host>
remote project dir: $PROJECT_DIR
remote Java: OpenJDK 8
remote Maven: tools/apache-maven-3.8.8/bin/mvn
clone timeout: 1800s
validation timeout: 300s
download workers: 1
validate workers: 1
batch size: 50
```

使用 `validate-workers=1` 是为了避免同一仓库多样本并发验证时产生 rsync/worktree 竞争。此前短暂尝试 `validate-workers=2` 时观察到同 repo 并发样本可能出现 pack/temp 文件竞争，因此正式全量筛选采用单验证 worker。

### 4.4 核心命令

初始化 full state：

```bash
python3 -u scripts/clone_validate_pipeline.py \
  --metadata data/metadata/idoft_candidate_fixed_pr_single_label.csv \
  --state-json runs_remote/pipeline_state_idoft_full.json \
  --events-jsonl runs_remote/pipeline_events_idoft_full.jsonl \
  --init-only
```

生成 50 条一组的 batch manifest：

```bash
python3 scripts/make_idoft_batches.py \
  --metadata data/metadata/idoft_candidate_fixed_pr_single_label.csv \
  --state-json runs_remote/pipeline_state_idoft_full.json \
  --output-dir data/metadata/batches/idoft_full \
  --manifest-json data/metadata/batches/idoft_full_manifest.json \
  --batch-size 50 \
  --prefix idoft_full_batch
```

连续运行 batch：

```bash
python3 -u scripts/run_idoft_batches.py \
  --manifest-json data/metadata/batches/idoft_full_manifest.json \
  --state-json runs_remote/pipeline_state_idoft_full.json \
  --events-jsonl runs_remote/pipeline_events_idoft_full.jsonl \
  --repo-cache-dir data/local_repo_cache/idoft \
  --patch-cache-dir data/local_patch_cache/idoft \
  --local-worktree-dir data/worktrees/idoft \
  --remote <remote-host> \
  --remote-dir '$PROJECT_DIR' \
  --mvn tools/apache-maven-3.8.8/bin/mvn \
  --clone-timeout 1800 \
  --validation-timeout 300 \
  --download-workers 1 \
  --validate-workers 1 \
  --poll-seconds 10 \
  --delete-repo-cache-if-no-feasible
```

生成最终汇总 CSV：

```bash
python3 scripts/summarize_pipeline_state.py \
  --state-json runs_remote/pipeline_state_idoft_full.json \
  --output-csv runs_remote/pipeline_state_idoft_full.csv
```

## 5. 状态与失败类型定义

### 5.1 样本状态

最终状态只保留两类：

```text
verified_feasible
verified_infeasible
```

历史中间状态包括：

```text
not_downloaded
downloaded_unverified
```

这两个中间状态只用于断点续跑和流水线调度。最终全量筛选结束后，8 条残留中间状态样本被手动归类为 `verified_infeasible / unknown_failure`，因此最终没有 open residual。

### 5.2 主要失败类型

```text
dependency_resolution:
  Maven 依赖解析失败、远程依赖不可得、仓库/POM 历史状态无法在当前环境解析。

checkout_failed:
  IDoFT 中记录的 SHA 或相关 Git 操作无法完成。

prepare_failed:
  checkout 后 module path 不存在、测试文件无法定位，或准备阶段缺少必要文件。

compilation_failure:
  Maven 能启动，但测试编译或项目编译失败。

test_selector_or_no_tests:
  Maven 测试选择器无法匹配目标测试，或没有测试被执行。

test_failure_or_error:
  目标测试在 smoke run 中确定性失败或报错。

timeout:
  validation 在 300 秒预算内没有完成。

dependency_private_or_auth:
  依赖需要私有仓库、认证或不可公开访问资源。

download_timeout_heavy_repo:
  仓库过大或下载长期阻塞，例如 languagetool-org/languagetool。

unknown_failure:
  当前分类器无法细分的失败，或最终手动归类的残留基础设施样本。
```

## 6. 最终结果

### 6.1 总体结果

```text
total samples:          2349
verified_feasible:      722  (30.7%)
verified_infeasible:    1627 (69.3%)
open residual:          0
unique repositories:    262
repos with feasible:    85
repos only infeasible:  177
```

该结果说明：在 IDoFT fixed PR single-label metadata 中，约三成样本在当前环境下具备后续执行式 LLM 修复实验的基本条件。约七成样本因历史依赖、仓库演化、模块迁移、测试选择器失效或基础设施问题无法直接用于执行式实验。

### 6.2 可用样本类别分布

| Category | Total | Feasible | Infeasible | Feasible Rate |
|---|---:|---:|---:|---:|
| ID | 1788 | 456 | 1332 | 25.5% |
| NIO | 143 | 119 | 24 | 83.2% |
| NOD | 27 | 1 | 26 | 3.7% |
| OD | 95 | 32 | 63 | 33.7% |
| OD-Brit | 10 | 0 | 10 | 0.0% |
| OD-Vic | 230 | 114 | 116 | 49.6% |
| TZD | 4 | 0 | 4 | 0.0% |
| UD | 3 | 0 | 3 | 0.0% |
| NA | 49 | 0 | 49 | 0.0% |

可用子集并不类别均衡。后续正式实验不能直接无控制地使用 722 条全量样本，否则结果会被 ID 和少数高占比仓库主导。更合适的做法是将 722 条作为候选池，再按 category 和 repo 做分层抽样。

### 6.3 可用样本最多的仓库

| Repository | Feasible Samples |
|---|---:|
| pholser/junit-quickcheck | 119 |
| j256/ormlite-core | 92 |
| google/TestParameterInjector | 51 |
| Adyen/adyen-java-api-library | 45 |
| alibaba/innodb-java-reader | 45 |
| alibaba/fastjson | 35 |
| alibaba/fastjson2 | 30 |
| apache/commons-collections | 22 |
| apache/commons-lang | 22 |
| okta/okta-hooks-sdk-java | 19 |
| castle/castle-java | 13 |
| vojtechhabarta/typescript-generator | 13 |
| ktuukkan/marine-api | 12 |
| apache/druid | 11 |
| apache/shardingsphere-elasticjob | 10 |
| dromara/hutool | 10 |
| OpenFeign/feign | 9 |
| abel533/Mapper | 9 |
| SAP/pair-distribution-app | 7 |
| stleary/JSON-java | 7 |

该分布提示两个后续设计要求：

1. 主实验应设置 repo-level cap，例如每个 repo 最多 5 或 10 条。
2. 如果做全量报告，需要同时报告 repo-weighted 和 sample-weighted 结果，避免少数仓库支配结论。

### 6.4 不可用样本最多的仓库

| Repository | Infeasible Samples |
|---|---:|
| apache/dubbo | 164 |
| wildfly/wildfly | 116 |
| apache/incubator-kie-drools | 105 |
| apache/tinkerpop | 57 |
| apache/ignite-3 | 49 |
| apache/activemq | 46 |
| apache/pulsar | 43 |
| apache/nifi | 42 |
| apache/servicecomb-java-chassis | 29 |
| FasterXML/jackson-databind | 27 |
| apache/hadoop | 26 |
| mybatis/mybatis-dynamic-sql | 26 |
| mock-server/mockserver | 23 |
| alibaba/fastjson2 | 22 |
| apache/incubator-seata | 22 |
| apache/pinot | 20 |
| apache/shenyu | 20 |
| flowable/flowable-engine | 17 |
| FasterXML/jackson-dataformats-binary | 16 |
| Wikidata-Toolkit/Wikidata-Toolkit | 16 |

这些失败不应被解释为项目质量差或 flaky test 修复失败。它们主要反映历史 Java/Maven 生态下的可执行性衰减：依赖消失、模块重构、测试选择器变化、私有依赖、JDK/Maven 版本不匹配和大型仓库下载成本。

### 6.5 不可用原因分布

| Failure Class | Count | Ratio Among Infeasible |
|---|---:|---:|
| unknown_failure | 655 | 40.3% |
| dependency_resolution | 587 | 36.1% |
| prepare_failed | 133 | 8.2% |
| checkout_failed | 98 | 6.0% |
| compilation_failure | 78 | 4.8% |
| timeout | 22 | 1.4% |
| empty_failure_class | 15 | 0.9% |
| test_selector_or_no_tests | 15 | 0.9% |
| dependency_private_or_auth | 9 | 0.6% |
| test_failure_or_error | 9 | 0.6% |
| download_timeout_heavy_repo | 3 | 0.2% |
| missing_test_resource | 2 | 0.1% |
| unhandled_exception | 1 | 0.1% |

`unknown_failure` 占比较高，说明当前失败分类器仍有改进空间。后续如果将该数据集构建作为论文贡献之一，可以进一步细化 Maven 日志分类，把 `unknown_failure` 拆分为更具体的依赖、插件、JDK、测试框架或执行环境问题。

## 7. 可复现产物

本次筛选产生或依赖以下核心文件：

```text
docs/experiment_log.md
  中文实验日志，记录每轮数据操作、命令、异常和结果。

docs/idoft_executable_subset_report.md
  本报告，用于解释数据集构建贡献和最终筛选结果。

data/metadata/idoft_candidate_fixed_pr_single_label.csv
  全量输入 metadata，2349 条。

data/metadata/batches/idoft_full_manifest.json
  50 条一组的 batch manifest。

data/metadata/batches/idoft_full/
  每个 batch 的输入 CSV。

runs_remote/pipeline_state_idoft_full.json
  最关键的状态表，记录每条样本的最终状态、失败原因、缓存路径、validation 记录等。

runs_remote/pipeline_state_idoft_full.csv
  从 JSON state 导出的扁平汇总表，适合后续统计和人工检查。

runs_remote/pipeline_events_idoft_full.jsonl
  append-only 事件日志，记录下载、验证、失败和手动归类事件。

data/local_repo_cache/idoft/
  本地 GitHub repo cache。

data/local_patch_cache/idoft/
  本地 PR patch cache。

remote: $PROJECT_DIR/data/worktrees/idoft/
  服务器端样本 worktree。可用样本保留，不可用样本已删除。
```

当前存储占用：

```text
local repo cache:       11G
local patch cache:      34M
remote worktrees:       723 directories
remote worktree size:   60G
remote repo cache:      11G
```

## 8. 后续实验使用建议

### 8.1 主候选池

后续 StabilityOps DSL 的 execution-level 实验应从以下条件筛选：

```text
status == verified_feasible
```

即 722 条样本作为主候选池。

### 8.2 分层抽样策略

建议不要直接把 722 条全部作为第一轮主实验。推荐分三层：

```text
Pilot:
  每个有足够样本的类别抽 10 条。
  每个 repo 最多 2 条。
  目标是验证 prompt、agent 输入包、patch 应用、rerun 成本。

Main Balanced:
  按类别分层。
  每个 repo 最多 5 条或 10 条。
  ID 类样本下采样，NIO/OD/OD-Vic 尽量保留。

Full Candidate Analysis:
  在预算允许时使用全部 722 条。
  必须同时报告 sample-weighted 和 repo-weighted 结果。
```

### 8.3 建议的正式实验子集构建原则

```text
1. 优先覆盖更多 repo，而不是最大化样本数。
2. 每个类别至少保留一个最小规模，避免只报告 ID。
3. 单仓库样本过多时设置 cap。
4. 对 OD-Vic、NIO 等较有代表性的类别尽量保留。
5. NOD 只有 1 条可用样本，正式修复实验排除该类别，只在数据筛选报告中说明排除原因。
6. developer patch 只用于评价，不进入 repair prompt。
7. IDoFT category 可用于 oracle 分析或 category-guided baseline，不应泄漏给 Direct LLM Repair。
```

### 8.4 可作为论文贡献的表述

可以写成：

```text
To support execution-based LLM repair experiments, we construct an execution-ready subset from IDoFT PR-linked flaky tests. Starting from 2,349 single-label Java/Maven samples, our pipeline checks repository availability, commit checkout, module and test-file localization, patch availability, and target-test smoke execution. The resulting subset contains 721 executable repair candidates from 85 repositories after excluding the single NOD sample from formal evaluation, while 1,628 samples are excluded or set aside with logged reasons. This subset reduces repeated infrastructure effort for future flaky-test repair studies and enables controlled comparisons under a shared buildability protocol.
```

中文版本：

```text
为了支持执行式 LLM flaky-test 修复实验，我们从 IDoFT 中构建了一个 execution-ready 子集。该流程从 2349 条 single-label Java/Maven PR-linked flaky tests 出发，依次检查仓库可获取性、commit checkout、module 和测试文件定位、PR patch 可获取性以及目标测试 smoke execution。最终得到 722 条来自 85 个仓库的可执行修复候选样本，并为 1627 条不可用样本记录排除原因。该子集可以减少后续研究在历史仓库构建问题上的重复成本，并为 flaky-test repair 方法提供统一的 buildability protocol。
```

## 9. 威胁与限制

### 9.1 构念有效性

`verified_feasible` 只代表基本可执行性，不代表 flaky 可复现性。后续 repair success 仍需要单独定义，例如编译通过、目标测试通过和 post-fix rerun 未观察到失败。

### 9.2 内部有效性

失败分类依赖当前日志分类器，`unknown_failure` 占比较高。该类别中可能混合了依赖、插件、JDK、测试框架、网络和脚本适配问题。若将数据集构建作为主要贡献之一，建议进一步细分该类别。

### 9.3 外部有效性

筛选环境固定为 Java 8 和 Maven 3.8.8。某些样本可能在其他 JDK、Maven 或系统依赖配置下可执行。因此，`verified_infeasible` 应理解为“在本协议和环境下不可用”，不是永久不可用。

### 9.4 结论有效性

可用子集存在类别和仓库分布偏斜。特别是 `pholser/junit-quickcheck`、`j256/ormlite-core` 等仓库贡献了大量可用样本。正式修复实验需要 repo-level cap 或 repo-weighted 统计，避免少数仓库支配结果。

### 9.5 可复现性

GitHub 仓库、PR patch、Maven 依赖和远端仓库都可能随时间变化。本报告记录了当前状态表、事件日志和缓存路径。后续开源时应尽量提供：

```text
1. pipeline scripts
2. full state JSON
3. summary CSV
4. batch manifest
5. failure classification rules
6. environment description
7. optional cached patch files
```

对于完整 repo cache 和 worktree，由于体积较大，建议只提供重建脚本和校验清单，而不是直接打包全部仓库。

## 10. 下一步

已完成的下一步数据产物：

```text
data/metadata/idoft_verified_feasible.csv
data/metadata/idoft_verified_feasible.jsonl
data/metadata/idoft_verified_feasible_summary.json
```

这三个文件将原始 IDoFT metadata 与 pipeline validation state 合并，保留后续修复实验需要的关键字段：

```text
sample_id
repo_slug
PrimaryCategory / Category
Project URL
SHA Detected
Module Path
fully-qualified test name
PR Link
test_class / test_method / maven_test_selector
remote_repo_dir / remote_module_dir
validation command / validation log path / validation elapsed time
patch cache path
patch_touches_test
```

已生成的默认子集：

```text
data/metadata/subsets/idoft_verified_feasible_repo_cap5.csv
data/metadata/subsets/idoft_verified_feasible_repo_cap10.csv
data/metadata/subsets/idoft_verified_feasible_pilot_10_each_repo2.csv
data/metadata/subsets/idoft_verified_feasible_balanced_10_each.csv
data/metadata/subsets/idoft_verified_feasible_balanced_30_each_repo10_per_category.csv
```

子集规模：

```text
all feasible:             722 samples, 85 repos
repo_cap5:                235 samples, 85 repos
repo_cap10:               333 samples, 85 repos
strict pilot repo2:        33 samples, 21 repos
balanced_10_each:          41 samples, 9 repos
balanced_30_each_repo10:  105 samples, 23 repos
```

子集解释：

```text
repo_cap5 / repo_cap10:
  用于控制单仓库支配问题，适合报告 repo-capped 全量趋势。

strict pilot repo2:
  每个 repo 最多 2 条，更适合调试 agent 流程，但由于 NIO 几乎集中在 pholser/junit-quickcheck，NIO 只能保留 2 条。

balanced_10_each:
  每个类别最多 10 条，不设 repo cap，适合快速验证 prompt 和 agent 行为。它牺牲 repo 多样性以保证类别覆盖。

balanced_30_each_repo10:
  每个类别最多 30 条，且每个类别内同一 repo 最多 10 条。建议作为第一轮主实验候选。
```

后续仍建议开展以下工作：

1. 为每条可用样本构造 agent 输入包，包含 test code、module path、目标测试选择器、build log 和必要上下文。
2. 明确哪些字段属于 prompt input，哪些字段只允许用于 evaluation。
3. 对 `unknown_failure` 抽样复核，判断是否需要进一步细化失败分类。
4. 在 `balanced_10_each` 上跑端到端 pilot，确认 agent prompt、patch 应用和 validation 预算。
5. pilot 稳定后，在 `balanced_30_each_repo10` 上做第一轮主实验。
