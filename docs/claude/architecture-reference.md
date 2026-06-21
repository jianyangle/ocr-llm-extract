# Architecture Reference

从 CLAUDE.md 抽出的详细子系统架构描述。领域术语（Task Queue Run、OCR Source、PDF Page Work Unit、Extract Completion Reducer、Markdown OCR Text 等）的权威定义见 `CONTEXT.md`；架构决策见 `docs/adr/`。

## 数据流

```
用户输入 (图片 / PDF / 文本)
  │
  ▼  TaskEngine.start() → TaskOrchestrator（流式 OCR↔Extract 流水线，ADR-0001）
  │
  ├─ OCR Stage（独占 worker）─────────────────────────────────────────────
  │    OCR Source 二分（RoutingOCRService 依 ocr_use_online 解析）：
  │      • Local OCR  → PaddleOCRService → TBPU 排版后处理（旋转→排序→段落→分隔符，
  │                      跨栏 \t 防串扰 ADR-0008）+ Adaptive Retry
  │          PDF 分支 → PDFPageProcessor 逐页 render/OCR（text-layer / raster /
  │                      Hybrid Page Render，ADR-0009）流式产出 PDFPageWorkUnit；
  │                      PDFOCRAggregator 聚合并持有 Retry Page Budget（ADR-0005）
  │      • Online OCR → OnlineOCRService（百度 AI Studio 异步 job，ADR-0011）
  │                      故意绕过本地文本质量管线（ADR-0010）；可携带 Markdown OCR Text
  │          PDF 分支 → 在 OCRStage._recognize_pdf 的 PdfOcrRun 选择点整篇提交为一个 job（ADR-0012），
  │                      完全绕过 PDFPageProcessor（无逐页 render / Hybrid / region rescue）
  │    └─▶ 写入 Stage Buffer（深度 2 的 FIFO，满则 OCR Stage 阻塞）
  │
  ├─ Extract Stage（独占 worker，消费 Stage Buffer）──────────────────────
  │    只用唯一的 Active Extraction Template（主窗口下拉选定，ADR-0007；无路由）
  │      • Extraction Input = flat OCR Text + 可选 Markdown OCR Text
  │      • Builtin 模板带 line_rules 且为 flat-text → TemplateExtractor 逐行 regex 抽明细
  │        （Markdown OCR Text 在场则跳过 line rules，ADR-0013）
  │      • Chunker 分段（flat 多轮 / markdown 覆盖式）
  │      • PromptBuilder + SchemaBuilder 构建 prompt 与 JSON Schema
  │      • LLMExtractor → provider_openai / provider_ollama（REST，无 SDK）
  │      • OutputNormalizer 解析 LLM JSON（strict/balanced/aggressive）
  │      • Grounding 原文溯源（五级分类）+ field-region rescue（仅 Local PDF）
  │      • ExtractCompletionReducer 归约 page/item 完成态 → terminal task outcome（ADR-0006）
  │
  ▼  EventPublisher 广播 typed EngineEvent（保留 EVT-TASK-00x 日志兼容 code）
  │
  ├─ UI：EventSink → 事件翻译 → Task Review Projection → Qt 主窗口（ADR-0004）
  └─ ExcelWriter 写入 xlsx
```

## 核心分层

- **`src/domain/schemas.py`** — 所有数据模型（`AppConfig`, `TaskItem`, `ExtractRow`, `ExtractionInput`, `ExtractionOutcome`, `ItemResult`, `WriteSummary`, `GroundedCell`, `GroundedExtractRow`, `ColumnSpec`, `FieldGroup`, `FieldRegion`, `LineRules`, `PDFPageOCRSnapshot`, `PDFPageExtractResult`, `PDFPageWorkUnit`, `PageError` 等），纯 dataclass，无业务逻辑
- **`src/core/task_engine.py`** — `TaskEngine` 是引擎的 Facade，管理任务队列状态（add/delete/pause/resume/retry/start），内部委托给 `TaskOrchestrator`
- **`src/core/task_pipeline.py`** — 流水线协作类：
  - `TaskOrchestrator` — 顺序处理 pending 任务，协调 OCR Stage → Extract Stage 流水线、Stage Buffer 背压、Engine Event 发布时机；OCR/Extract 的 per-item body 已外移，只在锁内应用状态突变、reducer 和事件发布
  - `SourceProcessor` — 处理 text/image/pdf 来源的 OCR 识别，并保留委托给 `PDFOCRAggregator` 的旧 PDF 兼容 façade；Online OCR PDF 分叉由 `OCRStage._recognize_pdf` 的 `PdfOcrRun` 选择点负责
  - `ResultStore` — 结果行的 upsert、序列化、构建
  - `EventPublisher` — typed `EngineEvent` 事件广播；保留 `EVT-TASK-001~006` 作为日志兼容 code，通过单参 `event_sink(event)` 通知 UI
