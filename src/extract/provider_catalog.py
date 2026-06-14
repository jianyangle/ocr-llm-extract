from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal


RuntimeProvider = Literal["openai_compatible", "ollama"]
ModelFetchStrategy = Literal["openai_models", "ollama_tags"]

DEFAULT_PROVIDER_PLATFORM_ID = "custom"


@dataclass(frozen=True)
class ProviderProfile:
    base_url: str
    api_key: str
    model: str

    def as_dict(self) -> dict[str, str]:
        return {"base_url": self.base_url, "api_key": self.api_key, "model": self.model}


@dataclass(frozen=True)
class ProviderCatalogEntry:
    id: str
    display_name: str
    runtime_provider: RuntimeProvider
    default_base_url: str
    logo_asset: str
    website_url: str
    api_key_url: str
    models_url: str
    recommended_models: tuple[str, ...]
    requires_api_key: bool
    model_fetch_strategy: ModelFetchStrategy
    thinking_disable_params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "thinking_disable_params",
            deepcopy(object.__getattribute__(self, "thinking_disable_params")),
        )

    def __getattribute__(self, name: str) -> Any:
        value = object.__getattribute__(self, name)
        if name == "thinking_disable_params":
            return deepcopy(value)
        return value


_CATALOG: tuple[ProviderCatalogEntry, ...] = (
    ProviderCatalogEntry(
        id="silicon",
        display_name="硅基流动",
        runtime_provider="openai_compatible",
        default_base_url="https://api.siliconflow.cn/v1",
        logo_asset="providers/silicon.png",
        website_url="https://www.siliconflow.cn",
        api_key_url="https://cloud.siliconflow.cn/account/ak",
        models_url="https://cloud.siliconflow.cn/models",
        recommended_models=("deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen3-8B"),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
        thinking_disable_params={"extra_body": {"enable_thinking": False}},
    ),
    ProviderCatalogEntry(
        id="deepseek",
        display_name="深度求索",
        runtime_provider="openai_compatible",
        default_base_url="https://api.deepseek.com",
        logo_asset="providers/deepseek.png",
        website_url="https://deepseek.com/",
        api_key_url="https://platform.deepseek.com/api_keys",
        models_url="https://api-docs.deepseek.com/",
        recommended_models=("deepseek-v4-flash", "deepseek-v4-pro"),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
        thinking_disable_params={"extra_body": {"thinking": {"type": "disabled"}}},
    ),
    ProviderCatalogEntry(
        id="ollama",
        display_name="Ollama",
        runtime_provider="ollama",
        default_base_url="http://localhost:11434",
        logo_asset="providers/ollama.png",
        website_url="https://ollama.com/",
        api_key_url="",
        models_url="https://ollama.com/library",
        recommended_models=("qwen3:8b", "llama3.1:8b", "deepseek-r1:8b"),
        requires_api_key=False,
        model_fetch_strategy="ollama_tags",
        thinking_disable_params={"think": False},
    ),
    ProviderCatalogEntry(
        id="zhipu",
        display_name="智谱",
        runtime_provider="openai_compatible",
        default_base_url="https://open.bigmodel.cn/api/paas/v4/",
        logo_asset="providers/zhipu.png",
        website_url="https://open.bigmodel.cn/",
        api_key_url="https://open.bigmodel.cn/usercenter/apikeys",
        models_url="https://open.bigmodel.cn/modelcenter/square",
        recommended_models=("glm-5.1", "glm-4.7", "glm-4.5-flash"),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
    ),
    ProviderCatalogEntry(
        id="moonshot",
        display_name="月之暗面",
        runtime_provider="openai_compatible",
        default_base_url="https://api.moonshot.cn/v1",
        logo_asset="providers/moonshot.webp",
        website_url="https://www.moonshot.cn/",
        api_key_url="https://platform.kimi.com/console/api-keys",
        models_url="https://platform.kimi.ai/docs/guide/faq",
        recommended_models=("kimi-k2.6", "kimi-k2.5", "moonshot-v1-128k"),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
    ),
    ProviderCatalogEntry(
        id="doubao",
        display_name="火山引擎",
        runtime_provider="openai_compatible",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
        logo_asset="providers/volcengine.png",
        website_url="https://console.volcengine.com/ark/",
        api_key_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        models_url="https://console.volcengine.com/ark/region:ark+cn-beijing/endpoint",
        recommended_models=("doubao-seed-1-6-250615", "doubao-seed-1-8-251228", "deepseek-v3-250324"),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
    ),
    ProviderCatalogEntry(
        id="minimax",
        display_name="MiniMax",
        runtime_provider="openai_compatible",
        default_base_url="https://api.minimaxi.com/v1",
        logo_asset="providers/minimax.png",
        website_url="https://platform.minimaxi.com/",
        api_key_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
        models_url="https://platform.minimaxi.com/docs/api-reference/api-overview",
        recommended_models=("MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5"),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
    ),
    ProviderCatalogEntry(
        id="hunyuan",
        display_name="腾讯混元",
        runtime_provider="openai_compatible",
        default_base_url="https://api.hunyuan.cloud.tencent.com/v1",
        logo_asset="providers/hunyuan.png",
        website_url="https://cloud.tencent.com/product/hunyuan",
        api_key_url="https://console.cloud.tencent.com/hunyuan/api-key",
        models_url="https://cloud.tencent.com/document/product/1729/111007",
        recommended_models=("hunyuan-turbos-latest", "hunyuan-standard", "hunyuan-lite"),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
    ),
    ProviderCatalogEntry(
        id="dashscope",
        display_name="阿里云百炼",
        runtime_provider="openai_compatible",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/",
        logo_asset="providers/bailian.png",
        website_url="https://www.aliyun.com/product/bailian",
        api_key_url="https://bailian.console.aliyun.com/?tab=model#/api-key",
        models_url="https://bailian.console.aliyun.com/?tab=model#/model-market",
        recommended_models=("qwen-plus", "qwen-max", "qwen3.5-plus", "deepseek-v3.2"),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
        thinking_disable_params={"extra_body": {"enable_thinking": False}},
    ),
    ProviderCatalogEntry(
        id="custom",
        display_name="自定义",
        runtime_provider="openai_compatible",
        default_base_url="",
        logo_asset="providers/custom.svg",
        website_url="",
        api_key_url="",
        models_url="",
        recommended_models=(),
        requires_api_key=True,
        model_fetch_strategy="openai_models",
    ),
)

