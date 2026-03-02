"""Text translation service supporting three modes.

Modes:
  - **azure**   – Azure Translator Text API (fast, cost-effective)
  - **llm**     – LLM-only translation (Azure OpenAI / OpenAI)
  - **premium** – Two-pass: Azure first, then LLM refinement (highest quality)
"""

import logging
from typing import Literal

import httpx

from ..config import settings
from .glossary_generator import _get_client as _get_llm_client
from .translator import SUPPORTED_LANGUAGES, _get_headers

logger = logging.getLogger(__name__)

_API_VERSION_TEXT = "3.0"

TranslationMode = Literal["azure", "llm", "premium"]


# ---------------------------------------------------------------------------
# Azure Translator Text API
# ---------------------------------------------------------------------------

async def _translate_azure(
    text: str,
    source_lang: str,
    target_lang: str,
) -> str:
    """Translate text using the Azure Translator Text API."""
    # Always use the custom-domain endpoint – it accepts both key auth
    # and bearer-token (managed identity), so local and Azure behave the same.
    base = settings.azure_translator_endpoint.rstrip("/")
    url = f"{base}/translator/text/v3.0/translate"

    headers = _get_headers()
    headers["Content-Type"] = "application/json"

    params: dict[str, str] = {
        "to": target_lang,
    }
    if source_lang and source_lang != "auto":
        params["from"] = source_lang

    body = [{"Text": text}]

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, params=params, json=body)

    if response.status_code != 200:
        detail = response.text[:500] if response.text else f"HTTP {response.status_code}"
        raise RuntimeError(f"Azure Text Translation API error {response.status_code}: {detail}")

    data = response.json()
    if not data or not data[0].get("translations"):
        raise RuntimeError("Azure Translator returned empty result")

    result = data[0]["translations"][0]["text"]
    detected = data[0].get("detectedLanguage", {}).get("language")

    logger.info(
        "Azure text translation OK (%d chars → %d chars, %s → %s)",
        len(text), len(result),
        detected or source_lang, target_lang,
    )
    return result


# ---------------------------------------------------------------------------
# LLM-only translation
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = """\
You are an expert multilingual translator. Translate the user's text from \
{source_lang} to {target_lang}. Output ONLY the translated text — no \
commentary, no explanations, no markdown formatting. Preserve the original \
formatting, paragraph breaks, and structure."""


def _translate_llm_sync(
    text: str,
    source_lang: str,
    target_lang: str,
) -> str:
    """Translate text using an LLM (synchronous OpenAI call)."""
    client, model = _get_llm_client()
    if client is None:
        raise RuntimeError(
            "No LLM configured. Set AZURE_OPENAI_ENDPOINT or OPENAI_API_KEY "
            "to use LLM translation."
        )

    src_name = SUPPORTED_LANGUAGES.get(source_lang, source_lang)
    tgt_name = SUPPORTED_LANGUAGES.get(target_lang, target_lang)
    if source_lang == "auto":
        src_name = "the auto-detected source language"

    system = _LLM_SYSTEM_PROMPT.format(source_lang=src_name, target_lang=tgt_name)

    logger.info("LLM translation: %s → %s (%d chars, model=%s)", src_name, tgt_name, len(text), model)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    )
    result = response.choices[0].message.content or ""
    logger.info("LLM translation OK (%d chars → %d chars)", len(text), len(result))
    return result.strip()


# ---------------------------------------------------------------------------
# Premium: Azure + LLM refinement
# ---------------------------------------------------------------------------

_REFINE_SYSTEM_PROMPT = """\
You are an expert multilingual translator and editor. You are given a source \
text in {source_lang} and its machine translation into {target_lang}.

Your task: refine the machine translation to be more natural, accurate, and \
fluent while preserving the original meaning. Fix any mistranslations, \
awkward phrasing, or terminology inconsistencies.

Output ONLY the refined translation — no commentary, no explanations, \
no markdown formatting. Preserve the original formatting and paragraph breaks."""


def _refine_with_llm(
    source_text: str,
    machine_translation: str,
    source_lang: str,
    target_lang: str,
) -> str:
    """Use an LLM to refine a machine translation."""
    client, model = _get_llm_client()
    if client is None:
        logger.warning("No LLM available for refinement, returning machine translation as-is.")
        return machine_translation

    src_name = SUPPORTED_LANGUAGES.get(source_lang, source_lang)
    tgt_name = SUPPORTED_LANGUAGES.get(target_lang, target_lang)
    if source_lang == "auto":
        src_name = "the auto-detected source language"

    system = _REFINE_SYSTEM_PROMPT.format(source_lang=src_name, target_lang=tgt_name)
    user_msg = (
        f"--- SOURCE TEXT ---\n{source_text}\n\n"
        f"--- MACHINE TRANSLATION ---\n{machine_translation}"
    )

    logger.info("LLM refinement: %s → %s (%d + %d chars, model=%s)",
                src_name, tgt_name, len(source_text), len(machine_translation), model)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    )
    result = response.choices[0].message.content or ""
    logger.info("LLM refinement OK (%d chars)", len(result))
    return result.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def translate_text(
    text: str,
    source_lang: str,
    target_lang: str,
    mode: TranslationMode = "azure",
) -> dict:
    """Translate text using the specified mode.

    Returns a dict with:
      - ``translated_text``: the translated string
      - ``mode``: the mode used
    """
    if mode == "azure":
        result = await _translate_azure(text, source_lang, target_lang)
        return {"translated_text": result, "mode": "azure"}

    elif mode == "llm":
        import asyncio
        result = await asyncio.to_thread(_translate_llm_sync, text, source_lang, target_lang)
        return {"translated_text": result, "mode": "llm"}

    elif mode == "premium":
        # Pass 1: Azure machine translation
        azure_result = await _translate_azure(text, source_lang, target_lang)
        # Pass 2: LLM refinement
        import asyncio
        refined = await asyncio.to_thread(
            _refine_with_llm, text, azure_result, source_lang, target_lang,
        )
        return {"translated_text": refined, "mode": "premium"}

    else:
        raise ValueError(f"Unknown translation mode: {mode}")
