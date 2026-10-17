# 渠道扩展与访问边界

渠道核验日期：2026-10-16，北京时间；管线契约更新于2026-10-17。当前默认10个平台、12条采集流，另有3条关闭的可选流。程序仍只有Python标准库，不调用AI、不启用定时。

## 实际新增渠道

| 来源ID | 获取内容 | 增量机制 | 全文策略 |
|---|---|---|---|
| `crossref_quantitative_finance` | Quantitative Finance期刊DOI、作者、题名、部分摘要及许可字段；已核对ISSN `1469-7696` | 索引时间范围、固定窗口、新版游标 | 默认仅题录；出版商PDF链接和许可字段不等于免费全文权限 |
| `datacite_quant_research` | Zenodo等研究仓储注册的金融研究题录、类型、许可及部分直接文件链接 | 元数据`updated`范围、游标 | 默认仅题录；不自动放行任意仓储域名 |
| `econstor_open_research` | 开放经济金融论文、工作论文、报告等 | OAI-PMH修改时间、`resumptionToken`及删除通知 | 解析公开PDF后下载；已实测一个35页样本 |

三条流均已默认启用。Crossref当前只查指定期刊，DataCite按金融标题查询及文献类型过滤，不是全平台镜像。DataCite的Text类型可能包含学位论文等材料；收录和关键词分数不等于研究质量评价。

## 扩展配置

`config.toml`通过`source_files = ["sources.d/*.toml"]`加载独立配置。新建同目录TOML文件后，下次运行自动发现；片段只能包含`[[sources]]`，不能覆盖全局预算或筛选。重复ID、未知字段、错误协议和缺失文件都会报错。同一来源ID关联长期水位，应保持稳定。

例如，新增已核验的RSS：

```toml
[[sources]]
id = "institution_working_papers"
name = "Institution working papers"
kind = "rss"
url = "https://example.org/research/feed.xml"
allowed_domains = ["example.org"]
enabled = true
download = true
```

示例地址需要替换为真实官方入口后再保存。添加期刊可复用Crossref配置并替换已核验ISSN；添加仓储可复用OAI-PMH配置，先读取其`Identify`、`ListMetadataFormats`和`ListSets`，确认`oai_dc`、时间粒度与集合名。`set_spec`可省略，`date_granularity`可选`seconds`或`days`。

```powershell
# 不联网，列出所有来源和获取边界，包括关闭的来源
.\run.ps1 -Command sources

# 先做单来源抽样检查，再在独立库试采
.\run.ps1 -Command doctor -Sources institution_working_papers
.\run.ps1 -Sources institution_working_papers -MetadataOnly -DataDir '.\tmp\new-source'

# 使用不同的完整配置文件
.\run.ps1 -Config '.\my-config.toml' -Command sources
```

新增全文域名及重定向域名仍需明确加入`allowed_domains`；不会根据远端返回链接自动放行。

## 新协议适配

`adapters.py`注册了六类协议：`rss`、`html`、`arxiv`、`crossref`、`datacite`、`oai_pmh`。新协议实现一个函数，用`@register`声明配置选项、覆盖范围、增量字段和是否支持续采，调度器无需增加具体网站分支。

```python
@register("new_api", options=("query",), coverage="明确覆盖范围",
          incremental_axis="明确API字段", resumable=True)
def new_api(client, source, since, max_pages, api):
    # 网络只能通过client.get；元数据映射为api.Paper。
    # 返回Harvest，不直接写SQLite或推进水位。
    # 参考现有适配器处理固定窗口、source['_checkpoint']和分页。
    ...
```

适配器可使用运行时 `source['_item_error_handler'](stage, payload, error)` 持久隔离单条映射错误；调用后继续处理其他条目。新增stage还需在 `replay_item` 中实现离线映射。不要隔离整个协议错误响应来假装采集完成。`Harvest.error`可携带分页中途请求错误，已有页面与下一页位置先持久保存，运行仍记录失败/预算延期。arXiv现已支持持久化偏移量；完整流程见 [PIPELINE.md](PIPELINE.md)。

