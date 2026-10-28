# SAITULS — FUTURE GATE PRODUCT ROADMAP

Status: FUTURE_GATE / NOT ACTIVE
Date: 2026-10-07
Scope: post-normalization SAITULS product evolution
Activation rule: promote one gate into an active SAIPEN ticket only after the current product-normalization work and the preserved Secure Apps / Vault line reach their required acceptance state, unless the operator explicitly reprioritizes.

## 0. Purpose

Turn SAITULS into a coherent Windows utility/control plane rather than a loose collection of scripts and panels.

The long-term product should absorb the highest-value daily functions currently provided by separate utilities while keeping the SAI product rules already established elsewhere:

- obvious normal-user UX;
- minimal bureaucracy;
- fail-open where safe;
- truthful status and metrics;
- reversible system changes;
- no fake success;
- no hidden destructive behavior;
- low idle overhead;
- no unnecessary background processes;
- one shared settings language and interaction model;
- advanced internals hidden behind diagnostics;
- clean separation between product UI, policy, state, and platform adapters.

This roadmap is intentionally a FUTURE_GATE. It is not permission to start every item at once.

## 1. Mandatory sequencing guard

Before activating any gate below, preserve this order unless the operator explicitly changes priority:

1. Finish the active T-264 acceptance corridor.
2. Finish T-265 interactive console usability repair.
3. Finish the unified SAITULS product/metrics/settings/Home normalization wave.
4. Resume and complete the preserved Secure Apps / Vault hardening line, including its real hardware/live acceptance gates.
5. Only then promote the future gates below, one coherent slice at a time.

Do not let attractive shell toys starve the security-critical Vault line indefinitely. Shiny buttons are still shiny buttons even when they have gradients.

## 2. Shared architecture contract for every future module

Every future module must follow the same baseline contract.

### 2.1 Product integration

Each module should appear as a native SAITULS capability, not as a separately themed mini-application pasted into the window.

Required:

- shared navigation;
- shared settings semantics;
- shared localization;
- shared diagnostics conventions;
- shared update/build/deploy model;
- consistent status vocabulary;
- keyboard accessibility;
- DPI-safe layout;
- safe standalone degradation when another optional SAI component is absent.

### 2.2 Runtime behavior

Default expectations:

- zero busy polling in paint/UI handlers;
- bounded background refresh;
- cancellation on shutdown;
- no duplicate singleton workers;
- no unnecessary console flashes;
- no process-per-row/process-per-frame designs;
- idle means effectively idle;
- deterministic cleanup after crashes/restarts.

### 2.3 Mutation safety

Any module that changes Windows state must provide:

- capability detection before mutation;
- exact before-state capture;
- dry-run/preview where meaningful;
- idempotent apply;
- truthful verify-after-write;
- rollback from captured state;
- partial-failure journaling;
- crash-safe continuation or compensation;
- explicit unsupported state instead of guessing.

### 2.4 Third-party parity / clean-room rule

Several desired modules are inspired by existing proprietary utilities.

Target behavioral parity, not unauthorized source-code or asset copying.

Do not:

- decompile and transplant proprietary implementation code;
- redistribute proprietary icons/cursors/assets without a compatible license;
- copy vendor branding or attribution-removal hacks;
- depend at runtime on another product's private binary internals unless explicitly licensed and intentionally supported.

Allowed direction:

- clean-room reimplementation from observable behavior, public documentation and Windows APIs;
- optional import of the operator's own local settings/configuration where technically and legally appropriate;
- user-side migration of local assets without bundling them into SAITULS when redistribution rights are unclear.

## 3. Priority model

Use these bands when deciding what to promote next.

P0 FUTURE: daily reliability defects that directly interrupt normal workflows.

P1 FUTURE: high-frequency convenience features with strong time savings and moderate implementation risk.

P2 FUTURE: deeper platform utilities with significant Windows API/internal coupling.

P3 FUTURE: broad optimization/tweaking surfaces that require large safety and compatibility matrices.

Recommended promotion order after Vault:

1. SAIWINDOW reliability
2. SAIJUMP
3. SAITRAY
4. SAI Accounts Window Starter
5. SAISENSE
6. SAIMOUSE
7. SAIBAR
8. SAIOPT foundation
9. SAIOPT catalog expansion

The order may change if live evidence shows a higher-value blocker.

---

# FG-01 — SAIWINDOW: RELIABLE HOTKEY / MACROPAD WINDOW SUMMONING

