---
name: cron
description: Schedule reminders and recurring tasks.
---

# Cron

Use the `cron` tool to schedule reminders or recurring tasks.

## Four Modes

1. **Reminder** - message is sent directly to user
2. **Task** - message is a task description, agent executes and sends result
3. **One-time** - runs once at a specific time, then auto-deletes
4. **Daily random window** - runs once per day at a random minute inside a local time window

## Examples

Fixed reminder:
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

Dynamic task (agent executes each time):
```
cron(action="add", message="Check HKUDS/nanobot GitHub stars and report", every_seconds=600)
```

One-time scheduled task (compute ISO datetime from current time):
```
cron(action="add", message="Remind me about the meeting", at="<ISO datetime>")
```

Timezone-aware cron:
```
cron(action="add", message="Morning standup", cron_expr="0 9 * * 1-5", tz="America/Vancouver")
```

Daily random surprise:
```
cron(action="add", message="Check Mastodon and share something interesting", daily_random_start="08:00", daily_random_end="20:00", tz="Asia/Shanghai")
```

Silent-progress background task:
```
cron(action="add", message="Quietly scan Mastodon and share only if something is truly worth interrupting the user for", daily_random_start="08:00", daily_random_end="20:00", tz="Asia/Shanghai", send_progress=False)
```

List/remove:
```
cron(action="list")
cron(action="remove", job_id="abc123")
```

## Time Expressions

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| 9am Vancouver time daily | cron_expr: "0 9 * * *", tz: "America/Vancouver" |
| once per day sometime between 8am and 8pm | daily_random_start: "08:00", daily_random_end: "20:00", tz: "Asia/Shanghai" |
| at a specific time | at: ISO datetime string (compute from current time) |

## Timezone

Use `tz` with `cron_expr` or daily random windows to schedule in a specific IANA timezone. Without `tz`, the server's local timezone is used.
