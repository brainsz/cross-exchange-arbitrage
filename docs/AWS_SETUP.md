# AWS Remote Development Guide

## Option 1: VS Code Remote - SSH (Recommended)
This is the best way to develop. It allows you to use your local VS Code to edit files and run commands directly on the AWS server.

1.  **Install Extension**: Install the "Remote - SSH" extension in VS Code.
2.  **Connect**:
    *   Click the green icon in the bottom-left corner of VS Code.
    *   Select **Remote-SSH: Connect to Host...**
    *   Select **Add New SSH Host...**
    *   Enter your connection command: `ssh -i /path/to/key.pem ubuntu@YOUR_AWS_IP`
    *   Select the config file to update (usually `~/.ssh/config`).
3.  **Open**: Click "Connect" on the notification or select the host from the list.

## Option 2: Using the Deployment Script
If you prefer to edit locally and just push the code to run it, use the provided `deploy.sh` script.

1.  **Edit `deploy.sh`**:
    Open `deploy.sh` and update the following variables:
    ```bash
    AWS_IP="1.2.3.4"           # Your AWS Public IP
    AWS_USER="ubuntu"          # Usually 'ubuntu' or 'ec2-user'
    KEY_PATH="/path/to/key.pem" # Path to your downloaded .pem file
    ```

2.  **Make Executable**:
    ```bash
    chmod +x deploy.sh
    ```

3.  **Run Deploy**:
    ```bash
    ./deploy.sh
    ```

## Setup on AWS Server
Once connected to the server (via VS Code or Terminal), you need to set up the environment:

1.  **Update System**:
    ```bash
    sudo apt update && sudo apt upgrade -y
    ```

2.  **Install Python & Pip**:
    ```bash
    sudo apt install python3-pip python3-venv -y
    ```

3.  **Create Virtual Environment**:
    ```bash
    cd ~/cross-exchange-arbitrage
    python3 -m venv venv
    source venv/bin/activate
    ```

4.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

5.  **Create .env File**:
    You need to manually create the `.env` file on the server as it is excluded from deployment for security.
    ```bash
    nano .env
    # Paste your environment variables here
    ```
