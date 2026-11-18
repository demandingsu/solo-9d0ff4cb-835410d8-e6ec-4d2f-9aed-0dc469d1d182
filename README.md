# Library Journal Access API

高校图书馆期刊可访问性判定服务。把出版商/数据库商提供的期刊覆盖表按**资源包**整理进库，
检索系统用「查询日 + ISSN」查出**当日实际可打开的出版日期段**及其来源包。

1.1 起新增**撤稿清单**：按 DOI 登记文章、接收带来源与修订号的撤稿/恢复通知，
按查询日输出「已确认撤稿但当日仍可从馆内打开」的文章清单（含通知依据与来源包），
以及「可访但各来源结论分歧、待人工核对」的文章清单，避免读者把可访问误当作可信。

技术栈：FastAPI + SQLite（WAL 模式），Python 标准库 `sqlite3`，无 ORM。

## 快速启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs （Swagger UI，可直接操作）
```

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# 测试：pip install pytest httpx && python -m pytest tests/ -v
```

SQLite 数据文件位置由环境变量 `DB_PATH` 决定（Compose 中为 `/data/library.db`，挂在命名卷上）。

## 概念与规则

- **资源包 package**：有名称、授权起止日（`license_start`/`license_end`，闭区间）、
  启用/停用标志。只有**查询日当天处于授权期内且未停用**的包才参与判定。
- **覆盖快照 coverage snapshot**：按包整体上传，每条记录 = ISSN + 出版日期范围 +
  最近禁访月数 `embargo_months`。`PUT .../coverage` 是**全量替换**，在一个事务里
  删旧插新。
- **移动禁访墙**：禁访边界随查询日移动。查询日为 `D`、禁访 `M` 个月时，该条覆盖
  可访区间 = `[start_date, min(end_date, D − M个月)]`；区间为空则该条当日不可访。
  月份相减按日历月，日数超出目标月则截到月末（如 3 月 31 日 − 1 月 = 2 月 28 日）。
- **拆段**：多个包覆盖同一期刊时，把可访区间按「来源包集合」切分——集合发生变化的
  边界处拆段，每段列出支撑它的全部来源包。停用某包只是把它从参与集合中移除，
  其他包的覆盖行原样保留，重叠段的依据不丢。
- **一致性**：每次成功写入（建包、改授权、换快照、启停用、登记/更正文章、提交通知）在同一事务里把全局
  `data_version` 加一；写入失败整体回滚，读者继续看到旧版本。每次查询在**单个读
  事务**内完成（WAL 快照读），响应里的 `data_version` 就是本次答案实际依据的提交
  版本——并发写不会把新旧来源混进同一个答案。

## 撤稿清单（1.1 新增）

- **文章 article**：按 DOI 登记，字段 = ISSN + 出版日。`PUT /articles` 幂等：
  不存在则登记（201），已存在则**更正** ISSN/出版日（200），内容不变则不产生新版本。
  DOI 统一规范化为小写裸形式（接受 `10.x/...`、`doi:10.x/...`、`https://doi.org/10.x/...`）。
- **通知 notice**：`POST /notices`，字段 = DOI + 来源 `source` + 修订号 `revision` +
  结论 `kind`（`retraction` 撤稿 / `restoration` 恢复）。`(doi, source, revision)`
  是通知的标识：
  - 同键同结论重复提交 → 幂等 200，状态不变；
  - **同键异结论 → 409 拒收**，什么都不写；
  - **同源迟到的低修订号**会被存档（审计可查），但标记 `superseded`，
    **不覆盖**该来源已有的新结论；
  - 通知**不要求文章已存在**——先到通知、后补文章时，文章登记后自动进入判定。
- **判定规则**（每次查询现算，不缓存）：每个来源取其**最高修订号**通知作为当前结论；
  所有来源一致撤稿 → `confirmed`（确认撤稿）；一致恢复 → 不上清单；
  **各来源最新结论相反 → `disputed`（待核）**。
