SELECT
  "jacobworsoe.dk" AS site,
  table_id,
  DATE(TIMESTAMP_MILLIS(creation_time)) date,    
  TIMESTAMP_DIFF(TIMESTAMP_MILLIS(creation_time), TIMESTAMP_TRUNC(TIMESTAMP_MILLIS(creation_time), DAY), MINUTE)/60 AS hours_after_midnight
FROM jacobworsoe-dk-app-web.analytics_207959700.__TABLES__
WHERE table_id NOT LIKE "%intra%"

UNION ALL

SELECT
  "yrsadunvad.dk" AS site,
  table_id,
  DATE(TIMESTAMP_MILLIS(creation_time)) date,    
  TIMESTAMP_DIFF(TIMESTAMP_MILLIS(creation_time), TIMESTAMP_TRUNC(TIMESTAMP_MILLIS(creation_time), DAY), MINUTE)/60 AS hours_after_midnight
FROM yrsadunvad-ga4.analytics_312143367.__TABLES__
WHERE table_id NOT LIKE "%intra%"