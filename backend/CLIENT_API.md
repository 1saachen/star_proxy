# 星辰平台 API 文档

版本：6.4.0 · 更新日期：2026-10-23

统一调用文本、图片、视频、向量和音频模型，并管理项目素材。正式地址：`https://api.richbest.cn`。

素材账号由服务端按已认证业务 Key 的 ID 和项目选择，客户请求不能覆盖该选择。未配置隔离项的 Key 保持原账号。若隔离项与 Key 所属项目不一致，接口返回 `503 asset_account_scope_mismatch`，不会回退到其他账号。此项默认不改变生成渠道，也不改变 TOS、Key 额度或已有接口字段；服务端迁移账号时须同时准备素材 ID 映射，旧账号 ID 不会自动转换。

管理员可同时为该 Key 指定独立的火山推理渠道，仅对其新请求生效；已有任务保留原渠道，其他 Key 和供应商保持原样。指定渠道不可用时返回 `503 account_channel_unavailable`。客户仍使用原业务 Key 与原接口，无需传入账号或渠道选择参数。

你只需要管理员签发的业务 API Key。需要使用哪个模型，请联系管理员为项目开通；项目下现有有效 Key 无需重新签发。可用型号及能力以 `/v1/models` 为准，人民币税前价目以 `/v1/pricing` 为准。

