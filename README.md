# Star Proxy · 星辰平台

**面向 ToB 客户的多供应商 AI 网关、火山素材管理与内部运营控制台。**

客户只使用平台签发的业务 API Key，即可调用已开通的文本、图片、视频、向量和音频接口。平台统一管理客户项目、上游渠道、访问密钥、额度、真实用量与月度账单，供应商凭证始终留在服务端。

[界面预览](#界面预览) · [核心能力](#核心能力) · [快速开始](#快速开始) · [客户接入](#客户接入) · [安全与运维](#安全与运维) · [生产部署](#生产部署) · [文档导航](#文档导航)

> 本页描述当前源码能力，不代表每个生产项目均已开通全部模型。可用模型及能力以业务 Key 查询 `/v1/models` 的结果为准。客户工具与控制台独立部署，关闭客户工具不影响 API。

国内 Seedance 2.5 支持 [Draft 两阶段 API](backend/docs/client-api/videos.md#seedance-25-draft样片转成片)：先用 `draft=true` 生成480p样片，再引用本站任务ID生成1080p成片。校验业务Key归属、成功状态、原渠道和7天有效期；两阶段独立记录真实Token，成片继承样片原始视频输入计价条件。海外渠道不自动开放，不新增环境变量；本地实现及模拟测试不代表已完成生产发布或付费验收。客户工具当前没有样片选择/升格按钮，可通过API调用。

![普通管理员概览](docs/screenshots/overview.jpg)

## 为什么使用 Star Proxy

- **一套客户 Key**：客户无需获取上游凭证，通过统一地址调用所属项目已开通的模型。
- **项目级隔离**：一个项目可签发多枚业务 Key，上游渠道按客户项目加密保存。
- **运营有据可查**：按 Key 查询调用日志，按项目控制额度、核对账单；未知用量不会被当作零成本。
- **分工清晰**：超级管理员管理安全、渠道和全局价目；普通管理员处理项目及日常业务。
- **素材完整闭环**：图片、视频、音频支持上传、登记、轮询、删除与失败清理。

技术栈：**Vinext / React / TypeScript · FastAPI / Python · SQLite · TOS · Docker Compose / Nginx**。

## 界面预览

以下 **16张截图**来自当前控制台真实组件的本地演示环境，使用虚构测试数据。账号、项目、邮箱、IP、价格和用量均不是生产数据或报价；掩码不是可用凭证。点击图片可查看大图。

### 普通管理员：业务运营

上方概览展示累计统计；初次加载使用占位符，不把未返回的数据显示为 0。

| 项目隔离 | 业务 API Keys |
|---|---|
| [![项目列表](docs/screenshots/projects.jpg)](docs/screenshots/projects.jpg) | [![业务密钥管理](docs/screenshots/api-keys.jpg)](docs/screenshots/api-keys.jpg) |
| 绑定真实ProjectName，区分显示名称与标识。 | 一项目多Key，支持绑定、启停及注销。 |

| 项目模型 | 模型价格 |
|---|---|
| [![模型与渠道绑定](docs/screenshots/project-models.jpg)](docs/screenshots/project-models.jpg) | [![只读模型价目](docs/screenshots/model-prices.jpg)](docs/screenshots/model-prices.jpg) |
| 为项目选择可用渠道、启用模型，查看任务与用量。 | 按厂商、类型、状态、名称和账期筛选价格。 |

| 计费账单 | 额度与用量 |
|---|---|
| [![项目计费和月度账单](docs/screenshots/billing.jpg)](docs/screenshots/billing.jpg) | [![项目总额度与Key子额度](docs/screenshots/quotas.jpg)](docs/screenshots/quotas.jpg) |
| 月度结算比例、调整、确认、支付登记与导出。 | 限额、事件和TOS待清理对象；空值表示不限额。 |

| 调用日志 | 客户文档 |
|---|---|
| [![逐Key调用日志](docs/screenshots/call-logs.jpg)](docs/screenshots/call-logs.jpg) | [![内嵌接口文档](docs/screenshots/api-docs.jpg)](docs/screenshots/api-docs.jpg) |
| 先项目后Key，查看脱敏请求与返回，导出近30天明细。 | 按供应商和模型分类，支持独立阅读、搜索和下载HTML。 |

### 超级管理员：七个独立目录

安全功能不再堆在一个长页面；各菜单按需读取自身数据，切换页面不会自动保存草稿。

| 安全管理 | 管理员账号 |
|---|---|
| [![TOTP和登录会话](docs/screenshots/admin-security.jpg)](docs/screenshots/admin-security.jpg) | [![管理员账号管理](docs/screenshots/admin-users.jpg)](docs/screenshots/admin-users.jpg) |
| TOTP换绑、登录策略、告警和会话撤销。 | 创建普通管理员，交付一次性密码，管理账号状态。 |

| 供应商渠道 | 全局模型价目 |
|---|---|
| [![供应商渠道](docs/screenshots/provider-channels.jpg)](docs/screenshots/provider-channels.jpg) | [![全局价目维护](docs/screenshots/global-rates.jpg)](docs/screenshots/global-rates.jpg) |
| 项目级凭证加密、名称修改、测试、轮换与启停。 | 按账期维护价格档位，区分未定价与零元。 |

| 磁盘与邮件 | 备份与恢复 |
|---|---|
| [![磁盘规则与多邮箱配置](docs/screenshots/disk-email.jpg)](docs/screenshots/disk-email.jpg) | [![SQLite备份与恢复](docs/screenshots/backups.jpg)](docs/screenshots/backups.jpg) |
| QQ、163等预设，多套发件账号与多个收件人。 | 自动备份、完整性校验、回滚点与安全确认。 |

<details>
<summary>安全审计截图</summary>

![管理员安全审计](docs/screenshots/admin-audits.jpg)

记录登录、账号变更和敏感操作，用于追溯，不向普通管理员开放。

</details>

截图来源及隐私规范见 [截图说明](docs/screenshots/README.md)。

## 核心能力

| 模块 | 能力 | 边界 |
|---|---|---|
| 客户与Key | 火山ProjectName校验、项目隔离、多Key、启停和子额度 | Bearer Key与管理员Session不能互相替代 |
| 模型中转 | 火山方舟、BytePlus ModelArk、豆包语音、阿里百炼、MiniMax、MaxModel、ModelTop等适配 | 默认关闭；未绑定可用渠道的模型不可调用 |
| 多模态API | Chat、Responses、图片、异步视频、向量、语音合成/识别和音频生成 | 按模型能力选接口，不是全模型全协议兼容 |
| 素材 | 图片/视频/音频、URL登记、素材组、TOS中转及清理 | 上传与登记分两步，assetId不代表已可用 |
| 风控 | QPM、写并发、每日上传量、素材数、存储及项目/Key额度 | 查询QPM超量告警，写QPM可拒绝；空额度默认不限额 |
| 账单 | 真实成功用量、月度价格、结算比例、调整、确认、CSV与打印 | 不是充值钱包，支付登记不是支付交易，账单不是发票 |
| 运维 | TOTP、角色分离、审计、会话、备份、磁盘告警与邮件重试 | 单实例SQLite，不宣称共享SQLite多实例安全 |

### 渠道与任务

- 同一项目模型使用一个活动渠道，不提供权重路由或自动故障切换。
- 凭证使用独立主密钥加密；客户不能覆盖供应商、渠道、Base URL、项目或真实上游模型ID。
- 异步任务固定提交时的渠道、凭证版本及模型，轮换凭证不破坏旧任务查询。
- 图片和视频支持 `Idempotency-Key`：同Key同请求复用结果，同幂等键不同请求体返回 `409`。
- 图片和视频结果透传供应商URL，不自动转存TOS，调用方应及时保存。

### 素材与用量

素材支持 `Image / Video / Audio`，按真实内容校验，不只信任扩展名或MIME。`uploadId`登记检查项目、Key、URL、类型和消费状态。素材需轮询至 `Active` 再用于生成；`asset://` 不应假定可跨供应商复用。

计费默认关闭，只归集本地真实成功用量。缺少用量或单价时保留“待计价”，不会按0元结算。金额采用整数微元和十进制运算；确认后冻结明细，迟到用量进入下一开放账期。详见 [账单接口](backend/ADMIN_BILLING_API.md)。

## 系统架构

```text
内部管理员 ─> 管理控制台 ─ Session + CSRF ─┐
客户程序   ────────────── Bearer业务Key ──┼─> HTTPS网关 ─> FastAPI
可选客户工具 ──────────── Bearer业务Key ──┘                  ├─ 项目 / Key / 额度 / 日志
                                                         ├─ 渠道 / 模型 / 任务 / 计费
                                                         ├─ 火山素材库 / TOS
                                                         └─ SQLite / 备份 / 磁盘告警

微信支付 ─ POST /minidrama/payments/callbacks/wechat
         └─ 网关透明代理 ─> LocalMiniDrama（验签、解密与订单）
```

| 运行单元 | 位置 | 职责 |
|---|---|---|
| 内部控制台 | `app/` | 超管和普通管理员的独立工作台 |
| 后端API | `backend/` | 接口、管理认证、数据和后台维护 |
| 可选客户工具 | `user-portal/` | 使用业务Key的素材、视频和模型调试界面 |
| HTTPS网关 | `deploy/volcengine/` | TLS、同源代理和外部支付回调转发 |

客户可以直接用API批量接入，不需要打开客户工具。内部控制台用于管理，不能把管理员Cookie交给客户调用业务接口。

## 快速开始

### 1. 环境准备

- Node.js `>=22.13.0`，npm。
- Python `>=3.11`，推荐使用 `uv`；后端容器使用Python 3.12。
- `ffprobe`和`ffmpeg`：音视频校验及相关测试所需，后端Docker镜像已安装FFmpeg。
- 按使用范围准备火山AK/SK、TOS及供应商服务权限。

以下本地命令使用PowerShell。已有配置时不要重复复制覆盖；完整字段见 [后端配置样例](backend/.env.example)。

### 2. 启动后端

```powershell
cd backend
Copy-Item .env.example .env
uv sync --extra test
```

按需求编辑本地配置：

| 配置 | 用途 |
|---|---|
| `DATABASE_PATH` | SQLite持久化位置 |
| `ADMIN_COOKIE_SECURE` | 本地HTTP设false，生产HTTPS必须true |
| `VOLCENGINE_ACCESS_KEY / VOLCENGINE_SECRET_KEY` | 素材和真实项目校验，仅服务端使用 |
| `VOLCENGINE_ASSET_ACCOUNTS_JSON` | 按业务 Key ID 和准确 ProjectName 隔离素材 AK/SK，可选独立火山推理渠道；默认空，其他 Key、供应商、IAM 校验和 TOS 沿用原配置 |
| `TOS_*` | 文件中转 |
| `MULTI_PROVIDER_ENABLED` | 模型中转开关，默认false |
| `PROVIDER_CREDENTIAL_ENCRYPTION_KEY` | 凭证独立主密钥；留空使用持久化密钥文件 |

首次初始化唯一超级管理员，再启动服务：

```powershell
uv run python -m app.admin_cli create --username admin --display-name "系统管理员"
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

初始密码只显示一次；首次登录需改密并绑定TOTP，不要把密码、二维码或恢复码写入README或Git。

### 3. 启动内部控制台

另开终端，在仓库根目录执行：

```powershell
Copy-Item .env.example .env.local
npm ci
npm run dev -- --host 127.0.0.1 --port 3001
```

打开 `http://localhost:3001`。管理请求固定访问同源 `/api/internal/*`；开发代理默认指向 `http://127.0.0.1:8000`，可用 `CONSOLE_API_PROXY_TARGET` 调整。`NEXT_PUBLIC_API_BASE_URL` 只是业务接口调试地址，不用于管理员跨域认证。

### 4. 接入第一个客户

1. 在火山创建客户项目，取得真实ProjectName，并开通对应上游权限。
2. 普通管理员在本系统创建项目，服务端校验该火山项目确实存在。
3. 超级管理员进入“供应商渠道”，为该客户录入上游凭证。
4. 普通管理员在“项目模型”选择渠道并启用模型，设置约定额度和可选计费规则。
5. 生成并安全交付业务Key、API地址和接口文档。项目下现有有效Key共享已开通模型权限。
6. 客户先查询模型再调用；真实生成会消耗上游额度，必须经过授权。

BytePlus和ModelTop使用各自平台的独立Key，不复用国内方舟Key。映射、权限及计价见 [BytePlus](backend/BYTEPLUS_MODELARK.md)、[ModelTop](backend/MODELTOP_RELAY.md) 和 [方舟目录](backend/VOLCENGINE_MODEL_CATALOG.md)。

<details>
<summary>可选：启动客户工具</summary>

```powershell
cd user-portal
Copy-Item .env.example .env.local
npm ci
npm run dev
```

默认地址 `http://localhost:3002`，只使用业务Key。详见 [客户工具说明](user-portal/README.md)。生产客户工具当前关闭，不应为更新控制台而重新启用。

</details>

## 客户接入

完整契约见 [客户API](backend/CLIENT_API.md) 或 [HTML阅读版](backend/CLIENT_API.html)。以下Bash示例只查询目录和价格，不生成媒体：

```bash
export BASE_URL="https://api.richbest.cn"
export API_KEY="替换为管理员签发的业务Key"

curl "$BASE_URL/api/auth/me" -H "Authorization: Bearer $API_KEY"
curl "$BASE_URL/v1/models" -H "Authorization: Bearer $API_KEY"
curl "$BASE_URL/v1/pricing" -H "Authorization: Bearer $API_KEY"
```

| 能力 | 入口 |
|---|---|
| 对话 / Responses | `POST /v1/chat/completions`、`POST /v1/responses` |
| 图片 | `POST /v1/images/generations` |
| 视频创建、查询、取消 | `/api/v3/contents/generations/tasks*` |
| 文本与多模态向量 | `/v1/embeddings`、`/v1/embeddings/multimodal` |
| 语音合成、异步识别、音频生成 | `/v1/audio/*` |
| 素材上传、登记与管理 | `/api/asset/*`、`/api/asset-group/*` |

使用 `/v1/models` 返回的公开id，不猜测上游Model ID。专用翻译模型不能一律按流式Chat调用。

长耗时生成不能套用30秒总超时。断线不代表未执行；图片/视频应保留业务Key、原请求体及原 `Idempotency-Key`，不要换键自动重试。见 [错误与安全重试](backend/docs/client-api/errors.md)。

## 安全与运维

### 认证与角色

- 管理员使用HttpOnly Session和CSRF；业务接口只接受Bearer业务Key。旧共享管理员令牌不具备权限。
- 超管由CLI初始化，控制台只能创建普通管理员。禁用、改密、重置和换绑按规则撤销相关会话。
- 手机先安装可信Authenticator。TOTP确认换绑前旧设备有效；确认后旧验证器和旧恢复码失效，新恢复码只显示一次。
- 免TOTP登录默认关闭，开启需密码、新动态码及风险确认，不取消敏感操作原有再认证。
- 登录来源IP用于审计，不是管理员登录白名单。QPM限流不等于完整客户IP白名单。
- 供应商凭证和SMTP授权码不回显；浏览器不持久化管理Secret。

### 备份、邮件与日志

- SQLite及管理员审计默认每24小时备份，保留30组。恢复前校验、建立回滚点和维护锁；恢复成功撤销全部管理Session。
- `admin_totp.key`、`provider_credentials.key`、`smtp_settings.key`或对应部署Secret需分别安全备份，数据库快照不能代替主密钥备份。
- 磁盘采样由后端独立运行；默认预警/严重/紧急80%/90%/95%、恢复线75%，告警去重、升级、恢复和失败重试均由后台执行。
- “磁盘与邮件”支持QQ、163、126、Yeah及自定义SMTP，最多10套发件账号、其中一套生效，最多50个混合服务收件人。SMTP授权码不是登录密码；原账号编辑留空可保留授权码。
- 邮箱编辑是草稿，保存后才生效。测试邮件会真实发送；旧配置迁移及密钥保护见 [部署说明](backend/DEPLOYMENT.md#5-控制台连接)。只发送磁盘告警，不发送登录邮件。
- 邮箱绑定可选：可删除所有发件邮箱或收件人并保存，系统自动停用邮件通知，磁盘监控继续运行。保存仍需超级管理员当前密码；重新绑定后需主动开启邮件通知。
- 调用明细保留近30天；累计请求和异常不随明细过期归零。文本内容经安全脱敏，仍可能含客户业务资料；导出文件按敏感数据保管。

### 控制台加载

项目、Key、概览和额度区域分别处理响应，慢接口不阻止已成功列表显示。退出或重新登录会阻止旧请求回填。最近请求按倒序rowid取8条，避免在大日志表上先排序全部命中记录；不改变累计口径与鉴权。

## 生产部署

配置见 [Compose](deploy/volcengine/compose.yaml)，详细步骤见 [部署说明](backend/DEPLOYMENT.md)。该文件含服务器路径和证书挂载，**不是无需配置的一键通用模板**。

发布顺序：**本地测试 → 一致性备份和密钥保护 → 新Release构建 → 隔离预检 → 只替换变更服务 → 健康与鉴权检查 → 必要时平滑reload网关 → 保留回滚点。**

- HTTPS网关提供控制台、业务API及健康检查；管理端保持同源，生产Cookie必须Secure。
- 模型中转默认关闭，启用开关不会自动为客户创建渠道或绑定模型。
- 持久卷覆盖数据库及独立密钥；当前采用单实例SQLite，不能直接共享文件横向扩容。
- API发布前排空在途生成及后台生图；不能把前端断线当作供应商取消。
- 只更新控制台时不重建API或客户工具；API更新保留项目、Key、渠道、邮件和历史账单。
- 微信支付回调只透明代理到LocalMiniDrama，本系统不验签、不解密、不处理订单。回调不能要求控制台登录；见 [回调契约](deploy/volcengine/MINIDRAMA_WECHAT_CALLBACK_PLAN.md)。

旧版镜像和切换记录已移至 [历史发布记录](docs/DEPLOYMENT_HISTORY.md)，避免把某天版本误写成永久的生产现状。

## 测试与开发

```powershell
# 仓库根目录：后端覆盖率 + 控制台单测/SSR + lint
npm run test:all

# 分项检查（根目录）
npm test
npm run lint
npm run build

# 后端（独立终端）
cd backend
uv run --extra test pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90
```

客户工具另在 `user-portal` 目录执行 `npm test`。自动测试必须使用临时SQLite、MockTransport、模拟TOS和SMTP，不能访问真实上游或发送真实邮件。覆盖率门槛90%，音视频测试需FFmpeg工具。

文档能力校验前运行 `npm ci --prefix tools/client-docs`。`npm run docs:build`生成HTML及控制台静态文档；`npm run docs:check`检查同步与链接，`npm run docs:test`验证阅读器。详见 [文档工具](tools/client-docs/README.md)。

## 文档导航

### Vega 统一渠道

同一个 `vega` 渠道支持5个Vega视频版本、3个 `vega-image-1.0-{lite,flash,pro}` 图片型号，以及3个 `wand-vega-vl-1.0-{lite,flash,pro}` 多模态理解型号。视频/图片使用腾讯云VOD的TC3签名，VL使用同渠道加密保存的API Token，不创建额外的VL供应商。

- `.env` 无Vega专属新增项；只需现有 `MULTI_PROVIDER_ENABLED=true`。超级管理员在“供应商渠道”选择Vega，填写国内/国际站、SubAppId、视频/图片的SecretId与SecretKey，以及VL的API Token；按需填写，轮换留空项保留原值。不要重新生成已有的 `PROVIDER_CREDENTIAL_ENCRYPTION_KEY` 或 `provider_credentials.key`。
- 普通管理员为项目选择Vega渠道并启用模型，客户使用原业务Key即可。视频复用本站 `/api/v3/contents/generations/tasks`；图片推荐 `/v1/images/tasks` 创建和查询，也支持 `/v1/images/generations` 同步兼容；VL使用 `/v1/chat/completions`。视频Pro素材/活体接口为 `/v1/vega/{action}`，按项目、Key、渠道隔离，不能引用火山素材ID。
- 图片Lite单次1张，Flash/Pro最多4张，以实际返回张数结算；视频Pro系列当前仅开放已验证的mp4输出，具体格式通过模型`outputFormats`查询。不支持的张数或格式在本站返回422，避免提交后才被供应商拒绝。
- 全局价目按所提供刊例价初始化：国内站人民币，视频及VL国际站美元按2026-10-22汇率快照 `6.689327` 折算人民币；图片国际站价格未提供，需人工补齐。已生效价格可由超级管理员统一调整；未启用项目不会自动计费。视频按实际秒数、图片按实际输出张数及Pro超额输入、VL按真实输入/输出/缓存Token结算；缓存不重复计算，缺失用量或价格进入待计价。
- 概览Token优先采用供应商总量；未返回总量时汇总已有的真实输入/输出分项，并提示未返回完整Token的调用。完全未知时显示“—”，不将未知值当零、不按秒数估算；历史用量无需回填。素材组名最多64字符，首次自动建组使用短名称，不覆盖已有组名。
- 素材/活体创建失败会保存错误和结果是否未知，同一幂等键不重复调用供应商；重启时，无上游ID的残留创建转为待核对，不继续假排队，也不推断供应商一定未创建。
- 腾讯云需先开通VOD并申请模型权限，默认通常仅1路并发；渠道“测试”不会自动发起付费生成。真实调用验收需单独授权。素材/活体及供应商存储收费不属于视频自动账单。

客户参数和示例见 [Vega视频与素材](backend/docs/client-api/vega.md)、[Vega图片与VL](backend/docs/client-api/vega-image-vl.md)，渠道配置、计量与本地验收见 [Vega接入说明](backend/VEGA_RELAY.md)。图片推荐异步任务，VL支持Chat JSON/SSE但不支持Responses；国际站图片价格未提供，需手动配置，未配置不按零元结算。

| 我需要… | 文档 |
|---|---|
| 客户字段、示例与错误码 | [客户API](backend/CLIENT_API.md) · [HTML](backend/CLIENT_API.html) · [模型中转](backend/MODEL_RELAY_API.md) |
| 模型映射与验收 | [方舟目录](backend/VOLCENGINE_MODEL_CATALOG.md) · [BytePlus](backend/BYTEPLUS_MODELARK.md) · [ModelTop](backend/MODELTOP_RELAY.md) |
| 项目计费与账单 | [计费API](backend/ADMIN_BILLING_API.md) |
| 部署和配置 | [生产部署](backend/DEPLOYMENT.md) · [后端样例](backend/.env.example) |
| 漫剧及回调对接 | [RichiDrama](backend/RICHIDRAMA_RELAY_ALIGNMENT.md) · [回调网关](deploy/volcengine/MINIDRAMA_WECHAT_CALLBACK_PLAN.md) |
| 额度及设计方案 | [额度与素材账本](backend/RISK_CONTROL_PLAN.md) · [多供应商设计](backend/MULTI_PROVIDER_RELAY_PLAN.md) |
| 前端及截图维护 | [客户工具](user-portal/README.md) · [截图说明](docs/screenshots/README.md) |
| 发布历史与开发约定 | [发布记录](docs/DEPLOYMENT_HISTORY.md) · [AGENTS.md](AGENTS.md) |

设计计划只是历史方案，当前行为以代码、测试、示例配置及客户API契约为准。

---

结构调研参考：[New API](https://github.com/QuantumNous/new-api)的截图及能力分区、[One API](https://github.com/songquanpeng/one-api)的部署与使用导航、[Portkey Gateway](https://github.com/Portkey-AI/gateway)的最短接入路径。只借鉴信息组织，不移植其他项目的功能承诺或许可声明。
