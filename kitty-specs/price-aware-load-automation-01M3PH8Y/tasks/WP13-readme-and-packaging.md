---
work_package_id: WP13
title: README and open-source packaging
dependencies:
- WP08
- WP10
- WP12
requirement_refs:
- FR-056
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T063
- T064
- T065
- T066
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: scribe-sally
agent: claude
authoritative_surface: README.md
create_intent:
- README.md
execution_mode: code_change
owned_files:
- README.md
role: documentarian
tags: []
tracker_refs: []
---

## ⚡ Do This First: Load Agent Profile

Before reading anything else in this file, load your assigned agent profile:

```
/ad-hoc-profile-load scribe-sally
```

This establishes your identity, governance scope, and boundaries for this work package. Do not
begin implementation until the profile is loaded.

## Objective

A README that lets someone else reproduce this setup — the minimum hardware, the SlimmeLezer
firmware configuration, the Home Assistant integrations, and the config file — without reading the
source.

Satisfies **FR-056**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP13 --agent <name>`. Depends on WP08, WP10 and WP12 —
  document what was actually built, not what was planned.

## Who this is for

Two audiences, and the document must serve both without a fork:

- **A friend with a similar setup in Flanders.** Digital meter, solar, a battery, Home Assistant.
  They want to know whether this applies to them and what to install. They will not read the code.
- **A stranger on the internet**, if this gets published. They may have a different grid operator, a
  different meter, no battery, or a three-phase connection where this assumes otherwise. They need
  to know quickly whether it fits — and the honest answer is often no.

Write for someone who is deciding whether to spend an evening on this. The most useful thing the
document can do is let them rule it out in two minutes if it will not work for them.

## The one rule

**Document what exists, not what is intended.** This mission ends with a planner that writes to a
log and commands nothing. A README implying otherwise would be actively harmful: someone could
install it expecting their battery to start behaving and conclude it is broken when nothing happens.

Say plainly, near the top, that it currently makes decisions and records them, and that the inverter
control layer is deliberately stubbed.

## Context you need

- `quickstart.md` — install and deploy steps; the README's install section derives from it
- `contracts/user-config.md` — the configuration schema
- `spec.md` Out of Scope — the honest limitations list
- WP08 T049 — the three meter entities; WP08 T048 — the quarter-hour semantics finding

---

### T063 — Write the hardware and prerequisites section

**Purpose**: Let a reader decide in two minutes whether this applies to them.

**Steps**:

1. Open with what the project does in three or four sentences, including that it **logs decisions
   rather than commanding hardware** in its current state.
2. List the minimum hardware, separating what is genuinely required from what is assumed:

   | Component | Required? | Notes |
   |---|---|---|
   | Belgian digital meter with P1 port | Yes | e-MUCS; the capacity-tariff features depend on it |
   | SlimmeLezer (or equivalent P1 reader) | Yes | Must expose the e-MUCS demand fields — see T064 |
   | Home Assistant | Yes | Container, OS or Supervised; Container needs no add-ons here |
   | HACS | Yes | For pyscript |
   | Solar array | Effectively yes | The forecast and solar-absorption logic assume one |
   | Home battery + hybrid inverter | Yes for value | The planner decides what a battery should do |
   | ENTSO-e API key | Yes | Free; needed for dynamic prices |

3. State the **geographic assumption plainly**: the capacity-tariff logic is specific to the Flemish
   capaciteitstarief. Someone in the Netherlands, Germany or Wallonia gets the price-arbitrage half
   and should disable the capacity half. Do not bury this.
4. State the **three-phase assumption** and why it matters: this installation nets across phases, and
   the code reads the meter's netted total deliberately. A single-phase reader is fine; someone
   summing per-phase sensors is not, and the README should say why.
5. Keep it scannable. Someone who cannot use this should discover that from the table, not from
   paragraph four.

**Files**: `README.md` (new)

**Validation**:
- A reader can tell within two minutes whether this fits their setup
- The log-only state is stated before any install instruction
- The Flanders-specific scope is unmissable

---

### T064 — Document the SlimmeLezer firmware configuration

**Purpose**: The capacity features depend on fields that are **off by default**. This is the step
most likely to be missed.

**Steps**:

1. Explain that stock SlimmeLezer firmware does not expose the e-MUCS demand registers, and that
   without them the capacity-tariff half of this project cannot work at all.
2. Give the ESPHome configuration verbatim:
   ```yaml
   - platform: dsmr
     active_energy_import_current_average_demand:
       name: "Huidig kwartiervermogen"
     active_energy_import_maximum_demand_running_month:
       name: "Maandpiek"
     active_energy_import_maximum_demand_last_13_months:
       name: "Gemiddelde maandpiek 13 maanden"
   ```
3. Map each to its e-MUCS field and its role, so a reader with differently-named entities can still
   identify them:

   | Field | Register | Role |
   |---|---|---|
   | Current average demand | `1-0:1.4.0` | Running quarter-hour average |
   | Maximum demand running month | `1-0:1.6.0` | The level the planner defends |
   | Average of last 13 months | — | What the grid fee is billed on |

4. Note that **reflashing is required** — this is not a Home Assistant setting — and link to the
   SlimmeLezer flashing documentation rather than reproducing it.
5. Explain the quarter-hour average's two possible meanings and that **the software detects which
   one applies automatically** — a reader does not need to test anything. Note the config field
   (`quarter_hour_average_mode`, default `auto`) and that the conclusion appears in the log, so a
   reader on different firmware can see what was decided and pin it if they prefer.
   Mention why it is detected rather than measured: a battery behind the meter masks household
   draw, so a manual test with a known load gives no clean reading.

**Files**: `README.md` (continues)

**Validation**:
- The YAML can be copied straight into an ESPHome config
- Each entity is mapped to its register and its purpose
- The need to reflash is stated, not implied

---

### T065 — Document the Home Assistant setup and configuration

**Purpose**: Everything between a working meter and a running planner.

**Steps**:

1. List the Home Assistant side in install order, each with why it is needed:
   - **HACS**, then **pyscript** — the host. Note the version that was tested.
   - **ENTSO-e integration** — dynamic prices; needs a free API key; note that day-ahead prices
     publish around 13:00 local.
   - The **`rest:` sensor** for forecast.solar, with the URL template and the parameter order
     `lat/lon/declination/azimuth/kwp` — and the azimuth convention, **0 = south, negative = east**,
     which is the single easiest thing to get backwards.
   - The **`pyscript:` block** with `allow_all_imports: true`, and why: the adapter parses YAML and
     JSON.
2. Show the file layout that must exist on the Home Assistant side, and which files are copied
   versus created once:

   ```
   <ha-config>/
     configuration.yaml          # edited
     pyscript/                   # copied, overwrite freely
     battery_planner/
       user_config.yaml          # created once from the example — never overwrite
       decisions.log             # generated
       cache/                    # generated
   ```

3. Walk through `user_config.yaml` section by section, calling out the two fields with **no sensible
   default**: battery capacity and the alert address. Reference `contracts/user-config.md` for the
   full schema rather than duplicating it — a copy would drift.
4. Explain how to confirm it is working: a record appears in `decisions.log` within five minutes, and
   `degraded=soc_stubbed` is **expected**, not a fault.
5. Include the troubleshooting table from `quickstart.md`, since its symptoms are the ones a new
   installer will actually hit.

**Files**: `README.md` (continues)

**Validation**:
- Someone can follow it end to end without opening another document, except for the config schema
- The azimuth convention is called out explicitly
- The expected `soc_stubbed` marker is explained before someone reports it as a bug

---

### T066 — Document limitations, licensing and contribution

**Purpose**: Be honest about what this does not do, and make the project safe to publish.

**Steps**:

1. State the limitations plainly, drawing on `spec.md` Out of Scope. At minimum:
   - **It commands nothing.** The inverter layer is stubbed; decisions are logged only.
   - **Battery charge is a stub.** No real state-of-charge reading.
   - Peak protection is **reactive**, not predictive — it defends a window already forming rather
     than holding charge back for a foreseeable evening peak.
   - Single solar plane; the free forecast tier supports no more.
   - Flanders-specific capacity logic.
   - No dashboard; the log is the only output.
2. Frame these as scope decisions with reasons, not apologies. A reader deciding whether to build on
   this needs to know which limitations are deliberate and which are merely not done yet.
3. Add a licence. **Ask the user which** rather than choosing one — it is their code and their
   decision. MIT and Apache-2.0 are the usual choices for something like this; if they have no
   preference, say so in the README rather than silently picking.
4. Add a short "if you want to adapt this" section pointing at the three seams most likely to need
   changing: the provider price coefficients, the capacity-tariff block for a different grid
   operator, and the inverter boundary for real control.
5. **Scrub the repository before publication.** Check that no API key, no email address, no
   coordinates more precise than the reader needs, and no `user_config.yaml` are committed. The
   `.gitignore` already covers `secrets.yaml` and the live config; verify rather than assume, and
   note in the README that a forker should do the same.

**Files**: `README.md` (continues)

**Validation**:
- Every limitation from Out of Scope that affects a user is listed
- The licence question is put to the user, not decided unilaterally
- A scrub for secrets and personal data has actually been run, not just recommended

---

## Definition of Done

- `README.md` lets a reader decide in two minutes whether this fits their setup
- The ESPHome firmware configuration is copy-pasteable and its registers explained
- The Home Assistant setup is complete enough to follow without reading the source
- The log-only state is stated before any install instruction
- Limitations are listed honestly, as decisions with reasons
- The licence has been chosen **by the user**
- The repository has been checked for secrets, keys and personal data

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **Implying the battery is controlled** | State log-only near the top; someone will otherwise install it and conclude it is broken |
| Omitting the firmware reflash step | The capacity half silently cannot work; T064 makes it prominent |
| Duplicating the config schema | Reference `contracts/user-config.md`; a copy drifts |
| Choosing a licence unilaterally | It is the user's code — ask |
| Publishing coordinates, an API key, or the live config | Scrub before publication; verify `.gitignore` rather than trusting it |
| Documenting the plan rather than the build | WP13 depends on WP08, WP10 and WP12 for exactly this reason |
| Azimuth convention left implicit | 0 = south, negative = east — the easiest thing to get backwards |

## Reviewer guidance

**Read it as someone who does not have this setup.** Can you tell within two minutes whether it
applies to you? If the Flanders-specific capacity logic or the three-phase assumption only becomes
clear halfway down, the document has wasted the reader's evening.

**Then check the honesty of the top section.** If a reader could finish the introduction still
believing their battery will start doing something, that is the most damaging possible error in this
document — worse than an omission, because it converts into a bug report against working software.

**Then verify the ESPHome block is copy-pasteable** and that the three entity names match what WP12
actually reads. A README that names one entity while the code reads another is worse than no README,
because it is believed.

Finally, confirm the secrets scrub was actually performed. "Should be checked" in a review is how an
API key reaches a public repository.
