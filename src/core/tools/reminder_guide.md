# Reminder Tool — Usage Guidelines

## Two Modes

### PASSIVE Mode (notify only)
- Default mode — just sends a notification at the specified time
- Use for: "Remind me to call John at 3 PM", "Remind me about the report tomorrow"
- The principal gets a message, they decide what to do

### ACTIVE Mode (action_goal — auto-execute)
- Sets a reminder that EXECUTES a task when it fires
- Use for: "Post on LinkedIn at 9 AM", "Send the report email tomorrow at 8 AM"
- The action_goal must contain the FULL task description so Nova can execute it autonomously
- Include all relevant details: post content, recipient, subject line, etc.

## Recurring Reminders
- Set recurrence: 'daily', 'weekdays' (Mon-Fri), 'weekly', or 'Nd' (every N days)
- "Every evening research and post" → ACTIVE + recurrence='daily'
- "Every weekday at 9 AM" → recurrence='weekdays'
- Recurring reminders auto-reschedule after firing — no manual re-creation needed
- Cancel a recurring reminder to stop all future occurrences

## Random Time Windows
- Use random_window_minutes for "between X and Y" style timing
- "Between 6-8 PM" → remind_at='18:00' + random_window_minutes=120
- Each occurrence picks a new random offset within the window

## Choosing the Right Mode
- "remind me to..." → PASSIVE (they want a nudge)
- "do X at Y time" or "schedule posting at..." → ACTIVE (they want execution)
- "every day/evening/morning do X" → ACTIVE + recurrence
- When in doubt, use PASSIVE — let the principal decide whether to act

## Time Parsing
- Support relative times: "in 30 minutes", "in 2 hours", "tomorrow morning"
- Support absolute times: "at 3 PM", "at 9 AM tomorrow", "next Monday at noon"
- Always confirm the parsed time: "I'll remind you at 3:00 PM PST"
