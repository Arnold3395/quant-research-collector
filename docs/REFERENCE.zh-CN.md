# 运行参考手册（中文）

[English overview](../README.md) · [中文首页](../README.zh-CN.md)

一个面向个人研究归档的文献采集项目：从公开 API、RSS 和静态研究目录发现量化交易、金融工程相关论文与研究报告，按确定性规则筛选，增量保存题录、来源摘要、版本记录及可获取的 PDF。

**Python 3.11+ · 仅标准库 · 无 AI 调用 · 无付费 API 依赖 · 本地 SQLite · 可手动或定时运行**

目前配置了 **10 个平台、12 条默认采集流、6 类适配器**，另有 3 条默认关闭的可选流。项目不会自动安装或启用定时任务。

文档更新：2026-10-17。新增候选审计、独立下载、历史任务、异常隔离、指标和全库检索导出，使用方式见 [PIPELINE.md](../PIPELINE.md)。文中的渠道可用性和示例库数量来自 2026-10-16 的实测，外部入口可能随时间变化；实际配置和本次运行结果分别以 `sources`、`last_run.json` 为准。

## 目录

- [项目定位与功能](#项目定位与功能)
- [环境与快速开始](#环境与快速开始)
- [信息来源与获取边界](#信息来源与获取边界)
- [项目结构与数据输出](#项目结构与数据输出)
- [命令与参数](#命令与参数)
- [配置参考](#配置参考)
- [增量采集与分页恢复](#增量采集与分页恢复)
- [去重与全文归档](#去重与全文归档)
- [状态与退出码](#状态与退出码)
- [日常维护](#日常维护)
- [备份恢复与迁移](#备份恢复与迁移)
- [定时运行](#定时运行)
- [采集管线与全库检索导出](#采集管线与全库检索导出)
- [新增渠道与开发扩展](#新增渠道与开发扩展)
- [网络代理与访问规则](#网络代理与访问规则)
- [故障排查](#故障排查)
- [验证与测试](#验证与测试)
- [范围限制与许可](#范围限制与许可)
- [相关文档与官方参考](#相关文档与官方参考)

## 项目定位与功能

默认研究方向包括因子与资产定价、组合与资产配置、CTA 与趋势、衍生品与波动率、风险管理、市场微观结构与执行、利率与信用，以及统计和机器学习金融方法。

| 能力 | 实际行为 |
|---|---|
| 增量发现 | 来源独立水位；重叠扫描；显式历史补采与普通增量分开管理 |
| 分页续采 | arXiv、Crossref、DataCite、OAI-PMH 保存固定窗口和续页位置 |
| 确定性筛选 | 中英文关键词、标题排除词、分类加分、来源独立阈值 |
| 文献与版本去重 | DOI、arXiv ID、来源标识、URL，以及保守的标题与作者关联 |
| 全文归档 | 公开 PDF 解析、内容哈希命名、文件复用、原子写入 |
| 故障恢复 | 独立来源失败处理、下载重试、限流等待、预算延期、中断恢复 |
| 可观测性 | 来源健康状态、下载事件、运行报告、配置快照、轮转日志 |
| 数据维护 | 完整性检查、损坏文件重新排队、指定重试、离线重新分类 |
| 数据保护 | 进程锁、数据库升级前快照、完整 ZIP 备份与校验恢复 |
| 可扩展渠道 | 配置片段加载和适配器注册表；网络与入库逻辑统一复用 |

程序不会生成 AI 摘要或语义标签。摘要来自发布方；评分用于筛选，不是论文质量评价。金融机器学习相关论文仍可被收录，研究主题与程序是否调用 AI 是两回事。

## 环境与快速开始

### 运行要求

- Python **3.11 或更高版本**，需要标准库中的 `tomllib`、`sqlite3` 等模块。
- Windows 使用 Windows PowerShell 或 PowerShell 7；Python 核心入口也可用于其他操作系统。本项目的实际验证环境为 Windows、Python 3.12.14。
- 联网采集需要能访问对应公开站点，并具有有效的 TLS 信任链。
- 资料目录需要写权限和足够磁盘空间。PDF、历史报告和完整备份会持续占用空间。

采集程序**没有 pip 安装步骤，也不需要模型密钥**。

### Windows：首次运行

在项目目录打开 PowerShell：

```powershell
cd quant-research-collector

# 确认 Python 版本
python --version

# 可选：创建独立环境，无需激活、无需安装第三方包
python -m venv .venv

# 离线查看配置中的全部来源，包括默认关闭的来源
.\run.ps1 -Command sources

# 抽样检查默认入口、访问规则和解析结果；不创建资料库、不下载论文全文
.\run.ps1 -Command doctor

# 首次建议先采题录，检查范围和匹配结果
.\run.ps1 -MetadataOnly

# 正常采集：补充题录并处理符合条件的 PDF 队列
.\run.ps1

# 用浏览器打开本地目录
Start-Process .\data\index.html
```

`run.ps1` 按以下顺序寻找解释器：项目 `.venv\Scripts\python.exe` → 本机已有的 Codex 附带 Python → `PATH` 中的 `python`。也可显式指定：

```powershell
.\run.ps1 -Python 'C:\Python312\python.exe' -Command sources
```

项目可以独立于 Codex 运行。若 PowerShell 执行策略不允许运行脚本，可直接使用下面的 Python 入口，无需为了采集而修改系统级执行策略。

### Python：通用入口

以下命令中的 `python` 应指向 Python 3.11+；Linux/macOS 可按安装情况换成 `python3`。

```bash
python collector.py --help
python collector.py sources
python collector.py doctor
python collector.py run --metadata-only
python collector.py run --max-downloads 5
python collector.py report --days 30
```

在 Windows 虚拟环境中，直接入口对应 `.\.venv\Scripts\python.exe collector.py ...`。

### 常用采集方式

```powershell
# 只处理指定来源；逗号分隔的来源 ID
.\run.ps1 -Sources arxiv_quant,bis_working_papers,econstor_open_research

# 单次回看90天，最多处理10个PDF获取任务
.\run.ps1 -Days 90 -MaxDownloads 10

# 使用独立试验库，避免与正式库混合
.\run.ps1 -MetadataOnly -DataDir '.\tmp\trial-library'

# 自定义完整配置；TOML和展开后的JSON配置均可
.\run.ps1 -Config '.\config.toml' -Command sources
```

不同 `DataDir` 是彼此独立的资料库，不共享去重和下载状态。`-Days 90` 也不能使一个只保留近期条目的 RSS 自动提供 90 天历史。

## 信息来源与获取边界

| 来源 ID | 内容与协议 | 默认状态 | 全文策略 |
|---|---|---|---|
| `arxiv_quant` | arXiv 八个量化金融分类；官方 API | 开启 | 获取公开 PDF |
| `arxiv_finance_methods` | 金融相关 ML/优化方法；arXiv API | 开启 | 获取公开 PDF |
| `bis_working_papers` | BIS 工作论文；RSS 1.0/RDF | 开启 | 解析公开 PDF |
| `fed_feds` | 美联储 FEDS；RSS | 开启 | 解析公开 PDF |
| `fed_ifdp` | 美联储 IFDP；RSS | 开启 | 解析公开 PDF |
| `aqr_public_research` | AQR 公开研究；静态 HTML 目录 | 开启 | 解析公开 PDF |
| `sse_public_research` | 上交所研究报告与特别报告；静态 HTML | 开启 | 解析公开 PDF |
| `cfm_public_research` | CFM 公开研究与文章；RSS | 开启 | 可解析到时下载 |
| `bank_canada_working_papers` | 加拿大央行工作论文；RSS | 开启 | 解析公开 PDF |
| `crossref_quantitative_finance` | Quantitative Finance 期刊 DOI 题录；Crossref API | 开启 | 默认仅题录 |
| `datacite_quant_research` | 金融研究仓储题录；DataCite API | 开启 | 默认仅题录 |
| `econstor_open_research` | EconStor 开放经济金融研究；OAI-PMH | 开启 | 解析公开 PDF；已实测成功样本 |
| `ecb_working_papers` | ECB 工作论文；RSS | 关闭 | 初测存在本机 TLS 证书链问题 |
| `rba_research_papers` | 澳洲联储研究论文；RSS | 关闭 | 初测存在本机 TLS 证书链问题 |
| `snb_working_papers` | 瑞士央行研究；RSS | 关闭 | 初测存在本机 TLS 证书链问题 |

这里的“开启”表示默认参与采集，不保证每次都有新论文或每条记录都有 PDF。Crossref 当前只检索配置中的指定期刊；DataCite 使用标题查询与文献类型过滤。二者不是全平台镜像。

SSRN、微信公众号、券商聚合站、付费期刊登录系统和需要验证码的网站尚未接入。HAL、OpenAlex 等候选的探索结果见 [EXTENDING.md](../EXTENDING.md)，不能计为已支持来源。

**免费题录、免费阅读、可自动下载和允许重新发布全文是不同条件。** Crossref 的出版商链接或许可字段不会自动转化为全文获取权限。

## 项目结构与数据输出

```text
quant-research-collector/
  collector.py                  # CLI、规则、HTTP访问、资料库、下载与导出
  adapters.py                   # 六类协议适配器与Harvest返回契约
  maintenance.py                # 配置校验、健康检查、重试、备份恢复
  pipeline.py                   # 候选审计、调度、历史任务、隔离、指标、检索和引用导出
  config.toml                   # 全局配置、筛选词表、原有来源
  sources.d/public_apis.toml     # 新增API与可选RSS配置片段
  run.ps1                       # Windows统一入口
  install_schedule.ps1          # 显式安装/删除Windows计划任务
  tests/                        # 离线单元测试
  tools/                        # 审计、维护演练与开发探测工具
  PIPELINE.md                   # 新增管线功能、使用方式与边界
  EXTENDING.md                  # 渠道扩展与访问边界
  VALIDATION.md                 # 历史实测记录
  data/                         # 默认资料库，运行后生成
  tmp/                          # 测试和演练输出
```

默认资料目录：

| 文件或目录 | 用途 |
|---|---|
| `library.sqlite3` | 权威题录、标识、版本、队列、水位、检查点、事件和运行历史 |
| `catalog.jsonl` | 全部来源记录/版本的导出，一行一条 JSON，包含嵌套 `metadata` |
| `index.html` | 本地可搜索目录，默认展示最近 7 天首次发现的记录 |
| `latest.md` | 同一发现窗口的 Markdown 目录与原始摘要 |
| `last_run.json` | 最近一次采集的汇总快照 |
| `reports/` | 每次采集的历史 Markdown 报告 |
| `pdf/<前两位哈希>/<SHA-256>.pdf` | 内容寻址的 PDF 文件 |
| `collector.log`、`collector.log.1` 等 | 当前日志与轮转历史 |
| `http_cache/` | RSS/HTML 最近响应及条件请求校验信息 |
| `config_history/` | 按配置哈希保存的展开配置快照 |
| `maintenance_history/` | 重试、重新分类及修复操作记录 |
| `backups/` | 升级前数据库快照，以及完整备份的默认输出位置 |
| `.collector.lock` | 进程锁文件；文件存在不代表仍有进程运行 |

`index.html` 的标题和摘要搜索不请求外部服务，只搜索当前页面中的记录，不解析 PDF 正文。点击原文链接仍会打开外部站点。

`report -Days 30` 中的 30 天是**首次发现时间**窗口，不是发表时间窗口。重新生成目录不会删除数据库中的旧记录；完整历史保存在 SQLite 和 JSONL 中。

### 文献、记录和文件的区别

- **文献**：合并后的研究成果，对应 `documents.id`。
- **记录/版本**：某来源中观察到的一份元数据状态，对应 `records.id`；同一文献可有多条。
- **PDF 文件**：实际获取的文件；相同字节内容共享同一个哈希路径。

因此，文献数、记录数与 PDF 数不会相等。`retry -RecordId` 使用的是来源记录 ID，而不是文献 ID。

### 主要数据表与字段

| 表 | 作用 |
|---|---|
| `documents`、`identifiers` | 文献身份与 DOI、arXiv、URL、来源标识映射 |
| `records` | 版本指纹、元数据 JSON、首次/最近发现时间、下载状态和路径 |
| `source_state` | 来源最近成功扫描水位 |
| `source_health` | 来源配置哈希、最近状态、失败次数和下次重试时间 |
| `harvest_checkpoints` | 来源及运行模式对应的固定窗口与分页游标 |
| `source_notices` | 仓储删除通知的历史记录 |
| `download_events` | 每次 PDF 获取或复用的结果 |
| `runs` | 每次采集的开始、结束、状态和汇总 |
| `candidates`、`raw_responses` | 有界候选审计与可选压缩响应 |
| `item_errors`、`error_tasks` | 异常条目、重放状态与关联历史任务 |
| `backfill_tasks`、`pipeline_state` | 固定日期补采任务、来源轮转位置 |
| `source_metrics`、`checkpoint_tracking` | 来源趋势与分页进度年龄 |

`records.metadata` 包含题名、作者、原始摘要、发表/更新时间、原始链接、DOI、arXiv ID、版本、类别、主题、匹配词、分数、许可字段、材料类型及来源删除标记。字段缺失会保留为空，不补造发表日期。存储时间和运行汇总使用 UTC；Windows 计划任务使用系统本地时区。

## 命令与参数

### 命令总览

| PowerShell `-Command` / Python 子命令 | 联网 | 主要行为 |
|---|---|---|
| `run` | 是 | 发现、筛选、入库，按预算处理全文队列并导出；PowerShell默认命令 |
| `doctor` | 是 | 抽样检查来源连接与解析；不推进正式水位 |
| `sources` | 否 | 列出来源、启用状态、覆盖边界与增量依据 |
| `report` | 否 | 重新生成目录，不覆盖历史采集状态和报告 |
| `status` | 否 | 检查来源健康、配置变化、检查点和重试耗尽情况 |
| `verify` | 否 | 检查 SQLite、外键、已下载 PDF 文件头与哈希 |
| `retry` | 否 | 将指定范围的未下载记录重置为可重试状态 |
| `reclassify` | 否 | 对已有题录重算当前词表标签与分数 |
| `backup` | 否 | 创建包含数据库与已归档全文的完整 ZIP |
| `restore` | 否 | 校验备份并恢复到不存在的新目录 |
| `download` | 是 | 独立处理已有PDF队列，不扫描题录 |
| `backfill` | 是 | 创建/续跑固定日期分段任务，仅采题录 |
| `jobs` | 否 | 查看历史任务和完成/受限/待审核状态 |
| `candidates` / `rescreen` | 否 | 查看候选；重新筛选并恢复当前通过的材料 |
| `quarantine` / `replay` / `resolve` | 否 | 查看异常、离线重放；或记录明确接受的未恢复缺口 |
| `metrics` | 否 | 来源趋势、积压、隔离与存储指标 |
| `search` | 否 | 全库题录筛选与分页，每篇默认一条匹配记录 |
| `export` | 否 | 按筛选条件导出JSON、CSV、BibTeX、RIS |

不联网不等于完全只读：修复、重试、重新分类和导出会写文件；打开旧版资料库也可能触发数据库升级。`status` 等维护命令需要已存在的资料库；`report` 可建立空库。

### 参数对照

| PowerShell 参数 | Python 参数 | 含义与适用范围 |
|---|---|---|
| `-Command run` | `run` | 选择命令 |
| `-Python <路径>` | 直接选择解释器 | 覆盖PowerShell自动寻找的Python |
| `-Config <路径>` | `--config <路径>` | 完整TOML或展开JSON配置；默认项目的 `config.toml` |
| `-DataDir <目录>` | `--data-dir <目录>` | 指定独立资料库；相对路径按当前工作目录解析 |
| `-Sources a,b` | `--sources a,b` | 来源ID列表；显式指定可选择默认关闭的来源 |
| `-Days 90` | `--days 90` | `run`回看天数；`report`首次发现窗口 |
| `-MetadataOnly` | `--metadata-only` | 本次 `run`不处理PDF队列 |
| `-MaxDownloads 5` | `--max-downloads 5` | 本次 `run`最多处理的PDF任务数；0表示不处理 |
| `-ForceSources` | `--force-sources` | 本次 `run`绕过来源故障冷却 |
| `-RecordId 12,15` | `--record-id 12 --record-id 15` | `retry`限定来源记录ID；可重复指定 |
| `-Repair` | `--repair` | `verify`将损坏或缺失全文重新排队 |
| `-Output <新文件>` | `--output <新文件>` | `backup/export`输出位置；不能覆盖已有文件 |
| `-Archive <文件.zip>` | `--archive <文件.zip>` | `restore`输入备份，必须同时指定新 `DataDir` |

PowerShell 中 `Days=0`、`MaxDownloads=-1` 是“未覆盖默认值”的入口约定；Python 直接入口要求 `--days` 为正数、`--max-downloads` 非负。PDF任务数包括解析、复用、未找到全文和失败尝试，不保证等于新增文件数。

网络采集与原有维护命令默认仅选择 `enabled=true` 的来源；`sources` 展示全部配置。新增全库检索、候选和隔离项读取默认包含关闭来源的历史记录；`metrics` 默认检查开启来源。显式 `-Sources` 会参与选择默认关闭来源，使用前请先检查其关闭原因。

## 配置参考

### 路径与配置文件

`config.toml` 包含 `[collector]`、`[filter]`、`[filter.topics]`、`[[sources]]`，也会加载 `source_files` 指定的片段。

- `[collector].data_dir` 相对于所使用的配置文件目录解析。
- `source_files` 相对于配置目录解析，只允许目录内的相对路径或glob；片段只允许 `[[sources]]`。
- CLI的 `DataDir`、`Output`、`Archive` 相对路径按当前工作目录解析。
- `ca_file` 建议使用绝对路径，避免工作目录变化影响证书加载。
- 完整备份内的 `config.expanded.json` 已合并所有片段，可以独立使用。

不要仅复制主配置而遗漏 `sources.d/`；匹配不到片段会明确报错。相同片段被重叠glob匹配时只加载一次，不同片段内重复来源ID会报错。

### 采集与维护预算

以下为原有采集预算；新增候选留存、阶段配额和关注阈值配置见 [PIPELINE.md](../PIPELINE.md#分阶段预算和公平处理)。CLI只覆盖提供了对应参数的项目：

| `[collector]` 字段 | 当前值 | 说明 |
|---|---|---|
| `data_dir` | `data` | 默认资料目录 |
| `user_agent` | `QuantResearchCollector/1.0 ...` | HTTP客户端标识，完整值见配置 |
| `initial_days` | 14 | 首次及配置变化后的基础回看窗口 |
| `overlap_days` | 3 | 普通增量向前重叠天数 |
| `report_days` | 7 | 采集后导出的首次发现窗口 |
| `request_interval_seconds` | 1.2 | 常规主机请求间隔；arXiv至少3.2秒 |
| `timeout_seconds` | 25 | HTTP单次连接/读取超时，不是整次下载总时长 |
| `http_attempts` | 2 | 一个HTTP请求的最大尝试次数 |
| `max_run_minutes` | 20 | 单次采集网络时长预算 |
| `max_downloads_per_run` | 30 | 单次最多处理的PDF获取任务数 |
| `max_pdf_mb` | 40 | 单个PDF响应大小上限 |
| `max_download_attempts` | 8 | 单条记录的PDF处理尝试上限 |
| `retry_hours` | 24 | PDF失败后的基础重试间隔 |
| `retry_max_hours` | 168 | 指数增长的PDF失败重试间隔上限 |
| `no_pdf_retry_hours` | 168 | 当前没有解析到公开PDF的再次检查间隔 |
| `source_failure_base_minutes` | 30 | 来源失败冷却起点 |
| `source_failure_max_hours` | 24 | 常规来源失败冷却上限 |
| `stale_source_hours` | 72 | 健康检查判定来源长期未成功的阈值 |
| `log_max_mb` | 10 | 单份日志轮转阈值 |
| `log_backups` | 5 | 轮转日志保留份数，另有当前日志 |
| `ca_file` | 未设置 | 可选、已信任的PEM证书文件 |

时长预算在请求和读取过程中检查，不是强制结束整个进程的硬实时计时器；单次阻塞、数据库写入和导出可能使实际耗时超过该值。增加采集预算时也应核对计划任务安装器的30分钟进程上限。

### 筛选规则

`[filter].minimum_score` 默认2，`exclude_terms` 默认空数组，排除词只检查标题；来源内的 `minimum_score` 可覆盖全局阈值。

对每个主题词表：词出现在标题计3分，只在摘要出现计1分，同一词在标题和摘要中同时出现时只取标题分。包含 `q-fin.*` 分类的记录另加3分；标题命中排除词则直接不收录。英文采用大小写归一化及词边界，中文按字面匹配。

评分按词表条目逐主题累加，因此词表内或跨主题重复的词可能重复贡献分数；最终展示的匹配词和主题会去重。调整词表时，应同时检查重复词及误匹配。

修改规则后：

1. 普通采集发现来源/筛选配置哈希变化，重扫基础窗口；如旧水位更早，则保留更早起点。
2. `reclassify`只重算已入库记录；新增 `rescreen`还可从已留存候选中恢复当前通过的材料。此前未保存、或已过期的候选仍需重新发现。
3. 需要更远历史时使用 `-Days` 补采；RSS/HTML仍受原站当前窗口限制。

### 来源配置

通用字段包括 `id`、`name`、`kind`、`url`、`allowed_domains`、`enabled`、`download`、`resolve_pdf`、`minimum_score`、`page_size`、`max_pages`、`notes`。

`download=false` 表示渠道只收题录；`resolve_pdf=false` 仅关闭HTML落地页的PDF搜索，不能替代 `download=false`，因为已有直接PDF链接仍可被使用。

| `kind` | 专用配置 | 增量依据与限制 |
|---|---|---|
| `rss` | 无；使用公开订阅URL | 解析RSS/Atom/RDF，处理当前可见窗口 |
| `html` | `link_pattern`、可选 `encoding` | 静态链接匹配；缺少可靠日期时按首次发现处理 |
| `arxiv` | `query` | `lastUpdatedDate`固定窗口查询与持久化偏移量；分页上限后续采 |
| `crossref` | `issn` 或 `query`，可选 `work_type` | 索引时间过滤，固定窗口和游标 |
| `datacite` | `query`、可选 `resource_types` | 元数据更新时间过滤，固定窗口和游标 |
| `oai_pmh` | `metadata_prefix`、`set_spec`、`date_granularity` | 当前支持 `oai_dc`；粒度为 `seconds` 或 `days` |

详细示例和适配器契约见 [EXTENDING.md](../EXTENDING.md)。

## 增量采集与分页恢复

一次普通运行大致经历以下步骤：

```text
加载和校验配置
  → 取得资料库进程锁
  → 升级数据库、修复中断状态、检查本地PDF
  → 按来源水位或分页检查点取得题录
  → 按规则筛选并幂等入库
  → 保存来源状态、分页进度和删除通知
  → 按预算处理符合条件的PDF队列
  → 导出目录、JSONL和运行报告
```

### 水位、补采与配置变化

- 首次普通运行使用 `initial_days`；后续从最近成功水位向前重叠 `overlap_days`。
- 显式 `-Days` 使用独立补采模式。有旧水位时，不以一次较短补采覆盖普通水位，也不抹去未处理的配置变化；首次成功扫描可初始化水位。
- 同一来源以不同 `-Days` 值运行，会使用不同模式的检查点。续采历史窗口时应继续使用原来的天数参数。
- 来源或筛选配置变化后，不复用旧配置的游标；首次回看窗口或更早的未覆盖区间会重新处理。
- 仅改变 `enabled`、`download` 不视为检索配置变化，不自动触发历史重扫；若需要重新覆盖旧范围，应显式补采。
- 来源失败、预算延期或查询未完整结束时，不将其误标为成功扫描完成。

重叠扫描会重复读取部分题录，以减少时间边界遗漏；它不意味着重复创建文献或重复传输已经确认一致的全文。

### 分页检查点

arXiv、Crossref、DataCite、OAI-PMH 的检查点包括来源、运行模式、配置哈希、固定窗口起止时间和下一页游标。

达到 `max_pages` 后会保存检查点，状态为 `continuing`。此时返回2，水位不推进，连续失败次数不增加，下次同模式继续。完整窗口结束后才清除检查点，并把普通水位推进到**原窗口结束时间**。

普通网络错误保留检查点。OAI明确返回过期令牌时，程序从同一窗口重启一次，依靠入库去重保留既有成果。若令牌有效期短于两次运行间隔，需要缩短窗口或调整单次页数预算；不要通过手工修改水位来掩盖未完成区间。

arXiv现已支持固定窗口和持久化偏移量续采，达到页数上限也会保存检查点。分页中途网络错误或预算耗尽时，会尽可能提交已经完成页面的题录及下一页位置；仍不承诺远端索引的快照一致性。

### 时间字段与来源删除

原始发表时间、元数据更新时间、API索引时间和本地首次发现时间用途不同。Crossref按索引时间发现变化，但保留原始出版/存入时间，不因论文早年发表而丢弃当前索引窗口内的条目。单纯索引时间改变不会制造新的元数据版本。

OAI删除通知写入 `source_notices`；已收录条目标记 `source_deleted` 并停止继续下载，历史题录和已取得的PDF保留。仓储条目删除不等于学术撤稿。

## 去重与全文归档

### 文献与版本

文献身份优先关联 DOI、arXiv ID、规范化URL和来源标识；缺少直接标识时，才保守使用完全匹配的规范化标题和首位作者，并检查已有强标识冲突。

一篇文献来自不同渠道、或元数据发生变化，可以保留多条来源记录。版本指纹涵盖标题、摘要、修订信息、作者、日期、标识、类别、PDF链接、许可字段和材料类型等，不包含本地规则分数。调整标签不会因为评分改变就制造新的文献版本。

身份字段互相冲突时报告错误，不自动合并。没有作者、稳定标识或一致标题的材料仍可能保留重复记录，后续需人工核查。

### PDF获取与复用

- 先使用明确的PDF链接，再按配置从落地页的引用元数据或无歧义链接解析。
- 没有唯一可确认的候选时保留题录，状态为 `no_public_pdf`，不会随意下载页面里的其他文件。
- 同一文献、相同链接且修订信息/元数据满足一致条件时，可复用经哈希检查的本地文件。
- 不同PDF链接的字节内容是否相同，通常仍需获取后才能判断；同一内容最终共享哈希文件。
- 可变链接的元数据已变化、又没有足够修订标记时，不盲目复用旧PDF。
- 检查文件头、响应大小、服务器声明的长度及SHA-256；HTML登录页不会保存为PDF。
- 文件先写入 `.part`，完成后原子替换。下次采集会检查已记录的本地PDF，缺失、损坏或哈希不符时重新排队。

文件头与哈希检查验证的是格式初检和字节一致性，不代表程序已理解全文或证明PDF内容正确。独立页数解析仅属于历史质量验证，不是采集依赖。

## 状态与退出码

### 来源状态

| 状态 | 含义 | 通常的下一步 |
|---|---|---|
| `ok` | 本次选择的来源扫描窗口已完成 | 正常增量运行 |
| `continuing` | 分页预算已用完，检查点已保存 | 同模式再次运行 |
| `truncated` | 查询未完整结束且没有该类续页检查点 | 调整页数或补采范围 |
| `backoff` | 来源仍处于故障/限流等待期 | 等待，或在确认条件改善后显式重试 |
| `deferred_budget` | 本次网络时长预算不足 | 下次继续，不视为来源故障 |
| `failed` | 访问、协议解析等来源级错误 | 查看来源错误与日志 |
| `needs_review` | 扫描结束，但有已保存的异常条目尚未解决 | 查看 `quarantine`，修复后 `replay` |

### PDF记录状态与处理结果

| 状态或结果 | 含义 |
|---|---|
| `pending` | 尚待处理；不保证当前就符合重试条件 |
| `metadata_only` | 来源配置主动只收题录，不是下载失败 |
| `downloaded` | 已登记本地PDF路径与哈希 |
| `no_public_pdf` | 本次解析未找到唯一可确认的公开PDF，不证明不存在免费版本 |
| `download_failed` | 网络、响应、文件或其他获取失败 |
| `policy_blocked` | 域名、robots或其他访问策略阻止处理 |
| `reused_pdf`、`already_downloaded` | 本次处理复用或跳过了已有文件；记录最终仍为 `downloaded` |
| `deferred_budget` | 本次处理延期；记录回到 `pending`，不消耗失败重试次数 |

`pending_downloads` 包括尚未获得全文且不是 `metadata_only` 的记录，也可能包含尚在等待、未解析到PDF、已耗尽重试或被规则暂停的条目，不能直接当成“现在能下载的文件数”。`metadata_only_records` 单独统计渠道仅收题录的记录。

### 退出码与运行汇总

| 退出码 | 含义 |
|---|---|
| 0 | 所选工作在本次预算内正常结束；不代表全部题录均取得全文 |
| 2 | 部分失败、来源冷却、查询截断、分页待续采，或维护检查需要关注 |
| 1 | 配置错误、库不存在或其他致命错误 |
| 130 | 用户中断 |

`verify -Repair` 在发现并重新排队损坏文件后仍返回2，应读其 `errors` 和 `repaired_records`；重新排队并不等于已经重新下载。

`last_run.json` 可查看 `sources`、`downloads`、新增文献/记录数、HTTP请求/字节/304数量、PDF总数、状态分布、配置哈希与运行ID。一次PDF处理因时长预算延期，或只有 `no_public_pdf` 结果时，不一定使整次运行返回2。

`last_run.json` 是最近一次采集的历史快照。之后执行重试、修复或重新分类，实时状态应查看 SQLite、`status` 和重新导出的目录，而不是认为快照中的旧计数已同步改变。

## 日常维护

```powershell
# 当前来源健康与队列状态，不联网
.\run.ps1 -Command status

# 检查数据库及实际PDF字节
.\run.ps1 -Command verify

# 将缺失或损坏PDF重新排队，不立即联网下载
.\run.ps1 -Command verify -Repair

# 重新生成最近30天首次发现的目录
.\run.ps1 -Command report -Days 30

# 修改词表后重新计算已有记录的标签与分数
.\run.ps1 -Command reclassify

# 只重置指定未下载记录的尝试次数和等待状态
.\run.ps1 -Command retry -RecordId 12,15

# 或重置指定来源的未下载记录
.\run.ps1 -Command retry -Sources bis_working_papers

# 来源访问恢复后，绕过来源冷却，仅扫描题录
.\run.ps1 -Sources bis_working_papers -ForceSources -MetadataOnly
```

记录ID示例只用于展示语法，执行前应从本地目录导出或数据库确认对象。`retry` 必须指定记录ID或来源范围；它保留已下载记录，不立即发出网络请求，也不会把渠道的 `download=false` 改成true。

来源失败默认30分钟开始指数冷却，常规上限24小时；PDF失败默认24、48、96小时增长，上限168小时。未找到公开PDF默认7天再检查，记录默认最多8次尝试。服务器 `Retry-After` 可要求更长等待，并会持久化。

普通增量遵守等待时间。`-ForceSources` 和显式 `-Days` 可以跳过**来源**冷却，但不会自动重置PDF等待和尝试上限；PDF需用 `retry` 明确重置。已持久保存的主机 `Retry-After` 和当前请求收到的HTTP限流仍按访问逻辑处理，`ForceSources/Days`不绕过主机等待。

`status` 默认在来源72小时未成功、配置变化未处理、最近采集异常或重试耗尽时返回2。正常待下载队列本身不视为故障。若计划每周运行，可按实际频率调整 `stale_source_hours`，避免72小时阈值产生预期内提示。

日志默认保留当前文件及5份轮转文件。文献、PDF、历史报告、配置快照与完整备份不会自动清理，应定期检查磁盘空间并安排备份存放位置。

## 备份恢复与迁移

### 创建完整备份

```powershell
# 默认输出到当前资料库backups目录，文件名带时间
.\run.ps1 -Command backup

# 输出到自选的新文件；父目录可自动创建，已有文件不会被覆盖
.\run.ps1 -Command backup -Output '.\tmp\library-backup.zip'
```

完整备份包含数据库一致性快照、登记为已下载的PDF、存在的目录导出、运行报告、配置/维护历史、原始入口配置，以及合并了所有片段的 `config.expanded.json`。每个成员都有大小和SHA-256清单。

HTTP缓存和日志不属于恢复必需数据，不打入完整备份；未被数据库登记为已下载的孤立文件也不自动纳入全文集合。完整性检查不通过时会拒绝备份，应先修复资料库。

### 恢复到新目录

```powershell
# 目标目录必须不存在；不覆盖正在使用的资料库
.\run.ps1 -Command restore -Archive '.\tmp\library-backup.zip' -DataDir '.\data-restored'

# 使用恢复出的独立配置，避免依赖原来的sources.d
.\run.ps1 -Config '.\data-restored\config.expanded.json' -DataDir '.\data-restored' -Command sources
.\run.ps1 -Config '.\data-restored\config.expanded.json' -DataDir '.\data-restored' -Command verify

# 检查无误后，在恢复库继续增量采集
.\run.ps1 -Config '.\data-restored\config.expanded.json' -DataDir '.\data-restored' -MetadataOnly
```

恢复会校验ZIP成员路径、文件集合、大小与哈希，再检查恢复库。它不覆盖原资料库，也不会自动切换以后不带参数的运行目标；请持续指定正确的 `Config` 与 `DataDir`。

相对数据目录以配置文件所在目录解析，因此使用恢复后的配置时，应显式指定 `DataDir`，避免把数据写入恢复目录下的另一个 `data/`。

### 数据库升级和跨机器迁移

当前数据库结构版本为 **4**，以 `PRAGMA user_version` 管理。旧库打开时自动升级，升级前保留 `backups/pre-migration-*.sqlite3`。这个自动快照**只有数据库，没有PDF**，不能替代完整备份。旧程序拒绝打开比自己更新的数据库。

跨机器迁移建议携带程序目录和完整ZIP，恢复到新位置，再用展开配置和显式数据目录检查。路径、Python解释器、可信CA和Windows计划任务均需在目标机器重新确认；计划任务不是资料库备份的一部分。

## 定时运行

项目目前只提供安装脚本；手动采集、检查和阅读README都不会启用计划任务。以下安装命令仅供使用者决定启用时执行。

```powershell
# 先预览，不创建任务
.\install_schedule.ps1 -Frequency Daily -At '08:30' -WhatIf

# 每天08:30运行，或选择下面的每周方案，通常二选一
.\install_schedule.ps1 -Frequency Daily -At '08:30'

# 每周一08:30运行
.\install_schedule.ps1 -Frequency Weekly -Day Monday -At '08:30'

# 查看或移除默认任务
Get-ScheduledTask -TaskName QuantResearchCollector
.\install_schedule.ps1 -Remove
```

时间按Windows本地时区解释；系统为China Standard Time时，08:30对应北京时间08:30。安装器显示时区，不修改系统时区。

计划任务使用当前用户的交互登录身份、不保存密码、同名任务不覆盖、重叠执行忽略；错过时间后在可运行时补跑。电脑关机或用户未登录时不能按预期执行。任务直接运行Python，不启动Codex或调用AI。

安装器当前固定使用项目的 `config.toml` 和其中的数据目录，任务进程上限为30分钟，不接收 `run.ps1` 的自定义配置和数据目录参数。若需要另一套计划配置，应自行调整任务动作，不能假定一次手动 `-Config` 会改变已安装任务。

Linux/macOS可通过系统调度器调用Python入口，使用绝对的解释器、脚本和配置路径；本项目未附带该平台的安装脚本。

## 采集管线与全库检索导出

```powershell
# PDF队列独立运行，不依赖题录接口本次成功
.\run.ps1 -Command download -MaxDownloads 10

# 修改词表后，重筛保留候选并同步已有记录标签
.\run.ps1 -Command rescreen

# 全库检索；CSV、BibTeX、RIS均可用相同条件导出
.\run.ps1 -Command search -Query 'volatility' -PdfStatus downloaded
.\run.ps1 -Command export -Format bibtex -Query 'portfolio' -Output '.\tmp\portfolio.bib'

# 查看异常、来源趋势与历史补采任务
.\run.ps1 -Command quarantine
.\run.ps1 -Command metrics
.\run.ps1 -Command jobs
```

候选默认保留90天、逻辑负载100 MiB，完整响应默认不归档。正常运行把默认20分钟网络预算中的最多65%交给发现阶段，下载在来源之间轮转；历史补采支持固定日期、自动分段、断点和受限历史标记。异常项先持久留存，其他材料继续处理，待修复后离线重放。

完整参数、历史任务创建/续跑示例、关注阈值、引用导出和限制见 [PIPELINE.md](../PIPELINE.md)。新增参数包括 `FromDate/UntilDate`、`ChunkDays/MaxTasks`、`JobId`、`ItemId/Reason`、`Query/Author/Topic/Doi/PdfStatus`、`DateField`、`AllVersions`、`Limit/Offset` 和 `Format`；Python入口采用对应的 `--from-date` 等参数。

## 新增渠道与开发扩展

同类渠道优先通过 `sources.d/` 配置片段扩展；新协议通过 `adapters.py` 的注册表实现。六类适配器共享访问策略、`Paper`字段、`Harvest`结果、水位、检查点和资料库存储。

一个适配器只应负责请求与字段映射，网络必须走 `Client`，返回记录和分页进度，不直接修改SQLite或推进水位。新增协议需要测试合法零结果、字段缺失、协议错误、分页结束、重复游标、预算中断和恢复，再进行小规模真实验证。

建议接入步骤：核对官方入口及使用条件 → 单来源 `doctor` → 独立 `DataDir` 试采 → 重复运行验证去重与续采 → 验证全文是否实际可获取 → 再决定默认启用及下载策略。

完整的配置示例、协议契约、已探索渠道和访问限制见 [EXTENDING.md](../EXTENDING.md)。不支持自定义JSON接口的任意字段映射配置，也不会仅凭一个网址就自动适配网站。

## 网络代理与访问规则

所有请求受配置域名白名单约束，重定向目标也检查。程序检查robots并遵守间隔、限流与TLS验证；arXiv官方API按其API访问规则处理。没有登录模拟、验证码识别或付费墙绕过。

Python `urllib` 使用运行环境中的代理配置；例如可在启动前为当前PowerShell进程设置代理：

```powershell
# 仅在你确有该代理时设置；下面是示例地址
$env:HTTPS_PROXY = 'http://127.0.0.1:7890'
.\run.ps1 -Command doctor -Sources arxiv_quant
```

使用计划任务时，需确认任务进程的代理环境与手动终端一致。修改当前终端环境不代表已经修改计划任务的环境。

本机透明代理可能使用Fake-IP DNS，因此命名域名以显式白名单为访问边界，不因本机DNS返回保留地址而直接拒绝；URL中的非公网IP字面量仍被限制。

证书链失败时可修复系统或代理的可信链，或在 `[collector]` 中指定已信任的 `ca_file` PEM绝对路径，然后针对该来源重新运行doctor。不要通过关闭TLS验证处理证书错误。某个端点连接失败也不能推断该平台收费。

## 故障排查

| 现象 | 解释与处理 |
|---|---|
| `No module named tomllib` | 使用的Python低于3.11；检查解释器路径和版本，不是缺少项目依赖 |
| PowerShell脚本被执行策略阻止 | 使用Python直接入口，或按本机管理策略处理脚本权限 |
| `Library not found` | 首次先执行 `run`，或检查 `DataDir` 是否指向已有资料库 |
| `Unknown source id` / 重复来源ID | 用 `sources` 核对ID，检查主配置与片段是否重名 |
| `No source files match` | 主配置移动、片段遗漏或glob拼错；恢复场景使用展开配置 |
| `CERTIFICATE_VERIFY_FAILED` | 检查代理/系统信任链及 `ca_file`；保持可选来源关闭直到复查通过 |
| `Host is outside configured allowed_domains` | 原文或重定向使用其他域名；先确认目标和权限，再明确修改白名单 |
| robots不允许或无法确认 | 查看日志和原站规则；不通过关闭检查或伪装登录绕过 |
| `continuing`，退出2 | 已保存下一页位置，继续同一普通或 `Days` 模式即可 |
| `truncated`，退出2 | 检查对应来源的页数上限，调整预算或补采范围 |
| `backoff` 或 `Retry-After` | 等待时间尚未到；确认访问恢复后再决定是否显式重试 |
| `No entries parsed` / 响应结构错误 | 可能目录改版、返回挑战页或协议变化；复查入口和适配器，不当作零新增 |
| `no_public_pdf` | 本次未得到唯一公开PDF；保留题录，后续按间隔再检查 |
| 文件像登录页而不是PDF | 正确行为是拒绝归档；不会把HTTP 200自动当成成功 |
| PDF被删或哈希不符 | `verify -Repair`重新排队，再正常采集；修复不会立即取得全文 |
| `Another collector process is already running` | 同一库有进程持锁；等待其结束。锁文件本身可能长期存在，不要据此判断进程存活 |
| 页面看不到旧论文 | 默认只展示最近7天首次发现记录；扩大 `report -Days`，或查看完整JSONL/SQLite |
| 没有新增论文 | 检查来源是否完成、`fetched/accepted/new_records`、时间窗口和筛选阈值；重复运行零新增可以是正常结果 |
| PDF任务长期不动 | 检查下载开关、重试上限、等待时间、重新分类/删除标记和单次预算；仅提高数量不解决这些条件 |
| 计划任务不执行 | 检查是否实际安装、用户是否登录、解释器与配置路径、时区、代理和任务最近运行结果 |
| 数据库版本比程序新 | 使用相匹配的新程序；不要手工降低 `user_version` |

## 验证与测试

### 代码与离线测试

```powershell
# 所有单元测试；临时数据位于tmp/tests，不操作正式库
python -m unittest discover -s tests -v

# Python语法检查
python -m py_compile collector.py adapters.py maintenance.py
```

测试覆盖规则、RSS/Atom/RDF、修订与身份去重、PDF处理、HTTP条件缓存、来源水位、补采、游标检查点、过期令牌、删除通知、配置片段、重试、备份恢复、日志轮转和进程锁。

### 实际资料库审计

```powershell
# 数据库、外键和已下载PDF的完整性
.\run.ps1 -Command verify

# 另核对JSONL与SQLite、HTML本地链接及最近执行状态一致性
python tools/verify_library.py

# 检查其他资料库
python tools/verify_library.py --data-dir '.\tmp\trial-library'
```

审计导出一致性时先等待采集完成，否则可能读到尚未全部导出的中间状态。`verify`与 `tools/verify_library.py`的检查范围不同，前者不能代替后者的目录一致性检查。

### 完整维护演练

```powershell
python tools/validate_maintenance.py
```

演练需要默认 `data/` 中已有且完成升级、健康状态正常的主库。它在 `tmp/maintenance-validation/`创建完整备份与副本，验证表数据和PDF往返一致，再在**恢复副本**上做重新分类、指定重试、故意损坏及恢复排队检查；不会故意损坏主库。演练目录会保留，执行前应留足磁盘空间。

开发探测器 `tools/probe_sources.py` 和 `tools/probe_extensions.py`会访问外部入口，用于检查接口，不是正式采集或定时入口。

### 历史实测快照

以下来自2026-10-16的验证记录，不是持续更新的覆盖率指标：

| 项目 | 实测结果 |
|---|---|
| 自动测试 | 77项通过 |
| 默认采集流 | 12条全来源重复运行成功，新增文献/记录均0 |
| 独立文献 / 来源记录 | 346 / 354 |
| 实际PDF / 总页数 | 39份 / 1,513页 |
| 渠道主动仅收题录 | 208条记录 |
| 分页恢复 | Crossref先保存3页进度，后续从原窗口继续并完成 |
| 新增全文样本 | EconStor公开PDF，35页；正常重跑未重复下载 |
| 备份恢复 | 数据库与PDF精确往返；展开配置独立加载成功 |

详细记录见 [VALIDATION.md](../VALIDATION.md)。本地若保留实测产物，可查看 `data/extensions_validation.json`、`data/integrity_audit.json` 和 `data/maintenance_validation.json`。这些是特定运行的证据，不应代替新的健康检查；独立PDF解析工具仅用于当时验证，不是本项目运行依赖。

## 范围限制与许可

- 公共API和RSS提供的覆盖、字段、更新时间与可用性不同，项目不承诺全网、全平台或完整历史。
- RSS窗口外的遗漏、缺乏时间字段的静态目录、远端索引在分页期间发生变动，都需要结合来源限制判断。
- 关键词规则存在误检和漏检，英文术语缩写和重复词需要维护；不提供语义检索、翻译、AI摘要、自动质量评级或PDF全文搜索。
- 文献身份关联是保守规则，不保证所有预印本、期刊版与报告变体都能正确合并；冲突需要人工检查。
- 网页/API改版后可能需要修改适配器，运行报告和测试用于暴露变化，不保证永久免维护。
- 不提供回测、交易信号、券商下单、投资组合执行或投资建议；金融研究主题不会触发任何交易操作。
- 当前设计面向个人研究归档。对外分享优先使用题录、来源链接和自己的笔记；PDF再分发需按各文献许可处理。许可元数据也可能不完整，不应据此假定全部全文可再发布。

## 相关文档与官方参考

- [采集管线、历史任务与检索导出](../PIPELINE.md)
- [渠道扩展、候选来源与协议契约](../EXTENDING.md)
- [实际执行和验证记录](../VALIDATION.md)
- [arXiv API](https://info.arxiv.org/help/api/user-manual.html)与[使用规则](https://info.arxiv.org/help/api/tou.html)
- [Crossref日期过滤](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/)
- [DataCite查询](https://support.datacite.org/docs/queries)与[分页](https://support.datacite.org/docs/pagination)
- [OAI-PMH协议](https://www.openarchives.org/OAI/openarchivesprotocol.html)
- [BIS RSS](https://www.bis.org/rss)、[美联储RSS](https://www.federalreserve.gov/feeds/feeds.htm)
