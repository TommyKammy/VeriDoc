# MVP Performance and Size Limits

The Phase 12 MVP uses the repository-owned `acceptance_limits` object in
`datasets/mvp_evaluation_manifest_v1.json` as the authoritative measurement
baseline. These limits are acceptance guards for representative local runs;
they are not an SLA or a production load-test result.

| Guard | MVP limit | Measurement and failure behavior |
| --- | ---: | --- |
| Representative processing time | 10,000 ms per document | `--mvp-harness` measures wall-clock conversion time. A result above the limit fails `evaluations.processing_time`. |
| Upload size | 2,097,152 bytes (2 MiB) | The PoC API rejects a larger decoded upload with HTTP 413, `upload_too_large`, and `max_upload_bytes`. The harness fails `evaluations.input_size` before conversion. |
| Processing timeout | 30,000 ms per document | The direct PoC API terminates and reaps the isolated conversion worker before returning HTTP 504 with `processing_timeout` and `timeout_ms`. The harness stops waiting at the manifest deadline and emits a failed `evaluations.timeout`; the web UI translates the error code into an operator-facing timeout message. |

The measured processing-time and size boundaries are inclusive: a document at
exactly either configured limit is accepted, while a larger value fails. The
timeout is an enforced wait deadline, so reaching it stops the harness from
waiting. Missing, malformed, or non-positive limit values invalidate the MVP
manifest rather than silently disabling the guard.

## Measurement

Run the representative harness from the repository root:

```bash
python3 scripts/evaluate_dataset.py --mvp-harness
```

Record each result's `processing_time_ms`, `evaluations.input_size`,
`evaluations.processing_time`, and `evaluations.timeout`. Machine load can affect
wall-clock measurements, so a failed timing row should be repeated on the same
document and environment and retained as acceptance evidence; the configured
limit must not be relaxed merely to make a run pass.

This check covers five fixed representative documents and narrow boundary
tests. Sustained concurrency, percentile latency, capacity planning, and SLA
claims require a separate load-test plan and remain out of scope.
