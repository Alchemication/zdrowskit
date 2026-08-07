# User Flow Diagrams

## Overview

```mermaid
flowchart TD
    user([User])
    apple([Apple Watch + iPhone])
    export["Auto Export"]
    funnel["Tailscale Funnel: public HTTPS"]
    receiver["Authenticated loopback receiver"]
    cloud["Optional iCloud / Google Drive source"]
    setup["Setup + doctor"]
    roster[(profiles.toml)]
    context[(Per-profile context files)]
    db[(Per-profile SQLite DB)]
    daemon["Background daemon"]
    telegram["Telegram bot"]
    llm["LLM provider"]
    reports["Insights reports"]
    coach["Coach proposals"]
    nudges["Reactive nudges"]
    chat["Interactive health chat"]
    manual["Manual add flow"]
    controls["Prefs, models, diagnostics, agents"]
    approvals["Accept / reject edits and feedback"]

    user --> setup
    setup --> roster
    setup --> context
    apple --> export
    export --> funnel
    funnel --> receiver
    receiver --> daemon
    export --> cloud
    cloud --> daemon
    user --> db
    user --> daemon
    daemon --> context
    daemon --> db
    daemon --> reports
    daemon --> nudges
    reports --> llm
    nudges --> llm
    reports --> telegram
    nudges --> telegram
    reports --> coach
    coach --> llm
    coach --> approvals
    approvals --> context
    telegram --> chat
    chat --> db
    chat --> context
    chat --> llm
    chat --> approvals
    telegram --> manual
    manual --> db
    telegram --> controls
    controls --> db
    controls --> context
    controls --> daemon
```

## Profile Routing And Isolation

```mermaid
flowchart TD
    roster[(profiles.toml)]
    daemon["One daemon process"]
    poller["One Telegram poller"]
    route{"Private sender ID is linked and enabled?"}
    deny["Deny + notify operator"]
    adam["Adam runtime"]
    anna["Anna runtime"]
    adamDb[(profiles/adam/health.db)]
    annaDb[(profiles/anna/health.db)]
    adamContext[(profiles/adam/ContextFiles)]
    annaContext[(profiles/anna/ContextFiles)]
    adamInput["Adam HTTP token/cache or cloud source"]
    annaInput["Anna HTTP token/cache or cloud source"]
    adamTelegram["Replies to Adam"]
    annaTelegram["Replies to Anna"]

    roster --> daemon
    daemon --> poller
    poller --> route
    route -- no --> deny
    route -- Adam --> adam
    route -- Anna --> anna
    adamInput --> adam
    annaInput --> anna
    adam --> adamDb
    adam --> adamContext
    adam --> adamTelegram
    anna --> annaDb
    anna --> annaContext
    anna --> annaTelegram
```

No profile identity is stored in health rows. The selected database file and
profile directory are the isolation boundary.

## Setup And Data Import

```mermaid
flowchart TD
    user([User])
    clone["Clone repo + uv sync"]
    setup["uv run python main.py setup"]
    env["Create .env"]
    addProfile["profile add NAME --operator"]
    roster["Create profiles.toml"]
    tree["Create profile DB + ContextFiles"]
    editContext["Fill me.md and strategy.md"]
    tailscale["Install + sign in to Tailscale"]
    doctor{"uv run python main.py doctor passes?"}
    fixConfig["Fix paths, credentials, or local setup"]
    ingestSetup["ingest setup: create profile token"]
    daemonInstall["daemon-install: start loopback receiver"]
    localHealth["Check local /healthz"]
    funnel["Start persistent Tailscale Funnel"]
    apple["Apple Watch + iPhone"]
    autoExport["Auto Export Metrics + Workouts automations"]
    receiver["Validate, authenticate, and pair uploads"]
    cloudImport["Optional local / Drive import"]
    parse["Parse metrics, workouts, routes, sleep"]
    migrate["Open DB with migrations"]
    db[(SQLite DB)]
    status["status / db status / db schema"]
    report["report"]
    insights["insights"]
    llm["LLM provider"]
    memory["Append memory to history.md"]

    user --> clone
    clone --> setup
    setup --> env
    user --> tailscale
    env --> addProfile
    addProfile --> roster
    addProfile --> tree
    tree --> editContext
    env --> doctor
    editContext --> doctor
    doctor -- no --> fixConfig
    fixConfig --> doctor
    doctor -- yes --> ingestSetup
    ingestSetup --> daemonInstall
    daemonInstall --> localHealth
    tailscale --> funnel
    localHealth --> funnel
    apple --> autoExport
    autoExport --> funnel
    funnel --> receiver
    receiver --> parse
    autoExport -. local / Drive alternative .-> cloudImport
    cloudImport --> parse
    parse --> migrate
    migrate --> db
    db --> status
    db --> report
    user --> insights
    insights --> db
    insights --> context
    insights --> llm
    insights --> memory
    memory --> context
```

