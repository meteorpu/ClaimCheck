"""LLM step: explain flagged claims and act as a second reviewer.

The rule engine decides; the LLM never changes a verdict. For each flagged claim it:
  1. writes a short plain-English explanation and next step for the biller
  2. reads the full payer documents and says whether it agrees with the engine;
     disagreements are surfaced to a human, not acted on.

Cost control: Haiku 5.5, payer documents in a cached system prompt, one call
per flagged claim only (passing claims are not sent), results cached on disk.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .load import DATA_DIR, ROOT

MODEL = "claude-haiku-5-5"
CACHE_PATH = ROOT / "output" / "llm_cache.json"
BRIEF_CACHE_PATH = ROOT / "output" / "llm_briefings.json"


class Explanation(BaseModel):
    summary: str = Field(description="One or two plain-English sentences for a billing clerk: what is wrong and why the payer would deny it.")
    next_step: str = Field(description="The single most useful action, naming who should do it (biller, care coordinator, authorization team).")
    agrees_with_engine: bool = Field(description="True if the payer documents support the rule engine's finding.")
    reviewer_note: str = Field(description="If you disagree or see another rule the engine may have missed, say which and cite the document. Otherwise empty.")


def _rule_documents() -> str:
    parts = []
    for name in ["billing_basics.md", "denial_codes.md"]:
        parts.append(f"<document name='{name}'>\n{(DATA_DIR / name).read_text()}\n</document>")
    for p in sorted((DATA_DIR / "payers").glob("*.md")):
        parts.append(f"<document name='{p.name}'>\n{p.read_text()}\n</document>")
    return "\n\n".join(parts)


SYSTEM_INSTRUCTIONS = """You support billing staff at US home care agencies who check claims before submitting them to payers.

A deterministic rule engine has already checked each claim against the payers' rules, EVV visit records,
authorizations and earlier claims. You receive the claim, the facts the engine used, and its findings.

Your jobs:
1. Explain the finding to a busy billing clerk in plain English: what is wrong, and why the payer would deny it.
   No jargon beyond terms they use daily (EVV, authorization, units, modifier). Be specific: use the numbers given.
2. Give the single most useful next step and who should take it.
3. Act as a second reviewer: using ONLY the payer documents below, say whether they support the engine's finding.
   If you think the engine misapplied a rule or missed one, say so in reviewer_note and cite the document.
   Never invent rules, authorization numbers or unit counts that are not in the documents or the facts given.

Payer documents follow. Rules have effective dates: judge each claim on the rules in force on its date of service."""


class Explainer:
    def __init__(self, model: str = MODEL):
        self.model = model
        self.enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
        self._client = None
        self.system = [{
            "type": "text",
            "text": SYSTEM_INSTRUCTIONS + "\n\n" + _rule_documents(),
            "cache_control": {"type": "ephemeral"},  # same prefix for every claim -> cached after the first call
        }]
        self.usage = {"calls": 0, "input": 0, "cache_read": 0, "output": 0}

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    @staticmethod
    def _payload(claim: dict) -> dict:
        keep = ["claim_id", "payer_id", "patient_id", "service_code", "dos", "submitted", "units",
                "auth_id", "modifier", "frequency_code", "original_claim_id"]
        c = {k: (str(claim[k].date()) if hasattr(claim.get(k), "date") else claim.get(k)) for k in keep}
        return {"claim": c, "predicted_denial_code": claim.get("pred_code"),
                "engine_findings": claim.get("all_problems", [])}

    def explain(self, claim: dict) -> Explanation | None:
        if not self.enabled or not claim.get("pred_code"):
            return None
        payload = self._payload(claim)
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
        if key in self.cache:
            return Explanation(**self.cache[key])

        import anthropic
        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=1024,
                system=self.system,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Claim to explain:\n" + json.dumps(payload, indent=2, default=str)}],
                output_format=Explanation,
            )
        except anthropic.APIError as e:
            print(f"LLM call failed for {claim['claim_id']}: {e}")
            return None
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return None
        u = resp.usage
        self.usage["calls"] += 1
        self.usage["input"] += u.input_tokens
        self.usage["cache_read"] += u.cache_read_input_tokens or 0
        self.usage["output"] += u.output_tokens
        result = resp.parsed_output
        self.cache[key] = result.model_dump()
        CACHE_PATH.parent.mkdir(exist_ok=True)
        CACHE_PATH.write_text(json.dumps(self.cache, indent=1))
        return result


BRIEFING_INSTRUCTIONS = """You write a short weekly briefing for the manager of a US home care agency's billing team.
The reader is not technical. Use plain words: say "rejected" not "denied", "visit record" not "EVV",
"approved hours" not "authorization units". No codes like D04 unless in brackets after the plain words.

You receive the results of an automatic scan of past claims, payer documents and claims waiting to be sent.
Write:
- 2-3 sentences: how things are going overall, with the key numbers.
- "What changed": up to 3 bullets on the most important findings and what they probably mean.
- "What to do this week": up to 3 bullets, each naming who should act.
Only use the facts given. If something is uncertain (for example a rule worked out from rejection notes), say so.
Keep it under 200 words. Markdown, no title."""


def write_briefing(report: dict, model: str = MODEL) -> str | None:
    """Turn a Monitor scan into a plain-English briefing. Returns None without an API key."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    facts = {k: report[k] for k in ("data_up_to", "claims_scanned", "batch_size", "rejection_rate_by_month",
                                    "documents", "auth_alerts")}
    facts["findings"] = [{k: f[k] for k in ("severity", "title", "what", "why", "action")} for f in report["findings"]]
    key = "brief:" + hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()[:16]
    cache = json.loads(BRIEF_CACHE_PATH.read_text()) if BRIEF_CACHE_PATH.exists() else {}
    if key in cache:
        return cache[key]
    import anthropic
    try:
        resp = anthropic.Anthropic().messages.create(
            model=model, max_tokens=1024, system=BRIEFING_INSTRUCTIONS, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Scan results:\n" + json.dumps(facts, indent=1, default=str)}])
    except anthropic.APIError as e:
        print(f"Briefing failed: {e}")
        return None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        return None
    cache[key] = text
    BRIEF_CACHE_PATH.parent.mkdir(exist_ok=True)
    BRIEF_CACHE_PATH.write_text(json.dumps(cache, indent=1))
    return text
