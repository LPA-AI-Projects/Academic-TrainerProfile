import json
from typing import Any

from anthropic import Anthropic
from openai import OpenAI

from ..config import Settings, get_settings
from ..utils.logger import get_logger

logger = get_logger(__name__)


def _mock_response() -> dict[str, Any]:
    return {
        "professional_titles": [
            "Corporate Trainer",
            "Learning and Development Consultant",
            "Industry-Focused Skills Facilitator",
        ],
        "profile": "Experienced trainer with a practical, outcomes-driven approach across corporate and academic audiences.",
        "programs_trained": [
            "Leadership and Team Effectiveness",
            "Business Communication",
            "Client Service Excellence",
        ],
        "training_delivered": [
            "Government entities",
            "Banking teams",
            "Corporate L&D cohorts",
        ],
        "education": [],
        "professional_experience": [],
        "core_competencies": [
            "Instructional design",
            "Facilitation",
            "Assessment and feedback",
            "Stakeholder management",
        ],
        "certificates": [],
        "awards_and_recognitions": [],
        "board_experience": [],
        "key_skills": [
            "Training delivery",
            "Workshop facilitation",
            "Presentation skills",
            "Coaching",
            "Curriculum design",
            "Communication",
            "Leadership",
            "Team building",
            "Needs analysis",
            "Learning impact measurement",
        ],
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model output does not contain valid JSON object.")
    return json.loads(text[start : end + 1])


def _generate_openai(prompt: str, settings: Settings, model_name: str) -> tuple[dict[str, Any], str]:
    if not settings.openai_api_key:
        if settings.allow_mock_generation:
            logger.warning(
                "LLM_MOCK_OPENAI key_missing=1 allow_mock_generation=1 — set OPENAI_API_KEY for real profiles."
            )
            return _mock_response(), "mock-openai-response"
        raise ValueError("OPENAI_API_KEY is missing.")

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=model_name,
        input=[
            {
                "role": "system",
                "content": "Return strict JSON only. Do not wrap in markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    output_text = response.output_text
    return _extract_json_object(output_text), output_text


def _generate_anthropic(prompt: str, settings: Settings, model_name: str) -> tuple[dict[str, Any], str]:
    if not settings.anthropic_api_key:
        if settings.allow_mock_generation:
            logger.warning(
                "LLM_MOCK_ANTHROPIC key_missing=1 allow_mock_generation=1 — set ANTHROPIC_API_KEY for real profiles."
            )
            return _mock_response(), "mock-anthropic-response"
        raise ValueError("ANTHROPIC_API_KEY is missing.")

    client = Anthropic(
        api_key=settings.anthropic_api_key,
        base_url=settings.anthropic_base_url or "https://api.anthropic.com",
    )
    response = client.messages.create(
        model=model_name,
        temperature=0.2,
        max_tokens=4000,
        system="Return strict JSON only. Do not use markdown code fences.",
        messages=[{"role": "user", "content": prompt}],
    )
    text_blocks = [b.text for b in response.content if hasattr(b, "text")]
    output_text = "\n".join(text_blocks)
    return _extract_json_object(output_text), output_text


def generate_profile_json(
    prompt: str, provider: str | None = None, model_name: str | None = None
) -> tuple[dict[str, Any], str, str]:
    settings = get_settings()
    resolved_provider = provider or settings.default_provider
    if model_name:
        resolved_model = model_name
    elif resolved_provider == "anthropic":
        resolved_model = settings.anthropic_model or settings.default_model
    else:
        resolved_model = settings.openai_model

    if resolved_provider == "openai":
        payload, raw = _generate_openai(prompt, settings, resolved_model)
    elif resolved_provider == "anthropic":
        payload, raw = _generate_anthropic(prompt, settings, resolved_model)
    else:
        raise ValueError("Unsupported provider. Use 'openai' or 'anthropic'.")

    return payload, resolved_provider, raw


def _stabilize_refined_profile_text(original: str, refined: str) -> str:
    """
    Keep refine outputs close to the prior version: no runaway length, same paragraph count when the
    source had two blocks (split on blank lines).
    """
    o = (original or "").strip()
    r = (refined or "").strip()
    if not r:
        return o
    if not o:
        return r
    o_parts = [p.strip() for p in o.split("\n\n") if p.strip()]
    if len(o_parts) >= 2:
        r_parts = [p.strip() for p in r.split("\n\n") if p.strip()]
        if len(r_parts) > 2:
            r = "\n\n".join(r_parts[:2])
    max_len = min(12000, max(int(len(o) * 1.12), len(o) + 500))
    if len(r) > max_len:
        cut = r[: max_len + 1]
        if "\n\n" in cut:
            r = cut.rsplit("\n\n", 1)[0].strip()
        else:
            r = cut.rstrip()
    return r


def refine_profile_text(
    *,
    existing_profile_text: str,
    profile_name: str,
    feedback: str,
    provider: str | None = None,
    model_name: str | None = None,
) -> tuple[str, str]:
    """
    Rewrite only the narrative profile text based on reviewer feedback.
    Returns (refined_profile_text, resolved_provider).
    """
    settings = get_settings()
    resolved_provider = provider or settings.default_provider
    if model_name:
        resolved_model = model_name
    elif resolved_provider == "anthropic":
        resolved_model = settings.anthropic_model or settings.default_model
    else:
        resolved_model = settings.openai_model

    prompt = (
        "You are making a minimal, surgical revision (like a v1 → v1.1 text edit) to an existing trainer profile.\n"
        "Apply ONLY what the feedback explicitly asks for. Preserve all other wording, facts, and structure unless "
        "the feedback requires a change.\n"
        "Do not add new employers, credentials, metrics, or topics not implied by the feedback and the current text.\n"
        "Keep overall length within ~12% of the current text (do not expand into a long essay).\n"
        "If the current text uses two paragraphs separated by a blank line, keep exactly two paragraphs in the same order.\n"
        "Do not include headings, JSON, bullets, markdown, or meta commentary.\n"
        "Return plain text only.\n\n"
        f"Trainer label: {profile_name}\n\n"
        "Current profile text:\n"
        f"{existing_profile_text}\n\n"
        "Feedback (apply only this):\n"
        f"{feedback}\n"
    )

    if resolved_provider == "openai":
        if not settings.openai_api_key:
            if settings.allow_mock_generation:
                return existing_profile_text, resolved_provider
            raise ValueError("OPENAI_API_KEY is missing.")
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=resolved_model,
            input=[
                {
                    "role": "system",
                    "content": "Return plain text only. Minimal edits; preserve structure and length unless asked.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        out = (response.output_text or "").strip()
        return _stabilize_refined_profile_text(existing_profile_text, out), resolved_provider
    if resolved_provider == "anthropic":
        if not settings.anthropic_api_key:
            if settings.allow_mock_generation:
                return existing_profile_text, resolved_provider
            raise ValueError("ANTHROPIC_API_KEY is missing.")
        client = Anthropic(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url or "https://api.anthropic.com",
        )
        response = client.messages.create(
            model=resolved_model,
            temperature=0.1,
            max_tokens=900,
            system=(
                "Return plain text only. Make the smallest edit that satisfies the feedback; "
                "keep the same paragraph breaks (two blocks if the input has two) and do not bloat length."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        text_blocks = [b.text for b in response.content if hasattr(b, "text")]
        out = "\n".join(text_blocks).strip()
        return _stabilize_refined_profile_text(existing_profile_text, out), resolved_provider

    raise ValueError("Unsupported provider. Use 'openai' or 'anthropic'.")
