# Ollama 对象键结构化输出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 仅 Ollama 链路改用「对象键行」结构化输出，用 GBNF 语法约束从根上消除字段串列，提升 1B–7B 小模型抽取精度。

**Architecture:** `build_rows_schema` 新增 `row_format="object"`，把每行从位置字符串数组改成以列名为键的对象。`output_normalizer` 新增对象行解析（`parse_object_rows_payload` + `_unwrap_object_rows`）与对象→位置数组映射（`map_object_rows_to_arrays`）。`_run_llm_grounded_extract` 在 Ollama + 结构化输出 + 列名无重复时启用对象 schema，解析失败时 try/except 回退到原数组路径。在线链路、grounding、dedupe、Excel 写入零改动。

**Tech Stack:** Python 3.12、pytest（stub adapter，无外部服务依赖）。

---

## 文件结构

- **Modify** `src/extract/schema_builder.py` — `build_rows_schema` 新增 `row_format` 参数与对象行分支。
- **Modify** `src/extract/output_normalizer.py` — 新增 `parse_object_rows_payload`、`_unwrap_object_rows`、`_looks_like_object_rows`、`map_object_rows_to_arrays`。
- **Modify** `src/extract/llm_extractor.py` — `_run_llm_grounded_extract` 路由 + 新增模块级 `_has_duplicate_columns`、解析辅助方法；扩充 `output_normalizer` 导入。
- **Modify** `tests/test_extract_schema_builder.py` — 对象 schema 形状测试。
- **Modify** `tests/test_extract_output_normalizer.py` — 对象解析与映射测试。
- **Modify** `tests/test_extract_llm_extractor.py` — 路由、回退、端到端测试。
- **Modify** `tests/test_provider_ollama_schema_sanitize.py` — sanitizer 不破坏对象 schema 的回归钉子。

每个 Task 自包含、可独立提交。

---

### Task 1: `build_rows_schema` 支持 `row_format="object"`

**Files:**
- Modify: `src/extract/schema_builder.py`
- Test: `tests/test_extract_schema_builder.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_extract_schema_builder.py` 末尾追加：

```python
def test_build_rows_schema_object_format_keys_by_header() -> None:
    schema = build_rows_schema(
        2,
        header=["开票日期", "金额"],
        columns=(
            ColumnSpec(name="开票日期", type="date", date_formats=("%Y年%m月%d日",)),
            ColumnSpec(name="金额", type="number", thousands_separator=",", currency_strip=True),
        ),
        row_format="object",
    )

    row_schema = schema["properties"]["rows"]["items"]
    assert row_schema["type"] == "object"
    assert row_schema["additionalProperties"] is False
    assert row_schema["required"] == ["开票日期", "金额"]
    assert row_schema["properties"]["开票日期"] == {
        "type": "string",
        "description": "开票日期（示例格式：%Y年%m月%d日）",
    }
    assert row_schema["properties"]["金额"] == {
        "type": "string",
        "description": "金额（数值列，输出去除货币符号和千分位）",
    }


def test_build_rows_schema_object_format_requires_header() -> None:
    with pytest.raises(ExtractServiceError) as exc:
        build_rows_schema(2, row_format="object")

    assert exc.value.code == "E_PARSE_001"


def test_build_rows_schema_array_format_unchanged_by_default() -> None:
    schema = build_rows_schema(2, header=["发票号码", "金额"])

    row_schema = schema["properties"]["rows"]["items"]
    assert row_schema["items"] is False
    assert "prefixItems" in row_schema
    assert row_schema.get("type") == "array"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_extract_schema_builder.py -v`
Expected: 前两个新测试 FAIL（`build_rows_schema() got an unexpected keyword argument 'row_format'`），`test_build_rows_schema_array_format_unchanged_by_default` PASS。

- [ ] **Step 3: 实现对象行分支**

在 `src/extract/schema_builder.py` 顶部导入 `Literal`：

```python
from __future__ import annotations

from typing import Literal

from src.domain.schemas import ColumnSpec

from .errors import ExtractServiceError
```

把 `build_rows_schema` 签名与函数体改为（保留原数组逻辑，新增对象分支）：

