// cropper.js -- a small drag-to-pan/zoom-to-crop dialog, shared by
// index.html (user's own avatar + banner) and admin.html (bot's avatar +
// banner). All four just need "pick a crop of an image at some aspect
// ratio and get a data URL back", so this lives once here instead of being
// duplicated per page/shape -- everything else in WebUI/ is deliberately
// self-contained per page (no build step), but a dialog this involved is
// worth the one shared <script> tag.
//
// Usage: window.openImageCropper(file, opts).then(function (dataUrl) { ... })
// dataUrl is a JPEG data URL at opts.outputWidth x opts.outputHeight, or
// null if the user cancelled. opts (all optional):
//   width, height    -- crop viewport size in css px (default 240x240, a
//                        circular avatar) -- e.g. 320x120 for a wide banner
//   round             -- true for a circular viewport (avatars), false for
//                        a rounded-rect one (banners); default true
//   outputWidth, outputHeight -- exported bitmap size; defaults to width/height
(function () {
  "use strict";

  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v === undefined || v === null || v === false) continue;
        if (k === "text") node.textContent = v;
        else if (k === "style") node.style.cssText = v;
        else if (k.indexOf("on") === 0 && typeof v === "function") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v);
      }
    }
    if (children) {
      [].concat(children).forEach(function (c) {
        if (c === null || c === undefined || c === false) return;
        node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    }
    return node;
  }

  function readAsImage(file) {
    return new Promise(function (resolve, reject) {
      const reader = new FileReader();
      reader.onload = function () {
        const img = new Image();
        img.onload = function () { resolve(img); };
        img.onerror = reject;
        img.src = reader.result;
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  window.openImageCropper = function (file, opts) {
    opts = opts || {};
    const VW = opts.width || 240, VH = opts.height || 240;
    const round = opts.round !== false; // default true, matches the original avatar-only behavior
    const OW = opts.outputWidth || VW, OH = opts.outputHeight || VH;

    return readAsImage(file).then(function (img) {
      return new Promise(function (resolve) {
        // "cover" fit at zoom=1 -- the image always fully fills the
        // viewport, so there's never a gap to see through.
        const baseScale = Math.max(VW / img.naturalWidth, VH / img.naturalHeight);
        const crop = { zoom: 1, scale: baseScale, tx: 0, ty: 0 };
        crop.tx = (VW - img.naturalWidth * baseScale) / 2;
        crop.ty = (VH - img.naturalHeight * baseScale) / 2;

        function clamp() {
          const dw = img.naturalWidth * crop.scale, dh = img.naturalHeight * crop.scale;
          crop.tx = Math.min(0, Math.max(VW - dw, crop.tx));
          crop.ty = Math.min(0, Math.max(VH - dh, crop.ty));
        }
        clamp();

        function applyTransform() {
          imgEl.style.width = (img.naturalWidth * crop.scale) + "px";
          imgEl.style.height = (img.naturalHeight * crop.scale) + "px";
          imgEl.style.left = crop.tx + "px";
          imgEl.style.top = crop.ty + "px";
        }

        function setZoom(zoom) {
          zoom = Math.max(1, Math.min(4, zoom));
          const oldScale = crop.scale;
          const newScale = baseScale * zoom;
          // Zoom about the viewport's center, not the image's top-left --
          // keeps whatever's centered in the preview centered after zooming.
          const cx = (VW / 2 - crop.tx) / oldScale;
          const cy = (VH / 2 - crop.ty) / oldScale;
          crop.zoom = zoom;
          crop.scale = newScale;
          crop.tx = VW / 2 - cx * newScale;
          crop.ty = VH / 2 - cy * newScale;
          clamp();
          applyTransform();
          zoomSlider.value = String(zoom);
        }

        const imgEl = h("img", {
          src: img.src, alt: "",
          // max-width:none/max-height:none override nocturne.css's global
          // `img { max-width: 100% }` -- without them the browser clamps
          // this element's rendered width to the viewport container while
          // leaving the JS-computed height alone, squashing/stretching the
          // image every time applyTransform() sets an explicit width/height
          // bigger than the viewport (i.e. almost always, since the image
          // has to overhang the viewport to be pannable at all).
          style: "position:absolute;left:0;top:0;max-width:none;max-height:none;user-select:none;pointer-events:none;",
          draggable: "false",
        });
        const viewport = h("div", {
          style: "position:relative;width:" + VW + "px;height:" + VH + "px;overflow:hidden;" +
            "border-radius:" + (round ? "50%" : "var(--radius-lg)") + ";background:#000;margin:0 auto;cursor:grab;touch-action:none;",
        }, imgEl);

        let dragging = false, startX = 0, startY = 0, startTx = 0, startTy = 0;
        viewport.addEventListener("pointerdown", function (e) {
          dragging = true;
          startX = e.clientX; startY = e.clientY;
          startTx = crop.tx; startTy = crop.ty;
          viewport.setPointerCapture(e.pointerId);
          viewport.style.cursor = "grabbing";
        });
        viewport.addEventListener("pointermove", function (e) {
          if (!dragging) return;
          crop.tx = startTx + (e.clientX - startX);
          crop.ty = startTy + (e.clientY - startY);
          clamp();
          applyTransform();
        });
        function endDrag() { dragging = false; viewport.style.cursor = "grab"; }
        viewport.addEventListener("pointerup", endDrag);
        viewport.addEventListener("pointercancel", endDrag);
        // Scroll wheel also zooms, since a slider-only zoom is fiddly for
        // fine adjustment when the pointer's already sitting on the image.
        viewport.addEventListener("wheel", function (e) {
          e.preventDefault();
          setZoom(crop.zoom + (e.deltaY < 0 ? 0.1 : -0.1));
        }, { passive: false });

        const zoomSlider = h("input", {
          type: "range", min: "1", max: "4", step: "0.01", value: "1",
          style: "width:100%;accent-color:var(--color-accent);",
          oninput: function (e) { setZoom(parseFloat(e.target.value)); },
        });

        applyTransform();

        function finish(result) {
          document.body.removeChild(backdrop);
          document.removeEventListener("keydown", onKeydown);
          resolve(result);
        }

        function onKeydown(e) {
          if (e.key === "Escape") finish(null);
        }
        document.addEventListener("keydown", onKeydown);

        function save() {
          const cv = document.createElement("canvas");
          cv.width = OW; cv.height = OH;
          const ctx = cv.getContext("2d");
          // The crop rectangle in the *original* image's own pixel space --
          // inverse of applyTransform()'s viewport-space placement.
          const sx = -crop.tx / crop.scale, sy = -crop.ty / crop.scale;
          const sw = VW / crop.scale, sh = VH / crop.scale;
          ctx.drawImage(img, sx, sy, sw, sh, 0, 0, OW, OH);
          finish(cv.toDataURL("image/jpeg", 0.85));
        }

        const backdrop = h("div", {
          class: "dialog-backdrop", style: "z-index:80;",
          onclick: function (e) { if (e.target === backdrop) finish(null); },
        }, h("div", { class: "dialog elev-lg", style: "width:min(" + Math.max(340, VW + 100) + "px,100%);" }, [
          h("div", { class: "dialog-title", text: round ? "Crop photo" : "Crop banner" }),
          h("div", { class: "dialog-body", style: "display:flex;flex-direction:column;gap:14px;align-items:stretch;" }, [
            viewport,
            zoomSlider,
            h("div", { style: "font-size:11.5px;color:color-mix(in srgb, var(--color-text) 55%, transparent);text-align:center;", text: "Drag to reposition, scroll or slide to zoom" }),
          ]),
          h("div", { class: "dialog-actions" }, [
            h("button", { class: "btn btn-ghost", onclick: function () { finish(null); }, text: "Cancel" }),
            h("button", { class: "btn btn-primary", onclick: save, text: "Save" }),
          ]),
        ]));
        document.body.appendChild(backdrop);
      });
    });
  };
})();
