Extract the candidate profile from the resume below.

Field guidance:
- full_name: the candidate's name (usually at the top or in the page header).
- email / phone / linkedin_url: copy exactly as written. Phone keeps its
  country code and spacing. Contact details may appear in a header line
  separated by "|".
- location: the candidate's current city/region/country.
- headline: the one-line title under the name, if any; otherwise null.
- summary: 2-3 neutral sentences describing the profile, based only on the resume.
- total_experience_years: use the figure the resume states. If not stated,
  compute it from the work_experience dates (full-time professional roles only;
  exclude internships and education; do not double-count overlapping roles;
  treat "Present" as {{today}}), rounded to one decimal. If dates are
  insufficient, null.
- current_title / current_company: from the role marked Present/Current; null
  if no current role.
- skills: every distinct skill, tool, language, framework or platform named
  anywhere in the resume (skills sections, tables, job bullets). Split
  comma-separated lists into one skill per entry, keep each skill's wording as
  written (a parenthetical qualifier such as "AWS (ECS, Lambda)" stays as one
  entry), and do not add skills that are not named. No duplicates.
- work_experience: one entry per role, most recent first. Dates as "YYYY-MM"
  (e.g. "Apr 2022" becomes "2022-04") or "YYYY" if only a year is given.
  is_current = true only for Present/Current roles (then end_date = null).
  highlights: up to 6 short bullets from that role, keeping numbers and metrics.
- education: one entry per qualification; graduation_year as a 4-digit integer.
- certifications, languages: as listed; [] if none.

Today's date: {{today}}

Resume text (between the markers):
<<<RESUME_START>>>
{{resume_text}}
<<<RESUME_END>>>