```python
def build_rows_schema(
    column_count: int,
    *,
    header: list[str] | None = None,
    columns: tuple[ColumnSpec, ...] | None = None,
    row_format: Literal["array", "object"] = "array",
) -> dict[str, object]:
    if column_count < 2:
        raise ExtractServiceError("E_PARSE_001", "examples must define at least 2 columns")

    if row_format == "object":
        if header is None:
            raise ExtractServiceError("E_PARSE_001", "object row schema requires header")
        if len(header) != column_count:
            raise ExtractServiceError("E_PARSE_001", "examples header length does not match column count")
        object_row_schema = {
            "type": "object",
            "properties": {
                str(header[index]): {
                    "type": "string",
                    "description": _describe_column(header, columns, index),
                }
                for index in range(column_count)
            },
            "required": [str(name) for name in header],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": object_row_schema,
                }
            },
            "required": ["rows"],
        }

    row_schema = {
        "type": "array",
        "minItems": column_count,
        "maxItems": column_count,
        "items": {"type": "string"},
    }
    if header is not None:
        if len(header) != column_count:
            raise ExtractServiceError("E_PARSE_001", "examples header length does not match column count")
        row_schema["prefixItems"] = [
            {"type": "string", "description": _describe_column(header, columns, index)}
            for index, _ in enumerate(header)
        ]
        row_schema["items"] = False
    return {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": row_schema,
            }
        },
        "required": ["rows"],
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_extract_schema_builder.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/extract/schema_builder.py tests/test_extract_schema_builder.py
git commit -m "feat: build_rows_schema 支持 object 行格式

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 对象行解析 `parse_object_rows_payload` + `_unwrap_object_rows`

**Files:**
- Modify: `src/extract/output_normalizer.py`
- Test: `tests/test_extract_output_normalizer.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_extract_output_normalizer.py` 顶部确保导入包含新符号（与文件现有 import 风格一致）：

```python
from src.extract.output_normalizer import parse_object_rows_payload
from src.extract.errors import ExtractServiceError
```

在文件末尾追加：

```python
def test_parse_object_rows_payload_unwraps_rows_key() -> None:
    raw = '{"rows": [{"姓名": "张三", "电话": "13800000000"}]}'

    rows = parse_object_rows_payload(raw)

    assert rows == [{"姓名": "张三", "电话": "13800000000"}]


def test_parse_object_rows_payload_accepts_top_level_object_array() -> None:
    raw = '[{"姓名": "张三"}, {"姓名": "李四"}]'

    rows = parse_object_rows_payload(raw)

    assert rows == [{"姓名": "张三"}, {"姓名": "李四"}]


def test_parse_object_rows_payload_survives_think_tag_and_code_fence() -> None:
    raw = '<think>推理</think>\n```json\n{"rows": [{"姓名": "张三",}]}\n```'

    rows = parse_object_rows_payload(raw, parse_mode="balanced")

    assert rows == [{"姓名": "张三"}]


def test_parse_object_rows_payload_rejects_array_rows() -> None:
    raw = '{"rows": [["张三", "13800000000"]]}'

    with pytest.raises(ExtractServiceError) as exc:
        parse_object_rows_payload(raw)

    assert exc.value.code == "E_LLM_002"


def test_parse_object_rows_payload_rejects_empty_content() -> None:
    with pytest.raises(ExtractServiceError) as exc:
        parse_object_rows_payload("   ")

    assert exc.value.code == "E_LLM_002"
```

若文件顶部尚未 `import pytest`，补上。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_extract_output_normalizer.py -k object_rows -v`
Expected: FAIL（`cannot import name 'parse_object_rows_payload'`）。

- [ ] **Step 3: 实现解析函数**

在 `src/extract/output_normalizer.py` 末尾追加（复用现有 `_THINK_TAG_RE`、`_build_parse_candidates`、`_try_parse_json`）：

