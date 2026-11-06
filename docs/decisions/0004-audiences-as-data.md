# 4. Audiences as data, resolved by a router

## Context

The world cannot know sessions, yet each event must reach the right players (spec I App. B).

## Decision

An event carries an audience value (`ToPlayer`, `ToAll`, `ToPlayers`, `ToAllExcept`, `ToTeam`). The routing module turns it into sessions.

## Consequences

- The audience table of the spec becomes one small module.
- The world stays free of connection types.
- Delivery rules are testable on their own.
