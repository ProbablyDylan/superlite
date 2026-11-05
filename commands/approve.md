---
description: Approve the plan so superlite can lint and build it.
argument-hint: "[path to the plan]"
---

Approve the plan. Arguments: $ARGUMENTS
Use the given path, else the newest plan in `docs/plans/`. Do not open it for the user. If they have not seen the overview yet, reply with it (`skills/drafting/review-plan.md`) and end the turn. Read their reply. If you understand it as approval, send approve: `superlite-cli orchestrator approve --review <path>`. Do not pass their sentence through and do not ask them to type the word approve. If you do not understand it as approval, ask what to change and end the turn. After approve succeeds, make any small changes they asked for, lint the plan with `--stable`, and start the build.
