"""Build the README graphics for agent_action_approvals.

    python docs/assets/src/build.py
    uv run --with playwright==1.56.0 --with pillow python docs/assets/src/render.py docs/assets/src docs/assets

hero and lifecycle are structural: the status names and transition methods are read from
agent_action_approvals/models.py, so they cannot drift from the code. races and queue render ONLY
from captures/demo_lifecycle.json, a real run of examples/acme_demo/run_demo.sh (a fictional
company, simulated executors, no network).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import readme_kit as k  # noqa: E402

REPO = "AGENT_ACTION_APPROVALS"
MODELS = (ROOT / "agent_action_approvals" / "models.py").read_text(encoding="utf-8")


def statuses() -> list[str]:
    """Status values in declaration order, straight from STATUS_CHOICES."""
    block = MODELS[MODELS.index("STATUS_CHOICES = ["):]
    block = block[: block.index("]")]
    names = re.findall(r"\(([A-Z_]+), \"", block)
    values = dict(re.findall(r"^    ([A-Z_]+) = \"([a-z_]+)\"$", MODELS, re.M))
    out = [values[n] for n in names]
    assert len(out) == 7, f"expected 7 statuses in models.py, found {out}"
    return out


def transitions() -> dict[str, tuple[str, str]]:
    """method -> (from, to), read from each method's _transition(...) call."""
    out = {}
    for m in re.finditer(r"def (\w+)\(self[^)]*\)[^:]*:(.*?)(?=\n    def |\n    @|\Z)", MODELS, re.S):
        name, body = m.group(1), m.group(2)
        frm = re.search(r"self\._transition\(\s*self\.([A-Z_]+)", body)
        to = re.search(r"(?:status=|\"status\": )self\.([A-Z_]+)", body)
        if frm and to:
            out[name] = (frm.group(1).lower(), to.group(1).lower())
    want = {"approve", "decline", "time_out", "mark_executing", "mark_done", "mark_failed"}
    assert want <= set(out), f"missing transitions in models.py: {want - set(out)}"
    return out


def hero() -> str:
    return k.hero(
        "AGENT_ACTION_APPROVALS · DJANGO APP · MIT",
        "Merge this PR.", "Not any PR.",
        "A durable approval queue for agent-initiated actions. The agent enqueues a row, a human decides it, "
        "and a worker runs it <b style='color:var(--ivory);font-weight:600'>only if approved, and at most once</b>.",
        [("A row, not a paused process", "Pending actions survive restarts and redeploys, and stay queryable."),
         ("Every transition is a compare-and-swap", "Two deciders or two workers on one row: exactly one wins."),
         ("Auto-approval ships dark", "Default deny, fail closed, behind a kill switch that starts off.")],
        f"1 TABLE · {len(statuses())} STATES · 0 DEPENDENCIES BEYOND DJANGO",
        k.wheel(statuses(), "ONE ROW", "Queue", size=560, node_r=50),
        f"{REPO} · HOW IT WORKS")


def lifecycle() -> str:
    t = transitions()

    def lab(method: str) -> str:
        return f"{method}()"

    # Boxes: a left-to-right happy path, with the side exits below it.
    boxes = (k.box(56, 250, 210, 150, "PENDING", ["agent enqueued it", "nothing has run"], True)
             + k.box(366, 250, 220, 150, "APPROVED", ["a human or one", "opted-in lane said yes"])
             + k.box(736, 250, 230, 150, "EXECUTING", ["a worker claimed it", "executing_at stamped"], True)
             + k.box(1116, 190, 228, 120, "DONE", ["result stored"])
             + k.box(1116, 360, 228, 120, "FAILED", ["exception +", "traceback stored"])
             + k.box(56, 520, 210, 130, "DECLINED", ["reason stored", "never runs"])
             + k.box(366, 520, 220, 130, "TIMED_OUT", ["expire_pending()", "no decision in time"])
             + k.box(736, 520, 608, 130, "IF THE WORKER PROCESS DIES", ["the row stays EXECUTING with executing_at set.",
                                                                          "it is never retried on its own: the side effect",
                                                                          "may already have happened. resolve it by hand."]))
    a = t["approve"]
    arrows = [
        (266, 325, 356, 325, lab("approve")),
        (586, 325, 726, 325, lab("mark_executing")),
        (966, 268, 1106, 255, lab("mark_done")),
        (966, 388, 1106, 400, lab("mark_failed")),
        (160, 400, 160, 510, lab("decline"), False, "right"),
        (230, 400, 420, 510, lab("time_out"), False, "right"),
        (851, 400, 851, 510, "", True),
    ]
    assert a == ("pending", "approved") and t["mark_executing"] == ("approved", "executing")
    assert t["decline"][1] == "declined" and t["time_out"][1] == "timed_out"
    return k.flow("THE STATE MACHINE", f"Every arrow is one {k.em('conditional')} UPDATE.",
                  "UPDATE ... SET status = <to> WHERE id = <row> AND status = <from>  ·  0 rows updated = InvalidTransition, nothing changes",
                  boxes, arrows, f"{REPO} · TRANSITIONS FROM models.py")


