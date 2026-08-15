# Pre-Tool Triage Contract

## Purpose

The Pre-Tool Triage is the observable output of the agent immediately before
real external tools are required.

It exists so that the system can be evaluated meaningfully even while the
real MCP/tool implementations are still pending.

> **Default output is now scope-focused.** The current project does *not*
> perform full real-estate due diligence. The only claims for which we
> collect external evidence are (1) supported nearby/place claims and
> (2) comparative price claims. `scripts/pre_tool_triage.py` therefore
> prints a **focused** view by default (see "Focused default output"
> below). The full, broad contract described in the rest of this document
> is still produced (it backs the focused projection and the JSON
> document) and is available on the console via `--verbose`.

## Focused default output

The default console output (`scripts/pre_tool_triage.py`, no `--verbose`)
answers only:

1. What property information needed by the available tools was extracted?
2. Which nearby claims relevant to the current supported tool categories
   were found?
3. Which price-comparison claims were found?
4. Which relevant claims are unsupported by the current tool contract?
5. Which tools will run?
6. What exact input will each tool receive?
7. What required information is missing?

It is a pure, deterministic projection (`build_focused_view`) of the full
report below onto the three current tools (`geo_resolver`,
`nearby_services`, `price_signal`) - it adds no new analysis, no LLM call,
and no tool execution. Sections:

- **PROPERTY CONTEXT** - only tool-relevant fields actually found.
- **RELEVANT CLAIMS FOUND** - only `nearby` and `price comparison` claims.
  A nearby claim is "supported" iff its own text maps to a frozen
  `nearby_services` category via the production `resolve_nearby_categories`
  (e.g. park/school/daycare); otherwise it is shown concisely as an
  unsupported nearby category with `tool: none` (never mis-mapped).
- **PRICE** - asking-price presence, comparative listing/buyer-question
  presence, and a `price_signal: NEEDED / NOT NEEDED / MISSING INPUT`
  decision. The mere existence of `asking_price` + `sqm` never causes
  `price_signal`.
- **FINAL TOOL PLAN** - only tools that would actually execute, with their
  exact prepared input, or "No external evidence tools needed."
- **MISSING INFORMATION** - only inputs that materially affect a relevant
  nearby/price check.
- **OTHER LISTING CONTENT** - one concise line noting that other seller
  claims were present but out of the current evidence scope (expand with
  `--verbose`).

`--from-report <path>` re-renders a previously saved report JSON through
this focused view without any pipeline run or LLM call.

The output must answer (full/`--verbose` contract):

1. What objective property information did the listing provide?
2. What meaningful claims did the listing make?
3. What did the buyer specifically ask?
4. Which claims can the current tools verify?
5. Which claims require another verification channel?
6. Which claims are subjective or unsupported?
7. Which tools would be executed?
8. What exact typed input would each tool receive?
9. What information is still missing?
10. What should the buyer ask the seller, broker, or lawyer next?

---

# 1. Property Facts

Structured fields extracted from the listing.

Example:

```json
{
  "listing_type": "purchase",
  "address": "גונן 14",
  "neighborhood": "תל חיים",
  "city": "תל אביב-יפו",
  "asking_price": 4350000,
  "rooms": 5,
  "area_sqm": 123,
  "floor": 5,
  "building_floors": 6
}
```

Property facts are seller/listing-provided information.

They are not automatically considered externally verified.

---

# 2. Buyer Goal

Examples:

```text
general_listing_triage
family_suitability
price_validation
nearby_services_check
general_question
```

When a buyer question is provided, preserve the original question.

Example:

```json
{
  "buyer_goal": "price_validation",
  "buyer_question": "האם 4,350,000 ש\"ח הוא מחיר הוגן?"
}
```

---

# 3. Atomic Claims

Important assertions must be represented separately.

A compound listing sentence such as:

```text
נוף ירוק ופתוח, מתחם גני ילדים מעבר לכביש והמון חניה ברחוב
```

should conceptually become:

```text
- יש נוף ירוק ופתוח
- יש מתחם גני ילדים מעבר לכביש
- יש הרבה חניה ברחוב
```

Do not require every atom to trigger a tool.

Each retained claim should expose, where supported:

```json
{
  "claim_id": "claim-x",
  "text": "...",
  "source": "listing",
  "category": "...",
  "verification_decision": "..."
}
```

---

# 4. Verification Decisions

Each material claim should fall into one of these semantic groups.

## LISTING_FACT

The value came from the listing and is used as structured context.

Examples:

* asking price;
* rooms;
* area;
* floor;
* address.

It is not independently verified merely because it was extracted correctly.

## CHECKABLE_WITH_TOOL

The current tool contract can collect relevant external evidence.

Examples:

* kindergarten proximity;
* nearby schools;
* nearby parks;
* price comparison when comparative intent exists.

## REQUIRES_SELLER_OR_DOCUMENT

The claim is factual, but current external tools cannot verify it.

Examples:

* safe room;
* exact floor;
* kitchen supplier/specification;
* upgraded bathroom;
* future parquet installation;
* delivery commitment.

