"""Router, five specialist desks, and the roster they report.

The shape is `router → desk(s) → verifier`. The router classifies intent and
enforces policy *before* any desk runs, because a question that has reached a
specialist has already had the shape of its answer decided: a desk asked "what
does this say?" will answer it, and by then it is too late to ask whether it
should have been answered at all.

Two rules run the whole design.

**Figures are computed, never generated.** No number in an answer comes from a
model. Counting downstream assets is a graph walk, not a language task, and a
system that is fluent about a count it did not compute is the exact failure
this project measures. The model, where it is used at all, decides *what was
asked* and writes prose around records the code already selected.

**Absence and prohibition are different, and both are structural.** A missing
field is an abstention. A policy is a refusal. Neither is a confidence score,
so neither can be tuned by making the model more certain. `owner_id is None`
is a fact about the catalog, and the honest answer to a question about it is
available before anything is generated.

Conflicts are surfaced, not resolved. Where two records disagree — a
certification against a failed run, a glossary term against the column it is
linked to — the desk cites both and raises `conflict`. Picking a side reads as
more helpful and is strictly worse: it hides that the disagreement exists from
the only person who could fix it.
"""
from __future__ import annotations

import re

from contextlayer.verify import Verifier, blank

ROSTER = [
    {"role": "router", "name": "intake",
     "owns": "Classifies intent and enforces scope and policy before dispatch. "
             "In the path on every answer."},
    {"role": "governance", "name": "governance_desk",
     "owns": "Ownership, documentation, classification and policy tags. "
             "Column-level meaning."},
    {"role": "lineage", "name": "lineage_desk",
     "owns": "Upstream and downstream reach, blast radius, dependency paths."},
    {"role": "operations", "name": "operations_desk",
     "owns": "Freshness, pipeline runs, usage and issue threads. Owns the "
             "certified-versus-actual conflict."},
    {"role": "glossary", "name": "glossary_desk",
     "owns": "Business term definitions and their agreement with the columns "
             "they are linked to."},
    {"role": "policy", "name": "policy_desk",
     "owns": "Refusals: restricted detail, personal data, advice, and "
             "questions that reach outside their scope."},
    {"role": "verifier", "name": "verifier",
     "owns": "Checks every draft against its citations, enforces masking, and "
             "downgrades ungrounded claims before anything ships."},
]

# ---------------------------------------------------------------------------
# Intent classification. Regex for now, and honestly the weakest part of the
# system: it generalises across phrasings it has seen and falls through on ones
# it has not. Falling through abstains rather than guesses, which loses marks
# in the right direction. `contextlayer/llm.py` replaces this with a cheap
# model call for intent only — the figures stay computed either way.
# ---------------------------------------------------------------------------

