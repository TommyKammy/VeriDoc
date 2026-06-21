### OCR extraction spike

Issue #5 evaluates whether Phase0 can extract text, page-local region
coordinates, and confidence values from a scanned PDF without sending document
content to an external service.

## Current candidate comparison

| Candidate | Result | bbox support | confidence support | Notes |
| --- | --- | --- | --- | --- |
| Tesseract CLI | selected local execution path | yes | yes | Uses a locally installed `tesseract` executable and TSV output. The repository does not vendor the OCR binary or language data. |
| PaddleOCR | comparison adapter | yes | yes | Uses a locally installed `paddleocr` Python runtime only when explicit local detection and recognition model directories are configured. Model installation/cache management remains outside this repository. |

## Intermediate OCR-to-IR shape

OCR output is represented by `OcrExtraction.to_dict()` before it is mapped into
the document IR:

```json
{
  "source_path": "<input-pdf>",
  "engine": "tesseract",
  "pages": [
    {
      "page_number": 1,
      "width_px": 1224,
      "height_px": 1584,
      "status": "ok",
      "low_confidence_count": 0,
      "regions": [
        {
          "text": "LOT-001",
          "page_number": 1,
          "bbox": {
            "x": 10.0,
            "y": 12.0,
            "width": 60.0,
            "height": 14.0,
            "unit": "px",
            "origin": "top-left"
          },
          "confidence": 91.5,
          "low_confidence": false,
          "engine": "tesseract"
        }
      ]
    }
  ]
}
```

The coordinate system is top-left-origin pixels in the rasterized page image.
This is intentionally separate from the PDF point coordinate contract used by
text-native PDF extraction.

## Failure and low-confidence handling

- A missing source file raises `FileNotFoundError`.
- A PDF that cannot be opened raises `ValueError` and must not be treated as a
  successful OCR result.
- Missing local OCR runtime dependencies are reported as `not-installed` in
  candidate comparison rather than inferred as success.
- PaddleOCR comparison is blocked unless
  `VERIDOC_PADDLEOCR_TEXT_DETECTION_MODEL_DIR` and
  `VERIDOC_PADDLEOCR_TEXT_RECOGNITION_MODEL_DIR` point to existing local model
  directories, so the adapter does not trigger model downloads during
  verification.
- Empty OCR text regions and regions whose clipped bbox has no positive visible
  area are skipped because they cannot satisfy the downstream coordinate
  contract.
- Missing confidence values and confidence values below the configured threshold
  are marked `low_confidence`; any page containing such regions has page status
  `low-confidence`.
- Pages with no accepted text regions have status `no-text`.
- The implemented adapters use local PDF rasterization and local OCR runtimes.
  They do not call external OCR APIs or send document content to a service.

## Focused verification

The focused regression command is:

```bash
python3 -m pytest tests/test_ocr_extraction.py
```

The test uses a synthetic image-only PDF and fake OCR adapters so the repository
can verify PDF rasterization, OCR output normalization, bbox/confidence
preservation, and fail-closed candidate reporting without requiring real
confidential documents or host-installed OCR engines.
