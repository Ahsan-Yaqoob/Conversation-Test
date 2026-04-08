import os
import json
import logging
from google import genai 
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL   = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash') 

_client = genai.Client(api_key=GEMINI_API_KEY)


def _format_conversation(messages: list) -> str:
    lines = []
    for msg in messages:
        role = msg.get('role', 'Unknown')
        text = msg.get('text', '')
        lines.append(f"[{role}]: {text}")
    return '\n'.join(lines)


def _format_reference_data(ref_data: dict) -> str:
    lines = []
    for section, items in ref_data.items():
        lines.append(f"\n## {section}")
        if isinstance(items, list):
            if items and isinstance(items[0], dict):
                for item in items:
                    lines.append("  - " + ", ".join(f"{k}: {v}" for k, v in item.items()))
            else:
                lines.append("  " + ", ".join(str(x) for x in items))
    return '\n'.join(lines)


def analyze_session(session_data: dict) -> dict:
    """
    Send session conversation, result JSON, and reference data to Gemini.
    Returns an analysis dict with overall_status, issues list, and summary.
    """
    conversation = session_data.get('conversation', [])
    result_json = session_data.get('result_json')
    reference_data = session_data.get('reference_data', {})

    # Skip analysis for sessions with no conversation or no result
    if not conversation or result_json is None:
        return {
            'overall_status': 'pending',
            'issues': [],
            'summary': 'No conversation or result data available for analysis.',
        }

    conv_text = _format_conversation(conversation)
    result_text = json.dumps(result_json, indent=2)
    ref_text = _format_reference_data(reference_data)

    prompt = f"""You are a quality assurance AI for a travel quotation system.
Your job is to verify that the AI quotation assistant correctly captured ALL information
from the user conversation into the result JSON, using the available reference data.

=== CONVERSATION ===
{conv_text}

=== RESULT JSON (produced by quotation AI) ===
{result_text}

=== REFERENCE DATA (valid options in the system) ===
{ref_text}

=== YOUR TASK ===
Carefully identify REAL errors only. Use the following rules strictly:

**DATE FORMAT RULE (critical):**
- All dates in the JSON must be in ISO format: YYYY-MM-DD (e.g. 2026-04-10)
- If a date is stored in any other format (e.g. "10-April-2026", "April 10", "10/04/2026") → flag as wrong_value (high severity)
- If a date VALUE is wrong (e.g. departure on Apr 10 but arrival on Apr 15 for same one-way flight leg) → flag as wrong_value (high severity)
- Do NOT flag dates that are in correct ISO format with correct values

**PREDICTION RULE (important):**
- If the user did NOT specify a value (e.g. direct vs indirect flight, meal type, room category) and the AI made a reasonable prediction based on context → this is ACCEPTABLE, do NOT flag it
- Only flag a predicted value if the prediction is clearly WRONG based on what the user said

**ID / REFERENCE RULE:**
- Cross-check supplier names, hotel names, room types, etc. against reference data
- If the AI used a wrong ID that doesn't match the name mentioned → flag as wrong_id (high severity)
- If a name mentioned by the user doesn't exist in reference data → flag as not_in_reference (medium severity)

**MISSING DATA RULE:**
- If the user clearly stated something but it's missing from JSON → flag as missing_field (high severity)
- If a field is null but the user never mentioned it → do NOT flag it

**EXCHANGE RATE RULE:**
- ONLY flag exchange_rate if the user EXPLICITLY stated a specific exchange rate value in the conversation (e.g. "exchange rate 200", "rate 1.5")
- If the user did NOT mention exchange rate at all → do NOT flag it, regardless of what value the AI stored (0, 1, null). The system auto-defaults it.
- If the user DID state a specific rate and the AI stored a different value → flag as wrong_value (low severity)

**HOTEL COUNT RULE (critical):**
- Count how many DISTINCT hotels the user mentioned (distinct = different hotel name OR different city OR different check-in/check-out dates)
- Count how many hotel objects exist in the JSON `hotels` array
- If the JSON has MORE hotel objects than distinct hotels the user mentioned → flag as wrong_value (high severity)
  * Example: user mentions 1 hotel with 2 room types → JSON should have 1 hotel entry, not 2
  * Describe: "User mentioned X hotel(s) but JSON contains Y hotel entries"
- Multiple room types for the SAME hotel do NOT require multiple hotel entries — they should be in ONE hotel object

**PAX-SIZE DESCRIPTOR RULE (critical — read before checking room type or quantities):**
The following words are PAX-SIZE DESCRIPTORS that describe room capacity:
  Single (1 pax), Double (2 pax), Twin (2 pax), Triple (3 pax), Quad (4 pax), 5-pax, 6-pax, etc.

**Double and Twin are IDENTICAL in the system — both = 2 pax:**
- When user says "1 Twin 1 Double" (or any mix of Twin+Double), the AI picks ONE of them to store in `room_type` and `room_type_id`. Either "Twin" or "Double" is correct — do NOT flag it.
- Do NOT flag `room_type="Double"` when user also said "Twin", and vice versa.
- Do NOT flag `room_type_id` for the same reason — whichever ID the AI picked for Twin or Double is acceptable.
- Only flag `room_type` if it is set to something completely unrelated to what the user mentioned.

**ROOM QUANTITIES for same-pax types (Twin + Double):**
- When user mentions both Twin and Double (both 2pax), the AI stores only ONE of their counts in `qty_2pax` (not the sum).
- Accept qty_2pax equal to the count of EITHER Twin OR Double as stated by user — both are valid.
- Example: user says "1 Double 2 Twin" → qty_2pax=1 OR qty_2pax=2 are BOTH acceptable. Do NOT flag either.
- Only flag qty_2pax if the value doesn't match any individual count the user stated (e.g. user said "1 Double 2 Twin" but qty_2pax=5 → flag).

- Room quantities in the JSON (e.g. qty_2pax, qty_3pax) represent NUMBER OF ROOMS of that pax capacity — do NOT treat them as people counts
- ROOM QUANTITIES CHECK (for non-same-pax types):
  * Standard pax capacity mapping: Single=1pax, Double=2pax, Twin=2pax, Triple=3pax, Quad=4pax
  * Example: user says "2 Double 3 Triple" → qty_2pax must be 2, qty_3pax must be 3
  * If qty value is genuinely wrong → flag as wrong_value (high severity)
  * If qty is off by 1 due to extra bed rounding → flag as wrong_value (low severity)
- Do NOT validate or flag `room_count` — it is auto-calculated by the system
- Do NOT validate or flag `weekday_price` or `weekend_price` fields on individual rooms — these are auto-calculated by the system, not set by the AI
- ROOM TYPE rule: The AI captures only the BASE (lowest capacity) room type in the `room_type` field. If user requests multiple room types (e.g. "1 Delux and 1 Triple"), the AI correctly stores only the smaller room type (Triple). Do NOT flag a missing room type for the higher room types — this is correct system behavior.
- Extra bed COUNT rule (critical):
  * extra_beds = (number of DISTINCT PAX CAPACITIES mentioned by user) - 1
  * Twin and Double are BOTH 2pax = 1 distinct capacity. "1 Twin 1 Double" → 1 distinct capacity → extra_beds = 0
  * Example: user says "1 Double, 2 Triple" → capacities: 2pax, 3pax → 2 distinct → extra_beds = 1
  * Example: user says "1 Twin, 1 Double" → BOTH 2pax → 1 distinct capacity → extra_beds = 0
  * Example: user says "2 Double, 3 Triple, 1 Quad" → 2pax, 3pax, 4pax → 3 distinct → extra_beds = 2
  * Flag as wrong_value (high) if the extra_beds value doesn't match this formula
- Extra bed CHARGE rule:
  * If the user explicitly states an extra bed price → that exact price must be used → flag wrong_value if different
  * If the user does NOT state a price → system calculates: (price of 2nd/higher room type) minus (price of 1st/lower room type)
  * Example: "2 Double of 200, 2 Triple of 300" → extra bed charge = 300 - 200 = 100
  * The extra bed charge is a SINGLE value, not stored separately per room type
  * Do NOT flag the extra bed charge if it matches this formula and the user didn't specify a price

Return ONLY a valid JSON object — no markdown, no extra text:
{{
  "overall_status": "ok",
  "issues": [
    {{
      "type": "missing_field|wrong_id|wrong_value|not_in_reference|null_field",
      "field": "exact field path in result JSON (e.g. hotels[0].meal_type)",
      "expected": "what it should be",
      "actual": "what it currently is",
      "description": "clear human-readable explanation",
      "severity": "high|medium|low"
    }}
  ],
  "summary": "1-2 sentence summary of the analysis result",
  "extractor_rating": 9,
  "rating_reason": "Brief explanation of why this rating was given"
}}

Rules:
- Set overall_status to "error" if any high-severity issues exist
- Set overall_status to "warning" if only medium/low severity issues exist
- Set overall_status to "ok" if no issues found
- Empty issues array means no problems
- Be precise — only flag genuine mistakes, not acceptable AI predictions

Extractor Rating Guide (extractor_rating integer 1–10):
- 10: No issues — perfect extraction
- 9:  1–2 low-severity issues only
- 8:  3–5 low-severity issues, OR 1 medium-severity issue
- 7:  2–3 medium-severity issues, OR 1 high-severity issue
- 6:  4–5 medium-severity issues, OR 2 high-severity issues
- 5:  3–4 high-severity issues
- 4:  5–6 high-severity issues
- 3:  7–8 high-severity issues
- 2:  9–10 high-severity issues
- 1:  11+ high-severity issues or fundamental extraction failure (nothing extracted correctly)
"""

    try:
        response = _client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = response.text.strip()

        # Strip markdown fences if present
        if text.startswith('```'):
            parts = text.split('```')
            text = parts[1] if len(parts) > 1 else text
            if text.startswith('json'):
                text = text[4:]
        text = text.strip()

        result = json.loads(text)
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Gemini JSON parse error: {e}")
        return {
            'overall_status': 'error',
            'issues': [],
            'summary': f'Failed to parse AI response: {e}',
        }
    except Exception as e:
        logger.error(f"Gemini analysis error: {e}")
        return {
            'overall_status': 'error',
            'issues': [],
            'summary': f'Analysis error: {str(e)}',
        }
