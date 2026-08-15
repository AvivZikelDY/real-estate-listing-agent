# Convert a real listing failure into a regression scenario

Analyze the supplied real-estate listing, optional buyer question, and
diagnostic output.

Do not modify production behavior yet.

The objective is to convert the observed failure into a reproducible,
deterministic regression scenario before any fix is implemented.

## Required procedure

1. Identify the expected semantic decomposition of the listing.
2. Compare it with:

    * raw LLM output;
    * parsed plan;
    * normalized claims;
    * retained claims;
    * final validated tool plan;
    * prepared tool inputs;
    * Evidence Bundle, when available.
3. Identify the first exact pipeline stage where the important information is
   lost or changed.
4. Classify the failure:

    * input loss;
    * LLM extraction;
    * parsing;
    * canonical normalization;
    * deduplication;
    * claim prioritization;
    * claim cap;
    * goal filtering;
    * tool linkage;
    * tool-input construction;
    * evidence linkage;
    * report grounding.
5. Create a deterministic typed fixture reproducing the failure without a real
   LLM call.
6. Add a failing test that expresses the desired semantic invariant.
7. Do not implement the production fix in this task.

## Required scenario record

Create a scenario containing:

* scenario ID;
* title;
* raw listing;
* optional buyer question;
* expected goal;
* expected extracted property facts;
* expected high-value claims;
* expected subjective claims;
* expected transaction-structure claims;
* expected tools;
* forbidden tools;
* expected nearby categories;
* expected missing information;
* expected claim-to-tool linkage;
* expected Evidence Bundle invariants.

## Required quality checks

The regression must:

* test a general semantic rule, not one exact address;
* avoid hard-coded claim indexes where possible;
* avoid relying on unstable real-LLM wording;
* reuse production models;
* exercise the real deterministic validation path;
* fail before the intended fix;
* remain valuable after the immediate bug is corrected.

## Required output

Return:

1. Failure classification.
2. First point of loss.
3. Expected semantic behavior.
4. Test file created or modified.
5. Exact failing assertion.
6. Why the scenario is general rather than overfit.
7. Production modules likely involved.
8. Suggested fix directions, without implementing them.
9. Test command.
10. Final verdict:

```text
REGRESSION_CAPTURED
```

or:

```text
FAILURE_NOT_REPRODUCIBLE
```

Stop before changing production behavior.