Priority: P0 FUTURE
Risk: Medium
Value: Very high

## Goal

Make program invocation from macro pad / hotkeys deterministic.

Current user-visible failure classes to eliminate:

- ZAICODE hotkey minimizes the app but does not restore it;
- invocation creates an extra black console window;
- taskbar button flashes but the application never becomes usable;
- a stale/frozen visual snapshot appears while the real window remains inaccessible;
- a window is technically restored but not activated correctly;
- the wrong process/window is targeted when multiple related processes exist.

## Target behavior

One invoke action should mean:

- if app is not running: launch it;
- if app is running and hidden/minimized: restore and foreground it;
- if app is visible but behind other windows: foreground it;
- if the app owns multiple windows: select the canonical main window;
- if the canonical window is hung: report HUNG instead of pretending activation succeeded;
- if activation is blocked by Windows foreground rules: use the least-invasive valid recovery path and report the result truthfully;
- no bootstrap console flash.

Optional toggle behavior may support "show/hide" semantics, but it must never confuse minimize with summon.

## Architecture

Create one reusable native window activation service rather than separate hacks per application.

Candidate capabilities:

- process identity + window identity resolution;
- HWND liveness validation;
- visibility/minimized/cloaked checks;
- owner/tool-window filtering;
- foreground activation contract;
- restore from tray/minimize when supported;
- UWP/Win32 distinction where needed;
- per-app adapters only for genuinely app-specific behavior.

## Acceptance

Test at minimum:

- ZAICODE;
- FastPrompter;
- SAITULS;
- Discord;
- Telegram;
- AIMP or another tray-capable Win32 app;
- app not running;
- minimized;
- behind foreground window;
- tray-hidden;
- hung/unresponsive;
- multiple windows;
- rapid repeated hotkey presses.

No ghost snapshot, no duplicate launch, no black helper console, no stuck taskbar-only flash.

---

# FG-02 — SAIJUMP: FILE DIALOG PATH FOLLOWER

Priority: P1 FUTURE
Risk: Medium
Value: Very high

## Goal

Provide the high-value Listary-style behavior:

1. User navigates to a directory in Explorer or Total Commander.
2. User activates a standard Windows Open/Save dialog.
3. SAITULS can immediately jump that dialog to the most recently relevant Explorer/Total Commander path.

The feature exists to remove manual path typing and repetitive navigation.

## Required behavior

Support at least:

- standard Win32 Open dialog;
- standard Win32 Save dialog;
- modern common item dialog where accessible;
- Explorer as a source;
- Total Commander as a source;
- multiple Explorer windows;
- multiple Total Commander panels;
- recent-focus ordering;
- Unicode paths;
- network paths where Windows exposes them normally;
- drive roots;
- long paths where supported.

## UX

Possible activation modes:

- automatic on supported dialog activation;
- hotkey to jump the current dialog;
- small unobtrusive button/action;
- source preference: Explorer / Total Commander / most recent.

Default must avoid surprise navigation. Automatic mode should be explicitly toggleable.

## Safety

Never type a path blindly into an unknown control.

Detect supported dialog structures and refuse cleanly on unsupported dialogs.

## Acceptance

Real Windows matrix against:

- Notepad/Open;
- browser file picker;
- image/video editor file picker where standard dialogs are used;
- Explorer source;
- Total Commander left/right panel source;
- Unicode and spaced paths;
- path that disappears between capture and jump.

---

# FG-03 — SAITRAY: SYSTEM TRAY VISIBILITY / MINIMIZER CONTROL

Priority: P1 FUTURE
Risk: Medium-High
Value: High

## Goal

Build a native SAITULS tray-management capability inspired by Tray Factory.

Primary operator use case:

- see current tray applications/icons;
- choose which should remain visible;
- hide/show selected icons where Windows permits it;
- optionally minimize selected applications to a SAITULS-managed tray entry;
- persist user choices.

## Scope split

### Phase A — inventory and user-level control

- enumerate observable notification-area entries/applications;
- stable app identity;
- visibility preference;
- persisted order/choice;
- truthful unsupported state.

### Phase B — application minimizer

- user-selected apps can minimize-to-tray through SAITULS;
- restore reliably through the shared SAIWINDOW service;
- no process termination;
- no accidental taskbar disappearance without a recovery route.

### Phase C — shell-specific visibility integration

Only if Windows exposes a stable/supportable path for the target OS build.

Do not depend on fragile Explorer-memory patching as the default product contract.

## Recovery requirement

