import os
import json
import logging
import threading
from datetime import datetime, timezone
from google import genai 
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL   = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash') 

_client = genai.Client(api_key=GEMINI_API_KEY)

# ── Concurrency guard — shared by scheduler auto-analysis + manual API endpoint ──
_analyzing_ids: set[str] = set()
_analyzing_lock = threading.Lock()


def is_analyzing(session_id: str) -> bool:
    with _analyzing_lock:
        return session_id in _analyzing_ids


def run_analysis_safe(session_id: str, session_data: dict) -> dict | None:
    """
    Thread-safe wrapper around analyze_session.
    Returns None immediately if the session is already being analyzed.
    Saves result to cache + DB when done.
    """
    with _analyzing_lock:
        if session_id in _analyzing_ids:
            logger.info(f"Analysis already in progress for {session_id[:8]}, skipping.")
            return None
        _analyzing_ids.add(session_id)

    try:
        from app.cache import get_session, save_session

        # Re-fetch latest data in case it was updated since we queued
        session = get_session(session_id) or session_data

        # Skip if already successfully analyzed (could have been done by another thread)
        existing = session.get('analysis', {})
        if existing and existing.get('overall_status') in ('ok', 'warning'):
            logger.info(f"Session {session_id[:8]} already analyzed, skipping.")
            return existing

        # Blobs may have been stripped from RAM after a prior analysis.
        # Fetch them from DB so we can re-analyze.
        if not session.get('conversation') and not session.get('result_json'):
            try:
                from app.database import get_session_db
                db_data = get_session_db(session_id)
                if db_data and (db_data.get('conversation') or db_data.get('result_json')):
                    session = {**session,
                               'conversation': db_data.get('conversation'),
                               'result_json': db_data.get('result_json'),
                               'reference_data': db_data.get('reference_data')}
            except Exception as fetch_err:
                logger.warning(f"Could not fetch blobs from DB for {session_id[:8]}: {fetch_err}")

        if not session.get('conversation') and not session.get('result_json'):
            logger.info(f"Session {session_id[:8]} has no data to analyze. Analysis will not run.")
            return None

        analysis = analyze_session(session)
        analysis['analyzed_at'] = datetime.now(timezone.utc).isoformat()
        session['analysis'] = analysis
        # reset_dismissed=True clears stale dismissed_issues from any prior analysis
        save_session(session_id, session, reset_dismissed=True)
        logger.info(
            f"Auto-analyzed {session_id[:8]} → {analysis.get('overall_status')} "
            f"({len(analysis.get('issues', []))} issues, rating={analysis.get('extractor_rating')})"
        )
        return analysis
    except Exception as e:
        logger.error(f"Analysis error for {session_id[:8]}: {e}", exc_info=True)
        return None
    finally:
        with _analyzing_lock:
            _analyzing_ids.discard(session_id)


def _format_conversation(messages: list) -> str:
    lines = []
    for msg in messages:
        role = msg.get('role', 'Unknown')
        text = msg.get('text', '')
        lines.append(f"[{role}]: {text}")
    return '\n'.join(lines)


def _format_reference_data(ref_data: dict) -> str:
    if not ref_data:
        return "(no reference data available)"
    lines = []
    for section, items in ref_data.items():
        lines.append(f"\n## {section}")
        if isinstance(items, list):
            if items and isinstance(items[0], dict):
                for item in items:
                    lines.append("  - " + ", ".join(f"{k}: {v}" for k, v in item.items()))
            else:
                lines.append("  " + ", ".join(str(x) for x in items))
        elif isinstance(items, dict):
            for k, v in items.items():
                lines.append(f"  {k}: {v}")

    # Highlight room type → pax capacity mapping explicitly for the AI
    room_types = ref_data.get('room_types') or ref_data.get('roomTypes') or []
    if room_types and isinstance(room_types, list) and isinstance(room_types[0], dict):
        pax_keys = [k for k in room_types[0] if 'pax' in k.lower() or 'capacity' in k.lower() or 'person' in k.lower()]
        name_keys = [k for k in room_types[0] if 'name' in k.lower() or 'type' in k.lower() or 'title' in k.lower()]
        if pax_keys and name_keys:
            lines.append("\n## ROOM TYPE PAX CAPACITY SUMMARY (use this to validate room_quantities)")
            for rt in room_types:
                name = rt.get(name_keys[0], '?')
                pax  = rt.get(pax_keys[0], '?')
                lines.append(f"  - {name} = {pax} pax")

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
Carefully identify REAL errors only. Use the following rules strictly.

