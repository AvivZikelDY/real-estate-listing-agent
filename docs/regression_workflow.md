# Real Listing Regression Workflow

Use this workflow for every new real listing tested before and after MCP
integration.

## Case A — listing behaves correctly

Run the listing through the diagnostic pipeline.

Check the Pre-Tool Triage against:

```text
docs/pre_tool_triage.md
```

If:

* important facts are present;
* important claims are retained;
* claim classifications are sensible;
* tool plan is correct;
* prepared inputs are correct;
* unsupported claims remain visible;
* follow-up questions are useful;

then add the listing as a passing regression scenario where useful.

No production change is required.

---

## Case B — listing behaves incorrectly

### Step 1 — capture the failure

Run:

```text
/failure-to-regression
```

Provide:

* the real listing;
* optional buyer question;
* diagnostic output;
* expected semantic behavior.

The prompt must:

* locate the first point of loss;
* create a deterministic failing regression;
* stop before implementing a fix.

Expected verdict:

```text
REGRESSION_CAPTURED
```

---

### Step 2 — implement the general fix

Start a fresh Copilot Agent chat and run:

```text
/fix-from-regression
```

The implementer must:

* read the regression and diagnosis;
* implement a general shared production rule;
* make the new regression pass;
* preserve all previous regressions;
* print the Pre-Tool Triage;
* run full tests;
* save before/after artifacts.

Expected verdict:

```text
REGRESSION_FIXED
```

---

### Step 3 — independent review

Start a new Copilot Chat.

Select:

```text
claim-retention-reviewer
```

or the relevant reviewer agent for the changed layer.

Send:

```text
Review the current regression fix independently.

Inspect the diagnosis, regression, production changes, full test suite, and
saved before/after outputs.

Do not edit files.
Do not make paid LLM calls unless explicitly authorized.

Confirm that:
- the fix is general;
- it is not hard-coded to the example listing;
- previous scenarios remain valid;
- claim selection is semantically correct;
- tool selection remains correct;
- tool inputs remain typed and grounded;
- important unsupported information is preserved;
- no new production LLM call was added;
- the live tool contracts were not broken.

Return PASS or FAIL with an acceptance matrix.
```

---

## Step 4 — gate

A new listing regression is considered closed only when:

```text
[ ] failing regression captured before fix
[ ] general production fix implemented
[ ] new regression passes
[ ] all previous regression scenarios pass
[ ] full pytest passes
[ ] Ruff clean
[ ] Pre-Tool Triage is semantically correct
[ ] reviewer verdict = PASS
```

Only then commit the change.

---

# Recommended iteration without the live tools

Do not wait for the MCP implementation.

Collect approximately 5–10 diverse real listings.

Prefer examples that stress different agent behaviors:

1. long marketing-heavy listing;
2. short listing with missing information;
3. listing with nearby amenity claims;
4. listing claiming below-market price;
5. listing with unusual transaction structure;
6. rental listing;
7. listing with many building attributes;
8. contradictory listing information;
9. buyer asks one very narrow question;
10. buyer asks a broad suitability question.

For each listing, inspect:

```text
Property Facts
Claims
Verification Decisions
Tool Plan
Prepared Tool Inputs
Verification Still Required
Missing Information
Follow-Up Questions
```

The purpose is not to make every listing produce tool calls.

The purpose is to ensure the agent preserves the right information and makes
the correct decision about what it can and cannot verify.

---

# Important principle

Do not optimize the implementation to make one listing pass.

Every failure must become a reusable semantic invariant.

The growing regression suite is the mechanism by which the system becomes
more reliable over time.