```python
def parse_object_rows_payload(raw_content: str, *, parse_mode: ParseMode = "balanced") -> list[dict]:
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise ExtractServiceError("E_LLM_002", "LLM response content is empty")

    cleaned = _THINK_TAG_RE.sub("", raw_content)
    if not cleaned.strip():
        raise ExtractServiceError("E_LLM_002", "LLM response content is empty")

    candidates = _build_parse_candidates(cleaned, parse_mode=parse_mode)
    for candidate in candidates:
        data = _try_parse_json(candidate)
        if data is None:
            continue
        rows = _unwrap_object_rows(data)
        if rows is None:
            continue
        return rows

    raise ExtractServiceError("E_LLM_002", "LLM response is not structured object rows")


def _unwrap_object_rows(data: object) -> list[dict] | None:
    if isinstance(data, list):
        if _looks_like_object_rows(data):
            return data
        return None
    if not isinstance(data, dict):
        return None

    value = data.get("rows")
    if isinstance(value, list) and _looks_like_object_rows(value):
        return value
    return None


def _looks_like_object_rows(rows: list[object]) -> bool:
    if not rows:
        return True
    return all(isinstance(row, dict) for row in rows)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_extract_output_normalizer.py -k object_rows -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/extract/output_normalizer.py tests/test_extract_output_normalizer.py
git commit -m "feat: 新增 parse_object_rows_payload 解析对象行

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 对象行 → 位置数组映射 `map_object_rows_to_arrays`

**Files:**
- Modify: `src/extract/output_normalizer.py`
- Test: `tests/test_extract_output_normalizer.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_extract_output_normalizer.py` 的 import 行补上：

```python
from src.extract.output_normalizer import map_object_rows_to_arrays
```

在文件末尾追加：

```python
def test_map_object_rows_to_arrays_is_key_order_independent() -> None:
    rows = [{"电话": "13800000000", "姓名": "张三"}]

    mapped = map_object_rows_to_arrays(rows, ["姓名", "电话"])

    assert mapped == [["张三", "13800000000"]]


def test_map_object_rows_to_arrays_fills_missing_key_with_space() -> None:
    rows = [{"姓名": "张三"}]

    mapped = map_object_rows_to_arrays(rows, ["姓名", "电话"])

    assert mapped == [["张三", " "]]


def test_map_object_rows_to_arrays_drops_extra_keys() -> None:
    rows = [{"姓名": "张三", "电话": "13800000000", "多余": "x"}]

    mapped = map_object_rows_to_arrays(rows, ["姓名", "电话"])

    assert mapped == [["张三", "13800000000"]]


def test_map_object_rows_to_arrays_handles_non_dict_row() -> None:
    mapped = map_object_rows_to_arrays(["not a dict"], ["姓名", "电话"])

    assert mapped == [[" ", " "]]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_extract_output_normalizer.py -k map_object_rows -v`
Expected: FAIL（`cannot import name 'map_object_rows_to_arrays'`）。

- [ ] **Step 3: 实现映射函数**

在 `src/extract/output_normalizer.py` 末尾追加：

```python
def map_object_rows_to_arrays(rows: list[dict], header: list[str]) -> list[list[object]]:
    keys = [str(name) for name in header]
    mapped: list[list[object]] = []
    for row in rows:
        if not isinstance(row, dict):
            mapped.append([" "] * len(keys))
            continue
        mapped.append([row.get(key, " ") for key in keys])
    return mapped
```

说明：本函数只做「键 → 位置」搬运，缺键填 `" "`；空白/类型清洗交给下游 `normalize_rows`（`_normalize_cell` 会 `str().strip()` 并把空串归一成 `" "`）。故 `test_map_object_rows_to_arrays_fills_missing_key_with_space` 期望的是原样 `" "`，无需在此 strip。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_extract_output_normalizer.py -k map_object_rows -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/extract/output_normalizer.py tests/test_extract_output_normalizer.py
git commit -m "feat: 新增 map_object_rows_to_arrays 对象行转位置数组

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `_run_llm_grounded_extract` 路由对象 schema 与回退

**Files:**
- Modify: `src/extract/llm_extractor.py`
- Test: `tests/test_extract_llm_extractor.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_extract_llm_extractor.py` 末尾追加（独立的捕获 stub，避免改动共享 `_StubAdapter`）：

```python
class _CaptureSchemaStub:
    def __init__(self, responses):
        self.responses = responses
        self.captured_schema = "UNSET"

    def chat_batch(self, **kwargs):
        self.captured_schema = kwargs.get("schema")
        return list(self.responses)


def _structured_cfg(provider: str):
    cfg = _cfg(provider, provider_platform_id=provider)
    cfg.use_structured_output = True
    cfg.grounding_mode = "off"
    # 强制走"传入 examples 即模板"的自定义路径，避免 default_config 的内置发票模板覆盖 header
    cfg.templates = []
    cfg.active_template_id = None
    return cfg