每个适配器应测试合法零结果、协议错误、响应字段缺失、分页结束、预算上限、重复游标、恢复和删除通知，并做小规模联网验证。HTTP 200、空HTML、登录页或验证码页不能当成采集成功。

## 分页与增量语义

- 每个来源、普通增量或显式`-Days`模式独立保存查询起止时间和游标。达到页数预算时为`continuing`，退出2，不推进水位、不计来源失败、不进入故障冷却；下次同模式继续。
- 完成固定窗口后清除检查点，普通水位推进到原窗口结束时间；期间新记录由下一窗口及重叠扫描处理。
- 配置变化不复用旧游标；网络超时保留旧检查点。OAI明确返回`badResumptionToken`时，仅从同一旧窗口重启一次，已入库记录幂等去重。令牌有效期短的仓储应使用更小窗口或更多页数预算。
- Crossref按索引时间发现变更，但保留原始发表/存入时间；不把早年论文在当前窗口重新索引的记录过滤掉，也不把单纯索引时间变化制造成新版本。
- Crossref于2026年8月升级分页行为：每页读取新游标，续页保留所有原始参数。远端索引变动仍可能改变结果集；固定窗口、重叠和去重减小影响，但不能保证外部API快照一致性。
- OAI删除通知写入`source_notices`，已有条目标记`source_deleted`并暂停下载，保留历史题录与PDF。仓储删除不等于学术撤稿。
- `metadata_only`表示渠道主动只取题录，不是下载失败；汇总将它与待下载数量分开。RSS/HTML仍受当前可见窗口限制，不能由接口扩展推导完整历史覆盖。

## 已探索但未启用的渠道

| 渠道 | 实际限制 | 处理 |
|---|---|---|
| 澳洲联储RDP RSS | 官方地址已确认，本机TLS证书链失败 | 配置`rba_research_papers`关闭 |
| 瑞士央行研究RSS | 官方地址已确认，本机TLS证书链失败 | 配置`snb_working_papers`关闭 |
| HAL API | 金融检索入口和修改时间字段存在，本机TLS证书链失败 | 尚未实现其JSON适配器，不计为已接入 |
| OpenAlex | 基础匿名查询与规模化深分页权限不同；游标深分页需要已验证的免费密钥 | 暂未实现；不代申请账户、不发邮件、不使用付费额度 |
| 英格兰银行目录 | 可连接，但本机静态HTML没有可用论文链接 | 未计为解析成功，不启用 |
| 纽约联储探测目录 | 返回的是2017年档案 | 不当作当前更新入口启用 |

原ECB可选流仍关闭。证书问题须修复可信CA，再用doctor复查；不关闭TLS验证。未接入渠道不宣称可无人值守获取。

## 备份与恢复

完整ZIP备份新增`config.expanded.json`，已展开所有来源片段，不依赖原`sources.d/`文件。检查点、删除通知及下载历史随SQLite一起备份。原`config.snapshot.toml`仅保留入口文件，跨机器恢复优先使用展开配置：

```powershell
.\run.ps1 -Config '.\data-restored\config.expanded.json' -DataDir '.\data-restored' -Command sources
```

## 官方参考

- [Crossref日期过滤](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/)、[2026年分页规则变更](https://community.crossref.org/t/changes-to-cursors-filtering-and-sorting-in-the-rest-api/16246)
- [DataCite查询及日期字段](https://support.datacite.org/docs/queries)、[分页](https://support.datacite.org/docs/pagination)
- [OAI-PMH协议](https://www.openarchives.org/OAI/openarchivesprotocol.html)、[EconStor身份与时间粒度](https://www.econstor.eu/oai/request?verb=Identify)
- [OpenAlex当前鉴权说明](https://help.openalex.org/api/authentication/)
- [RBA官方RSS目录](https://www.rba.gov.au/rss/)、[SNB官方RSS目录](https://www.snb.ch/en/services-events/digital-services/rss-calendar-feeds)
