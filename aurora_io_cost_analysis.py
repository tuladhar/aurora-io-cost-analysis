#!/usr/bin/env python3
"""
Estimate compute/storage/I/O spend for every Aurora cluster in a region,
and the projected savings from switching to Aurora I/O-Optimized storage.

Uses the local `aws` CLI (already-authenticated profile) for both
resource discovery (RDS, CloudWatch) and on-demand pricing lookups.
This is an ESTIMATE built from current usage snapshots, not a replacement
for actual Cost Explorer billing data.

Usage:
    python3 aurora_io_cost_analysis.py --profile my-profile --region ap-southeast-1
    python3 aurora_io_cost_analysis.py --profile my-profile --region ap-southeast-1 --io-lookback-days 14
    python3 aurora_io_cost_analysis.py --profile my-profile --region ap-southeast-1 --output html
"""

import argparse
import datetime
import html
import json
import subprocess
import sys

HOURS_PER_MONTH = 730
DAYS_PER_MONTH = 30
IO_OPTIMIZED_RECOMMEND_THRESHOLD_PCT = 25

ENGINE_TO_PRICING_NAME = {
    "aurora-postgresql": "Aurora PostgreSQL",
    "aurora-mysql": "Aurora MySQL",
}


def log(msg):
    print(msg, flush=True)


def aws(profile, region, *args):
    cmd = ["aws", "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    if region:
        cmd += ["--region", region]
    cmd += list(args)
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else {}


def aws_pricing(*args):
    # Pricing API is only queryable from us-east-1, regardless of target region.
    cmd = ["aws", "--output", "json", "--region", "us-east-1", "pricing", "get-products"] + list(args)
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"aws pricing get-products failed: {out.stderr.strip()}")
    data = json.loads(out.stdout)
    return [json.loads(p) for p in data.get("PriceList", [])]


def first_price_per_unit(price_docs):
    if not price_docs:
        return None
    terms = price_docs[0]["terms"].get("OnDemand", {})
    for term in terms.values():
        for dim in term["priceDimensions"].values():
            return float(dim["pricePerUnit"]["USD"])
    return None


def get_instance_price(region, engine_name, instance_class, io_optimized):
    storage_attr = "Aurora IO Optimization Mode" if io_optimized else "EBS Only"
    filters = [
        f"Type=TERM_MATCH,Field=instanceType,Value={instance_class}",
        f"Type=TERM_MATCH,Field=databaseEngine,Value={engine_name}",
        f"Type=TERM_MATCH,Field=regionCode,Value={region}",
        "Type=TERM_MATCH,Field=deploymentOption,Value=Single-AZ",
        f"Type=TERM_MATCH,Field=storage,Value={storage_attr}",
    ]
    docs = aws_pricing("--service-code", "AmazonRDS", "--filters", *filters)
    return first_price_per_unit(docs)


def get_storage_price(region, engine_name, io_optimized):
    volume_type = "IO Optimized-Aurora" if io_optimized else "General Purpose-Aurora"
    filters = [
        f"Type=TERM_MATCH,Field=regionCode,Value={region}",
        f"Type=TERM_MATCH,Field=databaseEngine,Value={engine_name}",
        "Type=TERM_MATCH,Field=productFamily,Value=Database Storage",
        f"Type=TERM_MATCH,Field=volumeType,Value={volume_type}",
    ]
    docs = aws_pricing("--service-code", "AmazonRDS", "--filters", *filters)
    return first_price_per_unit(docs)


def get_io_price(region, engine_name):
    filters = [
        f"Type=TERM_MATCH,Field=regionCode,Value={region}",
        f"Type=TERM_MATCH,Field=databaseEngine,Value={engine_name}",
        "Type=TERM_MATCH,Field=productFamily,Value=System Operation",
        "Type=TERM_MATCH,Field=group,Value=Aurora I/O Operation",
    ]
    docs = aws_pricing("--service-code", "AmazonRDS", "--filters", *filters)
    return first_price_per_unit(docs)  # USD per single IO request


