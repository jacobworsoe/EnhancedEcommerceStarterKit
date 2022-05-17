SELECT * FROM (
    select
        user_pseudo_id,
        (select value.int_value from unnest(event_params) where event_name = 'page_view' and key = 'ga_session_id') as session_id,
        (select value.int_value from unnest(event_params) where event_name = 'page_view' and key = 'ga_session_number') as session_number,
        event_timestamp,
        rank() over (partition by user_pseudo_id, (select value.int_value from unnest(event_params) where event_name = 'page_view' and key = 'ga_session_id') order by event_timestamp) as rank,
        case when (select value.string_value from unnest(event_params) where event_name = 'page_view' and key = 'source') is null then '(direct)' else (select value.string_value from unnest(event_params) where event_name = 'page_view' and key = 'source') end as source,
        case when (select value.string_value from unnest(event_params) where event_name = 'page_view' and key = 'medium') is null then '(none)' else (select value.string_value from unnest(event_params) where event_name = 'page_view' and key = 'medium') end as medium,        
        concat(traffic_source.source,' / ',traffic_source.medium) as source_medium_acquisition               
    
    from `jacobworsoe-dk-app-web.analytics_207959700.events_intraday_20220517`       
    where (select value.int_value from unnest(event_params) where event_name = 'page_view' and key = 'ga_session_id') is not null
)
where rank = 1
AND user_pseudo_id = "1708216931.1631526062"
ORDER BY event_timestamp DESC