# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is a single self-hosting administrator operating an rclone-based backup and synchronization system in a Debian or Ubuntu LXC/VM, commonly under Proxmox. The user needs full operational and configuration access on desktop and mobile.

## Product Purpose

Sicherpfad makes scheduled and manual copy, sync, bisync, restore, verification, and Proxmox Backup Server workflows observable and safely operable from one web console. Success means the administrator can quickly understand whether data is protected, identify required action, and execute risky operations without bypassing safety checks.

## Positioning

The product combines rclone job execution with explicit preflight planning, dry runs, deletion limits, mount and sentinel checks, scheduling, runtime locks, job history, diagnostics, and recovery controls. It is an operations console rather than a generic rclone configuration editor.

## Operating Context

- The console is used for daily health checks, incident response, maintenance windows, pair setup, schedule changes, job inspection, and recovery.
- Automatic jobs run through systemd-backed scheduler processes while manual jobs can be started from the web UI.
- Users inspect live progress, logs, stale or failed runs, storage state, Proxmox guest resources, and upcoming schedules.
- Productive sync or bisync actions may delete data and therefore require visible safety conditions, planning, and deliberate confirmation.
- The interface is German and uses German date, time, and number formatting.

## Capabilities and Constraints

- Preserve authentication, CSRF protection, optimistic configuration revisions, server-side validation, runtime locks, and all existing safety guarantees. Backend routes and configuration may be migrated where required by the confirmed job model.
- Support dashboard health, sync-pair management, scheduler control, plan and dry-run flows, productive execution, cancellation, quick sync, job history and logs, diagnostics, maintenance, audit, settings, notifications, PBS targets, configuration snapshots, and restore.
- Desktop and mobile must both support the complete product; mobile may reorganize complex configuration into guided steps rather than removing capability.
- Safety-critical server validation remains authoritative. The frontend must not imply that visual confirmation replaces backend guards.

## Domain Model

- A data path defines what is connected: local folder, cloud folder, transfer mode, filters, and safety rules. It has no schedule.
- A job defines when and how one or more data paths run: schedule, ordering or parallelism, retry behavior, and enabled state.
- A run is an immutable execution record of one job and stores the effective configuration revision used for that execution.
- A data path may belong to multiple jobs. Overlapping paths must be detected and conflicting simultaneous execution must be blocked.
- Manual checks, dry runs, and individual starts remain available directly from a data path.

## Interface Architecture

- Primary navigation is `Lagebild`, `Datenwege`, `Jobs`, `Läufe`, and `System`.
- Main views remain deliberately slim: one primary action, short tables, and only information needed for the current task.
- Secondary detail opens in a side drawer; compact choices and row actions use popovers.
- Password confirmation, destructive actions, restore, and productive execution use focused modals with one primary decision.
- Complex creation flows use a full-screen guided assistant. On mobile, drawers and large dialogs become full-screen sheets.
- The dashboard copy table prioritizes local folder, cloud folder, file count, size, and protection status. Expensive size calculations are cached and show their measurement age.

## Evidence on Hand

- Product behavior and safety requirements are documented in `README.md`.
- Existing workflows and German interface copy are implemented in `app/static/index.html` and `app/static/app.js`.
- Backend route and state behavior is covered by the existing automated test suite.
- No customer claims, usage benchmarks, or external brand assets are currently established and must not be invented.

## Product Principles

1. Show protection state and required action before operational detail.
2. Make safe actions easy and risky actions explicit, staged, and reversible where possible.
3. Use progressive disclosure so occasional operators are guided without slowing expert workflows.
4. Keep system state, user intent, and execution feedback visibly distinct.
5. Preserve full capability across desktop and mobile through structural adaptation.

## Accessibility & Inclusion

Target WCAG 2.2 AA. All primary workflows must be keyboard operable, readable at 200% zoom, usable without color alone, compatible with reduced-motion preferences, and resilient to long German labels and status messages.
