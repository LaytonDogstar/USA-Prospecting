// Evidence drawer: links with class "ev" open their claims in a side panel. Without JS they open as a page.
(function () {
  const drawer = document.getElementById("drawer");
  if (!drawer) return;
  const body = drawer.querySelector(".body");
  function close() { drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); }
  document.addEventListener("click", async (e) => {
    const a = e.target.closest("a.ev");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    const url = a.getAttribute("href") + (a.getAttribute("href").includes("?") ? "&" : "?") + "partial=1";
    body.textContent = "Loading…";
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    try {
      const r = await fetch(url, { credentials: "same-origin" });
      body.innerHTML = r.ok ? await r.text() : "Could not load the evidence.";
    } catch (err) { body.textContent = "Could not load the evidence."; }
  });
  drawer.querySelector(".close").addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  // Confirm destructive actions
  document.addEventListener("submit", (e) => {
    const msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
})();
