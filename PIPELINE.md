# 采集管线、历史任务与检索导出

2026-10-17 增补。所有功能只使用 Python 标准库，不调用 AI、不启用计划任务、不发送外部通知。以下命令在项目目录执行；PowerShell 与 Python 入口等价。

## 常用入口

```powershell
# 发现题录与处理全文；来源轮转，发现阶段为下载预留时间
.\run.ps1

# 仅发现题录，允许发现阶段使用全部总时长预算
.\run.ps1 -MetadataOnly

# 仅处理现有全文队列，不调用题录接口、不推进普通水位
.\run.ps1 -Command download -MaxDownloads 10

# 查看候选、异常隔离项与指标，默认列表每页50条
.\run.ps1 -Command candidates -Sources arxiv_quant -Limit 50 -Offset 0
.\run.ps1 -Command quarantine -Limit 50
.\run.ps1 -Command metrics
```

`run` 扫描来源失败，不再阻止同一来源已入库、已到重试时间的 PDF 任务。下载仍检查自身域名、robots、HTTP 限流、记录等待时间、尝试上限、规则和删除标记。HTTP 429/503携带的 `Retry-After`会按主机持久保存；同主机已知地址的队列暂缓，不消耗记录尝试次数，其他主机仍可处理。落地页解析后才发现等待中的重定向主机时，该次解析可能记一次失败并保存记录等待时间。题录接口与全文地址可能共用主机，独立下载不表示获得了绕过该主机访问限制的权限。

## 候选题录与筛选审计

`candidates` 保存配置查询实际返回并成功映射的候选，包括接受、分数不足、标题排除、RSS/HTML时间窗口外的条目。每个来源标识和元数据指纹对应一个候选，重复运行更新观察时间。它不是平台镜像，也不包含此前从未留存的条目。

候选保留规范化题录、当前判断和最近20次不同判断，判断记录包括规则哈希、分数、原因及观察时间：

| 原因 | 含义 |
|---|---|
| `accepted` | 通过本次筛选 |
| `below_threshold` | 分数不足 |
| `excluded_title` | 标题命中排除词 |
| `outside_window` | RSS/HTML条目日期不在本次扫描窗口内，未入正式库 |

```powershell
# 修改词表后，在本地候选与正式库上重新筛选
.\run.ps1 -Command rescreen

# 只处理一个来源
.\run.ps1 -Command rescreen -Sources arxiv_quant

# 如果只想更新已入库记录标签，仍可使用原来的命令
.\run.ps1 -Command reclassify
```

`rescreen` 更新正式库中已有记录的规则标签，并把当前通过的候选幂等入库。它不会联网、不删除历史记录/PDF，也不会仅因分数变化创建新版本。来源删除标记继续保留。显式重筛也会处理保留的 `outside_window` 候选，不再套用之前那次采集的日期窗口。

候选默认按**最近观察时间保留90天**，逻辑负载上限100 MiB；最新候选优先保留。默认不保存完整 HTTP 响应。可选原始响应使用 gzip 存在 SQLite 中，同样保留90天、单独限制50 MiB，并按来源、最终URL和响应哈希去重。只捕获题录发现阶段的响应，不保存PDF响应或认证头；诊断访问不归档。

容量统计包括候选元数据、判断历史及固定行开销估算；它不是 SQLite 文件大小的硬上限。清理后数据库文件会复用空闲页，不自动缩小。清理在采集、重筛、重放或启用的响应归档时执行，读取列表不会触发清理；一个批次处理中可能暂时超过负载上限。

异常隔离项为了防止未解决材料被自动丢弃，不受候选过期清理影响；其负载大小在 `metrics.storage.unresolved_payload_bytes` 中单独显示。正式文献、PDF、备份和指标历史也不受候选配额控制。

## 分阶段预算和公平处理

