Score the candidate against the job on four criteria. Each score is an
integer 0-100. Give 1-2 sentences of concrete evidence for each.

Rubric:

1. must_have_skills: share of the must-have skills that are evidenced,
   weighted by depth.
   100 = all evidenced with hands-on professional use.
   70  = all or nearly all evidenced, some only lightly.
   40  = about half evidenced.
   0   = none evidenced.

2. experience: relevant professional experience vs the required range
   ({{experience_range}}).
   100 = within range and all of it relevant.
   70  = within range but only partly relevant, or slightly outside the range
         with highly relevant work.
   40  = clearly below the minimum, or mostly unrelated experience.
   0   = no relevant professional experience.
   Being above the maximum is not a penalty by itself.

3. nice_to_have_skills: share of the nice-to-have skills evidenced.
   100 = all, 50 = about half, 0 = none.

4. role_relevance: how closely the candidate's recent roles and
   responsibilities match this job's day-to-day work (see Responsibilities).
   100 = doing essentially this job now, 50 = adjacent role, 0 = unrelated.

Skill lists: for matched_must_have_skills, missing_must_have_skills and
matched_nice_to_have_skills, use the exact skill names as written in the job
below. Every must-have skill must appear in exactly one of matched/missing.

=== JOB ===
Job ID: {{job_id}}
Title: {{job_title}}
Location: {{job_location}}
Must-have skills: {{must_have_skills}}
Nice-to-have skills: {{nice_to_have_skills}}
Description:
{{job_description}}

=== CANDIDATE PROFILE (JSON extracted from the resume) ===
<<<PROFILE_START>>>
{{candidate_profile}}
<<<PROFILE_END>>>