⛔ BEFORE YOU WRITE A SINGLE ISSUE, REMEMBER THESE UNBREAKABLE RULES:
  A) The JSON stores ONLY the LOWEST-PAX room type name. Never flag missing higher-pax room type names.
  B) The JSON stores ONLY the price for the LOWEST-PAX room type. Never flag missing higher-pax room prices — not individually, and NOT bundled under any invented field like `room_prices`, `prices`, or similar.
  C) Never invent a field name. Only flag fields that actually exist (or are truly required) in the result JSON.
  D) SAME-PAX qty = SUM, never "pick one". If Twin+Double both = 2pax, then qty_2pax = 1+1 = 2. NEVER flag qty_2pax=2 as wrong for "1 Double 1 Twin".

**SYSTEM MESSAGE RULE (critical):**
- Messages in the conversation that begin with `[SYSTEM]` are **system-injected automated messages**, NOT actual user input.
- Common system messages include "Python database match confirmed. Lead passenger details are COMPLETE — do not ask about them again:" followed by auto-filled passenger data (name, email, mobile, currency, etc.).
- Data that appears ONLY in a `[SYSTEM]` message (and was NOT explicitly stated by the real user in their own messages) is **auto-populated by the backend system**, not entered by the user.
- Do NOT flag any field as missing_field or wrong_value based solely on data from a `[SYSTEM]` message — the system handles those fields automatically and they do not need to appear in the result JSON.
- Only validate data that the actual human user explicitly stated in their own conversational messages.

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
- Exchange rate is a SINGLE GLOBAL value for the entire quotation — it is NOT per-service.
- The user states it ONCE in the conversation (e.g. "exchange rate 200") and the system applies it. It is stored in the first/base service only; it will NOT appear in every service object — this is by design.
- ONLY flag exchange_rate if the user EXPLICITLY stated a specific numeric exchange rate value in the conversation AND it was stored incorrectly in the first service.
- If the user did NOT mention exchange rate at all → do NOT flag it in any service, regardless of what value is stored (0, 1, null). The system auto-defaults it.
- NEVER flag exchange_rate as missing in a secondary service (flight, visa, transfer, etc.) — it is never expected to appear there.
- If the user DID state a specific rate and the AI stored a different value in the first service → flag as wrong_value (low severity)

**TRANSFER VEHICLE RULE (critical):**
Each transfer object has a `supplier_mode` field: `"existing"` or `"new"`.

- `supplier_mode = "existing"`: The vehicle is pre-registered in the system. Its specs (`luggage`, `transmission`, `pax_capacity`, `passengers_capacity`, `luggage_capacity`) are stored in the system database and are NOT expected in the JSON.
  * **NEVER flag** these specs as missing for existing-mode transfers — this is correct behaviour.

- `supplier_mode = "new"`: The user is defining a brand-new vehicle. Specs SHOULD be captured in the `new_vehicle_category` object where possible:
   * `passengers_capacity` → user's stated pax count
   * `luggage_capacity` → user's stated luggage count
   * `value` → vehicle category name (e.g. "Bus")
   * Implementations sometimes capture these specs in sibling vehicle-level fields (e.g., `vehicle_name_luggage_capacity`, `vehicle_name_passenger_capacity`, `vehicle_name_transmission_type`) or under `vehicle_category_*` paths. For correctness, treat a spec as captured if it appears anywhere in the vehicle object (either inside `new_vehicle_category` OR in these vehicle-level/vehicle_category fields).
   * If the user only stated pax/luggage/name for `new_vehicle_name`, do NOT treat that as a missing value for `new_vehicle_category`.
   * **Only flag a spec as missing_field** when ALL THREE conditions are true:
     1. `supplier_mode = "new"`
     2. The user explicitly stated that value for the vehicle category in conversation (e.g. "pax 40", "luggage 2", "transmission Manual")
     3. The field is absent or null in BOTH `new_vehicle_category` AND any vehicle-level/vehicle_category sibling fields (i.e. nowhere captured in the vehicle object)
   * If the user did NOT mention the value for the category, do NOT flag it regardless of what is in the JSON.
   * If duplicate specs exist (present both in `new_vehicle_category` and in vehicle-level fields), prefer `new_vehicle_category` for interpretation but DO NOT flag duplicates as incorrect.

