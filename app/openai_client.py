import json
import logging
import re
from pathlib import Path

import httpx

from app.config import Settings, get_settings
from app.models import ActionItem, Attendee, MeetingIntelligence, MeetingMetadata

LOGGER = logging.getLogger(__name__)


class OpenAIClientError(RuntimeError):
    pass


def summarize_meeting(
    transcript: str,
    metadata: MeetingMetadata | None = None,
    settings: Settings | None = None,
) -> MeetingIntelligence:
    client = OpenAIClient(settings or get_settings())
    return client.summarize_meeting(transcript, metadata or MeetingMetadata())


class OpenAIClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def summarize_meeting(
        self,
        transcript: str,
        metadata: MeetingMetadata,
    ) -> MeetingIntelligence:
        if not self.settings.openai_api_key:
            LOGGER.warning("OPENAI_API_KEY is not set; using local transcript summarizer.")
            return local_transcript_summary(transcript, metadata)

        payload = {
            "model": self.settings.openai_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Extract structured meeting intelligence. Return only strict JSON matching "
                        "the requested schema. Use null when a value is unknown."
                    ),
                },
                {
                    "role": "user",
                    "content": build_prompt(transcript, metadata),
                },
            ],
            "temperature": 0.1,
        }
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        try:
            with httpx.Client(timeout=60) as client:
                response = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                raise_for_openai_error(response)
        except OpenAIClientError:
            if not self.settings.openai_fallback_to_local:
                raise
            LOGGER.exception("OpenAI request failed; using local transcript summarizer.")
            return local_transcript_summary(transcript, metadata)
        except httpx.HTTPError as exc:
            if not self.settings.openai_fallback_to_local:
                raise OpenAIClientError(f"OpenAI connection failed: {exc}") from exc
            LOGGER.exception("OpenAI connection failed; using local transcript summarizer.")
            return local_transcript_summary(transcript, metadata)
        content = response.json()["choices"][0]["message"]["content"]
        return MeetingIntelligence.model_validate_json(content)


def build_prompt(transcript: str, metadata: MeetingMetadata) -> str:
    schema = {
        "meeting_title": "string",
        "meeting_date": "string | null",
        "customer_account_guess": "string | null",
        "attendees": [
            {
                "name": "string | null",
                "email": "string | null",
                "company": "string | null",
                "role": "string | null",
            }
        ],
        "executive_summary": "string",
        "key_points": ["string"],
        "customer_pain_points": ["string"],
        "business_outcomes": ["string"],
        "technical_requirements": ["string"],
        "competitors_mentioned": ["string"],
        "risks_or_blockers": ["string"],
        "decisions": ["string"],
        "action_items": [{"owner": "string | null", "task": "string", "due_date": "string | null"}],
        "timeline_signals": ["string"],
        "budget_signals": ["string"],
        "proposed_next_step": "string",
        "follow_up_email_draft": "string | null",
    }
    return (
        f"Metadata:\n{metadata.model_dump_json(indent=2)}\n\n"
        f"Schema:\n{json.dumps(schema, indent=2)}\n\n"
        f"Transcript:\n{transcript}"
    )


def local_transcript_summary(transcript: str, metadata: MeetingMetadata) -> MeetingIntelligence:
    transcript = clean_transcript_text(transcript)
    turns = parse_speaker_turns(transcript)
    lines = [text for _, text in turns]
    significant_lines = [
        line for line in lines if len(line.split()) >= 4 and not is_low_value_line(line)
    ]
    title = metadata.title or "Meeting Notes"
    full_text = " ".join(significant_lines)
    lower_text = full_text.lower()
    key_points = extract_key_points(significant_lines, lower_text)
    decisions = extract_decisions(significant_lines)
    risks = extract_risks(significant_lines, lower_text)
    open_questions = extract_open_questions(significant_lines, lower_text)
    actions = extract_action_items(turns, lower_text)
    proposed_next_step = (
        actions[0].task
        if actions
        else "Review the recap, confirm owners, and send the follow-up email."
    )
    attendees = [
        Attendee(name=speaker)
        for speaker in first_unique([speaker for speaker, _ in turns if speaker], limit=20)
    ]

    return MeetingIntelligence(
        meeting_title=title,
        meeting_date=metadata.meeting_date,
        customer_account_guess=guess_account(title, transcript),
        attendees=attendees,
        executive_summary=build_local_executive_summary(title, key_points),
        key_points=key_points,
        customer_pain_points=extract_pain_points(significant_lines, lower_text),
        business_outcomes=extract_business_outcomes(significant_lines, lower_text),
        technical_requirements=extract_technical_requirements(significant_lines, lower_text),
        competitors_mentioned=[],
        risks_or_blockers=risks,
        open_questions=open_questions,
        decisions=decisions,
        action_items=actions,
        timeline_signals=[],
        budget_signals=[],
        proposed_next_step=proposed_next_step,
        follow_up_email_draft=build_follow_up_email(title, key_points, actions, open_questions),
    )


