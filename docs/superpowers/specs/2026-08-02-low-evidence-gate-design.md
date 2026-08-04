# low_evidence gate for uncorroborated single-anchor matches — design

Date: 2026-08-02
Status: approved in conversation

## Problem

The RTL algorithm derives its correctness from chain verification: a match is
trusted because a second term independently confirms it through the parent
hierarchy. Two match types have no such confirmation — `single_term` (the
input reduced to one term) and `parent_resolved` (the rightmost term anchored,
nothing to its left verified). Both are nonetheless labeled `high` in
`CONFIDENCE_BY_TYPE` (line 2916), and both short-circuit on a single candidate
without any check on how that candidate was reached.

Measured on the 08-01 run, 5000 rows per sample, joined against the levels
file for origin:

| bucket | origin | n per 5k |
|---|---|---|
| single_term | preposition | 41 |
| single_term | spelling | 29 |
| single_term | mnt | 11 |
| single_term | transform | 7 |
| parent_resolved | mnt | 131 |
| parent_resolved | spelling | 14 |
| parent_resolved | transform | 6 |
| parent_resolved | preposition | 5 |

Two error families account for the wrong answers.

### Family A — false toponym from extraction

`extract_after_preposition` (line 1341) returns the substring after a spatial
preposition. When that substring is a common noun phrase it can still collide
with a real authority record:

```
Lutheran church in the village              -> The Village, Oklahoma [City]
Bant Main Street road two miles east of the city -> The City, East Suffolk [Settlement]
north east section of the city              -> The City, East Suffolk [Settlement]
on car floor near station                   -> Station, McLean, Kentucky [City]
626 Michigan Street, City                   -> City, Barton, Missouri [City]
335 State St., city                         -> City, Barton, Missouri [City]
```

Seven distinct input strings, one row each in the 5k baseline. The last two arrive by a
different route: the anchor term after the comma split is literally `City`,
and the MNT maps that bare term to two real authority records. The MNT is
harvested from prior normalizations, so a mapping that was right for one
record became a global rule that is wrong everywhere else.

### Family B — spelling correction that eats a jurisdiction descriptor

Fixed by `a48b26b` before implementation began, by a different and better
route than the one designed here. Retained for the analysis; see design
section 4 for what shipped instead and why the guard below is not being
built.

`query_spelling_corrections_local` (line 875) corrects at edit distance 1 and
accepts every authority record bearing the corrected name, without consulting
`Jurisdiction`. The input often declares its own type in a trailing
descriptor, and the correction overwrites that declaration:

```
Hill county      -> Hill Country    [City]      descriptor absorbed into the name
Hunt County      -> Hunt Country    [City]
Brazos County    -> Brazos Country  [City]
Bureau County    -> Burgau          [Town]      Germany
Sheboygan County -> Cheboygan       [City]      root corrected, descriptor dropped
Nicollet County  -> Nicolet         [City]      Quebec
```

Seven of the 43 unique spelling-origin low-evidence rows carry a trailing
jurisdiction token the candidate contradicts. Six are wrong. The seventh,
`BOYER TOWN R. D. 2 -> Boyertown [Borough]`, is correct, and its `TOWN` is
mid-string rather than trailing.

This is an inconsistency inside the codebase rather than a missing feature.
`query_fallback_transforms_local` (line 796) already applies the jurisdiction
filter; the spelling path is the one lookup that skips it.

### What the pipeline is missing architecturally

A toponym resolver has four stages: recognition (is this span a place mention
at all), candidate generation, context-aware reranking, and NIL prediction.
This pipeline implements candidate generation and a hierarchy-based form of
reranking. It has no recognition stage and no abstention, so every span that
collides with a gazetteer entry resolves. Family A is a recognition failure.

## Non-goals

- Prominence floor. Rejected on data: PA v77 has population on 25.6% of
  records overall and 25.3% at level 4, so a floor would read missing data as
  smallness and abstain on most correct city matches. Population may later
  serve as positive evidence when present, never as a negative when absent.
- Scored confidence model. Deferred until there is a labeled slice to fit a
  threshold against.
