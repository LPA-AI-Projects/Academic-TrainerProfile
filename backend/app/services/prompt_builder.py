import json


PROFILE_OUTPUT_SCHEMA = {
    "professional_titles": ["string"],
    "csat_score": "number between 4.5 and 4.9 (1 decimal)",
    "batches_delivered": "integer between 10 and 20",
    "profile": "string",
    "programs_trained": ["string"],
    "training_delivered": ["string"],
    "education": ["string"],
    "professional_experience": ["Title | Place of Work (Year - Year)"],
    "core_competencies": ["string"],
    "certificates": ["string"],
    "awards_and_recognitions": ["string"],
    "board_experience": ["string"],
    "key_skills": ["string (min 10 items)"],
}


def build_prompt(cv_text: str, outlines: list[str]) -> str:
    has_outline = bool(outlines)

    base_rules = [
        "You are a professional Trainer Profile Writer for Learners Point Academy, Dubai.",
        "Source-of-truth policy: use ONLY evidence present in the CV and provided outline text.",
        "Never hallucinate or fabricate employers, dates, certifications, tools, awards, or achievements.",
        "If a detail is missing from source text, leave it out instead of inventing.",
        "Narrative style must be polished, client-facing, in third person, with about 20% human warmth.",
        "Make course-domain relevance the main focus of profile, experience ordering, and skills (without copying modules verbatim).",
        "Do not omit experience roles found in CV.",
        "Keep each professional_experience item format exactly: Title | Place of Work (Year - Year).",
        "Provide at least 12 concise key_skills entries (2-4 words where possible).",
        "Include training_delivered organizations/clients if identifiable from CV.",
        "Return strict JSON only (no markdown, no commentary, no extra keys).",
    ]

    if has_outline:
        mode_rules = [
            "The outline provides domain/course context and relevance signals; CV remains the factual source.",
            "Do not copy-paste course module lines into profile text.",
            "Do not mention the explicit course name directly inside the profile narrative.",
            "Use outline context to prioritize role ordering, strengths, and professional titles.",
            "Set programs_trained[0] to the primary inferred course/topic from the outline heading if clearly identifiable.",
        ]
        input_context = (
            "INPUT MODE: CV + Course Outline(s)\n\n"
            f"CV:\n{cv_text}\n\n"
            f"COURSE OUTLINES:\n{chr(10).join(outlines)}"
        )
    else:
        mode_rules = [
            "Input contains only CV. Build output strictly from CV evidence.",
            "When details are missing, do not hallucinate institutions, years, or certifications.",
        ]
        input_context = f"INPUT MODE: CV only\n\nCV:\n{cv_text}"

    instruction_block = "\n".join(f"- {rule}" for rule in base_rules + mode_rules)
    return (
        "You are an expert profile writer for corporate training organizations.\n\n"
        "TASK REQUIREMENTS:\n"
        f"{instruction_block}\n\n"
        "OUTPUT JSON SCHEMA:\n"
        f"{json.dumps(PROFILE_OUTPUT_SCHEMA, indent=2)}\n\n"
        f"{input_context}"
    )
