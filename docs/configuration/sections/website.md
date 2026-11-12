# Configuration file - Website section

Settings for the built-in web server that provides a web interface to view and control the outputs.

| Key | Description | 
|:--|:--|
| HostingIP | The IP address to host the web server on. Use 0.0.0.0 to listen on all interfaces (the default). |
| Port | The port to host the web server on. |
| PageAutoRefresh | How often, in seconds, to refresh the live data on the web page. The page requests a fresh snapshot over its existing WebSocket connection (it does **not** reload the whole page), so control buttons keep working between refreshes. Set to 0 to disable the periodic refresh (live updates still arrive over the WebSocket whenever state changes). |
| DebugMode | Enable or disable debug mode for the web server (should be False in production). |
| AccessKey | An access key to secure the web interface. Alternatively, set the WEBAPP_ACCESS_KEY environment variable. Leave blank to disable access control. |

## Current Price field

Each output card shows a **Current Price** for electricity *right now*, together with a small
label naming the basis the price comes from:

- **Amber &lt;channel&gt;** — the live Amber price for the output's channel, used when the
  output is in `BestPrice` mode. This is the real current price and may be zero or even
  negative (for example when solar is exporting), in which case the negative value is shown
  as-is rather than hidden.
- **&lt;schedule&gt; schedule** — the price from the output's operating schedule, used when the
  output is in `Schedule` mode, or as a fallback when Amber price data is unavailable.
- **Default price** — the configured `General.DefaultPrice`, used only when neither an Amber
  price nor a schedule price is available.

Because the basis depends on each output's `Mode`, `AmberChannel` and `Schedule`, two outputs
can legitimately show different Current Prices; the basis label makes clear why. Outputs that
share the same basis always show the same Current Price. (The **Forecast Price** still differs
per output, as it reflects each output's own run plan.)

## Mobile summary page

The home page has a **📱 Mobile view** link that opens a compact, phone-optimised summary at
`/mobile`. It is designed to be read at a glance on a device such as an iPhone, with the text
scaled up to fill the screen and (for a typical number of probes and outputs) fit on a single
page without scrolling. The page is **view-only** — it has no control buttons — and it updates
live over the same WebSocket connection as the home page.

It shows:

- **Temperatures** — the latest reading for every probe in the `TempProbeLogging` section. The
  last-reading time is not shown. A reading that is out of date (older than
  `TempProbeLogging.LastReadingWithinMinutes`) is shown as **N/A**.
- **Outputs** — every output that is not hidden (`Outputs[#].HideFromWebApp` is not `True`),
  each with a coloured dot showing its state — **green** when on, **black/grey** when off —
  and that output's **Current Price** (on the output's own pricing basis, as described above).

The link and the page carry the `?key=` access key when one is configured.