def _section(out: str, n: int) -> list[str]:
    m = re.search(rf"^== {n}\. .*?(?=^== \d+\. |\Z)", out, re.M | re.S)
    assert m, f"section {n} missing from capture"
    return [ln for ln in m.group(0).splitlines() if ln.strip()]


def _short(line: str) -> str:
    line = re.sub(r"approval [0-9a-f-]{36}", "approval <uuid>", line)
    return line


def races() -> str:
    c = k.load_capture(HERE / "captures" / "demo_lifecycle.json")
    out = c["output"]
    s4, s5 = _section(out, 4), _section(out, 5)
    lines = [("h1", "Two deciders, two workers, one row")]
    for sec in (s4, s5):
        lines.append(("h2", k.esc(sec[0].lstrip("= "))))
        for ln in sec[1:]:
            if ln.startswith("$ "):
                lines.append(("code", k.esc(ln)))
            else:
                text = _short(ln.strip())
                text = re.sub(r" \(cannot (\w+) approval <uuid>: (status is now '\w+').*", r"\n      (\2 ...)", text)
                for i, part in enumerate(text.split("\n")):
                    html = k.esc(part)
                    if "REFUSED" in part:
                        html = html.replace("REFUSED", "<b style='color:var(--amber)'>REFUSED</b>")
                    lines.append(("li" if i == 0 else "li2", html))
    lines.append(("m", f"examples/acme_demo · fictional sample data · captured {c['captured_at'][:10]}"))
    notes = [(150, "Both copies were loaded while the row was still pending, like two web requests a second apart."),
             (275, "The second decision hits a conditional UPDATE that matches 0 rows, so it is refused and the row keeps the first decision."),
             (450, "The claim (approved to executing) is the same compare-and-swap. The executor ran once; the second worker never reached it."),
             (595, "Before this fix both decisions landed and the executor ran twice.")]
    return k.anatomy("REAL RUN: THE RACES", lines, notes,
                     f"{REPO} · REAL RUN {c['captured_at'][:10]} · SAMPLE DATA: ACME", doc_width=830)


def queue() -> str:
    c = k.load_capture(HERE / "captures" / "demo_lifecycle.json")
    s9 = _section(c["output"], 9)
    last_list = max(i for i, ln in enumerate(s9) if ln == "$ python manage.py approvals list")
    final = [ln for ln in s9[last_list + 1:] if re.match(r"\s+#\d+\s", ln)]
    assert len(final) == 10, f"expected 10 rows in the final list, found {len(final)}"
    lines = [("h1", "The queue after the demo: every outcome is a row")]
    for ln in final:
        m = re.match(r"\s+(#\d+)\s+(\w+)\s+(\w+)\s+(.*?)( decided_by=.*)?$", ln)
        num, status, atype, title, rest = m.groups()
        color = {"done": "var(--ivory)", "failed": "var(--bad)", "executing": "var(--amber)"}.get(status, "var(--muted)")
        lines.append(("code", f"<span style='color:var(--dim)'>{k.esc(num.ljust(4))}</span>"
                              f"<span style='color:{color}'>{k.esc(status.ljust(10))}</span>{k.esc(title[:44])}"))
    s8 = _section(c["output"], 8)
    lines.append(("h2", "Step 8: auto-approval, kill switch off and then on"))
    for ln in s8:
        if ln.strip().startswith("policy says"):
            lines.append(("li", k.esc(ln.strip())))
    crash = [ln.strip() for ln in s9 if "killed" in ln or "exited with status" in ln or "nothing approved" in ln]
    lines.append(("h2", "Step 9: the worker process was killed mid-executor"))
    for ln in crash:
        lines.append(("li", k.esc(ln)))
    lines.append(("m", f"examples/acme_demo · fictional sample data · captured {c['captured_at'][:10]}"))
    notes = [(120, "Approved, run once, result stored. Rows #1, #5 and #8 (the auto-approved one)."),
             (190, "Declined and timed-out rows never run, and say who or what decided."),
             (250, "A raising executor becomes a FAILED row with the traceback, not a lost job."),
             (330, "Pending rows wait as long as they need to, across restarts. Each step was its own process."),
             (445, "Same lane, same row: denied while the switch is off, then approved. The code PR is denied with the file named."),
             (560, "A killed worker leaves EXECUTING with a timestamp. Nothing retries it, because the merge may already have happened.")]
    return k.anatomy("REAL RUN: THE DURABLE QUEUE", lines, notes,
                     f"{REPO} · REAL RUN {c['captured_at'][:10]} · SAMPLE DATA: ACME", doc_width=800)


if __name__ == "__main__":
    pages = {"hero": hero(), "lifecycle": lifecycle()}
    if (HERE / "captures" / "demo_lifecycle.json").exists():
        pages["races"] = races()
        pages["queue"] = queue()
    k.write_pages(HERE, pages)
