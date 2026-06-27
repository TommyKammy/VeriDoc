# VeriDoc

## Template Definition Schema

Phase3 template definitions are modeled by `core/ir/template-definition.schema.json`.
The synthetic example in `core/ir/examples/sample-template-definition.json` covers
template identity/versioning, document type, anchors, fields, tables, risk ranking,
validation rules, and output mapping. Validate the schema/sample contract with:

```bash
python3 -m unittest tests.test_template_definition_schema
```
VeriDocはPDF／Word／Excelを対象とした文書変換・再構成システム

## Local PoC API authentication

`services/api/poc_web.py` can enforce local bearer-token authentication when
`VERIDOC_LOCAL_AUTH_TOKENS` is configured. Use comma-separated
`role:principal-id=token` entries:

```bash
VERIDOC_LOCAL_AUTH_TOKENS='viewer:<viewer-id>=<viewer-token>,reviewer:<reviewer-id>=<reviewer-token>,approver:<approver-id>=<approver-token>,admin:<admin-id>=<admin-token>' python3 -m services.api.poc_web
```

Roles are intentionally narrow:

- `viewer`: read jobs, downloads, and review audit events.
- `reviewer`: viewer access plus conversions, job creation, and review edit events.
- `approver`: reviewer access plus review approve events.
- `admin`: approver access plus retrying failed conversion jobs.
