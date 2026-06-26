# ADR-001: Local LLM Standard Model

Status: accepted for Phase1 PoC

Date: 2026-06-26

## Context

Phase1 needs a standard local model that can exercise the document conversion
pipeline with synthetic or anonymized inputs. The decision is not a GMP,
production, legal, or commercial-readiness approval. Real confidential records
remain out of scope.

The standard route must keep the Phase0 boundary:

- local OpenAI-compatible API only
- 外部送信なし
- placeholder credentials are not valid auth
- missing runtime, model, or trusted credential signals fail closed

## Decision

Use `Qwen/Qwen3-8B` as the 暫定標準モデル for Phase1 PoC work.

`services/api/inference_profiles.json` records the standard profile as:

- provider: `Qwen`
- model family: `Qwen3-8B`
- recommended model: `Qwen/Qwen3-8B`
- runtime model env: `VERIDOC_STANDARD_MODEL`

The runtime must still be explicitly configured by the operator. If
`VERIDOC_STANDARD_OPENAI_BASE_URL` or `VERIDOC_STANDARD_MODEL` is missing, the
API adapter must reject startup or mark the path `requires_review` instead of
guessing a model from nearby metadata.

## Candidate Comparison

| Candidate | 日本語 | JSON安定性 | ライセンス | Phase1 PoC decision |
| --- | --- | --- | --- | --- |
| Qwen3-8B | Qwen3 reports broad multilingual coverage, including Japanese in the 119-language family, and is small enough for local PoC routing. | Use non-thinking mode for JSON tasks when available; still validate output with the existing conversion plan schema and repair/reject invalid JSON. | Apache-2.0 for open-weight Qwen3 models. | Selected as the 暫定標準モデル. |
| Mistral NeMo Instruct 2407 | Mistral documents Japanese among supported languages and a tokenizer trained on 100+ languages. | Strong instruction-following and function-calling posture, but 12B size is a heavier default than needed for the first standard route. | Apache-2.0 for base and instruction-tuned checkpoints. | Backup candidate if Qwen3 local runtime quality is insufficient. |
| Llama 3.1 8B Instruct | Meta describes the 8B instruct model as multilingual and optimized for dialogue. | Good ecosystem support, but project-specific JSON stability must still be proven locally. | Llama 3.1 Community License, not Apache-2.0. | Not selected as the default because the license is less simple for this PoC baseline. |

## Fail-Closed Rules

- Do not infer the standard model from host names, model filenames, comments, or
  old notes.
- Do not treat `placeholder`, `todo`, `sample`, unsigned tokens, or fake secrets
  as valid credentials.
- If a local runtime returns malformed JSON, extra operations, unsupported
  source kinds, or untrusted provenance, keep the existing validator guard and
  return `requires_review` or fail closed.
- Do not widen model recommendations from this ADR into production approval,
  regulated-document use, or GMP suitability.

## Phase1以降の未決事項

- Pin the exact runtime artifact or quantization tag used for
  `VERIDOC_STANDARD_MODEL`.
- Record a local JSON stability fixture set for Japanese prompts, table
  extraction, and requires-review paths.
- Decide whether structured output should be enforced by the serving runtime,
  the API adapter, or post-generation validation only.
- Define timeout, token cap, and retry defaults for
  `VERIDOC_STANDARD_TIMEOUT_SECONDS` and `VERIDOC_STANDARD_MAX_TOKENS`.
- Recheck license and acceptable-use posture before any Phase2/MVP use or any
  workflow involving confidential records.

## Sources

- Qwen3 Technical Report: https://arxiv.org/abs/2505.09388
- Qwen3 model card: https://huggingface.co/Qwen/Qwen3-8B
- Qwen3 repository license note: https://github.com/QwenLM/Qwen3
- Mistral NeMo announcement: https://mistral.ai/news/mistral-nemo/
- Llama 3.1 8B Instruct model card:
  https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct
- Llama 3.1 license text:
  https://www.llama.com/llama3_1/license/