RULES: list[tuple[str, re.Pattern]] = [
    # Policy first. These decide the shape of the answer, not its content.
    ("advice", re.compile(
        r"\bshould(?!\s+know)\b|\bworth keeping\b|\brecommend|\bconsolidate\b"
        r"|\brewrite\b|\bbetter\b.*\bdefinition\b", re.I)),
    ("restricted_detail", re.compile(
        r"\b(dump|list every column|sample values?|what the data.*looks? like"
        r"|show me what)\b", re.I)),

    # Two-part questions, before either half can claim them. "Is it certified,
    # and is its data actually current?" reaches for governance and operations
    # at once, and whichever single rule matched first would have answered
    # half of it — correctly, which is the dangerous part.
    ("multi", re.compile(
        r"(\bwho owns\b|\bowner\b|\bcertified\b|\bcertification\b)"
        r".{0,80}(\bcurrent\b|\bon time\b|\bhealth\b|\bfresh\b|\bland(ed|ing)?\b)"
        r"|(\bcurrent\b|\bon time\b|\bhealth\b|\bfresh\b)"
        r".{0,80}(\bwho owns\b|\bowner\b|\bcertified\b)", re.I)),

    ("lineage_path", re.compile(
        r"\bfeed(s)? into\b|\bupstream of\b|\bdepend(s)? on\b", re.I)),
    ("lineage_count", re.compile(
        r"\bhow many assets\b|\bblast radius\b|\bdownstream\b|\baffected "
        r"downstream\b", re.I)),
    ("trust", re.compile(
        r"\bsafe to\b|\bcan i trust\b|\breliable\b|\bcurrent state\b", re.I)),
    ("usage", re.compile(
        r"\bstill (querying|using)\b|\bmoved off\b|\banyone still\b", re.I)),
    ("freshness", re.compile(
        r"\bfresh\b|\bsla\b|\blast land|\blanded\b|\bup to date\b", re.I)),
    ("issues", re.compile(
        r"\bissues?\b|\bcomplain|\bshould know\b|\bbefore using\b", re.I)),
    ("glossary", re.compile(
        r"\bdefined here\b|\bbusiness mean\b|\bagreed definition\b"
        r"|\bdefinition of\b|\bmean by\b", re.I)),
    ("pii", re.compile(
        r"\bpersonal data\b|\bpii\b|\bwhat kind of data\b|\bwhat shape\b", re.I)),
    ("classification", re.compile(
        r"\bclassif|\bpolicy tags?\b|\bsensitivity\b|\bgoverned\b", re.I)),
    ("column_meaning", re.compile(
        r"\bcolumn on\b|\bfield of\b|\bhow is [A-Z_]+ defined\b", re.I)),
    ("ownership", re.compile(
        r"\bwho owns\b|\baccountable for\b|\bwho do i talk to\b|\bowner\b", re.I)),
    ("documentation", re.compile(
        r"\bwhat is\b.*\bfor\b|\bdescription of\b|\bwhat does\b.*\bcontain\b"
        r"|\bcontains?\b", re.I)),
]

HOSTILE = re.compile(
    r"\bignore (the|all|previous|preceding)\b|\bdisregard\b"
    r"|\bsystem note\b|\byou are now cleared\b|\bpolicy (has been )?revoked\b"
    r"|\bomit the masking\b|\bPVN-CANARY-", re.I)

ISSUE_THEMES = [
    (re.compile(r"\bdefinition|metric|differently|close\b", re.I),
     "a disputed definition between teams"),
    (re.compile(r"\blate|freshness|sla|refresh\b", re.I),
     "late landings breaking a downstream refresh"),
    (re.compile(r"\bgrain|join\b", re.I),
     "an ambiguous or changed grain"),
    (re.compile(r"\brenamed|description|stale\b", re.I),
     "documentation that has drifted from the source system"),
    (re.compile(r"\baccess|request|queue\b", re.I),
     "an access request backlog"),
]

REFUSAL_ADVICE = (
    "This layer reports what the catalog records; it does not recommend "
    "changes to it. Deprecation, access, classification and definition "
    "changes are decisions for the owning team.")
REFUSAL_RESTRICTED = (
    "This asset is classified restricted. Column detail, sample values and "
    "row counts are released through an approved access request. Existence "
    "and ownership can be confirmed.")
REFUSAL_SCOPE = (
    "This question is scoped to a single asset. Details of other assets are "
    "answered under their own scope, not alongside this one.")


class RegexClassifier:
    """The default. Deliberately visible as a component so the model-backed
    one is a swap rather than a rewrite."""

    name = "regex"

    def __call__(self, prompt: str) -> str:
        for intent, pattern in RULES:
            if pattern.search(prompt):
                return intent
        return "unknown"