A user must always have a simple "Restore all managed windows/icons" escape hatch.

If Explorer restarts, SAITRAY must reconcile rather than lose state or duplicate entries.

---

# FG-04 — SAI ACCOUNTS WINDOW STARTER

Priority: P1 FUTURE
Risk: Medium
Value: Medium-High

## Goal

Provide an optional per-account/per-provider mechanism for keeping desired quota windows cycling instead of waiting for the user's first manual use after a reset.

The operator specifically wants 5-hour windows to be startable automatically and continuously when enabled.

## Hard safety rule

Do not silently burn quota or send hidden billable/model prompts merely to make a timer move.

First determine the provider-specific semantics:

- whether a window can be started without consuming meaningful quota;
- whether an official/local API action exists;
- whether a harmless status/session action starts the window;
- whether synthetic model activity would violate provider expectations or waste usage.

If safe activation cannot be proven, mark that provider/window UNSUPPORTED for automatic starting.

## Settings

Per account/window:

- Off by default;
- Enable auto-start;
- preferred start policy;
- optional quiet hours;
- minimum remaining threshold below which no activation occurs;
- next planned activation;
- last attempt/result;
- provider-safe explanation.

## Scheduling

Use one canonical scheduler/service rather than one task per account when practical.

Must survive:

- SAITULS restart;
- user logon;
- account reorder;
- account disable/freeze/hide;
- auth loss;
- provider outage.

Disabled/frozen accounts must never be auto-started.

## Acceptance

Prove with real provider state that activation changes the intended window state without a hidden prompt or unexplained quota burn.

---

# FG-05 — SAISENSE: HARDWARE SENSOR DASHBOARD

Priority: P1 FUTURE
Risk: Medium
Value: High

## Goal

Replace the temperature-display use case currently served by Argus Monitor with a lightweight SAITULS-native sensor layer.

Initial target is monitoring, not fan-control automation.

## Required sensors where available

- CPU package / CCD temperatures;
- GPU temperature / hotspot where exposed;
- SSD/NVMe temperatures;
- HDD temperatures;
- motherboard/chipset sensors where reliable;
- optional fan RPM readout if exposed safely.

## UX

User chooses which readings appear:

- Home compact metrics;
- tray tooltip/popup;
- dedicated Sensors page;
- optional alert thresholds.

Support friendly aliases such as CPU, GPU, NVMe K, HDD G instead of dumping raw hardware identifiers into normal UI.

## Architecture

Prefer a maintained sensor library or documented Windows/vendor interfaces with acceptable licensing.

Do not continuously invoke heavyweight external tools for every refresh.

Sensor acquisition must run on a bounded cadence with cached snapshots.

## Truth contract

Unavailable sensor != 0 C.

Stale data must be marked stale.

Sensor identity must survive reorder/enumeration changes as far as hardware identifiers permit.

## Later optional gate

Fan-control functionality, if ever desired, must be a separate future gate with much stronger safety requirements. It must not sneak into the monitoring ticket.

---

# FG-06 — SAIMOUSE: APPLICATION-SPECIFIC CURSOR OVERRIDES

Priority: P2 FUTURE
Risk: High
Value: Medium-High

## Goal

Build a clean-room YoloMouse-like capability inside SAITULS.

Primary behavior:

- replace/enhance cursor appearance in chosen applications;
- per-application profiles;
- hotkey/profile switching;
- size/color/style controls where technically possible;
- reliable restore to original behavior.

## Migration

Inspect the operator's existing local YoloMouse configuration only when this gate becomes active.

Migrate settings where the format can be safely interpreted.

Do not bundle or redistribute proprietary cursor assets unless their license permits it.

Where redistribution rights are unclear, support local import from the operator's installation instead.

## Technical investigation gate

Before implementation, prove the actual rendering/hooking requirements for:

- standard Win32 cursor paths;
- DirectX applications;
- raw-input games;
- hardware cursor cases;
- high-DPI scaling;
- anti-cheat-protected software.

## Safety

Never inject into protected/anti-cheat processes by default.

Maintain an explicit denylist/capability refusal path.

If safe support cannot be established for a process, SAIMOUSE must refuse rather than attempt increasingly invasive injection.

## Acceptance

- standard desktop app;
- DirectX test fixture;
- multi-monitor DPI;
- cursor restore after SAITULS crash;
- profile switching;
- no cursor leak into unrelated applications;
- no permanent system-cursor mutation unless explicitly requested.

---