- **`src/core/ocr_stage.py`** — `OCRStage` 是 OCR Stage 的 per-item seam：text 直接 passthrough，image 产出 `ImageRecognized`，PDF 以 generator 产出 `PdfDocStarted` / `PageCommitted` / `DocOcrCompleted` / `OCRFailure` 等语义 output；不持有队列、不发布事件、不修改任务完成态，由 `TaskOrchestrator` 负责扇出和排序
- **`src/core/extract_stage.py`** — `ExtractStage` 是 Extract Stage 的 per-item seam：`extract_one()` 将已 OCR 的工作项映射成 `ExtractResult`（success_rows / rescue_events / extraction_input / error），止于 reducer 之前；field rescue、text-layer attribution sidecar、typed rows finalize 保持在该模块内，任务完成归约仍由 `TaskOrchestrator` 执行
- **`src/core/engine_events.py`** — 冻结的 typed `EngineEvent` dataclass 定义（`TaskPageResultStreamed` / `TaskSucceeded` / `TaskFailed` 等）；进程内消费者按 dataclass 类型 dispatch，不解析裸事件码
- **`src/core/pdf_page_processor.py`** — `PDFPageProcessor` 是 Local OCR 的 PDF 页级处理 module，接收纯值输入并按页流式 yield `PDFPageWorkUnit`；每页选 text-layer / raster / Hybrid Page Render 策略；页级错误写入 `PageError`，文档级错误抛 `PDFDocumentError`。不拥有 LLM 抽取、field-region rescue、进度事件、任务状态变更
- **`src/core/pdf_ocr_aggregator.py`** — `PDFOCRAggregator` 是单个 PDF 文档在 OCR Stage 内的聚合边界（ADR-0005）；驱动 `PDFPageProcessor`，持有该文档的 Retry Page Budget，聚合 `PDFPageOCRSnapshot`，产出 PDF 级 OCR completion value object。接收窄 PDF/OCR 输入而非完整 `AppConfig`，区分页级失败（作为数据返回）与文档级失败（抛出）
- **`src/core/online_pdf_processor.py`** — Online OCR 的 PDF 处理路径：整篇文档提交为一个百度异步 job，流式映射每页结果为 `PDFPageOCRSnapshot`（ADR-0011/0012），绕过 `PDFPageProcessor`

## Extract 子系统 (`src/extract/`)

> **注意**：关键词/优先级路由机制已整套移除（ADR-0007）。不存在 `template_router.py`，`PromptTemplate` 也不再有 `keywords`/`exclude_keywords`/`priority` 字段。运行时只使用操作员选定的单一 Active Extraction Template。