| `[collector]` 配置 | 默认值 | 行为 |
|---|---|---|
| `retain_candidates` | `true` | 保存候选及判断历史 |
| `candidate_retention_days` | `90` | 候选/可选响应按最近观察时间过期 |
| `candidate_max_mb` | `100` | 候选逻辑负载上限，MiB |
| `retain_raw_responses` | `false` | 可选的题录HTTP响应归档 |
| `raw_response_max_mb` | `50` | 压缩原始响应负载预算，MiB |
| `discovery_budget_fraction` | `0.65` | 普通运行发现阶段最多占总网络预算的比例，范围 `(0,1]` |
| `source_max_seconds` | `180` | 单来源本次发现阶段的时间上限 |
| `download_max_per_source` | `10` | 单来源每次运行最多处理的PDF任务；`0`不设此项上限 |
| `backlog_alert_days` | `7` | 老旧待下载队列、持久检查点的关注阈值 |

普通运行默认总网络预算20分钟，发现最多13分钟，剩余时间供全文队列使用；未用完的发现时间也可供下载使用。单来源还受当前剩余发现时间的均分额度与 `source_max_seconds` 约束。来源起点在数据库中持久化，上次没轮到的来源可优先获得下一次机会。

下载在来源之间轮流取一条，每个来源内部优先较少尝试次数、较早发现的记录；下载起点也持久化。来源配额与全局 `MaxDownloads` 同时生效，配额按尝试处理数计算，不按新增文件数计算。达到配额时保留队列，下一次继续。

`download` 直接使用总网络预算，不先分配发现预算。`MetadataOnly` 和历史补采不执行全文下载，发现阶段可使用总预算。预算在网络操作之间检查，仍不是硬实时进程终止器。

## 固定日期历史任务

```powershell
# 起点包含，终点作为分段上界；首次创建一个job
# 这里按7天分段，本次最多执行2个分段任务
.\run.ps1 -Command backfill -Sources arxiv_quant `
  -FromDate '2026-01-01' -UntilDate '2026-02-01' -ChunkDays 7 -MaxTasks 2

# 以实际返回的job_id替换示例；后续不再传日期
.\run.ps1 -Command backfill -JobId 'bf-实际任务ID' -MaxTasks 2

# 查看所有任务，或只看一个job
.\run.ps1 -Command jobs
.\run.ps1 -Command jobs -JobId 'bf-实际任务ID'

# 补采完成后单独取得符合条件的全文
.\run.ps1 -Command download -Sources arxiv_quant -MaxDownloads 10
```

日期可用 ISO 日期或带时区时间戳；没有时区时按UTC解释。结束时间不能晚于当前时间。同一来源每个分段有固定起止日期、独立模式 `backfill:<task_id>` 和检查点。再次执行相同创建命令会建立**新job**；继续已有job必须指定 `JobId`。

每次最多尝试 `MaxTasks` 个任务，并共享一次总时长预算；任务先按日期再按ID处理。一条任务达到分页上限后，本次保留 `continuing`，下一次同job续页。已完成任务跳过，中断的 `running`、失败或预算延期的任务可恢复。暂停的方法是停止继续调用；运行中可用 Ctrl+C，中断后保留已提交进度。没有后台执行器。

| 任务状态 | 含义 |
|---|---|
| `pending` / `running` | 未开始 / 开始后尚未最终记录结果；中断后可恢复 |
| `continuing` | 固定窗口还有下一页 |
| `deferred_budget` / `failed` | 预算延期 / 请求或协议失败，可在下次续跑 |
| `done` | 支持历史时间过滤的接口窗口已完成 |
| `limited` | 当前可见窗口已处理，或明确接受了条目缺口，不能证明历史完整 |
| `needs_review` | 窗口扫描结束，但有已保存、未解决的异常条目 |

历史任务**不推进普通增量水位**，也不替代正常增量采集。普通来源冷却可被显式补采绕过，但不能绕过已持久保存的主机 `Retry-After` 或当前HTTP访问限制。续跑默认使用job里仍待处理的配置来源，包括原先显式选择的可选来源；也可传 `Sources` 限定本次任务范围。来源配置丢失会报错；改变来源/规则后，原窗口仍固定，但旧游标不复用。

返回的 `scan_finished` 包括 `done`、`limited` 和 `needs_review`；`all_api_windows_done` 只在全部任务为 `done` 时为真。`historical_coverage_guaranteed` 始终为假：完成已配置查询的可用接口窗口，不等于收齐平台全部论文。存在未完成或待审核任务时退出2，只有受限RSS/HTML窗口的任务处理完可退出0，同时明确显示 `limited`。

首次请求前就固定窗口，即使第一页失败，下次也沿原窗口重试，不让初始化回看范围随失败时间滑动。arXiv、Crossref、DataCite、OAI-PMH均支持持久化分页；arXiv使用固定查询窗口和偏移量。分页请求中途网络超时或预算耗尽，会尽可能先保存已经完成页面的题录与下一页位置，失败页下次重试。协议整体损坏仍是来源错误，不把挑战页当成空结果。远端索引可能变化，固定窗口、重叠与去重不等于服务端快照保证。

## 异常隔离与离线重放

```powershell
# 查询异常，items含原始条目/规范化题录、错误、处理阶段及关联任务
.\run.ps1 -Command quarantine -Sources crossref_quantitative_finance

