import json


PROFILE_OUTPUT_SCHEMA = {
    "professional_titles": ["string"],
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
        "Create an accurate trainer profile from the provided CV data.",
        "Use a polished, human tone in third person.",
        "Humanize profile and professional experience content by about 20%.",
        "Do not omit professional experience entries found in the CV.",
        "Keep professional experience title format exactly: Title | Place of Work (Year - Year).",
        "Provide short key skills with at least 10 points.",
        "Include training_delivered clients/organizations if identifiable from CV.",
        "Return JSON only, no markdown and no explanations.",
    ]

    if has_outline:
        mode_rules = [
            "Course outlines are additional context, not a source to copy from.",
            "Do not include course modules anywhere in output.",
            "Do not directly paste course outline titles in profile narrative.",
            "Subtly reflect course-related relevance in profile, professional experience, core competencies, and key skills.",
            "You may reuse course title terms in professional titles, programs trained, and subtle highlights only.",
        ]
        input_context = (
            "INPUT MODE: CV + Course Outline(s)\n\n"
            f"CV:\n{cv_text}\n\n"
            f"COURSE OUTLINES:\n{chr(10).join(outlines)}"
        )
    else:
        mode_rules = [
            "Input contains only CV. Build output strictly from CV evidence.",
            "When details are missing, do not hallucinate institutions or years.",
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