- **`llm_extractor.py`** — 核心抽取器 Facade。`extract_detailed()` 返回 `ExtractionOutcome`，显式携带 `rows` / `column_specs` / `field_regions`。消费 `ExtractionInput`（flat OCR Text + 可选 Markdown OCR Text）：flat-text 模式下若模板带 `line_rules`，先跑 `template_extractor` 逐行抽取命中明细，未命中文本再交 LLM 回填"父字段"；markdown 模式跳过 line rules。支持多轮抽取（`extraction_passes`），跨轮去重
- **`chunker.py`** — 文本分段。`split_passes()` 按 `max_char_buffer` + `passes_increment` 切分 flat 文本供多轮抽取；`split_markdown_passes()` 对 Markdown OCR Text 做覆盖式切分（表格为不可分原子，`extraction_passes` 不放大轮数）
- **`prompt_builder.py`** — Prompt 构建器。定义 `PromptTemplate` dataclass（name/description/examples + 应用私有规则字段 `columns`/`line_rules`/`field_regions`/`field_groups`/`exclusive_group_pairs`/`min_lines`/`max_lines`/`min_confidence`）和 `build_messages()`（`markdown_input` 标志切换 markdown 适配 prompt）
- **`template_catalog.py`** — Extraction Template Catalog 实现。持有 Builtin / User 模板身份、Builtin Template Override（仅存 name/prompt/examples）、reset/rename/delete、保存前校验、Active Extraction Template 指针。持久化在 `config.json` 的 `templates` 列表 + `active_template_id`，无独立目录文件
- **`template_extractor.py`** — 行级 regex 抽取器。按 `LineRules`（start/end/line/first_line/last_line/skip_line）状态机逐行扫描，返回 `LineExtractResult(matched, rows, unmatched_text)`，供 `llm_extractor` 与 LLM 回填协同
- **`schema_builder.py`** — JSON Schema 构建器。`build_rows_schema()` 根据 examples 列数生成 structured output 的 JSON Schema
- **`builtin_templates.py`** — 内置 `PromptTemplate`：`sigcard`（名片）、`invoice`（发票，带 `line_rules`/`field_regions`/`columns`/`field_groups`）。这些是应用私有规则字段，操作员不可见、不可编辑
- **`type_inference.py`** — 从 examples 推断列类型（phone/email/company 等），运行时使用但不持久化为用户模板字段
- **`field_types.py`** — 字段值归一化与等价判定（电话 `normalize_phone`、邮箱等），用于去重与 grounding
- **`region_attribution.py`** — text-layer 归属 sidecar：用几何阈值（已用反例校准，禁止运行时学习/暴露配置）判定字段角色归属（如购销方对调），与 Hybrid Page Render（内容完整性）正交
- **`provider_openai.py`** / **`provider_ollama.py`** — LLM HTTP 适配器，通过 `requests` 直接调用 REST API（未用 SDK）
- **`provider_catalog.py`** — provider 注册表，`get_provider_entry()` 按 `provider` 字段选适配器
- **`output_normalizer.py`** — 解析 LLM 返回 JSON，支持 strict/balanced/aggressive 三种模式，处理 markdown code fence、全角字符修复、trailing comma 等
- **`grounding.py`** — 将抽取结果与 `ExtractionInput` 的 **flat OCR Text**（永不对齐 Markdown OCR Text）对齐，五级 cell 分类：
  - `ASSIGNED` — 精确匹配（全文/去空格/仅字母数字）
  - `ASSIGNED_PARTIAL` — 合并多段 cell 中部分命中
  - `INFERRED` — 规则推断匹配
  - `INFERRED_FUZZY` — 模糊匹配（阈值 `grounding_fuzzy_threshold`，默认 0.75）
  - `UNCERTAIN` — 无法定位
  - 合并多段内容的 cell（如 `A；B`）按分隔符拆段独立匹配，全部命中才判 ASSIGNED
  - `classify_row()` 行级判定：前两列为 key cells，要求其全部 ∈ {ASSIGNED, ASSIGNED_PARTIAL} 且其它非空 cell 亦在允许状态内才判 `ASSIGNED`；否则 `INFERRED`；所有 key cell 为空则 `UNCERTAIN`
- **`example_parser.py`** — 解析/格式化 examples 文本与二维数组互转
- **`connection_check.py`** / **`network_diagnostics.py`** / **`model_fetcher.py`** / **`ollama_url.py`** — LLM 连接探测、网络诊断、模型列表拉取、Ollama URL 规整等设置辅助

## OCR 子系统 (`src/ocr/`)

OCR Source（Local / Online 二分）由 `RoutingOCRService` 依 `ocr_use_online` 解析；详见 ADR-0010/0011/0012。