**HOTEL COUNT RULE (critical):**
- Count how many DISTINCT hotels the user mentioned (distinct = different hotel name OR different city OR different check-in/check-out dates)
- Count how many hotel objects exist in the JSON `hotels` array
- If the JSON has MORE hotel objects than distinct hotels the user mentioned → flag as wrong_value (high severity)
  * Example: user mentions 1 hotel with 2 room types → JSON should have 1 hotel entry, not 2
  * Describe: "User mentioned X hotel(s) but JSON contains Y hotel entries"
- Multiple room types for the SAME hotel do NOT require multiple hotel entries — they should be in ONE hotel object

**PER-PERSON vs PER-SERVICE CALCULATION RULE (critical):**
- Check the `calculation_type` field in the result JSON (e.g. `"per_person"` or `"per_service"`).
- **If `calculation_type = "per_service"`:** Each hotel is independent. Validate each hotel's room fields against what the user explicitly stated for that specific hotel.

[COMMENTED OUT - NOT IN USE]
/*
- **If `calculation_type = "per_person"`:** The system automatically copies room_type, room_type_id, room_quantities, extra_beds, and all room-related fields from `hotels[0]` to ALL subsequent hotels (hotels[1], hotels[2], etc.). This is correct system behaviour — the same room configuration applies to all hotels.
  * Do NOT validate room_quantities, room_type, extra_beds, or any room fields for hotels[1] and beyond against what the user said for those hotels.
  * Do NOT flag hotels[1+] room data even if it differs from what the user specified for that specific hotel — it is always a copy of hotels[0].
  * Only validate room-related fields for `hotels[0]` when calculation_type is per_person.
*/

**NEW RULE - PER-PERSON HOTEL COMPARISON (critical):**
- **If `calculation_type = "per_person"`:** The system copies room configurations from `hotels[0]` to subsequent hotels. Instead of just accepting this blindly:
  * Extract what the USER explicitly specified for each hotel (hotels[1], hotels[2], etc.) from the conversation
  * Compare what the AI extracted for that hotel against what the user said
  * Flag as `wrong_value` if there's a mismatch between user input and AI extraction for:
    - Room type
    - Room view / category
    - Meal type
    - Extra beds count and charge
    - Room quantities (qty_Xpax)
  * Example: User says "Hotel 2: Double room with half-board, seaview, 2 extra beds"
    → Check if AI captured: Double room type, half-board meal, seaview view, 2 extra beds
    → If any differ, flag the mismatch (high severity)
  * Do NOT flag if AI correctly captured what user said, even if it differs from Hotel 1

**PAX-SIZE DESCRIPTOR RULE (critical — read before checking room type or quantities):**
The following words are PAX-SIZE DESCRIPTORS that describe room capacity:
  Single (1 pax), Double (2 pax), Twin (2 pax), Triple (3 pax), Quad (4 pax), 5-pax, 6-pax, etc.

**Double and Twin are IDENTICAL in the system — both = 2 pax:**
- When user says "1 Twin 1 Double" (or any mix of Twin+Double), the AI picks ONE of them to store in `room_type` and `room_type_id`. Either "Twin" or "Double" is correct — do NOT flag it.
- Do NOT flag `room_type="Double"` when user also said "Twin", and vice versa.
- Do NOT flag `room_type_id` for the same reason — whichever ID the AI picked for Twin or Double is acceptable.
- Only flag `room_type` if it is set to something completely unrelated to what the user mentioned.

**ROOM QUANTITIES for same-pax types (Twin + Double):**
- When user mentions both Twin and Double (both 2pax), the AI stores the SUM of their counts in `qty_2pax`.
- Example: user says "1 Double 2 Twin" → qty_2pax = 1+2 = 3. Only flag if qty_2pax ≠ 3.