[HTML 阅读版](CLIENT_API.html) · [模型与接口对照表](docs/client-api/models.md#模型与接口对照表) · [错误与安全重试](docs/client-api/errors.md)

国内 `doubao-seedance-2.5` 新增 [Draft 样片转成片](docs/client-api/videos.md#seedance-25-draft样片转成片)：通过同一创建接口先生成 480p 样片，选定后引用本站样片 ID 生成 1080p 成片；两次调用分别记录真实 Token 用量。是否已开通以 `/v1/models` 的 `draft` 能力为准，BytePlus、ModelTop 和 Vega 不自动支持此流程。

新增[Vega视频与素材](docs/client-api/vega.md)：5个视频版本统一使用本站视频创建/查询接口；[Vega素材与活体接口](docs/client-api/vega-materials.md)提供10个独立操作的参数和示例，位于“Vega → 素材接口”，使用 `POST /v1/vega/{action}`。Vega与火山素材ID不能混用；调用方始终只使用业务Key。

新增[Vega-Image 与 Vega-VL](docs/client-api/vega-image-vl.md)：各3个型号，共用同一个Vega渠道。图片支持异步任务及同步兼容入口，VL支持多模态Chat JSON/SSE；不支持VL Responses。

## 快速开始

### 准备业务 Key

以下 curl 示例使用 Bash，在你自己的服务器执行。不要将 Key 放进浏览器、公开仓库或 URL。

```bash
export BASE_URL="https://api.richbest.cn"
export API_KEY="替换为管理员签发的业务Key"

curl "$BASE_URL/api/auth/me" \
  -H "Authorization: Bearer $API_KEY"
```

成功返回 `authenticated=true` 和 `apiKeyId`。完整约定见[鉴权与通用约定](docs/client-api/basics.md)。

### 查询可用模型和价格

查询不会触发模型生成，先确认项目已开通，再选择接口：

```bash
curl "$BASE_URL/v1/models" \
  -H "Authorization: Bearer $API_KEY"

curl "$BASE_URL/v1/pricing?model=glm-5.2" \
  -H "Authorization: Bearer $API_KEY"
```

使用 `data[].id` 作为 `model`，使用 `display_name` 展示名称。目录为空表示项目暂无可用模型，不代表 Key 无效。价格字段是十进制字符串，未配置价格不代表免费。

### 发起第一次模型调用

下面的对话会产生真实用量；项目已开通 `glm-5.2` 才能使用。换模型前先检查 `modality` 和 `capabilities`。

```bash
curl "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "用一句话介绍你自己"}],
    "stream": false
  }'
```

从 `choices[0].message.content` 读取回答。需要流式输出时设置 `stream=true`，逐段读取 SSE，见[文本流式示例](docs/client-api/text.md#chat-completions)。

## 按模型查阅

在左侧先展开供应商，再选择模型，即可查看该供应商下的调用示例、参数、结果和限制。同名模型可出现在不同供应商下；实际供应商由项目绑定决定，可用 `/v1/pricing` 中同名项的 `provider` 核对。所有模型仍通过星辰平台业务 Key 调用。

| 模型系列 | 接口文档 |
|---|---|
| GPT Image | [gpt-image-2](docs/client-api/model-reference.md#gpt-image-2) · [2.5 Flare](docs/client-api/model-reference.md#gpt-image-25-flare) · [2.5 Sunburst](docs/client-api/model-reference.md#gpt-image-25-sunburst) |
| DeepSeek | [V4 Flash](docs/client-api/model-reference.md#deepseek-v4-flash) · [V4 Pro](docs/client-api/model-reference.md#deepseek-v4-pro) |
| GLM | [GLM 5.2](docs/client-api/model-reference.md#glm-52) |
| Doubao Seed | [2.1 Pro](docs/client-api/model-reference.md#doubao-seed-21-pro) · [2.1 Turbo](docs/client-api/model-reference.md#doubao-seed-21-turbo) · [完整模型目录](docs/client-api/models.md#模型与接口对照表) |
| Seedream 图片 | [5.0 Pro](docs/client-api/model-reference.md#doubao-seedream-50-pro) · [5.0 Lite](docs/client-api/model-reference.md#doubao-seedream-50-lite) · [Dola 5.0 Flash](docs/client-api/model-reference.md#dola-seedream-50-flash) |
| Seedance 视频 | [2.5](docs/client-api/model-reference.md#doubao-seedance-25) · [2.0](docs/client-api/model-reference.md#doubao-seedance-20) · [Dreamina 2.5](docs/client-api/model-reference.md#dreamina-seedance-25) |
| Wan / MiniMax 视频 | [wan3.0-video](docs/client-api/model-reference.md#wan30-video) · [minimax-h3](docs/client-api/model-reference.md#minimax-h3) |
| 向量 | [doubao-embedding-vision](docs/client-api/model-reference.md#doubao-embedding-vision) |
| 语音与音频 | [语音合成](docs/client-api/model-reference.md#doubao-seed-tts-20) · [录音识别](docs/client-api/model-reference.md#doubao-seedasr-20) · [音频生成](docs/client-api/model-reference.md#doubao-seed-audio-10) |
| Vega 图片 / 多模态理解 | [Vega-Image / Vega-VL 参数和限制](docs/client-api/vega-image-vl.md) · [Vega 视频](docs/client-api/vega.md) |

[查看全部模型的独立接口参考](docs/client-api/model-reference.md) · [查询可用模型与价格](docs/client-api/models.md)

## 素材接口

素材按供应商分别查阅，均使用星辰平台业务 Key，不能跨供应商混用资源ID：

- [火山素材](docs/client-api/assets.md)：位于“火山引擎 → 素材接口”，原有11项操作不变。上传与登记分两步，素材状态为 `Active` 后才可用于任务。
- [Vega素材与活体](docs/client-api/vega-materials.md)：位于“Vega → 素材接口”，包含登记、任务查询、素材/组管理及真人活体10项操作；仅支持视频Pro系列，素材任务完成且ErrCode=0后才可引用。

| 操作 | 文档 |
|---|---|
| 上传图片、视频、音频 | [上传文件](docs/client-api/assets.md#上传本地素材) |
| 火山素材组管理 | [创建](docs/client-api/assets.md#创建素材组) · [列表](docs/client-api/assets.md#查询素材组列表) · [详情](docs/client-api/assets.md#查询单个素材组) · [修改](docs/client-api/assets.md#修改素材组) · [删除](docs/client-api/assets.md#删除素材组) |
| 火山素材管理 | [登记](docs/client-api/assets.md#登记素材) · [列表](docs/client-api/assets.md#查询素材列表) · [状态](docs/client-api/assets.md#查询单个素材) · [修改名称](docs/client-api/assets.md#修改素材名称) · [删除](docs/client-api/assets.md#删除素材) |
| Vega素材及真人认证 | [逐项参数与示例](docs/client-api/vega-materials.md#素材流程与接口概览) |
| 流程与媒体限制 | [素材规格及处理流程](docs/client-api/assets.md#素材接口概览) |

## 接口总览

所有路径相对正式 API 根地址。除 `GET /health` 外均使用业务 Bearer Key；JSON 请求携带 `Content-Type: application/json`。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/auth/me` | 验证 API Key |
| `POST` | `/api/asset/upload-file` | 上传图片、视频或音频 |
| `POST` | `/api/asset-group/create` | 创建素材组 |
| `GET` | `/api/asset-group/list` | 查询素材组列表 |
| `GET` | `/api/asset-group/get` | 查询单个素材组 |
| `PUT` | `/api/asset-group/update` | 修改素材组 |
| `DELETE` | `/api/asset-group/delete` | 删除素材组 |
| `POST` | `/api/asset/create` | 登记图片、视频或音频 |
| `GET` | `/api/asset/list` | 查询素材列表和状态 |
| `GET` | `/api/asset/get` | 查询单个素材和状态 |
| `PUT` | `/api/asset/update` | 修改素材名称 |
| `DELETE` | `/api/asset/delete` | 删除素材 |
| `GET` | `/v1/models` | 查询当前业务 Key 可用模型 |
| `GET` | `/v1/pricing` | 查询当前项目可用模型的税前价目 |
| `POST` | `/v1/chat/completions` | OpenAI 兼容文本对话，支持 JSON/SSE |
| `POST` | `/v1/responses` | OpenAI 兼容 Responses，支持 JSON/SSE |
| `POST` | `/v1/images/generations` | OpenAI 兼容图片生成 |
| `POST` | `/v1/images/tasks` | Vega异步图片生成，必须提供幂等键 |
| `GET` | `/v1/images/tasks/{taskId}` | 查询Vega图片任务 |
| `POST` | `/v1/embeddings` | OpenAI 兼容文本向量化 |
| `POST` | `/v1/embeddings/multimodal` | 多模态向量化 |
| `POST` | `/v1/audio/speech` | 文本转语音，直接返回音频二进制 |
| `POST` | `/v1/audio/transcriptions` | 创建异步录音识别任务 |
| `GET` | `/v1/audio/transcriptions/{taskId}` | 查询录音识别任务 |
| `POST` | `/v1/audio/generations` | 根据提示词生成音频 |
| `POST` | `/api/v3/contents/generations/tasks` | 创建统一异步视频任务 |
| `GET` | `/api/v3/contents/generations/tasks/{taskId}` | 查询异步视频任务 |
| `DELETE` | `/api/v3/contents/generations/tasks/{taskId}` | 取消支持取消的视频任务 |
| `POST` | `/v1/vega/CreateAigcMaterial` | Vega登记素材 |
| `POST` | `/v1/vega/DescribeTaskDetail` | 查询Vega素材登记任务 |
| `POST` | `/v1/vega/DescribeAigcMaterial` | 查询Vega素材详情 |
| `POST` | `/v1/vega/DescribeAigcMaterialList` | 查询Vega素材列表 |
| `POST` | `/v1/vega/ModifyAigcMaterial` | 修改Vega素材名称 |
| `POST` | `/v1/vega/DeleteAigcMaterial` | 删除Vega素材或素材组 |
| `POST` | `/v1/vega/DescribeAigcMaterialGroup` | 查询Vega素材组详情 |
| `POST` | `/v1/vega/ModifyAigcMaterialGroup` | 修改Vega素材组 |
| `POST` | `/v1/vega/CreateAigcLivenessValidate` | 发起Vega真人活体认证 |
| `POST` | `/v1/vega/DescribeAigcLivenessValidateResult` | 查询Vega真人活体结果 |

## 调用前必须知道

- 模型授权按项目管理，但图片幂等、视频和录音识别任务访问按业务 Key 隔离。同项目另一枚 Key 也不能查询原 Key 的任务。
- 同名模型可绑定不同渠道，实际能力与价目由项目配置决定。请求不能指定供应商、渠道、Base URL、项目或真实上游模型 ID。
- 文本支持流式；图片支持同步 JSON，Vega另提供异步创建/查询。异步图片及视频创建成功只返回任务 ID，必须轮询至 `succeeded`。
- 图片、视频创建应提前保存 `Idempotency-Key` 和原始请求。超时或断线不代表没有生成、没有扣费；不要换键或开启 SDK 自动重试。
- 长耗时生成不能套用 30 秒总超时，见[等待时间与断线恢复](docs/client-api/errors.md#等待时间)。媒体 URL 可能过期，成功后及时保存。

## 客户系统对接检查表

| 用途 | 接口 | 客户端要求 |
|---|---|---|
| 同步模型目录 | `GET /v1/models` | 使用 `data[].id` 发起请求，使用 `display_name` 展示，不猜测或自行拼接模型名 |
| 查询价格 | `GET /v1/pricing` | 金额按十进制字符串处理；`configured=false` 或价格为空不表示免费 |
| 文本生成 | `POST /v1/chat/completions` 或 `/v1/responses` | 根据模型能力选择接口和流式模式，不把公开别名转换成供应商 Model ID |
| 图片生成 | `POST /v1/images/generations` | Seedream 不发送 `negative_prompt`；只展示当前模型真正支持的参数 |
| 异步视频 | `/api/v3/contents/generations/tasks*` | 只发送 `ratio`，不发送 `aspect_ratio`；保存返回的 `vid_*`，并使用创建任务的同一枚业务 Key 查询或取消 |
| 素材库 | `/api/asset*`、`/api/asset-group*` | 上传与登记分两步；素材变为 `Active` 后再用于生成任务 |

业务 Key 只能保存在客户自己的服务端，不应下发给浏览器或终端用户。请求不得包含 `provider`、供应商 Key、供应商项目名、渠道 ID、Base URL 覆盖值或真实上游模型 ID。图片和视频创建建议使用客户自己的业务记录 ID 构造唯一 `Idempotency-Key`；供应商返回的图片、视频和音频 URL 可能过期，应及时转存到客户自己的媒体存储。
