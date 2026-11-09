# Maps in the chat

When the agent talks about places, a route or a day plan, the reply carries an interactive map: numbered stops on a map, the route as a line, and the same stops as a list under it, each with a button that copies its coordinates.

Applies to: Pipe (needs the `map` Gateway plugin)

## What you see

- A map with a numbered pin per stop and, when the agent was asked for directions, the route as a line. A multi-day plan gets one colour per day.
- A summary per route: mode (walk, bike or drive), distance and time.
- The stops as a list under the map, in order, with the place the geocoder matched and a **Copy** button for the coordinates. The page has no links: the coordinates are for pasting into your own maps app.
- A stop the geocoder could not find is named under the list instead of being dropped silently.
- On a phone the map needs two fingers to move, so scrolling the chat past it still works with one.
- Without WebGL, or when the map tiles cannot load, a drawing of the stops and the route stays in place of the map.

The map stays in the chat: a reload shows it again.

## How it works

The agent calls the `map` tool. The Gateway plugin looks the stops up (Photon, then Nominatim), routes them only when asked (OSRM on routing.openstreetmap.de), and answers with a map id and the stops. The Pipe reads the whole map from the plugin's `map.get` method and writes it into the reply as one embedded page. All the services are free and need no key; the plugin keeps to their usage policies: one request per second per host, a User-Agent that names a contact, and a cache.

The map is drawn with MapLibre on OpenFreeMap tiles, loaded from a CDN with a pinned version and an integrity hash.

## Set up

1. Add the plugin to the Gateway: `gateway-plugins/map` of the [workspace repository](https://github.com/Eliav2/openclaw-workspace), listed in `plugins.load.paths` and enabled in `plugins.entries.map`.
2. Restart the Gateway.

Without the plugin the Pipe does nothing different. A map the Gateway no longer holds (it keeps each for an hour) is shown only if it was already written to the chat.
