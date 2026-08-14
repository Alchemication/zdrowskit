# Limitations

zdrowskit is a personal Apple Health coach, not a general health-data platform.

- **Apple Health source only** - health data still originates from Apple Health on an iPhone. There is no Garmin, Fitbit, or Android ingestion path. HTTP, Google Drive, and iCloud are transports, not additional health-data sources.
- **Single person per database** - family hosting uses one cache, context tree, and SQLite database per person. Cross-profile queries are intentionally unsupported.
- **Auto Export JSON required** - the parser supports [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) JSON, not Apple's built-in all-data XML export. Scheduled delivery depends on the automation features available in the installed app version.
- **Platform-specific service installation** - the daemon runs on macOS or Linux, but built-in service installation manages macOS `launchd` only. A Raspberry Pi deployment needs an external systemd unit or equivalent. The documented public HTTP path also assumes a Tailscale Funnel-capable host.
- **Public ingest depends on Tailscale Funnel** - upload tokens protect profile routing and Funnel exposes only the receiver, but availability still depends on the host being logged in and Tailscale running.
- **Not real time** - scheduled automations and HealthKit delivery run opportunistically. Data arrives in overlapping batches with minutes of latency, not seconds.
- **Small operator-managed roster only** - the code does not impose a numeric profile cap, but the operating model is a roster one person can manage directly. There is no self-service signup, group-chat support, web admin, hot reload, or per-profile timezone.
- **Shared provider billing** - every profile uses the operator's provider keys. V0 has no per-profile call or spend cap.
- **Manual context bootstrap** - `profile add` creates `me.md` and `strategy.md` templates, but you have to fill in the profile, goals, and weekly plan by hand. A guided LLM-driven onboarding flow does not exist yet.
- **Not fully local** - SQLite storage stays local, but LLM calls send selected context, metrics, workouts, and journal excerpts to your configured provider.

For model routing, fallbacks, and projected LLM spend, see [LLM setup](llm.md).
