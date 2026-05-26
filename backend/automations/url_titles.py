"""Built-in list-consuming workflow: fetch each URL's page title.

Consumes an input dataset (rows with a ``url`` — or ``target_url``/``profile_url``)
and visits each, tolerantly, recording the page title. This is the second half of
a pipeline: feed it the (projected) output of another workflow. The same shape as
a "connect each profile" / "message each lead" workflow, minus the account risk.
"""
from __future__ import annotations

import asyncio

from automations import userkit


async def run(params, sess, inputs):
    out = []
    total = len(inputs)
    for i, row in enumerate(inputs, 1):
        url = str(row.get("url") or row.get("target_url") or row.get("profile_url") or "").strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        title, ok = "", "no"
        try:
            await sess.goto(url)
            await sess.sleep(400)
            title = (await sess.evaluate("() => document.title")) or ""
            ok = "yes"
        except Exception as e:  # tolerant per-row: a bad URL never sinks the run
            userkit.log(f"[url-titles] {url} failed: {e}")
        out.append({"url": url, "title": title, "ok": ok})
        userkit.progress(i, total, message=f"{i}/{total} {url}", url=url)
        await asyncio.sleep(0.3)
    return out


def main(argv=None):
    params, server, output = userkit.parse(argv)
    inputs = userkit.input_rows(argv)
    if not inputs:
        userkit.error("no input rows — bind an input dataset of URLs to this run")
        userkit.write_csv(output, [], ["url", "title", "ok"])
        return 1
    rows = userkit.run_session(lambda p, s: run(p, s, inputs), params, server)
    userkit.write_csv(output, rows, ["url", "title", "ok"])
    return 0
