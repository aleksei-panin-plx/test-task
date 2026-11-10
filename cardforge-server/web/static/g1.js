// G1 page: «Запускай» or an edit in the person's words (→ plan repair → G1 again).
(function () {
  const post = async (suffix, body) => {
    const base = location.pathname.replace(/\/g1\/?$/, "");
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
  const approve = document.getElementById("approve");
  if (approve) approve.addEventListener("click", () => {
    if (confirm("Одобрить план и запустить рендер всех сетов?")) post("/g1/approve", { human_answer: "Запускай (страница G1)" });
  });
  const send = document.getElementById("sendEdit");
  if (send) send.addEventListener("click", () => {
    const text = document.getElementById("edit").value.trim();
    if (!text) { alert("Опишите правку словами."); return; }
    post("/g1/edit", { edit: text });
  });
})();