# FG-07 — SAIBAR: TASKBAR BEHAVIOR CONTROL

Priority: P2 FUTURE
Risk: High
Value: High

## Goal

Provide a SAITULS-native taskbar behavior module inspired by the useful parts of 7+ Taskbar Tweaker.

Target Windows 10 22H2 first because it is the operator's primary system and because Windows taskbar internals differ substantially across releases.

Windows 11 support must be capability-gated, not assumed.

## Feature families to investigate

### Taskbar item actions

- right click behavior;
- middle click behavior;
- drop/open behavior;
- hover behavior;
- thumbnail behavior;
- pinned item behavior.

### Grouping and combining

- grouping policy;
- combining policy;
- labels;
- active-item behavior;
- decombine conditions where supportable.

### Mouse wheel / empty-space actions

- cycle taskbar buttons;
- minimize/restore selected windows;
- double-click empty space action;
- middle-click empty space action;
- optional volume behavior only if it does not collide with existing system controls.

### Other taskbar options

- show/hide selected shell buttons where supported;
- taskbar/notification-area hover behavior;
- spacing behavior where technically safe.

## Architecture gate

Before shipping any feature, classify it:

- documented API;
- stable shell message/automation contract;
- version-sensitive implementation;
- unsupported/unsafe.

Version-sensitive features require exact build detection and fail-closed behavior on unknown builds.

Do not patch Explorer memory casually and call it product engineering.

## Recovery

Explorer crash/restart must not strand the taskbar.

Provide one Safe Reset action restoring SAIBAR-managed behavior to captured defaults.

---

# FG-08 — SAIOPT FOUNDATION: REVERSIBLE WINDOWS OPTIMIZATION ENGINE

Priority: P3 FUTURE
Risk: Very high
Value: Very high if done correctly

## Goal

Create SAIOPT as the internal SAITULS optimization/tweaking subsystem inspired by the useful functional breadth of Win 10 Tweaker, while removing vendor-specific branding, unrelated links, decorative clutter and unsafe one-click mythology.

This must NOT begin as hundreds of checkboxes wired directly to registry commands.

First build the engine that makes those checkboxes safe.

## Foundation requirements

Every tweak definition must declare:

- stable id;
- category;
- supported Windows editions/builds;
- human title;
- concise explanation;
- risk level;
- admin requirement;
- current-state detector;
- desired-state mutation;
- verify function;
- rollback function;
- reboot/logoff/Explorer-restart requirement;
- conflicts/dependencies;
- privacy/security/performance classification;
- evidence source or rationale.

## Transaction model

Before applying a batch:

- detect all current states;
- show what will actually change;
- create a transactional journal;
- create the minimum relevant recovery data;
- apply in deterministic order;
- verify every mutation;
- roll back failed dependent operations where safe;
- preserve exact failure evidence.

No global "success" if one item silently failed.

## Restore model

Support:

- revert one tweak;
- revert selected batch;
- restore original captured state;
- export/import SAIOPT profile;
- explain when original state is no longer reconstructable.

System Restore may be offered as an additional safety layer where available, but it is not a substitute for exact per-tweak rollback.

## Clean-room rule

Use the screenshots/current external behavior only as feature inspiration and coverage inventory.

Do not copy proprietary Win 10 Tweaker implementation code or protected assets.

---

# FG-09 — SAIOPT CATALOG V1

Priority: P3 FUTURE
Needs: FG-08
Risk: High

After the reversible engine is proven, implement a bounded catalog in waves.

## Wave A — Privacy

Potential areas from the captured wishlist/UI:

- telemetry settings;
- activity/history collection;
- advertising/user-experience identifiers;
- feedback/diagnostic prompts;
- selected cloud/activity integrations.

Rules:

- distinguish supported policy from brittle service deletion;
- never label a privacy preference as a performance gain without evidence;
- preserve Windows Update/security functionality unless the user explicitly chooses otherwise.

## Wave B — Context Menu

Integrate with the existing SAITULS Explorer/registry capability instead of creating duplicate registry ownership.

Potential options:

- Take ownership;
- Create shortcuts/symbolic links where safe;
- copy path/hash utilities;
- open with Notepad/terminal helpers;
- show/hide selected shell entries;
- remove known junk context entries through explicit selection.

All mutations must be reversible and locale-aware.

## Wave C — Interface

Potential options:

- Explorer presentation settings;
- shortcut-arrow behavior;
- selected shell visual behavior;
- cursor-related presentation only where it does not conflict with SAIMOUSE;
- desktop/system-icon preferences;
- optional UI animation toggles.

