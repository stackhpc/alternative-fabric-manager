---
name: review-loop
description: Run an independent review loop on AFM changes - a fresh reviewer agent reads the spec and code, read-only, reports confirmed findings with scenarios, you fix them, and it re-checks until it signs off. Use when the user asks for a review of a change, a spec, or "keep iterating until the reviewer is happy".
---

# Review loop

The aim is a reviewer that isn't anchored on your reasoning, and a loop that
ends only when it has no confirmed findings.

## Each round

1. **Spawn a fresh reviewer** for a new area, or resume the last one only to
   verify its own findings were fixed. Give it:
   - the repo, branch and exact commit range, and what each commit is for;
   - the spec sections the code must implement (specs/ha.md is signed off);
   - what it may rely on as already reviewed;
   - the invariants (see AGENTS.md) and networking-mlnx's contract:
     per-object ordering, a 400 is final, a DELETE's 404 counts as done,
     failed jobs are resent, 503 is retried;
   - **its limits**: read-only, no Docker, no `ib*`/`saquery`/`smp*`, no
     connecting to the live API or eswitchd, scripts only in a scratch dir;
   - what to report: confirmed findings ranked, each with file:line, a
     concrete scenario and a fix; lower-confidence concerns separately; an
     explicit verdict.
2. **Check before fixing.** Confirm each finding against the code, the
   OpenSM binary's strings, or a test; reviewers can be wrong, and claims
   about OpenSM log formats must be checked against the binary (see the
   `opensm-log-codes` skill).
3. **Fix with a regression test** that fails on the old code. For spec
   findings, change the spec first.
4. **Commit and push** each round, with the findings summarised in the
   commit message, never as review notes in the repo.
5. **Re-review** until the verdict is that it is happy. Then tell the user
   what changed across the rounds, and what the reviewer left as
   lower-confidence.