## Daemon Notifications

```mermaid
flowchart TD
    user([User])
    install["daemon-install or daemon-restart"]
    daemon["launchd daemon"]
    watcher["Receive HTTP, poll Drive, or watch iCloud + context files"]
    healthEvent{"Health file event?"}
    contextEvent{"me.md / log.md / strategy.md event?"}
    healthDebounce["Pair HTTP payloads or debounce file event"]
    contextDebounce["Debounce context event"]
    importData["Import latest health data"]
    delta["Describe new rows or changed context"]
    prefs{"Prefs enabled and not muted?"}
    quiet{"Before earliest send time?"}
    queue["Queue trigger for later drain"]
    reportWindow{"Near scheduled report?"}
    rate{"Daily cap and min interval ok?"}
    nudge["Run nudge LLM"]
    skip{"LLM returns SKIP?"}
    send["Send Telegram nudge + feedback button"]
    eventLog[(Events table)]
    state[(Daemon state file)]
    db[(SQLite DB)]
    context[(Context files)]
    llm["LLM provider"]
    telegram["Telegram bot"]

    user --> install
    install --> daemon
    daemon --> watcher
    watcher --> healthEvent
    watcher --> contextEvent
    healthEvent -- yes --> healthDebounce
    healthDebounce --> importData
    importData --> db
    importData --> delta
    contextEvent -- yes --> contextDebounce
    contextDebounce --> delta
    delta --> prefs
    prefs -- no --> eventLog
    prefs -- yes --> quiet
    quiet -- yes --> queue
    queue --> state
    queue --> prefs
    quiet -- no --> reportWindow
    reportWindow -- yes --> eventLog
    reportWindow -- no --> rate
    rate -- no --> eventLog
    rate -- yes --> nudge
    nudge --> db
    nudge --> context
    nudge --> llm
    nudge --> skip
    skip -- yes --> eventLog
    skip -- no --> send
    send --> telegram
    send --> state
    send --> eventLog
```

## Reports And Coaching

```mermaid
flowchart TD
    trigger{"What triggered it?"}
    weeklySchedule["Scheduled weekly report"]
    reviewCmd["Telegram /review current|last"]
    coachCmd["Telegram /coach current|last"]
    importData["Import latest data first"]
    prefs{"Report prefs allow send?"}
    already{"Already ran or skipped today?"}
    insights["Run insights LLM"]
    verify{"Verifier passes?"}
    sendReport["Send report to Telegram"]
    weeklyPath{"Scheduled weekly report path?"}
    memory["Append memory to history.md"]
    feedback["Attach feedback button"]
    coach["Run coach LLM after weekly report or /coach"]
    coachSkip{"Coach returns SKIP?"}
    bundle["Send narrative + per-edit buttons"]
    decision{"Accept / Reject / Diff"}
    apply["Apply accepted strategy.md edit"]
    reject["Record rejection in coach_feedback.md"]
    fail["Send failure notice with detail buttons"]
    db[(SQLite DB)]
    context[(Context files)]
    llm["LLM provider"]
    telegram["Telegram bot"]
    eventLog[(Events + LLM traces)]

    trigger --> weeklySchedule
    trigger --> reviewCmd
    trigger --> coachCmd
    weeklySchedule --> prefs
    reviewCmd --> importData
    prefs -- no --> eventLog
    prefs -- yes --> already
    already -- yes --> eventLog
    already -- no --> importData
    importData --> db
    importData --> insights
    insights --> db
    insights --> context
    insights --> llm
    insights --> verify
    verify -- no --> fail
    verify -- yes --> sendReport
    sendReport --> telegram
    sendReport --> feedback
    sendReport --> memory
    sendReport --> weeklyPath
    memory --> context
    weeklyPath -- yes --> coach
    weeklyPath -- no --> eventLog
    coachCmd --> coach
    coach --> db
    coach --> context
    coach --> llm
    coach --> coachSkip
    coachSkip -- yes --> eventLog
    coachSkip -- no --> bundle
    bundle --> telegram
    bundle --> decision
    decision -- Accept --> apply
    decision -- Reject --> reject
    decision -- Diff --> bundle
    apply --> context
    reject --> context
    fail --> telegram
    fail --> eventLog
```

## Telegram Chat And Context Edits