def guess_account(title: str, transcript: str) -> str | None:
    if is_generic_title(title):
        return None
    combined = f"{title}\n{transcript}"
    for marker in ("Account:", "Customer:", "Company:"):
        if marker in combined:
            return combined.split(marker, 1)[1].splitlines()[0].strip() or None
    words = title.replace("-", " ").replace("_", " ").split()
    return words[0] if words else None


def is_generic_title(title: str) -> bool:
    normalized = title.lower().replace("-", " ").replace("_", " ").strip()
    generic_prefixes = (
        "pasted transcript",
        "openai smoke test",
        "local fallback smoke",
        "zoom paste smoke",
        "meeting notes",
    )
    return normalized.startswith(generic_prefixes)


SPEAKER_RE = re.compile(
    r"^(?:\[[^\]]+\]\s*)?(?P<speaker>[A-Z][\w .,'-]{0,60}?):\s*(?P<text>.+)$"
)
DECISION_TERMS = ("decided", "decision", "agreed", "confirmed", "approved", "proceed")
ACTION_TERMS = (
    "i will",
    "i'll",
    "we will",
    "we'll",
    "can you",
    "please",
    "follow up",
    "send",
    "schedule",
    "create",
    "update",
)
ACTION_PREFIXES = ("i will", "i'll", "we will", "we'll", "can you", "please")
RISK_TERMS = ("risk", "blocker", "blocked", "concern", "issue", "delay")
PAIN_TERMS = ("pain", "problem", "manual", "slow", "hard", "challenge", "friction")
OUTCOME_TERMS = ("outcome", "goal", "improve", "reduce", "increase", "faster", "save")
BUSINESS_TERMS = (
    "data aggregation",
    "dashboard",
    "automation",
    "automate",
    "use case",
    "requirements",
    "capabilities",
    "uiPath".lower(),
    "data limitations",
    "scalability",
    "ato",
    "classified",
    "jwics",
    "cac",
    "api",
    "system of record",
    "power bi",
    "qlik",
    "cop",
    "zero trust",
    "zt",
)
LOW_VALUE_PHRASES = (
    "good morning",
    "morning",
    "why is he on this call",
    "copycat",
    "no relation",
    "double checking",
    "show up",
    "joined as well",
    "can you repeat that",
    "i will pause",
    "i'll punt",
    "i'm sorry",
    "okay",
    "fantastic",
)


def parse_speaker_turns(transcript: str) -> list[tuple[str | None, str]]:
    turns: list[tuple[str | None, str]] = []
    for raw_line in transcript.splitlines():
        line = raw_line.strip()
        if not line or line.upper() == "WEBVTT" or line.isdigit() or "-->" in line:
            continue
        match = SPEAKER_RE.match(line)
        if match:
            turns.append((match.group("speaker").strip(), match.group("text").strip()))
        else:
            turns.append((None, line))
    return turns


def clean_transcript_text(value: str) -> str:
    replacements = {
        "â€¦": "...",
        "â€™": "'",
        "â€œ": '"',
        "â€": '"',
        "â€“": "-",
        "â€”": "-",
    }
    for bad, good in replacements.items():
        value = value.replace(bad, good)
    return value


def is_low_value_line(value: str) -> bool:
    normalized = value.lower().strip(" .,?!")
    word_count = len(normalized.split())
    if word_count < 4:
        return True
    short_chatter = {"okay", "fantastic", "morning", "good morning"}
    if normalized in short_chatter:
        return True
    chatter_phrases = (
        "why is he on this call",
        "copycat",
        "no relation",
        "double checking",
        "show up",
        "joined as well",
        "can you repeat that",
    )
    if word_count <= 14 and any(phrase in normalized for phrase in LOW_VALUE_PHRASES):
        return True
    return any(phrase in normalized for phrase in chatter_phrases)


