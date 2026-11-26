"""ClaimCheck: find claims that will be rejected before you send them, and fix them.

    streamlit run app.py
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from claimcheck import monitor as M
from claimcheck.engine import FIX_DATA, FIX_NONE, FIX_PROCESS
from claimcheck.explain import Explainer, write_briefing
from claimcheck.load import clean_claims, load_claims
from claimcheck.monitor import REASON, payer_name
from claimcheck.pipeline import backtest, build, predict_batch

load_dotenv()
st.set_page_config(page_title="ClaimCheck", layout="wide")

# ---------- plain-English labels ----------
RESULT = {"paid": "✅ Ready to send", "denied": "⛔ Likely rejected"}
WHO = {FIX_DATA: "🔧 Fix here", FIX_PROCESS: "👥 Needs another team", FIX_NONE: "🚫 Can't be fixed", "": ""}
WHO_HELP = {
    FIX_DATA: "The right value is already in our records (an authorization, a visit record or an earlier claim). "
              "ClaimCheck proposes the change; you approve it.",
    FIX_PROCESS: "Someone outside billing has to act first, e.g. the care coordinator confirms a visit, "
                 "or the authorization team asks the payer for more hours.",
    FIX_NONE: "Sending it would only be rejected: it is past the payer's deadline, or the visit is already billed.",
}
SERVICE = {"T1019": "Personal care", "T1005": "Respite care", "S5125": "Attendant care"}
SEV = {"high": "🔴 Important", "medium": "🟠 Worth a look", "info": "🔵 For information"}
DECISION = {"accepted": "✅ Accepted", "later": "⏸ Later", "dismissed": "✖ Dismissed"}


def reason_label(code) -> str:
    return REASON.get(code, "") if isinstance(code, str) else ""


# ---------- cached setup ----------
@st.cache_resource
def setup():
    rules, engine, history = build()
    default_batch = load_claims("claims_to_check.csv", rules)
    return rules, engine, history, default_batch


@st.cache_resource
def explainer():
    return Explainer()


@st.cache_data
def run_backtest():
    rules, engine, history, batch = setup()
    return backtest(engine, history, batch)


@st.cache_data
def default_predictions():
    rules, engine, history, batch = setup()
    return predict_batch(engine, history, batch)


rules, engine, history, default_batch = setup()
ss = st.session_state
ss.setdefault("approved", {})       # claim_id -> fix approved by the biller
ss.setdefault("explanations", {})
ss.setdefault("briefing", None)
ss.setdefault("graph", "Rejections per month")


def apply_patches(batch: pd.DataFrame) -> pd.DataFrame:
    b = batch.copy()
    for cid, patch in ss.approved.items():
        for k, v in patch.items():
            b.loc[b.claim_id == cid, k] = v
    return b


# ---------- sidebar: words used in this app ----------
with st.sidebar:
    st.header("Words used here")
    st.markdown("""
**Payer**: the insurer or state plan that pays for the care (Northwind, Harbor, Summit).

**Claim**: a bill to the payer for one visit: who, what service, which day, how long.

**Rejected (denied)**: the payer refuses to pay. It gives a *reason code* (D01 to D99); this app shows the reason in words.

**Visit record (EVV)**: the carer's phone check-in and check-out. Payers only pay for visits with a *verified* record.

**Authorization**: the payer's approval in advance for a number of hours of a service for a patient, between two dates.
Each paid claim uses some of those hours.

**Units**: time is billed in 15-minute units (4 units = 1 hour). Payers round leftover minutes differently.

**Modifier (billing tag)**: a short code added to a claim, e.g. Harbor wants *U3* on weekend personal care.

**Timely filing**: the deadline for sending a claim after the visit.