# 修复适配器或身份冲突后，离线重试指定隔离项
.\run.ps1 -Command replay -ItemId 1,2

# 或重试指定来源全部未解决项
.\run.ps1 -Command replay -Sources crossref_quantitative_finance

# 只有明确接受材料缺口时，人工关闭指定异常；不会伪造恢复成功
.\run.ps1 -Command resolve -ItemId 1 -Reason '原站条目缺少题名，已人工核查并接受此缺口'
```

RSS/Atom/arXiv单条XML、Crossref/DataCite单条JSON、OAI单条XML的映射错误，以及规范化题录的身份/字段错误，会存入 `item_errors`，随后继续处理其他条目。身份冲突不会自动合并，出错项的部分数据库写入会回滚。

错误项按来源、阶段和原始片段哈希去重。`replay`使用当前适配器与当前规则重新处理保存的片段，不访问网络；成功后标记解决，失败则保留原因和重放次数。缺失字段不会凭空出现：需要修复映射、解决身份冲突，或者在来源恢复后重新发现正确材料。

确实无法恢复时，`resolve`要求明确条目ID和原因，把原始片段与人工接受缺口的理由一起保留并写入维护历史；不会增加文献或把原始错误删掉。其关联任务最多转成 `limited`，标记 `accepted_item_gaps`，不能成为全部接口窗口已完整处理的依据。

分页能够在异常条目持久留存后继续；来源窗口完成时显示 `needs_review`，不把有异常的运行当成正常完整入库。普通扫描水位此时可推进，未解决项由隔离队列负责，`status/metrics`继续提示。历史任务完成扫描后的 `needs_review` 在关联错误全部重放成功后可转为 `done/limited`。一个错误可关联多个任务。

整个JSON/XML无法解析、端点返回错误结构、HTTP错误等不是单条内容错误，不会靠跳过响应来推进水位。隔离队列也不是自动合并或人工编辑题录的界面。

`last_run.json`仍是历史快照。重放完成后它可继续显示过去那次部分失败；下一次正常扫描成功才产生新的运行快照。

## 指标与本地关注项

```powershell
.\run.ps1 -Command metrics
.\run.ps1 -Command status
```

指标包括逐来源扫描耗时、有效映射题录数、进入时间窗口的筛选数、通过数、隔离数、摘要/作者缺失数、30天下载结果及成功比例、到期可处理队列数、最老待下载记录、检查点创建/更新时间、候选与响应负载。30天下载比例分母包含有处理结果的获取任务（含未找到公开PDF），排除预算延期；它不是论文全文覆盖率。

异常检测不使用AI，规则为：同一来源、同一来源/筛选配置、完成的普通扫描，最近两次与之前3至6次比较。典型返回量至少5条时，两次都下降到历史中位数20%以下才报告数量异常；通过率类似比较并要求近期每次至少5条。通过率以实际进入时间窗口筛选的条目为分母，避免RSS条目自然移出窗口产生误报。作者/摘要缺失两次均达到90%，且历史典型缺失率低于50%时才报告字段退化。历史本就稀疏、正常零新增或单次零结果不据此报错。

积压、老旧检查点及未解决隔离项是独立关注原因。积压最老时间不意味着该条记录现在就能下载，可能在等待或已耗尽重试；`ready_downloads`才进一步排除记录/已知主机等待、耗尽、关闭下载、规则暂停与来源删除。主机等待在 `host_cooldowns` 中列出。旧版检查点没有创建时间，第一次重新保存后才开始跟踪年龄。

关注项写入本地JSON和状态，不发送邮件、Webhook或消息。`metrics/status`有关注项返回2；正常采集末尾也会汇总管线关注项，因此下载或来源请求成功仍可能返回2。

## 全库检索

```powershell
.\run.ps1 -Command search -Query 'volatility' -Author 'Smith'
.\run.ps1 -Command search -Topic '衍生品' -PdfStatus downloaded
.\run.ps1 -Command search -Doi '10.实际DOI/后缀'
.\run.ps1 -Command search -FromDate '2025-01-01' -UntilDate '2026-01-01' -DateField published
.\run.ps1 -Command search -Sources arxiv_quant -AllVersions -Limit 100 -Offset 0
```

检索读取全部已入正式库的历史记录，不受HTML展示窗口限制；默认也可查到关闭来源的历史记录。多个条件取交集，Query对标题、摘要、作者、主题、DOI做大小写和Unicode归一化后的**字面子串**匹配，不是布尔语法或语义搜索。作者、主题为子串，DOI为规范化后精确匹配，PDF状态精确匹配。

日期字段可选 `published`、`updated`、`first_seen`；范围左闭右开。只有年份/年月、无法可靠解析或缺失的日期，在按该字段设日期条件时排除，不人为补月补日。可切换到可靠的本地 `first_seen` 查询。无日期条件时仍可找到这些记录。

默认每篇文献返回最新发现的**匹配**记录一条；`AllVersions`返回全部匹配来源/版本。按首次发现时间、记录ID倒序排序，默认50条（PowerShell的 `Limit=0`表示使用命令默认值）；返回 `total` 为折叠后的总匹配数，`offset`用于翻页。日期、状态与其他条件在折叠前应用。

实现为本地元数据顺序筛选，适合当前个人资料库；尚未加入大型索引或PDF正文搜索。

## 引用与结构化导出

```powershell
# 默认导出全部匹配文献，每篇一条；也可带与search相同的筛选条件
.\run.ps1 -Command export -Format csv -Output '.\tmp\research.csv'
.\run.ps1 -Command export -Format bibtex -Query 'portfolio' -Output '.\tmp\portfolio.bib'
.\run.ps1 -Command export -Format ris -Topic '风险管理' -Output '.\tmp\risk.ris'
.\run.ps1 -Command export -Format json -AllVersions -Output '.\tmp\all-versions.json'

