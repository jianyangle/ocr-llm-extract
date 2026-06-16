# Ollama 抽取精度提升设计：对象键结构化输出

- 日期：2026-06-16
- 状态：已确认，待编写实现计划
- 范围：`src/extract/` 抽取链路，仅影响 Ollama provider

## 背景与问题

上个版本修复了 Ollama 抽取模型的支持，但 Ollama 链路抽取精度不及在线模型。

用户确认的主要失败模式（按突出程度）：

1. **字段错位 / 串列**：值填错列、跨行混合、Tab 分隔区块被拼接。
2. **格式 / 类型错误**：日期、数值、电话等格式不规范。

**不是** 掉行或幻觉问题，因此根因在指令遵循，而非 `num_ctx` 截断或采样参数。

观察到差距时使用的是 **1B–7B 小模型**，指令遵循能力弱。

### 根因

当前 Ollama 与在线模型复用同一套 schema。`build_rows_schema`（`src/extract/schema_builder.py:8`）生成的是**位置数组** schema：每行是 `type: "string"` 的定长数组，列名只存在于 `prefixItems[i].description`。

Ollama 把 JSON Schema 转成 GBNF 语法约束解码时，位置数组只能强制「每行 N 个字符串」：

- **不锚定**哪个值进哪一列 → 弱模型只能靠 prompt 记列序 → 串列。
- 全是 `string`，无类型约束 → 格式错。

在线强模型指令遵循好，位置数组无碍；小模型会漂移。

## 方案

**方案 A 为主，吸收方案 B：** 仅 Ollama 改用「对象键行」结构化输出，从语法层消除串列；格式问题靠 schema 描述 + 既有下游规范化，本期不加激进后处理。

被否决的方案：

- **方案 B（仅 prompt + 启发式后处理）**：治标，位置数组根因仍在，串列只能事后启发式猜，召回有限。
- **方案 C（逐列拆分抽取）**：对弱模型最稳但 N× 耗时，Ollama 并发本就为 1，违反 YAGNI，否决。

## 架构与数据流

仅当 `provider == "ollama"` 且 `use_structured_output == True` 且**列名无重复**时启用对象键行。

```
build schema(对象键, 仅 ollama)
  → Ollama format / GBNF 语法约束解码
  → 对象行 {"姓名": ..., "职位": ...}
  → 按 header 映射回位置数组
  → normalize_rows → ground_rows（下游完全不变）
```

在线链路、grounding、dedupe、Excel 写入**零改动**，无回归面。

## 组件设计

### 1. `schema_builder.build_rows_schema`

新增参数 `row_format: Literal["array", "object"] = "array"`，默认保持现状。

`row_format == "object"` 时，每行 schema 为：

```json
{
  "type": "object",
  "properties": {
    "姓名": {"type": "string", "description": "姓名"},
    "...": {"type": "string", "description": "..."}
  },
  "required": ["<全部列名>"],
  "additionalProperties": false
}
```

- 每键 `description` 复用现有 `_describe_column`，继续携带日期允许格式、数值去千分位/货币提示。
- GBNF 强制模型逐键输出 → 值被键名锚定 → 串列从语法层消除；键顺序对下游无影响。

**职责**：构建 schema，输入列数 / header / column specs / row_format，输出 JSON Schema dict。

### 2. 回退与硬错误（两类条件分开处理）

对象键依赖列名唯一。两种情况语义不同，**必须拆成两个分支**，不可合并：

- **重复列名（对象键会塌缩）→ 回退**到 `row_format="array"` 的位置数组 schema，**可继续抽取**。
  - 通过 `LLMExtractor` 现有的模块级 `_logger.warning`（`llm_extractor.py:46`）记一条降级日志；不引入新的 event_logger 通道（结构化 event_logger 目前仅在 adapter 内，回退发生在 `_run_llm_grounded_extract` 这层拿不到）。
  - **静默回退**：不在 UI 层提示，仅日志。
- **列数 < 2 → 维持现有 `E_PARSE_001` 硬错误**（`build_rows_schema:14` 现状），**不可继续**。这不是"回退到数组"——数组路径同样会在列数 < 2 时 `raise`。实现时不要把列数 < 2 做成静默回退。

### 3. 对象行解析与映射

