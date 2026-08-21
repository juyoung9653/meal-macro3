(function () {
  "use strict";

  var storageKey = "meal-manual-large-text";
  var body = document.body;
  var fontButton = document.querySelector("[data-font-toggle]");
  var printButton = document.querySelector("[data-print]");
  var expandButton = document.querySelector("[data-expand-all]");

  function readLargeTextSetting() {
    try {
      return window.localStorage.getItem(storageKey) === "true";
    } catch (_error) {
      return false;
    }
  }

  function saveLargeTextSetting(enabled) {
    try {
      window.localStorage.setItem(storageKey, String(enabled));
    } catch (_error) {
      // 로컬 파일 보안 설정으로 저장할 수 없어도 현재 화면에서는 동작한다.
    }
  }

  function applyLargeText(enabled) {
    body.classList.toggle("large-text", enabled);
    if (fontButton) {
      fontButton.setAttribute("aria-pressed", String(enabled));
      fontButton.textContent = enabled ? "글자 원래대로" : "글자 크게";
    }
  }

  applyLargeText(readLargeTextSetting());

  if (fontButton) {
    fontButton.addEventListener("click", function () {
      var enabled = !body.classList.contains("large-text");
      applyLargeText(enabled);
      saveLargeTextSetting(enabled);
    });
  }

  if (printButton) {
    printButton.addEventListener("click", function () {
      window.print();
    });
  }

  if (expandButton) {
    expandButton.addEventListener("click", function () {
      var details = Array.prototype.slice.call(document.querySelectorAll("details"));
      var shouldOpen = details.some(function (item) { return !item.open; });
      details.forEach(function (item) { item.open = shouldOpen; });
      expandButton.textContent = shouldOpen ? "질문 모두 접기" : "질문 모두 펼치기";
      expandButton.setAttribute("aria-expanded", String(shouldOpen));
    });
  }

  var pageName = decodeURIComponent(window.location.pathname.split("/").pop() || "");
  document.querySelectorAll("[data-page]").forEach(function (link) {
    if (link.getAttribute("data-page") === pageName) {
      link.setAttribute("aria-current", "page");
    }
  });
}());
