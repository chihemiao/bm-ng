# Goal: HL Funding Carry V1 — Testnet

**状态**：GATE_0_COMPLETE / PURE_EXECUTION_LEARNING / ZERO_LIVE_AUTHORITY / NO_MAINNET_KEY
**合法终态（二选一）**：
- `HL_T0_TESTNET_V1_CONFIRMED` — 执行层在 testnet 上通过全部故障注入
- `HL_T0_NO_GO_COMPLETE` — 经济性或资格不成立，带证据停止

**本 Goal 不包含 mainnet 上线。** 真实资金是下一个 Goal 的事，前置条件是本 Goal 已确认。

---

## 0. 元规则（优先级高于以下全部内容）

1. **证据是数据，不是代码。** 任何 attempt / retry / stage / receipt / 验证结果一律写入 append-only 事件流。修复 = 改现有文件，不新增文件。
2. **门禁必须机器可执行。** 写在文档里而 CI 不检查的规则，等于不存在。本文档自身的行数也由 CI 检查。
3. **上下文预算是第一性指标。** 整个运行闭包必须能一次性装进一个 context window。上个项目真正的死因不是行数超标，是代码库大到读不动之后迭代速度崩溃，于是更倾向新建文件绕过去。

本 Goal 有 6 个 Gate。**想加第 7 个 Gate 时，先删掉一个。**

---

## 1. 范围与前置条件

**做什么**：Hyperliquid BTC/ETH 永续 + 一个冻结的对冲腿，delta 中性资金费 carry，**仅在 testnet 上执行**。

**不做什么**（不在本 Goal 内讨论）：
mainnet 下单 / 任何真实资金 / Tier 1 做市 / Tier 2 逆选择过滤 / BTC·ETH 以外标的 / 无人值守 / WebUI / 税务导出。

**硬前置条件（Gate 0，未满足则整个 Goal 终止）**：
- 交易所账户为本人名下、实名、符合服务条款的全部资格要求（含年龄）。不成立则 `NO_GO_COMPLETE`。
- 资金费差值的期望在某个可实现持有期下不为显著负。判据见 Gate 0。

**诚实的收益预期**：本 Goal 的产出是执行层与对账能力，**不是收入，也不是可上线的策略**。

---

## 2. 不可变边界

### 2.1 隔离
- 独立仓库、独立运行目录、独立部署身份。
- 不得读取、导入或复用 OKX 项目的凭据、账户文件、运行状态、证据根、服务配置。
- 一个账户只能有一个持开仓租约的 risk-increasing writer；检测到多个持有者立即 fail-closed。
- 额外实例只允许撤单，不得 reduce-only、平仓、市价了结或提交任何新订单；每实例使用独立 testnet 凭据并记录实例 ID。该约束是代码级而非交易所权限级，凭据级 cancel-only 不可得会单独阻止未来 mainnet Goal。
- kill switch 的平仓只能由主执行器提交；主执行器失效时 watchdog 只能撤单。主执行器必须把经对账确认的外部撤单视为正常状态。

### 2.2 凭据
- **仓库内、主机上、环境变量里不得存在任何 mainnet 私钥或 API key。** CI 扫描强制。
- testnet 使用独立 agent wallet；每个下单进程一个独立 agent wallet（HL 的 nonce 属于 signer，共用 key 会 nonce 冲突）。
- agent wallet 有效期 30 天，到期前 7 天告警并轮换。轮换必须是有意的、单步的、记录在案的。
- 凭据不得进入日志、数据库、事件流、Git 或任何证据文件。

### 2.3 执行
- 双腿建仓必须在同一个资金费小时窗口内完成，否则 fail-closed 退出。
- HL 最小下单名义 10 USD，价格须符合 5 位有效数字规则——下单前本地校验，不依赖交易所拒单。
- **Native order modify 关闭。** 改价必须：撤旧单 → 对账旧单终态 → 用新 client order ID 下新单。
- 模糊下单结果**禁止直接重试**。必须先用同一 client order ID 对账订单、成交、仓位。
- **状态未知时不得猜测仓位并自动反向交易。** 分叉规则：
  - 仓位与订单**权威已知** + 超限 → 允许自动减风险到中性
  - 仓位或订单**未知** → 只允许撤单 + 冻结 + 告警，**禁止提交任何新订单**（包括标记为 reduce-only 的）