- Semantic drift via transform (`Manning River district -> New South Wales`).
  Roughly one of seven transform-origin rows, no clean signal yet.
- Changing `chain_verified` behavior. Those rows carry corroboration.
- The jurisdiction guard on the spelling path, described in design section 4
  and then superseded by `a48b26b`. See that section for why implementing it
  would now be a regression.

## Design

### 1. Span recording in `NameCache`

The gate needs the string that actually matched. For `Lutheran church in the
village` the name_cache key is the whole sentence and the resolving span
`the village` is discarded, so no test on the anchor can fire.

`NameCache` (line 197) already tracks per-uuid provenance through `origins`,
`record`, and `origin_of`. Add the parallel structure:

- `self.spans = {}`, keyed `(key, uuid)`
- `record_span(key, uuid, span)`, first writer wins, matching `record`
- `span_of(key, uuid)`, defaulting to `key`

Callers: the four phases that look up a rewritten form of the term —
`transform`, `preposition`, `spelling`, `cardinal_strip` — call `record_span`
at the point where they already update the cache. Every other phase looks up
the key itself, so the default is correct for them.

Recording rather than recomputing is load-bearing. Two attempts to reconstruct
the span post-hoc from the output files, during design, each produced a
different wrong answer and a different set of spurious rejections:

- Treating the anchor as the span gated 16 correct rows, because a
  transform-origin anchor keeps a lowercase leading function word
  (`near Despatch`, `south of Cole Camp`) that the transform strips before
  lookup.
- Taking the first preposition left-to-right gated 12 correct rows, because
  `extract_after_preposition` sorts its candidates shortest-first and returns
  the first that resolves. The real span for
  `Chapel of the Presbyterian Church in Bozeman` is `Bozeman`, not
  `the Presbyterian Church in Bozeman`.

Both classes are in the verification list as negative tests.

### 2. `is_description(span, original)`

```python
DETERMINERS = frozenset({'the', 'a', 'an'})

GENERIC_FEATURE_WORDS = frozenset({
    'station', 'church', 'cemetery', 'farm', 'home', 'residence', 'hospital',
    'river', 'creek', 'lake', 'island', 'beach', 'hill', 'valley', 'mountain',
    'park', 'area', 'place', 'community', 'camp', 'mission', 'school',
    'hotel', 'depot', 'mine', 'bridge', 'road', 'street', 'avenue',
    'junction', 'crossing', 'landing', 'corner', 'center', 'centre', 'grove',
})

APPELLATIVES = BARE_JURISDICTION_WORDS | GENERIC_FEATURE_WORDS
```

`BARE_JURISDICTION_WORDS` (line 1401) is derived from `JURISDICTION_SUFFIXES`
and `JURISDICTION_PREFIXES` and already covers county, township, city, town,
village, borough, parish, district, state, and province. Reusing it keeps
those words single-sourced.

Two tests, either of which rejects:

**List test.** Strip a leading determiner. Reject when exactly one word
remains and it is in `APPELLATIVES`. The single-word requirement is what
protects real names built from generic words: `Grove City` and `Lake Village`
have two appellatives and no determiner, so they pass.

**Case test.** When the original string carries informative case — it contains
letters, is not all uppercase, and is not all lowercase — reject a span whose
first character is not uppercase.

Measured contribution on the `a48b26b` pre-gate baseline, 5k rows: the list
test rejects 7 rows, the case test rejects 0 additional rows. Every row the case test would catch
is already caught by the list test — `on car floor near station` is entirely
lowercase so case is uninformative there, and `335 State St., city` trips both.

The case test is retained despite contributing nothing on this sample, because
it covers the list's known failure mode: a generic word nobody added. It is
also the component most sensitive to a wrong span, as the two reconstruction
failures in section 1 show. If span recording is ever suspect, this is the
test that misfires first, and it is the one to disable.

Survivors to hold in the test suite: `South Vineland`, `DaCosta`,
`Camden Place` (`camden` is not an appellative), `Wakarusa township`
(`wakarusa` is not an appellative), `Grove City`, `Lake Village`.

