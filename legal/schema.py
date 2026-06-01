"""JSON Schema for legal CLI agent responses."""

from __future__ import annotations

from typing import Any


LEGAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://webcam.local/schemas/legal-cli-response.schema.json",
    "title": "Legal CLI agent response",
    "description": "Uniform machine-readable response emitted by legal.cli.",
    "oneOf": [
        {"$ref": "#/$defs/success_response"},
        {"$ref": "#/$defs/error_response"},
    ],
    "$defs": {
        "json_object": {
            "type": "object",
            "additionalProperties": True,
        },
        "document_metadata": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "pdf_bytes": {"type": "integer", "minimum": 0},
                "saved": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        "string_array": {
            "type": "array",
            "items": {"type": "string"},
        },
        "provenance": {
            "type": "object",
            "required": ["source_urls", "fetched_urls", "fetched_at"],
            "properties": {
                "source_urls": {"$ref": "#/$defs/string_array"},
                "fetched_urls": {"$ref": "#/$defs/string_array"},
                "fetched_at": {"type": "string"},
                "source_map": {"type": "string"},
                "source_response_id": {"type": "string"},
                "raw": {"$ref": "#/$defs/json_object"},
            },
            "additionalProperties": False,
        },
        "page_info": {
            "type": "object",
            "required": ["has_more"],
            "properties": {
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
                "page": {"type": "integer"},
                "total": {"type": "integer"},
                "has_more": {"type": "boolean"},
                "next_cursor": {"type": "string"},
                "search_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "legal_item": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
                "date": {"type": "string"},
                "document_type": {"type": "string"},
                "url": {"type": "string"},
                "file_url": {"type": "string"},
                "snippet": {"type": "string"},
                "facets": {"$ref": "#/$defs/json_object"},
                "source_fields": {"$ref": "#/$defs/json_object"},
                "raw": {"$ref": "#/$defs/json_object"},
                "provenance": {"$ref": "#/$defs/provenance"},
            },
            "additionalProperties": False,
        },
        "legal_document": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
                "date": {"type": "string"},
                "document_type": {"type": "string"},
                "body": {"type": "string"},
                "url": {"type": "string"},
                "file_url": {"type": "string"},
                "content_type": {"type": "string"},
                "text_format": {"type": "string"},
                "metadata": {"$ref": "#/$defs/document_metadata"},
                "links": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/json_object"},
                },
                "files": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/json_object"},
                },
                "source_fields": {"$ref": "#/$defs/json_object"},
                "raw": {"$ref": "#/$defs/json_object"},
                "provenance": {"$ref": "#/$defs/provenance"},
            },
            "additionalProperties": False,
        },
        "unsupported_operation": {
            "type": "object",
            "required": ["operation", "error_code"],
            "properties": {
                "operation": {"type": "string"},
                "error_code": {"type": "string"},
                "capability_required": {"type": "string"},
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "source_info": {
            "type": "object",
            "required": ["id", "name", "operations", "browser_required"],
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "operations": {"$ref": "#/$defs/string_array"},
                "source_map": {"type": "string"},
                "notes": {"type": "string"},
                "capabilities": {"$ref": "#/$defs/string_array"},
                "browser_required": {"type": "boolean"},
                "unsupported_operations": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/unsupported_operation"},
                },
            },
            "additionalProperties": False,
        },
        "response_item": {
            "oneOf": [
                {"$ref": "#/$defs/legal_item"},
                {"$ref": "#/$defs/source_info"},
            ]
        },
        "legal_error": {
            "type": "object",
            "required": ["code", "message", "retryable"],
            "properties": {
                "code": {
                    "type": "string",
                    "enum": [
                        "captcha_required",
                        "unsupported_captcha",
                        "source_unavailable",
                        "network_error",
                        "parse_error",
                        "not_found",
                        "usage_error",
                        "unsupported_operation",
                    ],
                },
                "message": {"type": "string"},
                "retryable": {"type": "boolean"},
                "capability_required": {"type": "string"},
                "details": {"$ref": "#/$defs/json_object"},
            },
            "additionalProperties": False,
        },
        "success_response": {
            "type": "object",
            "required": ["ok", "source", "operation", "warnings", "provenance"],
            "properties": {
                "ok": {"const": True},
                "source": {"type": "string"},
                "operation": {"type": "string"},
                "query": {"$ref": "#/$defs/json_object"},
                "request": {"$ref": "#/$defs/json_object"},
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/response_item"},
                },
                "document": {"$ref": "#/$defs/legal_document"},
                "facets": {"$ref": "#/$defs/json_object"},
                "page": {"$ref": "#/$defs/page_info"},
                "provenance": {"$ref": "#/$defs/provenance"},
                "warnings": {"$ref": "#/$defs/string_array"},
            },
            "anyOf": [
                {"required": ["items"]},
                {"required": ["document"]},
                {"required": ["facets"]},
            ],
            "not": {"required": ["error"]},
            "additionalProperties": False,
        },
        "error_response": {
            "type": "object",
            "required": ["ok", "source", "operation", "warnings", "error"],
            "properties": {
                "ok": {"const": False},
                "source": {"type": "string"},
                "operation": {"type": "string"},
                "query": {"$ref": "#/$defs/json_object"},
                "request": {"$ref": "#/$defs/json_object"},
                "error": {"$ref": "#/$defs/legal_error"},
                "provenance": {"$ref": "#/$defs/provenance"},
                "warnings": {"$ref": "#/$defs/string_array"},
            },
            "additionalProperties": False,
        },
    },
}