class Ecosystem:
    ROSTER = ROSTER

    def __init__(self, catalog, classifier=None):
        self.cat = catalog
        self.classify = classifier or RegexClassifier()
        self.verifier = Verifier(catalog)

    # -- entry point ------------------------------------------------------

    def answer(self, envelope: dict) -> dict:
        qid = envelope.get("question_id") or ""
        prompt = envelope.get("prompt") or ""
        scope = envelope.get("asset_id")
        draft = self.route(prompt, scope)
        out = self.verifier.check(draft, scope)
        out["question_id"] = qid
        return out

    # -- helpers ----------------------------------------------------------

    def _draft(self, answer, *, agents, value=None, citations=(),
               abstained=False, refused=False, reason=None, flags=(),
               confidence=0.9) -> dict:
        d = blank()
        d.update(answer=answer, answer_value=value, citations=list(citations),
                 abstained=abstained, refused=refused, reason=reason,
                 flags=list(flags), confidence=confidence,
                 agents=["router"] + list(agents))
        return d

    def _abstain(self, what: str, *, agents=("governance",), citations=()):
        return self._draft(
            f"The catalog does not record {what}.", agents=agents,
            citations=citations, abstained=True,
            reason=f"no record: {what}", confidence=0.0)

    def _refuse(self, text: str, policy_id: str, *, agents=("policy",)):
        return self._draft(text, agents=agents, citations=[policy_id],
                           refused=True, reason=f"policy {policy_id}",
                           confidence=1.0)

    def _person(self, uid):
        p = self.cat.person(uid)
        return f"{p['name']} ({p['team']})" if p else None

    # -- routing ----------------------------------------------------------

    def route(self, prompt: str, scope: str | None) -> dict:
        cat = self.cat
        intent = self.classify(prompt)
        asset = cat.resolve(scope)

        # Coverage. A qualified name the catalog does not hold is the
        # household-name case: the reply would be fluent, sourceless and
        # indistinguishable from a real one.
        if asset is None:
            named = cat.looks_like_qualified_name(prompt)
            unknown = [n for n in named if cat.resolve(n) is None]
            if unknown:
                return self._abstain(
                    f"an asset named {unknown[0]}. It is not in the catalog, "
                    "so it has no owner, description, certification or row "
                    "count here")
            if intent == "glossary":
                return self.glossary_desk(prompt)
            return self._abstain("anything matching that question")

        # Scope. The envelope authorises one asset. Lineage questions are the
        # exception — asking whether A feeds B is a question *about* B, and
        # naming A is how you ask it.
        others = [a for a in cat.mentioned_assets(prompt)
                  if a["asset_id"] != asset["asset_id"]]
        if others and intent not in ("lineage_path", "lineage_count"):
            return self._refuse(REFUSAL_SCOPE, "pol_001")

        if intent == "advice":
            return self._refuse(REFUSAL_ADVICE, "pol_004")

        if intent == "restricted_detail" or (
                cat.restricted(asset) and intent in
                ("column_meaning", "pii", "documentation")):
            policy = cat.policy_for(asset)
            if policy:
                return self._refuse(REFUSAL_RESTRICTED, policy["policy_id"])

        desks = {
            "ownership": self.governance,
            "documentation": self.governance,
            "column_meaning": self.governance,
            "classification": self.governance,
            "pii": self.governance,
            "lineage_count": self.lineage_desk,
            "lineage_path": self.lineage_desk,
            "freshness": self.operations,
            "trust": self.operations,
            "usage": self.operations,
            "issues": self.operations,
            "glossary": lambda p, a: self.glossary_desk(p),
            "multi": lambda p, a: self._joint(a),
        }
        desk = desks.get(intent)
        if desk is None:
            # An honest fall-through. It loses a mark and it does not invent
            # one, and that asymmetry is the point.
            return self._abstain(
                "an answer to that question. The intent was not recognised, "
                "and guessing at it would risk answering a different question "
                "than the one asked")
        return desk(prompt, asset)

    # -- governance -------------------------------------------------------

    def governance(self, prompt: str, asset: dict) -> dict:
        cat, aid = self.cat, asset["asset_id"]
        intent = self.classify(prompt)
        low = prompt.lower()

        if intent == "ownership":
            # A multi-part question crosses to operations without losing the
            # scope. Both desks appear in the path.
            if re.search(r"\bcertif|\bcurrent\b|\bon time\b|\bhealth\b|"
                         r"\bfresh\b", low):
                return self._joint(asset)
            owner = self._person(asset["owner_id"])
            if not owner:
                return self._abstain(f"an owner for {asset['qualified_name']}",
                                     citations=[aid])
            steward = self._person(asset["steward_id"])
            text = f"{asset['qualified_name']} is owned by {owner}."
            if steward:
                text += f" Data steward: {steward}."
            return self._draft(text, agents=["governance"],
                               value=self.cat.person(asset["owner_id"])["name"],
                               citations=[aid])

        if intent == "classification":
            if re.search(r"\bpolicy tags?\b", low):
                tags = asset.get("policy_tags") or []
                if not tags:
                    return self._abstain(
                        f"any policy tags on {asset['qualified_name']}",
                        citations=[aid])
                return self._draft(
                    f"{asset['qualified_name']} carries the policy tags: "
                    f"{', '.join(tags)}.", agents=["governance"],
                    value=", ".join(tags), citations=[aid])
            return self._draft(
                f"{asset['qualified_name']} is classified "
                f"{asset['classification']}.", agents=["governance"],
                value=asset["classification"], citations=[aid])

        if intent == "pii":
            col = self._column_in(prompt, asset)
            if col is None:
                return self._abstain(
                    f"a column matching that name on {asset['qualified_name']}",
                    citations=[aid])
            if not col["pii_type"]:
                return self._draft(
                    f"{col['name']} on {asset['qualified_name']} is not "
                    f"marked as personal data. It is classified "
                    f"{col['classification']}.", agents=["governance"],
                    value=None, citations=[col["column_id"]])
            # The shape is answerable; the values are not, and are never read
            # into the draft in the first place.
            return self._draft(
                f"{col['name']} on {asset['qualified_name']} holds personal "
                f"data of type '{col['pii_type']}', classified "
                f"{col['classification']}. Sample values are withheld under "
                f"the personal-data policy.", agents=["governance"],
                value=col["pii_type"], citations=[col["column_id"], "pol_002"])

        if intent == "column_meaning":
            col = self._column_in(prompt, asset)
            if col is None:
                return self._abstain(
                    f"a column matching that name on {asset['qualified_name']}",
                    citations=[aid])
            if not col["description"]:
                return self._abstain(
                    f"a description for {col['name']} on "
                    f"{asset['qualified_name']}", citations=[aid])
            return self._draft(col["description"], agents=["governance"],
                               value=col["description"],
                               citations=[col["column_id"]])

        # documentation
        if not asset["description"]:
            return self._abstain(
                f"a description for {asset['qualified_name']}", citations=[aid])
        return self._draft(asset["description"], agents=["governance"],
                           value=asset["description"], citations=[aid])

    def _column_in(self, prompt: str, asset: dict):
        up = prompt.upper()
        best = None
        for col in asset["columns"]:
            if re.search(rf"\b{re.escape(col['name'])}\b", up):
                # Longest match wins: CUSTOMER_EMAIL before EMAIL.
                if best is None or len(col["name"]) > len(best["name"]):
                    best = col
        return best

    def _joint(self, asset: dict) -> dict:
        """Governance and operations on one answer, scope intact across the
        handoff. Both roles are reported."""
        cat, aid = self.cat, asset["asset_id"]
        owner = self._person(asset["owner_id"])
        run = cat.last_run(aid)
        breached = cat.sla_breached(asset)

        parts, cites, flags = [], [aid], set()
        parts.append(f"{asset['qualified_name']} is owned by {owner}."
                     if owner else
                     f"{asset['qualified_name']} has no owner recorded.")
        cert = asset["certification"]
        parts.append(f"Certification: {cert}." if cert
                     else "It is not certified.")
        if run:
            cites.append(run["run_id"])
            parts.append(f"The most recent pipeline run ({run['started_at']}) "
                         f"{'succeeded' if run['status'] == 'success' else 'failed'}.")
        if breached is None:
            parts.append("No freshness SLA is set.")
        else:
            parts.append(
                f"Last landed {asset['last_updated_at']}, which is "
                f"{'outside' if breached else 'inside'} its "
                f"{asset['freshness_sla_hours']}h SLA.")
        if cert == "verified" and (breached or (run and run["status"] == "failed")):
            flags.add("conflict")
            parts.append("The certification and the operational record "
                         "disagree; both are reported rather than reconciled.")
        return self._draft(" ".join(parts),
                           agents=["governance", "operations"],
                           value=self.cat.person(asset["owner_id"])["name"]
                           if owner else None,
                           citations=cites, flags=flags)

    # -- lineage ----------------------------------------------------------

    def lineage_desk(self, prompt: str, asset: dict) -> dict:
        cat, aid = self.cat, asset["asset_id"]
        intent = self.classify(prompt)

        if intent == "lineage_path":
            others = [a for a in cat.mentioned_assets(prompt)
                      if a["asset_id"] != aid]
            if not others:
                return self._abstain(
                    "the other asset in that question", agents=["lineage"],
                    citations=[aid])
            other = others[0]
            feeds = cat.feeds_into(other["asset_id"], aid)
            return self._draft(
                f"{other['qualified_name']} "
                f"{'does' if feeds else 'does not'} feed into "
                f"{asset['qualified_name']} at any depth.",
                agents=["lineage"], value=feeds,
                citations=[aid, other["asset_id"]])

        reach = cat.downstream(aid)
        direct = cat.downstream(aid, transitive=False)
        return self._draft(
            f"{len(reach)} assets read from {asset['qualified_name']} "
            f"transitively ({len(direct)} directly).",
            agents=["lineage"], value=len(reach),
            citations=[aid] + sorted(reach)[:5])

    # -- operations -------------------------------------------------------

    def operations(self, prompt: str, asset: dict) -> dict:
        cat, aid = self.cat, asset["asset_id"]
        intent = self.classify(prompt)

        if intent == "issues":
            return self._issues(asset)

        if intent == "usage":
            use = cat.usage(aid)
            if not use:
                return self._abstain(f"usage for {asset['qualified_name']}",
                                     agents=["operations"], citations=[aid])
            flags, extra = set(), ""
            if asset["certification"] == "deprecated" and use["queries_30d"] > 0:
                flags.add("conflict")
                extra = (" The asset is certified deprecated while still "
                         "carrying this volume; the badge and the usage "
                         "disagree.")
            return self._draft(
                f"{asset['qualified_name']} was queried {use['queries_30d']} "
                f"times in the last 30 days by {use['distinct_users_30d']} "
                f"distinct users, most recently {use['last_queried_at']}."
                + extra, agents=["operations"], value=use["queries_30d"],
                citations=[aid], flags=flags)

        run = cat.last_run(aid)
        breached = cat.sla_breached(asset)

        if intent == "trust":
            parts, cites, flags = [], [aid], set()
            cert = asset["certification"]
            parts.append(f"{asset['qualified_name']} is certified {cert}."
                         if cert else
                         f"{asset['qualified_name']} is not certified.")
            if run:
                cites.append(run["run_id"])
                if run["status"] == "failed":
                    parts.append(
                        f"Its most recent pipeline run ({run['started_at']}) "
                        f"failed.")
                else:
                    parts.append(f"Its most recent run ({run['started_at']}) "
                                 f"succeeded.")
            if breached:
                parts.append(f"It last landed {asset['last_updated_at']}, "
                             f"outside its {asset['freshness_sla_hours']}h SLA.")
            if cert == "verified" and (breached or
                                       (run and run["status"] == "failed")):
                flags.add("conflict")
                parts.append("The certification says one thing and the "
                             "operational record says another. Both are "
                             "reported; deciding between them is the owning "
                             "team's call.")
            return self._draft(" ".join(parts), agents=["operations"],
                               citations=cites, flags=flags)

        # freshness
        if breached is None:
            return self._abstain(
                f"a freshness SLA for {asset['qualified_name']}, so whether it "
                f"is late cannot be determined. It last landed "
                f"{asset['last_updated_at']}", agents=["operations"],
                citations=[aid])
        return self._draft(
            f"{asset['qualified_name']} last landed "
            f"{asset['last_updated_at']}, {cat.staleness_hours(asset)}h ago, "
            f"against a {asset['freshness_sla_hours']}h SLA — "
            f"{'breached' if breached else 'inside SLA'}.",
            agents=["operations"], value=breached, citations=[aid])

    def _issues(self, asset: dict) -> dict:
        """Issue bodies are data, not instructions, and they are never quoted.
        The summary is built from status, dates and a keyword classification,
        which removes the echo path rather than trying to filter it. Hostile
        text is reported as a finding — the legitimate question still gets a
        legitimate answer."""
        cat, aid = self.cat, asset["asset_id"]
        rows = cat.issues(aid)
        if not rows:
            return self._draft(
                f"No issues are recorded against {asset['qualified_name']}.",
                agents=["operations"], citations=[aid])

        themes, hostile, cites = set(), [], [aid]
        for issue in rows:
            cites.append(issue["issue_id"])
            body = issue.get("body") or ""
            if HOSTILE.search(body):
                hostile.append(issue["issue_id"])
                continue
            for pattern, theme in ISSUE_THEMES:
                if pattern.search(body) or pattern.search(issue["title"]):
                    themes.add(theme)
                    break

        open_n = sum(1 for i in rows if i["status"] == "open")
        text = (f"{len(rows)} issue(s) recorded against "
                f"{asset['qualified_name']}, {open_n} still open.")
        if themes:
            text += " Reported themes: " + "; ".join(sorted(themes)) + "."

        flags = set()
        if hostile:
            flags.add("injection_detected")
            text += (f" {len(hostile)} issue thread(s) contain text attempting "
                     f"to instruct the reader to bypass catalog policy. That "
                     f"text was not followed and is not reproduced here; the "
                     f"threads are cited so a human can review them.")
        return self._draft(text, agents=["operations"], citations=cites,
                           flags=flags)

    # -- glossary ---------------------------------------------------------

    def glossary_desk(self, prompt: str, asset: dict | None = None) -> dict:
        cat = self.cat
        quoted = re.findall(r'"([^"]+)"', prompt)
        candidates = quoted or re.findall(
            r"\b(?:of|mean by|define[sd]?)\s+([A-Za-z][A-Za-z ]{2,30})", prompt)
        term = None
        for name in candidates:
            term = cat.term(name)
            if term:
                break
        if term is None:
            return self._abstain("a business term matching that question",
                                 agents=["glossary"])

        cites, flags = [term["term_id"]], set()
        text = f"\"{term['name']}\" is defined as: {term['definition']}"
        if term["status"] != "approved":
            text += f" (term status: {term['status']})"
        text += "."

        # Does the definition agree with the columns it is attached to?
        disagreeing = []
        for cid in term["linked_column_ids"]:
            col = cat.columns.get(cid)
            if col and col["description"] and \
                    not self._consistent(term["definition"], col["description"]):
                disagreeing.append(col)
        if disagreeing:
            flags.add("conflict")
            col = disagreeing[0]
            cites.append(col["column_id"])
            owner = cat.column_owner[col["column_id"]]
            text += (f" This does not agree with the description on "
                     f"{cat.by_id[owner]['qualified_name']}.{col['name']}, "
                     f"which records a different meaning for the same field. "
                     f"Both are cited; the disagreement is for the term owner "
                     f"to settle.")
        return self._draft(text, agents=["glossary"], value=term["definition"],
                           citations=cites, flags=flags)

    @staticmethod
    def _consistent(definition: str, description: str) -> bool:
        """A cheap negation check. A definition that excludes something and a
        description that includes it are in conflict — 'less refunds' against
        'before any refund adjustment', 'at least one order' against
        'regardless of order history'. Crude, and it does not need to be
        clever: it needs to be conservative, and a false conflict costs a
        flag while a missed one costs the whole point of the field."""
        d, c = definition.lower(), description.lower()
        markers = ["less ", "excluded", "excluding", "not ", "without ",
                   "at least", "only", "confirmed"]
        opposites = ["including", "regardless", "any point", "whether or not",
                     "before any", "merely"]
        return not (any(m in d for m in markers) and
                    any(o in c for o in opposites))