PROVIDER_PLATFORM_IDS = tuple(entry.id for entry in _CATALOG)
_BY_ID = {entry.id: entry for entry in _CATALOG}


def get_provider_catalog() -> tuple[ProviderCatalogEntry, ...]:
    return _CATALOG


def get_provider_entry(platform_id: str) -> ProviderCatalogEntry:
    return _BY_ID.get(platform_id, _BY_ID[DEFAULT_PROVIDER_PLATFORM_ID])


def runtime_provider_for_platform(platform_id: str) -> RuntimeProvider:
    return get_provider_entry(platform_id).runtime_provider


def coerce_provider_platform_id(value: object, *, runtime_provider: str) -> str:
    platform_id = str(value or "").strip()
    if platform_id in _BY_ID and platform_id not in {"openai_compatible"}:
        return platform_id
    if runtime_provider == "ollama":
        return "ollama"
    return DEFAULT_PROVIDER_PLATFORM_ID


def profile_from_top_level(data: dict[str, object]) -> ProviderProfile:
    return ProviderProfile(
        base_url=_string_field(data.get("base_url")),
        api_key=_string_field(data.get("api_key")),
        model=_string_field(data.get("model")),
    )


def profile_from_mapping(raw: object, fallback: ProviderProfile) -> ProviderProfile:
    if not isinstance(raw, dict):
        return fallback
    return ProviderProfile(
        base_url=_string_field(raw.get("base_url", fallback.base_url)),
        api_key=_string_field(raw.get("api_key", fallback.api_key)),
        model=_string_field(raw.get("model", fallback.model)),
    )


def profile_with_catalog_default(platform_id: str, profile: ProviderProfile) -> ProviderProfile:
    entry = get_provider_entry(platform_id)
    base_url = profile.base_url.strip() or entry.default_base_url
    model = profile.model.strip() or (entry.recommended_models[0] if entry.recommended_models else "")
    return ProviderProfile(base_url=base_url, api_key=profile.api_key, model=model)


def catalog_default_profiles() -> dict[str, ProviderProfile]:
    return {
        entry.id: ProviderProfile(
            base_url=entry.default_base_url,
            api_key="",
            model=entry.recommended_models[0] if entry.recommended_models else "",
        )
        for entry in _CATALOG
    }


def profiles_as_dict(profiles: dict[str, ProviderProfile]) -> dict[str, dict[str, str]]:
    return {key: value.as_dict() for key, value in profiles.items() if key in _BY_ID}


def profiles_from_dict(raw_profiles: object) -> dict[str, ProviderProfile]:
    profiles = catalog_default_profiles()
    if isinstance(raw_profiles, dict):
        for platform_id in PROVIDER_PLATFORM_IDS:
            profiles[platform_id] = profile_from_mapping(raw_profiles.get(platform_id), profiles[platform_id])
    return {
        platform_id: profile_with_catalog_default(platform_id, profile)
        for platform_id, profile in profiles.items()
    }


def _string_field(value: object) -> str:
    if value is None:
        return ""
    return str(value)
