"""LLM-агенты 1..3 через Anthropic SDK (docs/modules/pipeline/03-architecture.md).

Промты — в app/pipeline/prompts/. Маппинг агент→модель — в app/core/config.
Prompt caching стабильных system-промтов; cost-ledger в llm_usage.
"""
