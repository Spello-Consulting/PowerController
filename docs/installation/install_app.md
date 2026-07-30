# Installing the PowerController Application

The application installation instructions are generally the same for Linux, macOS and CentOS (RaspberryPi). Start a new terminal window on your target system and select parent folder that we'll create the installation folder in. In our example below, we'll use _~/apps_ folder for our user nick (_/home/nick/apps_)

## Step 1: Validate that the prerequisites are properly installed

Run the following commands. If any of them fail, or the python version is less than 3.13.0 please go back and review the [prerequisite installation](prerequisites.md) section again.

```bash
python3 --version
git --version
uv --version
```

## Step 2: Download the app from GitHub

Create a folder for the app
```bash
cd ~/apps
mkdir PowerController
cd PowerController
```

Clone the app from GitHub
```bash
git clone https://github.com/Spello-Consulting/PowerController.git .
```

Use UV to initialise the application and download all the supporting libraries
```bash
uv sync
```

Watch the uv sync command and make sure there are no errors reported. If all's well, activate the Python virtual environment and make sure we're using the right version of Python:
```bash
source .venv/bin/activate
uv python pin 3.13
```

## Step 3: Create the default config file

An example configuration file has been provided for you in the application's root folder (**config_example.yaml**). Use this as a starting point for your config file:

```bash
cp config_example.yaml config.yaml
```

## Step 4: Create a .env file for secure credentials

If you plan on using the following features:
- Integration with Amber Electric for energy pricing
- Email notification of application errors or excessive energy use
- Secure integration with the PowerControllerViewer app
- Stronger protection for the PowerController web application
- Tesla charging integration

Then we recommend creating a .env file to hold passwords and API keys. For now, let's create a file and store your email account SMTP username and password:

```bash
nano .env


# In nano, create the following entries, save and exit:
SMTP_USERNAME=<SMTP username here>
SMTP_PASSWORD=<SMTP password here>
```

You're now ready to edit config.yaml and setup the configuration for your installation.

# Next Step >> [Configure the App](../configuration/index.md)