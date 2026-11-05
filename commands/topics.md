---
description: List this project's superlite topics: the live one and any set aside, in plain English.
argument-hint: "[topic to bring back]"
---

Arguments: $ARGUMENTS
Run `superlite-cli topics status`. Tell the user, in plain English and without commands, paths or task ids: the topic in progress (if any) and each parked topic with what it was for, how far it got and when it was last worked on.
If they named a topic to bring back, or then ask for one, run `superlite-cli session ensure --slug <topic>` (that sets aside the live one, if any, and says so) and continue with `/superlite:resume`.
