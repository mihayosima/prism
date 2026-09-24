/* =====================================================================
   prism_theme —— 前端布局重构（DOM 层）
   仅在 <body data-theme="prism_theme"> 时运行。职责：
   ① 把左侧导航 .nav 收进「右侧可滑出抽屉」（默认隐藏）；
   ② 顶栏加棱镜开关按钮 + 换上 prism SVG 标志 + 精简（隐藏 session 文本）；
   ③ 把“新会话”按钮移入抽屉底部。
   皮肤本身的颜色/玻璃/辉光由 theme.css 负责，本文件只动结构。
   ===================================================================== */
(function () {
  "use strict";
  var body = document.body;
  if (!body || body.getAttribute("data-theme") !== "prism_theme") return;

  var nav = document.querySelector(".nav");
  var top = document.querySelector(".top");
  if (!nav || !top) return;

  /* ---------- ① 抽屉：收纳左侧导航 ---------- */
  var overlay = document.createElement("div");
  overlay.className = "prism-overlay";

  var drawer = document.createElement("aside");
  drawer.className = "prism-drawer";
  drawer.innerHTML =
    '<div class="prism-drawer-head"><span>控制台</span>' +
    '<button class="prism-close" aria-label="关闭">✕</button></div>';
  drawer.appendChild(nav);

  document.body.appendChild(overlay);
  document.body.appendChild(drawer);

  function openDrawer()  { drawer.classList.add("open");  overlay.classList.add("show"); }
  function closeDrawer() { drawer.classList.remove("open"); overlay.classList.remove("show"); }

  /* ---------- ② 顶栏：棱镜开关 + 标志 + 精简 ---------- */
  var toggle = document.createElement("button");
  toggle.className = "prism-toggle";
  toggle.setAttribute("aria-label", "打开控制台");
  toggle.innerHTML =
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg>';
  top.insertBefore(toggle, top.firstChild);

  var mark = document.querySelector(".brand .mark");
  if (mark) {
    mark.classList.add("prism-mark");
    mark.innerHTML =
      '<svg viewBox="0 0 32 32" width="100%" height="100%" aria-hidden="true">' +
      '<defs><linearGradient id="pg" x1="0" y1="0" x2="1" y2="1">' +
      '<stop offset="0" stop-color="#ff5d8f"/><stop offset=".5" stop-color="#a06bff"/>' +
      '<stop offset="1" stop-color="#34e3ff"/></linearGradient></defs>' +
      '<path d="M16 3 L29 27 L3 27 Z" fill="none" stroke="url(#pg)" stroke-width="2.2" stroke-linejoin="round"/>' +
      '<path d="M16 3 L16 27 M16 3 L3 27 M16 3 L29 27" stroke="url(#pg)" stroke-width="1" opacity=".7"/>' +
      '<circle cx="16" cy="3" r="2.4" fill="#ff5d8f"/>' +
      '<circle cx="3" cy="27" r="2.4" fill="#a06bff"/>' +
      '<circle cx="29" cy="27" r="2.4" fill="#34e3ff"/></svg>';
  }

  /* session 文本隐藏，新会话按钮收进抽屉 */
  var sess = document.getElementById("session");
  if (sess) sess.style.display = "none";
  var newBtn = document.getElementById("newSession");
  var foot = nav.querySelector(".nav-foot");
  if (newBtn && foot) { foot.appendChild(newBtn); newBtn.classList.add("prism-new"); }

  /* ---------- ③ 事件 ---------- */
  toggle.addEventListener("click", function () {
    if (drawer.classList.contains("open")) closeDrawer(); else openDrawer();
  });
  overlay.addEventListener("click", closeDrawer);
  drawer.querySelector(".prism-close").addEventListener("click", closeDrawer);
  nav.querySelectorAll(".nav-item").forEach(function (b) {
    b.addEventListener("click", closeDrawer);   // 选中即收起，回到极简对话态
  });

  /* 顶栏毛玻璃类（theme.css 也可直接命中 .top，这里仅作语义标记） */
  top.classList.add("prism-top");
})();