```mermaid
flowchart TD
    user([User])
    message["Plain Telegram message"]
    reply{"Reply to previous bot message?"}
    inject["Inject quoted report/nudge context"]
    agentMode{"Codex or Claude mode active?"}
    chat["Health chat LLM"]
    tools["Tools: run_sql, chart, update_context"]
    answer["Answer in Telegram"]
    chart{"Chart requested or emitted?"}
    sendChart["Render and send chart image"]
    editProposal{"Context edit proposed?"}
    proposal["Show proposed content + Accept / Reject / Diff"]
    decision{"User action"}
    apply["Apply accepted edit"]
    reject["Save rejection reason if provided"]
    feedback["Thumbs-down category + optional reason"]
    db[(SQLite DB)]
    context[(Context files)]
    llm["LLM provider"]
    telegram["Telegram bot"]
    eventLog[(LLM trace + feedback rows)]
    agent["Run local Codex or Claude CLI"]

    user --> message
    message --> reply
    reply -- yes --> inject
    inject --> chat
    reply -- no --> agentMode
    agentMode -- yes --> agent
    agent --> telegram
    agentMode -- no --> chat
    chat --> db
    chat --> context
    chat --> llm
    chat --> tools
    tools --> answer
    answer --> telegram
    answer --> chart
    chart -- yes --> sendChart
    sendChart --> telegram
    answer --> editProposal
    editProposal -- yes --> proposal
    editProposal -- no --> feedback
    proposal --> decision
    decision -- Accept --> apply
    decision -- Reject --> reject
    decision -- Diff --> proposal
    apply --> context
    reject --> context
    feedback --> eventLog
```

## Manual Add Flow

```mermaid
flowchart TD
    user([User])
    add["Telegram /add"]
    types["Load frequent workout types"]
    choose{"Choose workout or sleep"}
    workoutType["Pick workout type"]
    workoutDuration["Pick duration"]
    workoutDate["Pick date"]
    workoutFeel["Pick feel"]
    clone["LLM selects historical workout clone"]
    adjustWorkout["Deterministic feel adjustment"]
    confirmWorkout["Confirm workout"]
    sleepDate["Pick sleep date"]
    sleepDuration["Pick sleep duration"]
    sleepFeel["Pick sleep feel"]
    padSleep["Deterministic in-bed padding"]
    confirmSleep["Confirm sleep"]
    save{"Save?"}
    persist["Insert manual row"]
    undo{"Undo tapped?"}
    delete["Delete manual row"]
    db[(SQLite DB)]
    llm["LLM provider"]
    telegram["Telegram bot"]

    user --> add
    add --> types
    types --> choose
    choose -- Workout --> workoutType
    workoutType --> workoutDuration
    workoutDuration --> workoutDate
    workoutDate --> workoutFeel
    workoutFeel --> clone
    clone --> llm
    clone --> adjustWorkout
    adjustWorkout --> confirmWorkout
    choose -- Sleep --> sleepDate
    sleepDate --> sleepDuration
    sleepDuration --> sleepFeel
    sleepFeel --> padSleep
    padSleep --> confirmSleep
    confirmWorkout --> save
    confirmSleep --> save
    save -- yes --> persist
    save -- cancel --> telegram
    persist --> db
    persist --> telegram
    telegram --> undo
    undo -- yes --> delete
    undo -- no --> db
    delete --> db
```

## Controls, Diagnostics, And Agent Mode

```mermaid
flowchart TD
    telegram["Telegram bot"]
    notify["/notify request"]
    notifyLLM["Interpret preference request"]
    clarify{"Needs clarification?"}
    proposal["Show proposed notification changes"]
    notifyDecision{"Accept or reject?"}
    prefs[(notification_prefs.json)]
    models["/models"]
    modelPanel["Button model route panel"]
    modelDecision{"Set, reset, or doctor?"}
    modelPrefs[(model_prefs.json)]
    status["/status"]
    events["/events or /events usage"]
    llmLog["/llm_log"]
    contextCmd["/context"]
    tutorial["/tutorial /advanced"]
    diagnostics["Status, events, traces, context overview"]
    codex["/codex"]
    claude["/claude"]
    agentPanel["Agent panel: on, off, new session"]
    agentMode{"Plain-message agent mode active?"}
    agentRun["Run local CLI in repo workspace"]
    agentReply["Reply with result and session state"]
    db[(SQLite DB)]
    context[(Context files)]
    llm["LLM provider"]
    state[(Daemon state file)]

    telegram --> notify
    notify --> notifyLLM
    notifyLLM --> llm
    notifyLLM --> clarify
    clarify -- yes --> notifyLLM
    clarify -- no --> proposal
    proposal --> notifyDecision
    notifyDecision -- Accept --> prefs
    notifyDecision -- Reject --> telegram

    telegram --> models
    models --> modelPanel
    modelPanel --> modelDecision
    modelDecision -- Set or reset --> modelPrefs
    modelDecision -- Doctor --> diagnostics

    telegram --> status
    telegram --> events
    telegram --> llmLog
    telegram --> contextCmd
    telegram --> tutorial
    status --> diagnostics
    events --> diagnostics
    llmLog --> diagnostics
    contextCmd --> diagnostics
    diagnostics --> db
    diagnostics --> context

    telegram --> codex
    telegram --> claude
    codex --> agentPanel
    claude --> agentPanel
    agentPanel --> agentMode
    agentMode -- yes --> agentRun
    agentMode -- no --> telegram
    agentRun --> state
    agentRun --> agentReply
    agentReply --> telegram
```
