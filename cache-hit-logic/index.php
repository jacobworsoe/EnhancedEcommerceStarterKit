<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>Cache hit logic</title>    
  </head>
  <body>
    <h1>Cache hit logic</h1>

    <script>
        var phpTimestamp = <?php echo microtime(true); ?>;
        var timestamp_diff = phpTimestamp - (window.performance.timing.navigationStart/1000);
        var cache_hit = "unknown";

        if(timestamp_diff < -3) { cache_hit = "Cache hit"; }
        else if(timestamp_diff >= -3 && timestamp_diff <= 0) { cache_hit = "Page generated less than 3 seconds before request"; }
        else if(timestamp_diff > 0) { cache_hit = "Cache miss"; }    

    </script>

    <script>
      (function (i, s, o, g, r, a, m) {
        i["GoogleAnalyticsObject"] = r;
        (i[r] =
          i[r] ||
          function () {
            (i[r].q = i[r].q || []).push(arguments);
          }),
          (i[r].l = 1 * new Date());
        (a = s.createElement(o)), (m = s.getElementsByTagName(o)[0]);
        a.async = 1;
        a.src = g;
        m.parentNode.insertBefore(a, m);
      })(
        window,
        document,
        "script",
        "//www.google-analytics.com/analytics.js",
        "ga"
      );

      ga("create", "UA-11679419-2", "auto");

      if (typeof cache_hit !== "undefined") {
        ga('set', 'dimension4', cache_hit);
      }
      
      ga("send", "pageview");
    </script>
    

  </body>
</html>
