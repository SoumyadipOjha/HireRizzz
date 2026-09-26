Conversation so far:
<<<CONVERSATION_START>>>
{{conversation}}
<<<CONVERSATION_END>>>

The candidate just said:
<<<CANDIDATE_START>>>
{{candidate_message}}
<<<CANDIDATE_END>>>

CURRENT STEP: {{current_step}}
{{step_guidance}}

Decide:
1. candidate_intent — what their latest message is doing:
   "consent_yes" (happy to talk now), "answer" (answers the current question,
   including "I'd rather not say"), "question" (they asked you something),
   "reschedule" (not a good time / call another time), "opt_out" (not
   interested, do not contact me, wants to stop), "unclear" (you could not
   understand or it was cut off), "other".
2. current_step_resolved — true only if the CURRENT STEP is done and the
   conversation can move on. An answer that is vague but usable counts as
   resolved. A clear refusal to answer also counts as resolved.
3. reschedule_note — if they want another time, when (their words); else null.
4. say — what you say next:
   - If resolved: {{if_resolved}}
   - If not resolved: {{if_not_resolved}}
   - If they asked a question: answer briefly first (per your rules), then
     continue as above.
   - If "reschedule" or "opt_out": acknowledge politely, confirm, and say goodbye.
     Do not ask anything else.
