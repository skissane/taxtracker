// Shows UTC times in the browser's time zone, for custom admin pages.
// Each <time data-lt-local datetime="...Z"> gets its time as YYYY-MM-DD HH:MM
// in the browser's time zone (or as e.g. "Fri 02:59" with
// data-lt-local="weekday"), each [data-lt-time-zone] the zone's name,
// and each [data-lt-local-only], hidden in the page so nothing shows empty
// without JavaScript, is shown.
(function () {
  "use strict";

  var WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  function pad(number) {
    return String(number).padStart(2, "0");
  }

  // No seconds: they're the same as the UTC time's, as no time zone in use
  // has an offset that isn't a whole number of minutes.
  function formatTime(date) {
    return pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  function format(date, style) {
    if (style === "weekday") {
      return WEEKDAYS[date.getDay()] + " " + formatTime(date);
    }
    return (
      String(date.getFullYear()).padStart(4, "0") +
      "-" +
      pad(date.getMonth() + 1) +
      "-" +
      pad(date.getDate()) +
      " " +
      formatTime(date)
    );
  }

  function show() {
    var zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
    document.querySelectorAll("time[data-lt-local]").forEach(function (element) {
      var date = new Date(element.dateTime);
      if (!isNaN(date)) {
        element.textContent = format(date, element.dataset.ltLocal);
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
