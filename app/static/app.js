(function () {
  "use strict";

  function tick() {
    const el = document.getElementById("utc-clock");
    if (!el) return;
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    el.textContent =
      "UTC " + now.getUTCFullYear() + "-" + pad(now.getUTCMonth() + 1) + "-" + pad(now.getUTCDate()) +
      " " + pad(now.getUTCHours()) + ":" + pad(now.getUTCMinutes()) + ":" + pad(now.getUTCSeconds());
  }
  tick();
  setInterval(tick, 1000);

  // 表单提交时给出一次性反馈，避免重复点击。
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      const button = form.querySelector("button[type=submit]");
      if (!button) return;
      if (button.dataset.busy === "1") return;
      button.dataset.busy = "1";
      button.dataset.label = button.textContent;
      button.textContent = "处理中…";
      setTimeout(function () {
        button.dataset.busy = "";
        button.textContent = button.dataset.label || "提交";
      }, 8000);
    });
  });

  // 上传文件后显示所选文件名。
  document.querySelectorAll("input[type=file]").forEach(function (input) {
    input.addEventListener("change", function () {
      const target = document.querySelector('[data-file-for="' + input.id + '"]');
      if (target) {
        target.textContent = input.files && input.files[0] ? input.files[0].name : "";
      }
    });
  });

  // 站点切换后刷新可用日期提示。
  const stationSelect = document.getElementById("observation-station");
  if (stationSelect) {
    stationSelect.addEventListener("change", function () {
      const opt = stationSelect.selectedOptions[0];
      const latest = opt ? opt.dataset.latest : "";
      const hint = document.getElementById("station-hint");
      if (hint) {
        hint.textContent = latest ? "该站最新观测：" + latest + " UTC" : "该站暂无观测数据";
      }
      const publishSelect = document.getElementById("publish-station");
      if (publishSelect) publishSelect.value = stationSelect.value;
    });
  }

  // 复制按钮。
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      const sel = button.getAttribute("data-copy");
      const node = document.querySelector(sel);
      if (!node) return;
      navigator.clipboard.writeText(node.textContent || "").then(function () {
        const old = button.textContent;
        button.textContent = "已复制";
        setTimeout(function () { button.textContent = old; }, 1500);
      });
    });
  });
})();
