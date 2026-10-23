// Shows UTC times in the browser's time zone, for custom admin pages.
// Each <time data-lt-local datetime="...Z"> gets its time as YYYY-MM-DD
// HH:MM:SS in the browser's time zone, each [data-lt-time-zone] the zone's name,
// and each [data-lt-local-only], hidden in the page so nothing shows empty
// without JavaScript, is shown.
(function () {
  "use strict";

  function pad(number) {
    return String(number).padStart(2, "0");
  }

  function format(date) {
    return (
      String(date.getFullYear()).padStart(4, "0") +
      "-" +
      pad(date.getMonth() + 1) +
      "-" +
      pad(date.getDate()) +
      " " +
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes()) +
      ":" +
      pad(date.getSeconds())
    );
  }

  function show() {
    var zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
    document.querySelectorAll("time[data-lt-local]").forEach(function (element) {
      var date = new Date(element.dateTime);
      if (!isNaN(date)) {
        element.textContent = format(date);
      }
    });
    document.querySelectorAll("[data-lt-time-zone]").forEach(function (element) {
      element.textContent = zone;
    });
    document.querySelectorAll("[data-lt-local-only]").forEach(function (element) {
      element.hidden = false;
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", show);
  } else {
    show();
  }
})();
