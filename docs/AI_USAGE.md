# AI usage

Claude Code wrote much of the code, tests and documentation; Codex reviewed code and documents and implemented the reliability fixes described below. I set the scope, the product behaviour and the trade-offs, used the application after each round, and decided what changed.

## What I decided

- **Scenario and target:** one burn-in line of about 200 servers, 50 sensors each, 10,000 readings/s, derived from stated production and burn-in assumptions. Capacity was measured on an earlier revision ([Capacity](DESIGN.md#capacity)).
- **Domain rules:** fans alert when they fall *below* their limit, because a failing fan slows down; language and time zone are plant-wide settings, because stations on a line are shared.
- **Architecture:** one API process generates, broadcasts and stores the telemetry. I had its limit measured (twice the target) before considering a queue or more instances, and kept per-server readings in the WebSocket message only up to one line (200 servers), so the message size stays bounded.
- **Failure behaviour:** every connection that can break was broken on purpose and given a defined outcome.
- **One schema migration:** the project starts from an empty database, so I had a second migration I spotted folded into the initial one.

## What I changed after using the UI

- The live page did not visibly update, its window buttons were unexplained, and it showed *Live* while no data arrived: fixed and explained.
- Filters, paging and the save button were placed inconsistently or out of view: unified across pages.
- Import showed only 10 preview rows and unclear errors, and allowed one file: full preview, per-row errors, multi-file import. I asked how duplicate files were detected and had a second sample added to prove it.
- Pages implied things that were not true (a live label on batch-written history, a page-number box on cursor paging): reworded or removed.

## Where I overruled the AI

- During the earlier assignment, Claude initially used database-specific write forms (`upsert` and `DELETE ... LIMIT`). I required general ORM operations to meet its rule and had three writes rewritten: retention deletes old rows in primary-key ranges, and a retried batch reads existing rows and inserts only the missing ones. This gave me concrete query and retry behavior to review.
- I proactively asked AI to inspect **`EXPLAIN` query plans and measure query execution time**. Generated ORM queries needed evidence about index use, rows scanned and latency with a large table. The development record describes the resulting history-pagination and anomaly-filter changes in [Database access](DESIGN.md#database-access); those earlier measurements were not repeated during the documentation review.
- Idempotency keys were proposed as a known limit to save time; I had them built, since a lost import response would otherwise store every row twice.
- A review kept `PUT` for a partial settings update as too risky to change; I checked the scope (about ten lines, all tested) and had it changed to `PATCH`.
- I explicitly asked AI to add **mypy type checking**, with configurations for the backend and frontend, so annotations could be checked rather than accepted because generated code looked plausible. The commands and verification scope are in [TESTING.md](TESTING.md).
- A database health-check budget was set from arithmetic; I questioned it and asked for first-start measurements. On the Mac, MariaDB went from a new-volume container start to healthy in about 16 s; the whole source-download-to-admin-login run took about 2 min 47 s with existing base images and some build cache. Windows DB-only measurements were reported separately (over six minutes while busy, about 2 min 54 s after other containers stopped). These are not identical timing boundaries or proof of a Windows full-stack login test.

## How it was checked

The verified correction cycle passed 392 backend tests on real MariaDB 11.7 with 96% reported coverage, 108 frontend tests with seven Node-dependent tests skipped, Ruff, and backend mypy. The Mac application started with a new database volume and an admin login was checked in a real browser. The first Mac source was downloaded as a commit archive because local Git was blocked by an Xcode license prompt. Windows initialization figures were user-reported; its complete startup and three-account browser acceptance were not independently verified in this cycle. Commands and the manual acceptance checklist are in [TESTING.md](TESTING.md).

During the later document review, Codex also ran frontend mypy successfully on 19 source files. A first run used a read-only source mount without a writable cache and failed inside mypy; rerunning with an explicit writable cache completed. Tool failures are distinguished from application type errors rather than reported as a passing check.

After publication, I requested an independent agent to clone the new public repository and repeat acceptance in a separate Docker project with a new database volume. It tested commit `80e9920`; the subsequent commits changed only this document. A fresh BuildKit builder used `--pull --no-cache`, and its log showed Python layers and dependencies being downloaded. The build took about 87 s, builder startup about 25 s, and Compose startup to all services healthy about 40 s. These are separate intervals; the full clone-to-login duration was not precisely recorded. The database did not become unhealthy. The agent repeated the 392 backend tests (96% coverage), 108 frontend passes and seven Node-dependent skips, Ruff and backend mypy. In a real browser it logged out between successful admin, engineer and viewer logins, observed changing telemetry, searched and paged records, generated an Excel report to the download-ready state, and checked the engineer import and viewer read-only navigation. It did not download and inspect that browser-generated workbook. The tested application source matches the published source; this was a fresh project and application build, not a claim that the host had no pre-existing images.


## A review cycle, and what it caught

I had a separate agent review the code as a strict senior examiner, then required source evidence and reproducible probes before accepting a finding. Codex implemented four corrections and their regression tests: startup recovery could overwrite a new minute's statistics; a slow lease renewal could keep generation running past expiry; the write-buffer limit ignored in-flight items; and one Excel report could use different ranges on different sheets. The database and workbook probes used real MariaDB; the lease and buffer probes used controlled delays and a shortened TTL. These were service-level experiments, not a physical network partition. A claimed token-invalidation race was rejected because MariaDB returned a conflict instead of committing the stale update. The first AI fix for the lease put its expiry check in the wrong loop; lint and independent review caught it. My role was to set the review standard and authorize corrections; I do not present the AI's edits as code I wrote by hand.

## How I would handle a requirement change

Asked to ingest real BMC readings instead of simulated data, I would first pin down sensor identity, units, timestamp authority, ordering, sampling rate, authentication and the allowed loss or duplication. Then I would deliver in small, tested slices: one authenticated server persisted with replay tests, then thresholds and alerts, then many servers, late or duplicate packets, recovery and capacity.
