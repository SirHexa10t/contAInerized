#!/usr/bin/env bash

# per-user installation
mkdir -p ~/.docker/cli-plugins
curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m) -o ~/.docker/cli-plugins/docker-compose
chmod +x ~/.docker/cli-plugins/docker-compose

sudo usermod -aG docker $USER  # restart to have it take effect, so you won't need to constantly run the command below
newgrp docker

