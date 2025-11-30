#!/bin/bash

# AWS Connection Details
# Replace these with your actual values
AWS_IP="YOUR_AWS_IP_HERE"
AWS_USER="ubuntu" # or ec2-user
KEY_PATH="/path/to/your/key.pem"
REMOTE_DIR="~/cross-exchange-arbitrage"

# Check if key file exists
if [ ! -f "$KEY_PATH" ]; then
    echo "Error: Key file not found at $KEY_PATH"
    echo "Please edit deploy.sh and set the correct KEY_PATH."
    exit 1
fi

# Ensure key has correct permissions
chmod 400 "$KEY_PATH"

echo "🚀 Deploying to $AWS_USER@$AWS_IP..."

# Create remote directory if it doesn't exist
ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no "$AWS_USER@$AWS_IP" "mkdir -p $REMOTE_DIR"

# Sync files using rsync
# Excludes venv, logs, .git, and .env (security)
rsync -avz --progress \
    --exclude 'venv' \
    --exclude 'logs' \
    --exclude '.git' \
    --exclude '.env' \
    --exclude '__pycache__' \
    --exclude '.DS_Store' \
    -e "ssh -i $KEY_PATH" \
    . "$AWS_USER@$AWS_IP:$REMOTE_DIR"

echo "✅ Deployment complete!"
echo "To connect via SSH:"
echo "ssh -i $KEY_PATH $AWS_USER@$AWS_IP"