def cw_stat(profile, region, cluster_id, metric, stat, start, end, period):
    resp = aws(
        profile, region, "cloudwatch", "get-metric-statistics",
        "--namespace", "AWS/RDS",
        "--metric-name", metric,
        "--dimensions", f"Name=DBClusterIdentifier,Value={cluster_id}",
        "--start-time", start,
        "--end-time", end,
        "--period", str(period),
        "--statistics", stat,
    )
    points = resp.get("Datapoints", [])
    if not points:
        return 0.0
    return sum(p.get(stat, 0.0) for p in points)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def render_ascii_table(headers, rows):
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))

    def sep():
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(vals, align="left"):
        cells = []
        for v, w in zip(vals, widths):
            s = str(v)
            cells.append(f" {s.ljust(w)} " if align == "left" else f" {s.rjust(w)} ")
        return "|" + "|".join(cells) + "|"

    out = [sep(), line(headers), sep()]
    for r in rows:
        out.append(line(r, align="right"))
    out.append(sep())
    return "\n".join(out)


def render_html_report(meta, headers, data_rows, total_row, notes, threshold_pct, lookback_days):
    esc = html.escape

    def cell(v, numeric):
        return f'<td class="num">{esc(str(v))}</td>' if numeric else f"<td>{esc(str(v))}</td>"

    header_html = "".join(f"<th>{esc(h)}</th>" for h in headers)

    body_rows_html = []
    for r in data_rows:
        recommend = r[-1]
        badge = (
            '<span class="badge badge-yes">YES</span>' if recommend == "YES"
            else '<span class="badge badge-no">no</span>'
        )
        cells = "".join(cell(v, i > 0 and i < len(r) - 1) for i, v in enumerate(r[:-1]))
        body_rows_html.append(f"<tr>{cells}<td>{badge}</td></tr>")

    total_cells = "".join(cell(v, i > 0) for i, v in enumerate(total_row))
    total_row_html = f"<tr class=\"total-row\">{total_cells}</tr>"

    notes_html = ""
    if notes:
        items = "".join(f"<li><strong>{esc(n['cluster'])}:</strong> {esc(n['note'])}</li>" for n in notes)
        notes_html = f'<div class="notes"><strong>Notes:</strong><ul>{items}</ul></div>'

    generated_at = meta["generated_at"]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Aurora I/O Cost Analysis</title>
