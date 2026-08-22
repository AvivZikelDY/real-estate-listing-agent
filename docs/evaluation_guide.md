# Evaluation & Demo Guide

## Agent purpose

This is a **fast Tel Aviv real-estate listing triage agent**. Given a
free-text listing (optionally with a buyer question), it decides which of
a small, fixed set of supported evidence checks are worth running, collects
that evidence, and writes a grounded, buyer-facing report.

It is **not**:

- a full due-diligence system
- a certified appraisal
- a legal checker
- a generic real-estate search engine

The main agentic behavior being evaluated is the loop:

```
user goal → claim prioritization → tool selection → evidence collection/reuse → grounded buyer-facing report
```

Two LLM modules drive this (`triage_planner`, `report_writer`); everything
in between — validation, tool-input building, evidence collection, and
evidence-bundle assembly — is deterministic Python.

## Supported question types

### Nearby services

Supported categories exactly: `supermarket`, `school`, `park`, `pharmacy`,
`daycare`, `public_transport`, `hospital`.

Representative Hebrew questions:

- יש פארק קרוב?
- יש סופרמרקט קרוב?
- יש בית ספר קרוב?
- יש גן ילדים / מעון קרוב?
- יש תחבורה ציבורית קרובה?
- יש בית מרקחת קרוב?
- יש בית חולים קרוב?

Distances reported are **straight-line estimates**, not walking
times/routes — the report should never claim a walking duration.

### Price comparison

Representative questions:

- האם המחיר הגיוני ביחס לאזור?
- האם המחיר גבוה או נמוך ביחס לעסקאות דומות?
- המודעה טוענת "מחיר מציאה" — יש לזה תמיכה?

Prerequisites for a real price check: a **purchase** listing, an asking
price, an sqm value, and a sufficiently resolved Tel Aviv location. Missing
any of these should produce an honest "cannot compare" answer, never an
invented figure. `price_signal` is a data-driven signal only — never a
professional/certified appraisal.

### Buyer-goal questions

Representative questions:

- האם הדירה מתאימה למשפחה?
- אילו טענות במודעה אפשר לבדוק?
- מה היית בודק קודם במודעה הזאת?

Broad suitability questions should produce **bounded evidence** (specific
facts found, with their limitations), never an unsupported yes/no verdict.

## Recommended multi-turn demo

A good sequence to demonstrate context reuse and incremental tool use:

1. `יש פארק קרוב?`
2. `כמה רחוק?`
3. `ומה עם סופר?`
4. `וכמה רחוק הסופר הקרוב?`

Expected behavior (conceptually — exact place counts/distances depend on
live data and are not promised here):

- **Turn 1**: resolves the address and fetches park evidence.
- **Turn 2**: `"כמה רחוק?"` resolves to the previous turn's park focus and
  reuses that evidence rather than re-running the tool.
- **Turn 3**: `"ומה עם סופר?"` switches focus to supermarkets and collects
  only the newly required evidence (park evidence is not re-fetched).
- **Turn 4**: reuses the supermarket evidence from turn 3 without rerunning
  any tool.

## Missing-data demo

Ask `"האם המחיר הגיוני ביחס לאזור?"` for a listing where **sqm is absent**.

Expected behavior:

- does not invent an sqm value
- does not fabricate price evidence
- does not produce a market verdict ("cheap"/"expensive"/"below market")
- explains the missing prerequisite instead

The same restraint applies to an inexact/street-only address: the agent
should never invent a house number to make the address look more resolved
than the listing actually states.

## Scope-awareness / edge questions

These are useful specifically because they test restraint — a correct
result may be a stated limitation, a clarification request, or an explicit
statement that current evidence tools cannot verify the claim. The agent
should never map an unsupported concept onto an arbitrary supported tool.

- האם השכונה בטוחה?
- האם בית הספר טוב?
- כמה דקות הליכה לפארק?
- האם יש ממ"ד?
- האם יש חניה רשומה בטאבו?
- האם הבניין יעבור תמ"א / פינוי בינוי?
- האם זו השקעה טובה?
- מה תהיה תשואת השכירות?
- יש בית קפה קרוב?
- יש חדר כושר קרוב?

## Persona / ambiguous questions

Include: `"האם הדירה מתאימה לסטודנטים?"`

Expected behavior:

- preserves "students" as the actual persona being asked about
- does **not** silently convert the question into a family-suitability
  question
- does not claim student suitability as a verified fact
- may explain which supported dimensions could be checked (e.g. proximity
  to public transport or a supermarket) if the buyer wants to narrow the
  question
- may ask the buyer to narrow the criteria

There is no student-specific tool or `buyer_goal` value — and the agent
should never claim there is one.

## What good agent behavior looks like

- The current user's intent controls the plan for this turn.
- Not every listing claim has to be checked every turn.
- Only justified tools are used — no fan-out "just in case".
- Previously successful evidence is reused instead of re-run.
- A new tool only runs when the new question actually needs new evidence.
- Missing values are never guessed or invented.
- A technical failure (timeout, unresolved address) is distinguished from
  genuine evidence of absence ("no results found").
- Claims the current tools cannot check stay explicitly unresolved.
- `price_signal` output is presented as a signal, never an appraisal.
- Subjective listing language ("beautiful", "charming") is never presented
  as a verified fact.

**Important:** a good result does not necessarily mean a tool was executed.
Choosing *not* to run a tool can itself be the correct autonomous decision.

## Current scope boundaries

- Tel Aviv listings only.
- The nearby-services category list is intentionally limited to the seven
  categories above.
- No walking-route/travel-time engine — distances are straight-line only.
- No school-quality or neighborhood-safety scoring.
- No legal/title/building-permit verification.
- No rental-yield or investment recommendation.
- Session/follow-up context is currently process-local and in-memory; it is
  not guaranteed to persist across serverless cold starts or instances.
  Stateless single-turn use is unaffected.