- 未知订单、未知仓位、数据缺口、时钟异常、依赖 digest 漂移、nonce 异常一律 fail-closed。

### 2.4 事件流约束（"证据是数据"的防膨胀条款）
- 事件 schema 必须版本化，存储 append-only；顶层 kind 冻结为 `market / decision / order / reconciliation / ops`。
- 公共信封含 `schema_ver/event_kind/payload_schema/venue/conn_id/boot_id/recv_wall_ns/recv_mono_ns/source`。runtime 不得按 `source` 分支；无法解析的原始帧以 `ops.raw_quarantine` 原样留证并 fail-closed。
- 订单信封的 client/venue order ID 可空并带 `identity_status`；请求事件强制 client order ID 非空，未知身份不得被 schema 丢弃，必须进入 reconciliation，逾时未解决则冻结。
- 新增事件类型必须同时提供从旧 schema 的重放兼容性测试。
- 事件类型总数 ≤ 20（CI 计数）。
- 任一历史时间窗口必须可确定性重放。

### 2.5 反膨胀（CI 硬门禁，违反即 fail，无人工豁免）
作用域：RUNTIME=`data/execution/strategy/risk/reconciliation/ops`；TESTS=`tests`；RESEARCH=`research/`（无 `__init__.py`，≤10 文件）。Gate 0 两份根目录证据为 SEALED，不计限额且不得修改。

| 检查 | 阈值 | 作用域 |
|---|---|---|
| 运行闭包总行数 / 文件数 | ≤8,000 / ≤40 | RUNTIME |
| fault harness 行数 | ≤600 | `tests/harness/` |
| markdown / 事件类型 / 顶层包 | ≤8 / ≤20 / ≤7 | 全仓 / RUNTIME / 原七目录 |
| 单文件 / 单函数 / 圈复杂度 | ≤400 / ≤60 / ≤10 | RUNTIME+TESTS+RESEARCH 的 `.py` |
| 代码重复率 / 死代码 | ≤3% / 0（置信度≥80%） | RUNTIME+TESTS / RUNTIME |
| 直接依赖 / 单 PR 净增 | ≤25 / ≤200 | 全仓 |
| 本文档行数 / 版本化文件名 | ≤300 / 0 | 本文件 / 全仓 |
| mainnet 凭据特征 | 0 | 全仓，含 notebook 输出 |

TESTS 与 RESEARCH 不计运行闭包；PR 净增仍按全仓计。部署 artifact 只允许六个 RUNTIME 包与依赖，出现 TESTS/RESEARCH 即 fail。`tests/` 文件必须为 `test_*.py` 或位于 `tests/harness/`。

版本化文件名正则（直接 fail）：
```
_v[0-9]|_new|_fixed|_final|_copy|_old|_backup|_retry[0-9]|_attempt[0-9]
```

**棘轮规则**：行数、文件数、依赖数三项的阈值只能下调，不能上调。CI 记录历史最低值，超过历史最低值即 fail。

**豁免规则**：单 PR 净增行数每月最多豁免 1 次，豁免次数由 CI 计数器强制，用尽即 fail。其余所有检查**无豁免**。

**分支保护禁止 admin bypass。** 门禁必须是 required check。

**架构边界**（`import-linter` contract，CI 强制）：
- RUNTIME 不得 import TESTS 或 RESEARCH
- `ops` 可 import `execution.cancel`，不得 import `execution.orders`
- `strategy` 不得直接 import 交易所 SDK（必须走 `execution` 抽象）

---

## 3. Gate（6 个）

### Gate 0 — 资格与经济性前置判定
**COMPLETE（2026-08-10）**：人类已确认账户资格；拓扑冻结为 T0A（HL perp ↔ Bybit perp）。
12 个月实际结算资金费在全 maker 6bp 后为 0 项显著正，分类 `near_zero`；本 Goal 仅作为执行层学习继续，不构成策略 GO。
SEALED 证据：`gate0_funding.ipynb` SHA-256 `88cb74296a62776c5f52ab4cfb599705232053a55f5e32ae3019fb8239c91136`；`gate0_funding_raw.jsonl.gz` SHA-256 `4896c59d7884b74083064214581f8169c2af1093e2431fd3c88dd15f4386c4b5`。

---

