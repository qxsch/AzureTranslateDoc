"""LLM-powered glossary generation for enhanced translation.

Compares original source text with a machine-translated version and uses
an LLM to produce a TSV glossary of domain-specific terms, proper nouns,
and inconsistencies.  The glossary can then be fed back into the Azure
Translator for a second, more accurate translation pass.

Supports two backends (configured via environment):
  - **Azure OpenAI** (preferred in production) – uses managed identity
  - **OpenAI API** (fallback for local dev) – uses API key

The module gracefully returns an empty glossary when the LLM is
unavailable, so the caller can fall back to the standard translation.
"""

import logging
from typing import Optional

from ..config import settings

logger = logging.getLogger(__name__)

# Maximum characters of source / translated text to send to the LLM.
# This keeps the prompt well within token limits (~50K chars ≈ ~15K tokens).
_MAX_TEXT_CHARS = 50_000

# ---------------------------------------------------------------------------
# Client initialisation (lazy)
# ---------------------------------------------------------------------------

_client = None
_client_kind: Optional[str] = None  # "azure" | "openai" | None


def _get_client():
    """Lazily initialise and return the OpenAI client + model name.

    Prefers Azure OpenAI with managed identity when configured.
    Falls back to OpenAI API key for local development.
    """
    global _client, _client_kind

    if _client is not None:
        model = (
            settings.azure_openai_deployment
            if _client_kind == "azure"
            else settings.openai_model
        )
        return _client, model

    # --- Azure OpenAI (API key preferred, managed identity fallback) ---
    if settings.azure_openai_endpoint:
        try:
            from openai import AzureOpenAI

            if settings.azure_openai_key:
                # API key auth – works everywhere (local, Docker, cloud)
                _client = AzureOpenAI(
                    azure_endpoint=settings.azure_openai_endpoint,
                    api_key=settings.azure_openai_key,
                    api_version=settings.azure_openai_api_version,
                )
                _client_kind = "azure"
                logger.info(
                    "Glossary generator: using Azure OpenAI at %s "
                    "(deployment=%s, auth=api-key)",
                    settings.azure_openai_endpoint,
                    settings.azure_openai_deployment,
                )
                return _client, settings.azure_openai_deployment

            # Managed identity fallback
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            credential = DefaultAzureCredential()
            token_provider = get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default"
            )
            _client = AzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                azure_ad_token_provider=token_provider,
                api_version=settings.azure_openai_api_version,
            )
            _client_kind = "azure"
            logger.info(
                "Glossary generator: using Azure OpenAI at %s "
                "(deployment=%s, auth=managed-identity)",
                settings.azure_openai_endpoint,
                settings.azure_openai_deployment,
            )
            return _client, settings.azure_openai_deployment
        except Exception as exc:
            logger.warning(
                "Failed to initialise Azure OpenAI client: %s. "
                "Will try OpenAI API key fallback.",
                exc,
            )

    # --- OpenAI API key fallback ---
    if settings.openai_api_key:
        try:
            from openai import OpenAI

            _client = OpenAI(api_key=settings.openai_api_key)
            _client_kind = "openai"
            logger.info(
                "Glossary generator: using OpenAI API (model=%s)",
                settings.openai_model,
            )
            return _client, settings.openai_model
        except Exception as exc:
            logger.warning("Failed to initialise OpenAI client: %s", exc)

    logger.warning(
        "No OpenAI configuration found. "
        "Set AZURE_OPENAI_ENDPOINT (Azure) or OPENAI_API_KEY (local dev) "
        "to enable glossary-enhanced translation."
    )
    return None, None


def is_available() -> bool:
    """Return True if an LLM backend is configured and reachable."""
    client, _ = _get_client()
    return client is not None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert multilingual translator and terminologist. Your task is to
analyse a source document and its machine translation, then produce a glossary
of terms that would improve the translation if applied consistently.

Output ONLY a TSV (tab-separated values) list with exactly two columns and
NO header row:
  source_term<TAB>target_term

Rules:
- Include domain-specific terms, proper nouns, abbreviations, product names,
  and any term where you see an inconsistency or mistranslation.
- Do NOT include common everyday words that any machine translator handles well.
- Preserve the original casing of source terms.
- Each glossary entry must be an exact match for how the term appears in the
  source text (the Azure Translator glossary is case-sensitive).
- Maximum 200 entries. Prioritise the most impactful terms.
- Do NOT wrap the output in markdown code fences or add any commentary."""


def _build_user_prompt(
    source_text: str,
    translated_text: str,
    source_lang: str,
    target_lang: str,
) -> str:
    src = source_text[:_MAX_TEXT_CHARS]
    tgt = translated_text[:_MAX_TEXT_CHARS]
    return (
        f"Source language: {source_lang}\n"
        f"Target language: {target_lang}\n\n"
        f"--- SOURCE TEXT ---\n{src}\n\n"
        f"--- MACHINE TRANSLATION ---\n{tgt}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_glossary(
    source_text: str,
    translated_text: str,
    source_lang: str,
    target_lang: str,
) -> bytes:
    """Generate a TSV glossary using an LLM.

    Returns the glossary as UTF-8 encoded bytes suitable for passing to
    the Azure Translator API.  Returns empty bytes (``b""``) if the LLM
    is unavailable or the generation fails.
    """
    client, model = _get_client()
    if client is None:
        logger.warning("Glossary generation skipped – no LLM configured.")
        return b""

    user_prompt = _build_user_prompt(
        source_text, translated_text, source_lang, target_lang
    )

    try:
        logger.info(
            "Generating glossary via %s (model=%s, src=%d chars, tgt=%d chars)",
            _client_kind, model, len(source_text), len(translated_text),
        )
        response = client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content or ""

        # Strip any accidental markdown fences
        content = content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            # Remove first and last lines (``` markers)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            elif lines[0].startswith("```"):
                lines = lines[1:]
            content = "\n".join(lines)

        # Validate: each non-empty line should have exactly one tab
        valid_lines = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    valid_lines.append(f"{parts[0]}\t{parts[1]}")

        glossary_text = "\n".join(valid_lines)
        glossary_bytes = glossary_text.encode("utf-8")

        logger.info(
            "Glossary generated: %d term pairs (%d bytes)",
            len(valid_lines), len(glossary_bytes),
        )
        return glossary_bytes

    except Exception as exc:
        logger.error("Glossary generation failed: %s", exc)
        return b""
