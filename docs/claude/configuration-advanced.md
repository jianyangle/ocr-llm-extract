# Configuration Advanced Reference

从 CLAUDE.md 抽出的高级配置参数与 profile 映射表。

## 高级参数（仅 config.json，UI 不暴露）

- `extraction_passes` / `extraction_max_char_buffer` / `extraction_passes_increment` / `extraction_parse_mode`：`extraction_profile` 的底层字段。若 config.json 中与 profile 预设不一致，加载时标记为 custom 并以 config.json 中的值为准。
- `ocr_use_textline_orientation`、`ocr_use_doc_orientation_classify`、`ocr_use_doc_unwarping`、`ocr_text_det_limit_side_len`、`ocr_text_det_thresh`、`ocr_layout_parser`、`ocr_restore_paragraphs`、`ocr_ignore_areas`、`ocr_adaptive_retry_enabled`、`ocr_retry_confidence_threshold`、`ocr_retry_target_profile`：OCR 底层参数，同上 custom 检测。

## `ocr_layout_parser` 值映射

支持新值 `none`、`multi_none`、`multi_line`、`multi_para`、`single_none`、`single_line`、`single_para`、`single_code`，并兼容旧值 `auto`、`single_column`、`multi_column`。

设置对话框当前只暴露 `single_line` / `multi_para` / `none` 三个选项；若加载到旧值（如 `auto`），界面会映射显示为 `multi_para` 并保留原始持久化值，直到用户主动改动该下拉框后才会覆盖保存。

## 行为说明

区域 rescue 具备软降级语义：当 `PIL` 不可导入或 `Image.open()` 失败时，任务会保留原始抽取结果继续执行，不应因 rescue 分支中断整条流水线。

TBPU 后处理流水线为：整页旋转预处理 → 阅读顺序排序 → 段落判定 → 块间分隔符推断。`auto` 当前映射到 `multi_para`。

## Extraction profile 预设映射表

| profile  | extraction_passes | extraction_max_char_buffer | extraction_passes_increment | extraction_parse_mode |
|----------|-------------------|----------------------------|-----------------------------|-----------------------|
| fast     | 1                 | 3000                       | 3000                        | balanced              |
| balanced | 2                 | 2200                       | 800                         | balanced              |
| accurate | 3                 | 1800                       | 600                         | aggressive            |

## OCR profile 与 adaptive retry

**OCR profile 覆盖 `adaptive_retry_enabled`：`fast`=False，`balanced`/`accurate`=True**。
