from __future__ import annotations
CODEX_TEXT_MODELS: tuple[tuple[str, str], ...] = (('gpt-6.1-sol', 'GPT 6.1 Sol'), ('gpt-6-astra', 'GPT 6 Astra'), ('gpt-6-sol', 'GPT 6 Sol'), ('gpt-6-luna', 'GPT 6 Luna'), ('gpt-5.6-sol', 'GPT 5.6 Sol'), ('gpt-5.6-terra', 'GPT 5.6 Terra'), ('gpt-5.6-luna', 'GPT 5.6 Luna'))
CODEX_TEXT_MODEL_IDS = frozenset((model for model, _label in CODEX_TEXT_MODELS))
DEFAULT_CODEX_TEXT_MODEL = 'gpt-5.6-luna'
