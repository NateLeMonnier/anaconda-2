# RTL Matcher: Level Preference for parent_only Results

**Date**: 2026-06-02
**Tag**: `RTL-LEVEL-PREF` (all changes wrapped in `# --- BEGIN RTL-LEVEL-PREF ---` / `# --- END RTL-LEVEL-PREF ---`)

## Problem

When `match_entry()` returns `parent_only` (depth == 1), the candidate set contains UUIDs at mixed levels (L4 cities, L5 counties, L6 states, L8 countries) with no preference logic. `rank_candidates()` scores by Type_Ahead_Value token overlap only, which provides no signal to distinguish Ohio (state) from Ohio, Peach, Georgia (city). Result: ~43% of parent_only matches are spurious.

## Solution

New function `resolve_parent_only()` called after `match_entry` returns `parent_only`. Implements Leafprint disambiguation rules. Separate from existing `rank_candidates` and `match_entry` core loop.

## Function: `resolve_parent_only(candidate_ids, auth_cache, fm)`

### Input
- `candidate_ids`: list of UUIDs (the `parent_only` candidate set)
- `auth_cache`: dict of UUID -> FM fieldData
- `fm`: FM connection (for fetching missing auth records if needed)

### Output
- `(winner_uuid, 'resolved')` — single winner found
- `(None, 'amb')` — genuinely ambiguous, no resolution possible

### Algorithm

1. **Fetch any missing auth records** for candidates not yet in auth_cache.

2. **Partition candidates**: `low` (level <= 4) vs `high` (level >= 5).

3. **If low candidates exist**, apply Leafprint L4 rules in order:
   - a. All low pops are zero/empty AND high candidates exist -> use high group
   - b. All low pops are zero/empty AND no high candidates -> Amb
   - c. Highest low pop >= 50,000 AND all other low pops are zero -> use that L4 entry
   - d. Highest low pop < 50,000 AND high candidates exist -> use high group
   - e. Highest low pop > 5x second-highest low pop -> use that L4 entry
   - f. Otherwise AND high candidates exist -> use high group
   - g. Otherwise AND no high candidates -> Amb

4. **Selecting from high group** (reached via 3a/3d/3f, or when no low candidates):
   - Single candidate -> return it
   - Multiple candidates -> apply same population disambiguation:
     - Highest pop >= 50,000 AND all others zero -> use it
     - Highest pop > 5x next -> use it
     - Multiple w/ pop >= 50,000 -> Amb
     - All zero -> Amb (no higher level to escalate to)

### Population Field

- FM field: `Population`
- Treat missing/empty/non-numeric as 0
- Already in auth_cache since it stores full fieldData

## Integration Point

In the main loop (around line 466), after `match_entry` returns:

```
if mtype == 'parent_only' and ids:
    result = resolve_parent_only(ids, auth_cache, fm)
    if result is Amb:
        # write amb marker
    else:
        # use resolved winner, update match_type to 'parent_resolved'
```

New match_type value `parent_resolved` distinguishes these from raw `parent_only` in output for tracking.

## Revert Strategy

All code wrapped in:
```python
# --- BEGIN RTL-LEVEL-PREF ---
...
# --- END RTL-LEVEL-PREF ---
```

To revert: delete everything between BEGIN/END markers (inclusive), remove callsite block in main loop.

## Out of Scope

- Abbreviation expansion (MO -> Missouri, etc.)
- inferred_place context (future enhancement)
- Changes to rank_candidates, match_entry core loop, walk_up_chain, or Phase 1/2 logic
