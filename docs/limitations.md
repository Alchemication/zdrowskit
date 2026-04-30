# Limitations

zdrowskit is a personal Apple Health coach, not a general health-data platform.

- **Apple ecosystem only** - works only with Apple Watch + iPhone + iCloud Drive. No Garmin, Fitbit, or Android. Adding another data source would mean a new parser and import path.
- **Single user, tied to one Apple ID** - each instance is bound to one Apple ID's HealthKit + iCloud Drive. This is a personal tool, not a multi-tenant service.
- **Third-party export app required** - Apple's built-in XML export crashes on real-world data sizes, so [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) is needed. Premium tier is required for scheduled automations.
- **macOS-only daemon** - the always-on watcher assumes macOS iCloud paths and `launchctl`. CLI commands run anywhere Python runs; the daemon does not.
- **Not real time** - iOS only exports HealthKit data while the phone is unlocked, and Auto Export runs on a schedule. Data arrives in batches with minutes of latency, not seconds.
- **Single profile per macOS user** - multiple always-on daemons on one user account are not supported. Running separate profiles on separate macOS user accounts is the clean path today.
- **Manual context bootstrap** - `setup` creates `me.md` and `strategy.md` templates, but you have to fill in your profile, goals, and weekly plan by hand. A guided LLM-driven onboarding flow does not exist yet.
- **Not fully local** - SQLite storage stays local, but LLM calls send selected context, metrics, workouts, and journal excerpts to your configured provider.

For model routing, fallbacks, and projected LLM spend, see [LLM setup](llm.md).