## REQUIRES_CONTRACT_REVIEW

The claim concerns commercial/legal transaction terms.

Examples:

* rent followed by purchase;
* purchase option;
* future purchase price;
* rent credits;
* whether purchase is optional or binding.

## SUBJECTIVE

Marketing or experiential wording that cannot be objectively established by
the current tools.

Examples:

* beautiful;
* luxurious;
* pampering kitchen;
* amazing green view.

A factual subclaim embedded inside subjective wording must still be preserved
when possible.

## OUT_OF_SCOPE

The question or claim is outside both current tools and the intended triage
scope.

## MISSING_INFORMATION

The claim could potentially be evaluated, but required input is absent.

Example:

```text
"האם המחיר משתלם?"
```

when the listing does not provide asking price or area.

---

# 5. Tool Plan

Display only the tools actually required by retained relevant claims.

Example:

```json
[
  {
    "tool_name": "geo_resolver",
    "supports_claim_ids": ["claim-daycare"],
    "depends_on": []
  },
  {
    "tool_name": "nearby_services",
    "supports_claim_ids": ["claim-daycare"],
    "depends_on": ["geo-call"]
  }
]
```

A tool must not run merely because the project happens to have that tool.

---

# 6. Prepared Tool Inputs

Display the exact typed payload produced by production `build_tool_input()`.

Example:

```json
{
  "tool_name": "geo_resolver",
  "arguments": {
    "address": "גונן 14, תל חיים, תל אביב-יפו",
    "city_hint": "תל אביב-יפו"
  }
}
```

Dependent values must remain explicit references until the dependency executes.

Example:

```json
{
  "tool_name": "nearby_services",
  "arguments": {
    "latitude": {
      "from_call": "geo-call",
      "field": "latitude"
    },
    "longitude": {
      "from_call": "geo-call",
      "field": "longitude"
    },
    "categories": ["daycare"]
  }
}
```

Never fabricate unresolved coordinates or normalized addresses.

---

# 7. Verification Still Required

Print a concise list grouped by verification channel.

Example:

```text
Can be checked with current tools:
- kindergarten proximity

Needs seller/document verification:
- existence of safe room
- delivery date
- kitchen specification
- bathroom upgrade
- parquet installation commitment

Needs contractual clarification:
- exact rent-to-purchase mechanism
- future purchase price
- whether rent is credited against purchase price
- whether purchase is optional or binding

Current tools cannot establish:
- actual street-parking availability
- subjective quality of the view
```

---

# 8. Missing Information

Preserve meaningful missing data.

Examples:

```text
- exact delivery date
- future purchase price
- terms of rent-to-purchase arrangement
```

When claims are truncated because of a system safety limit, diagnostics should
record:

* number produced;
* number retained;
* number removed;
* removal reason/categories.

Important buyer-relevant information must not disappear silently.

---

# 9. Buyer Follow-Up Questions

Generate or deterministically retain actionable follow-up topics based on
unverified material claims.

For Gonen 14, appropriate questions include:

```text
- האם הממ"ד מופיע בתוכנית ובהיתר?
- מה מועד המסירה החוזי?
- האם התקנת הפרקט לאחר המסירה מופיעה בהסכם?
- מה בדיוק כלול במפרט המטבח והמקלחות?
- מה תנאי השכירות לשנה או שנתיים?
- האם מחיר הרכישה העתידי נקבע מראש?
- האם דמי השכירות מתקזזים ממחיר הרכישה?
- האם קיימת התחייבות לרכוש או רק אופציה?
- מה קורה אם אחד הצדדים אינו מעוניין להמשיך לרכישה?
```

Questions must be grounded in claims or missing information from this listing,
not a generic real-estate checklist.

> **Implementation note (executable diagnostic).**
> `scripts/pre_tool_triage.py` makes this contract executable. There is no
> deterministic follow-up-question generator in production code, and adding
> one would be a second pseudo-production LLM-adjacent feature. Natural-language
> buyer-question generation therefore remains a `report_writer` concern.
> Instead, the diagnostic derives `follow_up_topics` *strictly
> structurally*: one topic per retained claim whose
> `verification_decision` is `REQUIRES_SELLER_OR_DOCUMENT` or
> `REQUIRES_CONTRACT_REVIEW`, plus one per buyer-facing
> `missing_information` entry - each a mechanical restatement of the
> claim's own text (never the hand-authored Hebrew prose above, and never
> an LLM call).

---

# 10. Acceptance Criteria

A Pre-Tool Triage passes when:

* essential structured property facts are preserved;
* material claims are atomic enough for independent classification;
* buyer-question-relevant claims receive highest priority;
* tool-checkable claims are not displaced by marketing wording;
* transaction structure remains visible even without a tool;
* redundant address/price/rooms/area claims do not consume the claim budget;
* no tool is invoked without semantic justification;
* every planned tool receives a valid typed input;
* unsupported claims do not become fabricated evidence;
* important unverified claims produce useful follow-up questions;
* no real-tool result is invented.

This contract is the primary acceptance surface until Nir's real tools are
connected.