- **清单** `GET /retractions?date=...`：在**单个读事务**内取文章、通知、覆盖与授权的
  同一已提交快照，输出：
  - `confirmed`：确认撤稿且**查询日当日可访**的文章（DOI、通知依据 `basis`、
    当日提供访问的**所有来源包** `packages`）；
  - `disputed`：当日可访但来源结论分歧、待人工核对的文章。
  当日不可访（无授权、包停用、覆盖不含出版日、仍在禁访墙内）的文章不上清单。
- **重新判定**：清单在查询时现算，因此文章更正、覆盖快照替换、授权窗口修改、
  包启停用，以及禁访边界随查询日移动，都会在下一次查询自动反映。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/packages` | 登记资源包（名称 + 授权起止日） |
| GET | `/packages` / `/packages/{id}` | 查看包（含覆盖行数） |
| PUT | `/packages/{id}/license` | 修改授权起止日（事务提交） |
| PUT | `/packages/{id}/coverage` | 全量替换该包覆盖快照（原子） |
| GET | `/packages/{id}/coverage` | 查看该包当前快照 |
| POST | `/packages/{id}/deactivate` / `activate` | 停用 / 重新启用 |
| GET | `/access?issn=...&date=...` | 查询可访出版日期段及来源包 |
| PUT | `/articles` | 按 DOI 登记文章，或更正其 ISSN / 出版日（幂等） |
| GET | `/articles` | 查看已登记文章（可按 `?doi=` 过滤） |
| POST | `/notices` | 提交撤稿/恢复通知（来源 + 修订号；同键异内容 409） |
| GET | `/notices` | 通知审计轨迹（按到达序，含 `superseded` 标记） |
| GET | `/retractions?date=...` | 撤稿但仍可访清单 + 可访但结论分歧清单 |

ISSN 接受 `0028-0836` / `00280836` / 末位 `X`（大小写均可），库内统一存 `XXXX-XXXX`
大写形式；只校验格式，不校验校验位（出版商表格常有脏数据）。

### 示例

```bash
# 1. 登记两个包
curl -X POST localhost:8000/packages -H 'Content-Type: application/json' \
  -d '{"name":"Elsevier-ScienceDirect","license_start":"2024-01-01","license_end":"2026-12-31"}'
curl -X POST localhost:8000/packages -H 'Content-Type: application/json' \
  -d '{"name":"JSTOR-Archive","license_start":"2024-01-01","license_end":"2026-12-31"}'

# 2. 上传覆盖快照（同一 ISSN，禁访月数不同）
curl -X PUT localhost:8000/packages/1/coverage -H 'Content-Type: application/json' \
  -d '{"entries":[{"issn":"0028-0836","start_date":"2010-01-01","end_date":"2026-12-31","embargo_months":0}]}'
curl -X PUT localhost:8000/packages/2/coverage -H 'Content-Type: application/json' \
  -d '{"entries":[{"issn":"0028-0836","start_date":"2015-01-01","end_date":"2026-12-31","embargo_months":12}]}'

# 3. 查询
curl 'localhost:8000/access?issn=0028-0836&date=2026-11-18'
```

响应（注意禁访墙随查询日移动、来源变化处拆段）：

```json
{
  "issn": "0028-0836",
  "date": "2026-11-18",
  "data_version": 4,
  "segments": [
    {"start": "2010-01-01", "end": "2014-12-31", "sources": ["Elsevier-ScienceDirect"]},
    {"start": "2015-01-01", "end": "2025-11-18", "sources": ["Elsevier-ScienceDirect", "JSTOR-Archive"]},
    {"start": "2025-11-19", "end": "2026-11-18", "sources": ["Elsevier-ScienceDirect"]}
  ]
}
```

### 撤稿清单示例

```bash
# 1. 登记文章（DOI 规范化存储）
curl -X PUT localhost:8000/articles -H 'Content-Type: application/json' \
  -d '{"doi":"10.1234/test.1","issn":"0028-0836","pub_date":"2024-06-15"}'

# 2. 提交撤稿通知（可先于文章到达；乱序、迟到低修订不会覆盖新结论）
curl -X POST localhost:8000/notices -H 'Content-Type: application/json' \
  -d '{"doi":"10.1234/test.1","source":"crossref","revision":2,"kind":"retraction"}'

