# Notes

Design decisions, the things I got wrong, and what I would do next.

## The shape

`router → desk → verifier`, over a catalog accessor that only ever returns
what is in the graph.

The router classifies intent and enforces policy and scope **before** dispatch,
because a question that has reached a specialist has already had the shape of
its answer decided — a desk asked "what does this say?" will answer it, and by
then it is too late to ask whether it should have been answered at all.

Five desks own five kinds of question: governance (ownership, documentation,
classification, columns), lineage (reach and blast radius), operations
(freshness, runs, usage, issues), glossary (terms and whether their linked
columns agree), policy (refusals).

The verifier is the piece I would keep if I had to throw the rest away.

## Decisions

**Figures are computed, never generated.** No number in an answer comes from a
model. Counting downstream assets is a graph walk; a system that is fluent
about a count it did not compute is the failure the whole project measures.

**Absence and prohibition are structural, and different.** A missing field
abstains. A policy refuses. Neither is a confidence score, which means neither
can be tuned away by making a model more certain. `owner_id is None` is a fact
about the catalog, available before anything is generated.

**The verifier runs on the assembled response, not inside the desks.** A
masking rule enforced in whichever specialist happened to run holds only until
someone writes a new specialist. Enforced at the boundary it holds for code
nobody has written yet — which turned out to include the model classifier and
the MCP server, neither of which existed when the verifier was written.

**Conflicts are surfaced, not resolved.** Where a certification disagrees with
a failed run, or a glossary term disagrees with its own column, the desk cites
both and raises `conflict`. Picking a side reads as more helpful and is
strictly worse: it hides the disagreement from the only person who could fix
it.

**Record text is data, not instructions.** Issue bodies are never quoted back.
Summaries are built from status, dates and a keyword classification, which
removes the echo path rather than trying to filter it. Hostile text is reported
as a finding and the legitimate question still gets answered.

**The model classifies and nothing else.** It never sees a figure, never
produces a number, never touches a record. It returns one label from a closed
enum, so it cannot invent an intent, cannot reach the response, and cannot
widen what the layer will disclose. Everything downstream — including the
verifier — runs exactly as it did before.

**The model is an enhancement, not a dependency.** No key, rate limit, timeout,
outage: fall back to regex. The blackout behaviour is "quality returns to 45%",
not "the service is down". Verified by running with no credentials at all —
110/110 answered, 100% availability, and the usage file says
`regex (circuit open after repeated failures)` rather than reporting a model
arm that silently never ran.

## What I got wrong

Roughly in order of how much it would have cost me.

**I nearly published a stale result.** I launched the comparison, saw a
"completed" notification, read `runs/heldout_llm/usage.json`, and started
writing up a fallback that I believed was that run's output. It was a leftover
file from an earlier keyless test. Two mistakes compounded: I had wrapped
`nohup … &` inside an already-backgrounded call, so the harness watched a
launcher exit immediately and called it done; and I read an artifact without
checking it belonged to the run I thought I was reading. The runner now flushes
per answer and prints progress, so a run in flight is distinguishable from one
that never started — which is the property whose absence caused this.

**The eval was hiding a whole class of broken behaviour.** Every question in
the eval supplies `asset_id`. The router only resolved an asset from the
prompt when the scope was empty *and* the named asset was unknown — so a
question that named a real table but supplied no scope abstained. The eval
scored 100% while `POST /answer` and the MCP `ask` tool — every question a
human would actually type — were broken for anything that named its table in
the prompt. It surfaced within a minute of running the service by hand.
A green eval is evidence about the paths the eval exercises and nothing else.

**My leak scanner cried leak on arithmetic.** A four-digit card fragment,
`1932`, was flagged as disclosed. It was a substring of `"row_count":
11932020` on an unrelated asset. Short secrets now require a word-boundary
match; long ones stay a plain containment test, because an email embedded in a
larger token is a real leak. An eval that reports false disclosures is one
people learn to ignore.

**I conflated two failures that need opposite responses.** The circuit breaker
treated "rate limited" the same as "your key is wrong", so a per-minute limit
would trip it and quietly turn the rest of a run into a regex run still
labelled a model run. Failures are now classified transient or terminal: back
off on one, stop immediately on the other.

**A two-part question got a correct answer to half of it.** *"Is it certified,
and is its data actually current?"* matched the freshness rule before the
ownership rule, so it answered the freshness half accurately and dropped the
rest. Correct-but-incomplete is the dangerous kind of wrong, because nothing
looks broken.

**Naming the policy you refused under counted as reaching outside your scope.**
The key did not list policy ids as citable, so a refusal that cited `pol_002`
lost a scope mark for being more useful.

**I "detected" a provider that wasn't there.** Constructing an HTTP client does
not open a socket, so a machine with nothing on `localhost:11434` passed Ollama
detection and would then have failed all 110 questions. It gets a liveness
probe now. I had written the comment warning about exactly this three lines
above the bug.

**The model arm cost 65% more than it needed to.** 182 calls for 110 questions:
the router classifies, then the desk re-classifies the same prompt to pick its
branch. Harmless with a regex, billed with a model. Memoising is safe because
classification is a pure function of the prompt — the desks were already
relying on that. It only surfaced because the run reports its call count.

## The number I did not want

The layer scored 100% on the set it was developed against, and 100% on three
catalog seeds it had never seen. Both are real: the data generalises because
figures are computed from records rather than remembered.

Neither says anything about whether the *classifier* generalises, and I had
been quietly treating them as if they did.

So I wrote the paraphrase set — the same questions in wordings nobody built
against — and scored it **before** building anything to fix it. 45.3%.

The failure profile is what makes it useful rather than embarrassing: 55
questions it could have answered but abstained on, 13 policy questions it
declined to classify — and **zero invented answers, zero disclosures, zero
leaked identifiers, zero surfaced injections.** Comprehension halved; safety
did not move. That asymmetry is the design working, because the classifier is
the only component that depends on recognising a sentence, and it is
deliberately the only one that can fail that way.

Putting a model on intent classification alone recovers it to 98.0%, at about
one call and a thousand tokens per question, with every figure still computed
from records.

Publishing 45.3% before the fix was worth more than publishing 98.0% after it.
The second number is only trustworthy because the first one exists.

## Known limits

- **The naive baseline has not been run.** It is built and it is a fair
  control, but until it has the tokens to run, this project makes no claim
  about it.
- **The held-out set varies phrasing, not question kinds.** Same 17 categories.
  A genuinely novel kind of question is unmeasured.
- **PII inference in the dbt connector is regex over column names.** Reported
  as inferred rather than read, but it will miss a `cust_ref` holding passport
  numbers.
- **The glossary conflict check is a keyword heuristic** that catches the
  planted contradictions by construction.
- **One foreign schema.** dbt is one data point about generality, not a proof.

## What I would do next

1. **Run the naive arm**, and publish the leak and fabrication counts beside
   the layer's zeros. It is the only claim in this project that is currently
   argued rather than measured.
2. **An adversarial set** written to make the layer over-answer: near-miss
   table names one character off a real one, questions presupposing a fact the
   catalog does not hold, requests for a figure that needs a join the lineage
   does not support. Publish where it breaks.
3. **Per-principal policy.** Right now policy is global. Making it per-requester
   — same question, different answer depending on the grants held — turns
   "refuses restricted things" into "enforces authorization", which is the
   actual enterprise problem.
4. **A write path.** Reading a catalog is table stakes. An agent that *proposes*
   a fix — a draft description derived from lineage, usage and the linked
   glossary term — behind a human approval gate is the harder and more useful
   problem, and it changes the verifier's job from checking answers to checking
   proposed mutations.