def test_ollama_uses_object_schema_when_structured() -> None:
    stub = _CaptureSchemaStub(responses=('{"rows": [{"姓名": "张三", "电话": "13800000000"}]}',))
    extractor = LLMExtractor(ollama_adapter=stub)
    cfg = _structured_cfg("ollama")
    examples = [["姓名", "电话"]]

    extractor.extract_detailed(
        text="张三 13800000000",
        prompts="p",
        examples=examples,
        provider_cfg=cfg,
    )

    row_schema = stub.captured_schema["properties"]["rows"]["items"]
    assert row_schema["type"] == "object"
    assert row_schema["required"] == ["姓名", "电话"]


def test_ollama_object_schema_round_trips_to_positional_values() -> None:
    stub = _CaptureSchemaStub(responses=('{"rows": [{"电话": "13800000000", "姓名": "张三"}]}',))
    extractor = LLMExtractor(ollama_adapter=stub)
    cfg = _structured_cfg("ollama")
    examples = [["姓名", "电话"]]

    outcome = extractor.extract_detailed(
        text="张三 13800000000",
        prompts="p",
        examples=examples,
        provider_cfg=cfg,
    )

    assert [row.values for row in outcome.rows] == [["张三", "13800000000"]]


def test_openai_keeps_array_schema_when_structured() -> None:
    stub = _CaptureSchemaStub(responses=('{"rows": [["张三", "13800000000"]]}',))
    extractor = LLMExtractor(openai_adapter=stub)
    cfg = _structured_cfg("openai_compatible")
    examples = [["姓名", "电话"]]

    extractor.extract_detailed(
        text="张三 13800000000",
        prompts="p",
        examples=examples,
        provider_cfg=cfg,
    )

    row_schema = stub.captured_schema["properties"]["rows"]["items"]
    assert row_schema["type"] == "array"
    assert row_schema["items"] is False


def test_ollama_duplicate_columns_fall_back_to_array_schema(caplog) -> None:
    stub = _CaptureSchemaStub(responses=('{"rows": [["张三", "李四"]]}',))
    extractor = LLMExtractor(ollama_adapter=stub)
    cfg = _structured_cfg("ollama")
    examples = [["姓名", "姓名"]]

    with caplog.at_level("WARNING"):
        extractor.extract_detailed(
            text="张三 李四",
            prompts="p",
            examples=examples,
            provider_cfg=cfg,
        )

    row_schema = stub.captured_schema["properties"]["rows"]["items"]
    assert row_schema["type"] == "array"
    assert any("duplicate column" in record.message.lower() for record in caplog.records)


def test_ollama_falls_back_to_array_parsing_when_model_returns_array() -> None:
    stub = _CaptureSchemaStub(responses=('{"rows": [["张三", "13800000000"]]}',))
    extractor = LLMExtractor(ollama_adapter=stub)
    cfg = _structured_cfg("ollama")
    examples = [["姓名", "电话"]]

    outcome = extractor.extract_detailed(
        text="张三 13800000000",
        prompts="p",
        examples=examples,
        provider_cfg=cfg,
    )

    assert [row.values for row in outcome.rows] == [["张三", "13800000000"]]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_extract_llm_extractor.py -k "object_schema or array_schema or duplicate_columns or array_parsing or positional_values" -v`
Expected: `test_ollama_uses_object_schema_when_structured`、`test_ollama_object_schema_round_trips_to_positional_values`、`test_ollama_duplicate_columns_fall_back_to_array_schema` 等 FAIL（当前 Ollama 仍发数组 schema、解析按数组走对 object 内容报错）。

- [ ] **Step 3: 改导入**

在 `src/extract/llm_extractor.py` 把第 13 行的 import 改为：

```python
from .output_normalizer import (
    map_object_rows_to_arrays,
    normalize_rows,
    parse_object_rows_payload,
    parse_rows_payload,
)
```

- [ ] **Step 4: 新增模块级重复列名检测**

在 `src/extract/llm_extractor.py` 模块级（例如 `_resolve_parse_mode` 附近）新增：

```python
def _has_duplicate_columns(header: list[str]) -> bool:
    names = [str(name).strip() for name in header]
    return len(names) != len(set(names))
