"""Render the findings: three figures and one static page.

Each figure answers one question and is chosen for the job the data does.

`retrieval_curve.png` is change over an ordered sequence, so it is a line chart,
two series (as scanned against normalised) over the twelve eras, with the
out-of-vocabulary rate on its own panel underneath rather than on a second y
axis. The two panels share the era axis and nothing else; putting recall and OOV
on one pair of axes would be the dual-axis mistake.

`answer_grid.png` is identity and state, not magnitude: for each question and
era, did the system answer, abstain, or answer with an anachronism? That is a
status grid, so the states carry a glyph as well as a colour and the legend spells
them out. Nothing here is encoded by colour alone.

`term_life.png` is a span per term, first era to last, with the peak marked. One
series, so the title names it and there is no legend box.

The era axis is equally spaced and categorical rather than a true time axis. The
gaps between editions are uneven and "modern" has no year at all, so equal
spacing is the honest rendering: this is a sequence of twelve snapshots, not a
continuous signal.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from common import ERAS, FIGS, OUT, PROC, TABLES, get_logger  # noqa: E402

log = get_logger("s10")

# From the validated reference palette. Blue and orange pass every hard gate as
# an adjacent pair (CVD dE 24.7, normal-vision 33.6).
SERIES_1 = "#2a78d6"
SERIES_2 = "#eb6834"
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
SURFACE = "#fcfcfb"
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE = "#e1e0d9", "#c3c2b7"


def style() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE, "text.color": INK,
        "axes.labelcolor": INK_2, "axes.edgecolor": BASELINE,
        "xtick.color": MUTED, "ytick.color": MUTED,
        "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False,
        "font.size": 9, "axes.titlesize": 11, "figure.dpi": 150,
        "lines.linewidth": 2.0, "lines.markersize": 5.5,
    })


def read_csv(name: str) -> list[dict]:
    path = TABLES / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def eras_present(rows: list[dict], key: str = "era") -> list[str]:
    seen = {r[key] for r in rows}
    return [e for e in ERAS if e in seen]


def figure_retrieval_curve() -> None:
    abl = read_csv("retrieval_ablation.csv")
    seg = read_csv("segmentation_stats.csv")
    if not abl:
        log.warning("no retrieval_ablation.csv: run s07")
        return
    eras = eras_present(abl)
    x = range(len(eras))

    def series(variant: str, field: str) -> list[float | None]:
        by_era = {r["era"]: float(r[field]) for r in abl if r["variant"] == variant}
        return [by_era.get(e) for e in eras]

    oov_before: dict[str, list[float]] = defaultdict(list)
    oov_after: dict[str, list[float]] = defaultdict(list)
    for r in seg:
        oov_before[r["era"]].append(float(r["oov_before"]))
        oov_after[r["era"]].append(float(r["oov_after"]))

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8.4, 6.4), sharex=True,
                                   gridspec_kw={"height_ratios": [1.35, 1]})
    for variant, colour, label in (("raw", SERIES_2, "as scanned"),
                                   ("norm", SERIES_1, "normalised")):
        ys = series(variant, "recall_at_10")
        pts = [(i, y) for i, y in zip(x, ys) if y is not None]
        ax1.plot([p[0] for p in pts], [p[1] for p in pts], marker="o",
                 color=colour, label=label, zorder=3,
                 markeredgecolor=SURFACE, markeredgewidth=1.2)
        if pts:
            i, y = pts[-1]
            ax1.annotate(f"{y:.2f}", (i, y), textcoords="offset points",
                         xytext=(6, 0), color=colour, fontsize=9,
                         va="center", fontweight="bold")
    ax1.set_ylabel("recall@10")
    ax1.set_title("Finding the right entry from a modern description, by era",
                  loc="left", color=INK)
    ax1.set_ylim(0, 1.05)
    ax1.grid(axis="y", zorder=0)
    ax1.legend(frameon=False, loc="upper left", labelcolor=INK_2)

    for source, colour, label in ((oov_before, SERIES_2, "as scanned"),
                                  (oov_after, SERIES_1, "normalised")):
        pts = [(i, 100 * sum(source[e]) / len(source[e]))
               for i, e in enumerate(eras) if source.get(e)]
        if pts:
            ax2.plot([p[0] for p in pts], [p[1] for p in pts], marker="o",
                     color=colour, label=label, zorder=3,
                     markeredgecolor=SURFACE, markeredgewidth=1.2)
    ax2.set_ylabel("out of vocabulary (%)")
    ax2.set_title("The same boundary, measured a different way: the long s leaving print",
                  loc="left", color=INK)
    ax2.grid(axis="y", zorder=0)
    ax2.set_xticks(list(x))
    ax2.set_xticklabels(eras, rotation=45, ha="right")
    ax2.legend(frameon=False, loc="upper right", labelcolor=INK_2)

    for ax in (ax1, ax2):
        ax.axvspan(-0.4, 5.4, color=GRID, alpha=0.45, zorder=0, lw=0)
    ax1.annotate("long s in use", (2.5, 0.96), color=MUTED, ha="center", fontsize=8)

    fig.tight_layout()
    out = FIGS / "retrieval_curve.png"
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


def figure_answer_grid() -> None:
    rows = read_csv("question_timeline.csv")
    if not rows:
        log.warning("no question_timeline.csv: run s08 then s09")
        return
    gens = sorted({r["generator"] for r in rows})
    questions = list(dict.fromkeys(r["question"] for r in rows))
    eras = eras_present(rows)

    fig, axes = plt.subplots(1, len(gens), figsize=(4.6 + 4.6 * len(gens), 4.6),
                             squeeze=False, sharey=True)
    for ax, gen in zip(axes[0], gens):
        cell = {(r["question"], r["era"]): r for r in rows if r["generator"] == gen}
        for yi, q in enumerate(questions):
            for xi, era in enumerate(eras):
                r = cell.get((q, era))
                if not r:
                    continue
                if r["mode"].startswith("abstain"):
                    colour, glyph = MUTED, "–"          # en dash: declined
                elif int(r["n_anachronisms"]) > 0:
                    colour, glyph = CRITICAL, "×"       # cross: flagged
                else:
                    colour, glyph = GOOD, "●"           # dot: answered
                ax.scatter(xi, yi, s=190, color=colour, zorder=2,
                           edgecolor=SURFACE, linewidth=1.4)
                ax.annotate(glyph, (xi, yi), color=SURFACE, ha="center",
                            va="center", fontsize=8, zorder=3)
        ax.set_xticks(range(len(eras)))
        ax.set_xticklabels(eras, rotation=45, ha="right")
        ax.set_yticks(range(len(questions)))
        ax.set_yticklabels(questions)
        ax.set_xlim(-0.6, len(eras) - 0.4)
        ax.set_ylim(-0.6, len(questions) - 0.4)
        ax.set_title(f"{gen} generator", loc="left", color=INK)
        ax.grid(color=GRID, zorder=0)
        ax.invert_yaxis()

    handles = [plt.Line2D([], [], marker="o", linestyle="", color=c, markersize=9,
                          label=lab)
               for c, lab in ((GOOD, "answered ●"),
                              (CRITICAL, "answered, anachronism flagged ×"),
                              (MUTED, "abstained –"))]
    axes[0][0].legend(handles=handles, frameon=False, loc="upper left",
                      bbox_to_anchor=(0, -0.22), ncol=3, labelcolor=INK_2)
    fig.suptitle("What each era could be asked, and whether the answer stayed in period",
                 x=0.01, ha="left", color=INK, fontsize=11)
    fig.tight_layout(rect=(0, 0.06, 1, 0.96))
    out = FIGS / "answer_grid.png"
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


def figure_term_life() -> None:
    """Terms that arrive and terms that leave, on one era axis.

    The first version of this drew every term the questions turn on and was
    useless: *motion*, *theory*, *fluid* and *particle* are ordinary English and
    span all twelve eras, so the chart was twenty identical full-width bars. Only
    the terms whose span is *partial* carry information, so those are the ones
    drawn, in two groups. Arrivals come from the question set's own hypotheses;
    departures come from the dying-terms table, which is the same measurement
    pointed the other way.
    """
    arrivals = [r for r in read_csv("term_first_appearance.csv")
                if r["first_era"] and int(r["n_eras"]) < len(ERAS)]
    # The table keeps anything attested twice in the proofread 1911 layer, which
    # is right for a table and too loose for a picture: at that threshold the top
    # of the list still carries scanner residue. Ten occurrences is enough to be
    # confident the word is a word, and it is what the figure draws.
    departures = [r for r in read_csv("dying_terms.csv")
                  if r.get("first_era") and int(r.get("attested_1911", 0)) >= 10][:14]
    if not arrivals and not departures:
        log.warning("nothing to draw: run s09")
        return
    order = {e: i for i, e in enumerate(ERAS)}
    arrivals.sort(key=lambda r: order.get(r["first_era"], 0))
    departures.sort(key=lambda r: -float(r["early_per_million"]))

    items = ([(r["term"], r["question"], r["first_era"], r["last_era"],
               r["peak_era"], SERIES_1) for r in arrivals[:14]]
             + [(r["term"], "fell out of use", r["first_era"], r["last_era"],
                 r["peak_era"], SERIES_2) for r in departures])
    fig, ax = plt.subplots(figsize=(8.6, 0.32 * len(items) + 1.9))
    for yi, (term, note, first, last, peak, colour) in enumerate(items):
        a, b = order.get(first, 0), order.get(last, len(ERAS) - 1)
        ax.plot([a, b], [yi, yi], color=colour, alpha=0.55, lw=3.5, zorder=2,
                solid_capstyle="round")
        ax.scatter([order.get(peak, a)], [yi], s=70, color=colour, zorder=3,
                   edgecolor=SURFACE, linewidth=1.2)
        ax.annotate(note, (len(ERAS) - 0.4, yi), color=MUTED, fontsize=7.5,
                    va="center")
    ax.set_yticks(range(len(items)))
    ax.set_yticklabels([i[0] for i in items], fontsize=8.5)
    ax.set_xticks(range(len(ERAS)))
    ax.set_xticklabels(ERAS, rotation=45, ha="right")
    ax.set_xlim(-0.5, len(ERAS) + 1.8)
    ax.invert_yaxis()
    ax.grid(axis="x", zorder=0)
    handles = [plt.Line2D([], [], color=c, lw=3.5, label=lab) for c, lab in
               ((SERIES_1, "entered the corpus later than 1771"),
                (SERIES_2, "used early, gone by 1842"))]
    ax.legend(handles=handles, frameon=False, loc="lower right", labelcolor=INK_2,
              fontsize=8)
    ax.set_title("Words that arrive and words that leave (dot marks peak use)",
                 loc="left", color=INK)
    fig.tight_layout()
    out = FIGS / "term_life.png"
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


def render_page() -> None:
    """One static page: the figures, then every answer with its citations."""
    rows = read_csv("question_timeline.csv")
    dying = read_csv("dying_terms.csv")[:40]
    by_q: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_q[r["question"]].append(r)

    parts = ["""<!doctype html><meta charset="utf-8">
