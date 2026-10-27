"use strict";
document.querySelectorAll("[data-print]").forEach(button => button.addEventListener("click", () => window.print()));
const viewer = document.getElementById("image-viewer");
const viewerImage = document.getElementById("viewer-image");
const caption = document.getElementById("viewer-caption");
const original = document.getElementById("original-image");
let trigger = null;
document.querySelectorAll("a.shot").forEach(link => {
  link.addEventListener("click", event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || typeof viewer.showModal !== "function") return;
    event.preventDefault();
    trigger = link;
    const image = link.querySelector("img");
    viewerImage.src = link.href;
    viewerImage.alt = image.alt;
    caption.textContent = image.alt;
    original.href = link.href;
    viewer.showModal();
    const scroller = viewer.querySelector(".image-scroll");
    scroller.scrollTop = 0;
    scroller.scrollLeft = 0;
    document.getElementById("close-viewer").focus();
  });
});
document.getElementById("close-viewer").addEventListener("click", () => viewer.close());
viewer.addEventListener("click", event => { if (event.target === viewer) viewer.close(); });
viewer.addEventListener("close", () => { if (trigger) trigger.focus(); });