```

- [ ] **Step 5: 改写 `_run_llm_grounded_extract`**

把 `_run_llm_grounded_extract` 整个方法体替换为：

```python
    def _run_llm_grounded_extract(
        self,
        *,
        source: ExtractionInput,
        template: PromptTemplate,
        expected_columns: int,
        provider_cfg: AppConfig,
    ) -> list[GroundedExtractRow]:
        parse_mode = _resolve_parse_mode(provider_cfg)
        header = template.examples[0]
        if source.has_markdown:
            pass_texts = split_markdown_passes(source.extraction_text, provider_cfg)
        else:
            pass_texts = _build_pass_texts(source.flat_text, provider_cfg, template=template)

        use_object_schema = False
        if getattr(provider_cfg, "provider", None) == "ollama" and provider_cfg.use_structured_output:
            if _has_duplicate_columns(header):
                _logger.warning(
                    "Ollama object-schema disabled: duplicate column names in header; "
                    "falling back to array schema"
                )
            else:
                use_object_schema = True

        if provider_cfg.use_structured_output:
            schema = build_rows_schema(
                expected_columns,
                header=header,
                columns=template.columns,
                row_format="object" if use_object_schema else "array",
            )
        else:
            schema = None
        schema_hint = json.dumps(schema, ensure_ascii=False) if schema is not None else None
        messages_list = [
            build_messages(
                template=template,
                text=pass_text,
                pass_index=pass_index,
                total_passes=len(pass_texts),
                schema_hint=schema_hint,
                markdown_input=source.has_markdown,
            )
            for pass_index, pass_text in enumerate(pass_texts, start=1)
        ]
        raw_contents = self._call_provider_batch(
            messages_list=messages_list,
            provider_cfg=provider_cfg,
            schema=schema,
        )

        grounded_rows: list[GroundedExtractRow] = []
        for raw_content in raw_contents:
            normalized = self._parse_to_normalized_rows(
                raw_content,
                parse_mode=parse_mode,
                expected_columns=expected_columns,
                header=header,
                use_object_schema=use_object_schema,
            )
            grounded_rows.extend(ground_rows(normalized, source_text=source.flat_text, cfg=provider_cfg))
        return grounded_rows

    def _parse_to_normalized_rows(
        self,
        raw_content: str,
        *,
        parse_mode: str,
        expected_columns: int,
        header: list[str],
        use_object_schema: bool,
    ) -> list[list[str]]:
        if use_object_schema:
            try:
                object_rows = parse_object_rows_payload(raw_content, parse_mode=parse_mode)
                mapped = map_object_rows_to_arrays(object_rows, header)
                return normalize_rows(mapped, expected_columns=expected_columns)
            except ExtractServiceError:
                _logger.warning(
                    "Ollama object-schema parse failed; falling back to array parsing"
                )
        rows = parse_rows_payload(raw_content, parse_mode=parse_mode)
        return normalize_rows(rows, expected_columns=expected_columns)
```

- [ ] **Step 6: 运行新测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_extract_llm_extractor.py -k "object_schema or array_schema or duplicate_columns or array_parsing or positional_values" -v`
Expected: 全部 PASS。

- [ ] **Step 7: 跑全量 extract 测试确认无回归**

Run: `.venv/bin/python -m pytest tests/test_extract_llm_extractor.py tests/test_extract_output_normalizer.py tests/test_extract_schema_builder.py -v`
Expected: 全部 PASS。

- [ ] **Step 8: 提交**

```bash
git add src/extract/llm_extractor.py tests/test_extract_llm_extractor.py
git commit -m "feat: Ollama 结构化抽取启用对象键 schema 并支持回退

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: sanitizer 不破坏对象 schema 的回归钉子

**Files:**
- Test: `tests/test_provider_ollama_schema_sanitize.py`

说明：纯回归测试，无生产代码改动。钉死 `_sanitize_schema_for_ollama` 只剥离 `items: false`，不会误删对象 schema 的 `required`（list）和 `additionalProperties: false`（key 非 `items`）。防止未来扩充 sanitizer 规则时误伤对象 schema。

- [ ] **Step 1: 写测试**

在 `tests/test_provider_ollama_schema_sanitize.py` 末尾追加：

```python
def test_ollama_sanitize_preserves_object_schema_constraints() -> None:
    """对象 schema 的 required 与 additionalProperties:false 不能被 sanitizer 剥离。"""
    captured: dict = {}

    def _post(_url, *, headers, json, timeout):
        captured["json"] = json
        return _Resp(200, {"message": {"content": '{"rows": [{"姓名": "张三"}]}'}})

    schema = build_rows_schema(2, header=["姓名", "电话"], row_format="object")

    adapter = OllamaAdapter(http_post=_post)
    adapter.chat(
        base_url="http://localhost:11434",
        model="qwen3:4b",
        messages=[{"role": "user", "content": "u"}],
        schema=schema,
    )

    sent_row_schema = captured["json"]["format"]["properties"]["rows"]["items"]
    assert sent_row_schema["type"] == "object"
    assert sent_row_schema["additionalProperties"] is False
    assert sent_row_schema["required"] == ["姓名", "电话"]
    assert set(sent_row_schema["properties"].keys()) == {"姓名", "电话"}