<style>
  :root {{
    --bg: #ffffff; --fg: #1a1a1a; --muted: #666666; --border: #d9dce1;
    --head-bg: #f4f6f8; --total-bg: #eef2f7; --accent: #0b5fff;
    --yes-bg: #d7f3e0; --yes-fg: #14683a; --no-bg: #eceff2; --no-fg: #5a626b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #16181d; --fg: #e8eaed; --muted: #9aa1ab; --border: #33383f;
      --head-bg: #1f232a; --total-bg: #23272f; --accent: #6ea8ff;
      --yes-bg: #123a25; --yes-fg: #7fe3a4; --no-bg: #262a30; --no-fg: #a3aab3;
    }}
  }}
  body {{
    background: var(--bg); color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    margin: 0; padding: 2rem; line-height: 1.5;
  }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  .meta {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.5rem; }}
  .meta div {{ margin: 0.1rem 0; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; font-size: 0.9rem; white-space: nowrap; }}
  th, td {{ border: 1px solid var(--border); padding: 0.5rem 0.7rem; text-align: left; }}
  th {{ background: var(--head-bg); font-weight: 600; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr.total-row {{ background: var(--total-bg); font-weight: 600; }}
  .badge {{ display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.8rem; font-weight: 600; }}
  .badge-yes {{ background: var(--yes-bg); color: var(--yes-fg); }}
  .badge-no {{ background: var(--no-bg); color: var(--no-fg); }}
  .legend, .threshold-note, .estimate-note, .notes {{
    margin-top: 1.5rem; padding: 1rem 1.2rem; border: 1px solid var(--border); border-radius: 8px;
    background: var(--head-bg); font-size: 0.88rem;
  }}
  .legend dt {{ font-weight: 600; float: left; width: 130px; clear: left; }}
  .legend dd {{ margin-left: 140px; margin-bottom: 0.4rem; color: var(--fg); }}
  .threshold-note {{ border-color: var(--accent); }}
  .estimate-note {{ color: var(--muted); font-style: italic; }}
  footer {{ margin-top: 2rem; color: var(--muted); font-size: 0.85rem; border-top: 1px solid var(--border); padding-top: 1rem; }}
</style>
</head>
<body>
  <h1>Aurora I/O Cost Analysis</h1>
  <div class="meta">
    <div><strong>Account/Identity:</strong> {esc(meta['who'])}</div>
    <div><strong>Profile:</strong> {esc(meta['profile'])}</div>
    <div><strong>Region:</strong> {esc(meta['region'])}</div>
    <div><strong>Generated:</strong> {esc(generated_at)}</div>
  </div>

  <div class="table-wrap">
    <table>
      <thead><tr>{header_html}</tr></thead>
      <tbody>
        {''.join(body_rows_html)}
        {total_row_html}
      </tbody>
    </table>
  </div>

  {notes_html}

  <div class="legend">
    <strong>Legend</strong>
    <dl>
      <dt>Inst</dt><dd>Number of DB instances in the cluster</dd>
      <dt>Compute$</dt><dd>Est. monthly on-demand instance cost, standard storage mode</dd>
      <dt>Storage$</dt><dd>Est. monthly storage cost (consumed GB &times; standard rate)</dd>
      <dt>I/O$</dt><dd>Est. monthly I/O request cost, standard mode ($ per 1M requests)</dd>
      <dt>I/O%</dt><dd>I/O$ as a percentage of this cluster's Std Total$</dd>
      <dt>Std Total$</dt><dd>Compute$ + Storage$ + I/O$ (current pricing model: Aurora Standard)</dd>
      <dt>IOOpt Total$</dt><dd>Est. total cost if this cluster used Aurora I/O-Optimized storage (higher compute + storage rate, but $0 per-request I/O charge)</dd>
      <dt>Savings$</dt><dd>Std Total$ &minus; IOOpt Total$ (positive = switching saves money)</dd>
      <dt>Recommend?</dt><dd>YES if I/O% is at or above the AWS-suggested switch threshold</dd>
    </dl>
  </div>

  <div class="threshold-note">
    AWS generally recommends Aurora I/O-Optimized once I/O makes up roughly <strong>{threshold_pct}%</strong> or more
    of a cluster's Aurora Standard bill &mdash; below that threshold, I/O-Optimized's higher compute/storage rate
    typically costs more overall.
  </div>

  <div class="estimate-note">
    Estimate based on a {lookback_days}-day CloudWatch I/O sample extrapolated to 30 days, and current on-demand
    pricing. This is not actual Cost Explorer billing data.
  </div>

  <footer>Created by Puru Tuladhar (aws@purutuladhar.com)</footer>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=None, help="AWS CLI profile to use")
    parser.add_argument("--region", required=True, help="AWS region, e.g. ap-southeast-1")
    parser.add_argument("--io-lookback-days", type=int, default=7,
                         help="Days of CloudWatch history to average I/O from (default: 7)")
    parser.add_argument("--output", choices=["text", "html"], default="text",
                         help="Output format: 'text' prints an ASCII table to the terminal (default), "
                              "'html' writes a shareable HTML report file")
    parser.add_argument("--output-file", default=None,
                         help="Path to write the HTML report to (only used with --output html; "
                              "default: aurora_io_cost_report.html)")
    args = parser.parse_args()

    profile_label = args.profile or "default"
    try:
        identity = aws(args.profile, args.region, "sts", "get-caller-identity")
        who = identity.get("Arn", "unknown identity")
    except RuntimeError as e:
        log(f"Warning: could not resolve caller identity ({e})")
        who = "unknown identity"

    log(f"Running as: {who}")
    log(f"Profile:    {profile_label}")
    log(f"Region:     {args.region}")
    log("")
    log("Discovering Aurora clusters...")

    end = datetime.datetime.now(datetime.timezone.utc)
    io_start = end - datetime.timedelta(days=args.io_lookback_days)
    storage_start = end - datetime.timedelta(days=1)

    clusters = aws(args.profile, args.region, "rds", "describe-db-clusters").get("DBClusters", [])
    aurora_clusters = [c for c in clusters if c.get("Engine") in ENGINE_TO_PRICING_NAME]

    if not aurora_clusters:
        log(f"No Aurora clusters found in {args.region}.")
        return

    cluster_ids = [c["DBClusterIdentifier"] for c in aurora_clusters]
    log(f"Found {len(aurora_clusters)} Aurora cluster(s): {', '.join(cluster_ids)}")
    log("")
    log(f"Gathering CloudWatch metrics (I/O lookback window: {args.io_lookback_days} day(s) "
        f"-- use --io-lookback-days N to change this)...")

    instances = aws(args.profile, args.region, "rds", "describe-db-instances").get("DBInstances", [])
    instances_by_cluster = {}
    for i in instances:
        cid = i.get("DBClusterIdentifier")
        if cid:
            instances_by_cluster.setdefault(cid, []).append(i["DBInstanceClass"])

    price_cache = {}

    def price(fn, *key):
        if key not in price_cache:
            price_cache[key] = fn(*key)
        return price_cache[key]

    rows = []
    for idx, c in enumerate(aurora_clusters, start=1):
        cluster_id = c["DBClusterIdentifier"]
        log(f"  [{idx}/{len(aurora_clusters)}] {cluster_id}: fetching storage + I/O usage...")
        engine_name = ENGINE_TO_PRICING_NAME[c["Engine"]]
        instance_classes = instances_by_cluster.get(cluster_id, [])

        # Storage: latest average consumed storage, in GB (decimal).
        storage_bytes = cw_stat(args.profile, args.region, cluster_id, "VolumeBytesUsed", "Average",
                                 iso(storage_start), iso(end), 86400)
        storage_gb = storage_bytes / 1e9

        # I/O: total read+write ops over the lookback window, extrapolated to a 30-day month.
        period_seconds = args.io_lookback_days * 86400
        reads = cw_stat(args.profile, args.region, cluster_id, "VolumeReadIOPs", "Sum",
                         iso(io_start), iso(end), period_seconds)
        writes = cw_stat(args.profile, args.region, cluster_id, "VolumeWriteIOPs", "Sum",
                          iso(io_start), iso(end), period_seconds)
        monthly_io_requests = (reads + writes) * (DAYS_PER_MONTH / args.io_lookback_days)

        io_price = price(get_io_price, args.region, engine_name)
        io_cost = monthly_io_requests * io_price if io_price else 0.0

        std_storage_price = price(get_storage_price, args.region, engine_name, False)
        ioopt_storage_price = price(get_storage_price, args.region, engine_name, True)
        std_storage_cost = storage_gb * (std_storage_price or 0.0)
        ioopt_storage_cost = storage_gb * (ioopt_storage_price or 0.0)

        std_compute_cost = 0.0
        ioopt_compute_cost = 0.0
        skipped_classes = set()
        for ic in instance_classes:
            if ic.startswith("db.serverless"):
                skipped_classes.add(ic)
                continue
            std_hr = price(get_instance_price, args.region, engine_name, ic, False)
            ioopt_hr = price(get_instance_price, args.region, engine_name, ic, True)
            std_compute_cost += (std_hr or 0.0) * HOURS_PER_MONTH
            ioopt_compute_cost += (ioopt_hr or 0.0) * HOURS_PER_MONTH

        standard_total = std_compute_cost + std_storage_cost + io_cost
        ioopt_total = ioopt_compute_cost + ioopt_storage_cost
        savings = standard_total - ioopt_total
        io_share = (io_cost / standard_total * 100) if standard_total else 0.0
        recommend = io_share >= IO_OPTIMIZED_RECOMMEND_THRESHOLD_PCT

        rows.append({
            "cluster": cluster_id,
            "instances": len(instance_classes) or 0,
            "compute": std_compute_cost,
            "storage": std_storage_cost,
            "io": io_cost,
            "io_share_pct": io_share,
            "standard_total": standard_total,
            "ioopt_total": ioopt_total,
            "savings": savings,
            "recommend": recommend,
            "note": f"skipped pricing for: {', '.join(skipped_classes)}" if skipped_classes else "",
        })

    log("")
    log("Fetching AWS on-demand pricing (compute/storage/I/O rates)...")
    log("Done. Building report...")
    log("")

    rows.sort(key=lambda r: r["savings"], reverse=True)

    headers = ["Cluster", "Inst", "Compute$", "Storage$", "I/O$", "I/O%",
               "Std Total$", "IOOpt Total$", "Savings$", "Recommend?"]
    table_rows = []
    totals = {"compute": 0.0, "storage": 0.0, "io": 0.0, "standard_total": 0.0, "ioopt_total": 0.0, "savings": 0.0}
    for r in rows:
        for k in totals:
            totals[k] += r[k]
        table_rows.append([
            r["cluster"], r["instances"],
            f"${r['compute']:.2f}", f"${r['storage']:.2f}", f"${r['io']:.2f}",
            f"{r['io_share_pct']:.0f}%",
            f"${r['standard_total']:.2f}", f"${r['ioopt_total']:.2f}", f"${r['savings']:.2f}",
            "YES" if r["recommend"] else "no",
        ])
    total_row = [
        "TOTAL", "", f"${totals['compute']:.2f}", f"${totals['storage']:.2f}", f"${totals['io']:.2f}", "",
        f"${totals['standard_total']:.2f}", f"${totals['ioopt_total']:.2f}", f"${totals['savings']:.2f}", "",
    ]
    notes = [r for r in rows if r["note"]]

    if args.output == "html":
        output_path = args.output_file or "aurora_io_cost_report.html"
        meta = {
            "who": who,
            "profile": profile_label,
            "region": args.region,
            "generated_at": end.strftime("%Y-%m-%d %H:%M UTC"),
        }
        report = render_html_report(
            meta, headers, table_rows, total_row, notes,
            IO_OPTIMIZED_RECOMMEND_THRESHOLD_PCT, args.io_lookback_days,
        )
        with open(output_path, "w") as f:
            f.write(report)
        log(f"HTML report written to: {output_path}")
        log(f"Total estimated savings if switching flagged clusters to I/O-Optimized: ${totals['savings']:.2f}/mo")
        log("Open it in a browser, or share the file with your team.")
        return

    print(render_ascii_table(headers, table_rows + [total_row]))

    if notes:
        print()
        for r in notes:
            print(f"Note ({r['cluster']}): {r['note']}")

    print()
    print("Legend:")
    print("  Inst          Number of DB instances in the cluster")
    print("  Compute$      Est. monthly on-demand instance cost, standard storage mode")
    print("  Storage$      Est. monthly storage cost (consumed GB x standard rate)")
    print("  I/O$          Est. monthly I/O request cost, standard mode ($ per 1M requests)")
    print("  I/O%          I/O$ as a percentage of this cluster's Std Total$")
    print("  Std Total$    Compute$ + Storage$ + I/O$  (current pricing model: Aurora Standard)")
    print("  IOOpt Total$  Est. total cost if this cluster used Aurora I/O-Optimized storage")
    print("                (higher compute + storage rate, but $0 per-request I/O charge)")
    print("  Savings$      Std Total$ - IOOpt Total$  (positive = switching saves money)")
    print("  Recommend?    YES if I/O% is at or above the AWS-suggested switch threshold")
    print()
    print(f"Note: AWS generally recommends Aurora I/O-Optimized once I/O makes up roughly "
          f"{IO_OPTIMIZED_RECOMMEND_THRESHOLD_PCT}% or more of a cluster's Aurora Standard bill --")
    print("      below that threshold, I/O-Optimized's higher compute/storage rate typically costs more overall.")
    print()
    print(f"(Estimate based on a {args.io_lookback_days}-day CloudWatch I/O sample extrapolated to 30 days,")
    print(" and current on-demand pricing. This is not actual Cost Explorer billing data.)")
    print()
    print("Created by Puru Tuladhar (aws@purutuladhar.com)")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