**Corrected claim**: a resend that replaces a rejected claim, instead of a duplicate.
""")

# ---------- header ----------
st.title("ClaimCheck")
st.caption("Find the claims that will be rejected **before** you send them, see why in plain words, and fix them. "
           "The rules decide, AI explains, and you approve every change.")

tab_check, tab_monitor, tab_test, tab_rules = st.tabs(
    ["📋 Check claims", "🔔 Monitor", "🎯 How accurate is it?", "📖 Payer rules"])

# =====================================================================
# CHECK CLAIMS
# =====================================================================
with tab_check:
    st.markdown("Upload the claims you are about to send. ClaimCheck checks each one against the payer's rules, "
                "the visit records, the authorizations and the claims already sent, and tells you which will be rejected.")
    up = st.file_uploader("Claims file (CSV, same columns as claims_to_check.csv)", type="csv")
    if up is not None:
        raw = pd.read_csv(up, dtype=str).drop_duplicates()
        batch = clean_claims(raw, rules)
        st.info(f"Loaded {len(batch)} claims from {up.name}")
    else:
        batch = default_batch
        st.caption(f"Showing the sample file claims_to_check.csv ({len(batch)} claims).")

    res = predict_batch(engine, history, apply_patches(batch))

    n = len(res)
    risky = res[res.pred_status == "denied"]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Claims checked", n)
    c2.metric("✅ Ready to send", int((res.pred_status == "paid").sum()))
    c3.metric("🔧 Fix here", int((risky.fix_type == FIX_DATA).sum()), help=WHO_HELP[FIX_DATA])
    c4.metric("👥 Needs another team", int((risky.fix_type == FIX_PROCESS).sum()), help=WHO_HELP[FIX_PROCESS])
    c5.metric("🚫 Can't be fixed", int((risky.fix_type == FIX_NONE).sum()), help=WHO_HELP[FIX_NONE])
    st.caption(f"Money at risk: **${risky.billed_amount.sum():,.0f}** of ${res.billed_amount.sum():,.0f}. "
               f"Fixes you approved this session: {len(ss.approved)}.")

    f1, f2, f3, f4 = st.columns([1, 1, 2, 1])
    show = f1.selectbox("Show", ["Likely rejected", "All claims", "Ready to send"])
    payer = f2.selectbox("Payer", ["All"] + sorted(res.payer_id.dropna().unique()), format_func=lambda p: p if p == "All" else payer_name(p))
    whof = f3.multiselect("What can be done", [FIX_DATA, FIX_PROCESS, FIX_NONE], format_func=lambda x: WHO[x])
    details = f4.toggle("Show details", help="Adds the rule source and any tidying of the data (e.g. dates in mixed formats).")
    view = res
    if show == "Likely rejected":
        view = view[view.pred_status == "denied"]
    elif show == "Ready to send":
        view = view[view.pred_status == "paid"]
    if payer != "All":
        view = view[view.payer_id == payer]
    if whof:
        view = view[view.fix_type.isin(whof)]

    table = pd.DataFrame({
        "claim": view.claim_id,
        "payer": view.payer_id.map(payer_name),
        "patient": view.patient_id,
        "service": view.service_code.map(lambda s: SERVICE.get(s, s)),
        "visit date": view.dos.dt.date,
        "hours": view.units / 4,
        "result": view.pred_status.map(RESULT),
        "why": view.pred_code.map(reason_label),
        "details": view.reason,
        "what can be done": view.fix_type.map(WHO),
        "what to do": view.suggested_fix,
    })
    if details:
        table["reason code"] = view.pred_code.fillna("")
        table["rule comes from"] = view.rule_source
        table["data tidied"] = view.issues.map(lambda x: "; ".join(x))
    st.dataframe(table, use_container_width=True, hide_index=True, height=360,
                 column_config={"hours": st.column_config.NumberColumn(format="%.2f")})

    d1, d2 = st.columns(2)
    pred_csv = res.rename(columns={"pred_status": "status", "pred_code": "denial_code"})[["claim_id", "status", "denial_code"]]
    d1.download_button("⬇️ Results file (predictions.csv)", pred_csv.to_csv(index=False), "predictions.csv", "text/csv")
    ready = res[res.pred_status == "paid"].drop(columns=["patch", "all_problems", "issues"], errors="ignore")
    d2.download_button(f"⬇️ Claims ready to send ({len(ready)})", ready.to_csv(index=False), "ready_to_submit.csv", "text/csv")

    # ---------- one claim ----------
    st.divider()
    st.subheader("Look at one claim")
    options = list(view[view.pred_status == "denied"].claim_id)
    if not options:
        st.write("No likely-rejected claims in this view.")
    else:
        cid = st.selectbox("Claim", options)
        row = {k: (None if not isinstance(v, (list, dict)) and pd.isna(v) else v)
               for k, v in res[res.claim_id == cid].iloc[0].to_dict().items()}
        left, right = st.columns([3, 2])
        with left:
            st.markdown(f"**{cid}** · {payer_name(row['payer_id'])} · {SERVICE.get(row['service_code'], row['service_code'])} · "
                        f"{row['dos']:%a %d %b %Y} · {row['units'] / 4:g} hours ({row['units']} units) · "
                        f"authorization {row['auth_id'] or 'none'} · billing tag {row['modifier'] or 'none'}")
            st.error(f"**Likely rejected: {reason_label(row['pred_code'])}** ({row['pred_code']})  \n{row['reason']}  \n"
                     f"_Rule from: {row['rule_source']}_")
            if len(row["all_problems"]) > 1:
                st.markdown("Other problems on this claim (the payer only reports one, so fix them all):")
                for p in row["all_problems"][1:]:
                    st.markdown(f"- **{reason_label(p['code'])}**: {p['reason']} → _{p['fix']}_")

            patch = row["patch"] or {}
            st.markdown(f"**What to do** ({WHO[row['fix_type']]}): {row['suggested_fix']}")
            if row["fix_type"] == FIX_DATA and patch:
                st.table(pd.DataFrame([{"field": k, "now": str(row.get(k) or "(blank)"), "change to": str(v)} for k, v in patch.items()]))
                st.caption("The new values come only from our own records. Nothing changes until you approve.")
                if st.button("✅ Approve this fix and check again", key=f"approve_{cid}"):
                    ss.approved[cid] = {**ss.approved.get(cid, {}), **patch}
                    st.rerun()
            elif row["fix_type"] == FIX_PROCESS:
                st.info("This can't be fixed by editing the claim. " + WHO_HELP[FIX_PROCESS])
        with right:
            st.markdown("**Ask AI to explain**")
            ex = explainer()
            if not ex.enabled:
                st.caption("Add ANTHROPIC_API_KEY to the .env file to turn on AI explanations.")
            elif cid in ss.explanations or st.button("Explain this claim", key=f"explain_{cid}"):
                if cid not in ss.explanations:
                    with st.spinner("Asking Claude..."):
                        ss.explanations[cid] = ex.explain(row)
                e = ss.explanations[cid]
                if e is None:
                    st.warning("No explanation available.")
                else:
                    st.write(e.summary)
                    st.markdown(f"**Next step:** {e.next_step}")
                    if e.agrees_with_engine and not e.reviewer_note:
                        st.success("AI double-checked the payer documents and agrees.")
                    else:
                        st.warning(f"AI double-check: please review. {e.reviewer_note}")

    if ss.approved:
        with st.expander(f"Fixes you approved ({len(ss.approved)})"):
            st.json(ss.approved)
            if st.button("Undo all approved fixes"):
                ss.approved = {}
                st.rerun()

# =====================================================================
# MONITOR
# =====================================================================
GRAPHS = {
    "Rejections per month": dict(
        purpose="Shows what share of claims each payer rejected, month by month.",
        read="A line that jumps up means something changed: a new rule, or a new problem in how claims are made. "
             "Use it to see whether things are getting better or worse."),
    "Why claims were rejected": dict(
        purpose="For one payer, shows how many claims were rejected for each reason, month by month.",
        read="Look for a reason that suddenly appears or grows. That is how ClaimCheck spotted Summit's unannounced "
             "\"whole hours only\" rule in July 2026."),
    "Hours running out": dict(
        purpose="Patients whose approved hours will be used up before their authorization ends, at the current pace.",
        read="The bar is how much of the approved hours is used; the dot is how much of the time has passed. "
             "When the bar is well ahead of the dot, the hours will run out early: schedulers should ask the payer for more."),
    "Claims waiting to be sent": dict(
        purpose="Why the claims you are about to send would be rejected.",
        read="The tallest bars are the problems worth fixing at the source, not just claim by claim."),
}


def show_graph(name: str, report: dict):
    h = history.assign(month=history.dos.dt.to_period("M").dt.strftime("%Y-%m"))
    if name == "Rejections per month":
        rate = h.assign(rej=h.status == "denied").pivot_table(index="month", columns="payer_id", values="rej", aggfunc="mean")
        st.line_chart((rate * 100).rename(columns=payer_name), y_label="% of claims rejected", x_label="month of visit")
    elif name == "Why claims were rejected":
        pm = st.selectbox("Payer", sorted(h.payer_id.unique()), format_func=payer_name, key="graph_payer")
        hp = h[(h.payer_id == pm) & h.denial_code.notna()]
        t = hp.pivot_table(index="month", columns="denial_code", values="claim_id", aggfunc="count").fillna(0)
        st.bar_chart(t.rename(columns=lambda c: REASON.get(c, c)), y_label="claims rejected", x_label="month of visit")
    elif name == "Hours running out":
        res_all = default_predictions()
        as_of = pd.Timestamp(report["data_up_to"])
        al = M.auth_burn(engine, history, res_all, as_of)
        st.caption(f"As of {as_of:%d %b %Y}: {len(al)} authorizations.")
        st.dataframe(pd.DataFrame({
            "patient": al.patient_id, "payer": al.payer_id.map(payer_name),
            "service": al.service_code.map(lambda s: SERVICE.get(s, s)),
            "hours used": al.pct_used * 100, "time passed": al.pct_time * 100,
            "runs out about": al.runs_out.dt.date, "authorization ends": al.end_date.dt.date, "authorization": al.auth_id,
        }), hide_index=True, use_container_width=True, height=300, column_config={
            "hours used": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
            "time passed": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100)})
    else:
        r = default_predictions()
        counts = r[r.pred_status == "denied"].pred_code.map(lambda c: REASON.get(c, c)).value_counts()
        st.bar_chart(counts, horizontal=True, x_label="claims", y_label="")


with tab_monitor:
    if M.REPORT_PATH.exists() and "report" not in ss:
        ss.report = json.loads(M.REPORT_PATH.read_text())

    top_l, top_r = st.columns([3, 1])
    with top_r:
        if st.button("🔄 Run the scan now", use_container_width=True) or "report" not in ss:
            with st.spinner("Scanning claims, documents and authorizations..."):
                ss.report = M.scan(engine, rules, history, default_predictions())
                ss.briefing = None
        ai_ready = bool(explainer().enabled)
        if st.button("✨ Write a briefing with AI", use_container_width=True, disabled=not ai_ready,
                     help=None if ai_ready else "Add ANTHROPIC_API_KEY to the .env file to turn this on."):
            with st.spinner("Claude is writing the briefing..."):
                ss.briefing = write_briefing(ss.report) or "The AI briefing could not be written. Try again."
    report = ss.report
    board = M.load_board()
    findings = report["findings"]
    open_items = [f for f in findings if f["id"] not in board or board[f["id"]]["decision"] == "later"]

    with top_l:
        st.subheader("Last scan")
        st.caption(f"Run {report['scanned_at'].replace('T', ' at ')}. The scan runs by itself every weekday morning "
                   "and you can run it any time. It reads past claims, the payers' documents and the claims waiting to be sent, "
                   "and looks for changes people would not notice on their own.")
        for line in M.plain_summary(report):
            st.markdown(f"- {line}")
    if ss.briefing:
        with st.container(border=True):
            st.markdown("**✨ AI briefing**")
            st.markdown(ss.briefing)
            st.caption("Written by Claude from the scan results only. Check the suggestions below before acting.")

    # ---------- suggestion board ----------
    st.divider()
    b1, b2 = st.columns([3, 1])
    b1.subheader(f"Suggestions ({len(findings) - len(open_items)} of {len(findings)} decided)")
    b1.caption("Each card is something the scan found and what we suggest doing. Accept it, leave it for later, or dismiss it. "
               "Decisions are saved, so the team can see what has been handled.")
    filt = b2.radio("Show", ["To do", "All", "Done"], horizontal=True, label_visibility="collapsed")
    st.progress((len(findings) - len(open_items)) / max(len(findings), 1))

    shown = {"To do": open_items, "All": findings,
             "Done": [f for f in findings if f in findings and f not in open_items]}[filt]
    if not shown:
        st.success("Nothing left to do. 🎉")
    for f in shown:
        d = board.get(f["id"])
        with st.container(border=True):
            h1, h2 = st.columns([5, 2])
            h1.markdown(f"**{f['title']}**")
            h2.markdown(f"{SEV[f['severity']]}" + (f" · {DECISION[d['decision']]}" if d else ""))
            st.markdown(f"{f['what']}  \n_{f['why']}_")
            st.markdown(f"👉 **Suggested:** {f['action']}  ·  **For:** {f['team']}")
            if d and d.get("note"):
                st.caption(f"Note: {d['note']} ({d['at'].replace('T', ' ')})")

            cols = st.columns([1, 1, 1, 1, 3])
            note = cols[4].text_input("Note", key=f"note_{f['id']}", placeholder="Add a note (optional)",
                                      label_visibility="collapsed")
            if cols[0].button("✅ Accept", key=f"acc_{f['id']}"):
                if f["kind"] == "document":
                    M.accept_documents(rules)
                M.save_decision(f["id"], "accepted", note)
                st.rerun()
            if cols[1].button("⏸ Later", key=f"later_{f['id']}"):
                M.save_decision(f["id"], "later", note)
                st.rerun()
            if cols[2].button("✖ Dismiss", key=f"dis_{f['id']}"):
                M.save_decision(f["id"], "dismissed", note)
                st.rerun()
            graph_for = {"new_reason": "Why claims were rejected", "spike": "Why claims were rejected",
                         "authorization": "Hours running out", "batch": "Claims waiting to be sent"}.get(f["kind"])
            if graph_for and cols[3].button("📈 Graph", key=f"g_{f['id']}"):
                ss.graph = graph_for
                if f.get("payer"):
                    ss.graph_payer = f["payer"]
                st.rerun()
            if f["kind"] == "authorization" and d and d["decision"] == "accepted":
                al = M.auth_burn(engine, history, default_predictions(), pd.Timestamp(report["data_up_to"]))
                st.download_button("⬇️ List for schedulers", al[["auth_id", "patient_id", "payer_id", "service_code",
                                   "used", "units_authorized", "runs_out", "end_date"]].to_csv(index=False),
                                   "authorizations_running_out.csv", "text/csv", key="dl_sched")
    if board and st.button("Reset all decisions"):
        M.clear_board()
        st.rerun()

    # ---------- graph viewer ----------
    st.divider()
    st.subheader("Look at the trends")
    g = st.selectbox("Graph", list(GRAPHS), key="graph")
    info = GRAPHS[g]
    gl, gr = st.columns([3, 1])
    with gr:
        st.markdown(f"**What it shows**  \n{info['purpose']}")
        st.markdown(f"**How to read it**  \n{info['read']}")
    with gl:
        show_graph(g, report)

# =====================================================================
# HOW ACCURATE IS IT?
# =====================================================================
with tab_test:
    st.markdown("To check ClaimCheck before trusting it, we replayed **every past claim** "
                f"({len(history):,} claims, June to October 2026) in the order they were sent. For each one, ClaimCheck "
                "made its prediction using only what was known at that moment, and we compared it with what the payer actually did.")
    df = run_backtest()
    ac, pc = df.denial_code.fillna("PAID"), df.pred_code.fillna("PAID")
    tp = ((df.status == "denied") & (df.pred_status == "denied")).sum()
    fp = ((df.status == "paid") & (df.pred_status == "denied")).sum()
    fn = ((df.status == "denied") & (df.pred_status == "paid")).sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Right about paid or rejected", f"{(df.status == df.pred_status).mean():.1%}",
              help="Out of all past claims, how often ClaimCheck correctly said 'will be paid' or 'will be rejected'.")
    m2.metric("When it warns you, it's right", f"{tp / (tp + fp):.1%}",
              help="Of the claims ClaimCheck said would be rejected, how many really were (precision). "
                   "High means few false alarms.")
    m3.metric("Rejections it catches", f"{tp / (tp + fn):.1%}",
              help="Of the claims that really were rejected, how many ClaimCheck warned about (recall).")
    m4.metric("Gives the right reason", f"{(ac[ac != 'D99'] == pc[ac != 'D99']).mean():.1%}",
              help="How often it predicts both the outcome and the exact rejection reason, leaving out "
                   "'held for manual review', which no rule can predict.")
    st.info("The rejections it misses are almost all **'held by the payer for manual review'** (D99): the payer decides "
            "those by hand, so no rule can predict them.")

    with st.expander("Details for reviewers: accuracy by reason, and every claim it got wrong"):
        rows = []
        for code in sorted(set(ac) | set(pc)):
            a_, p_ = ac == code, pc == code
            rows.append({"reason": "paid" if code == "PAID" else f"{REASON.get(code, code)} ({code})",
                         "really happened": int(a_.sum()), "predicted": int(p_.sum()), "both": int((a_ & p_).sum()),
                         "caught": f"{(a_ & p_).sum() / a_.sum():.0%}" if a_.sum() else "—",
                         "right when predicted": f"{(a_ & p_).sum() / p_.sum():.0%}" if p_.sum() else "—"})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.markdown("**What really happened (rows) vs what ClaimCheck predicted (columns)**. Numbers on the diagonal are correct.")
        st.dataframe(pd.crosstab(ac.rename("really happened"), pc.rename("predicted")))
        st.markdown("**Claims it got wrong**")
        mm = df[ac != pc]
        st.dataframe(pd.DataFrame({
            "claim": mm.claim_id, "payer": mm.payer_id.map(payer_name), "visit date": mm.dos.dt.date,
            "really": mm.denial_code.fillna("paid"), "payer's note": mm.remark,
            "predicted": mm.pred_code.fillna("paid"), "ClaimCheck's reason": mm.reason,
        }), hide_index=True, use_container_width=True)

# =====================================================================
# PAYER RULES
# =====================================================================
ROUNDING = {
    "eight_minute_rule": "Time is billed in 15-minute units. Leftover minutes of 8 or more count as one more unit.",
    "whole_units_only": "Time is billed in 15-minute units. Only full units count: leftover minutes are not paid.",
}


def when(frm, to) -> str:
    fmt = lambda d: pd.Timestamp(d).strftime("%d %b %Y")
    if frm and to:
        return f"visits {fmt(frm)} to {fmt(to)}"
    if frm:
        return f"visits from {fmt(frm)}"
    if to:
        return f"visits up to {fmt(to)}"
    return "all visits"


with tab_rules:
    st.markdown("Every rule ClaimCheck checks claims against. Each comes from a payer document, so anyone can check it. "
                "Rules have dates because payers change them: a claim is judged on the rules in force **on the day of the visit**.")
    with st.expander("What the columns mean"):
        st.markdown("""
