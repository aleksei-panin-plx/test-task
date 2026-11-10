// G3 page: regenerate cards with the person's edit, or accept and build the delivery.
(function () {
  const post = async (suffix, body) => {
    const base = location.pathname.replace(/\/g3\/?$/, "");
    const msg = document.getElementById("msg");
    msg.textContent = "отправляю…";
    const r = await fetch(base + suffix, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CardForge": "1" }, body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    msg.textContent = r.ok ? (j.message || "готово") : ("ошибка: " + (j.error || r.status));
    if (r.ok) setTimeout(() => location.reload(), 800);
  };
  const regen = document.getElementById("regen");
  if (regen) regen.addEventListener("click", () => {
    const cards = document.getElementById("cards").value.split(/[\s,;]+/).filter(Boolean);
    const note = document.getElementById("note").value.trim();
    if (!cards.length || !note) { alert("Нужны номера карт и правка словами."); return; }
    post("/g3/regenerate", { cards, note });
  });
  const exp = document.getElementById("export");
  if (exp) exp.addEventListener("click", () => post("/g3/export", {}));
})();
