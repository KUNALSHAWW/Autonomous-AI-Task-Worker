"""Runtime settings, all overridable with environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

PROVIDER_PRESETS = {
    # Default: ChatOllama. With OLLAMA_API_KEY it talks to Ollama Cloud, without it to a local Ollama.
    "ollama": ("", "OLLAMA_API_KEY", "gpt-oss:120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-flash"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "qwen/qwen3-235b-a22b:free"),
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN", "openai/gpt-oss-120b:fastest"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4.1-mini"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b"),
    "anthropic": ("https://api.anthropic.com", "ANTHROPIC_API_KEY", "claude-sonnet-4-5"),
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _detect_provider() -> str:
    explicit = _env("LLM_PROVIDER")
    if explicit:
        return explicit
    if _env("OLLAMA_API_KEY") or _env("OLLAMA_MODEL") or _env("OLLAMA_BASE_URL"):
        return "ollama"
    for prov, (_, key_env, _) in PROVIDER_PRESETS.items():
        if prov != "ollama" and _env(key_env):
            return prov
    return "ollama"


@dataclass
class Settings:
    provider: str = field(default_factory=_detect_provider)
    model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    native_tools: bool = field(default_factory=lambda: _env("LLM_NATIVE_TOOLS", "1") != "0")
    # Extra JSON merged into every request body, e.g. '{"reasoning_effort": "low"}'
    extra_body: str = field(default_factory=lambda: _env("LLM_EXTRA_BODY"))

    environment_file: str = field(default_factory=lambda: _env(
        "WORKER_ENVIRONMENT", os.path.join(os.path.dirname(__file__), "..", "config", "environment.yaml")))
    sandbox_url: str = field(default_factory=lambda: _env("SANDBOX_URL", "http://127.0.0.1:8100"))
    runs_dir: str = field(default_factory=lambda: _env(
        "RUNS_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "runs")))
    headless: bool = field(default_factory=lambda: _env("HEADLESS", "1") != "0")

    max_steps: int = field(default_factory=lambda: int(_env("MAX_STEPS", "45")))
    max_minutes: float = field(default_factory=lambda: float(_env("MAX_MINUTES", "20")))
    max_repair_rounds: int = field(default_factory=lambda: int(_env("MAX_REPAIR_ROUNDS", "2")))
    observation_chars: int = field(default_factory=lambda: int(_env("OBSERVATION_CHARS", "7000")))
    approval_timeout_s: float = field(default_factory=lambda: float(_env("APPROVAL_TIMEOUT_S", "3600")))

    def resolved(self) -> tuple[str, str, str]:
        base, key_env, model = PROVIDER_PRESETS.get(self.provider, ("", "LLM_API_KEY", ""))
        if self.provider == "ollama":
            return (self.base_url or _env("OLLAMA_BASE_URL"), self.api_key or _env(key_env),
                    self.model or _env("OLLAMA_MODEL") or model)
        return (self.base_url or base, self.api_key or _env(key_env), self.model or model)

    @property
    def llm_configured(self) -> bool:
        base, key, model = self.resolved()
        return bool(key) or (self.provider == "ollama" and bool(base))


def make_chat_model(s: Settings):
    """The LangChain chat model for the configured provider (used by the deep-agent
    investigator). Returns None for providers without a LangChain integration here."""
    base, key, model = s.resolved()
    if s.provider == "ollama":
        from .llm.langchain_llm import make_ollama
        return make_ollama(model, key, base)
    if s.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=key, temperature=0.1, max_tokens=4000)
    if s.provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0.1)
    return None


def make_llm(s: Settings):
    from .llm.anthropic_llm import AnthropicLLM
    from .llm.openai_compat import OpenAICompatLLM

    base, key, model = s.resolved()
    if s.provider == "ollama":
        from .llm.langchain_llm import LangChainLLM, make_ollama
        if not key and not base:
            raise RuntimeError("No Ollama endpoint configured. Set OLLAMA_API_KEY (Ollama Cloud) and OLLAMA_MODEL, "
                               "or OLLAMA_BASE_URL for a local Ollama server (see README).")
        return LangChainLLM(make_ollama(model, key, base), f"{model} @ ChatOllama ({base or 'ollama.com'})",
                            native_tools=s.native_tools)
    if s.provider == "anthropic":
        return AnthropicLLM(key, model)
    if not key:
        raise RuntimeError(
            f"No API key for provider '{s.provider}'. Set {PROVIDER_PRESETS.get(s.provider, ('', 'LLM_API_KEY'))[1]} "
            f"or LLM_API_KEY (see README).")
    import json

    extra = json.loads(s.extra_body) if s.extra_body else None
    return OpenAICompatLLM(base, key, model, native_tools=s.native_tools, extra_body=extra)