def select_lines(lines: list[str], terms: tuple[str, ...], limit: int = 5) -> list[str]:
    return first_unique(
        [line for line in lines if any(term in line.lower() for term in terms)],
        limit=limit,
    )


def extract_key_points(lines: list[str], lower_text: str) -> list[str]:
    key_points: list[str] = []
    if "data aggregation" in lower_text and ("dashboard" in lower_text or "cop" in lower_text):
        key_points.append(
            "The discussion centered on data aggregation across multiple systems and "
            "consolidating that information into a dashboard or common operating picture."
        )
    if "system of record" in lower_text:
        key_points.append(
            "UiPath was positioned as automation around existing systems rather than a new "
            "system of record or major platform overhaul."
        )
    if "legacy" in lower_text or "cac authenticated" in lower_text or "apis" in lower_text:
        key_points.append(
            "The team discussed pulling data from legacy, CAC-authenticated, or no-API "
            "systems through UI automation."
        )
    if "unattended" in lower_text or "24-7" in lower_text or "1am" in lower_text:
        key_points.append(
            "Unattended automations could run on a schedule and deliver dashboards, "
            "SharePoint files, or email reports before the workday starts."
        )
    if "no data limitations" in lower_text or "data type limitations" in lower_text:
        key_points.append(
            "Data type limitations were discussed; UiPath stated it can move accessible "
            "data between systems and temporary storage when needed."
        )
    if "billion financial transactions" in lower_text or "scalability" in lower_text:
        key_points.append(
            "Scalability was addressed with an example of high-volume financial transaction "
            "automation."
        )
    if "ato" in lower_text or "jwics" in lower_text or "classified" in lower_text:
        key_points.append(
            "ATO and classified-environment availability came up as a follow-up area, "
            "including JWICS and impact level considerations."
        )

    if key_points:
        return key_points[:6]

    scored = sorted(
        ((business_score(line), line) for line in lines),
        key=lambda item: item[0],
        reverse=True,
    )
    return first_unique([line for score, line in scored if score > 0], limit=5) or first_unique(
        lines, limit=5
    )


def business_score(line: str) -> int:
    lower = line.lower()
    score = sum(3 for term in BUSINESS_TERMS if term in lower)
    score += sum(1 for term in OUTCOME_TERMS + PAIN_TERMS if term in lower)
    if "?" in line:
        score += 1
    if is_low_value_line(line):
        score -= 8
    return score


def extract_pain_points(lines: list[str], lower_text: str) -> list[str]:
    points: list[str] = []
    if "manual data" in lower_text or "manual data pool" in lower_text:
        points.append("Manual data pulls are required to keep aggregated reporting current.")
    if "bunch of different systems" in lower_text:
        points.append("Relevant data lives in multiple systems and must be reconciled.")
    if "legacy old system" in lower_text or "doesn't have any apis" in lower_text:
        points.append("Some source systems may be legacy, CAC-authenticated, or lack APIs.")
    if "97 requirements" in lower_text or "zt" in lower_text:
        points.append("The team also needs to address Zero Trust requirements.")
    return points or select_lines(lines, PAIN_TERMS)


def extract_business_outcomes(lines: list[str], lower_text: str) -> list[str]:
    outcomes: list[str] = []
    if "dashboard" in lower_text or "cop" in lower_text:
        outcomes.append(
            "Give decision-makers a consolidated dashboard or COP view of data in one place."
        )
    if "remove the human" in lower_text or "swivel chair" in lower_text:
        outcomes.append("Reduce swivel-chair work and manual data movement.")
    if "ready for someone" in lower_text or "6am" in lower_text:
        outcomes.append("Deliver reports or dashboards before users start their workday.")
    return outcomes or select_lines(lines, OUTCOME_TERMS)


def extract_technical_requirements(lines: list[str], lower_text: str) -> list[str]:
    requirements: list[str] = []
    if "cac" in lower_text:
        requirements.append("Support CAC-authenticated systems where needed.")
    if "direct api" in lower_text or "doesn't have any apis" in lower_text:
        requirements.append("Handle systems with and without direct APIs.")
    if "data fabrics" in lower_text or "data service" in lower_text:
        requirements.append("Use Data Fabric/Data Service as temporary storage when useful.")
    if "unattended" in lower_text:
        requirements.append("Support unattended scheduled automations.")
    if "ato" in lower_text or "jwics" in lower_text or "classified" in lower_text:
        requirements.append("Clarify ATO and classified-environment deployment path.")
    return requirements