- **`routing_service.py`** — `RoutingOCRService`：唯一注入的 OCR 服务，按 `ocr_use_online` 在 Local / image-region Online 之间路由，二者共用 `recognize()` duck 接口；Online OCR PDF 不经过该 `recognize()` 路由，而在 `OCRStage._recognize_pdf` 的 `PdfOcrRun` 选择点分叉
- **`models.py`** — OCR 数据模型：`OCRTextBlock`（含 text/score/box/end）和 `OCRResult`（整页结果，含置信度统计与 Adaptive Retry 状态）
- **`paddle_service.py`** — 封装 PaddleOCR 3.2.0，支持 fast/balanced/accurate profile，Adaptive Retry（低置信度时升半档 profile），OCR 后交 TBPU 排版后处理，模型在 `models/`。是 Local OCR 与 Adaptive Retry 的 owner
- **`pdf_adapter.py`** — 用 pypdfium2 读 PDF：优先抽原生文本层（`get_text_bounded`），失败则按 `pdf_render_dpi` 逐页栅格化
- **`online_service.py`** — `OnlineOCRService`：在线 OCR 服务封装，image 任务仍符合 `recognize()` duck 接口；委托 `OnlineOCRClient` 调百度异步 job API
- **`online_client.py`** — 百度 AI Studio 托管 PaddleOCR 异步 job 客户端（submit job → poll state → stream 每页结果，`bearer` 认证，`model` 可选）
- **`markdown_normalizer.py`** — 将 VL 模型返回的含 HTML 表格的 markdown 归一化为轻量 markdown（pipe 表格、剥样式）；产出 Markdown OCR Text，保持 cell 文本与 flat 文本一致以便 grounding（ADR-0013）
- **`online_catalog.py`** / **`online_model_presets.py`** — 在线平台/模型 preset 与模块支持矩阵（含 `is_vl_family` 等）
- **`online_connection_check.py`** — 在线 OCR 连接探测
- **`region_geometry.py`** — region/bbox 几何工具（field-region rescue 与归属用）
- **`tbpu/`** — 文本块排版后处理子包（Text Block Post-processing Unit），算法移植自 Umi-OCR（MIT, hiroi-sora）：
  - `__init__.py` — `Tbpu` 基类、`Parser` 注册表、`get_parser()` 工厂（含旧值兼容映射）
  - `parsers.py` — 八种排版解析器：`MultiNone`/`MultiLine`/`MultiPara`（多栏）、`SingleNone`/`SingleLine`/`SinglePara`/`SingleCode`（单栏）、`ParserNone`（透传）
  - `gap_tree.py` — 行分组与阅读顺序排序算法
  - `line_preprocessing.py` — 整页旋转预处理（角度检测、归一化 bbox）
  - `paragraph_parse.py` — 段落边界判定（行间距阈值）
  - `separators.py` — 块间分隔符推断（CJK 间无空格、连字符合并、跨栏大间距插 `\t` 防串扰）
- 模型目录：`PP-OCRv5_mobile_det`(检测), `PP-OCRv5_mobile_rec`(识别), `PP-LCNet_x1_0_textline_ori`(文本行方向), `PP-LCNet_x1_0_doc_ori`(文档方向), `UVDoc`(去畸变)

## UI 层 (`src/ui/`)

UI 不直接解析裸事件码；它消费 typed `EngineEvent`，翻译为 projection 命令后驱动 Task Review Projection（ADR-0004）。

- **`main_window.py`** — PySide6 主窗口，含 `MainWorkbenchController`（连接 TaskEngine 与 UI）和完整 widget 组合（Task Queue、Result Review、Inspector、状态栏），以及 Active Extraction Template 下拉
- **`task_review_projection.py`** — Task Review Projection：Task Review 显示态的 owner，用 `task_id`/`page_index`/`row_id` 标识选择（非 row index），负责 Task Queue / Result Review / Inspector 选择一致性；持有 stream 结果缓存与 OCR Text 缓存
- **`task_review_event_translator.py`** — 将 typed `EngineEvent` 翻译为语义化的 projection 命令（替换/追加结果行、记录 OCR Text 等）
- **`task_review_coordinator.py`** — 协调 EventSink → 翻译 → projection → Qt adapter 的更新流
- **`settings_dialog.py`** — 设置对话框，含 `SettingsController`（配置加载/校验/保存）与 Extraction Template 管理 tab（左列表 + 右编辑器，new/delete/reset）
- **`settings_validation.py`** — 设置校验：`SettingsFormData` dataclass 与 `build_validated_config()`（Operator Settings Module 边界，校验 Settings Draft → merge 进 `AppConfig`）
- **`theme.py`** / **`icon_loader.py`** / **`styled_button.py`** / **`table_delegates.py`** — Refined Light 主题、图标加载、按钮、表格委托等表现层组件

## IO 层 (`src/io/`)

- **`config_store.py`** — `ConfigStore` 低层配置持久化到 `~/.ocr_extract_app/config.json`，带类型强制转换（不拥有 operator settings 语义）
- **`excel_writer.py`** — 追加写入 xlsx，自动去重文件名
- **`log_store.py`** — 事件日志写入 `~/.ocr_extract_app/logs/`，自动脱敏 api_key

## 运维与发布 (`src/ops/`, `src/release/`)

- **`perf_reliability.py`** — 性能/可靠性自动化评估套件
- **`ocr_accuracy_eval.py`** — OCR 准确率评估
- **`package_release.py`** — PyInstaller 打包命令构建 + release manifest 生成
</content>
