from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from src.ocr.errors import OCRServiceError
from src.ocr.markdown_normalizer import normalize_markdown
from src.ocr.models import OCRTextBlock
from src.ocr.paddle_service import PaddleOCRService


@dataclass(frozen=True)
class OnlinePageResult:
    text: str
    blocks: list[OCRTextBlock]
    confidence_avg: float | None
    confidence_min: float | None
    block_count: int
    error_code: str | None
    markdown: str | None = None


@dataclass(frozen=True)
class OnlineJobResult:
    pages: list[OnlinePageResult]


HttpPost = Callable[..., Any]
HttpGet = Callable[..., Any]
CancelCheck = Callable[[], bool]


class OnlineOCRClient:
    """异步在线 OCR 客户端：submit → poll_until_done → fetch_jsonl。

    与远端约定为「整文档异步 job」：提交后轮询 job 状态，完成后下载 JSONL，
    每行对应一页。识别封套结构与本地 PP-OCR predict JSON 一致，因此复用
    ``PaddleOCRService._extract_raw_blocks`` 解析文本块。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        optional_payload: dict[str, Any],
        http_post: HttpPost | None = None,
        http_get: HttpGet | None = None,
        poll_interval: float = 5,
        poll_timeout: float = 1800,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._optional_payload = optional_payload
        self._http_post = http_post or _requests_post
        self._http_get = http_get or _requests_get
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._cancel_check = cancel_check

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"bearer {self._api_key}"}

    def submit(self, *, file_bytes: bytes, filename: str) -> str:
        """提交文档，返回 jobId。"""
        data = {"model": self._model, "optionalPayload": json.dumps(self._optional_payload)}
        files = {"file": (filename, file_bytes)}
        try:
            response = self._http_post(
                self._base_url,
                headers=self._headers,
                data=data,
                files=files,
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一映射
            raise OCRServiceError("E_OCR_011", "在线 OCR 提交请求失败") from exc

        status_code = getattr(response, "status_code", None)
        if status_code in (401, 403):
            raise OCRServiceError("E_OCR_012", "在线 OCR 鉴权失败（401/403）")

        payload = self._read_json(response, "E_OCR_011")
        if payload.get("code") != 0:
            raise OCRServiceError("E_OCR_013", f"在线 OCR 提交返回异常：{payload.get('msg')}")
        job_id = (payload.get("data") or {}).get("jobId")
        if not isinstance(job_id, str) or not job_id:
            raise OCRServiceError("E_OCR_013", "在线 OCR 提交响应缺少 jobId")
        return job_id

    def poll_until_done(
        self,
        job_id: str,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        """轮询 job 状态，完成后返回结果 JSONL 的 URL。

        ``progress_callback(extracted_pages, total_pages)``：在 ``running`` 期间用远端
        ``extractProgress`` 回报页级进度，供 UI 驱动进度条（整份 job 在 done 前无结果，
        否则长任务期间界面会像卡死，见 spec §2）。
        """
        url = f"{self._base_url}/{job_id}"
        deadline = time.monotonic() + self._poll_timeout
        while True:
            if self._cancel_check is not None and self._cancel_check():
                raise OCRServiceError("E_OCR_011", "在线 OCR 轮询被取消")

            try:
                response = self._http_get(url, headers=self._headers, timeout=30)
            except Exception as exc:  # noqa: BLE001 - 网络异常统一映射
                raise OCRServiceError("E_OCR_011", "在线 OCR 轮询请求失败") from exc

            status_code = getattr(response, "status_code", None)
            if status_code in (401, 403):
                raise OCRServiceError("E_OCR_012", "在线 OCR 鉴权失败（401/403）")

            payload = self._read_json(response, "E_OCR_011")
            data = payload.get("data") or {}
            if progress_callback is not None:
                extract_progress = data.get("extractProgress") or {}
                extracted = extract_progress.get("extractedPages")
                total = extract_progress.get("totalPages")
                if isinstance(extracted, int) and isinstance(total, int):
                    progress_callback(extracted, total)
            state = data.get("state")
            if state == "done":
                json_url = (data.get("resultUrl") or {}).get("jsonUrl")
                if not isinstance(json_url, str) or not json_url:
                    raise OCRServiceError("E_OCR_013", "在线 OCR job 完成但缺少 jsonUrl")
                return json_url
            if state == "failed":
                raise OCRServiceError("E_OCR_013", f"在线 OCR job 失败：{data.get('errorMsg')}")

            if time.monotonic() >= deadline:
                raise OCRServiceError("E_OCR_011", "在线 OCR 轮询超时")
            time.sleep(self._poll_interval)

    def fetch_jsonl(self, jsonl_url: str) -> OnlineJobResult:
        """下载结果 JSONL 并解析为按页结果。

        jsonUrl 是百度对象存储(BOS)的预签名 URL，鉴权信息已在 query string 中。
        若再带上我们的 ``Authorization: bearer`` 头，BOS 会按 header 鉴权方案处理并
        因缺少 ``Date``/``x-bce-date`` 头而返回 400 (MissingDateHeader)，故此处不发该头。
        """
        try:
            response = self._http_get(jsonl_url, headers={}, timeout=120)
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            text = response.text
        except OCRServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络异常统一映射
            raise OCRServiceError("E_OCR_011", "在线 OCR 结果下载失败") from exc

        pages: list[OnlinePageResult] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                line_obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OCRServiceError("E_OCR_013", "在线 OCR 结果行非合法 JSON") from exc
            pages.extend(self._parse_line_pages(line_obj))
        return OnlineJobResult(pages=pages)

    @staticmethod
    def _parse_line_pages(line_obj: dict[str, Any]) -> list[OnlinePageResult]:
        """解析一行 JSONL → 按页结果列表。

        实测：多页 PDF 的整份结果在「一行」JSONL 内，页边界是封套条目——
        ``layoutParsingResults`` 每个条目（VL / PP-StructureV3）或 ``ocrResults``
        每个条目（PP-OCRv5 / PP-OCRv6）= 一页。单页 = 单条目，故单页行为不变。
        """
        error_code_raw = line_obj.get("errorCode")
        error_code = str(error_code_raw) if error_code_raw not in (None, 0, "0") else None
        result = line_obj.get("result") or {}

        # 优先级 1：VL / PP-StructureV3 的 markdown 封套，每个条目一页
        layout_results = result.get("layoutParsingResults")
        if isinstance(layout_results, list) and layout_results:
            pages = []
            for item in layout_results:
                if not isinstance(item, dict):
                    continue
                raw_markdown = (item.get("markdown") or {}).get("text", "") or ""
                pages.append(
                    OnlinePageResult(
                        text=_markdown_to_text(raw_markdown),
                        blocks=[],
                        confidence_avg=None,
                        confidence_min=None,
                        block_count=0,
                        error_code=error_code,
                        markdown=normalize_markdown(raw_markdown),
                    )
                )
            if pages:
                return pages

        # 优先级 2：PP-OCRv5 / PP-OCRv6 的 ocrResults 封套，每个条目一页，复用本地块解析
        ocr_results = result.get("ocrResults")
        if isinstance(ocr_results, list) and ocr_results:
            pages = []
            for item in ocr_results:
                if not isinstance(item, dict):
                    continue
                pruned = item.get("prunedResult")
                blocks: list[OCRTextBlock] = (
                    PaddleOCRService._extract_raw_blocks(pruned) if isinstance(pruned, dict) else []
                )
                scores = [float(b.score) for b in blocks if isinstance(b.score, (int, float))]
                pages.append(
                    OnlinePageResult(
                        text="\n".join(b.text for b in blocks),
                        blocks=blocks,
                        confidence_avg=(sum(scores) / len(scores)) if scores else None,
                        confidence_min=min(scores) if scores else None,
                        block_count=len(blocks),
                        error_code=error_code,
                    )
                )
            if pages:
                return pages

        # 优先级 3：空结果/错误行——保留一页占位以记录页级错误
        return [
            OnlinePageResult(
                text="",
                blocks=[],
                confidence_avg=None,
                confidence_min=None,
                block_count=0,
                error_code=error_code,
            )
        ]

    @staticmethod
    def _read_json(response: Any, fallback_code: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise OCRServiceError(fallback_code, "在线 OCR 响应非合法 JSON") from exc
        if not isinstance(payload, dict):
            raise OCRServiceError("E_OCR_013", "在线 OCR 响应结构异常")
        return payload


class _MarkdownTextExtractor(HTMLParser):
    """把 PaddleOCR-VL 的 markdown(混入 HTML 表格/图片)规整为纯文本。

    下游 LLM 抽取的 prompt/examples 是按平铺纯文本调校的，喂 HTML ``<table>``
    会导致字段定位失败与幻觉。这里去掉 ``<img>``、把表格按行线性化（单元格用
    空格拼接、每行一行）、其余标签丢弃只留文本。

    **假设：表格不嵌套**（PaddleOCR-VL/PP-StructureV3 发票输出为扁平表格）。
    用单层状态而非栈；遇 ``<td>``/``<tr>`` 起始或文档结束会先 flush 上一个**未闭合**
    的 cell/row，故对**缺失闭合标签或被截断**的 markdown 也尽量保住已读内容，
    不静默丢页（嵌套表格会被压平、不保证层次，但发票场景不出现）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._in_cell = False
        self._cell: list[str] = []
        self._row: list[str] = []

    def _flush_cell(self) -> None:
        if self._cell:
            cell = " ".join("".join(self._cell).split())
            if cell:
                self._row.append(cell)
        self._cell = []
        self._in_cell = False

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._row:
            self.lines.append(" ".join(self._row))
        self._row = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("td", "th"):
            self._flush_cell()  # 容错：上一个 cell 若没闭合先收尾
            self._in_cell = True
            self._cell = []
        elif tag == "tr":
            self._flush_row()  # 容错：上一行若没闭合先收尾
        elif tag == "br" and self._in_cell:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._flush_cell()
        elif tag == "tr":
            self._flush_row()

    def handle_data(self, data: str) -> None:
        # VL 在单元格内用字面量反斜杠-n 作分隔，规整为空格
        data = data.replace("\\n", " ")
        if self._in_cell:
            self._cell.append(data)
        else:
            stripped = data.strip()
            if stripped:
                self.lines.append(stripped)

    def finalize(self) -> None:
        """文档结束后 flush 残留(被截断/缺失闭合标签时,保住已读单元格内容)。"""
        self._flush_row()


def _markdown_to_text(markdown: str) -> str:
    """规整 VL markdown 为纯文本（见 _MarkdownTextExtractor）。"""
    parser = _MarkdownTextExtractor()
    parser.feed(markdown)
    parser.close()
    parser.finalize()
    text = "\n".join(line for line in (raw.strip() for raw in parser.lines) if line)
    # 去掉 ATX markdown 标题井号（# / ## ...），更贴近本地平铺文本
    return re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)


def _requests_post(url: str, *, headers: dict[str, str], data: dict[str, Any], files: dict[str, Any], timeout: int) -> Any:
    import requests

    return requests.post(url, headers=headers, data=data, files=files, timeout=timeout)


def _requests_get(url: str, *, headers: dict[str, str], timeout: int) -> Any:
    import requests

    return requests.get(url, headers=headers, timeout=timeout)