**两个新增物，均落在 `output_normalizer.py`，不改动 `parse_rows_payload`：**

a. `parse_object_rows_payload(raw_content, *, parse_mode) -> list[dict]`：
- **复用**现有 `_build_parse_candidates`（去 `<think>` 标签、code fence 剥离、JSON 修复——这些对小模型很关键），仅替换最后的"行形状校验"：用新的 `_unwrap_object_rows` 取出 `rows` 并要求每行是 `dict`。
- 不复用 `parse_rows_payload` 的 `_validate_rows`/`_looks_like_2d_rows`（那套要求每行是 list，会拒 dict）；也不给 `parse_rows_payload` 加 `row_format` 参数，避免污染数组路径的 `E_LLM_007` 语义。

b. `map_object_rows_to_arrays(rows: list[dict], header: list[str]) -> list[list[str]]`，纯函数：
- 按 `header` 顺序取每个对象的键值。
- 缺键填 `" "`。
- 多余键丢弃。
- 键顺序无关（更鲁棒）。

### 4. `llm_extractor._run_llm_grounded_extract` 路由

- 计算 `use_object_schema = provider_cfg.provider == "ollama" and provider_cfg.use_structured_output and <header 无重复列名>`。
- `use_object_schema` 为真时用 `row_format="object"` 构建 schema，`schema_hint` 自动切换成对象 schema 文本。
- 解析阶段：对象 schema 路径走 `parse_object_rows_payload` → `map_object_rows_to_arrays` → `normalize_rows`；数组路径保持走原 `parse_rows_payload`。
- **不新增任何用户可见配置**，对小模型自动生效。

### 5. 格式 / 类型问题（吸收 B，限定力度）

- 对象 schema 每键 `description` 带格式约束；prompt 的 `字段类型约束` 块保留。
- 值仍输出为 `string`：grounding 需靠原文匹配，不可在抽取阶段转 number/date，否则匹配断裂。
- 真正的类型规范化仍由下游 `canonicalize_typed_cells`（`output_normalizer.py:64`）在写出阶段完成，**不动**。
- **本期不加**激进格式后处理；不简化系统提示主体，只让 `schema_hint` 自动切换。

## 错误处理与边界

- 对象 schema 下模型仍吐数组：解析兜底先试对象、失败再退回原数组路径，不直接报错。
- 重复列名：回退数组 schema（可继续）；列数 < 2：维持 `E_PARSE_001` 硬错误（不回退）。见组件 2。
- Ollama 输出对象但缺键：缺的填 `" "`，交由 grounding 判定。

## 测试策略

全部用 stub adapter，无外部依赖（沿用现有约定）。

- `schema_builder`：对象 schema 形状、`required` 含全部列、`additionalProperties: false`。
- `provider_ollama`：**回归钉子**——对象 schema 经 `_sanitize_schema_for_ollama` 后 `required`（list）与 `additionalProperties: false`（key 非 `items`）不被剥离。这是方案最关键的"不破坏点"，防止未来扩充 sanitizer 规则时误伤。
- `parse_object_rows_payload`：复用 `_build_parse_candidates` 后能解析带 `<think>`/code fence/末尾逗号的对象行；非对象行（list）走该函数被正确拒绝。
- `map_object_rows_to_arrays`：键顺序无关、缺键补 `" "`、多余键丢弃。
- `llm_extractor`：Ollama 走对象 schema 且解析正确；OpenAI 仍走数组 schema 不变；重复列名回退数组 schema 并记 `_logger.warning`；列数 < 2 维持 `E_PARSE_001` 硬错误（不回退）。
- 端到端：对象行 → 正确位置数组 → 正确 grounding 分类。

### 手动冒烟验证（non-test，计划阶段执行一次）

用真实 Ollama + 一个 1B–7B 模型跑一份多列样本，确认 GBNF 确实把对象键编成语法字面量、值被列锚定（串列消失）。设计已对"模型仍吐数组"做了兜底（见错误处理），不依赖该假设绝对成立，但需确认收益实际兑现。

## 非目标

- 不改在线（`openai_compatible`）链路。
- 不新增用户可见配置项。
- 不做逐列拆分抽取。
- 本期不加激进格式后处理、不改系统提示主体。
- 不处理掉行 / `num_ctx` 截断（非本次失败模式）。