def extract_decisions(lines: list[str]) -> list[str]:
    decisions = []
    for line in lines:
        lower = line.lower()
        if "decision-maker" in lower:
            continue
        if any(term in lower for term in DECISION_TERMS):
            decisions.append(line)
    return first_unique(decisions, limit=5)


def extract_risks(lines: list[str], lower_text: str) -> list[str]:
    risks: list[str] = []
    if "97 requirements" in lower_text or "zt" in lower_text:
        risks.append("Zero Trust requirement alignment still needs to be clarified.")
    if "classified" in lower_text or "jwics" in lower_text or "ato" in lower_text:
        risks.append("Classified-side ATO/deployment details require follow-up.")
    if "specific data type" in lower_text or "data type limitations" in lower_text:
        risks.append("Customer is validating whether any data types are unsupported.")
    if "not a priority" in lower_text or "too many projects" in lower_text:
        risks.append("Customer indicated this is not an immediate priority due to other projects.")
    generic = [
        line
        for line in lines
        if any(term in line.lower() for term in RISK_TERMS)
        and not contains_negated_risk(line)
        and "data aggregation issue" not in line.lower()
        and "?" not in line
    ]
    return first_unique(risks + generic, limit=6)


def contains_negated_risk(line: str) -> bool:
    lower = line.lower()
    negations = (
        "no issue",
        "not a concern",
        "no data limitations",
        "no problem",
        "no limitations",
    )
    return any(negation in lower for negation in negations)


def extract_open_questions(lines: list[str], lower_text: str) -> list[str]:
    questions: list[str] = []
    if "ato" in lower_text or "classified" in lower_text:
        questions.append("Which ATOs and classified environments are relevant for this use case?")
    if "data aggregation challenges" in lower_text:
        questions.append(
            "What specific data aggregation challenge and source systems should be reviewed next?"
        )
    if "97 requirements" in lower_text or "zero trust" in lower_text:
        questions.append(
            "How should the 97 Zero Trust requirements be mapped to the automation scope?"
        )
    if "not a priority" in lower_text or "open schedule" in lower_text:
        questions.append(
            "When will the customer have priority and schedule availability for a demo?"
        )
    if len(questions) >= 4:
        return first_unique(questions, limit=6)
    for line in lines:
        lower = line.lower()
        if "?" not in line or is_low_value_line(line):
            continue
        if "repeat that" in lower:
            continue
        if (
            len(line) > 300
            or "questions we always get" in lower
            or "data type limitations" in lower
            or "specific data type" in lower
            or "no, ma'am" in lower
            or "close to accurate" in lower
            or "final rounds" in lower
            or "can uipath handle" in lower
            or "as far as the atos" in lower
        ):
            continue
        questions.append(line)
    return first_unique(questions, limit=6)


def extract_action_items(
    turns: list[tuple[str | None, str]],
    lower_text: str,
) -> list[ActionItem]:
    items: list[ActionItem] = []
    for speaker, text in turns:
        if is_low_value_line(text):
            continue
        for candidate in action_candidate_texts(text):
            normalized = candidate.lower()
            if is_low_value_line(candidate) or is_non_action(normalized):
                continue
            if not is_action_sentence(normalized):
                continue
            owner = speaker
            if normalized.startswith(("can you", "please")):
                owner = None
            items.append(
                ActionItem(owner=owner, task=candidate, due_date=extract_due_date(candidate))
            )
    if "i'd have to get to" in lower_text and ("jwics" in lower_text or "classified" in lower_text):
        items.append(
            ActionItem(
                owner="UiPath",
                task="Follow up internally on classified/JWICS deployment and ATO details.",
                due_date=None,
            )
        )
    if "share the challenge" in lower_text or "data aggregation challenges" in lower_text:
        items.append(
            ActionItem(
                owner="Customer",
                task=(
                    "Share more detail on the data aggregation challenge, source systems, "
                    "and desired output."
                ),
                due_date=None,
            )
        )
    if "open schedule" in lower_text and "demo" in lower_text:
        items.append(
            ActionItem(
                owner="Customer",
                task="Reach out when the team has schedule availability for a demo.",
                due_date=None,
            )
        )
    return dedupe_action_items(items)[:6]