### Gate 1 — 数据采集（有时间价值，Gate 0 通过后立即启动）
- 公开只读 WS 采集：HL `l2Book/trades/bbo/activeAssetCtx`；Bybit `orderbook/publicTrade/tickers`；BTC/ETH。HL `activeAssetCtx` 与 Bybit `tickers` 记录 public current funding rate（及场所提供时的 next funding time），不使用私有账户流。
- 原始帧到达即打 wall/monotonic 时间并原样落盘，归一化只在重放时做。行情记录 exchange/recv 时间；连接订阅记录 send/ack；订单请求记录 send/ack/terminal；不存在的时间必须为空，不得复制伪造。
- 记录重连、丢包、乱序、schema 变化、时钟漂移。
- append-only 压缩文件 + 每文件 checksum + manifest。
- 缺口检测按场所：Bybit `orderbook.50` 用 `u` 连续性（snapshot 重置），所有连接用 ping/pong；HL 无 sequence，只有 ping/pong、订阅 ACK（重连后须重确认全部 14 条流）与文件完整性，不能证明单流仍在投递，证据弱且须披露。行情到达间隔只作 `(venue,channel,symbol)` 软告警，不参与“无未解释缺口”判定。
- explained gap 单次 ≤4h、窗口累计 ≤2%、可用于延迟统计的小时覆盖率 ≥95%；unexplained gap 从恢复后的首个验证连续点重置计数，旧数据保留。
- HL 官方 S3 requester-pays 仅作可选离线基线，不是在线补缺或连续性权威，不创建 AWS profile。

**采集器归属**：进 `data/`，Gate 2 前最多 800 行 / 5 文件，触顶即暂停 Gate 1、先做 Gate 2；Gate 2 完成时必须一并通过全部门禁。

**验收**：连续 7 天在上述可判定证据范围内无未解释缺口且满足覆盖率，可确定性重放任一时间窗口；HL 无法证明单流持续投递的限制及两所证据强度差异已记录。单向延迟仅作含不可分离时钟偏差的描述性指标；到达间隔和同机 RTT 只作质量描述。

---

### Gate 2 — 骨架与门禁
- **在写任何策略或执行代码之前**，先把 §2.5 全部检查装进 pre-commit + CI required check。
- Gate 1 使用原生 `websockets`；执行 SDK、`ccxt`、`polars`/`duckdb` 只在对应 Gate 的失败测试需要时逐项批准，禁止预装。**不使用 NautilusTrader**。
- 冻结依赖精确版本 + SBOM + 漏洞扫描。
- 目录：`data/ execution/ strategy/ risk/ reconciliation/ ops/ tests/`（7 个，即上限）。
- 任何项目 testnet 凭据或 agent wallet 的创建、配置，或首次私有 endpoint 调用，必须在 Gate 2 验收之后；AI 全程不接触凭据。

**验收**：CI 全绿；`foo_v2.py`、假 mainnet key、`ops → execution.orders` 违规 import、TESTS/RESEARCH 混入部署 artifact 均被拒；Gate 1 采集器已纳入门禁并通过。

---

### Gate 3 — 执行与对账安全层
实现并用**故障注入**验证（不是单元测试 mock，是真实注入）：
- 单一 writer 强制（进程锁 + 独立 agent wallet fencing + 启动时检测）
- 策略/版本绑定的 client order ID
- 启动时订单、成交、仓位、余额四项对账
- 部分成交 / 一腿成功一腿失败 / ACK 丢失但实际成交 / 断线期间成交 / 重启恢复
- 过期信号拒绝 / 最大裸露时间与金额
- 未知状态分叉（§2.3）：验证未知状态下系统**不会**自动下单
- USDC/USDT 汇率换算
- nonce 冲突与 agent key 临近过期

**验收**：所有故障注入证明不会因自动重试产生重复仓位。（参考：Hummingbot HL 连接器有停止策略时重复成交的记录 issue #7295——不是假想风险。）

---

### Gate 4 — Kill Switch 与 Dead-man Switch
两者是不同的东西，都必须有：

**Kill switch（主动）**：一条命令，把两腿平到 delta 中性并停止所有下单进程。自动触发条件：
- 两边 USD delta 超限 / 单腿裸露超时或超额
- 数据缺口超过 N 秒 / 时钟异常
- nonce 异常或 agent key 剩余有效期 < 7 天
- 稳定币价差超阈值 / 依赖·配置·adapter digest 漂移
- HL 出现验证者干预型事件（下架、强制结算、oracle 异常）
- 连续 K 次对账不一致 → **进入未知状态分叉，只撤单不平仓**

