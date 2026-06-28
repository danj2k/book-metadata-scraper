# Agent Instructions

## Project documentation and knowledge management

When working in this repository, maintain both:

1. the source code, and
2. the project's documentation and accumulated knowledge.

Documentation is a first-class part of this project. It exists to preserve:
- goals and intent
- design reasoning
- architectural decisions
- domain knowledge
- implementation constraints
- discoveries made during development

Code explains what the system does.
Documentation explains why it exists and how to reason about it.

## Before making changes

Before modifying code:

1. Inspect the existing documentation.
2. Understand the current architecture and conventions.
3. Identify whether the planned change affects:
   - behaviour
   - architecture
   - APIs/interfaces
   - assumptions
   - project goals
   - design decisions

Update documentation when necessary before or alongside code changes.

## Documentation structure

Prefer maintaining a `docs/` directory containing documents such as:

- `PROJECT.md`
  - purpose
  - goals
  - non-goals
  - constraints

- `ARCHITECTURE.md`
  - system overview
  - components
  - responsibilities
  - data flow
  - dependencies

- `DESIGN_DECISIONS.md`
  - important decisions
  - alternatives considered
  - trade-offs
  - consequences

- `IMPLEMENTATION_NOTES.md`
  - non-obvious implementation details
  - algorithms
  - invariants
  - edge cases
  - tricky areas

- `DEVELOPMENT_LOG.md`
  - significant changes
  - discoveries
  - problems solved

- `KNOWN_ISSUES.md`
  - limitations
  - technical debt
  - future improvements

Adapt this structure to the project. Do not create documents unnecessarily.

## While working

Record useful knowledge discovered during development:

- implicit rules
- surprising behaviour
- important assumptions
- patterns worth preserving
- approaches that failed and why
- decisions that future developers should understand

Prefer durable explanations over temporary notes.

Avoid documentation that simply repeats the code.

Prefer explanations like:

"This exists because..."

"This approach was chosen instead of X because..."

"The system assumes..."

"Changing this requires updating..."

## After completing work

After meaningful changes:

1. Review whether documentation still matches the implementation.
2. Update affected documents.
3. Add any new information that would help another engineer or future agent.

Keep documentation accurate and concise.

## Documentation discipline

The goal is a useful project memory, not a duplicate copy of the code.

Prefer:
- updating existing documents
- consolidating related information
- recording reasoning and constraints

Avoid:
- documenting trivial code details
- creating stale documents
- writing large amounts of low-value text

