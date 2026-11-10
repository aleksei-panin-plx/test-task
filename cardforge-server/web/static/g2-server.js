// Server part of the core G2 page (g2-core.js = the core review_page.html script, unchanged):
// the reviewer is the signed-in owner, and «Отправить решения» posts the same JSON the local page
// downloads (format cardforge-g2-review-v2, blind card-NN keys) to the run.
(function () {
  const owner = (document.querySelector('meta[name="cf-owner"]') || {}).content || "";
  const rv = document.getElementById("reviewer");
  if (owner && rv) { rv.value = owner; rv.readOnly = true; state.reviewer = owner; save(); }
  const old = document.getElementById("saveBtn");
  const btn = old.cloneNode(true);  // drops the core «download» listener
  old.replaceWith(btn);
  btn.addEventListener("click", async () => {
    const text = exportable();
    if (!text) return;
    btn.disabled = true;
    try {
      const r = await fetch(location.pathname.replace(/\/g2\/?$/, "/g2/decisions"), {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CardForge": "1" }, body: text,
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok) {
        try { localStorage.removeItem(KEY); } catch (e) {}
        alert("Решения записаны. " + (j.message || ""));
        location.reload();
      } else {
        alert("Не записано: " + (j.error || r.status));
      }
    } finally { btn.disabled = false; }
  });
})();