**Dead-man switch（被动）**：交易所侧 scheduled cancel。心跳停止 → 交易所自动撤单。
**这一条保护的是进程已死、主机断网、你不在电脑前的场景，kill switch 保护不了它。**

**能力矩阵**：HL 必须真实演练 venue-side `scheduleCancel`；Bybit 普通账户无 DCP，必须演练 host-side cancel-only watchdog，并把主机/网络同时故障不受保护记为未缓解风险。

**验收**：可自然发生的条件在 testnet 真实触发；不可自然制造的条件由未打包的 `tests/harness/` 写入合法 `controlled_injection` 事件，走同一 detector→decision→action 链。含注入窗口不得用于延迟/微结构统计。每场所最强可得断线保护均已演练，能力矩阵完整。

---

### Gate 5 — Testnet 双腿闭环与影子运行
顺序：历史回测 → 实时 shadow（不下单，只记录"如果下单会怎样"）→ testnet 双腿闭环 → 故障注入复测。

一个完整闭环 = 双腿建仓 → 订单·成交·仓位对账 → 资金费结算 → 双腿退出 → 最终余额与 PnL 对账。

**不得**把尚未结算的公布资金费率记作已实现收益。

**前置行为观测**：Gate 5 开始前，两所最小 testnet 仓位各跨过一次结算并查询账户级记录。两所都产生真实记录才保持现行闭环定义；任一不产生则停止，由用户选择证据降级或该项 NO-GO，不得预授权降级。

**验收**：≥3 个完整闭环，全部对账一致，零未知订单/仓位。

---

## 4. 完成判定

到达 `HL_T0_TESTNET_V1_CONFIRMED` 需要全部满足：
- Gate 5 的 ≥3 个 testnet 闭环全部对账一致
- Kill switch 每条条件已按真实触发/受控注入规则验证；每场所最强可得断线保护已演练并记录能力矩阵
- 未知状态分叉已验证：未知状态下系统不下新单
- 完成审计前任意连续 30 日历日采集无未解释缺口且满足覆盖率；该窗口与 Gate 2–5 并行
- 反膨胀 CI 全绿；运行闭包 ≤8,000 行且可一次性装入单个 context window
- 仓库与主机上不存在任何 mainnet 私钥或 API key
- 未发生未经授权的范围扩展（标的、场所、Tier、真实资金）

**完成判定里没有任何以"天数"计的持仓运行条件。** 需要半年才能满足的条件会让 Goal 永远进行中，这正是上个项目状态漂移的成因。

---

## 5. 停止规则

任一条出现即停止，不自动恢复：

**资格**：账户资格不满足或发生变化。
**经济性**：Gate 0 冻结的 GO/NO-GO 门槛不达标；最小可执行双腿超过 testnet 可模拟范围。
**数据与模型**：不可解释的数据缺口；未来数据泄漏；依赖或配置 digest 漂移。
**执行**：两个进程同时拥有写权限；外部动作结果不确定且无法权威对账；未知订单/仓位/余额；delta 超限；nonce 异常。
**平台**：HL 验证者投票下架、强制结算或干预市场价格（JELLY 2025-03、POPCAT 2025-11、Fartcoin 2026-04 均有先例；BTC/ETH 概率低但机制存在）；HLP 大额异常损益；条款变更影响自动化交易或澳洲用户资格。
**权限**：任何涉及真实资金、mainnet 私钥、加杠杆、加币种或改策略的情况——一律开新 Goal；两所未同时具备 venue-side DMS 时不得开启任何 mainnet Goal。

---

## 6. 本文档的维护规则

- 本文档 ≤300 行，由 CI 检查。想加内容时，先问能不能删掉等量的旧内容。
- 每次发现新的失败模式，加一条**规则**，不加一个 Gate。
- **禁止新建 `GOAL_v2.md` 之类的文件。** 修订直接改本文件，历史由 git 保管。
- 任何"为了满足本文档某条要求而新增源码文件"的冲动，先检查能不能改现有文件。
- 若六个月后本文档变成 800 行、15 个 Gate，说明上个项目的失败模式已复现，正确动作是 `git checkout` 回到这个版本重来。
