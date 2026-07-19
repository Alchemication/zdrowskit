# Limitations

zdrowskit is a personal Apple Health coach, not a general health-data platform.

- **Apple Health source only** - health data still originates from Apple Health on an iPhone. There is no Garmin, Fitbit, or Android ingestion path. Google Drive makes the importer portable, not the health-data source.
- **Single profile per database** - Google Drive can expose multiple people's exports, but each person needs a separate cache and SQLite database. The schema has no user identifier and is not multi-tenant.
- **Third-party export app required** - Apple's built-in XML export crashes on real-world data sizes, so [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) is needed. Premium tier is required for scheduled automations.
- **Platform-specific service installation** - the Google Drive daemon runs on macOS or Linux, but built-in service installation manages macOS `launchd` only. A Raspberry Pi deployment needs an external systemd unit or equivalent.
- **Not real time** - iOS only exports HealthKit data while the phone is unlocked, and Auto Export runs on a schedule. Data arrives in batches with minutes of latency, not seconds.
- **Single profile per daemon process** - multiple people's folders can share one service account, but each daemon still owns one cache, database, context directory, and lock. There is no multi-profile supervisor yet.
- **Manual context bootstrap** - `setup` creates `me.md` and `strategy.md` templates, but you have to fill in your profile, goals, and weekly plan by hand. A guided LLM-driven onboarding flow does not exist yet.
- **Not fully local** - SQLite storage stays local, but LLM calls send selected context, metrics, workouts, and journal excerpts to your configured provider.

For model routing, fallbacks, and projected LLM spend, see [LLM setup](llm.md).
