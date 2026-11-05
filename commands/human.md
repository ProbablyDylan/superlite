---
description: Switch this project to the human orchestrator: you approve an overview in chat before any build.
argument-hint: ""
---

Run `superlite-cli orchestrator set human`, then `superlite-cli orchestrator status`, and confirm in one line. From now on no build is orchestrated (linted or dispatched) until the user approves the overview in chat; small inline work needs only their yes in chat. After the approval the run decides on its own and lists any scope changes at finish.
