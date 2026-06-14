from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from src.domain.schemas import AppConfig

from .builtin_templates import BUILTIN_TEMPLATES
from .errors import ExtractServiceError
from .example_parser import format_examples
from .prompt_builder import PromptTemplate


BUILTIN_INVOICE_ID = "builtin:invoice"
BUILTIN_SIGCARD_ID = "builtin:sigcard"
_DEFAULT_ACTIVE_TEMPLATE_ID = BUILTIN_INVOICE_ID
_BUILTIN_ID_TO_KEY = (
    (BUILTIN_INVOICE_ID, "invoice"),
    (BUILTIN_SIGCARD_ID, "sigcard"),
)
_BUILTIN_TEMPLATE_IDS = {template_id for template_id, _ in _BUILTIN_ID_TO_KEY}


@dataclass(frozen=True)
class TemplateCatalogEntry:
    id: str
    builtin: bool
    name: str


@dataclass(frozen=True)
class TemplateDraftValidationResult:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class _StoredTemplate:
    id: str
    builtin: bool
    name: str
    prompts: str
    examples: list[list[str]]


class TemplateCatalog:
    def __init__(
        self,
        *,
        builtin_overrides: dict[str, dict[str, object]],
        user_templates: dict[str, _StoredTemplate],
        active_template_id: str | None,
    ) -> None:
        self._builtin_overrides = dict(builtin_overrides)
        self._user_templates = dict(user_templates)
        self._active_template_id = active_template_id

    @classmethod
    def load(
        cls,
        raw_templates: Any,
        active_template_id: str | None = None,
    ) -> TemplateCatalog:
        builtin_overrides: dict[str, dict[str, object]] = {}
        user_templates: dict[str, _StoredTemplate] = {}

        if isinstance(raw_templates, list):
            for index, item in enumerate(raw_templates):
                parsed = cls._parse_entry(item=item, index=index)
                if parsed is None:
                    continue
                if parsed.builtin:
                    builtin_overrides[parsed.id] = cls._override_payload(parsed)
                else:
                    user_templates[parsed.id] = parsed

        return cls(
            builtin_overrides=builtin_overrides,
            user_templates=user_templates,
            active_template_id=active_template_id,
        )

    @property
    def active_id(self) -> str:
        return self._resolve_active_id()

    @active_id.setter
    def active_id(self, value: str | None) -> None:
        self._active_template_id = value

    def list_entries(self) -> list[TemplateCatalogEntry]:
        entries = [
            TemplateCatalogEntry(
                id=template_id,
                builtin=True,
                name=self._resolve_builtin_template(template_id).name,
            )
            for template_id, _builtin_key in _BUILTIN_ID_TO_KEY
        ]
        user_entries = [
            TemplateCatalogEntry(id=template.id, builtin=False, name=template.name)
            for template in sorted(self._user_templates.values(), key=lambda item: item.name)
        ]
        return entries + user_entries

    def active_template(self) -> PromptTemplate:
        return self.template_by_id(self.active_id)

    def active_template_name(self) -> str:
        return self.active_template().name

    def template_by_id(self, template_id: str) -> PromptTemplate:
        if template_id in _BUILTIN_TEMPLATE_IDS:
            return self._resolve_builtin_template(template_id)
        template = self._user_templates.get(template_id)
        if template is None:
            raise ExtractServiceError("E_TMPL_001", f"template not found: {template_id}")
        return PromptTemplate(
            name=template.name,
            description=template.prompts,
            examples=[list(row) for row in template.examples],
        )

    def create_user_template(self, name: str, prompts: str, examples: str | list[list[str]]) -> str:
        normalized = self._normalize_draft(name=name, prompts=prompts, examples=examples)
        template_id = f"user:{uuid4()}"
        self._user_templates[template_id] = _StoredTemplate(
            id=template_id,
            builtin=False,
            name=normalized.name,
            prompts=normalized.prompts,
            examples=normalized.examples,
        )
        return template_id

    def update(self, template_id: str, name: str, prompts: str, examples: str | list[list[str]]) -> None:
        normalized = self._normalize_draft(
            name=name,
            prompts=prompts,
            examples=examples,
            exclude_id=template_id,
        )
        self._apply_normalized_update(template_id, normalized)

    def apply_updates(self, drafts: dict[str, tuple[str, str, str | list[list[str]]]]) -> None:
        normalized_updates: dict[str, _StoredTemplate] = {}
        for template_id, (name, prompts, examples) in drafts.items():
            if template_id not in _BUILTIN_TEMPLATE_IDS and template_id not in self._user_templates:
                raise ExtractServiceError("E_TMPL_001", f"template not found: {template_id}")
            try:
                normalized_updates[template_id] = self._normalize_draft_fields(
                    name=name,
                    prompts=prompts,
                    examples=examples,
                    exclude_id=template_id,
                )
            except ExtractServiceError as exc:
                template_name = name.strip() or self.template_by_id(template_id).name
                raise ExtractServiceError(exc.code, f"{template_name}: {exc.message}") from exc

        self._validate_final_names(normalized_updates)
        for template_id, normalized in normalized_updates.items():
            self._apply_normalized_update(template_id, normalized)

    def _apply_normalized_update(self, template_id: str, normalized: _StoredTemplate) -> None:
        if template_id in _BUILTIN_TEMPLATE_IDS:
            builtin_template = self._resolve_builtin_template(template_id, include_override=False)
            override: dict[str, object] = {}
            if normalized.name != builtin_template.name:
                override["name"] = normalized.name
            if normalized.prompts != builtin_template.description:
                override["prompts"] = normalized.prompts
            if normalized.examples != builtin_template.examples:
                override["examples"] = [list(row) for row in normalized.examples]
            if override:
                self._builtin_overrides[template_id] = override
            else:
                self._builtin_overrides.pop(template_id, None)
            return

        if template_id not in self._user_templates:
            raise ExtractServiceError("E_TMPL_001", f"template not found: {template_id}")
        self._user_templates[template_id] = _StoredTemplate(
            id=template_id,
            builtin=False,
            name=normalized.name,
            prompts=normalized.prompts,
            examples=normalized.examples,
        )

    def reset(self, template_id: str) -> None:
        if template_id not in _BUILTIN_TEMPLATE_IDS:
            raise ExtractServiceError("E_TMPL_001", f"template reset only supports builtin templates: {template_id}")
        self._builtin_overrides.pop(template_id, None)

    def delete(self, template_id: str) -> None:
        if template_id in _BUILTIN_TEMPLATE_IDS:
            raise ExtractServiceError("E_TMPL_001", f"builtin template cannot be deleted: {template_id}")
        self._user_templates.pop(template_id, None)

    def validate_draft(
        self,
        *,
        name: str,
        prompts: str,
        examples: str | list[list[str]],
        exclude_id: str | None = None,
    ) -> TemplateDraftValidationResult:
        try:
            self._normalize_draft(
                name=name,
                prompts=prompts,
                examples=examples,
                exclude_id=exclude_id,
            )
        except ExtractServiceError as exc:
            return TemplateDraftValidationResult(ok=False, reason=exc.message)
        return TemplateDraftValidationResult(ok=True)

    def serialize(self) -> dict[str, object]:
        payload: list[dict[str, object]] = []
        for template_id, template in sorted(self._user_templates.items(), key=lambda item: item[1].name):
            payload.append(
                {
                    "id": template_id,
                    "kind": "user",
                    "name": template.name,
                    "prompts": template.prompts,
                    "examples": [list(row) for row in template.examples],
                }
            )
        for template_id, _builtin_key in _BUILTIN_ID_TO_KEY:
            override = self._builtin_overrides.get(template_id)
            if not override:
                continue
            item = {"id": template_id, "kind": "builtin_override"}
            item.update(override)
            payload.append(item)
        return {
            "templates": payload,
            "active_template_id": self.active_id,
        }

    @staticmethod
    def _parse_entry(item: Any, *, index: int) -> _StoredTemplate | None:
        if isinstance(item, PromptTemplate):
            return _StoredTemplate(
                id=f"user:legacy-{index}",
                builtin=False,
                name=item.name.strip(),
                prompts=item.description.strip(),
                examples=[list(row) for row in item.examples],
            )
        if not isinstance(item, dict):
            return None

        template_id = str(item.get("id", "")).strip()
        kind = str(item.get("kind", "")).strip()
        if template_id in _BUILTIN_TEMPLATE_IDS and kind == "builtin_override":
            override = TemplateCatalog._normalize_override_fields(item)
            return _StoredTemplate(
                id=template_id,
                builtin=True,
                name=str(override.get("name", "")),
                prompts=str(override.get("prompts", "")),
                examples=[list(row) for row in override.get("examples", [])] if "examples" in override else [],
            )
        if kind != "user" or not template_id.startswith("user:"):
            return None

        name = str(item.get("name", "")).strip()
        prompts = str(item.get("prompts", "")).strip()
        if not name or not prompts:
            return None
        try:
            examples = TemplateCatalog._normalize_examples(item.get("examples"))
        except ExtractServiceError:
            return None
        return _StoredTemplate(
            id=template_id,
            builtin=False,
            name=name,
            prompts=prompts,
            examples=examples,
        )

    @staticmethod
    def _normalize_override_fields(item: dict[str, object]) -> dict[str, object]:
        override: dict[str, object] = {}
        if "name" in item:
            name = str(item.get("name", "")).strip()
            if name:
                override["name"] = name
        if "prompts" in item:
            prompts = str(item.get("prompts", "")).strip()
            if prompts:
                override["prompts"] = prompts
        if "examples" in item:
            try:
                override["examples"] = TemplateCatalog._normalize_examples(item.get("examples"))
            except ExtractServiceError:
                pass
        return override

    @staticmethod
    def _override_payload(template: _StoredTemplate) -> dict[str, object]:
        payload: dict[str, object] = {}
        if template.name:
            payload["name"] = template.name
        if template.prompts:
            payload["prompts"] = template.prompts
        if template.examples:
            payload["examples"] = [list(row) for row in template.examples]
        return payload

    def _resolve_builtin_template(
        self,
        template_id: str,
        *,
        include_override: bool = True,
    ) -> PromptTemplate:
        builtin_key = dict(_BUILTIN_ID_TO_KEY)[template_id]
        template = BUILTIN_TEMPLATES[builtin_key]
        if not include_override:
            return template
        override = self._builtin_overrides.get(template_id, {})
        return PromptTemplate(
            name=str(override.get("name", template.name)),
            description=str(override.get("prompts", template.description)),
            examples=[
                list(row)
                for row in override.get("examples", template.examples)
            ],
            columns=template.columns,
            line_rules=template.line_rules,
            field_regions=template.field_regions,
            field_groups=template.field_groups,
            exclusive_group_pairs=template.exclusive_group_pairs,
            min_lines=template.min_lines,
            max_lines=template.max_lines,
            min_confidence=template.min_confidence,
        )

    def _resolve_active_id(self) -> str:
        candidate = self._active_template_id or _DEFAULT_ACTIVE_TEMPLATE_ID
        if candidate in _BUILTIN_TEMPLATE_IDS:
            return candidate
        if candidate in self._user_templates:
            return candidate
        return _DEFAULT_ACTIVE_TEMPLATE_ID

    def _normalize_draft(
        self,
        *,
        name: str,
        prompts: str,
        examples: str | list[list[str]],
        exclude_id: str | None = None,
    ) -> _StoredTemplate:
        normalized = self._normalize_draft_fields(
            name=name,
            prompts=prompts,
            examples=examples,
            exclude_id=exclude_id,
        )
        duplicate_names = {
            entry.name
            for entry in self.list_entries()
            if entry.id != exclude_id
        }
        if normalized.name in duplicate_names:
            raise ExtractServiceError("E_TMPL_009", "模板名称已存在")
        return normalized

    def _normalize_draft_fields(
        self,
        *,
        name: str,
        prompts: str,
        examples: str | list[list[str]],
        exclude_id: str | None = None,
    ) -> _StoredTemplate:
        normalized_name = name.strip()
        if not normalized_name:
            raise ExtractServiceError("E_TMPL_006", "模板名称不能为空")

        normalized_prompts = prompts.strip()
        if not normalized_prompts:
            raise ExtractServiceError("E_TMPL_007", "Prompts 不能为空")

        normalized_examples = self._normalize_examples(examples)
        if len(normalized_examples) < 2:
            raise ExtractServiceError("E_TMPL_008", "Examples 至少需要表头和一行示例数据")

        return _StoredTemplate(
            id=exclude_id or "",
            builtin=False,
            name=normalized_name,
            prompts=normalized_prompts,
            examples=normalized_examples,
        )

    def _validate_final_names(self, normalized_updates: dict[str, _StoredTemplate]) -> None:
        seen_names: dict[str, str] = {}
        for entry in self.list_entries():
            if entry.id in normalized_updates:
                candidate_name = normalized_updates[entry.id].name
            else:
                candidate_name = self.template_by_id(entry.id).name
            previous_id = seen_names.get(candidate_name)
            if previous_id is not None and previous_id != entry.id:
                raise ExtractServiceError("E_TMPL_009", f"{candidate_name}: 模板名称已存在")
            seen_names[candidate_name] = entry.id

    @staticmethod
    def _normalize_examples(value: Any) -> list[list[str]]:
        if isinstance(value, str):
            return format_examples(value)
        if isinstance(value, list):
            return format_examples(json.dumps(value, ensure_ascii=False))
        raise ExtractServiceError("E_PARSE_002", "examples must be a 2D array")


def project_active_template_config(config: AppConfig) -> AppConfig:
    raw_templates = list(getattr(config, "templates", []))
    active_template_id = getattr(config, "active_template_id", None)
    if not raw_templates and not active_template_id:
        return config

    catalog = TemplateCatalog.load(raw_templates, active_template_id)
    serialized = catalog.serialize()
    active_template = catalog.active_template()
    projected = copy.deepcopy(config)
    projected.templates = list(serialized["templates"])  # type: ignore[assignment]
    projected.active_template_id = str(serialized["active_template_id"])
    projected.prompts = active_template.description
    projected.examples_normalized = [list(row) for row in active_template.examples]
    projected.examples_raw = json.dumps(active_template.examples, ensure_ascii=False, indent=2)
    return projected