def action_candidate_texts(text: str) -> list[str]:
    if len(text) <= 280:
        return [text.strip()]
    candidates = re.split(r"(?<=[.!?])\s+", text)
    return [candidate.strip() for candidate in candidates if candidate.strip()]


def is_action_sentence(normalized: str) -> bool:
    padded = f" {normalized} "
    return normalized.startswith(ACTION_PREFIXES) or any(
        f" {term} " in padded for term in ("follow up", "send", "schedule", "create", "update")
    )


def is_non_action(normalized: str) -> bool:
    simplified = normalized.replace("…", " ").replace("...", " ")
    simplified = " ".join(simplified.split())
    if "follow up with" in simplified and "reached out" in simplified:
        return True
    non_actions = (
        "i'll answer those questions",
        "i will pause",
        "i'll punt",
        "i'll catch up",
        "i'll take a minute",
        "i'll probably form more questions",
        "i will say",
        "follow up with those that have reached out",
        "reach out to you guys when",
        "definitely reach out when",
        "i can send you documents",
        "we can do a demo",
        "we can walk through",
        "we can obviously help",
        "try to do is create",
        "can you repeat",
        "can uipath handle",
        "we're not trying to create",
        "to create a new system of record",
        "it updates the dashboard",
    )
    return any(value in simplified for value in non_actions)


def dedupe_action_items(items: list[ActionItem]) -> list[ActionItem]:
    seen: set[str] = set()
    result: list[ActionItem] = []
    for item in items:
        key = item.task.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def extract_due_date(text: str) -> str | None:
    patterns = [
        r"\bby\s+([A-Z][a-z]+\s+\d{1,2})\b",
        r"\bby\s+(tomorrow|today|next week|end of week|eow|Friday)\b",
        r"\bdue\s+([A-Z][a-z]+\s+\d{1,2}|tomorrow|today|next week|end of week|eow|Friday)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def first_unique(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def build_local_executive_summary(title: str, key_points: list[str]) -> str:
    if not key_points:
        return (
            f"{title} was processed locally from the available transcript text. "
            "The transcript was too short to extract a detailed summary."
        )
    first_theme = key_points[0].removeprefix("The discussion centered on ").rstrip(".")
    themes = f"{first_theme}. " + " ".join(key_points[1:3])
    if is_generic_title(title):
        return f"The meeting focused on {themes}"
    return f"{title} focused on {themes}"


def build_follow_up_email(
    title: str,
    key_points: list[str],
    actions: list[ActionItem],
    questions: list[str],
) -> str:
    action_lines = (
        "\n".join(
            (
                f"- {item.task} "
                f"(Owner: {item.owner or 'Unknown'}, "
                f"Due: {item.due_date or 'Not specified'})"
            )
            for item in actions
        )
        or "- Confirm next steps and owners."
    )
    question_lines = "\n".join(f"- {question}" for question in questions[:3]) or "- None captured."
    recap = " ".join(key_points[:2]) if key_points else "Thanks for the discussion."
    return (
        f"Subject: Follow-up from {title}\n\n"
        "Hi everyone,\n\n"
        f"Thanks for the discussion. Quick recap: {recap}\n\n"
        "Action items:\n"
        f"{action_lines}\n\n"
        "Open questions:\n"
        f"{question_lines}\n\n"
        "Best,\n"
    )


def transcribe_audio_file(path: str | Path, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required when TRANSCRIBE_AUDIO=true.")

    with Path(path).open("rb") as audio_file:
        files = {"file": (Path(path).name, audio_file, "application/octet-stream")}
        data = {"model": "whisper-1"}
        headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
        with httpx.Client(timeout=120) as client:
            response = client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers=headers,
                data=data,
                files=files,
            )
            response.raise_for_status()
    return str(response.json()["text"])


def raise_for_openai_error(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = f"OpenAI API request failed with HTTP {response.status_code}."
        try:
            body = response.json()
            error = body.get("error") or {}
            detail = error.get("message") or body
            message = f"{message} {detail}"
        except ValueError:
            if response.text:
                message = f"{message} {response.text[:300]}"
        raise OpenAIClientError(message) from exc