Accepted false-positive class: a mixed-case original in which the true place
name is written lowercase, such as `Route 2, lenoir`. Those rows are
uncorroborated by definition, and the candidate survives in the output.

### 3. Gate application

Two call sites, each already holding the anchor term:

- `match_entry` line 3022, the `len(right_to_left) == 1` branch, evaluated
  before the `len(ranked) == 1` short-circuit. Covers `single_term` and the
  `freq_resolved` produced by that branch.
- `resolve_parent_match` line 3675, before the `parent_resolved` /
  `freq_resolved` return. Covers those two.

The span comes from `name_cache.span_of(key, uuid)` for the top-ranked
candidate. `match_entry` already holds `name_cache`; `resolve_parent_match`
does not, and its signature must grow a `name_cache` parameter. It has one
production call site, at line 4020 inside `_run_phase3`, which has
`name_cache` in scope.

When candidates carry different spans, the test runs on the top-ranked
candidate's span only. A mixed-span candidate set means the anchor was reached
by two different rewrites, which is rare enough not to justify a policy yet.

Rejection returns:

```python
MatchResult(candidate_ids=[],
            tied_ids=cap_candidates(ranked_ids, "low_evidence"),
            match_type='low_evidence',
            depth=match.depth,
            steps=match.steps)
```

plus two table entries:

```python
CONFIDENCE_BY_TYPE['low_evidence'] = 'low'
NON_RESOLUTION_KIND['low_evidence'] = 'suspect'
```

No I/O change is required. `build_result_row` (line 3301) reads
`match.candidate_ids or match.tied_ids` for the candidate columns and blanks
`authority_name`, `type_ahead`, `jurisdiction`, `level`, and `authority_id`
whenever `resolution_kind != 'resolved'`. A gated row therefore emits no match
while the rejected candidate stays visible in `candidate_ids` and
`candidate_names`.

Known imprecision: `suspect` is documented at line 2934 as "a single candidate
won, but the walk dropped a more specific term", which is not what happened
here. Reusing it avoids updating every downstream consumer. A fourth kind,
`rejected`, is the cleaner alternative if consumers turn out to be few.

The gate applies regardless of origin, including `mnt`. A heuristic vetoing a
curated mapping is new behavior for this codebase, chosen because emitting a
known-wrong match on the authority of a bad dictionary row is the worse
outcome.

### 4. Jurisdiction guard — SUPERSEDED, not implemented

Commit `a48b26b` "fix: stop spelling corrections from deleting exact matches
in ranking" solved this family before implementation began, and solved it
better. It extracted `_prune_jurisdictions` and applies it separately to the
exact and correction groups, so a fuzzy City can no longer delete an exact
County. Because PA stores counties under the bare name (`Term=sheboygan`,
`LevelName=County`), the transform path already finds the exact County record
and now outranks the correction.

Measured on the six affected inputs, all now abstain instead of resolving
wrong: `Sheboygan County`, `Hill county`, `Hunt County`, `Brazos County`,
`Nicollet County`, and `Bureau County` all return `parent_amb` with their
candidates listed.

The guard below would have been a regression. Filtering corrections by
jurisdiction leaves `Sheboygan County` resolving to Cheboygan County,
**Michigan** — type-correct, wrong state — where the shipped code correctly
declines to answer. It is recorded here for the reasoning, not as work to do.

The original design, for reference: in `query_spelling_corrections_local`
(line 910) and its FileMaker twin `query_spelling_corrections` (line 2223),
filter candidate records by the anchor's declared type, mirroring line 798:

```python
hint = detect_jurisdiction_hint(key)
records = _LOCAL.pa_by_name.get(candidate, [])
if hint:
    records = [r for r in records if r['Jurisdiction'].lower() == hint.lower()]
```

`JURISDICTION_SUFFIXES` (line 1090) is already `$`-anchored, so
`detect_jurisdiction_hint` (line 1265) detects trailing descriptors only and
needs no change. `BOYER TOWN R. D. 2` yields no hint and the guard stays
inert.

