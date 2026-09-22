"""Real-browser regression review against isolated, running FastAPI applications.

Usage: python scripts/ui_review.py --axe /path/to/axe.min.js --output /tmp/ui-review
The browser and axe are test-only dependencies. Production has no Node toolchain.
"""

import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Capture the product and exercise complete reviewer workflows, not mock markup."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--axe", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/tmp/proofline-review"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--browser", choices=("chromium", "firefox", "webkit"), default="chromium")
    parser.add_argument("--port-base", type=int, default=8400)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    processes, handles, checks, audits, snapshots, page_errors = [], [], [], [], [], []
    result = {"browser": args.browser, "checks": checks, "axe_audits": audits,
              "screenshots": snapshots, "page_errors": page_errors, "limitations": [
                  "Synthetic adapters and isolated databases; no live remediation was launched.",
                  "Automated accessibility checks are not a manual screen-reader certification.",
                  "Navigation timings are local-runner observations, not production performance."]}

    def check(name: str, condition: bool) -> None:
        """Record one review assertion and stop immediately when it fails."""

        # ELI5: keep every check in the JSON result while making failures visible to the runner.
        checks.append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)

    def serve(scenario: str, offset: int, source: Path = ROOT) -> str:
        """Launch a real process. PYTHONPATH deliberately selects the requested checkout."""
        log = open(output / f"server-{offset}.log", "w")
        handles.append(log)
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/serve_review.py"), "--scenario", scenario,
             "--port", str(args.port_base + offset)], cwd=source,
            env={**os.environ, "PYTHONPATH": str(source)}, stdout=log, stderr=subprocess.STDOUT,
        )
        processes.append(process)
        origin = f"http://127.0.0.1:{args.port_base + offset}"
        for _ in range(120):
            try:
                with urlopen(origin + "/", timeout=1) as response:
                    if response.status == 200:
                        return origin
            except (URLError, HTTPError, TimeoutError):
                pass
            if process.poll() is not None:
                raise RuntimeError(f"Fixture server exited: {scenario}; see server-{offset}.log")
            time.sleep(.2)
        raise RuntimeError(f"Fixture server did not become ready: {scenario}")

    try:
        demo = serve("demo", 0)
        empty = serve("empty", 1)
        live = serve("live-empty", 2)
        failed = serve("storage-error", 3)
        queued = serve("queued", 4)
        large = serve("large", 5)
        unlinked = serve("unlinked", 7)
        baseline = serve("demo", 6, args.baseline.resolve()) if args.baseline else None
        with urlopen(demo + "/report.json") as response:
            report = json.load(response)
        passing = next(job for job in report["jobs"] if job["issue_number"] == 105)
        stopped = next(job for job in report["jobs"] if job["issue_number"] == 106)
        test_run = next(job for job in report["jobs"] if job["issue_number"] == 101)
        axe_source = args.axe.read_text()
        with sync_playwright() as playwright:
            browser = getattr(playwright, args.browser).launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            page = context.new_page()
            # ELI5: capture browser exceptions so a page that looks right cannot hide a JavaScript crash.
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            external_requests = []
            # ELI5: record any request that escapes the local fixture servers.
            page.on("request", lambda request: external_requests.append(request.url)
                    if not request.url.startswith("http://127.0.0.1:") else None)

            def screenshot(name: str, full: bool = True) -> None:
                """Save one named browser view and include it in the machine-readable result."""

                # ELI5: screenshots make each route and viewport check inspectable after the run.
                filename = f"{name}.png"
                page.screenshot(path=str(output / filename), full_page=full, animations="disabled")
                snapshots.append(filename)

            def audit(name: str) -> None:
                """Run the bundled axe rules against the currently visible page."""

                # ELI5: load axe in DevTools, then store compact violations instead of raw browser output.
                # DevTools evaluation avoids relaxing the page's real production CSP.
                page.evaluate(axe_source)
                assessment = page.evaluate("""async () => await axe.run(document, {
                    runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21aa']}
                })""")
                violations = [{"id": row["id"], "impact": row["impact"], "description": row["description"],
                               "targets": [node["target"] for node in row["nodes"]]}
                              for row in assessment["violations"]]
                audits.append({"page": name, "violations": violations,
                               "incomplete_checks": len(assessment["incomplete"]),
                               "passed_rules": len(assessment["passes"])})
                check(f"axe: {name}", not violations)

            routes = {
                "landing": demo + "/", "workbench": demo + "/dashboard",
                "application-pass": demo + "/jobs/" + passing["id"],
                "application-stopped": demo + "/jobs/" + stopped["id"],
                "test-proof": demo + "/jobs/" + test_run["id"],
                "contracts": demo + "/cases", "archive": demo + "/evidence",
                "no-matches": demo + "/dashboard?q=no-such-case",
                "empty-storage": empty + "/dashboard", "live-blocked": live + "/cases",
                "not-found": demo + "/jobs/not-found", "storage-error": failed + "/dashboard",
                "queued": queued + "/dashboard", "unlinked": unlinked + "/dashboard",
            }
            for width, height, size in [(1440, 1000, "desktop"), (390, 844, "mobile")]:
                page.set_viewport_size({"width": width, "height": height})
                if baseline:
                    page.goto(baseline + "/")
                    screenshot(f"before-workbench-{size}")
                for name, url in routes.items():
                    response = page.goto(url)
                    expected_status = 404 if name == "not-found" else 503 if name == "storage-error" else 200
                    check(f"HTTP {name} {size}", response.status == expected_status)
                    check(f"no overflow: {name} {size}", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                    check(f"one h1: {name} {size}", page.locator("h1").count() == 1)
                    screenshot(f"after-{name}-{size}")
                    audit(f"{name}-{size}")
                if size == "mobile":
                    page.goto(demo + "/dashboard")
                    top = page.locator(".runs-table").bounding_box()["y"]
                    result["mobile_first_record_y"] = round(top, 1)
                    check("mobile ledger precedes portfolio charts", top < page.locator(".handoff").bounding_box()["y"])
                    check("mobile first record begins in first 800px", top < 800)
                    check("mobile finding uses full record width", page.locator(".finding-cell").first.bounding_box()["width"] > 290)

            # Keyboard-only search, explicit GET submission, status filters, and back-link state.
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.goto(demo + "/dashboard")
            page.keyboard.press("/")
            expect(page.locator("#q")).to_be_focused()
            check("slash focuses search", True)
            page.keyboard.type("105")
            page.keyboard.press("Enter")
            expect(page.locator(".runs-table tbody tr")).to_have_count(1)
            check("keyboard search is server backed", "q=105" in page.url)
            page.get_by_role("link", name="Inspect evidence for issue 105").click()
            expect(page.get_by_role("heading", name="Malformed YAML import", exact=True)).to_be_visible()
            page.get_by_role("link", name="Back to workbench").click()
            expect(page.locator("#q")).to_have_value("105")
            check("detail back link preserves filters", True)
            page.goto(demo + "/dashboard")
            page.select_option("#kind", "application")
            page.get_by_role("button", name="Apply filters").click()
            expect(page.locator(".runs-table tbody tr")).to_have_count(2)
            page.get_by_role("link", name="Needs attention 1", exact=True).click()
            expect(page.locator(".runs-table tbody tr")).to_have_count(1)
            check("kind and state filters intersect", "#106" in page.locator("tbody").inner_text())
            page.goto(demo + "/dashboard?q=nomatch")
            page.get_by_role("link", name="Clear all filters").first.click()
            expect(page.locator(".runs-table tbody tr")).to_have_count(6)
            check("empty filter recovery", True)

            # Real export: a complete mode-labelled JSON attachment, never filtered silently.
            with page.expect_download() as download_event:
                page.get_by_role("link", name="Export all evidence").click()
            download = download_event.value
            destination = output / download.suggested_filename
            download.save_as(destination)
            exported = json.loads(destination.read_text())
            check("download includes all six simulated runs", len(exported["jobs"]) == 6 and exported["truth"]["simulation"])

            # Refresh states retain stale evidence on errors and do not erase draft inputs.
            page.locator("#q").fill("draft-not-applied")
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#notice-message")).to_have_text("Snapshot refreshed. No work was started.")
            expect(page.locator("#q")).to_have_value("draft-not-applied")
            expect(page.locator("#refresh")).to_be_focused()
            check("refresh preserves draft input and keyboard focus", True)
            held = []
            page.route("**/dashboard", lambda route: held.append(route))
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("[data-refresh-root]")).to_have_attribute("aria-busy", "true")
            expect(page.locator("#refresh")).to_be_disabled()
            screenshot("after-refresh-loading-desktop")
            check("loading state retains records and prevents duplicate refresh", page.locator("tbody tr").count() == 6)
            held[0].fulfill(response=held[0].fetch())
            expect(page.locator("#notice-message")).to_contain_text("Snapshot refreshed")
            expect(page.locator("#refresh")).to_be_enabled()
            page.unroute("**/dashboard")
            page.route("**/dashboard", lambda route: route.abort("failed"))
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#notice-message")).to_contain_text("Your last snapshot is still shown")
            expect(page.locator(".runs-table tbody tr")).to_have_count(6)
            screenshot("after-refresh-error-desktop")
            check("refresh failure retains evidence and re-enables retry", page.locator("#refresh").is_enabled())
            page.get_by_role("button", name="Dismiss", exact=True).click()
            expect(page.locator("#notice")).to_be_hidden()
            page.unroute("**/dashboard")
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#notice-message")).to_contain_text("Snapshot refreshed")
            check("refresh recovers after a failed request", True)

            # Milestones are a disclosure, not deleted evidence; refresh retains disclosure state.
            page.goto(demo + "/jobs/" + stopped["id"])
            collapsed = page.locator("#event-list li:visible").count()
            page.get_by_role("button", name=f"Show all {stopped['event_count']} events").click()
            check("all events remain inspectable", page.locator("#event-list li:visible").count() == stopped["event_count"] > collapsed)
            page.locator("#run-metadata summary").click()
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#notice-message")).to_contain_text("Snapshot refreshed")
            check("refresh preserves open event trail and metadata", page.locator("#run-metadata").get_attribute("open") is not None
                  and page.locator("#event-list li:visible").count() == stopped["event_count"])
            page.get_by_role("button", name="Copy candidate sha", exact=False).click()
            expect(page.locator("#notice")).to_be_visible()
            # Clipboard completion is asynchronous; a prior refresh notice is still visible.
            expect(page.locator("#notice-message")).to_contain_text(re.compile("Copied|selected"))
            copy_notice = page.locator("#notice-message").inner_text()
            check("copy succeeds or offers a selectable fallback", "Copied" in copy_notice or "selected" in copy_notice)

            page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
                configurable: true, value: {writeText: async () => { throw new Error('Test: denied'); }}
            })""")
            page.get_by_role("button", name="Copy candidate sha", exact=False).click()
            expect(page.locator("#notice-message")).to_contain_text("copy it manually")
            check("denied clipboard selects the exact SHA", page.evaluate("getSelection().toString()") == stopped["candidate_sha"])

            # Broader responsive reflow, reduced motion, and native focus outlines.
            for width, height in [(320, 740), (720, 900), (768, 1024), (1024, 768), (1280, 720)]:
                page.set_viewport_size({"width": width, "height": height})
                for name in ("landing", "workbench", "application-pass", "contracts", "archive"):
                    page.goto(routes[name])
                    check(f"reflow {width}px: {name}", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            check("reduced motion is active", page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))
            page.goto(demo + "/dashboard")
            page.keyboard.press("Tab")
            check("keyboard focus has a visible outline", page.evaluate("getComputedStyle(document.activeElement).outlineStyle !== 'none'"))
            page.goto(large + "/dashboard")
            expect(page.locator(".runs-table tbody tr")).to_have_count(20)
            page.get_by_role("link", name="Next →", exact=True).click()
            expect(page.locator(".runs-table tbody tr")).to_have_count(20)
            page.get_by_role("link", name="Next →", exact=True).click()
            expect(page.locator(".runs-table tbody tr")).to_have_count(5)
            check("45-run fixture paginates 20 / 20 / 5", "page=3" in page.url)

            # The core remains usable with scripts completely disabled.
            nojs = browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844})
            plain = nojs.new_page()
            plain.goto(demo + "/dashboard")
            plain.locator("#q").fill("105")
            plain.get_by_role("button", name="Apply filters").click()
            expect(plain.locator(".runs-table tbody tr")).to_have_count(1)
            plain.get_by_role("link", name="Inspect evidence for issue 105").click()
            expect(plain.locator("#event-list li:visible")).to_have_count(passing["event_count"])
            check("no-JS search, navigation and complete event trail", True)
            plain.screenshot(path=str(output / "after-no-js-mobile.png"), full_page=True)
            snapshots.append("after-no-js-mobile.png")
            nojs.close()
            check("no uncaught browser errors", not page_errors)
            check("no external page requests", not external_requests)
            result["external_requests"] = external_requests
            result["runtime_asset_bytes"] = {name: (ROOT / "app/static" / name).stat().st_size
                                             for name in ("app.css", "app.js", "mark.svg", "wordmark.svg")}
            browser.close()
        result["passed"] = True
    except Exception as error:
        result["passed"] = False
        result["error"] = str(error)
        result["traceback"] = traceback.format_exc()
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for handle in handles:
            handle.close()
        (output / "validation.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
