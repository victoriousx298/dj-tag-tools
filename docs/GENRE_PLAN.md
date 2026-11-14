# Genre feature

Implemented in `genre_suggest.py`; see the README section "Genre suggestions" for behaviour and confidence rules.

Decisions: several genres (max 5) named as the sources return them, medium confidence or better, ordered high to low,
no API keys, existing genre tags are never replaced. Source: MusicBrainz only.

Possible later additions: Beatport (needs confirmed terms/API access), a duration check to separate same-name artists.
