# The overview (human orchestrator)

With a human orchestrator the human approves an overview, sent as your chat reply. It is not a file and not a
document to open. The plan files are for the agents; the human reads only the overview.

## Order

1. Research, then write the full machine plan `docs/plans/<date>-<topic>.md` in the drafting format, with all the
   detail the implementers need. Check it with plain `superlite-cli plan_tools lint --plan <path>` (not gated) and fix
   what it reports, so the overview describes a plan that holds. Do not run `lint --stable`, `dispatch`, `wave0` or
   `build_prime`: they refuse until the approval.
2. Reply with the overview below, in plain text (not AskUserQuestion, not a file, do not open the plan for the user),
   ending with "Say go ahead in any words to start, or tell me what to change." END THE TURN.
3. Read the answer.
   - Approval, alone or with small changes that keep the approach: send
     `superlite-cli orchestrator approve --review <plan path>` first (it needs the human's reply to be newer than the
     plan file, so approve before editing it), then make the small changes in the plan. Do not pass their words through
     and do not ask them to retype approve.
   - Changes to the goal or approach: revise the plan, send a new overview, END THE TURN again.
4. After the approval, `lint --stable` and dispatch in the same turn. A later change to the scope or approach the human
   saw is decided without asking: record it as `Ruling: [scope] <decision> - <why> - <cost if wrong>`; `superlite-cli
   finish` lists these under "Changed from the approved plan".

## The overview

Plain English, at the level of what and why, never how-to. As long as the work needs and no longer: a small change
can be a few lines, a large one a few paragraphs per part. Do not pad and do not cut what the human needs to decide.

- **Goal:** what changes and why, in a sentence or two.
- **Approach:** how, as bullets or short paragraphs. Name each part by what it does for the human. Say what you
  assumed. When a real alternative exists, say in one sentence why you did not take it.
- **Touches:** the areas of the project it changes: modules, data, assets, tools. A folder or file name is fine when it
  helps the human see the reach; never line numbers.
- **Questions:** only choices the human should make, each with your default: `Grade width for small lakes: narrower
  than the bay (default) | the same`. A go-ahead accepts every default. With none, write "None."

Add these only when they matter for this work: what it will not do, risks worth knowing, how the human will be able to
check it worked, anything that needs them later (a test in game, a name to choose).

## Leave out

Commands, line numbers, function names and signatures, code, task ids, the task graph, Produces/Consumes, sizes and
predicted minutes, agent and model names, exact values (unless one is a choice for the human), and the plan's path
(the human does not need to open it).
