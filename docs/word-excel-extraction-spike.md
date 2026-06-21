# Word and Excel extraction spike

Issue #4 evaluates whether Phase0 can extract DOCX and XLSX structure from
synthetic or sanitized samples only.

## Current extraction scope

| Source | Extracted structure | Notes |
| --- | --- | --- |
| DOCX | paragraphs, heading-style paragraphs, tables | Uses the OOXML package and `word/document.xml` directly. Heading detection is based on paragraph styles whose value starts with `Heading`. |
| XLSX | sheet names, dimensions, cell references, cell value types, merged ranges | Uses workbook relationships, shared strings, and worksheet XML directly. |

The current implementation intentionally avoids real confidential documents and
does not claim GMP suitability or production readiness.

## Intermediate data contract

DOCX extraction returns ordered blocks:

- `kind`: `heading`, `paragraph`, or `table`.
- `text`: block text, with table cells joined by tabs and rows joined by
  newlines for a stable plain-text preview.
- `style`: source paragraph style when present.
- `rows`: table cell text as row arrays for table blocks.

XLSX extraction returns ordered sheets:

- `name`: workbook sheet name.
- `dimension`: worksheet dimension reference when present.
- `cells`: cell reference, normalized value, and normalized value type.
- `merged_ranges`: merged-cell references such as `A3:C3`.

Normalized XLSX value types currently include:

- `shared_string`
- `inline_string`
- `string`
- `number`
- `boolean`
- `blank`

## Document IR v0 candidate fields

The current `document-ir/v0` schema can already represent the source type as
`docx` or `xlsx`, but Word and Excel inputs need additional structural fields
before the IR can faithfully preserve source layout:

| Candidate field | Applies to | Why it matters |
| --- | --- | --- |
| `source_block_index` | DOCX | Preserves document order when headings, paragraphs, and tables are converted into IR blocks. |
| `source_style` | DOCX | Keeps heading and paragraph style evidence instead of relying only on extracted text. |
| `table.rows` | DOCX, XLSX | Keeps cell grid structure separate from the plain-text table preview. |
| `sheet.name` | XLSX | Identifies workbook scope for extracted cells and table-like regions. |
| `sheet.dimension` | XLSX | Records the worksheet extent used by the extractor. |
| `cell.ref` | XLSX | Anchors values to their workbook coordinates. |
| `cell.value_type` | XLSX | Separates numbers, booleans, shared strings, inline strings, and blanks. |
| `merged_ranges` | XLSX | Preserves layout signals that affect table reconstruction. |

These fields should be treated as Phase0 IR design inputs, not as a finished
Phase1 or Phase2 end-to-end conversion contract.

## Failure handling

- A missing DOCX or XLSX source raises `FileNotFoundError`.
- A malformed package or required OOXML part raises `ValueError`.
- Missing worksheet relationships are rejected instead of guessed from sheet
  order or naming conventions.
- Invalid shared-string indexes are rejected because the extracted cell value
  cannot be authoritatively resolved.