```

- [ ] **Step 2: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_provider_ollama_schema_sanitize.py -v`
Expected: 全部 PASS（sanitizer 本就不剥离这些键，新测试直接绿；它是防回归钉子）。

- [ ] **Step 3: 提交**

```bash
git add tests/test_provider_ollama_schema_sanitize.py
git commit -m "test: 钉死 sanitizer 不破坏对象 schema 约束

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 全量回归与手动冒烟验证

**Files:** 无代码改动。

- [ ] **Step 1: 跑全量测试套件**

Run: `.venv/bin/python -m pytest`
Expected: 全部 PASS（原 967 + 新增用例）。若有 FAIL，先读错误再定位，不得跳过。

- [ ] **Step 2: 手动冒烟验证（non-test，需真实 Ollama）**

前置：本机 Ollama 已拉取一个 1B–7B 模型（如 `qwen2.5:3b` 或 `qwen3:4b`）。

操作：在应用内将 provider 设为 `ollama`、开启结构化输出，导入一份**多列**样本（如名片或发票），执行抽取。

验证点：
1. 抽取结果无字段串列（值落在正确列）。
2. 与切回数组 schema（临时把同一模型在重复列名场景或回退路径对比）相比，串列明显减少。
3. 控制台无 `object-schema parse failed` 频繁回退日志（偶发可接受，频繁说明模型未遵守对象 schema，需在 issue 记录但不阻断本期）。

记录结论到 `.scratch/`（按 issue-tracker 约定），如收益不及预期，开后续 issue 评估 prompt 简化或换模型，不在本计划范围内修改。

- [ ] **Step 3: 无代码改动，不需提交**

---

## Self-Review

**1. Spec coverage：**
- 组件 1（`build_rows_schema` row_format=object）→ Task 1 ✓
- 组件 2（重复列名回退 / 列数<2 硬错误）→ Task 4 Step 5 路由含 `_has_duplicate_columns` 回退 + `_logger.warning`；列数<2 由 `build_rows_schema:14` 现有 `E_PARSE_001` 守住（Task 1 未改该守卫）✓
- 组件 3a/a'（`parse_object_rows_payload` + `_unwrap_object_rows`）→ Task 2 ✓
- 组件 3b（`map_object_rows_to_arrays`）→ Task 3 ✓
- 组件 4（路由 + fallback 归调用方）→ Task 4 `_parse_to_normalized_rows` try/except ✓
- 组件 5（格式靠 schema description + 下游规范化，不动）→ 复用 `_describe_column`，未改 `canonicalize_typed_cells` ✓
- 测试策略全部覆盖（schema 形状 / sanitizer 回归 / 对象解析 / 映射 / 路由 / 端到端）→ Task 1–5 ✓
- 手动冒烟验证 → Task 6 ✓

**2. Placeholder scan：** 无 TBD/TODO；每个改代码的 step 均给出完整代码。

**3. Type consistency：**
- `build_rows_schema(..., row_format=...)` 在 Task 1 定义、Task 4 调用一致。
- `parse_object_rows_payload(raw_content, *, parse_mode)` 在 Task 2 定义、Task 4 `_parse_to_normalized_rows` 调用一致。
- `map_object_rows_to_arrays(rows, header)` 在 Task 3 定义、Task 4 调用一致，返回 `list[list[object]]` 与 `normalize_rows` 入参兼容。
- `_has_duplicate_columns(header)` Task 4 Step 4 定义、Step 5 使用一致。
- `_parse_to_normalized_rows(...)` 签名（含 `use_object_schema`）在 Task 4 定义与调用一致。
