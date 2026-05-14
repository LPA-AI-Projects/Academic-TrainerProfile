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
    "professional_experience_sections": [
        {
            "title": "string (strategic advisory-style header; Title Case; max ~100 chars; never ALL CAPS)",
            "bullets": [
                "string (bullet 1: complete sentence ending in a period; target 120–165 characters so it wraps to at most two lines in the brochure column; never exceed 180 characters)",
                "string (bullet 2: same length discipline as bullet 1)",
            ],
        }
    ],
    "professional_experience": [],
    "core_competencies": ["string"],
    "certificates": ["string"],
    "awards_and_recognitions": ["string"],
    "board_experience": ["string"],
    "key_skills": ["string (min 10 items)"],
    "industry_exposure": ["string (exactly 4 items; Title Case or sentence case; never ALL CAPS; max 72 chars; see task rules)"],
    "solutions_delivered": ["string (exactly 4 items; Title Case or sentence case; never ALL CAPS; max 72 chars; see task rules)"],
}


def build_prompt(
    cv_text: str,
    outlines: list[str],
    *,
    trainer_heading_name: str | None = None,
    programs_trained_hints: list[str] | None = None,
    training_delivered_hints: list[str] | None = None,
) -> str:
    has_outline = bool(outlines)
    heading = (trainer_heading_name or "").strip()
    hints = [str(x).replace("\n", " ").strip() for x in (programs_trained_hints or []) if str(x).strip()][:40]
    td_hints = [str(x).replace("\n", " ").strip() for x in (training_delivered_hints or []) if str(x).strip()][
        :30
    ]

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
        "SECTION — JSON key professional_experience_sections (brochure section 'Professional experience'): premium executive/advisory format, not employment-style CV roles.",
        "professional_experience_sections: output exactly 3 objects. Each object has title (string) and bullets (array of exactly 2 strings). Do not mention company names, organization names, locations, years, clients, or reporting structures in titles or bullets.",
        "professional_experience_sections — casing: never use ALL CAPS or shouting headline casing. Use Title Case for each title string. Use sentence case or Title Case for each bullet string.",
        "professional_experience_sections titles: strategic, consulting/advisory oriented, transformation-focused; aligned with trainer specialization and course outline when outline text exists; otherwise grounded only in CV + outline evidence. Avoid generic titles like Trainer, Instructor, Manager, or Employee.",
        "professional_experience_sections bullets: each bullet must be a single complete sentence (or two very short sentences) ending with proper punctuation. Write so each bullet naturally wraps to at most two lines in a fixed ~488px-wide column at ~11.5px body text (target roughly 120–165 characters per bullet; hard maximum 180 characters). Do not write long multi-clause paragraphs, stacked lists, or semicolon-heavy chains that require a third line.",
        "professional_experience_sections bullets — display policy: the brochure renders full bullet text with round list markers (no ellipsis truncation). If a bullet is too long it will overflow the layout; therefore obey the character targets strictly and revise wording until each bullet fits two lines without relying on truncation.",
        "professional_experience_sections: each title at most 100 characters; each bullet at most 180 characters. Set professional_experience to an empty array [].",
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
        "training_delivered: output exactly 12 to 14 points. Keep each item concise and brochure-friendly, but do not truncate text with ellipses.",
        *(
            [
                "When CLIENT-SUPPLIED TRAINING DELIVERED lines are provided below: list every distinct Zoho line first (same order), normalized to short 'Org – Region' style when possible without inventing regions.",
                "Then append additional training_delivered items grounded in the CV (clients/orgs) that are not duplicates or near-duplicates of any Zoho line.",
                "If a CV line matches a Zoho line, keep a single entry — prefer the Zoho wording.",
            ]
            if td_hints
            else []
        ),
        "training_delivered must be client/organization names or very short phrases only (no long sentences or narrative). Prefer 'Company – Region' style.",
        "SECTION — JSON key industry_exposure (brochure section 'Industry exposure'): output exactly 4 strings. Do not mention company names, client names, or locations in these bullets.",
        "industry_exposure — casing: never use ALL CAPS. Use Title Case (capitalize major words) or sentence case for each string.",
        "industry_exposure: focus only on industries, business sectors, enterprise environments, and operational domains. Wording must align naturally with the course outline (when provided) and the trainer's specialization as evidenced in CV + outline; if unsupported by that evidence, omit rather than invent.",
        "industry_exposure: enterprise-level, transformation-oriented phrasing; premium corporate and consulting-oriented tone; concise, strategic, proposal-friendly; GCC corporate proposal friendly; avoid academic phrasing and repetitive wording. Each item at most 72 characters.",
        "SECTION — JSON key solutions_delivered (brochure section 'Solutions delivered'): output exactly 4 strings. Do not mention company names, client names, or locations in these bullets.",
        "solutions_delivered — casing: never use ALL CAPS. Use Title Case or sentence case for each string.",
        "solutions_delivered: focus on business solutions, transformation initiatives, capability domains, tools/frameworks, and strategic training applications. Align directly with course outline topics, tools, methodologies, and learning outcomes when outline text is present; otherwise ground only in CV-stated delivery and capability evidence.",
        "solutions_delivered: modern, strategic, business-impact wording; executive-level positioning; avoid technical overload, academic phrasing, and repetition with industry_exposure or key_skills. Must NOT duplicate or paraphrase training_delivered org/client lines. Each item at most 72 characters.",
        "key_skills (used for STRENGTHS): exactly 10 or 11 points, never exceed 11. Each item at most 50 characters; one short phrase per line.",
        "Prefer clean competency tags (short skill phrases) instead of long program-style statements.",
        "Keep each key_skills point concise and CV/domain aligned; must fit one line in the fixed brochure (no wrapping paragraphs).",
        "Do not repeat the same or near-duplicate wording across programs_trained, training_delivered, industry_exposure, solutions_delivered, key_skills, or professional_experience_sections.",
        "awards_and_recognitions: max 6 items. Keep wording concise and avoid ellipsis-based truncation.",
        "Avoid repetition and generic filler. Prefer concise premium corporate wording.",
        *(
            ["Include training_delivered organizations/clients from CV only after Zoho-supplied lines are exhausted."]
            if td_hints
            else ["Include training_delivered organizations/clients if identifiable from CV."]
        ),
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

    if td_hints:
        td_block = "\n".join(f"- {h}" for h in td_hints)
        input_context += (
            "\n\nCLIENT-SUPPLIED TRAINING DELIVERED (from Zoho CRM; highest priority for training_delivered; "
            "then CV-backed orgs, no duplicates):\n"
            f"{td_block}"
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
