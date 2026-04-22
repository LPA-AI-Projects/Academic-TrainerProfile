import json
from typing import Any

from anthropic import Anthropic
from openai import OpenAI

from ..config import Settings, get_settings


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
        resolved_model = settings.default_model

    if resolved_provider == "openai":
        payload, raw = _generate_openai(prompt, settings, resolved_model)
    elif resolved_provider == "anthropic":
        payload, raw = _generate_anthropic(prompt, settings, resolved_model)
    else:
        raise ValueError("Unsupported provider. Use 'openai' or 'anthropic'.")

    return payload, resolved_provider, raw