# Python可直接输出内容到标准输出；指定Output时返回输出文件信息
python collector.py export --format bibtex --query volatility
```

支持 `json/csv/bibtex/ris`，UTF-8；输出文件必须不存在，父目录自动创建。默认导出全部匹配结果，提供Limit/Offset时才分页。JSON保留来源记录字段和嵌套元数据；CSV提供题名、作者、DOI、摘要、主题、来源和本地PDF路径；BibTeX/RIS可用于文献管理软件导入。

BibTeX当前使用通用 `misc`，RIS使用 `GEN`，不虚构缺失的刊名、卷期、页码或文章类型。引用键由文献/记录ID构成；作者仍是来源提供的姓名字符串。BibTeX转义特殊字符，RIS合并字段内换行；CSV对公式起始文本加前置单引号，便于作为文本在表格中打开。PDF不嵌入引用文件，导出也不会重新分发全文。

## 数据结构、升级和验证

数据库结构版本4在版本3基础上增加：`candidates`、`item_errors`、`error_tasks`、`raw_responses`、`pipeline_state`、`backfill_tasks`、`source_metrics`、`checkpoint_tracking`。原有文献ID、记录ID、PDF路径及下载记录保留，升级前自动保存数据库快照。

完整ZIP备份通过SQLite快照包含这些表，候选、压缩响应、异常、任务、指标和检查点会一起恢复。展开配置保存新增预算参数。候选过期清理不会影响正式记录；较旧备份恢复到新版时会迁移，旧版程序不能打开版本4。

```powershell
python -m unittest discover -s tests -v
.\run.ps1 -Command verify
python tools/verify_library.py
```

测试覆盖候选恢复与配额、错误隔离与重放、独立下载与轮转、固定历史窗口、中断续跑、arXiv续页、跨页预算中断、删除标记、连续异常、全库筛选、引用转义、迁移及备份往返。实际验证记录见 [VALIDATION.md](VALIDATION.md)。
