import json
from typing import Any

from anthropic import Anthropic
from openai import OpenAI

from ..config import Settings, get_settings
from ..utils.logger import get_logger
from .prompt_builder import PROFILE_OUTPUT_SCHEMA

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
        "professional_experience_sections": [
            {
                "title": "Corporate learning and workforce enablement strategy",
                "bullets": [
                    "Shapes enterprise capability plans that tie learning goals to measurable performance and adoption signals.",
                    "Runs cohort workshops that turn strategy into repeatable habits for leaders and frontline teams.",
                ],
            },
            {
                "title": "Instructional design and assessment innovation advisory",
                "bullets": [
                    "Builds concise learning paths with practice, feedback loops, and clear competency signals for adults.",
                    "Strengthens evaluation design, QA checkpoints, and portfolio-level improvements with stakeholder partners.",
                ],
            },
            {
                "title": "Stakeholder engagement and change enablement consulting",
                "bullets": [
                    "Aligns communication, governance, and role clarity so change initiatives land with less friction.",
                    "Links programs to real workflows, risk awareness, and leadership priorities for sustainable uptake.",
                ],
            },
        ],
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
        "industry_exposure": [
            "Corporate and professional services environments",
            "Government and public sector program delivery",
            "Financial services and banking transformation contexts",
            "Education and workforce development ecosystems",
            "Healthcare and regulated operations contexts",
        ],
        "solutions_delivered": [
            "Leadership and team effectiveness workshop programs",
            "Business communication and client excellence pathways",
            "Assessment design and learning measurement frameworks",
            "Stakeholder engagement and change enablement playbooks",
            "Digital fluency and productivity enablement programs",
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


# Refine API only updates brochure page-2 left column lists (not training_delivered or narrative).
REFINE_MERGE_KEYS = ("industry_exposure", "solutions_delivered")

REFINE_OUTPUT_SCHEMA = {
    "industry_exposure": [
        "string (exactly 5 items; Title Case or sentence case; never ALL CAPS; max 72 chars each)"
    ],
    "solutions_delivered": [
        "string (exactly 5 items; Title Case or sentence case; never ALL CAPS; max 72 chars each)"
    ],
}


def _merge_refined_profile_dict(existing: dict[str, Any], refined: dict[str, Any]) -> dict[str, Any]:
    out = dict(existing)
    for k in REFINE_MERGE_KEYS:
        if k in refined and refined[k] is not None:
            out[k] = refined[k]
    return out


def refine_generated_profile_json(
    *,
    existing_profile: dict[str, Any],
    refine_instruction: str,
    trainer_label: str,
    provider: str | None = None,
    model_name: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    """
    Apply refinement to ``industry_exposure`` and ``solutions_delivered`` only.

    Returns ``(merged_profile, resolved_provider, raw_model_output)``.
    """
    settings = get_settings()
    resolved_provider = provider or settings.default_provider
    if model_name:
        resolved_model = model_name
    elif resolved_provider == "anthropic":
        resolved_model = settings.anthropic_model or settings.default_model
    else:
        resolved_model = settings.openai_model

    schema_hint = json.dumps(REFINE_OUTPUT_SCHEMA, indent=2, ensure_ascii=False)
    context = {
        "industry_exposure": existing_profile.get("industry_exposure") or [],
        "solutions_delivered": existing_profile.get("solutions_delivered") or [],
        "professional_titles": existing_profile.get("professional_titles") or [],
        "programs_trained": (existing_profile.get("programs_trained") or [])[:8],
    }
    current_json = json.dumps(context, indent=2, ensure_ascii=False)
    prompt = (
        "You are editing INDUSTRY EXPOSURE and SOLUTIONS DELIVERED lists on a trainer brochure.\n\n"
        "CURRENT LISTS (context only — do not return other profile fields):\n"
        f"{current_json}\n\n"
        "REFINE INSTRUCTION:\n"
        f"{refine_instruction}\n\n"
        f"Trainer label (context only): {trainer_label}\n\n"
        "RULES:\n"
        "- Return ONE JSON object only (no markdown).\n"
        "- Output ONLY keys industry_exposure and solutions_delivered.\n"
        "- Each array must contain exactly 5 strings.\n"
        "- Learner/brochure tone: sectors and solution capabilities — never org/client names from CRM.\n"
        "- Never use ALL CAPS for list items; Title Case or sentence case.\n"
        "- Max 72 characters per string.\n"
        "- Do NOT change training_delivered, profile narrative, programs, or experience sections.\n"
        "- Ground changes in the refine instruction and existing list context; do not invent credentials.\n\n"
        "OUTPUT JSON SCHEMA:\n"
        f"{schema_hint}\n"
    )

    if resolved_provider == "openai":
        if not settings.openai_api_key:
            if settings.allow_mock_generation:
                logger.warning("REFINE_JSON_MOCK_OPENAI unchanged profile allow_mock_generation=1")
                return dict(existing_profile), resolved_provider, "mock-openai-refine"
            raise ValueError("OPENAI_API_KEY is missing.")
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=resolved_model,
            input=[
                {"role": "system", "content": "Return strict JSON only. Do not wrap in markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        output_text = response.output_text or ""
        refined_obj = _extract_json_object(output_text)
        merged = _merge_refined_profile_dict(existing_profile, refined_obj)
        logger.info(
            "REFINE_JSON_DONE provider=%s model=%s merged_keys=%s",
            resolved_provider,
            resolved_model,
            len(merged),
        )
        return merged, resolved_provider, output_text

    if resolved_provider == "anthropic":
        if not settings.anthropic_api_key:
            if settings.allow_mock_generation:
                logger.warning("REFINE_JSON_MOCK_ANTHROPIC unchanged profile allow_mock_generation=1")
                return dict(existing_profile), resolved_provider, "mock-anthropic-refine"
            raise ValueError("ANTHROPIC_API_KEY is missing.")
        client = Anthropic(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url or "https://api.anthropic.com",
        )
        response = client.messages.create(
            model=resolved_model,
            temperature=0.2,
            max_tokens=8192,
            system="Return strict JSON only. Do not use markdown code fences.",
            messages=[{"role": "user", "content": prompt}],
        )
        text_blocks = [b.text for b in response.content if hasattr(b, "text")]
        output_text = "\n".join(text_blocks)
        refined_obj = _extract_json_object(output_text)
        merged = _merge_refined_profile_dict(existing_profile, refined_obj)
        logger.info(
            "REFINE_JSON_DONE provider=%s model=%s merged_keys=%s",
            resolved_provider,
            resolved_model,
            len(merged),
        )
        return merged, resolved_provider, output_text

    raise ValueError("Unsupported provider. Use 'openai' or 'anthropic'.")


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
    refine: str,
    provider: str | None = None,
    model_name: str | None = None,
) -> tuple[str, str]:
    """
    Rewrite only the narrative profile text based on ``refine`` instructions.
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

    def _detect_refine_targets(text: str) -> list[str]:
        t = (text or "").lower()
        targets: list[str] = []
        if any(k in t for k in ("bio", "summary", "profile")):
            targets.extend(["profile_para1", "profile_para2"])
        if any(k in t for k in ("tone", "style", "wording", "grammar")) and not targets:
            targets.extend(["profile_para1", "profile_para2"])
        return targets or ["profile_para1", "profile_para2"]

    target_fields = _detect_refine_targets(refine)
    structured_current = {
        "trainer_label": profile_name,
        "profile_text": existing_profile_text,
        "target_fields": target_fields,
    }
    prompt = (
        "You are improving an existing trainer profile narrative.\n\n"
        "CURRENT PROFILE JSON:\n"
        f"{json.dumps(structured_current, indent=2, ensure_ascii=False)}\n\n"
        "REFINE INSTRUCTION:\n"
        f"{refine}\n\n"
        "STRICT RULES:\n"
        "- You MUST apply the refine instruction.\n"
        "- Only update targeted narrative content; keep all other content unchanged.\n"
        "- Do NOT invent new facts, employers, credentials, metrics, or domains.\n"
        "- Preserve paragraph count/order (if two paragraphs exist, keep two).\n"
        "- Keep length near original (within about 12%).\n"
        "- Return plain text only (no JSON, no markdown, no bullets).\n"
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
        stabilized = _stabilize_refined_profile_text(existing_profile_text, out)
        logger.info(
            "REFINE_DIFF provider=%s model=%s changed=%s old_len=%s new_len=%s old_head=%r new_head=%r",
            resolved_provider,
            resolved_model,
            stabilized != (existing_profile_text or "").strip(),
            len((existing_profile_text or "").strip()),
            len(stabilized),
            (existing_profile_text or "").strip()[:160],
            stabilized[:160],
        )
        return stabilized, resolved_provider
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
        stabilized = _stabilize_refined_profile_text(existing_profile_text, out)
        logger.info(
            "REFINE_DIFF provider=%s model=%s changed=%s old_len=%s new_len=%s old_head=%r new_head=%r",
            resolved_provider,
            resolved_model,
            stabilized != (existing_profile_text or "").strip(),
            len((existing_profile_text or "").strip()),
            len(stabilized),
            (existing_profile_text or "").strip()[:160],
            stabilized[:160],
        )
        return stabilized, resolved_provider

    raise ValueError("Unsupported provider. Use 'openai' or 'anthropic'.")