- **Payer**: who the rule belongs to.
- **Rule**: the kind of rule (deadline, rounding, billing tag...).
- **What it says**: the rule in plain words.
- **Applies to**: which visits it covers, by visit date. A rule that was replaced has an end date.
- **If broken, rejected as**: the reason the payer gives.
- **Comes from**: the document and section it was taken from.
- **Confirmed?**: ✅ written in a payer document. ⚠️ worked out by ClaimCheck from past rejections; it should be confirmed with the payer.
""")
    rows = []

    def add(pid, rule, says, applies, code, source):
        rows.append({"payer": payer_name(pid), "rule": rule, "what it says": says, "applies to": applies,
                     "if broken, rejected as": REASON.get(code, code), "comes from": source,
                     "confirmed?": "⚠️ unconfirmed" if "inferred" in source else "✅ yes"})

    for pid, p in rules["payers"].items():
        add(pid, "Visit record", "Every visit must have a verified visit record (EVV).", "all visits", "D01", "payer manual")
        add(pid, "Authorization", "Every visit must be covered by an authorization with hours left.", "all visits", "D07", "payer manual")
        add(pid, "Rounding", ROUNDING[p["rounding"]], "all visits", "D09", p["rounding_source"])
        for t in p["timely_filing"]:
            add(pid, "Deadline", f"Send within {t['days']} days of the visit.", when(t["dos_from"], t["dos_to"]), "D03", t["source"])
        if not p["timely_filing"]:
            add(pid, "Deadline", "No deadline for sending.", "all visits", "D03", "payer manual")
        if p["daily_max_units"]:
            u = p["daily_max_units"]["units"]
            add(pid, "Daily limit", f"At most {u} units ({u // 4} hours) per patient per day, all services together.",
                "all visits", "D06", p["daily_max_units"]["source"])
        for m in p["modifiers"]:
            days = " on weekdays" if m.get("weekdays_only") else " on weekends" if m.get("weekends_only") else ""
            add(pid, "Billing tag", f"{SERVICE.get(m['service'], m['service'])} needs tag {m['required']}{days}.",
                when(m["dos_from"], m["dos_to"]), "D04", m["source"])
        for c in p["contract_rules"]:
            add(pid, "Contract", c["description"], when(c["dos_from"], c.get("dos_to")), c["denial_code"], c["source"])
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, height=560)
    st.caption(f"Rule list version {rules['version']}, approved {rules['approved_on']}. Drafted by Claude from the payer "
               "documents and checked by a person against the documents and past claims.")
    st.markdown("**When a claim breaks several rules**, the payer only reports one reason. Past claims show the order "
                "payers check in (first match is reported):  \n"
                + " → ".join(REASON.get(c, c) for c in engine.precedence))
