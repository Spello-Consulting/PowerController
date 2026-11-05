# Configuration file - Website section

Settings for the built-in web server that provides a web interface to view and control the outputs.

| Key | Description | 
|:--|:--|
| HostingIP | The IP address to host the web server on. Use 0.0.0.0 to listen on all interfaces (the default). |
| Port | The port to host the web server on. |
| PageAutoRefresh | How often, in seconds, to refresh the live data on the web page. The page requests a fresh snapshot over its existing WebSocket connection (it does **not** reload the whole page), so control buttons keep working between refreshes. Set to 0 to disable the periodic refresh (live updates still arrive over the WebSocket whenever state changes). |
| DebugMode | Enable or disable debug mode for the web server (should be False in production). |
| AccessKey | An access key to secure the web interface. Alternatively, set the WEBAPP_ACCESS_KEY environment variable. Leave blank to disable access control. |
