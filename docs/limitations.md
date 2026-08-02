# Limitations

zdrowskit is a personal Apple Health coach, not a general health-data platform.

- **Apple Health source only** - health data still originates from Apple Health on an iPhone. There is no Garmin, Fitbit, or Android ingestion path. HTTP, Google Drive, and iCloud are transports, not additional health-data sources.
- **Single person per database** - family hosting uses one cache, context tree, and SQLite database per person. Cross-profile queries are intentionally unsupported.
- **Third-party export app required** - Apple's built-in XML export crashes on real-world data sizes, so [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) is needed. Premium tier is required for scheduled automations.
- **Platform-specific service installation** - the daemon runs on macOS or Linux, but built-in service installation manages macOS `launchd` only. A Raspberry Pi deployment needs an external systemd unit or equivalent. The documented public HTTP path also assumes a Tailscale Funnel-capable host.
- **Public ingest depends on Tailscale Funnel** - Funnel is currently a Tailscale beta feature. Upload tokens protect profile routing and Funnel exposes only the receiver, but availability still depends on the host being logged in and Tailscale running.
- **Not real time** - iOS only exports HealthKit data while the phone is unlocked, and Auto Export runs on a schedule. Data arrives in batches with minutes of latency, not seconds.
- **Small operator-managed roster only** - one daemon can serve roughly 1–10 linked private Telegram accounts, but there is no self-service signup, group-chat support, web admin, hot reload, or per-profile timezone.
- **Shared provider billing** - every profile uses the operator's provider keys. V0 has no per-profile call or spend cap.
- **Manual context bootstrap** - `profile add` creates `me.md` and `strategy.md` templates, but you have to fill in the profile, goals, and weekly plan by hand. A guided LLM-driven onboarding flow does not exist yet.
- **Not fully local** - SQLite storage stays local, but LLM calls send selected context, metrics, workouts, and journal excerpts to your configured provider.

For model routing, fallbacks, and projected LLM spend, see [LLM setup](llm.md).