Do not duplicate SAIBAR ownership.

## Wave D — System

Potential options:

- selected notifications;
- SmartScreen/user-choice settings only with strong security warnings;
- memory diagnostics behavior;
- removable-media autorun;
- selected legacy shell behavior;
- OneDrive integration preferences;
- cache-related settings where evidence supports them.

Security-sensitive controls must default conservative and must not be presented as universally recommended optimizations.

## Wave E — Services

The captured inspiration exposes a large "I do not use X" service-disabling surface.

SAIOPT must implement this far more safely.

Do not disable services by a giant static list.

Use capability bundles such as:

- no Xbox features;
- no Bluetooth;
- no printing;
- no Hyper-V;
- no Windows biometric features;
- no offline maps;
- no consumer gaming capture;
- no wireless networking;
- no smart-card functionality;
- no Windows Media sharing.

Each bundle must:

- detect whether the capability is actually present/in use;
- enumerate exact services/tasks/features affected;
- show impact before apply;
- preserve dependencies;
- support rollback;
- refuse if the system configuration contradicts the user's selected assumption.

A service count such as "130/130 disabled" is never a success metric by itself.

## Wave F — Cleanup

Potential cleanup targets:

- temp caches;
- browser caches where explicitly selected;
- shader caches;
- Windows Update leftovers;
- logs;
- obsolete driver packages only through supported servicing APIs;
- empty/dead directories;
- selected application caches.

Rules:

- preview bytes/items first;
- never delete user documents;
- no recursive guess-based deletion;
- use supported Windows cleanup/servicing interfaces where possible;
- distinguish safe cache cleanup from risky registry "cleaning".

Registry cleaners should not be included as a default optimization feature.

## Wave G — Applications / Optional Windows Apps

Provide inventory and controlled removal/restoration where Windows supports it.

Never assume an app is useless because a third-party tweaker drew a tombstone over its icon.

Support:

- current installed/provisioned state;
- remove for user / provisioned package distinction;
- restoration path where possible;
- consequences for dependent features.

## Wave H — Startup

Build on existing Windows startup inventory rather than a second implementation.

Support:

- startup entries;
- scheduled startup tasks where appropriate;
- enable/disable;
- source/path display;
- verified rollback.

Do not silently delete startup entries.

## Wave I — Optimization / Storage

Potential advanced operations:

- component-store cleanup;
- obsolete update cleanup;
- compression options;
- duplicate-file discovery;
- duplicate replacement with links only as a separate explicit operation;
- old-driver cleanup through supported tools;
- storage analysis.

Any operation that can make boot/recovery harder must be separately confirmed and strongly evidenced.

## Wave J — Security / Malware Scan Integration

Do not invent another antivirus engine.

Possible scope:

- Windows Defender scan shortcuts/status;
- hash/reputation helper integrations;
- optional VirusTotal-style file hash workflow only with clear privacy disclosure and explicit upload consent if content upload is involved.

Never upload files automatically.

## Wave K — System Information

Native read-only inventory:

- OS/build;
- motherboard;
- CPU;
- GPU;
- monitors;
- RAM;
- storage;
- USB device summary;
- uptime;
- network summary;
- selected copy buttons;
- optional export to diagnostics bundle.

Reuse SAISENSE hardware identity where possible.

## Wave L — Personal Recommendations

Generate recommendations from actual detected system state, not generic fear-based tweaking.

Each recommendation must show:

- why it applies;
- expected effect;
- confidence/evidence level;
- risk;
- exact change;
- Apply;
- Revert.

Examples may include:

- delayed startup for suitable third-party apps;
- freeing hibernation space when the operator explicitly does not use hibernation;
- reducing unnecessary startup/background software;
- disabling an unused capability only after confirming it is unused.

A recommendation engine must never silently mutate the system.

---

# FG-10 — SAIOPT PROFILE / BACKUP / PORTABILITY

Priority: P3 FUTURE
Needs: FG-08, partial FG-09

## Goal

Make system tuning reproducible and recoverable across reinstall/rebuild without turning SAITULS into an image-management monster.

Support:

- export selected tweak profile;
- import with compatibility preview;
- original-state backup metadata;
- optional driver inventory/export through supported Windows tooling;
- extension/file-association export where reliable;
- clear distinction between "desired profile" and "backup of this exact machine".

Never apply a foreign-machine backup blindly.