<title>Britannica Time Machine</title>
<style>
 body{background:#fcfcfb;color:#0b0b0b;font:14px/1.55 -apple-system,Segoe UI,sans-serif;
      margin:0 auto;padding:28px 20px;max-width:960px}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:32px 0 8px}
 p.sub{color:#52514e;margin:0 0 20px}
 img{max-width:100%;border:1px solid #e1e0d9;border-radius:6px;margin:8px 0 4px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 td,th{border-bottom:1px solid #e1e0d9;padding:5px 7px;text-align:left;
       vertical-align:top}
 th{color:#52514e;font-weight:600}
 .era{color:#898781;white-space:nowrap;font-variant-numeric:tabular-nums}
 .cite{color:#898781;font-size:11px}
 .abstain{color:#898781;font-style:italic}
 .flag{color:#d03b3b;font-size:11px}
 code{background:#f2f1ec;padding:1px 4px;border-radius:3px;font-size:12px}
</style>
<h1>Britannica Time Machine</h1>
<p class="sub">One question asked of twelve encyclopaedias, 1771 to now, each
answering only from what it contains.</p>"""]

    for name, caption in (
            ("retrieval_curve.png", "Retrieval degrades backwards in time, and the "
             "gain from normalising the OCR stops exactly where the long s does."),
            ("answer_grid.png", "Whether each era could answer, and whether the "
             "answer used words that era never printed."),
            ("term_life.png", "When each term the questions turn on entered the "
             "corpus, and when it left.")):
        if (FIGS / name).exists():
            parts.append(f'<h2>{html.escape(caption)}</h2>'
                         f'<img src="figures/{name}" alt="{html.escape(caption)}">')

    for q, items in by_q.items():
        parts.append(f"<h2>{html.escape(q)}</h2><table>"
                     "<tr><th>era</th><th>answer</th></tr>")
        for r in sorted(items, key=lambda r: ERAS.index(r["era"])
                        if r["era"] in ERAS else 99):
            if r["generator"] != items[0]["generator"]:
                continue
            if r["mode"].startswith("abstain"):
                body = '<span class="abstain">no entry in this edition answers it</span>'
            else:
                body = html.escape(r["answer"][:700])
                if r["citations"]:
                    body += f'<div class="cite">{html.escape(r["citations"])}</div>'
                if r["n_anachronisms"] and int(r["n_anachronisms"]):
                    body += (f'<div class="flag">out of period: '
                             f'{html.escape(r["anachronisms"])}</div>')
            parts.append(f'<tr><td class="era">{r["era"]}</td><td>{body}</td></tr>')
        parts.append("</table>")

    if dying:
        parts.append("<h2>Words the early editions used and the later ones "
                     "stopped using</h2><table><tr><th>term</th>"
                     "<th>per million, 1771 to 1817</th><th>per million, 1842 on</th>"
                     "</tr>")
        for r in dying:
            parts.append(f'<tr><td><code>{html.escape(r["term"])}</code></td>'
                         f'<td>{r["early_per_million"]}</td>'
                         f'<td>{r["late_per_million"]}</td></tr>')
        parts.append("</table>")

    out = OUT / "report.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    log.info("wrote %s", out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="curve, grid, terms, page")
    args = ap.parse_args()
    only = {p.strip() for p in args.only.split(",") if p.strip()}
    style()
    if not only or "curve" in only:
        figure_retrieval_curve()
    if not only or "grid" in only:
        figure_answer_grid()
    if not only or "terms" in only:
        figure_term_life()
    if not only or "page" in only:
        render_page()
    return 0


if __name__ == "__main__":
    sys.exit(main())