Filter rather than reject, matching the transform path. Verified against PA
v77:

```
Bureau County    -> Burgau          no County-typed record   -> dropped
Nicollet County  -> Nicolet         no County-typed record   -> dropped
Hill county      -> Hill Country    [City]                   -> dropped
Hunt County      -> Hunt Country    [City]                   -> dropped
Brazos County    -> Brazos Country  [City]                   -> dropped
Sheboygan County -> Cheboygan       County-typed exists (MI) -> survives
```

Accepted residual: `Sheboygan County` (Wisconsin) lands on Cheboygan County,
Michigan. Type-correct, wrong state. A parent-only row carries no state
context capable of rejecting it.

This is the only change touching a path outside the two low-evidence buckets,
so it needs coverage asserting the 36 sampled spelling corrections without a
jurisdiction token are untouched.

### 5. MNT defect report

Terms gated while carrying origin `mnt` are dictionary defects and need
fixing at the source rather than suppressing on every future corpus.

Global file, not per run: `mnt_defects.tsv` at the anaconda-2 root,
overridable with `--mnt-defects`. Read on start when present, merge by
`(term, uuid)`, write back sorted by term then uuid. A `(term, uuid)` pair
appears once.

The key is the pair rather than the term because one bad term can carry
several mappings. `city` resolves to two authority records in the sample:

```
term  uuid    auth_name               jurisdiction  rows_last_run  last_input
city  <uuid>  City, Barton, Missouri  City          2              snowball2_sample_5k_01
city  <uuid>  London, ...             City          2              snowball2_sample_5k_01
```

`rows_last_run` is overwritten, not accumulated. The 01 and 02 samples
overlap, so a running total would inflate whenever the same data is rerun.

## Expected effect

Measured against a pre-gate baseline run on `a48b26b` — 5000 rows, of which
620 sit in the `single_term` / `parent_resolved` / `freq_resolved` buckets:

| change | rows |
|---|---|
| gated by appellative list | 7 |
| gated by case test | 0 |
| total affected | 7 |

```
single_term      preposition  Lutheran church in the village          The Village
single_term      preposition  Bant Main Street road two miles east…   The City
single_term      preposition  elev a miles northwest of the city      The City
single_term      preposition  north east section of the city          The City
single_term      preposition  on car floor near station               Station
parent_resolved  mnt          626 Michigan Street, City               City
parent_resolved  mnt          335 State St., city                     City
```

Five arrive through the `match_entry` gate, two through
`resolve_parent_match`. `chain_verified` is untouched, and no correct match in
the sample is lost. The earlier figure of 25 counted the superseded
jurisdiction guard and double-counted an overlapping 10k sample pair.

This is the acceptance target: a real rerun reproduces these 7 rows and no
others.

## Verification

- `is_description` truth table over every rejected row above and every survivor
  named in section 2, including the all-lowercase and all-uppercase
  case-uninformative paths.
- Negative tests from the two span-reconstruction failures in section 1, which
  must all resolve rather than gate: `near Despatch -> Dispatch`,
  `near Port De posit -> Port Deposit`, `near Rhineland er -> Rhinelander`,
  `south of Cole Camp -> Cole Camp`, `farm home, east central Kansas -> Kansas`,
  `Chapel of the Presbyterian Church in Bozeman -> Bozeman`,
  `home of his parents in DaCosta -> Dacosta`,
  `near the great log jam north of Camden Place -> Camden Place`,
  `residence of the brides parents in Wakarusa township -> Wakarusa`.
- `low_evidence` through `build_result_row`: `authority_*` blank,
  `candidate_ids` and `candidate_names` populated, `resolution_kind` set.
- Gating an `mnt`-origin anchor.
- Defect-file merge idempotency: two runs over the same input leave one row
  per `(term, uuid)`.
- Full 5k rerun diffed against the `a48b26b` pre-gate baseline, confirming the
  7 predicted rows changed and nothing else did. The `rtl-outputs/08-01/`
  files predate `a48b26b` and are not a valid comparison point: six county
  rows differ between them for reasons unrelated to this work.
