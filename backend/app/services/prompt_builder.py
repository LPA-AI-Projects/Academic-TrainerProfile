import json


PROFILE_OUTPUT_SCHEMA = {
    "full_name": "string (max 18 characters, use 'This Trainer' if needed)",
    "professional_titles": ["string"],
    "csat_score": "number between 4.5 and 4.9 (1 decimal)",
    "batches_delivered": "integer between 10 and 20",
    "bio_para1": "string (70-85 words)",
    "bio_para2": "string (70-85 words)",
    "profile": "string (optional combined fallback)",
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


def build_prompt(
    cv_text: str,
    outlines: list[str],
    *,
    trainer_heading_name: str | None = None,
    programs_trained_hints: list[str] | None = None,
) -> str:
    has_outline = bool(outlines)
    heading = (trainer_heading_name or "").strip()
    hints = [str(x).replace("\n", " ").strip() for x in (programs_trained_hints or []) if str(x).strip()][:40]

    base_rules = [
        "You are a professional Trainer Profile Writer for Learners Point Academy, Dubai.",
        "Generate content for a premium fixed 3-page A4 brochure layout.",
        "Source-of-truth policy: use ONLY evidence present in the CV and provided outline text.",
        "Never hallucinate or fabricate employers, dates, certifications, tools, awards, or achievements.",
        "If a detail is missing from source text, leave it out instead of inventing.",
        "Narrative style must be polished, client-facing, in third person, with about 20% human warmth.",
        "In profile narrative sections, prefer the phrasing 'The Trainer' / 'This Trainer' instead of personal names.",
        *(
            [
                f"When a trainer heading label is provided, set JSON 'full_name' to exactly this value (trimmed, max 18 characters): {heading[:18]!r}.",
                "Keep the profile body in third person using 'The Trainer' / 'This Trainer' phrasing; the heading label is for the 'full_name' field only.",
            ]
            if heading
            else []
        ),
        "Make course-domain relevance the main focus of profile, experience ordering, and skills (without copying modules verbatim).",
        "Extract every professional role found in CV; do not omit, merge, or summarize away any role.",
        "For professional_experience, keep one role per item and preserve title + organization clearly.",
        "Do not include date ranges/month-year text in professional_experience items; omit year/date suffixes entirely.",
        "STRICT LENGTH RULES: full_name max 18 chars.",
        "Bio must be provided as bio_para1 and bio_para2, each 50-55 words.",
        "programs_trained: output between 18 and 24 points.",
        *(
            [
                "When CLIENT-SUPPLIED PROGRAMS are provided below: list every distinct client program first (same order, each ≤72 characters), then add CV- and outline-backed programs that are not duplicates or near-duplicates of any client line or each other.",
                "If a CV program matches a client-supplied line (same or trivial rewording), keep a single entry — prefer the client-supplied wording.",
            ]
            if hints
            else [
                "programs_trained: include all CV/outline-backed programs first.",
            ]
        ),
        "If programs_trained has fewer than 18 explicit points, add inferred points from CV evidence and trainer domain (not generic fillers) until minimum 18 is reached.",
        "Do not exceed 24 points in programs_trained. Each list item must be at most 72 characters (short course-style titles only).",
        "training_delivered: output exactly 12 to 14 points. Each item must be at most 58 characters.",
        "training_delivered must be client/organization names or very short phrases only (no long sentences or narrative). Prefer 'Company – Region' style.",
        "key_skills (used for STRENGTHS): exactly 10 or 11 points, never exceed 11. Each item at most 50 characters; one short phrase per line.",
        "Prefer clean competency tags (short skill phrases) instead of long program-style statements.",
        "Keep each key_skills point concise and CV/domain aligned; must fit one line in the fixed brochure (no wrapping paragraphs).",
        "Do not repeat the same or near-duplicate wording across programs_trained, training_delivered, or key_skills.",
        "professional_experience: include every CV role as its own item. Each item at most 96 characters (short title + organization; no dates).",
        "awards_and_recognitions: max 6 items, each max 70 characters.",
        "Avoid repetition and generic filler. Prefer concise premium corporate wording.",
        "Include training_delivered organizations/clients if identifiable from CV.",
        "Do not map education entries into awards_and_recognitions.",
        "Return strict JSON only (no markdown, no commentary, no extra keys).",
    ]

    if has_outline:
        mode_rules = [
            "The outline provides domain/course context and relevance signals; CV remains the factual source.",
            "Do not copy-paste course module lines into profile text.",
            "Do not mention the explicit course name directly inside the profile narrative.",
            "Use outline context to prioritize role ordering, strengths, and professional titles.",
            *(
                []
                if hints
                else [
                    "Set programs_trained[0] to the primary inferred course/topic from the outline heading if clearly identifiable.",
                ]
            ),
            *(
                [
                    "After client-supplied programs_trained seeds, place the primary outline-inferred course/topic next if clearly identifiable and not already listed.",
                ]
                if hints
                else []
            ),
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

    if hints:
        hint_block = "\n".join(f"- {h}" for h in hints)
        input_context += (
            "\n\nCLIENT-SUPPLIED PROGRAMS (highest priority for programs_trained; merge with CV/outline, no duplicates):\n"
            f"{hint_block}"
        )

    instruction_block = "\n".join(f"- {rule}" for rule in base_rules + mode_rules)
    return (
        "You are an expert profile writer for corporate training organizations.\n\n"
        "TASK REQUIREMENTS:\n"
        f"{instruction_block}\n\n"
        "OUTPUT JSON SCHEMA:\n"
        f"{json.dumps(PROFILE_OUTPUT_SCHEMA, indent=2)}\n\n"
        f"{input_context}"
    )
