/*!
 * Cobijo Health — embeddable medical-bill help widget.
 *
 * A partner (clinic, legal-aid office, community org) drops ONE line on their page:
 *
 *   <script src="https://cobijohealth.org/embed.js" data-lang="es" async></script>
 *
 * and the free intake tool renders inline. By default the widget is inserted right where the
 * script tag sits; to place it elsewhere, add an empty <div id="cobijo-widget"></div> and the
 * widget mounts there instead.
 *
 * Design: an <iframe> to cobijohealth.org/embed — full CSS/JS isolation from the host page (their
 * theme can't break the tool; the tool can't touch their DOM), and the tool's own security headers
 * apply. The origin is derived from THIS script's own URL, so the same file works on localhost and
 * in production with no build step. Height auto-sizes via postMessage from the framed page.
 */
(function () {
  "use strict";
  var script = document.currentScript;
  if (!script) {
    // Fallback for async/deferred loads where currentScript is null: last cobijo embed script.
    var all = document.querySelectorAll('script[src*="embed.js"]');
    script = all[all.length - 1];
  }
  if (!script) return;

  var origin = new URL(script.src).origin;                 // works on localhost AND cobijohealth.org
  var lang = (script.getAttribute("data-lang") || "").trim().toLowerCase();
  var path = lang && lang !== "en" ? "/" + encodeURIComponent(lang) + "/embed" : "/embed";

  var iframe = document.createElement("iframe");
  iframe.src = origin + path;
  iframe.title = "Cobijo Health — free help with your medical bills";
  iframe.loading = "lazy";
  iframe.setAttribute("allow", "clipboard-write");
  iframe.style.cssText =
    "width:100%;border:0;display:block;min-height:560px;overflow:hidden;background:transparent";

  var mount = document.getElementById("cobijo-widget");
  if (mount) mount.appendChild(iframe);
  else script.parentNode.insertBefore(iframe, script.nextSibling);

  // Auto-size: the framed page reports its height; we only ever accept a number from OUR origin.
  window.addEventListener("message", function (e) {
    if (e.origin !== origin) return;
    var m = e.data;
    if (m && m.cobijo === "height" && typeof m.height === "number" && m.height > 0) {
      iframe.style.height = Math.ceil(m.height) + "px";
    }
  });
})();
