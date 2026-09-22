/* Progressive enhancement only. Forms, deep links, proof and exports work without JS. */
(() => {
  "use strict";
  document.documentElement.classList.add("js");
  let noticeTimer;

  function notify(message, isError = false) {
    const notice = document.getElementById("notice");
    if (!notice) return;
    clearTimeout(noticeTimer);
    notice.hidden = false;
    notice.toggleAttribute("data-error", isError);
    const output = document.getElementById("notice-message");
    output.setAttribute("role", isError ? "alert" : "status");
    output.textContent = message;
    // Errors stay visible until the next action; successful copy/refresh stays briefly.
    if (!isError) noticeTimer = setTimeout(() => { notice.hidden = true; }, 6000);
  }

  async function refresh(button) {
    const root = document.querySelector("[data-refresh-root]");
    if (!root || root.getAttribute("aria-busy") === "true") return;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    const openDetails = [...root.querySelectorAll("details[open][id]")].map(el => el.id);
    const drafts = [...root.querySelectorAll("input[id], select[id]")].map(el => [el.id, el.value]);
    const eventsExpanded = Boolean(root.querySelector("[data-events][data-expanded]"));
    const focusedId = document.activeElement?.id;
    const scroll = { x: window.scrollX, y: window.scrollY };
    button.disabled = true;
    root.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(window.location.href, {
        headers: { Accept: "text/html" }, cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error("Unavailable");
      const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = parsed.querySelector("[data-refresh-root]");
      if (!next) throw new Error("Invalid snapshot");
      // Replace only the server-rendered workspace. Never execute response scripts.
      root.replaceWith(next);
      drafts.forEach(([id, value]) => { const input = document.getElementById(id); if (input) input.value = value; });
      openDetails.forEach(id => { const details = document.getElementById(id); if (details) details.open = true; });
      if (eventsExpanded) {
        next.querySelector("[data-events]")?.setAttribute("data-expanded", "");
        const toggle = next.querySelector("[data-event-toggle]");
        if (toggle) { toggle.setAttribute("aria-expanded", "true"); toggle.textContent = "Show milestones only"; }
      }
      if (focusedId) document.getElementById(focusedId)?.focus({ preventScroll: true });
      window.scrollTo(scroll.x, scroll.y);
      notify("Snapshot refreshed. No work was started.");
    } catch (_error) {
      notify("Could not refresh. Your last snapshot is still shown. Try Refresh again.", true);
    } finally {
      clearTimeout(timeout);
      root.removeAttribute("aria-busy");
      button.disabled = false;
    }
  }

  document.addEventListener("click", async event => {
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest("[data-dismiss]")) { document.getElementById("notice").hidden = true; return; }
    const refreshButton = target?.closest("[data-refresh]");
    if (refreshButton) { await refresh(refreshButton); return; }
    const eventToggle = target?.closest("[data-event-toggle]");
    if (eventToggle) {
      const list = document.getElementById(eventToggle.getAttribute("aria-controls"));
      if (!list) return;
      const expanded = list.toggleAttribute("data-expanded");
      eventToggle.setAttribute("aria-expanded", String(expanded));
      eventToggle.textContent = expanded ? "Show milestones only" : `Show all ${eventToggle.dataset.total} events`;
      return;
    }
    const copyButton = target?.closest("[data-copy]");
    if (!copyButton) return;
    const source = document.getElementById(copyButton.dataset.copy);
    if (!source) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(source.textContent.trim());
      notify("Copied to clipboard.");
    } catch (_error) {
      // Select the value as a useful manual fallback when clipboard access is denied.
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(source);
      selection?.removeAllRanges();
      selection?.addRange(range);
      notify("Clipboard unavailable. The value is selected; copy it manually.", true);
    }
  });

  document.addEventListener("keydown", event => {
    const target = event.target;
    const typing = target instanceof HTMLElement && (target.isContentEditable || /INPUT|TEXTAREA|SELECT/.test(target.tagName));
    if (event.key === "/" && !typing && !event.metaKey && !event.ctrlKey && !event.altKey) {
      const search = document.getElementById("q");
      if (search) { event.preventDefault(); search.focus(); }
    }
    if (event.key === "Escape") {
      const notice = document.getElementById("notice");
      if (notice) notice.hidden = true;
    }
  });
})();
