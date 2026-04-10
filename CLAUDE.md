# Tool bootstrap

Before starting work, run a single `ToolSearch` call to pre-fetch all
deferred tools you may need during the session:
`ToolSearch(query: "select:TodoWrite", max_results: 1)`. This avoids
repeated ToolSearch round-trips later.
