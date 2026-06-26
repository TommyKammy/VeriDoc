from __future__ import annotations

_SECRET_PARAMETER_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "authentication",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "csrftoken",
        "connection_string",
        "jwt",
        "password",
        "private_key",
        "secret",
        "session",
        "sessionid",
        "sig",
        "signature",
        "set_cookie",
        "token",
        "xsrf",
        "xsrf_token",
    }
)
_SECRET_PARAMETER_KEY_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_authorization",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_sig",
    "_signature",
    "_token",
)
_SECRET_PARAMETER_KEY_PREFIXES = (
    "api_key_",
    "apikey_",
    "auth_",
    "authorization_",
    "credential_",
    "credentials_",
    "password_",
    "private_key_",
    "secret_",
    "sig_",
    "signature_",
    "token_",
)
_SECRET_PARAMETER_KEY_PHRASES = (
    "api_key",
    "apikey",
    "private_key",
    "secret",
    "signature",
    "connection_string",
)
_SECRET_PARAMETER_KEY_COMPONENTS = frozenset(
    {
        "authorization",
        "auth",
        "authentication",
        "cookie",
        "credential",
        "credentials",
        "jwt",
        "password",
        "secret",
        "session",
        "signature",
        "sig",
        "token",
    }
)
_SECRET_PARAMETER_KEY_COMPONENT_SEQUENCES = (
    ("account", "key"),
    ("api", "key"),
    ("access", "key"),
    ("functions", "key"),
    ("private", "key"),
    ("subscription", "key"),
)

_CONTENT_BEARING_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "content",
        "attachment",
        "attachments",
        "body",
        "data",
        "document",
        "form_data",
        "input",
        "instructions",
        "json",
        "message",
        "messages",
        "output_bytes",
        "previous_response",
        "prompt",
        "output",
        "output_data",
        "payload",
        "raw_data",
        "request_body",
        "request_data",
        "source",
        "source_bytes",
        "source_data",
        "synthetic_text",
        "text",
        "upload",
        "uploads",
    }
)
_SAFE_CONTENT_WORD_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "content_type",
        "content_encoding",
        "content_length",
        "content_md5",
        "max_tokens",
        "max_prompt_tokens",
        "x_amz_content_sha256",
    }
)
_CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENTS = frozenset(
    {
        "content",
        "attachment",
        "attachments",
        "body",
        "document",
        "form_data",
        "input",
        "instructions",
        "message",
        "messages",
        "prompt",
        "payload",
        "text",
        "upload",
        "uploads",
    }
)
_CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENT_SEQUENCES = (
    ("form", "data"),
    ("json", "data"),
    ("json", "output"),
    ("json", "raw"),
    ("json", "request"),
    ("json", "response"),
    ("json", "result"),
    ("raw", "source"),
    ("raw", "output"),
)
_CONTENT_BYTE_AUDIT_PARAMETER_ANCESTOR_COMPONENTS = frozenset(
    {
        "output",
        "source",
    }
)

_SAFE_MESSAGE_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "assistant_message_id",
        "last_message_at",
        "message_count",
        "message_id",
        "message_role",
        "message_status",
        "message_type",
        "system_message_id",
        "user_message_id",
    }
)
_SAFE_MESSAGE_METADATA_DESCRIPTOR_COMPONENTS = frozenset(
    {
        "at",
        "count",
        "id",
        "ids",
        "index",
        "name",
        "role",
        "status",
        "timestamp",
        "type",
    }
)
_SAFE_DATA_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "meta_data",
        "model_data",
    }
)
_SAFE_JSON_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "metadata_json",
        "schema_json",
    }
)
_JSON_ENCODED_AUDIT_METADATA_KEYS = frozenset(
    {
        "metadata_json",
        "schema_json",
    }
)
_SAFE_FORM_DATA_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "form_data_content_type",
        "form_data_description",
        "form_data_type",
        "multipart_form_data_content_type",
        "multipart_form_data_description",
        "multipart_form_data_type",
        "request_form_data_content_type",
        "request_form_data_description",
        "request_form_data_type",
    }
)
_SAFE_DESCRIPTOR_COMPONENT_SEQUENCES = (
    ("content", "type"),
    ("description",),
    ("id",),
    ("status",),
    ("status", "code"),
    ("type",),
)
_SAFE_AUDIT_PARAMETER_SEQUENCE_KEYS = frozenset(
    {
        "stop",
    }
)
_SAFE_TWO_STRING_AUDIT_PARAMETER_LIST_KEYS = frozenset(
    {
        "generation_parameters",
        "model_parameters",
    }
)

_SAFE_SCHEMA_LITERAL_STRINGS = frozenset(
    {
        "accepted",
        "active",
        "approved",
        "closed",
        "complete",
        "completed",
        "draft",
        "error",
        "failed",
        "final",
        "inactive",
        "open",
        "optional",
        "pending",
        "rejected",
        "required",
        "success",
        "summary",
        "unknown",
    }
)
_JSON_SCHEMA_VALUE_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "const",
        "default",
        "enum",
        "examples",
    }
)
_JSON_SCHEMA_SINGLE_SCHEMA_KEYS = frozenset(
    {
        "additional_properties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "property_names",
        "then",
        "unevaluated_items",
        "unevaluated_properties",
    }
)
_JSON_SCHEMA_SCHEMA_ARRAY_KEYS = frozenset(
    {
        "all_of",
        "any_of",
        "one_of",
        "prefix_items",
    }
)
_JSON_SCHEMA_SCHEMA_MAP_KEYS = frozenset(
    {
        "defs",
        "definitions",
        "dependent_schemas",
        "pattern_properties",
        "properties",
    }
)
_SAFE_JSON_SCHEMA_AUDIT_PARAMETER_METADATA_KEYS = frozenset(
    {
        "content_encoding",
        "content_media_type",
        "data_type",
        "description",
        "format",
        "title",
        "type",
    }
)

_KEY_VALUE_AUDIT_PARAMETER_SEQUENCE_CONTAINER_KEYS = frozenset(
    {
        "cookies",
        "extra_cookies",
        "extra_headers",
        "files",
        "headers",
        "http_headers",
        "options",
        "params",
        "parameters",
        "query_params",
        "request_headers",
    }
)
_QUERY_AUDIT_PARAMETER_CONTAINER_PREFIX_COMPONENTS = frozenset(
    {
        "callback",
        "custom",
        "default",
        "extra",
        "query",
        "redirect",
        "request",
        "search",
        "uri",
        "url",
    }
)
_RAW_AUDIT_PARAMETER_CONTAINER_PREFIX_COMPONENTS = frozenset(
    {
        "provider",
    }
)
_FILE_AUDIT_PARAMETER_CONTAINER_KEYS = frozenset({"file", "files"})
