# Configuration file - General section

General settings for the Power Controller application.

| Key | Description | 
|:--|:--|
| Label | The label (name) that your installation. Used in the email subject and web viewer app |
| PollingInterval | Number of seconds to sleep between each check of the run plan and possible changes to outputs. Recommended 30 seconds or more. |
| TempProbeAveragingWindow | Optional rolling-average window, in seconds, for temperature probe readings. When greater than zero, every use of a probe temperature (output `TempProbeConstraints`, `TempProbeLogging`, and the viewer/data API) uses the trailing average over this many seconds instead of the instantaneous reading. This smooths brief temperature spikes that would otherwise immediately flip an output. Set to `0`, leave blank, or omit to disable (point-in-time readings are used). |
| ReportCriticalErrorsDelay | Some critical errors can trigger email notifications - for example the AmberAPI not responding. This is the time in minutes for an issue to persist before we send an email notification. Leave blank to disable. |
| PrintToConsole | Print some basic information to the console during startup and operation |
| DefaultPrice | A default price to use if the Amber API is not available and there is no schedule price defined |
| CurrencySymbol | The character to use for the "major" denomination of your currency, for example "$" |
| SubunitSymbol | The character to use for the "minor" denomination of your currency (i.e. 1/100th of the major unit), for example "¢" |