**SAME-PAX COLLAPSING RULE (critical):**
- This applies to ALL room types, not just Twin/Double. Any two room types that share the same pax capacity from the reference data are collapsed into ONE `qty_Xpax` entry.
- When two (or more) room types have the same pax capacity, the AI stores the SUM of all their counts in `qty_Xpax`.
- Example: if "Quad"=4pax and "Delux11"=4pax, then "1 Quad 1 Delux11" → `qty_4pax = 1+1 = 2` — do NOT flag it as wrong.
- Example: if "Twin"=2pax and "Double"=2pax, then "2 Twin 3 Double" → `qty_2pax = 2+3 = 5`.
- Use the REFERENCE DATA to look up each room type's pax capacity. Flag as wrong_value if qty_Xpax ≠ sum of all same-pax room counts.

- Room quantities in the JSON (e.g. qty_2pax, qty_3pax) represent NUMBER OF ROOMS of that pax capacity — do NOT treat them as people counts
- **DATABASE ROOM TYPE PAX RULE (critical):** Room types are NOT limited to standard names (Single, Double, Twin, Triple, Quad). Types like "Delux", "Premium", "Executive", "Suite", "Penthouse" etc. are real room types in the database and each has its own pax capacity stored in the system (e.g. Delux may be 3pax, Premium may be 7pax). The AI correctly looks up each room type's pax capacity from the database and sets the corresponding `qty_Xpax` field. You CANNOT know the exact pax capacity from the room type name alone. Therefore:
  * Do NOT flag any `qty_Xpax` entry as wrong just because you don't recognize the pax number for a named room type
  * Do NOT flag `qty_7pax`, `qty_5pax`, `qty_6pax` etc. as unexpected — these are valid capacities for non-standard room types from the database
  * ONLY flag qty values if the count of rooms is wrong (e.g. user said "2 Delux" but qty shows 1)
- ROOM QUANTITIES CHECK (for standard types only):
  * Standard pax mapping: Single=1pax, Double=2pax, Twin=2pax, Triple=3pax, Quad=4pax
  * Example: user says "2 Double 3 Triple" (no same-pax collision) → qty_2pax=2, qty_3pax=3
  * If two room types share the same pax (e.g. Twin+Double, Quad+Delux11), apply SAME-PAX COLLAPSING RULE above — qty = SUM of all same-pax room counts
  * If qty count is genuinely wrong → flag as wrong_value (high severity)
- Do NOT validate or flag `room_count` — it is auto-calculated by the system
- **WEEKDAY / WEEKEND PRICE RULE (critical):**
  * `weekday_price` and `weekend_price` MUST always hold the price of the BASE room type (lowest pax) — never a higher-pax room type's price.
  * If the user stated **only one price** for the base room (no separate weekday/weekend distinction), BOTH `weekday_price` AND `weekend_price` must be set to that same value. If they differ → flag as `wrong_value` (high).
  * If the user stated **two distinct prices** (explicitly "weekday X, weekend Y") → each field takes its respective value.
  * If the price in `weekday_price` or `weekend_price` belongs to a DIFFERENT (non-base) room type → flag as `wrong_value` (high).
