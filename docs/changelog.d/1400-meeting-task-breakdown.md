- **Meetings now break down into tasks with an owner and a due date.** A new stage runs when a
  meeting's cleaned transcript is complete: it splits the commitments made in the call into discrete
  tasks, binds each owner to the speaker who took it (an unclaimed one stays `unassigned` rather than
  being guessed), and resolves the spoken deadline — "by Friday", "end of the month", "in two weeks" —
  against the meeting's own date. Each task lands in the workspace as
  `kg/entities/task/<slug>.md`, with the whole breakdown indexed at
  `kg/entities/meeting/<id>.tasks.json`, and rides the meeting stream as a `task` event. Governed
  from `agents/tasks.md`; `VEXA_MEETING_TASKS=0` switches it off. See
  [Break a meeting into tasks](/how-to/meeting-tasks).
