# Local Inference Setup

This Phase0 setup records the local inference contract for the future `services/api`
implementation. It is a PoC prerequisite only: GMP適合や業務利用可能性は主張しない.

## Boundary

- Inference must run with 外部送信なし.
- The API calls an OpenAI互換 API endpoint on the operator-controlled local network boundary.
- Do not commit model weights, real regulated documents, real records, API keys, or generated private outputs.
- Placeholder credentials are not valid auth. If a local runtime requires an API key, provide it from an operator-managed secret source outside the repository.

## Profile Source

The machine-readable profile contract is `services/api/inference_profiles.json`.
The file defines two local routes:

- 標準モード: `Qwen/Qwen3-8B` as the Phase1 PoC 暫定標準モデル through an operator-controlled local OpenAI-compatible runtime.
- 高品質モード: DeepSeek V4 Flash on DwarfStar 4 exposed through an OpenAI互換 API.

Both profiles set `egress` to `disabled` and use `credential_source` value
`local-placeholder-only` so implementation work fails closed until real local
runtime settings are provided.

The standard-model selection is recorded in
`adr/ADR-001-local-llm-standard-model.md`. That ADR compares 日本語,
JSON安定性, and ライセンス posture for the Phase1 candidates and records the
remaining Phase1 open items.

## Environment Variables

Standard mode:

- `VERIDOC_STANDARD_OPENAI_BASE_URL`: local OpenAI-compatible base URL, for example `http://127.0.0.1:<port>/v1`.
- `VERIDOC_STANDARD_MODEL`: model identifier served by the standard local runtime; default PoC target is `Qwen/Qwen3-8B`.
- `VERIDOC_STANDARD_OPENAI_API_KEY`: optional local runtime token; use a placeholder only for runtimes that ignore auth.
- `VERIDOC_STANDARD_TIMEOUT_SECONDS`: optional request timeout.
- `VERIDOC_STANDARD_MAX_TOKENS`: optional response-token cap for API implementation tests.

High-quality mode:

- `VERIDOC_HIGH_QUALITY_OPENAI_BASE_URL`: DwarfStar 4 OpenAI-compatible base URL, for example `http://<dwarfstar-host>:<port>/v1`.
- `VERIDOC_HIGH_QUALITY_MODEL`: DeepSeek V4 Flash model identifier served by DwarfStar 4.
- `VERIDOC_HIGH_QUALITY_OPENAI_API_KEY`: optional local runtime token; use a placeholder only for runtimes that ignore auth.
- `VERIDOC_HIGH_QUALITY_TIMEOUT_SECONDS`: optional request timeout.
- `VERIDOC_HIGH_QUALITY_MAX_TOKENS`: optional response-token cap for API implementation tests.

## Local Smoke Check

Use the runtime's OpenAI-compatible `/chat/completions` path from the local
network boundary. A future `services/api` adapter should select either the
`standard` or `high_quality` profile, read only the matching env vars, and reject
startup when the required base URL or model is missing.

The smoke check should use synthetic text only. Do not use 実機密文書, 実記録書,
or customer data for this issue.