- **ROOM PRICE RULE (critical):** The system ONLY stores the price for the BASE room type (= the room type with the LOWEST pax capacity). Prices for ALL other room types are NOT stored anywhere in the JSON — not individually, and NOT in any bundled field like `room_prices`. Do NOT flag missing prices for any room type that is NOT the lowest-pax one, regardless of what the user stated or the order they mentioned it. Even if the user stated 3 different prices, only 1 (the lowest-pax room's price) is expected. This is by design.
- **ROOM TYPE rule:** The AI captures only the BASE room type (LOWEST pax capacity) in `room_type` and `room_type_id`. Order of mention does NOT matter — the system always picks the lowest-pax room type as the base.
  * Example: user says "1 Executive Suite (4pax) 1 Twin (2pax)" → base = Twin (2pax) → `room_type="Twin"`, price = Twin's price → CORRECT. Do NOT flag this.
  * Example: user says "1 Delux (3pax) 1 Single (1pax)" → base = Single (1pax) → CORRECT.
  * Do NOT flag `room_type` or `room_type_id` just because a different room type was mentioned first by the user — only the lowest-pax room matters.
  * Do NOT flag the price as wrong if it matches the LOWEST-pax room type's price, even if a higher-pax room was mentioned first.

**CUSTOM / NEW ROOM TYPE RULE (critical):**
- ⚠️ THIS RULE APPLIES ONLY TO THE FIRST/BASE ROOM TYPE. Secondary room type names are NEVER stored — do not apply this rule to them.
- When the user's FIRST/BASE room type is NOT in the reference data (e.g. "Penthouse", "Executive Suite", a custom name), the system may store it in `room_type` OR in an alternate field such as `new_room_type`, `custom_room_type`, or any similar field on the hotel object.
- To check if the BASE room type was captured: look at ALL room-type-related fields (`room_type`, `new_room_type`, `custom_room_type`, etc.).
- Only flag as `wrong_value` or `missing_field` if the user's BASE room type name is **completely absent** from ALL room-type-related fields.
- If the value exists in ANY of those fields (even if `room_type` itself is empty), do NOT flag it — the value has been captured correctly.
- Do NOT flag an empty `room_type` field if the custom name appears in `new_room_type` or another variant field.
- Do NOT apply this rule to the 2nd, 3rd, or any additional room type mentioned by the user — their names are simply not stored by design.

**CUSTOM / NEW VALUE RULE (critical):**
- This applies to any service field that can have a custom/new value, such as hotel meal type, flight class/type, visa type, transfer type, or similar.
- If the user explicitly gives a value that is not in the reference data, the AI may store it in an alternate `new_*` or custom field instead of the primary field.
- Meal types (explicit guidance): If the user specified a custom meal (e.g., "Royal Breakfast") that is NOT present in reference data, the AI may capture it in one of two valid ways:
  * directly in the primary field (e.g., `hotels[0].meal_type`) while the corresponding id field (e.g., `meal_type_id`) is missing or null, OR
  * in an explicit `new_meal_type` / `custom_meal_type` field on the hotel object.
  In either case, treat the meal as correctly captured and DO NOT flag it.
- Do NOT map or truncate a custom meal name to a canonical meal (e.g., changing "Royal Breakfast" → "Breakfast"). If such truncation occurred and the original custom phrase does NOT appear anywhere in the capture path (primary field, `new_*`, or custom field), then flag as `wrong_value` (high severity).
- Do NOT flag the primary field as wrong or missing if the same value is correctly captured in the matching `new_*` / custom field.
- Only flag `missing_field` or `wrong_value` when the value is absent from BOTH the primary field and the related `new_*` / custom field for that item.
- In short: if the custom value exists anywhere in its own capture path, it is correct and should not be flagged just because it is not in the main field.

- **NEW ROOM TYPE POSITION RULE (critical):**
  * The frontend form enforces: a custom/new room type (not in the reference data) can ONLY be used as a secondary (non-base) room type if its pax capacity is GREATER than the base room type's pax.
  * If the custom/new room type has LOWER pax than the defined base room type → the custom type MUST be the base, not secondary.
  * Scenario to flag: result JSON shows a defined room type (e.g. Twin=2pax) as base, but the user also mentioned a custom/new room type with explicitly stated lower pax (e.g. 1pax) → flag as `wrong_value` (high): "The new/custom room type has lower pax than the base room type. It must be set as the base (lowest pax / first) room type."
  * IMPORTANT: Only flag this if the custom room type's pax was explicitly stated by the user in the conversation, OR is clearly derivable from context. If pax is unknown for the custom type → do NOT flag.
  * Do NOT flag if the custom room type's pax is GREATER than or EQUAL to the base room type's pax — that is valid and correct.
- Extra bed COUNT rule (critical):
  * extra_beds = (number of DISTINCT PAX CAPACITIES mentioned by user) - 1
  * Use REFERENCE DATA to find each room type's pax capacity. Two room types with the SAME pax = 1 distinct capacity.
  * Twin and Double are BOTH 2pax = 1 distinct capacity. "1 Twin 1 Double" → 1 distinct → extra_beds = 0
  * Example: user says "1 Quad 1 Dupplex" — if reference data shows both are 4pax → 1 distinct capacity → extra_beds = 0
  * Example: user says "1 Double, 2 Triple" → capacities: 2pax, 3pax → 2 distinct → extra_beds = 1
  * Example: user says "2 Double, 3 Triple, 1 Quad" → 2pax, 3pax, 4pax → 3 distinct → extra_beds = 2
  * Example: user says "1 Triple, 1 Delux, 1 Premium" → if all have different pax from DB → 3 distinct → extra_beds = 2
  * IMPORTANT: If you cannot find a room type's pax in the reference data, do NOT guess its pax capacity — skip flagging extra_beds in that case
  * Flag as wrong_value (high) ONLY if extra_beds clearly doesn't match the formula AND you have confirmed pax capacities from the reference data for all room types mentioned
  * **"NO EXTRA BED" OVERRIDE EXCEPTION (critical):** If the user explicitly says "no extra bed" BUT `room_quantities` in the JSON contains more than one distinct pax entry (e.g. qty_1pax AND qty_2pax both present), do NOT flag extra_beds as wrong. The extra_beds value in this case is system-derived from the multi-room-type structure — it is not a physical extra bed the user rejected. "No extra bed" from the user only applies when room_quantities has a single pax type; in that case extra_beds must be 0.
- Extra bed CHARGE rule:
  * If the user explicitly states an extra bed price → that exact price must be used → flag wrong_value if different
  * If the user does NOT state a price → system calculates: (price of 2nd/higher room type) minus (price of 1st/lower room type)
  * Example: "2 Double of 200, 2 Triple of 300" → extra bed charge = 300 - 200 = 100
  * The extra bed charge is a SINGLE value, not stored separately per room type
  * Do NOT flag the extra bed charge if it matches this formula and the user didn't specify a price

⛔ ABSOLUTE RULES — NEVER VIOLATE THESE — READ BEFORE WRITING ANY ISSUE:

1. **NEVER flag a missing room type name for any room type that is NOT the lowest-pax one.**
   The system stores ONLY the lowest-pax room type name in `room_type`. All higher-pax room type names are NEVER stored anywhere in the JSON. There is NO field for them. Do NOT invent field names like `room_type_premium_name`, `room_type_2`, or similar. If it is not in the JSON schema, it does not exist.

2. **NEVER flag a missing or uncaptured price for any room type that is NOT the lowest-pax one.**
   The system stores ONLY the price for the lowest-pax room type. Prices for all higher-pax room types are NEVER stored in the JSON — not in any field. Do NOT flag `room_type_premium_price`, `price_premium`, `room_prices`, or any invented field that groups multiple room prices. This is by design. Even if the user stated 3 different room prices, ONLY the lowest-pax room's price is expected in the JSON.

3. **NEVER flag a field that does not exist in the actual result JSON.**
   Only report issues for fields that are actually present in the JSON (with a wrong value) or fields that are genuinely required by the schema but missing. Do not invent field names.

4. **SAME-PAX COLLAPSING: qty_Xpax ALWAYS = SUM of all same-pax room counts — NEVER "pick one".**
   When multiple room types share the same pax capacity (e.g. Twin=2pax + Double=2pax, or Quad=4pax + Delux11=4pax), the correct `qty_Xpax` value is the SUM of their counts. Example: "1 Double 1 Twin" → `qty_2pax = 2` (NOT 1). Example: "2 Twin 3 Double" → `qty_2pax = 5`. NEVER flag `qty_Xpax = SUM` as wrong. NEVER say "pick one" or "store only one". If the JSON shows `qty_2pax = 2` for "1 Double 1 Twin", that is CORRECT — do NOT flag it.

**REDUNDANT QUESTION RULE (AI conversation quality check):**
- Read the full conversation and identify every question the AI asked the user.
- For each AI question, check whether the user had ALREADY provided that information earlier in the conversation (before the AI asked).
- If the AI asked for information that the user had ALREADY clearly stated → flag as `wrong_value` with severity `medium`.
  * Field: use `conversation.ai_behavior` as the field path
  * Expected: "AI should not ask for already-provided information"
  * Actual: Describe what information was asked again and where the user had already provided it
  * Example: User said "I need 2 rooms" at message 2, then AI asked "How many rooms do you need?" at message 6 → flag it
- Do NOT flag if:
  * The AI is asking for CLARIFICATION on an ambiguous or incomplete earlier answer (e.g. user said "some rooms" without a number)
  * The AI is CONFIRMING details back to the user (e.g. "You mentioned 2 rooms, is that correct?") — confirmation is acceptable
  * The information was given very early and the AI is re-checking due to a conversation restart or context switch
- This check is about AI EFFICIENCY and USER EXPERIENCE — the AI should never make users repeat themselves.

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
