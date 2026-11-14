/* Column manager shared by both pages: "Columns" dropdown (show/hide + reorder) and drag-to-resize
   handles on the header cell edges. Settings are remembered per page in localStorage.

   Works on tables whose rows are rebuilt by the page: a MutationObserver re-applies the layout.
   Cells are matched to headers by position among the headers the page itself has not hidden
   (th.hidden), then tagged with data-ci (the header's original index). */
(function () {
  const style = document.createElement("style");
  style.textContent = `
    .col-off { display:none !important; }
    .colbar { position:relative; text-align:right; margin:0 0 8px; }
    .colbar > button { padding:5px 12px; font-size:13px; }
    .colmenu { position:absolute; right:0; top:100%; z-index:30; min-width:230px; text-align:left; background:var(--card); color:var(--fg);
               border:1px solid var(--line); border-radius:10px; padding:8px; box-shadow:0 6px 24px rgba(0,0,0,.18); }
    .colmenu .ci { display:flex; align-items:center; gap:8px; padding:3px 4px; font-size:13px; }
    .colmenu .ci label { flex:1; display:flex; gap:8px; align-items:center; cursor:pointer; }
    .colmenu .ci button { padding:0 7px; font-size:12px; line-height:20px; }
    .colmenu .foot { border-top:1px solid var(--line); margin-top:6px; padding-top:6px; display:flex; justify-content:space-between; font-size:12px; color:var(--muted); }
    .colmenu .foot button { padding:3px 10px; font-size:12px; }
    th .rs { position:absolute; top:0; right:-3px; width:7px; height:100%; cursor:col-resize; z-index:2; user-select:none; }
    th .rs:hover, th .rs.on { background:var(--accent); opacity:.5; }
    th { position:relative; overflow:hidden; text-overflow:ellipsis; }
    th:has(.rs) { overflow:visible; }`;
  document.head.appendChild(style);

  window.DJColumns = {
    init({ table, key }) {
      const head = table.tHead.rows[0], body = table.tBodies[0], store = "djcols:" + key;
      const ths = [...head.children];
      ths.forEach((th, i) => {
        th.dataset.ci = i;
        th.dataset.label = th.dataset.label || th.textContent.trim() || "Select";
        const rs = document.createElement("div"); rs.className = "rs"; th.appendChild(rs);
        rs.addEventListener("mousedown", e => startResize(e, th, rs));
        rs.addEventListener("click", e => e.stopPropagation());
      });
      let st;
      try { st = JSON.parse(localStorage.getItem(store) || "null"); } catch { st = null; }
      st = st || { order: [], hidden: [], widths: {} };
      st.order = st.order.filter(i => i < ths.length);
      const fillNew = () => ths.forEach((th, i) => {  // columns new to this layout: add at the end, optionally off by default
        if (!st.order.includes(i)) { st.order.push(i); if (th.hasAttribute("data-off")) st.hidden.push(i); }
      });
      fillNew();
      const save = () => localStorage.setItem(store, JSON.stringify(st));
      const appHidden = th => th.classList.contains("hidden");
      const pos = ci => st.order.indexOf(+ci);

      function sizeTable() {
        const vis = ths.filter(t => !appHidden(t) && !st.hidden.includes(+t.dataset.ci));
        if (vis.length && vis.every(t => st.widths[t.dataset.ci])) {
          table.style.width = vis.reduce((s, t) => s + st.widths[t.dataset.ci], 0) + "px"; table.style.minWidth = "100%";
        } else { table.style.width = ""; table.style.minWidth = ""; }
      }
      function apply() {
        obs.disconnect();
        const act = ths.filter(t => !appHidden(t));
        [...ths].sort((a, b) => pos(a.dataset.ci) - pos(b.dataset.ci)).forEach(t => head.appendChild(t));
        ths.forEach(t => {
          const ci = +t.dataset.ci;
          t.classList.toggle("col-off", st.hidden.includes(ci));
          t.style.width = st.widths[ci] ? st.widths[ci] + "px" : "";
        });
        table.style.tableLayout = Object.keys(st.widths).length ? "fixed" : "";
        table.parentElement.style.overflowX = "auto";
        sizeTable();
        for (const tr of body.rows) {
          const cells = [...tr.children];
          if (cells.some(c => !c.dataset.ci)) {
            if (cells.length !== act.length) continue;           // e.g. a full-width "nothing to show" row
            cells.forEach((c, k) => { c.dataset.ci = act[k].dataset.ci; });
          }
          cells.sort((a, b) => pos(a.dataset.ci) - pos(b.dataset.ci)).forEach(c => {
            tr.appendChild(c); c.classList.toggle("col-off", st.hidden.includes(+c.dataset.ci));
          });
        }
        obs.observe(body, { childList: true });
      }
      const obs = new MutationObserver(apply);

      function startResize(e, th, rs) {
        e.preventDefault(); e.stopPropagation();
        ths.forEach(t => { if (!appHidden(t) && !st.hidden.includes(+t.dataset.ci)) st.widths[t.dataset.ci] = Math.round(t.getBoundingClientRect().width); });
        const ci = +th.dataset.ci, x0 = e.clientX, w0 = st.widths[ci];
        apply(); rs.classList.add("on");
        const mv = ev => { st.widths[ci] = Math.max(40, Math.round(w0 + ev.clientX - x0)); th.style.width = st.widths[ci] + "px"; sizeTable(); };
        const up = () => { document.removeEventListener("mousemove", mv); document.removeEventListener("mouseup", up); rs.classList.remove("on"); save(); };
        document.addEventListener("mousemove", mv); document.addEventListener("mouseup", up);
      }

      // ---- dropdown ----
      const bar = document.createElement("div"); bar.className = "colbar";
      const btn = document.createElement("button"); btn.textContent = "Columns ▾";
      const menu = document.createElement("div"); menu.className = "colmenu"; menu.hidden = true;
      bar.append(btn, menu); table.insertAdjacentElement("beforebegin", bar);
      function renderMenu() {
        menu.replaceChildren();
        const list = st.order.filter(ci => !appHidden(ths[ci]));
        list.forEach((ci, k) => {
          const row = document.createElement("div"); row.className = "ci";
          const lab = document.createElement("label"), cb = document.createElement("input");
          cb.type = "checkbox"; cb.checked = !st.hidden.includes(ci);
          cb.onchange = () => { (st.touched = st.touched || {})[ci] = 1; st.hidden = cb.checked ? st.hidden.filter(x => x !== ci) : [...st.hidden, ci]; save(); apply(); };
          lab.append(cb, ths[ci].dataset.label);
          const mv = (d, txt) => {
            const b = document.createElement("button"); b.textContent = txt; b.disabled = k + d < 0 || k + d >= list.length;
            b.onclick = () => { const other = list[k + d], i = st.order.indexOf(ci), j = st.order.indexOf(other); [st.order[i], st.order[j]] = [st.order[j], st.order[i]]; save(); apply(); renderMenu(); };
            return b;
          };
          row.append(lab, mv(-1, "▲"), mv(1, "▼")); menu.append(row);
        });
        const foot = document.createElement("div"); foot.className = "foot";
        const reset = document.createElement("button"); reset.textContent = "Reset layout";
        reset.onclick = () => { st = { order: [], hidden: [], widths: {}, touched: {} }; fillNew(); table.style.tableLayout = ""; save(); apply(); renderMenu(); };
        foot.append(document.createTextNode("Drag header edges to resize"), reset); menu.append(foot);
      }
      btn.onclick = e => { e.stopPropagation(); menu.hidden = !menu.hidden; if (!menu.hidden) renderMenu(); };
      document.addEventListener("click", e => { if (!bar.contains(e.target)) menu.hidden = true; });
      apply();
      return {
        /* Show/hide the column with this header label automatically, unless the user chose its visibility in the menu. */
        setAuto(label, show) {
          const th = ths.find(t => t.dataset.label === label); if (!th) return;
          const ci = +th.dataset.ci;
          if (st.touched && st.touched[ci]) return;
          if (show === !st.hidden.includes(ci)) return;
          st.hidden = show ? st.hidden.filter(x => x !== ci) : [...st.hidden, ci]; apply();
        },
      };
    },
  };
})();
