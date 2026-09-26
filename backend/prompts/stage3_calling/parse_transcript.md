Extract the screening outcome from the call transcript below.

Field guidance:
- interested_in_role: true/false only if the candidate said so; else null.
- notice_period_days: convert to days (1 week = 7, 1 month = 30,
  "immediately" or "already serving and free now" = 0). If they gave a range,
  use the upper bound. null if not stated.
- current_ctc / expected_ctc: exactly as stated, with units and currency
  (e.g. "18 LPA", "₹1.5 lakh per month"). null if not stated or declined.
- willing_to_relocate: true/false for relocation or hybrid office attendance
  as asked; null if not answered.
- answers: exactly one entry per question in the list below, in the same
  order, copying question_id and question text as given.
- candidate_questions: questions the candidate asked the agent.
- red_flags: specific concerns only (e.g. contradicts resume, says they're
  not actually looking, rude/hostile, asks not to be contacted). [] if none.
- sentiment: the candidate's overall attitude on the call.
- overall_summary: 3-4 neutral sentences for the hiring team.

Candidate (from resume): {{candidate_name}}
Role: {{job_title}}

Questions the agent was instructed to ask:
{{questions}}

Transcript (between the markers):
<<<TRANSCRIPT_START>>>
{{transcript}}
<<<TRANSCRIPT_END>>>