Profile import must re-detect current state and compatibility first.

---

# FG-11 — UNIFIED FUTURE MODULE HOME / STATUS INTEGRATION

Priority: P2 FUTURE
Needs: at least two implemented future modules

## Goal

Keep future growth from turning SAITULS Home into a cockpit from a submarine designed by twelve committees.

Home should surface only actionable summaries.

Examples:

- Sensors: CPU 62 C / GPU 51 C, only if selected;
- SAI Accounts: important quota/reset summary;
- SAITRAY: active, 4 managed apps;
- SAIWINDOW: healthy, no hung targets;
- SAIOPT: 3 recommendations / 1 pending reboot;
- Vault: protected/locked/unlocked state;
- Explorer integration: ready/repair needed.

No giant raw telemetry wall.

Each card/row opens the owning module.

---

# 4. Shared settings model

Future modules must not create a forest of unrelated config formats.

Prefer one versioned SAITULS settings authority with module namespaces and migrations.

Example conceptual namespaces:

- general.*
- accounts.*
- metrics.*
- window.*
- jump.*
- tray.*
- sensors.*
- mouse.*
- taskbar.*
- saiopt.*
- vault.*

Requirements:

- schema version;
- migration tests;
- atomic writes;
- corrupt-config recovery;
- no secret material in ordinary settings;
- portable export excludes secrets by default;
- unknown future keys survive older readers where practical.

# 5. Shared diagnostics model

All future modules should feed one bounded diagnostic/event stream.

Normal UI shows human summaries.

Advanced view may include:

- timestamp;
- module;
- operation;
- state/result;
- error category;
- source path/API/build;
- correlation id.

No credentials, OAuth tokens, recovery keys or sensitive raw payloads in logs.

# 6. Shared performance budget

Every promoted gate must record before/after idle behavior.

Track at minimum:

- SAITULS idle CPU;
- working set;
- handle count;
- thread count;
- child-process launches/minute;
- tray-hidden behavior;
- startup time to usable UI;
- shutdown latency.

New module acceptance requires no unexplained idle regression.

A feature that saves two clicks but adds a permanent polling furnace is not an improvement.

# 7. Shared compatibility matrix

Primary platform:

- Windows 10 Pro x64 22H2 / build 19045.

For version-sensitive shell features, test exact supported builds.

Windows 11 support should be capability-detected and added deliberately.

Never assume a shell-internal technique valid on Windows 10 remains valid on Windows 11.

# 8. Promotion protocol

When the operator chooses a future gate:

1. Re-open the latest SAITULS archive/repository state.
2. Check whether the desired capability has already partially appeared or moved.
3. Inspect relevant current implementations and shared SAI components.
4. Convert only that gate into a concrete implementation ticket/handoff.
5. Name exact repository files/symbols once discovered.
6. Establish red-before-green evidence where practical.
7. Implement the narrowest complete vertical slice.
8. Run live Windows acceptance.
9. Record evidence.
10. Only then promote the next gate.

Do not dump this entire roadmap into the active board as dozens of immediate tickets.

# 9. Current wishlist coverage map

This roadmap captures the current deferred wishes as follows:

- automatic 5-hour quota-window starting -> FG-04 SAI Accounts Window Starter;
- Argus Monitor temperature-display replacement -> FG-05 SAISENSE;
- YoloMouse replacement + settings migration -> FG-06 SAIMOUSE;
- 7+ Taskbar Tweaker replacement -> FG-07 SAIBAR;
- Listary Open/Save path jump -> FG-02 SAIJUMP;
- Tray Factory replacement -> FG-03 SAITRAY;
- macro-pad summon/minimize/black-window/frozen-window failures -> FG-01 SAIWINDOW;
- Win 10 Tweaker-inspired integrated optimization center -> FG-08/FG-09/FG-10 SAIOPT;
- shared mature product surface -> FG-11 + shared architecture contracts.

# 10. Definition of roadmap success

The roadmap is successful if future SAITULS evolves into one coherent Windows control utility without accumulating the exact class of problems it is intended to remove.

That means:

- fewer separate resident utilities;
- fewer manual paths/clicks;
- fewer black consoles and broken summons;
- truthful hardware/account state;
- reversible Windows changes;
- safe recovery;
- bounded background cost;
- one understandable interface;
- no duplicated state authorities;
- no proprietary-code cargo culting;
- no feature is considered complete until its real Windows behavior is demonstrated.

END OF FUTURE_GATE ROADMAP
