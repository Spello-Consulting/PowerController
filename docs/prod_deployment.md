# Production Deployment

Now that you have the app installed and [tested from the command line](running.md) it's time to set this up properly to run in a production environment. In this setup we will configure the PowerController app (and its web interface if configured) to run via system daemon.

Note: These instructions shoudl work for Linux (Ubuntu tested), CentOS (RaspberryPi) and macOS environments. Windows installations will be documented at a later date.

## 1. Create a service file

Create a new service file and edit it to review:
```bash
sudo cp deploy/PowerController.service /etc/systemd/system/PowerController.service

sudo nano /etc/systemd/system/PowerController.service
```

Key options to review:

- **ExecStart**: Change the path to suit your installation.
- **WorkingDirectory**: Change the path to suit your installation.
- **User**: Change the username to suit your installation.
- **Environment**: Change the path to suit your installation.
- **Restart=on-failure**: restart if the script exits with a non-zero code.
- **RestartSec=5**: wait 5 seconds before restarting.
- **StandardOutput=journal**: logs go to journalctl.


## 2. Enable and start the service

Run each of the commands in turn to enable and start the service. 

```bash
sudo systemctl daemon-reexec       # re-executes systemd in case of changes
sudo systemctl daemon-reload       # reload service files
sudo systemctl enable PowerController   # enable on boot
sudo systemctl start PowerController    # start now
```

## 3. View the logs

First view the application log to make sure it's running OK:

```bash
cd /home/nick/scripts/PowerController
tail -f logfile.log
```

If there are any problems, check the system logs

```bash
sudo systemctl status PowerController.service
sudo journalctl -u PowerController -f
```

## 4. View the website

If you have the web app enabled, check that you can access, ideally from another device on your local network. If you can't, make sure there are no firewall rules on the machine running that app that block access. 