# 3. 按查询日取清单
curl 'localhost:8000/retractions?date=2026-11-18'
```

响应（确认撤稿且当日可访的文章，含通知依据与所有来源包；分歧另列）：

```json
{
  "date": "2026-11-18",
  "data_version": 6,
  "confirmed": [
    {
      "doi": "10.1234/test.1",
      "issn": "0028-0836",
      "pub_date": "2024-06-15",
      "basis": [{"source": "crossref", "revision": 2, "kind": "retraction"}],
      "packages": ["Elsevier-ScienceDirect"]
    }
  ],
  "disputed": []
}
```

## 项目结构

```
app/
  main.py     FastAPI 路由与事务边界（包/覆盖/授权 + 文章/通知/撤稿清单）
  db.py       SQLite 连接、schema、读/写事务、版本计数
  logic.py    纯函数：月份相减、禁访墙、区间拆段合并、通知分类判定
  schemas.py  Pydantic 请求/响应模型（含 ISSN / DOI 规范化）
tests/
  test_logic.py        拆段、禁访墙与通知分类单测
  test_api.py          API 集成测试（重叠覆盖、失败回滚、并发一致性）
  test_retractions.py  撤稿清单集成测试（乱序、分歧、更正、重判定）
Dockerfile / docker-compose.yml / requirements.txt
```

## 验证情况（如实说明）

**已实现并验证**（`pytest` 45 项全过 + uvicorn 实跑 curl 冒烟）：

- 按包上传全量覆盖快照、登记/修改授权起止日；
- 只采用查询当日有授权且未停用的包（授权前/后查询返回空）；
- 禁访边界随查询日移动（含月末截断、整段被禁访为空）；
- 多包重叠覆盖按来源集合变化拆段，不同禁访月数也正确拆段；
- 停用包后其他包的覆盖依据保留，重新启用后恢复；
- 非法快照（日期倒置、ISSN 格式错）整体 422 拒绝，`data_version` 不变、旧数据照常服务；
- 非法授权修改（起晚于止）422 拒绝，旧授权继续生效；
- 并发读写在替换快照期间，每个响应的 `data_version` 与其段内容严格对应，未观察到新旧混用；
- `/docs`（Swagger UI）与 `/openapi.json` 可用。

撤稿清单（1.1 新增，均经 pytest 与 curl 冒烟验证）：

- 按 DOI 登记/更正文章（幂等；DOI 大小写、`doi:`、`https://doi.org/` 前缀统一规范化）；
- 通知先到、文章后补：文章登记后自动进入判定；
- 同源乱序：高修订先到、低修订迟到仅存档标 `superseded`，不覆盖新结论；
- 同源同修订号异结论：409 拒收，`data_version` 与清单内容不变；
- 同内容重复提交：幂等 200，不产生新版本、不重复存储；
- 不同来源最新结论相反：进入 `disputed` 待核清单；一方提修订号达成一致后回到 `confirmed`；
- 恢复通知（更高修订号）使文章离开清单；
- 清单按查询日现算：文章更正（ISSN/出版日）、覆盖替换、授权修改、包启停用、
  禁访墙随查询日移动，均在下一次查询反映；
- 报告在单个读事务内取文章+通知+覆盖+授权的同一提交快照，响应携带 `data_version`，
  与 `/access` 报告的版本一致；各类非法写入（422/409）后清单与版本均不变。

**未验证 / 限制**：

- 本环境无 Docker，`Dockerfile` / `docker-compose.yml` 经静态核对（基础镜像、依赖版本与本地
  实测一致的 `fastapi==0.115.6` / `uvicorn==0.32.1`、启动命令与本地实跑相同），但未实际构建运行；
- SQLite 单写者，适合馆内单实例部署；多实例水平扩展需换外部数据库；
- ISSN 只校验格式不校验校验位；DOI 只校验 `10.<registrant>/<suffix>` 基本形态；
  覆盖快照、文章与通知的上传均未做鉴权（内网部署假设）；
- 通知的「内容」按结论（`kind`）判定，同键仅 `note` 不同的重复提交按相同通知幂等处理；
- 撤稿判定以「每个来源的最高修订号」为准，假设同一来源的修订号单调递增、
  高修订代表该来源更新结论；跨来源分歧不做自动裁决，一律列入待核。
